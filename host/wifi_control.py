"""Asynchronous radio-side Wi-Fi control. Credentials never enter player state."""
from concurrent.futures import ThreadPoolExecutor, CancelledError
import hashlib
import json
import re
import socket
import time

SOCKET = '/run/family-radio-wifi-radio/control.sock'
ID = re.compile('[0-9a-f]{32}')
STATES = {'idle', 'starting', 'ready', 'connecting', 'failed', 'connected', 'stopping',
          'cancelled', 'expired', 'recovery_failed', 'persistence_failed', 'error'}
ERRORS = {'-', 'NETWORK', 'RECOVERY', 'PERSISTENCE', 'CANCELLED', 'START', 'UNAVAILABLE'}


def request_broker(request, path=SOCKET):
    deadline = time.monotonic()+4
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(path)
        connection.sendall(json.dumps(request).encode()+b'\n')
        data = bytearray()
        while not data.endswith(b'\n') and len(data) <= 4096:
            connection.settimeout(max(.01, deadline-time.monotonic()))
            chunk = connection.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        if len(data)>4096 or not data.endswith(b'\n'):
            raise OSError('Setup broker is unavailable.')
        value = json.loads(data)
        if 'error' in value and 'state' not in value:
            raise ValueError('Setup request was rejected.')
        return checked_view(value)


def checked_view(value):
    fields = {'version', 'setup_id', 'revision', 'state', 'remaining', 'error', 'terminal'}
    if (not isinstance(value, dict) or set(value) not in (fields, fields|{'access'})
            or value['version'] != 1 or value['setup_id'] is not None and (not isinstance(value['setup_id'], str) or not ID.fullmatch(value['setup_id']))
            or type(value['revision']) is not int or not 0 <= value['revision'] <= 2147483647
            or value['state'] not in STATES or type(value['remaining']) is not int or not 0 <= value['remaining'] <= 600
            or value['error'] not in ERRORS or type(value['terminal']) is not bool):
        raise ValueError('Invalid setup response.')
    if 'access' in value:
        access = value['access']
        if (value['state'] not in ('ready', 'failed') or value['terminal']
                or not isinstance(access, dict) or set(access) != {'ssid', 'password'}
                or not isinstance(access['ssid'], str) or not 1 <= len(access['ssid'].encode('utf-8')) <= 32
                or any(ord(c)<32 or ord(c)==127 for c in access['ssid'])
                or not isinstance(access['password'], str) or not 8 <= len(access['password']) <= 63
                or any(not 32 <= ord(c) <= 126 for c in access['password'])):
            raise ValueError('Invalid setup access response.')
    return value


def wifi_payload(ssid, password):
    def escaped(value):
        return ''.join('\\'+c if c in '\\;,:"' else c for c in value)
    return 'WIFI:T:WPA;S:'+escaped(ssid)+';P:'+escaped(password)+';;'


def empty(state='idle', error='-'):
    return dict(version=1, setup_id=None, revision=0, state=state, remaining=0, terminal=True, error=error)


class WifiControl:
    """One in-flight operation; a pending cancel is never displaced by a poll."""
    def __init__(self, rpc=request_broker, clock=time.monotonic):
        self.rpc, self.clock = rpc, clock
        self.last_activity = 0
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wifi-control')
        self.future = None
        self.job = None
        self.queued = None
        self.epoch = 0
        self.session = ''
        self.request_id = self.completed_request = 0
        self.view = empty()
        self.open = False
        self.setup_id = None

    def close(self):
        self.epoch += 1
        self.session = ''
        self.request_id = self.completed_request = 0
        self.queued = None
        self.view = empty()
        self.setup_id = None
        self.open = False
        if self.future:
            self.future.cancel()

    def shutdown(self):
        self.close()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def action(self, session, request_id, action, setup_id):
        if session != self.session:
            self.close()
            self.session = session
        if request_id < self.request_id or request_id == self.request_id:
            return  # Retransmit the cached view, never a privileged operation.
        if action not in ('P', 'S', 'Q', 'T', 'X'):
            return
        if action == 'X' and (self.queued and self.queued[1] == 'T' or self.job and self.job[2] == 'T'):
            self.view.pop('access', None)
            return
        self.request_id = request_id
        if action == 'X':
            self.close()
            return
        self.open = True
        self.last_activity = self.clock()
        if action != 'Q':
            self.view.pop('access', None)
        self.queued = (request_id, action, setup_id)
        if action == 'T':
            self.view.update(state='stopping', terminal=False)
        self.tick()

    def _call(self, action, setup_id, nonce):
        if action == 'S':
            result = self.rpc(dict(op='start', request_id=nonce))
        elif action == 'T':
            if not setup_id:
                return empty('error', 'START')
            result = self.rpc(dict(op='stop', setup_id=setup_id))
        elif action == 'Q' and setup_id:
            result = self.rpc(dict(op='status', setup_id=setup_id))
        else:
            result = self.rpc(dict(op='prepare'))
        result = checked_view(result)
        if result['state'] in ('ready', 'failed') and not result['terminal'] and result['setup_id']:
            try:
                result = checked_view(self.rpc(dict(op='credential', setup_id=result['setup_id'])))
            except ValueError:
                latest = checked_view(self.rpc(dict(op='status', setup_id=result['setup_id'])))
                if latest['state'] in ('ready', 'failed'):
                    raise
                result = latest
        return result

    def tick(self):
        if self.open and self.clock()-self.last_activity>8:
            self.close()
        if self.future and self.future.done():
            job = self.job
            try:
                value = self.future.result()
            except (OSError, ValueError, KeyError, TypeError, RecursionError, CancelledError):
                value = empty('error', 'UNAVAILABLE')
                if job[2] in ('Q', 'T') and job[3]:
                    # Transport failure does not create a new setup revision.
                    # Keep the binding so the Dial can consume its pending reply.
                    value.update(setup_id=job[3], revision=job[4])
            self.future = self.job = None
            if job[0] == self.epoch and self.open:
                if value['setup_id']:
                    self.setup_id = value['setup_id']
                if not self.queued:
                    self.view = value
                    self.completed_request = job[1]
                else:
                    value.pop('access', None)
        if self.future is None and self.queued and self.open:
            request_id, action, identity = self.queued
            self.queued = None
            identity = identity or self.setup_id
            nonce = hashlib.sha256((self.session+':'+str(request_id)).encode()).hexdigest()[:32]
            revision = self.view['revision'] if self.view['setup_id'] == identity else 0
            self.job = (self.epoch, request_id, action, identity, revision)
            self.future = self.executor.submit(self._call, action, identity, nonce)

    def record(self):
        if not self.open or not self.session or not self.request_id or self.completed_request != self.request_id:
            return None
        value = self.view
        access = value.get('access') or {}
        ssid, password = access.get('ssid', ''), access.get('password', '')
        payload = wifi_payload(ssid, password) if access else ''
        fields = ['B8', 'W', self.session, str(self.request_id), value['setup_id'] or '-',
                  str(value['revision']), value['state'].upper(), str(value['remaining']),
                  ssid.encode().hex() or '-', password.encode().hex() or '-', payload.encode().hex() or '-',
                  value['error']]
        record = ('\t'.join(fields)+'\n').encode('ascii')
        if len(record)>1153:
            raise ValueError('Setup record exceeds the USB boundary.')
        return record
