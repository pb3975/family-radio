#!/usr/bin/env python3
"""Local and parent-approved Spotify playback with USB Dial controls."""
import argparse
import calendar
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import time
import tty
import uuid
import unicodedata
import glob
import random
from collections import Counter
try:
    from host.library import atomic, load as load_library, tagged_artist
    from host.visuals import Visuals, SpotifyVisuals, ArtTransfer, level_history
    from host.playback import Mpv, SourceError, MPV_STARTUP_TIMEOUT, AudioOwner, LocalItem, SpotifyItem, Select, SetPaused, SetVolume, Stop
    from host.catalogs import Catalogs
    from host.wifi_control import WifiControl
    from host.clock import ClockSync
except ModuleNotFoundError:
    from library import atomic, load as load_library, tagged_artist
    from visuals import Visuals, SpotifyVisuals, ArtTransfer, level_history
    from playback import Mpv, SourceError, MPV_STARTUP_TIMEOUT, AudioOwner, LocalItem, SpotifyItem, Select, SetPaused, SetVolume, Stop
    from catalogs import Catalogs
    from wifi_control import WifiControl
    from clock import ClockSync

MAX_LINE = 1152
TIMEOUT = 3.0
MPV_STARTUP_TIMEOUT = 10.0


class Lines:
    """Discard oversized records through LF, including across reads."""
    def __init__(self):
        self.buffer = bytearray()
        self.dropping = False

    def feed(self, data):
        records = []
        for byte in data:
            if byte == 10:
                if not self.dropping:
                    try:
                        records.append(self.buffer.decode('ascii'))
                    except UnicodeDecodeError:
                        pass
                self.buffer.clear()
                self.dropping = False
            elif not self.dropping:
                self.buffer.append(byte)
                if len(self.buffer) > MAX_LINE:
                    self.buffer.clear()
                    self.dropping = True
        return records


def title_text(value, fallback="Local song"):
    value = value.translate(str.maketrans({'’': "'", '‘': "'", '“': '"', '”': '"', '–': '-', '—': '-'}))
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    value = ' '.join(''.join(c if 32 <= ord(c) <= 126 else ' ' for c in value).split())
    if not value:
        return title_text(fallback) if fallback != 'Local song' else 'Local song'
    return value if len(value) <= 64 else value[:30] + '...' + value[-31:]


def local_clock_minute(timestamp):
    """Civil minutes in the host timezone, or zero when its clock is unusable."""
    try:
        local = time.localtime(timestamp)
        if not 2024 <= local.tm_year <= 2099:
            return 0
        return calendar.timegm(local) // 60
    except (ValueError, OverflowError, OSError):
        return 0


