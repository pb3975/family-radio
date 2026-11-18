#!/usr/bin/env python3
"""Loopback-only Wi-Fi setup demo. Never changes the machine's networking."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import threading
import time
import unicodedata

VERSION = 3
MAX_BODY = 2048
ASSETS = Path(__file__).with_suffix('')


def validate_join(data):
    """Validate external input without retaining or describing credentials."""
    if not isinstance(data, dict) or set(data) != {'ssid', 'security', 'password'}:
        raise ValueError('Enter a network name, security and password.')
    ssid, security, password = (data[k] for k in ('ssid', 'security', 'password'))
    if (not isinstance(ssid, str) or not 1 <= len(ssid.encode('utf-8')) <= 32
            or any(unicodedata.category(c).startswith('C') for c in ssid)):
        raise ValueError('Network names must contain 1 to 32 UTF-8 bytes without control characters.')
    if security not in ('open', 'wpa2') or not isinstance(password, str):
        raise ValueError('Choose Open or WPA2 Personal security.')
    if security == 'open' and password:
        raise ValueError('An open network does not use a password.')
    if security == 'wpa2' and not (8 <= len(password) <= 63
            and all(32 <= ord(c) <= 126 for c in password)
            or len(password) == 64 and all(c in '0123456789abcdefABCDEF' for c in password)):
        raise ValueError('WPA2 needs 8 to 63 ASCII characters or a 64-digit hexadecimal key.')
    return ssid, security, password


class DemoNetwork:
    """Deterministic synthetic networks; injected clock makes progress testable."""
    def __init__(self, clock=time.monotonic, delay=2):
        self.clock = clock
        self.delay = delay
        self.deadline = None
        self.ssid = ''
        self.state = 'ready'
        self.failed = False

    def scan(self):
        return [dict(ssid='Demo Home Network', security='wpa2'),
                dict(ssid='Workshop guest', security='open'),
                dict(ssid='Studio & friends', security='wpa2')]

    def join(self, ssid, security, password):
        self.ssid = ssid
        self.failed = password == 'wrong-password'
        self.state = 'connecting'
        self.deadline = self.clock() + self.delay

    def status(self):
        if self.state == 'connecting' and self.clock() >= self.deadline:
            self.state = 'failed' if self.failed else 'connected'
        messages = {'ready': 'Choose a network to begin.',
                    'connecting': 'Trying the selected network.',
                    'connected': 'Demo connection complete. Internet access was not tested.',
                    'failed': 'Could not join. Check the network name and password, then try again.'}
        return dict(version=VERSION, demo=True, state=self.state,
                    ssid=self.ssid, message=messages[self.state])


class Setup:
    """Serialize network operations so duplicate submissions cannot race."""
    def __init__(self, network):
        self.network = network
        self.lock = threading.Lock()

    def networks(self):
        with self.lock:
            return dict(version=VERSION, demo=True, networks=self.network.scan())

    def status(self):
        with self.lock:
            return self.network.status()

    def join(self, data):
        ssid, security, password = validate_join(data)
        with self.lock:
            if self.network.status()['state'] == 'connecting':
                return False
            self.network.join(ssid, security, password)
            return True


class HelperNetwork:
    """Unprivileged client of the fixed-purpose, root-owned setup helper."""
    def __init__(self, path='/run/family-radio-wifi/control.sock'):
        self.path = path

    def request(self, operation, **fields):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(self.path)
            connection.sendall(json.dumps(dict(op=operation, **fields)).encode() + b'\n')
            data = bytearray()
            while not data.endswith(b'\n') and len(data) <= 32768:
                part = connection.recv(4096)
                if not part:
                    break
                data.extend(part)
            if len(data) > 32768 or not data.endswith(b'\n'):
                raise OSError('Setup helper response unavailable.')
            result = json.loads(data)
            if 'error' in result:
                raise ValueError('Setup request was not accepted.')
            return result

    def networks(self):
        return self.request('networks')

    def status(self):
        return self.request('status')

    def join(self, data):
        validate_join(data)
        return self.request('join', join=data)['accepted']


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, setup, authority=None, real=False):
        if not ipaddress.ip_address(address[0]).is_loopback and not (real and address == ('192.168.4.1', 80)):
            raise ValueError('The portal must bind to loopback or the fixed setup address.')
        self.real = real
        self.slots = threading.BoundedSemaphore(8)
        self.setup = setup
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, PortalHandler)
        self.authority = authority or f'{address[0]}:{self.server_port}'
        self.origin = f'http://{self.authority}'


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


class PortalHandler(BaseHTTPRequestHandler):
    server_version = 'FamilyRadioSetup'

    def setup(self):
        super().setup()
        self.connection.settimeout(3)
        self.deadline_timer = threading.Timer(8, self.expire)
        self.deadline_timer.daemon = True
        self.deadline_timer.start()

    def expire(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self.deadline_timer.cancel()
        super().finish()

    def log_message(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.respond(code, dict(error='Request not supported.'))

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def respond(self, status, body=b'', content_type='application/json', location=None):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        self.send_header('Connection', 'close')
        if location:
            self.send_header('Location', location)
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def trusted_host(self):
        hosts = self.headers.get_all('Host', [])
        allowed = (self.server.authority, '192.168.4.1:80') if self.server.real and self.server.authority == '192.168.4.1' else (self.server.authority,)
        if len(hosts) != 1 or hosts[0] not in allowed:
            self.respond(403, dict(error='Open the setup address shown by the server.'))
            return False
        return True

    def do_GET(self):
        if self.server.real and self.path in ('/generate_204', '/gen_204', '/hotspot-detect.html', '/connecttest.txt', '/redirect', '/canonical.html', '/success.txt', '/ncsi.txt'):
            self.respond(302, location=self.server.origin + '/')
            return
        try:
            self.get_page()
        except (OSError, ValueError):
            self.respond(503, dict(error='Setup is unavailable. Reconnect to setup Wi-Fi or use the recovery instructions.'))

    def get_page(self):
        if not self.trusted_host():
            return
        if self.path == '/api/status':
            self.respond(200, self.server.setup.status())
        elif self.path == '/api/networks':
            self.respond(200, self.server.setup.networks())
        elif self.path in ('/', '/app.js', '/style.css'):
            name, mime = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/style.css': ('style.css', 'text/css; charset=utf-8')}[self.path]
            body = (ASSETS / name).read_bytes().replace(b'__SETUP_TOKEN__', self.server.token.encode())
            if self.server.real and name == 'index.html':
                body = body.replace(b'LOCAL DEMO \xc2\xb7 SYNTHETIC NETWORKS', b'RADIO WI-FI SETUP').replace(b'This demo runs on your computer. It does not change Wi-Fi. Use a made-up password.', b'Choose a 2.4 GHz network. Setup Wi-Fi will disconnect while the radio joins. If joining fails, reconnect to setup Wi-Fi to retry.')
            self.respond(200, body, mime)
        elif self.path in ('/generate_204', '/hotspot-detect.html', '/connecttest.txt', '/redirect'):
            self.respond(302, location='/')
        else:
            self.respond(404, dict(error='Page not found.'))

    def do_POST(self):
        if not self.trusted_host():
            return
        if any(len(self.headers.get_all(name, [])) > 1 for name in ('Origin', 'X-Setup-Token', 'Content-Type', 'Transfer-Encoding')):
            self.respond(400, dict(error='Use one value per request header.'))
            return
        origin = self.headers.get('Origin')
        token = self.headers.get('X-Setup-Token', '')
        if (origin is not None and origin != self.server.origin
                or not hmac.compare_digest(token.encode(), self.server.token.encode())):
            self.respond(403, dict(error='Reload the setup page and try again.'))
            return
        if self.path != '/api/join':
            self.respond(404, dict(error='Page not found.'))
            return
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type') != 'application/json':
            self.respond(415, dict(error='Use a JSON request.'))
            return
        lengths = self.headers.get_all('Content-Length', [])
        try:
            if len(lengths) != 1:
                raise ValueError
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                self.respond(413, dict(error='Request is too large or empty.'))
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError
            data = json.loads(body)
            accepted = self.server.setup.join(data)
        except (ValueError, UnicodeError, RecursionError):
            self.respond(400, dict(error='Check the network name, security and password.'))
            return
        except (ConnectionError, TimeoutError):
            self.respond(408, dict(error='Request timed out. Try again.'))
            return
        except OSError:
            self.respond(503, dict(error='Setup is unavailable. Reconnect to setup Wi-Fi.'))
            return
        if accepted:
            self.respond(202, self.server.setup.status())
        else:
            self.respond(409, dict(error='A connection attempt is already in progress.'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--radio', action='store_true', help='Use the installed setup helper and fixed AP address.')
    args = parser.parse_args()
    address = ('192.168.4.1', 80) if args.radio else ('127.0.0.1', args.port)
    backend = HelperNetwork() if args.radio else Setup(DemoNetwork())
    with PortalServer(address, backend, authority='192.168.4.1' if args.radio else None, real=args.radio) as server:
        print(f'Wi-Fi setup: {server.origin}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
