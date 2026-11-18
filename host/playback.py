"""Serialized local and Spotify audio ownership. No cloud requests run here."""
from collections import deque
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time

MPV_STARTUP_TIMEOUT = 10.0


class SourceError(ValueError):
    pass


class Mpv:
    def __init__(self, song=None, executable='mpv', audio='alsa/default', cancelled=lambda: False):
        self.temp = tempfile.TemporaryDirectory(prefix='family-radio-')
        self.process = None
        self.sock = None
        self.buffer = bytearray()
        self.request_id = 0
        self.loaded = 0
        self.source_error = False
        self.duration = 0
        self.cancelled = cancelled
        try:
            self.process = subprocess.Popen([
                executable, '--no-config', '--no-video', '--no-terminal',
                '--load-scripts=no', '--input-default-bindings=no',
                '--idle=yes', '--keep-open=yes', '--pause=yes', '--volume=100',
                '--audio-stream-silence=yes',  # Keep this I2S amplifier quiet while paused.
                '--audio-device=' + audio, '--input-ipc-server=' + self.temp.name + '/ipc',
                 ], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + MPV_STARTUP_TIMEOUT
            while True:
                if self.cancelled():
                    raise Cancelled()
                if self.process.poll() is not None:
                    raise RuntimeError('mpv exited during startup')
                candidate = socket.socket(socket.AF_UNIX)
                candidate.settimeout(0.5)
                try:
                    candidate.connect(self.temp.name + '/ipc')
                    self.sock = candidate
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    candidate.close()
                    if time.monotonic() >= deadline:
                        raise RuntimeError('mpv startup timed out')
                    time.sleep(0.02)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('mpv startup timed out')
            if not isinstance(self.command('get_property', 'pause', timeout=remaining,
                                           cancelled=self.cancelled), bool):
                raise RuntimeError('mpv startup readiness failed')
            if song is not None:
                self.load(song)
        except BaseException:
            self.close()
            raise

    def command(self, *args, timeout=0.7, cancelled=None):
        deadline = time.monotonic() + timeout
        if cancelled and cancelled():
            raise Cancelled()
        if self.process.poll() is not None:
            raise RuntimeError('mpv exited')
        self.request_id += 1
        self.sock.settimeout(timeout)
        try:
            self.sock.sendall((json.dumps({'command': args, 'request_id': self.request_id}) + '\n').encode())
        except TimeoutError as exc:
            raise RuntimeError(f'mpv IPC timeout: {args[0]}') from exc
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                raise Cancelled()
            if self.process.poll() is not None:
                raise RuntimeError('mpv exited')
            while b'\n' in self.buffer:
                line, _, rest = self.buffer.partition(b'\n')
                self.buffer = bytearray(rest)
                try:
                    reply = json.loads(line)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise RuntimeError('invalid mpv IPC JSON') from exc
                if not isinstance(reply, dict):
                    raise RuntimeError('invalid mpv IPC reply')
                if reply.get('event') == 'file-loaded':
                    self.loaded = getattr(self, 'loaded', 0) + 1
                    self.source_error = False
                if reply.get('event') == 'end-file' and reply.get('reason') == 'error':
                    self.source_error = True
                if reply.get('request_id') == self.request_id:
                    if reply.get('error') == 'property unavailable':
                        return None
                    if reply.get('error') != 'success':
                        raise RuntimeError('mpv command failed')
                    return reply.get('data')
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.sock.settimeout(min(0.1, remaining))
                data = self.sock.recv(4096)
            except TimeoutError:
                continue
            if not data:
                raise RuntimeError('mpv IPC closed')
            self.buffer.extend(data)
            if len(self.buffer) > 65536:
                raise RuntimeError('mpv IPC record limit')
        raise RuntimeError(f'mpv IPC timeout: {args[0]}')

    def load(self, song):
        path = Path(song).resolve()
        if path.suffix.lower() not in ('.mp3', '.wav') or not path.is_file():
            raise SourceError('song must be a regular MP3 or WAV file')
        self.pause(True)
        # Consume old events before taking the barrier. A file-loaded event after
        # loadfile replace, plus an explicit pause, separates the old EOF.
        self.command('get_property', 'pause')
        barrier = self.loaded
        self.source_error = False
        self.command('loadfile', str(path), 'replace')
        deadline = time.monotonic() + 3
        while self.loaded == barrier:
            if self.cancelled():
                raise Cancelled()
            self.command('get_property', 'idle-active')
            if self.source_error or time.monotonic() >= deadline:
                raise SourceError('mpv could not decode song')
            time.sleep(0.01)
        self.pause(True)
        self.duration = self.command('get_property', 'duration') or 0

    def progress(self):
        if self.source_error:
            raise SourceError('mpv playback failed')
        return (self.command('get_property', 'time-pos') or 0, self.duration)

    def pause(self, paused):
        self.command('set_property', 'pause', paused)

    def play(self):
        if self.command('get_property', 'eof-reached'):
            self.command('seek', 0, 'absolute')
        self.pause(False)

    def ended(self):
        # EOF can precede draining the audio output. keep-open pauses after drain.
        return bool(self.command('get_property', 'eof-reached') and
                    self.command('get_property', 'pause'))

    def close(self):
        if self.sock:
            self.sock.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.temp.cleanup()


class Cancelled(Exception):
    """A newer audio intent replaced work that was still starting."""


def gain_for_db(decibels):
    if not math.isfinite(decibels) or not -120 <= decibels <= 0:
        raise RuntimeError('OUTPUT')
    return 10 ** (decibels / 20)


@dataclass(frozen=True)
class LocalItem:
    key: str
    path: Path
    duration_ms: int


@dataclass(frozen=True)
class SpotifyItem:
    key: str
    uri: str
    duration_ms: int


@dataclass(frozen=True)
class Select:
    generation: int
    item: LocalItem | SpotifyItem
    playing: bool
    position_ms: int = 0


@dataclass(frozen=True)
class SetPaused:
    generation: int
    paused: bool


@dataclass(frozen=True)
class SetVolume:
    percent: int


@dataclass(frozen=True)
class Stop:
    generation: int


@dataclass(frozen=True)
class AudioEvent:
    generation: int
    state: str
    position_ms: int
    duration_ms: int
    ended: bool = False
    error: str | None = None


def _reap(process):
    if process is not None and process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


class LocalBackend:
    def __init__(self, mpv_factory=Mpv):
        self.factory, self.player, self.item = mpv_factory, None, None

    def set_volume(self, percent):
        subprocess.run(['amixer', '-q', '-D', 'default', 'sset', 'Master', f'{percent}%'],
                       check=True, timeout=1, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start(self, item, position, guard, volume):
        if not guard():
            raise Cancelled()
        if self.player is None:
            self.player = self.factory(cancelled=lambda: not guard())
        self.player.cancelled = lambda: not guard()
        if item != self.item or position is not None:
            self.player.load(item.path)
            if position:
                self.player.command('seek', position / 1000, 'absolute')
            self.item = item
        if not guard():
            raise Cancelled()
        self.set_volume(volume())
        if not guard():
            raise Cancelled()
        self.player.play()

    def prepare(self, item, guard, volume):
        if self.player is None:
            self.player = self.factory(cancelled=lambda: not guard())
        self.player.cancelled = lambda: not guard()
        self.player.load(item.path)
        self.item = item
        self.set_volume(volume())

    def pause(self):
        if self.player:
            self.player.pause(True)

    def status(self):
        if self.player is None or self.item is None:
            return 'STOPPED', 0, 0, False
        position, duration = self.player.progress()
        ended = self.player.ended()
        paused = self.player.command('get_property', 'pause')
        return ('STOPPED' if ended else 'PAUSED' if paused else 'PLAYING',
                round(position * 1000), round(duration * 1000), ended)

    def stop(self):
        if self.player:
            self.player.close()
        self.player, self.item = None, None

    close = stop


class PipeWireOutput:
    """A private, stateless session. It never enables a boot service."""
    def __init__(self, guard):
        self.temp = tempfile.TemporaryDirectory(prefix='family-radio-pipewire-')
        self.children = []
        self.guard = guard
        self.env = dict(os.environ)
        root = Path(self.temp.name)
        for variable, name in [('XDG_RUNTIME_DIR', 'run'), ('PIPEWIRE_RUNTIME_DIR', 'run'),
                               ('XDG_STATE_HOME', 'state'), ('XDG_CONFIG_HOME', 'config'),
                               ('XDG_CACHE_HOME', 'cache')]:
            folder = root / name
            folder.mkdir(mode=0o700, exist_ok=True)
            self.env[variable] = str(folder)
        self.env['PIPEWIRE_REMOTE'] = 'pipewire-0'
        for variable in ('PIPEWIRE_CORE', 'PIPEWIRE_CONFIG_DIR', 'WIREPLUMBER_CONFIG_DIR'):
            self.env.pop(variable, None)
        self.muted = True
        try:
            config = root / 'config/wireplumber/wireplumber.conf.d'
            config.mkdir(parents=True, mode=0o700)
            (config / '10-radio-output.conf').write_text('''monitor.alsa.rules = [
              { matches = [ { device.name = "~alsa_card.*" } ]
                actions = { update-props = { api.alsa.soft-mixer = true } } }
              { matches = [ { node.name = "~alsa_output.*" } ]
                actions = { update-props = {
                  session.suspend-timeout-seconds = 0
                  node.pause-on-idle = false
                } } }
            ]\n''')
            self.children.append(self.launch(['pipewire']))
            self._wait(lambda: (root / 'run/pipewire-0').exists(), guard, 10)
            self.children.append(self.launch(['wireplumber', '--profile', 'main-embedded']))
            card = Path('/proc/asound/MAX98357A').resolve().name
            if not re.fullmatch(r'card[0-9]+', card):
                raise RuntimeError('OUTPUT')
            number = card[4:]
            def discover():
                data = json.loads(self.run(['pw-dump']))
                devices = {str(obj['id']) for obj in data
                           if str(obj.get('info', {}).get('props', {}).get('api.alsa.card')) == number}
                sinks = [(obj['id'], obj['info']['props'].get('node.name')) for obj in data
                         if obj.get('info', {}).get('props', {}).get('media.class') == 'Audio/Sink'
                         and (str(obj['info']['props'].get('api.alsa.pcm.card')) == number
                              or str(obj['info']['props'].get('device.id')) in devices)]
                if len(sinks) == 1 and sinks[0][1]:
                    self.node_id, self.node_name = sinks[0]
                    return self._route_level(data, self.node_id) is not None
                return False
            self._wait(discover, guard, 15)
            self.mute(True)
            self.set_volume(0)
        except BaseException:
            self.close()
            raise

    def launch(self, command, stdout=subprocess.DEVNULL):
        return subprocess.Popen(command, env=self.env, stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=subprocess.DEVNULL)

    def run(self, command):
        result = subprocess.run(command, env=self.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2)
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
            raise RuntimeError('OUTPUT')
        return result.stdout.decode()

    def _wait(self, ready, guard, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if not guard():
                raise Cancelled()
            if any(child.poll() is not None for child in self.children):
                raise RuntimeError('OUTPUT')
            if ready():
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('OUTPUT')
            time.sleep(.05)

    def _level(self):
        objects = json.loads(self.run(['pw-dump']))
        result = self._route_level(objects, self.node_id)
        if result is None:
            raise RuntimeError('OUTPUT')
        return result

    @staticmethod
    def _route(objects, node_id):
        node = next((obj for obj in objects if obj.get('id') == node_id), None)
        if node is None:
            return None
        info = node.get('info', {})
        identity = info.get('props', {})
        candidates = []
        if 'device.id' in identity and 'card.profile.device' in identity:
            device = next((obj for obj in objects
                           if str(obj.get('id')) == str(identity['device.id'])), {})
            # WirePlumber's mixer-api controls an active Device Route when one
            # matches this node. Node Props can stay unchanged at idle.
            candidates = [route
                          for route in device.get('info', {}).get('params', {}).get('Route', [])
                          if str(route.get('device')) == str(identity['card.profile.device'])
                          and route.get('direction', 'Output') == 'Output']
            if len(candidates) > 1:
                raise RuntimeError('OUTPUT')
        # This MAX98357A setup is controlled through a Device Route. The
        # transient sink can appear before that route; do not write to its
        # temporary Node Props and lose the write when policy creates the route.
        if not candidates:
            return None
        return device['id'], candidates[0]

    @staticmethod
    def _route_level(objects, node_id):
        selected = PipeWireOutput._route(objects, node_id)
        if selected is None:
            return None
        props = selected[1].get('props', {})
        values = props.get('channelVolumes')
        if isinstance(values, list) and values and type(props.get('mute')) is bool:
            if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1.00001 for v in values):
                raise RuntimeError('OUTPUT')
            return max(values), props['mute']
        return None

    def _write_route(self, *, gain=None, mute=None):
        objects = json.loads(self.run(['pw-dump']))
        selected = self._route(objects, self.node_id)
        if selected is None or self._route_level(objects, self.node_id) is None:
            raise RuntimeError('OUTPUT')
        device_id, route = selected
        if any(type(value) is not int or value < 0
               for value in (device_id, route.get('index'), route.get('device'))):
            raise RuntimeError('OUTPUT')
        props = {}
        if gain is not None:
            props['channelVolumes'] = [gain] * len(route['props']['channelVolumes'])
        if mute is not None:
            props['mute'] = mute
        payload = dict(index=route['index'], device=route['device'], props=props, save=True)
        # Target the discovered Device Route directly. The first wpctl mute
        # returned success without changing this Route on the actual Pi.
        self.run(['pw-cli', 'set-param', str(device_id), 'Route', json.dumps(payload)])

    def set_volume(self, percent):
        if percent == 0:
            gain = 0
        else:
            # Use the existing softvol's actual quantized dB curve. The private
            # PipeWire session bypasses that PCM and has its hardware mixer off.
            result = self.run(['amixer', '-D', 'default', 'sset', 'Master', f'{percent}%'])
            levels = [float(value) for value in re.findall(r'\[(-?[0-9.]+)dB\]', result)]
            if not levels or max(levels) != min(levels):
                raise RuntimeError('OUTPUT')
            gain = gain_for_db(levels[0])
        self._write_route(gain=gain)
        self._wait_level(lambda actual, _: math.isclose(actual, gain, rel_tol=.001, abs_tol=.00001),
                         self.guard)

    def _wait_level(self, ready, guard, timeout=2):
        deadline = time.monotonic() + timeout
        while True:
            if not guard():
                raise Cancelled()
            if ready(*self._level()):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('OUTPUT')
            time.sleep(.05)

    def mute(self, value):
        self._write_route(mute=value)
        # A safety mute must finish even when the previous start was cancelled.
        self._wait_level(lambda _, muted: muted == value,
                         (lambda: True) if value else self.guard)
        self.muted = value

    def close(self):
        for process in reversed(self.children):
            _reap(process)
        self.children.clear()
        self.temp.cleanup()


class SoloistBackend:
    """One private output session, with a separately gated process per track."""
    def __init__(self, binary, key_path, session, output_factory=PipeWireOutput,
                 now=time.monotonic, wall_time=time.time):
        self.binary, self.key_path, self.session = str(binary), Path(key_path), Path(session)
        self.output_factory, self.now, self.wall_time = output_factory, now, wall_time
        self.output = self.player = self.trace = self.item = None
        self.buffer = bytearray()
        self.state, self.uri, self.position, self.duration = 'STOPPED', '', 0, 0
        self.anchor, self.speed = now(), 0
        self.saw_playing = False
        self.finished = False
        self.paused_since = None
        self.last_transport = 'STOPPED'
        self.end_position = 0
        self.log = None
        self.secret = ''
        self.position_revision = 0
        self.position_received_at = now()
        self.terminal_at = None
        self.pending_resume = None

    def _position(self):
        return min(self.duration, max(0, round(self.position + (self.now() - self.anchor) * self.speed * 1000)))

    def _invalid_trace(self, line):
        self._diagnostic('INVALID_TRACE_SHAPE', None, line)
        raise RuntimeError('PLAYER')

    def _consume(self):
        if self.trace is None:
            return
        for _ in range(64):
            try:
                chunk = os.read(self.trace.stdout.fileno(), 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            self.buffer.extend(chunk)
            if len(self.buffer) > 65536:
                self._invalid_trace(b'oversized trace event')
            while b'\n' in self.buffer:
                line, _, self.buffer = self.buffer.partition(b'\n')
                if not line:
                    continue
                # `ctl trace` prefixes each event with its epoch milliseconds;
                # `ctl now --json` and synthetic transports use bare JSON.
                prefix, separator, payload = line.partition(b' ')
                if separator and 1 <= len(prefix) <= 20 and prefix.isdigit() and payload.startswith(b'{'):
                    line = payload
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    self._diagnostic('INVALID_TRACE_JSON', None, line)
                    raise RuntimeError('PLAYER') from None
                if not isinstance(message, dict):
                    self._invalid_trace(line)
                kind = message.get('type')
                if self.terminal_at is not None and kind in (
                        'playback_state', 'track_changed', 'position_sync', 'playback_changed'):
                    continue
                if kind in ('playback_state', 'track_changed'):
                    item = message.get('item', {})
                    if not isinstance(item, dict) or not isinstance(item.get('decorations', {}), dict):
                        self._invalid_trace(line)
                    uri = item.get('uri', '')
                    if not isinstance(uri, str):
                        self._invalid_trace(line)
                    if self.item is not None and uri and uri != self.item.uri:
                        received_at = self.now()
                        self.output.mute(True)
                        if not self.saw_playing or self.uri != self.item.uri:
                            self._diagnostic('UNEXPECTED_TRACK_BEFORE_PLAYING', None)
                            raise SourceError('Spotify changed the requested item')
                        # A successful single-track end can briefly expose the
                        # next buffered item. Keep the requested duration and
                        # its terminal drain anchor, never the replacement's 0.
                        projected = self._position()
                        if self.state == 'BUFFERING' and self.last_transport == 'PLAYING':
                            delay = max(0, received_at - self.position_received_at)
                            if delay <= 2:
                                projected = min(self.duration, self.position + delay * 1000)
                        if self.last_transport != 'PAUSED' and projected < self.duration - 500:
                            self._diagnostic('UNEXPECTED_TRACK_MID_PLAYBACK', None)
                            raise SourceError('Spotify changed the requested item')
                        self.position, self.anchor, self.speed = round(projected), self.now(), 0
                        self.end_position = self.position
                        self.terminal_at = received_at
                        self.state = 'PAUSED' if self.last_transport == 'PAUSED' else 'BUFFERING'
                        continue
                    self.uri = uri
                    playback = item.get('decorations', {}).get('playback', {})
                    if not isinstance(playback, dict):
                        self._invalid_trace(line)
                    duration = playback.get('duration_ms')
                    if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
                        self.duration = round(duration)
                if kind in ('playback_state', 'playback_changed'):
                    state = message.get('status')
                    if state in ('playing', 'paused', 'buffering', 'idle'):
                        self.position, self.anchor = self._position(), self.now()
                        self.state = {'playing': 'PLAYING', 'paused': 'PAUSED',
                                      'buffering': 'BUFFERING', 'idle': 'STOPPED'}[state]
                        self.speed = 1 if state == 'playing' else 0
                        self.saw_playing |= state == 'playing'
                        if state not in ('idle', 'buffering'):
                            self.last_transport = self.state
                        elif state == 'idle':
                            self.end_position = self.position
                if kind in ('playback_state', 'position_sync'):
                    position = message.get('position', {})
                    if not isinstance(position, dict):
                        self._invalid_trace(line)
                    value, speed = position.get('position_ms'), position.get('speed')
                    if type(value) in (int, float) and math.isfinite(value) and value >= 0 and speed in (0, 1):
                        # The anchor is received late; advance only a bounded delay.
                        stamp = position.get('timestamp_ms')
                        lag = max(0, min(2000, self.wall_time() * 1000 - stamp)) if type(stamp) in (int, float) and math.isfinite(stamp) else 0
                        self.position, self.anchor, self.speed = round(value + lag * speed), self.now(), speed
                        self.position_received_at = self.now()
                        self.position_revision += 1

    def _control(self, *command):
        result = subprocess.run([self.binary, 'ctl', *map(str, command), '--data-dir', str(self.session)],
                                env=self.output.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=2)
        if result.returncode:
            raise RuntimeError('PLAYER')

    def _wait(self, ready, guard, seconds=10):
        deadline = self.now() + seconds
        while True:
            if not guard():
                raise Cancelled()
            self._consume()
            if ready():
                return
            if self.player.poll() is not None or self.now() >= deadline:
                code = 'EXPIRED' if self.player.poll() == 10 else 'PLAYER'
                self._diagnostic(code, self.player.poll())
                raise RuntimeError(code)
            time.sleep(.02)

    def _audible(self, guard, volume):
        if not guard():
            raise Cancelled()
        self.output.set_volume(volume())
        if not guard():
            raise Cancelled()
        self.output.mute(False)
        if not guard():
            self.output.mute(True)
            raise Cancelled()

    def start(self, item, position, guard, volume):
        if not isinstance(item, SpotifyItem) or not re.fullmatch(r'spotify:track:[A-Za-z0-9]{22}', item.uri):
            raise SourceError('invalid Spotify track')
        if self.output is None:
            self.output = self.output_factory(guard)
        self.output.guard = guard
        self.output.mute(True)
        same = item == self.item and position is None and self.player is not None and self.player.poll() is None
        retained = position or 0
        if position is None and item == self.item:
            retained = self.pending_resume if self.pending_resume is not None else self._position()
        if self.finished:
            retained = 0
        if not same:
            self._stop_player()
            if not guard():
                raise Cancelled()
            try:
                if (self.key_path.stat().st_mode & 0o077 or not self.session.is_dir()
                        or self.session.stat().st_mode & 0o077):
                    raise RuntimeError('ACCOUNT')
                key = self.key_path.read_text().strip()
            except OSError:
                raise RuntimeError('ACCOUNT') from None
            if not key or len(key) > 16384:
                raise RuntimeError('ACCOUNT')
            self.item, self.duration = item, item.duration_ms
            self.pending_resume = retained if retained else None
            self.position, self.speed, self.anchor = 0, 0, self.now()
            self.state, self.uri, self.finished, self.saw_playing = 'BUFFERING', '', False, False
            self.last_transport, self.end_position, self.paused_since = 'BUFFERING', 0, None
            self.terminal_at = None
            for name in ('ws.port', 'ws.addr'):
                (self.session / name).unlink(missing_ok=True)
            self.log = (Path(self.output.temp.name) / 'soloist.log').open('wb+')
            self.secret = key
            self.player = subprocess.Popen([self.binary, '--device-name', 'Family Radio', '--api-key', key,
                        '--data-dir', str(self.session), '--cache-dir', str(Path(self.output.temp.name) / 'soloist-cache'),
                        '--cache-size', '100', '--initial-volume', '0', '--pipewire-device', self.output.node_name,
                        '--single-track', item.uri, '--ws', '127.0.0.1:0'], env=self.output.env,
                        stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT)
            del key
            self._wait(lambda: (self.session / 'ws.port').is_file(), guard)
            self.trace = subprocess.Popen([self.binary, 'ctl', 'trace', '--data-dir', str(self.session)],
                        env=self.output.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self.log)
            os.set_blocking(self.trace.stdout.fileno(), False)
            self._wait(lambda: self.uri == item.uri and self.state == 'PLAYING', guard, 60)
            if retained:
                self.end_position = 0
                before_seek = self.position_revision
                self._control('seek', retained)
                self._wait(lambda: (self.position_revision > before_seek and self.uri == item.uri
                                   and self.state == 'PLAYING'
                                   and max(0, retained - 50) <= self.position <= retained + 2000), guard)
            self.pending_resume = None
        else:
            self._control('play')
            self._wait(lambda: self.uri == item.uri and self.state == 'PLAYING', guard)
        self.paused_since = None
        self._audible(guard, volume)
        self.output.guard = lambda: True

    def set_volume(self, percent):
        if self.output:
            self.output.set_volume(percent)

    def prepare(self, item, guard, volume):
        self.stop()
        self.item, self.duration = item, item.duration_ms

    def pause(self):
        if self.output:
            self.output.guard = lambda: True
            self.output.mute(True)
        if self.pending_resume is not None:
            self._stop_player()
            self.position, self.speed, self.anchor = self.pending_resume, 0, self.now()
            self.pending_resume = None
            self.state = self.last_transport = 'PAUSED'
            self.paused_since = self.now()
            return
        self.position, self.anchor = self._position(), self.now()
        if self.player is not None and self.player.poll() is None:
            try:
                self._control('pause')
                self._wait(lambda: self.state == 'PAUSED', lambda: True, 3)
            except RuntimeError:
                self._stop_player()
        self.position, self.speed, self.anchor = self._position(), 0, self.now()
        self.state = 'PAUSED'
        self.last_transport = 'PAUSED'
        self.paused_since = self.now()

    def status(self):
        self._consume()
        if (self.terminal_at is not None and self.player is not None and self.player.poll() is None
                and self.now() - self.terminal_at > 2):
            self._diagnostic('REPLACEMENT_DID_NOT_EXIT', None)
            raise SourceError('Spotify did not stop after changing the requested item')
        if self.player is not None and self.player.poll() is not None:
            code = self.player.returncode
            position = self._position()
            if self.state == 'PAUSED':
                self._diagnostic('PAUSED_PROCESS_EXIT', code)
                self._stop_player()
            elif (code == 0 and self.saw_playing and self.last_transport == 'PLAYING'
                    and self.duration > 0 and max(position, self.end_position) >= self.duration - 500):
                self.position, self.speed, self.state = self.duration, 0, 'STOPPED'
                self.finished = True
                self._stop_player()
            else:
                self._diagnostic('UNEXPECTED_PROCESS_EXIT', code)
                self._stop_player()
                raise RuntimeError('EXPIRED' if code == 10 else 'PLAYER')
        if (self.state == 'PAUSED' and self.player is not None and self.paused_since is not None
                and self.now() - self.paused_since >= 30):
            self._stop_player()
        if self.item is not None and self.uri and self.uri != self.item.uri:
            if self.output:
                self.output.mute(True)
            raise RuntimeError('SOURCE')
        return self.state, self._position(), self.duration, self.finished

    def _diagnostic(self, reason, code, trace_line=None):
        lines = []
        if self.log is not None:
            self.log.flush()
            size = self.log.seek(0, 2)
            self.log.seek(max(0, size - 16384))
            raw = self.log.read().decode('utf-8', 'replace')
            raw = raw.replace(self.secret, '<redacted>') if self.secret else raw
            raw = re.sub(r'https?://\S+', '<url>', raw)
            raw = re.sub(r'[A-Za-z0-9_-]{24,}', '<redacted>', raw)
            raw = re.sub(r'Users/[^ /]+', 'Users/<redacted>', raw)
            lines = [line[:240] for line in raw.splitlines()
                     if re.search(r'single-track:|expired|failed|error|timed out', line, re.I)][-8:]
        value = dict(reason=reason, exit_code=code, state=self.state,
                     position_ms=self._position(), messages=lines)
        if trace_line is not None:
            prefix = trace_line.decode('utf-8', 'replace').split('{', 1)[0]
            prefix = prefix.replace(self.secret, '<redacted>') if self.secret else prefix
            prefix = re.sub(r'https?://\S+|spotify:\S+|[A-Za-z0-9_-]{24,}', '<redacted>', prefix)
            value['trace_prefix'] = prefix[:120]
        try:
            fd, name = tempfile.mkstemp(prefix='.audio-fault-', dir=self.session.parent)
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump(value, stream)
                os.replace(name, self.session.parent / 'playback-diagnostic.json')
            finally:
                Path(name).unlink(missing_ok=True)
        except OSError:
            pass

    def _stop_player(self):
        _reap(self.trace)
        _reap(self.player)
        if self.trace is not None and self.trace.stdout:
            self.trace.stdout.close()
        self.trace = self.player = None
        self.buffer.clear()
        if self.log is not None:
            self.log.close()
            self.log = None
        self.secret = ''

    def stop(self):
        try:
            if self.output:
                self.output.guard = lambda: True
                self.output.mute(True)
        finally:
            self._stop_player()
            self.state, self.position, self.speed, self.item = 'STOPPED', 0, 0, None
            self.uri, self.finished = '', False
            self.paused_since = None
            self.pending_resume = None

    def close(self):
        try:
            self.stop()
        finally:
            if self.output:
                self.output.close()
                self.output = None


class AudioOwner:
    """One worker owns audio; submit replaces intent rather than queuing starts."""
    def __init__(self, *, soloist_binary='soloist',
                 soloist_key=Path.home() / '.local/share/family-radio/spotify/soloist.key',
                 soloist_session=Path.home() / '.local/share/family-radio/spotify/soloist-session',
                 local_factory=None, spotify_factory=None, now=time.monotonic,
                 initial_volume=60, poll_interval=.05):
        self.factories = {LocalItem: local_factory or LocalBackend,
                          SpotifyItem: spotify_factory or (lambda: SoloistBackend(soloist_binary, soloist_key, soloist_session, now=now))}
        self.now, self.interval = now, poll_interval
        self.condition = threading.Condition()
        self.item, self.generation, self.playing, self.position = None, -1, False, 0
        self.stopped = True
        self.version = self.selection = 0
        self.action_revision = 0
        self.volume, self.volume_version = initial_volume, 0
        self.closed = False
        self.transitions = deque(maxlen=4)
        self.latest = None
        self.thread = threading.Thread(target=self._work, name='radio-audio', daemon=True)
        self.thread.start()

    def submit(self, command):
        with self.condition:
            if self.closed:
                return
            if isinstance(command, SetVolume):
                if type(command.percent) is not int or not 0 <= command.percent <= 100:
                    raise ValueError('volume must be an integer from 0 to 100')
                self.volume, self.volume_version = command.percent, self.volume_version + 1
            elif isinstance(command, Select):
                if not isinstance(command.item, (LocalItem, SpotifyItem)) or command.position_ms < 0:
                    raise ValueError('invalid selection')
                if command.generation < self.generation:
                    return
                self.item, self.generation = command.item, command.generation
                self.playing, self.position = command.playing, command.position_ms
                self.stopped = not command.playing
                self.version, self.selection = self.version + 1, self.selection + 1
                self.action_revision += 1
            elif isinstance(command, SetPaused):
                if command.generation != self.generation or self.item is None:
                    return
                self.playing, self.version = not command.paused, self.version + 1
                self.stopped = self.stopped and command.paused
                self.action_revision += 1
            elif isinstance(command, Stop):
                if command.generation < self.generation:
                    return
                self.item, self.generation, self.playing, self.position = None, command.generation, False, 0
                self.stopped = True
                self.version, self.selection = self.version + 1, self.selection + 1
                self.action_revision += 1
            else:
                raise TypeError('unknown audio command')
            if not isinstance(command, SetVolume):
                self.latest = None
                self.transitions.clear()
            self.condition.notify()

    def _guard(self, version):
        with self.condition:
            return not self.closed and self.version == version

    def _volume(self):
        with self.condition:
            return self.volume

    def _emit(self, event, action_revision=None):
        with self.condition:
            if (event.generation < self.generation
                    or action_revision is not None and action_revision != self.action_revision):
                return
            if event.error or event.ended:
                self.latest = None
                if not self.transitions or self.transitions[-1] != event:
                    self.transitions.append(event)
            else:
                self.latest = event

    def poll(self):
        with self.condition:
            events = list(self.transitions)
            self.transitions.clear()
            if self.latest is not None:
                events.append(self.latest)
                self.latest = None
            return events

    @staticmethod
    def _cleanup(backend):
        if backend is not None:
            try:
                backend.close()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                pass  # Cleanup errors must not terminate the worker.

    def _work(self):
        backend, kind, seen, selected, volume_seen = None, None, -1, -1, -1
        generation, item, last_position, duration = -1, None, 0, 0
        reported_end, applied_playing, failed_action = False, False, -1
        try:
            while True:
                with self.condition:
                    if self.closed:
                        break
                    version, selection = self.version, self.selection
                    target, generation, playing, stopped = self.item, self.generation, self.playing, self.stopped
                    position, volume_revision = self.position, self.volume_version
                    action_revision = self.action_revision
                guard = lambda: self._guard(version)
                if action_revision == failed_action:
                    with self.condition:
                        if not self.closed and self.action_revision == failed_action:
                            self.condition.wait(self.interval)
                    continue
                try:
                    if version != seen:
                        fresh = selection != selected
                        if target is None or (kind is not None and type(target) != kind):
                            if backend:
                                backend.close()
                            backend, kind = None, None
                        item = target
                        if item is None:
                            last_position, duration = 0, 0
                            self._emit(AudioEvent(generation, 'STOPPED', 0, 0), action_revision)
                        else:
                            duration = item.duration_ms
                            if fresh:
                                last_position, reported_end = position, False
                            if backend is None and (playing or isinstance(item, LocalItem)):
                                backend, kind = self.factories[type(item)](), type(item)
                            if playing and (fresh or not applied_playing):
                                reported_end = False
                                self._emit(AudioEvent(generation, 'BUFFERING', last_position, duration), action_revision)
                                backend.start(item, last_position if fresh else None, guard, self._volume)
                            elif not playing and backend and (fresh or applied_playing):
                                if fresh:
                                    backend.prepare(item, guard, self._volume)
                                else:
                                    backend.pause()
                            if backend is None:
                                self._emit(AudioEvent(generation, 'STOPPED', last_position, duration), action_revision)
                        seen, selected, applied_playing = version, selection, playing
                    if backend and volume_seen != volume_revision:
                        backend.set_volume(self._volume())
                        volume_seen = volume_revision
                    if backend and item is not None:
                        state, last_position, duration, ended = backend.status()
                        if ended and not playing:
                            ended = False
                            state = 'STOPPED' if stopped else 'PAUSED'
                        if not playing and stopped and state == 'PAUSED':
                            state = 'STOPPED'
                        if not guard():
                            continue
                        if ended and playing and not reported_end:
                            self._emit(AudioEvent(generation, 'STOPPED', last_position, duration, ended=True), action_revision)
                            reported_end = True
                            applied_playing = False
                            with self.condition:
                                if self.action_revision == action_revision:
                                    self.playing = False
                                    self.stopped = True
                        elif not ended:
                            self._emit(AudioEvent(generation, state, last_position, duration), action_revision)
                except Cancelled:
                    if backend:
                        try:
                            backend.pause()
                        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                            self._cleanup(backend)
                            backend, kind = None, None
                    applied_playing = False
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    code = 'SOURCE' if isinstance(exc, SourceError) else str(exc) if str(exc) in ('PLAYER', 'OUTPUT', 'ACCOUNT', 'EXPIRED') else 'PLAYER'
                    self._cleanup(backend)
                    backend, kind = None, None
                    applied_playing = False
                    with self.condition:
                        if self.action_revision != action_revision:
                            continue
                    seen, selected = version, selection
                    failed_action = action_revision
                    self._emit(AudioEvent(generation, 'ERROR', last_position, duration, error=code), action_revision)
                    with self.condition:
                        if self.action_revision == action_revision:
                            self.playing = False
                with self.condition:
                    if not self.closed and self.version == seen and self.volume_version == volume_revision:
                        self.condition.wait(self.interval)
        finally:
            self._cleanup(backend)

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise RuntimeError('audio worker did not stop')
