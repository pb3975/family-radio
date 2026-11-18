#!/usr/bin/env python3
"""Root-only manual setup session. HTTP gets only status, networks and join."""
import argparse
import grp
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import socketserver
import struct
import threading
import time
import uuid

try:
    from host.wifi_broker import STATUS, RUNTIME, IDENTITY
except ModuleNotFoundError:
    from wifi_broker import STATUS, RUNTIME, IDENTITY

try:
    from host.wifi_network import AP_UUID, NetworkError, NetworkManager, private_write, keyfile, run
    from host.wifi_portal import validate_join
except ModuleNotFoundError:
    from wifi_network import AP_UUID, NetworkError, NetworkManager, private_write, keyfile, run
    from wifi_portal import validate_join

DATA = Path('/var/lib/family-radio-wifi')
SOCKET = '/run/family-radio-wifi/control.sock'
SESSION_SECONDS = 600


class Session:
    def __init__(self, network, directory=DATA, runner=run, clock=time.monotonic, pause=time.sleep,
                 runtime=RUNTIME, status_directory=STATUS, setup_id=None, stop_event=None):
        self.network, self.directory, self.run, self.clock = network, Path(directory), runner, clock
        self.pause = pause
        self.runtime, self.status_directory = Path(runtime), Path(status_directory)
        self.setup_id = setup_id or uuid.uuid4().hex
        if not IDENTITY.fullmatch(self.setup_id):
            raise ValueError('Invalid setup invocation.')
        self.stop_event = stop_event or threading.Event()
        self.cancel_intent = False
        self.revision, self.ap_up = 0, False
        self.status_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.state, self.ssid, self.message = 'starting', '', 'Choose a network to begin.'
        self.networks, self.deadline, self.finished = [], self.clock() + SESSION_SECONDS, False
        self.journal = self.directory / 'session.json'
        self.record = None

    def save(self, record=None):
        value = self.record if record is None else record
        private_write(self.journal, json.dumps(value))
        self.record = value

    def services(self, action):
        self.run(['systemctl', action, 'family-radio-wifi-dns.service', 'family-radio-wifi-http.service'])

    def firewall(self, enable):
        if enable:
            self.run(['nft', '-f', '/usr/local/lib/family-radio/deploy/wifi/isolation.nft'])
        else:
            tables = self.run(['nft', 'list', 'tables'])
            if 'table inet family_radio_setup' in tables.splitlines():
                self.run(['nft', 'delete', 'table', 'inet', 'family_radio_setup'])

    def publish(self, state=None, *, terminal=False, error='-'):
        try:
            old = json.loads((self.status_directory / 'status.json').read_text())
            if old.get('setup_id') == self.setup_id:
                self.revision = max(self.revision, old['revision'])
        except FileNotFoundError:
            pass
        self.revision += 1
        value = dict(version=1, setup_id=self.setup_id, revision=self.revision,
                     state=state or self.state, deadline=self.deadline, error=error,
                     terminal=terminal, ap_up=self.ap_up)
        private_write(self.status_directory / 'status.json', json.dumps(value))

    def cancelled(self):
        return self.stop_event.is_set() or self.clock() >= self.deadline

    def start(self):
        self.recover(publish=False)
        self.state = 'starting'
        self.publish()
        self.network.preflight()
        if 'table inet family_radio_setup' in self.run(['nft', 'list', 'tables']).splitlines():
            raise NetworkError('The setup firewall table already exists without a recovery record.')
        original = self.network.active()
        self.networks = self.network.scan()
        access = json.loads((self.directory / 'access.json').read_text())
        ssid = access['ssid']
        if not isinstance(ssid, str) or not 1 <= len(ssid.encode('utf-8')) <= 32 or any(ord(c)<32 or ord(c)==127 for c in ssid):
            raise ValueError('Invalid setup network name.')
        credentials = dict(setup_id=self.setup_id, ssid=ssid, password=secrets.token_urlsafe(18))
        self.runtime.mkdir(mode=0o750, parents=True, exist_ok=True)
        private_write(self.runtime / 'access.json', json.dumps(credentials))
        self.ssid = ''
        self.record = dict(version=2, setup_id=self.setup_id, original=original, candidate=None,
                           committed=False, autoconnect=self.network.autoconnect(), deadline=self.deadline)
        self.save()
        try:
            if self.cancelled():
                return
            self.network.set_autoconnect(False)
            self.network.load(AP_UUID, keyfile(AP_UUID, ssid, 'wpa2', credentials['password'], ap=True))
            self.firewall(True)
            if self.cancelled():
                return
            self.network.activate(AP_UUID)
            self.ap_up = True
            if self.cancelled():
                return
            self.services('start')
            self.network.setup_ready()
            if self.cancelled():
                return
            self.state = 'ready'
            self.publish()
        except Exception:
            if not self.cancelled():
                self.state = 'error'
            raise

    def status(self):
        return dict(version=3, demo=False, state=self.state, ssid=self.ssid, message=self.message)

    def join(self, data):
        ssid, security, password = validate_join(data)
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if self.finished or self.cancelled() or self.clock() + 60 >= self.deadline:
                raise ValueError('The setup session has ended.')
            if self.state not in ('ready', 'failed'):
                return False
            self.state, self.ssid = 'connecting', ssid
            self.message = 'Setup Wi-Fi will disconnect. Rejoin your chosen network, or return to setup Wi-Fi if joining fails.'
            self.publish()
            worker = threading.Thread(target=self._join, args=(ssid, security, password), daemon=True)
            worker.start()
            return True
        finally:
            self.lock.release()

    def _join(self, ssid, security, password):
        # stop/recovery waits for the bounded NetworkManager operation. It cannot
        # restore the old network while a late activation is still being issued.
        with self.lock:
            if self.finished:
                return
            if self.cancelled() or self.clock() + 60 >= self.deadline:
                self.state, self.message = 'failed', 'The setup session has ended.'
                self.finished = True
                return
            identity = str(uuid.uuid4())
            self.record['candidate'] = identity
            try:
                self.save()
                self.network.load(identity, keyfile(identity, ssid, security, password))
                self.pause(2)  # Let the bounded HTTP acknowledgement leave the AP.
                self.services('stop')
                self.network.deactivate(AP_UUID)
                self.ap_up = False
                self.firewall(False)
                self.network.activate(identity)
                if self.cancelled():
                    raise NetworkError('The setup session expired during joining.')
                self.network.persist(identity, keyfile(identity, ssid, security, password, persistent=True))
                self.save(dict(self.record, committed=True))
                self.network.finish_commit(identity)
                self.state, self.message = 'connected', 'Wi-Fi connected and saved. Internet access was not tested.'
                self.finished = True
            except Exception:
                try:
                    disk_record = json.loads(self.journal.read_text())
                except (OSError, ValueError):
                    self.state, self.message = 'persistence_failed', 'Setup state could not be read. Use the physical recovery procedure.'
                    self.finished = True
                    return
                if disk_record['committed']:
                    self.record = disk_record
                    self.state, self.message = 'persistence_failed', 'Wi-Fi joined but saving needs recovery. Do not start another setup session.'
                    self.finished = True
                    return
                self.state, self.message = 'failed', 'Could not join. Return to setup Wi-Fi and check the network name and password.'
                try:
                    self._discard_candidate()
                    if self.cancelled():
                        self.finished = True
                        return
                    self.firewall(True)
                    self.network.activate(AP_UUID)
                    self.ap_up = True
                    if self.cancelled():
                        self.finished = True
                        return
                    self.services('start')
                    self.network.setup_ready()
                    self.publish(error='NETWORK')
                except Exception:
                    self.state, self.message = 'recovery_failed', 'Setup recovery failed. Use the documented physical recovery procedure.'
                    self.finished = True
            finally:
                password = None

    def _discard_candidate(self):
        candidate = self.record.get('candidate')
        if candidate:
            self.network.discard(candidate)
            # A failed persistence step may have written a file before reload.
            self.network.path(candidate, persistent=True).unlink(missing_ok=True)
            self.record['candidate'] = None
            self.save()

    def recover(self, publish=True):
        if not self.journal.exists():
            return
        record = json.loads(self.journal.read_text())
        legacy = isinstance(record, dict) and record.get('version') == 1
        common = {'version', 'original', 'candidate', 'committed', 'autoconnect'}
        expected = common if legacy else common | {'setup_id', 'deadline'}
        if (not isinstance(record, dict) or set(record) != expected
                or record['version'] not in (1, 2) or type(record['committed']) is not bool
                or type(record['autoconnect']) is not bool
                or legacy and not isinstance(record['original'], str)
                or not legacy and (not isinstance(record['setup_id'], str)
                    or not IDENTITY.fullmatch(record['setup_id'])
                    or type(record['deadline']) not in (int, float) or not 0 <= record['deadline'] < 1e12)):
            raise NetworkError('Invalid setup recovery record. Manual recovery is required.')
        original = str(uuid.UUID(record['original'])) if record['original'] is not None else None
        candidate = str(uuid.UUID(record['candidate'])) if record['candidate'] else None
        if (original == AP_UUID or candidate in (original, AP_UUID) and candidate is not None
                or record['committed'] and candidate is None):
            raise NetworkError('Invalid setup recovery ownership.')
        if legacy:
            # The old private journal owns the same UUIDs. Migrate only after
            # validating them, before attempting idempotent cleanup.
            record = dict(record, version=2, setup_id=self.setup_id, deadline=self.deadline)
            self.save(record)
        self.record = record
        failures = []
        unavailable = object()
        network_unavailable = False

        def attempt(operation):
            try:
                return operation()
            except Exception:
                failures.append(True)
                return unavailable

        attempt(lambda: self.services('stop'))
        attempt(lambda: self.network.deactivate(AP_UUID))
        attempt(lambda: self.network.discard(AP_UUID))
        active = attempt(self.network.active)
        if active is not unavailable and active != AP_UUID:
            self.ap_up = False
            attempt(lambda: self.firewall(False))
        else:
            failures.append(True)
        try:
            if record['committed']:
                if not candidate:
                    raise NetworkError('Invalid committed setup recovery record.')
                attempt(lambda: self.network.finish_commit(candidate))
            else:
                attempt(self._discard_candidate)
                if original is not None:
                    try:
                        self.network.restore(original)
                    except NetworkError:
                        # An unreachable saved network is not unfinished owned cleanup.
                        network_unavailable = True
        finally:
            attempt(lambda: self.network.set_autoconnect(record['autoconnect']))
            active = attempt(self.network.active)
            if active is not unavailable and active != AP_UUID:
                self.ap_up = False
                attempt(lambda: self.firewall(False))
        if failures:
            self.state = 'persistence_failed' if record['committed'] else 'recovery_failed'
            if publish:
                self.setup_id = record['setup_id']
                self.deadline = record['deadline']
                self.publish(terminal=True, error='PERSISTENCE' if record['committed'] else 'RECOVERY')
            raise NetworkError('Setup recovery is incomplete. Use the documented recovery procedure.')
        self.journal.unlink()
        (self.runtime / 'access.json').unlink(missing_ok=True)
        if publish:
            self.setup_id = record['setup_id']
            self.deadline = record['deadline']
            terminal = 'connected' if record['committed'] else 'expired' if self.clock() >= self.deadline else 'cancelled' if self.cancel_intent else 'error' if self.state in ('error', 'recovery_failed', 'persistence_failed') else 'cancelled'
            self.state = terminal
            self.publish(terminal=True, error='NETWORK' if network_unavailable or terminal == 'error' else '-')

    def stop(self, cancel=True):
        self.cancel_intent = self.cancel_intent or cancel
        self.stop_event.set()
        with self.lock:
            self.finished = True
            if self.journal.exists():
                self.recover()
            elif self.state in ('starting', 'error'):
                self.ap_up = False
                self.publish('error' if self.state == 'error' and not self.cancel_intent else 'cancelled', terminal=True, error='NETWORK' if self.state == 'error' and not self.cancel_intent else '-')