def mixer(volume):
    subprocess.run(['amixer', '-q', '-D', 'default', 'sset', 'Master', str(volume) + '%'],
                   check=True, timeout=1, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Controller:
    """Serial-thread state. Blocking audio/catalog work belongs to its workers."""
    def __init__(self, player, set_volume, title='Local song', ceiling=100, initial=60,
                 songs=None, root=None, state_path=None, visuals=None, shuffle_items=None,
                 catalogs=None, spotify_visuals=None, wifi=None, wall_time=time.time, clock_ready=lambda: False):
        if not 0 <= initial <= 60 or not initial <= ceiling <= 100:
            raise ValueError('require initial <= 60 and initial <= ceiling <= 100')
        self.player, self.set_volume = player, set_volume
        self.wifi = wifi or WifiControl()
        self.wall_time, self.clock_ready = wall_time, clock_ready
        self.async_audio = hasattr(player, 'submit')
        self.local_songs = songs if songs is not None else [dict(id='synthetic', title=title, path='synthetic.wav')]
        self.songs = self.local_songs
        self.root = Path(root) if root else None
        self.visuals, self.spotify_visuals, self.catalogs = visuals, spotify_visuals, catalogs
        self.state_path = Path(state_path) if state_path else None
        self.source = 'L'
        self.current = self.cursor = 0 if self.songs else -1
        self.item_token = self.browser_token = 1
        self.scope = self.browse_scope = None
        self.shuffle = False
        self.shuffle_items = shuffle_items or random.shuffle
        self.browse_kind, self.browse_items, self.browse_until = 'B', [], 0
        self.session, self.seq, self.last_seen = '', 0, 0
        self.state, self.error, self.resume_state = 'STOPPED', '-', 'STOPPED'
        self.ceiling, self.volume = ceiling, initial
        self.volume_dirty = False
        self.dirty_at = None
        self.elapsed = self.duration = 0
        self.fatal = False
        self.pending_start = None
        self.catalog_serial = -1
        self._saved_spotify_id = None
        self._saved_spotify_scope = None
        self._build_artists()
        if self.state_path:
            try:
                saved = json.loads(self.state_path.read_text())
                if saved.get('version') in (1, 2, 3):
                    self.current = next((i for i, song in enumerate(self.songs) if song['id'] == saved.get('selected_id')), self.current)
                    self.shuffle = saved.get('shuffle') is True
                    scope = saved.get('scope')
                    if scope in self.artists and self.current >= 0 and self.artist(self.songs[self.current]) == scope:
                        self.scope = scope
                    if type(saved.get('volume')) is int:
                        self.volume = max(0, min(saved['volume'], initial, ceiling))
                    if saved.get('version') == 3 and saved.get('source') == 'S' and catalogs:
                        self.source = 'S'
                        self._saved_spotify_id = saved.get('selected_id')
                        self._saved_spotify_scope = tuple(scope) if isinstance(scope, list) and len(scope) == 2 and scope[0] in ('artist', 'playlist') and isinstance(scope[1], str) else None
                        self.songs, self.current, self.scope = [], -1, None
                        self._build_artists()
            except (OSError, ValueError, AttributeError, TypeError):
                pass
        self.cursor = max(0, self.current)
        self.rebuild_queue()
        try:
            self.set_volume(self.volume)
            if player is None:
                raise RuntimeError('player unavailable')
            if self.async_audio:
                self.player.submit(SetVolume(self.volume))
                if self.current >= 0:
                    self._load('STOPPED')
            elif self.root and self.current >= 0:
                self.select(self.current, 'STOPPED')
            else:
                player.pause(True)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self.fail('STARTUP', exc)

    @staticmethod
    def raw_artist(song):
        return tagged_artist(song) or 'Demo Artist'

    @staticmethod
    def artist_key(name):
        return unicodedata.normalize('NFKC', ' '.join(name.split())).casefold()

    def _build_artists(self):
        forms = {}
        for song in self.songs:
            raw = self.raw_artist(song)
            forms.setdefault(self.artist_key(raw), set()).add(raw)
        self.artist_names = {key: sorted(names, key=lambda name: (name.casefold(), name))[0] for key, names in forms.items()}
        if self.source == 'S':
            groups = self._groups('artist')
            self.artists = [g['id'] for g in groups]
            names = {g['id']: g['name'] for g in groups}
        else:
            self.artists = sorted(self.artist_names.values(), key=lambda name: (name.casefold(), name))
            names = {name: name for name in self.artists}
        labels = [title_text(names[name]) for name in self.artists]
        label_counts = Counter(labels)
        self.artist_labels = {name: label if label_counts[label] == 1 else label[:56]+f' [{i+1}]'
                              for i, (name, label) in enumerate(zip(self.artists, labels))}
        self.song_index = {song['id']: i for i, song in enumerate(self.songs)}

    def artist(self, song):
        raw = self.raw_artist(song)
        return self.artist_names.get(self.artist_key(raw), raw)

    def _groups(self, kind):
        if not self.catalogs or not self.catalogs.approvals:
            return []
        existing = {(g['kind'], g['id']): g for g in self.catalogs.catalog['groups']}
        groups = []
        for grant in self.catalogs.approvals['approvals']:
            if grant['kind'] == kind:
                name = grant.get('label') or kind.title()
                status = self.catalogs.group_status.get((kind, grant['id']))
                if status in ('AUTH', 'OFFLINE', 'ERROR'):
                    name += ' unavailable'
                groups.append(existing.get((kind, grant['id']), dict(kind=kind, id=grant['id'], name=name, track_ids=[])))
        return groups

    def scope_items(self, scope):
        if self.source == 'S':
            if scope is None:
                return list(range(len(self.songs)))
            grants = {(a['kind'], a['id']) for a in self.catalogs.approvals['approvals']} if self.catalogs and self.catalogs.approvals else set()
            group = next((g for g in self.catalogs.catalog['groups'] if (g['kind'], g['id']) == tuple(scope)), None) if self.catalogs and tuple(scope) in grants else None
            return [self.song_index[identity] for identity in group['track_ids'] if identity in self.song_index] if group else []
        return [i for i, song in enumerate(self.songs) if scope is None or self.artist(song) == scope]

    def rebuild_queue(self):
        items = self.scope_items(self.scope)
        if self.current >= 0 and self.current not in items:
            items.insert(0, self.current)
        if self.shuffle and self.current in items:
            items.remove(self.current)
            self.shuffle_items(items)
            items.insert(0, self.current)
        self.completed_ids = set()
        self.queue = items
        self.queue_position = items.index(self.current) if self.current in items else 0

    def _view_changed(self):
        if self.browser_token == 2147483647:
            self.browser_token = 1
            self.browse_until = 0
            self.session = uuid.uuid4().hex[:16] if self.session else ''
            self.seq = 0
            self._pause()
            if self.state in ('PLAYING', 'BUFFERING'):
                self.state = 'PAUSED'
        else:
            self.browser_token += 1

    def _next_item(self):
        if self.item_token == 2147483647:
            raise RuntimeError('selection token exhausted; restart the stopped host')
        else:
            self.item_token += 1
        self.pending_start = None
        self._view_changed()

    def _close_browser(self):
        if self.browse_until:
            self.browse_until = 0
            self._view_changed()

    def _mode(self):
        return self.browse_kind if self.browse_until else 'N'

    def open_browser(self, kind, now, scope=None):
        self.browse_kind, self.browse_scope = kind, scope
        if kind == 'R':
            self.browse_items = ['L', 'S']
        elif kind == 'P':
            self.browse_items = [g['id'] for g in self._groups('playlist')]
        elif kind == 'A':
            self.browse_items = list(self.artists)
        else:
            self.browse_items = self.scope_items(scope)
        self.cursor = self.browse_items.index(self.current) if kind == 'B' and self.current in self.browse_items else 0
        self._view_changed()
        self.browse_until = now + 8

    def _pause(self, paused=True):
        self.pending_start = None
        if self.async_audio:
            self.player.submit(SetPaused(self.item_token, paused))
        else:
            self.player.pause(paused)

    def _stop(self):
        self.pending_start = None
        if self.async_audio:
            self.player.submit(Stop(self.item_token))
        else:
            self.player.pause(True)

    def _play(self):
        if self.current < 0:
            return
        self._saved_spotify_id = self._saved_spotify_scope = None
        self.resume_state = 'PLAYING'
        if self.source == 'S':
            self.catalogs.tick(selected=False, force=True)
            if not self.catalogs.permitted(self.songs[self.current]['id']):
                self.pending_start = self.item_token
                self.state, self.error = 'BUFFERING', '-'
                self.catalogs.tick(selected=True, force=True)
                return
        if self.async_audio:
            self.player.submit(SetPaused(self.item_token, False))
            self.state = 'BUFFERING'
        else:
            self.player.play()
            self.state = 'PLAYING'
        self.error = '-'

    def _load(self, state):
        song = self.songs[self.current]
        playing = state == 'PLAYING'
        if playing and self.source == 'S':
            self.catalogs.tick(selected=False, force=True)
            if not self.catalogs.permitted(song['id']):
                self.pending_start = self.item_token
                playing, state = False, 'BUFFERING'
                self.catalogs.tick(selected=True, force=True)
        if self.async_audio:
            item = SpotifyItem('spotify:' + song['id'], song['uri'], song['duration_ms']) if self.source == 'S' else LocalItem('local:' + song['id'], self.root / song['path'] if self.root else Path(song['path']), round(song.get('duration', 0) * 1000))
            self.player.submit(Select(self.item_token, item, playing))
            self.state = 'BUFFERING' if playing else state
        else:
            if self.root:
                self.player.load(self.root / song['path'])
            if playing:
                self.player.play()
            self.state = state
        self.error = '-'

    def select(self, index, state):
        self._saved_spotify_id = self._saved_spotify_scope = None
        if not self.songs:
            self.current = -1
            self._next_item()
            self._stop()
            self.state, self.elapsed, self.duration = 'STOPPED', 0, 0
            return
        self.current = max(0, min(len(self.songs)-1, index))
        self._next_item()
        self.elapsed = self.duration = 0
        self.resume_state = state
        try:
            self._load(state)
        except SourceError as exc:
            self._pause()
            self.state, self.error = 'ERROR', 'SOURCE'
            print(f'SOURCE: {exc}', file=sys.stderr, flush=True)

    def advance(self, direction, now):
        if not self.queue:
            return False
        position = max(0, min(len(self.queue)-1, self.queue_position+direction))
        if position != self.queue_position:
            if direction > 0 and self.current >= 0:
                self.completed_ids.add(self.songs[self.current]['id'])
            self.queue_position = position
            intent = self.state if self.state in ('PLAYING', 'PAUSED', 'STOPPED') else self.resume_state
            if self.state == 'BUFFERING':
                intent = 'PLAYING'
            self.select(self.queue[position], intent)
            self.dirty_at = now
            return True
        return False

    def switch_source(self, source, now):
        if source not in ('L', 'S') or source == self.source:
            self._close_browser()
            return
        self._saved_spotify_id = self._saved_spotify_scope = None
        self._next_item()
        self._stop()
        self.source, self.scope = source, None
        self.songs = self.local_songs if source == 'L' else []
        self.current = 0 if self.songs else -1
        self.queue, self.queue_position = [], 0
        self.state, self.error, self.elapsed, self.duration = 'STOPPED', '-', 0, 0
        self._build_artists()
        if source == 'S':
            self.catalog_serial = -1
            self._catalog_tick(now)
        elif self.current >= 0:
            self._load('STOPPED')
        self.rebuild_queue()
        self.open_browser('P' if source == 'S' and self._groups('playlist') else 'A' if source == 'S' else 'B', now)
        self.dirty_at = now

    def _catalog_tick(self, now):
        if not self.catalogs:
            return
        serial = self.catalogs.tick(selected=self.source == 'S')
        if self.source != 'S':
            return
        if serial != self.catalog_serial:
            self.catalog_serial = serial
            selected_id = self.songs[self.current]['id'] if self.current >= 0 else None
            old_id = self._saved_spotify_id or selected_id
            had_selection = selected_id is not None and selected_id == old_id
            old_queue = [self.songs[i]['id'] for i in self.queue] if hasattr(self, 'queue') else []
            old_view = [self.songs[i]['id'] for i in self.browse_items] if self.browse_kind == 'B' and self.browse_until else list(self.browse_items)
            old_cursor = old_view[self.cursor] if self.browse_until and 0 <= self.cursor < len(old_view) else None
            old_position = self.queue_position if hasattr(self, 'queue_position') else 0
            grants = {(a['kind'], a['id']) for a in self.catalogs.approvals['approvals']} if self.catalogs.approvals else set()
            visible = set()
            for group in self.catalogs.catalog['groups']:
                if (group['kind'], group['id']) in grants:
                    visible.update(group['track_ids'])
            self.songs = [dict(track, artist=', '.join(a['name'] for a in track['artists']))
                          for identity, track in self.catalogs.catalog['tracks'].items() if identity in visible]
            self._build_artists()
            self.current = self.song_index.get(old_id, -1)
            if self.current < 0:
                self._next_item()
                self._stop()
                self.current = 0 if self.songs else -1
                self.state, self.error, self.elapsed, self.duration = 'STOPPED', '-', 0, 0
                if self.current >= 0:
                    self._load('STOPPED')
            elif not had_selection:
                self._next_item()
                self._load('STOPPED')
            if self._saved_spotify_scope and self.current in self.scope_items(self._saved_spotify_scope):
                self.scope = self._saved_spotify_scope
            complete = {(group['kind'], group['id']) for group in self.catalogs.catalog['groups']}
            scope_removed = self._saved_spotify_scope is not None and self._saved_spotify_scope not in grants
            scope_ready = self._saved_spotify_scope is None or self._saved_spotify_scope in complete
            selection_known = self._saved_spotify_id in self.song_index or grants <= complete
            if self.catalogs.readable and (scope_removed or scope_ready and selection_known):
                self._saved_spotify_id = self._saved_spotify_scope = None
            items = self.scope_items(self.scope)
            item_set = set(items)
            retained = [self.song_index[identity] for identity in old_queue if identity in self.song_index and (self.song_index[identity] in item_set or self.song_index[identity] == self.current)]
            if self.shuffle and not had_selection and self.current >= 0:
                retained = [self.current]
            retained_set = set(retained)
            additions = [index for index in items if index not in retained_set and self.songs[index]['id'] not in self.completed_ids]
            if self.shuffle:
                self.shuffle_items(additions)
            self.queue = retained + additions
            self.queue_position = self.queue.index(self.current) if self.current in self.queue else min(old_position, max(0, len(self.queue)-1))
            if self.browse_until:
                if self.browse_kind == 'B':
                    self.browse_items = self.scope_items(self.browse_scope)
                    new_view = [self.songs[i]['id'] for i in self.browse_items]
                elif self.browse_kind == 'A':
                    self.browse_items = list(self.artists)
                    new_view = self.browse_items
                elif self.browse_kind == 'P':
                    self.browse_items = [g['id'] for g in self._groups('playlist')]
                    new_view = self.browse_items
                else:
                    new_view = self.browse_items
                self.cursor = new_view.index(old_cursor) if old_cursor in new_view else min(self.cursor, max(0, len(new_view)-1))
                if new_view != old_view:
                    self._view_changed()
            elif [self.songs[i]['id'] for i in self.queue] != old_queue:
                self._view_changed()
        if self.current >= 0 and self.state in ('PLAYING', 'BUFFERING', 'PAUSED') and not self.pending_start:
            if not self.catalogs.permitted(self.songs[self.current]['id'], start=False):
                self._pause()
                self.state, self.error = 'PAUSED', 'REVOKED' if self.catalogs.approvals and self.catalogs.readable else 'CATALOG'
                self._view_changed()
        if self.pending_start and self.pending_start == self.item_token:
            if self.current >= 0 and self.catalogs.permitted(self.songs[self.current]['id']):
                self.pending_start = None
                self._play()
            elif (self.current < 0 or not self.catalogs.pending_for(self.songs[self.current]['id'])) and self.catalogs.status != 'LOADING':
                self.pending_start = None
                self.state, self.error = 'ERROR', self.catalogs.error or 'REVOKED'

    def save(self):
        if self.state_path and self.dirty_at is not None:
            identity = self.songs[self.current]['id'] if self.current >= 0 else None
            scope = self.scope
            if self.source == 'S' and self._saved_spotify_id is not None:
                identity, scope = self._saved_spotify_id, self._saved_spotify_scope
            atomic(self.state_path, (json.dumps(dict(version=3, source=self.source, selected_id=identity,
                    volume=self.volume, shuffle=self.shuffle, scope=scope)) + '\n').encode())
            self.dirty_at = None

    def fail(self, code, exc):
        self.state, self.error = 'ERROR', code
        self.volume_dirty = False
        print(f'{code}: {exc}', file=sys.stderr, flush=True)
        self.fatal = True
        if self.player:
            self.player.close()

    def disconnect(self):
        self.wifi.close()
        self.session, self.seq, self.last_seen = '', 0, 0
        self._close_browser()
        if not self.fatal:
            try:
                self._pause()
                if self.state in ('PLAYING', 'BUFFERING'):
                    self.state = 'PAUSED'
                if self.resume_state == 'PLAYING':
                    self.resume_state = 'PAUSED'
            except (OSError, RuntimeError) as exc:
                self.fail('PLAYER', exc)

    def apply_volume(self):
        if self.volume_dirty and not self.fatal:
            try:
                if self.async_audio:
                    self.player.submit(SetVolume(self.volume))
                else:
                    self.set_volume(self.volume)
                self.volume_dirty = False
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                self.fail('OUTPUT', exc)

    def receive(self, record, now):
        if self.browse_until and now >= self.browse_until:
            self._close_browser()
        if self.session and now - self.last_seen >= TIMEOUT:
            self.disconnect()
        if record == 'B8\tREADY':
            self.disconnect()
            self.session = uuid.uuid4().hex[:16]
            self.last_seen = now
            self.browser_token = 1
            return
        f = record.split('\t')
        if len(f) == 7 and f[:2] == ['B8', 'W']:
            if (not self.session or f[2] != self.session or not re.fullmatch('[0-9]{1,10}', f[3])
                    or not self.seq < int(f[3]) <= 2147483647 or not re.fullmatch('[0-9]{1,10}', f[4])
                    or not 1 <= int(f[4]) <= 2147483647 or f[5] not in ('P', 'S', 'Q', 'T', 'X')
                    or f[6] != '-' and not re.fullmatch('[0-9a-f]{32}', f[6])):
                return
            self.seq, self.last_seen = int(f[3]), now
            self.wifi.action(self.session, int(f[4]), f[5], None if f[6] == '-' else f[6])
            return
        if len(f) != 8 or f[:2] != ['B8', 'E'] or not self.session or f[2] != self.session:
            return
        if not re.fullmatch(r'[0-9]{1,10}', f[3]) or not self.seq < int(f[3]) <= 2147483647:
            return
        if not re.fullmatch(r'-?[0-9]{1,10}', f[5]) or f[6] not in ('N', 'B', 'A', 'P', 'R') or not re.fullmatch(r'[0-9]{1,10}', f[7]):
            return
        kind, value, mode, token = f[4], int(f[5]), f[6], int(f[7])
        if not 1 <= token <= 2147483647:
            return
        if not ((kind == 'H' and value == 0) or (kind == 'L' and value in (-1, 0, 1, 2, 3))
                or (kind == 'Q' and value in (0, 1)) or (kind == 'K' and -20 <= value <= 20)
                or (kind == 'P' and ((mode == 'N' and value in (0, 1)) or (mode != 'N' and 0 <= value < len(self.browse_items))))
                or (kind == 'S' and value in (-1, 1))):
            return
        if kind != 'H' and (mode != self._mode() or (not (mode == 'N' and kind in ('K', 'Q')) and token != self.browser_token)):
            return
        self.seq, self.last_seen = int(f[3]), now
        if self.fatal:
            return
        try:
            if kind == 'L':
                if value == -1:
                    self._close_browser()
                else:
                    self.open_browser({0: 'B', 1: 'A', 2: 'P', 3: 'R'}[value], now)
            elif kind == 'Q':
                if self.shuffle != bool(value):
                    self.shuffle = bool(value)
                    self.rebuild_queue()
                    self.dirty_at = now
                    self._view_changed()
            elif kind == 'K':
                if mode != 'N':
                    self.cursor = max(0, min(max(0, len(self.browse_items)-1), self.cursor+value))
                    self.browse_until = now+8
                else:
                    self.volume = max(0, min(self.ceiling, self.volume + value*2))
                    self.volume_dirty, self.dirty_at = True, now
            elif kind == 'P':
                self.apply_volume()
                if mode == 'R':
                    self.switch_source(self.browse_items[value], now)
                elif mode in ('A', 'P'):
                    group = self.browse_items[value]
                    self.open_browser('B', now, (('artist' if mode == 'A' else 'playlist'), group) if self.source == 'S' else group)
                elif mode == 'B':
                    self.scope = self.browse_scope
                    self.select(self.browse_items[value], 'PLAYING')
                    self.rebuild_queue()
                    self._close_browser()
                    self.dirty_at = now
                elif self.current >= 0:
                    if value == 0:
                        self._pause()
                        self.state, self.error, self.resume_state = 'PAUSED', '-', 'PAUSED'
                    elif self.error in ('SOURCE', 'REVOKED', 'CATALOG', 'NETWORK', 'ACCOUNT'):
                        self.select(self.current, 'PLAYING')
                    else:
                        self._play()
            elif kind == 'S':
                self.advance(value, now)
                self._close_browser()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self.fail('PLAYER', exc)

    def tick(self, now):
        self.wifi.tick()
        if self.session and now - self.last_seen >= TIMEOUT:
            self.disconnect()
        if self.browse_until and now >= self.browse_until:
            self._close_browser()
        self._catalog_tick(now)
        self.apply_volume()
        if not self.fatal:
            try:
                if self.async_audio:
                    for event in self.player.poll():
                        if event.generation != self.item_token:
                            continue
                        self.elapsed, self.duration = event.position_ms / 1000, event.duration_ms / 1000
                        if event.error:
                            self.state, self.error = 'ERROR', event.error
                        elif event.ended:
                            if self.state in ('PLAYING', 'BUFFERING') and not self.pending_start and not self.advance(1, now):
                                self._pause()
                                self.state = 'STOPPED'
                        elif not self.pending_start:
                            # A stale PLAYING report cannot undo an explicit pause/disconnect.
                            if event.state in ('PLAYING', 'BUFFERING') and self.state == 'PAUSED':
                                self._pause()
                            else:
                                self.state = event.state
                                if self.state != 'ERROR' and self.error not in ('REVOKED', 'CATALOG', 'NETWORK', 'ACCOUNT'):
                                    self.error = '-'
                else:
                    if hasattr(self.player, 'progress') and self.error != 'SOURCE':
                        self.elapsed, self.duration = self.player.progress()
                    if self.state == 'PLAYING' and self.player.ended():
                        if not self.advance(1, now):
                            self.player.pause(True)
                            self.state = 'STOPPED'
            except SourceError as exc:
                self.resume_state = self.state
                self._pause()
                self.state, self.error = 'ERROR', 'SOURCE'
            except (OSError, RuntimeError) as exc:
                self.fail('PLAYER', exc)
        if self.dirty_at is not None and now-self.dirty_at >= 2:
            self.save()

    def asset(self):
        if self.current < 0:
            return b'', b'', '-'
        if self.source == 'S':
            return self.spotify_visuals.get(self.songs[self.current]) if self.spotify_visuals else (b'', b'', '-')
        return self.visuals.get(self.songs[self.current]) if self.visuals else (b'', b'', '-')

    def _row_label(self, position, mode):
        if not 0 <= position < len(self.browse_items):
            return ''
        item = self.browse_items[position]
        if mode == 'R':
            return 'Local songs' if item == 'L' else 'Spotify'
        if mode == 'A':
            return self.artist_labels[item]
        if mode == 'P':
            return title_text(next(g['name'] for g in self._groups('playlist') if g['id'] == item))
        return title_text(self.songs[item]['title'])

    def snapshot(self):
        mode = self._mode()
        current = self.songs[self.current] if self.current >= 0 else None
        cursor = self.cursor if mode != 'N' else max(0, self.current)
        label = (self._row_label(cursor, mode) or {'B':'No songs','A':'No artists','P':'No playlists','R':'Sources'}[mode]) if mode != 'N' else title_text(current['title']) if current else 'No songs'
        status = self.catalogs.status if self.source == 'S' and self.catalogs else 'AUTH' if self.source == 'S' else 'READY' if self.songs else 'EMPTY'
        title = title_text(current['title']) if current else 'Spotify loading' if status == 'LOADING' else 'No songs'
        scope = self.browse_scope if mode == 'B' else self.scope
        if self.source == 'S' and mode == 'B' and not self.browse_items and scope and self.catalogs:
            status = self.catalogs.group_status.get(tuple(scope), status)
        scope_label = 'All songs'
        if scope:
            scope_label = title_text(next((g['name'] for g in self.catalogs.catalog['groups'] if (g['kind'], g['id']) == tuple(scope)), 'Songs')) if self.source == 'S' else self.artist_labels.get(scope, 'Songs')
        art, levels, key = self.asset()
        fields = ['B8', 'S', self.session, self.state, str(self.volume), str(self.ceiling), title, self.error, mode,
                  str(self.item_token if current else 0), str(cursor), str(len(self.songs)),
                  str(max(0, min(999999, int(self.elapsed)))), str(max(0, min(999999, int(self.duration)))), label,
                  key, (bytes(12) if self.source == 'S' else level_history(levels, self.elapsed, self.state == 'PLAYING')).hex(),
                  title_text(self.raw_artist(current)) if current else 'Spotify' if self.source == 'S' else 'Local songs', scope_label,
                  '1' if self.shuffle else '0', str(self.browser_token), str(len(self.browse_items) if mode != 'N' else len(self.songs))]
        fields += [self._row_label(cursor+delta, mode) if mode != 'N' else '' for delta in (-2, -1, 1, 2)]
        fields += [self.source, status, str(local_clock_minute(self.wall_time()) if self.clock_ready() else 0)]
        data = ('\t'.join(fields)+'\n').encode('ascii')
        assert len(data) <= 1153
        return data


class SerialLink:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            tty.setraw(self.fd)
            attrs = termios.tcgetattr(self.fd)
            attrs[4] = attrs[5] = termios.B115200
            attrs[2] |= termios.CLOCAL | termios.CREAD
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except Exception:
            os.close(self.fd)
            raise
        self.lines = Lines()
        self.pending = bytearray()
        self.pending_wifi = None
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.fd, selectors.EVENT_READ)

    def read(self, timeout=0.05):
        if not self.selector.select(timeout):
            return []
        try:
            data = os.read(self.fd, 1024)
        except BlockingIOError:
            return []
        if not data:
            raise OSError('serial EOF')
        return self.lines.feed(data)

    @staticmethod
    def wifi_identity(record):
        fields = record.split(b'\t', 8)
        return tuple(fields[:8]) if len(fields) > 8 and fields[:2] == [b'B8', b'W'] else None

    def scrub_wifi(self, record=None):
        if self.pending_wifi is not None and self.pending_wifi != self.wifi_identity(record or b''):
            # End an abandoned partial line before the next normal snapshot.
            self.pending[:] = b'\0' * len(self.pending)
            self.pending = bytearray(b'\n')
            self.pending_wifi = None

    def send(self, record):
        # Finish one partial record before accepting a new snapshot. No queue.
        if not self.pending:
            self.pending = bytearray(record)
            self.pending_wifi = self.wifi_identity(record)
        try:
            written = os.write(self.fd, self.pending)
            self.pending[:written] = b'\0' * written
            del self.pending[:written]
            if not self.pending:
                self.pending_wifi = None
        except BlockingIOError:
            pass

    def close(self):
        self.pending[:] = b'\0' * len(self.pending)
        self.pending.clear()
        self.pending_wifi = None
        self.selector.close()
        os.close(self.fd)


