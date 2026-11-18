#!/usr/bin/env python3
"""Local account-authorized control of the fixed Wi-Fi setup unit."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import socket
import socketserver
import struct
import threading
import time
import uuid

try:
    from host.wifi_network import NetworkError, private_write, run
except ModuleNotFoundError:
    from wifi_network import NetworkError, private_write, run

DATA = Path('/var/lib/family-radio-wifi')
STATUS = Path('/run/family-radio-wifi-status')
RUNTIME = Path('/run/family-radio-wifi')
SOCKET = '/run/family-radio-wifi-radio/control.sock'
UNIT = 'family-radio-wifi.service'
IDENTITY = re.compile('[0-9a-f]{32}')
STATES = {'idle', 'starting', 'ready', 'connecting', 'failed', 'connected',
          'stopping', 'cancelled', 'expired', 'recovery_failed', 'persistence_failed', 'error'}
ACTIVE = {'starting', 'ready', 'connecting', 'failed', 'stopping'}


def read_json(path, maximum=4096):
    with Path(path).open('rb') as source:
        data = source.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError('Invalid private setup record.')
    return json.loads(data)


def checked_status(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'setup_id', 'revision', 'state', 'deadline', 'error', 'terminal', 'ap_up'}
            or value['version'] != 1 or not isinstance(value['setup_id'], str)
            or not IDENTITY.fullmatch(value['setup_id']) or type(value['revision']) is not int
            or not 1 <= value['revision'] <= 2147483647 or value['state'] not in STATES
            or type(value['deadline']) not in (int, float) or not 0 <= value['deadline'] < 1e12
            or value['error'] not in ('-', 'NETWORK', 'RECOVERY', 'PERSISTENCE', 'CANCELLED')
            or type(value['terminal']) is not bool or type(value['ap_up']) is not bool):
        raise ValueError('Invalid private setup status.')
    return value


def checked_access(value):
    if (not isinstance(value, dict) or set(value) != {'setup_id', 'ssid', 'password'}
            or not isinstance(value['setup_id'], str) or not IDENTITY.fullmatch(value['setup_id'])
            or not isinstance(value['ssid'], str) or not 1 <= len(value['ssid'].encode('utf-8')) <= 32
            or any(ord(c) < 32 or ord(c) == 127 for c in value['ssid'])
            or not isinstance(value['password'], str) or not 8 <= len(value['password']) <= 63
            or any(not 32 <= ord(c) <= 126 for c in value['password'])):
        raise ValueError('Invalid live setup access configuration.')
    return value


class Broker:
    def __init__(self, directory=DATA, status_directory=STATUS, runtime=RUNTIME,
                 runner=run, clock=time.monotonic, pause=time.sleep):
        self.directory, self.status_directory, self.runtime = Path(directory), Path(status_directory), Path(runtime)
        self.run, self.clock, self.pause = runner, clock, pause

    def unit(self):
        text = self.run(['systemctl', 'show', UNIT, '-p', 'ActiveState', '-p', 'MainPID',
                         '-p', 'ControlPID', '-p', 'Job', '-p', 'InvocationID'], timeout=3)
        fields = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        if (fields.get('ActiveState') not in ('active', 'activating', 'deactivating', 'inactive', 'failed')
                or not fields.get('MainPID', '').isdigit() or not fields.get('ControlPID', '').isdigit()
                or 'Job' not in fields or 'InvocationID' not in fields
                or fields['InvocationID'] and not IDENTITY.fullmatch(fields['InvocationID'])):
            raise NetworkError('Setup service state is unavailable.')
        return fields

    @staticmethod
    def busy(unit):
        return (unit['ActiveState'] not in ('inactive', 'failed') or unit['MainPID'] != '0'
                or unit['ControlPID'] != '0' or bool(unit['Job']))

    def _status(self):
        try:
            return checked_status(read_json(self.status_directory / 'status.json')).copy()
        except FileNotFoundError:
            return None

    def view(self, unit=None, prepare=False):
        unit = self.unit() if unit is None else unit
        status = self._status()
        busy = self.busy(unit)
        queued = busy and unit['ActiveState'] in ('inactive', 'failed')
        if queued:
            identity = None
        elif busy:
            identity = unit['InvocationID'] or (status['setup_id'] if status and unit['ActiveState']=='deactivating' else None)
        else:
            identity = status['setup_id'] if status else None
        if busy:
            if not identity or not status or status['setup_id'] != identity:
                status = dict(setup_id=identity, revision=0, state='starting', deadline=self.clock()+600, error='-')
            if unit['ActiveState'] == 'deactivating':
                status.update(state='stopping', error='-')
        elif (self.directory / 'session.json').exists():
            status = dict(setup_id=status['setup_id'] if status else None, revision=status['revision'] if status else 0,
                          state='recovery_failed', deadline=0, error='RECOVERY')
        elif prepare or status is None:
            status = dict(setup_id=None, revision=0, state='idle', deadline=0, error='-')
        elif status['state'] in ACTIVE:
            status.update(state='cancelled', error='CANCELLED')
        result = {k: status[k] for k in ('setup_id', 'revision', 'state', 'error')}
        result.update(version=1, terminal=status.get('terminal', not busy),
                      remaining=max(0, min(600, int(status['deadline'] - self.clock()))))
        return result

    def _ledger(self):
        try:
            value = read_json(self.status_directory / 'requests.json', 32768)
        except FileNotFoundError:
            return []
        if not isinstance(value, list) or len(value) > 64:
            raise ValueError('Invalid setup request ledger.')
        for row in value:
            if (not isinstance(row, dict) or set(row) != {'request_id', 'setup_id', 'created', 'failed'}
                    or not IDENTITY.fullmatch(row['request_id'])
                    or row['setup_id'] is not None and not IDENTITY.fullmatch(row['setup_id'])
                    or type(row['created']) not in (int, float) or type(row['failed']) is not bool):
                raise ValueError('Invalid setup request ledger.')
        return value

    def _save_ledger(self, value):
        private_write(self.status_directory / 'requests.json', json.dumps(value))

    def request(self, request):
        if not isinstance(request, dict) or request.get('op') not in ('prepare', 'start', 'status', 'stop', 'credential'):
            raise ValueError('Unsupported setup operation.')
        op = request['op']
        expected = {'op'} if op == 'prepare' else {'op', 'request_id'} if op == 'start' else {'op', 'setup_id'}
        if set(request) != expected:
            raise ValueError('Unsupported setup fields.')
        if op != 'prepare':
            identity = request['request_id' if op == 'start' else 'setup_id']
            if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
                raise ValueError('Invalid setup identity.')
        with (self.status_directory / 'broker.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            unit = self.unit()
            if op in ('prepare', 'status', 'credential'):
                result = self.view(unit, prepare=op == 'prepare')
                if op != 'prepare' and result['setup_id'] != request['setup_id']:
                    raise ValueError('Setup session changed.')
                if op == 'credential':
                    if (unit['ActiveState'] != 'active' or result['state'] not in ('ready', 'failed')
                            or result['terminal'] or not self._status()['ap_up']):
                        raise ValueError('Setup access is not ready.')
                    access = checked_access(read_json(self.runtime / 'access.json'))
                    if access['setup_id'] != result['setup_id']:
                        raise ValueError('Setup session changed.')
                    return dict(result, access={k: access[k] for k in ('ssid', 'password')})
                return result
            if op == 'start':
                ledger = self._ledger()
                previous = next((r for r in ledger if r['request_id'] == request['request_id']), None)
                if previous:
                    result = self.view(unit)
                    if previous['setup_id'] and result['setup_id'] == previous['setup_id']:
                        return result
                    return dict(version=1, setup_id=previous['setup_id'], revision=0, state='error',
                                remaining=0, terminal=True, error='START')
                if self.busy(unit) or (self.directory / 'session.json').exists():
                    raise ValueError('Setup is already running or recovering.')
                # Never evict a recent or active nonce to admit another start.
                ledger = [r for r in ledger if self.clock()-r['created'] < 86400 or r['setup_id'] == unit['InvocationID']]
                if len(ledger) >= 64:
                    raise ValueError('Too many setup requests. Try again later.')
                entry = dict(request_id=request['request_id'], setup_id=None, created=self.clock(), failed=True)
                ledger.append(entry)
                self._save_ledger(ledger)
                old = unit['InvocationID']
                try:
                    self.run(['systemctl', 'start', '--no-block', UNIT], timeout=3)
                    for _ in range(20):
                        unit = self.unit()
                        if self.busy(unit) and unit['InvocationID'] and unit['InvocationID'] != old:
                            entry.update(setup_id=unit['InvocationID'], failed=False)
                            self._save_ledger(ledger)
                            return self.view(unit)
                        self.pause(.05)
                except NetworkError:
                    pass
                # A delayed start must not activate the AP after a final error.
                try:
                    self.run(['systemctl', 'stop', '--no-block', UNIT], timeout=3)
                except NetworkError:
                    return dict(version=1, setup_id=None, revision=0, state='recovery_failed',
                                remaining=0, terminal=True, error='RECOVERY')
                return dict(version=1, setup_id=None, revision=0, state='error', remaining=0, terminal=True, error='START')
            current = self.view(unit)
            if current['setup_id'] != request['setup_id']:
                raise ValueError('Setup session changed.')
            if self.busy(unit):
                # A queued new invocation without an ID must never be stopped
                # using an old completed session's identity.
                if unit['ActiveState']=='deactivating':
                    return current
                if unit['ActiveState'] not in ('activating', 'active') or unit['InvocationID'] != request['setup_id']:
                    raise ValueError('Setup invocation is not yet available.')
                self.run(['systemctl', 'stop', '--no-block', UNIT], timeout=3)
                current.update(state='stopping', error='-')
            return current


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 4

    def __init__(self, address, broker, radio_uid, inherited=None):
        self.broker, self.radio_uid = broker, radio_uid
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(address, Handler, bind_and_activate=inherited is None)
        if inherited is not None:
            self.socket.close()
            self.socket = inherited

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        super().process_request(request, address)

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid not in (0, self.server.radio_uid):
            return
        self.request.settimeout(2)
        def close():
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(4, close)
        timer.start()
        try:
            line = self.rfile.readline(2049)
            if len(line) > 2048 or not line.endswith(b'\n'):
                raise ValueError
            result = self.server.broker.request(json.loads(line))
        except (OSError, ValueError, KeyError, TypeError, RecursionError, NetworkError):
            result = dict(error='Setup request was not accepted.')
        finally:
            timer.cancel()
        try:
            self.wfile.write(json.dumps(result).encode()+b'\n')
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--radio-user', default='family-radio')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('The setup broker must run as root.')
    if os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1':
        parser.error('Start this broker through its systemd socket.')
    inherited = socket.socket(fileno=3)
    with Server(SOCKET, Broker(), pwd.getpwnam(args.radio_user).pw_uid, inherited) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