class Control(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 4

    def __init__(self, address, session, allowed_uid):
        self.session, self.allowed_uid = session, allowed_uid
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(address, Handler)

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
        self.request.settimeout(3)
        _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid not in (0, self.server.allowed_uid):
            return
        try:
            data = self.rfile.readline(4097)
            if len(data) > 4096 or not data.endswith(b'\n'):
                return
            request = json.loads(data)
            if request == {'op': 'status'}:
                result = self.server.session.status()
            elif request == {'op': 'networks'}:
                result = dict(version=3, demo=False, networks=self.server.session.networks)
            elif isinstance(request, dict) and set(request) == {'op', 'join'} and request['op'] == 'join':
                result = dict(accepted=self.server.session.join(request['join']))
            else:
                result = dict(error='Request not supported.')
        except (ValueError, OSError, RecursionError):
            result = dict(error='Setup request failed.')
        self.wfile.write(json.dumps(result).encode() + b'\n')


def main():
    import pwd
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('init', 'credentials', 'serve', 'recover'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run this fixed-purpose command as root.')
    if args.command == 'init':
        DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (DATA / 'access.json').exists():
            private_write(DATA / 'access.json', json.dumps(dict(ssid='Family-Radio-' + secrets.token_hex(2))))
        else:
            value = json.loads((DATA / 'access.json').read_text())
            private_write(DATA / 'access.json', json.dumps(dict(ssid=value['ssid'])))
        return
    if args.command == 'credentials':
        value = json.loads((RUNTIME / 'access.json').read_text())
        print('Setup network:', value['ssid'])
        print('Setup password:', value['password'])
        return
    stop = threading.Event()
    session = Session(NetworkManager(), setup_id=os.environ.get('INVOCATION_ID') or None, stop_event=stop)
    if args.command == 'recover':
        session.recover()
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    Path(SOCKET).unlink(missing_ok=True)
    portal = pwd.getpwnam('family-wifi')
    with Control(SOCKET, session, portal.pw_uid) as server:
        os.chown(SOCKET, 0, grp.getgrnam('family-wifi').gr_gid)
        os.chmod(SOCKET, 0o660)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            if stop.wait(5):
                return
            try:
                session.start()
            except Exception:
                if not session.cancelled():
                    session.state = 'error'
                raise
            while not stop.wait(.5):
                if session.finished or session.clock() >= session.deadline:
                    break
        finally:
            session.stop(cancel=stop.is_set())
            server.shutdown()
            Path(SOCKET).unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, NetworkError, RecursionError):
        raise SystemExit('Wi-Fi setup command failed. Check the service status.')