def run(controller, device):
    link = None
    transfer = ArtTransfer()
    open_error_logged = False
    retry_at = next_state = next_poll = next_wifi = 0
    try:
        while True:
            now = time.monotonic()
            if link is None and now >= retry_at:
                try:
                    matches = glob.glob(device)
                    if len(matches) != 1:
                        raise OSError('Dial path must resolve to exactly one device')
                    link = SerialLink(matches[0])
                    open_error_logged = False
                except OSError as exc:
                    if not open_error_logged:
                        print(f'Cannot open Dial: {exc}; retrying', file=sys.stderr, flush=True)
                        open_error_logged = True
                    retry_at = now + 1
            if link:
                try:
                    transitions = 0
                    for record in link.read(0.005 if transfer.active else 0.02):
                        transfer.acknowledge(record, controller)
                        fields = record.split('\t')
                        transition = len(fields) == 8 and fields[4] in ('P', 'S')
                        if transition and transitions:
                            continue  # No backlog: bound slow player loads per read.
                        previous_seq = controller.seq
                        controller.receive(record, time.monotonic())
                        if transition and controller.seq != previous_seq:
                            transitions += 1
                    current_wifi_record = controller.wifi.record()
                    link.scrub_wifi(current_wifi_record)
                    if controller.session:
                        if transfer.sync(controller):
                            next_state = 0  # Bind every new image identity before its first chunk.
                        wifi_record = current_wifi_record if now >= next_wifi else None
                        if link.pending:
                            link.send(b'')
                        elif wifi_record and now >= next_wifi and next_wifi < next_state:
                            link.send(wifi_record)
                            next_wifi = now + .3
                        elif now >= next_state:
                            link.send(controller.snapshot())
                            next_state = now + 0.1
                        elif wifi_record:
                            link.send(wifi_record)
                            next_wifi = now + .3
                        else:
                            art = transfer.record(controller, now)
                            if art:
                                link.send(art)
                except OSError:
                    link.close()
                    link = None
                    retry_at = now + 1
                    controller.disconnect()
            else:
                time.sleep(0.05)
            if now >= next_poll:
                controller.tick(time.monotonic())
                next_poll = now + 0.1
                if controller.fatal:
                    if link and controller.session:
                        link.send(controller.snapshot())
                    raise RuntimeError(controller.error)
    finally:
        controller.disconnect()
        try:
            controller.save()
        finally:
            controller.wifi.shutdown()
            if controller.player:
                controller.player.close()
            if link:
                link.close()


