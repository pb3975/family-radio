"""Fixed-interface NetworkManager boundary for the opt-in setup session."""
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import time
import tempfile
import uuid
import socket
import struct
import urllib.request

INTERFACE = 'wlan0'
ADDRESS = '192.168.4.1'
SUBNET = ipaddress.ip_network('192.168.4.0/24')
AP_UUID = '319034a6-e56c-4773-9984-44a65fc343cd'
PREFIX = 'family-setup-'


class NetworkError(Exception):
    """Safe public error. Command output is deliberately never propagated."""


def run(arguments, *, timeout=15, input=None):
    try:
        result = subprocess.run(arguments, input=input, capture_output=True, text=True,
                                timeout=timeout, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired):
        raise NetworkError('Network operation timed out or was unavailable.') from None
    if result.returncode:
        raise NetworkError('Network operation did not complete.')
    return result.stdout


def private_write(path, value):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def autoconnect_keyfile(value, enabled):
    section, count, lines = None, 0, []
    for line in value.splitlines():
        if line.startswith('[') and line.endswith(']'):
            section = line
        if section == '[connection]' and line.partition('=')[0] == 'autoconnect':
            line = 'autoconnect=' + str(enabled).lower()
            count += 1
        lines.append(line)
    if count != 1:
        raise NetworkError('Invalid saved connection autoconnect setting.')
    return '\n'.join(lines) + '\n'


def key_string(value):
    return ''.join({'\\': '\\\\', ' ': '\\s', '\t': '\\t', '\n': '\\n', '\r': '\\r'}.get(c, c) for c in value)


def keyfile(identity, ssid, security, password, *, ap=False, persistent=False):
    uuid.UUID(identity)
    name = PREFIX + ('ap' if ap else identity)
    value = (f'[connection]\nid={name}\nuuid={identity}\ntype=wifi\ninterface-name={INTERFACE}\n'
             f'autoconnect={str(persistent).lower()}\nautoconnect-priority=100\n\n[wifi]\n'
             f'ssid={";".join(str(b) for b in ssid.encode("utf-8"))};\nmode={"ap" if ap else "infrastructure"}\nband=bg\n')
    if not ap:
        value += 'hidden=true\n'
    if security == 'wpa2':
        value += ('\n[wifi-security]\nkey-mgmt=wpa-psk\nproto=rsn;\npairwise=ccmp;\n'
                  + ('group=ccmp;\n' if ap else '')
                  + f'pmf=1\npsk={key_string(password)}\n')
    value += ('\n[ipv4]\nmethod=manual\naddress1=192.168.4.1/24\nnever-default=true\n'
              if ap else '\n[ipv4]\nmethod=auto\nmay-fail=false\n')
    return value + '\n[ipv6]\nmethod=disabled\n'


def terse(line):
    fields, field, escaped = [], '', False
    for char in line:
        if escaped:
            field += char
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == ':':
            fields.append(field)
            field = ''
        else:
            field += char
    if escaped:
        raise NetworkError('Invalid network scan response.')
    return fields + [field]


class NetworkManager:
    def __init__(self, runner=run, runtime=Path('/run/NetworkManager/system-connections'),
                 persistent=Path('/etc/NetworkManager/system-connections')):
        self.run, self.runtime, self.persistent = runner, Path(runtime), Path(persistent)

    def nm(self, *arguments, wait=10):
        return self.run(['nmcli', '--wait', str(wait), *arguments], timeout=wait + 3)

    def active(self):
        value = self.nm('-g', 'GENERAL.CON-UUID', 'device', 'show', INTERFACE).strip()
        if value in ('', '--'):
            return None
        return str(uuid.UUID(value))

    def preflight(self):
        if self.nm('-g', 'GENERAL.NM-MANAGED', 'device', 'show', INTERFACE).strip() != 'yes':
            raise NetworkError('NetworkManager must manage the Wi-Fi interface.')
        if self.nm('-g', 'WIFI-PROPERTIES.AP', 'device', 'show', INTERFACE).strip() != 'yes':
            raise NetworkError('This Wi-Fi interface cannot create a setup network.')
        routes = json.loads(self.run(['ip', '-j', '-4', 'route', 'show']))
        for route in routes:
            destination = route.get('dst', 'default')
            if destination != 'default' and SUBNET.overlaps(ipaddress.ip_network(destination, strict=False)):
                raise NetworkError('The setup subnet overlaps an existing route.')

    def scan(self):
        rows = self.nm('-t', '-e', 'yes', '-f', 'SSID,SECURITY,FREQ', 'device', 'wifi', 'list',
                       'ifname', INTERFACE, '--rescan', 'yes', wait=15)
        networks = {}
        for row in rows.splitlines():
            fields = terse(row)
            if len(fields) != 3:
                continue
            ssid, security, frequency = fields
            frequency = frequency.removesuffix(' MHz')
            if not ssid or not frequency.isdigit() or int(frequency) >= 3000:
                continue
            if security in ('', '--'):
                mode = 'open'
            elif 'WPA2' in security and not any(word in security for word in ('802.1X', 'EAP')):
                mode = 'wpa2'
            else:
                continue
            networks[(ssid, mode)] = dict(ssid=ssid, security=mode)
        return list(networks.values())[:100]

    def path(self, identity, persistent=False):
        uuid.UUID(identity)
        return (self.persistent if persistent else self.runtime) / f'{PREFIX}{identity}.nmconnection'

    def load(self, identity, value, persistent=False):
        path = self.path(identity, persistent)
        private_write(path, value)
        self.nm('connection', 'load', str(path))

    def autoconnect(self):
        return self.nm('-g', 'GENERAL.AUTOCONNECT', 'device', 'show', INTERFACE).strip() == 'yes'

    def set_autoconnect(self, enabled):
        self.nm('device', 'set', INTERFACE, 'autoconnect', 'yes' if enabled else 'no')

    def deactivate(self, identity):
        if self.active() == identity:
            self.nm('connection', 'down', 'uuid', identity)
            if self.active() == identity:
                raise NetworkError('The setup connection did not stop.')

    def activate(self, identity):
        self.nm('connection', 'up', 'uuid', str(uuid.UUID(identity)), 'ifname', INTERFACE, wait=40)
        if self.active() != identity:
            raise NetworkError('The requested Wi-Fi connection is not active.')
        addresses = self.nm('-g', 'IP4.ADDRESS', 'device', 'show', INTERFACE).replace(' | ', '\n').splitlines()
        if not any(not ipaddress.ip_interface(a).ip.is_link_local for a in addresses if a):
            raise NetworkError('Wi-Fi connected but did not receive an address.')

    def restore(self, identity):
        try:
            self.activate(identity)
            return
        except NetworkError:
            # Let NetworkManager choose another existing saved profile by its
            # unchanged priorities. Do not create a replacement profile.
            self.set_autoconnect(True)
            for _ in range(15):
                active = self.active()
                if active and active != AP_UUID:
                    return
                time.sleep(1)
            raise NetworkError('No saved Wi-Fi connection could be restored.') from None

    def discard(self, identity):
        # Ownership is established by the private session journal, even when a
        # crash happened after file creation but before NM loaded the UUID.
        try:
            loaded = self.nm('-g', 'UUID', 'connection', 'show').splitlines()
            if identity in loaded:
                try:
                    self.deactivate(identity)
                except NetworkError:
                    pass  # Deletion can deactivate an active profile.
                try:
                    self.nm('connection', 'delete', 'uuid', identity)
                except NetworkError:
                    pass  # Check the converged result after file removal/reload.
        finally:
            self.path(identity).unlink(missing_ok=True)
            self.path(identity, persistent=True).unlink(missing_ok=True)
            self.nm('connection', 'reload')
        if identity in self.nm('-g', 'UUID', 'connection', 'show').splitlines():
            raise NetworkError('The unfinished setup connection could not be removed.')

    def persist(self, identity, value):
        self.load(identity, autoconnect_keyfile(value, False), persistent=True)
        self.path(identity).unlink(missing_ok=True)
        self.nm('connection', 'reload')
        if self.active() != identity:
            raise NetworkError('The saved connection is no longer active.')

    def finish_commit(self, identity):
        path = self.path(identity, persistent=True)
        value = path.read_text()
        if f'uuid={identity}\n' not in value or f'id={PREFIX}{identity}\n' not in value:
            raise NetworkError('The saved setup connection ownership is invalid.')
        private_write(path, autoconnect_keyfile(value, True))
        self.nm('connection', 'load', str(path))


    def setup_ready(self):
        """Probe the actual setup endpoints before showing access credentials."""
        if self.active() != AP_UUID:
            raise NetworkError('Setup network is not active.')
        self.run(['systemctl', 'is-active', '--quiet', 'family-radio-wifi-dns.service', 'family-radio-wifi-http.service'])
        for _ in range(10):
            try:
                with urllib.request.urlopen('http://192.168.4.1/api/status', timeout=.5) as response:
                    value = json.load(response)
                if value.get('demo') is not False:
                    raise ValueError
                query = struct.pack('!HHHHHH', 1739, 256, 1, 0, 0, 0) + b'\x05setup\x07invalid\0' + struct.pack('!HH', 1, 1)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
                    connection.settimeout(.5)
                    connection.sendto(query, (ADDRESS, 53))
                    answer, _ = connection.recvfrom(4096)
                if answer[:2] != query[:2] or answer[-4:] != socket.inet_aton(ADDRESS):
                    raise ValueError
                return
            except (OSError, ValueError, KeyError):
                time.sleep(.1)
        raise NetworkError('Setup web page and DNS are not ready.')