def stop_on_signal(signum, frame):
    raise KeyboardInterrupt


def main():
    signal.signal(signal.SIGTERM, stop_on_signal)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, default=Path.home() / '.local/share/family-radio/library/manifest.json')
    parser.add_argument('--visuals', type=Path, default=Path.home() / '.local/share/family-radio/visuals/manifest.json')
    parser.add_argument('--state', type=Path, default=Path.home() / '.local/share/family-radio/state.json')
    parser.add_argument('--device', default=os.environ.get('FAMILY_RADIO_DEVICE', '/dev/serial/by-id/*'), help='stable by-id path or glob resolving to exactly one target')
    parser.add_argument('--ceiling', type=int, default=100)
    parser.add_argument('--initial', type=int, default=60)
    parser.add_argument('--spotify-directory', type=Path, default=Path.home() / '.local/share/family-radio/spotify')
    parser.add_argument('--approvals', type=Path, default=Path.home() / '.local/share/family-radio/approvals.sqlite3')
    parser.add_argument('--catalog', type=Path, default=Path.home() / '.local/share/family-radio/spotify/catalog.json')
    parser.add_argument('--soloist', default='soloist')
    parser.add_argument('--soloist-key', type=Path, default=Path.home() / '.local/share/family-radio/spotify/soloist.key')
    parser.add_argument('--soloist-session', type=Path, default=Path.home() / '.local/share/family-radio/spotify/soloist-session')
    args = parser.parse_args()
    if not args.device.startswith('/dev/serial/by-id/'):
        parser.error('use a stable /dev/serial/by-id/ path')
    player = catalogs = spotify_visuals = None
    try:
        library = load_library(args.library)
        catalogs = Catalogs(args.approvals, args.spotify_directory, args.catalog)
        spotify_visuals = SpotifyVisuals()
        player = AudioOwner(soloist_binary=args.soloist, soloist_key=args.soloist_key,
                            soloist_session=args.soloist_session, initial_volume=args.initial)
        controller = Controller(player, lambda volume: None, songs=library['songs'], root=args.library.resolve().parent,
                                ceiling=args.ceiling, initial=args.initial, state_path=args.state,
                                visuals=Visuals(args.visuals), catalogs=catalogs, spotify_visuals=spotify_visuals)
        with ClockSync() as clock:
            controller.clock_ready = clock.ready
            run(controller, args.device)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr, flush=True)
        return 1
    finally:
        if player:
            player.close()
        if catalogs:
            catalogs.close()
        if spotify_visuals:
            spotify_visuals.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
