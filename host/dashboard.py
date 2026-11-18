#!/usr/bin/env python3
"""Private parent editor for Spotify approvals and the local radio library."""
import argparse
import base64
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import ipaddress
import json
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
import zlib

try:
    from host import approvals, catalogs, library, spotify, visuals
except ModuleNotFoundError:
    import approvals, catalogs, library, spotify, visuals

DEFAULT_DIRECTORY = Path.home() / '.local/share/family-radio'
JSON_LIMIT = 64 * 1024
AUDIO_LIMIT = 200 * 1024 * 1024
IMAGE_LIMIT = 16 * 1024 * 1024
SESSION_SECONDS = 30 * 86400
COOKIE = 'family_radio_parent'


class Problem(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def edit_manifest(manifest, operation):
    """Return a new queue. File and HTTP validation belong to the caller."""
    result = deepcopy(manifest)
    songs = result['songs']
    action = operation['action']
    if action == 'add':
        song = operation['song']
        if any(s['id'] == song['id'] for s in songs):
            raise ValueError('This file is already in the library.')
        songs.append(song)
        return result
    index = next((i for i, s in enumerate(songs) if s['id'] == operation['id']), None)
    if index is None:
        raise ValueError('This song is no longer in the library. Reload the page.')
    song = songs[index]
    if action == 'edit':
        song['title'] = operation['title']
        if operation['artist']:
            song['artist'] = operation['artist']
        else:
            song.pop('artist', None)
            song['metadata'] = {k: v for k, v in song['metadata'].items() if k.lower() != 'artist'}
            if 'stream_metadata' in song:
                song['stream_metadata'] = [{k: v for k, v in tags.items() if not isinstance(k, str) or k.lower() != 'artist'}
                                           if isinstance(tags, dict) else tags for tags in song['stream_metadata']]
    elif action == 'move':
        target = index + operation['direction']
        if 0 <= target < len(songs):
            songs[index], songs[target] = songs[target], songs[index]
    elif action == 'remove':
        if len(songs) == 1:
            raise ValueError('Keep at least one song. Add another song before removing this one.')
        songs.pop(index)
    elif action == 'cover':
        song['cover_sha256'] = operation['sha256']
    else:
        raise ValueError('Unknown library action.')
    return result


class Dashboard:
    def __init__(self, directory, transport=spotify.request_json, now=time.time):
        self.directory = Path(directory).expanduser().resolve()
        self.private = self.directory / 'dashboard'
        self.private.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.private.is_symlink() or self.private.stat().st_mode & 0o077:
            raise ValueError('Dashboard directory must have mode 0700 and must not be a symlink.')
        self.token_path = self.private / 'token'
        self.sessions_path = self.private / 'sessions.json'
        self.mutex = threading.RLock()
        self.session_lock = threading.Lock()
        self.sessions, self.sessions_stamp = {}, None
        self.now, self.transport = now, transport
        self.store = approvals.Store(self.directory / 'approvals.sqlite3')
        self.store.initialize()
        self.library_path = self.directory / 'library/manifest.json'
        self.visuals_path = self.directory / 'visuals/manifest.json'
        self.spotify_directory = self.directory / 'spotify'
        self.catalog_path = self.spotify_directory / 'catalog.json'
        self.page = Path(__file__).with_name('dashboard.html').read_bytes()
        hashes = []
        for tag in (b'script', b'style'):
            hashes.append("'sha256-" + base64.b64encode(hashlib.sha256(
                re.search(b'<' + tag + b'>(.*?)</' + tag + b'>', self.page, re.S)[1]).digest()).decode() + "'")
        self.csp = ("default-src 'none'; script-src " + hashes[0] + "; style-src " + hashes[1]
                    + "; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        with self.writer():
            if not self.token_path.exists():
                library.atomic(self.token_path, secrets.token_urlsafe(32).encode())
            self.token()

    def sweep_uploads(self):
        """Only the serving process may call this; another command could delete a live upload."""
        for leftover in self.private.glob('upload-*'):
            shutil.rmtree(leftover, ignore_errors=True)

    def current_sessions(self):
        """Reread on change so deleting or replacing the file takes effect at once."""
        try:
            info = self.sessions_path.lstat()
            stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
        except FileNotFoundError:
            self.sessions, self.sessions_stamp = {}, None
            return self.sessions
        if stamp != self.sessions_stamp:
            self.sessions_stamp, self.sessions = stamp, {}
            try:
                if not self.sessions_path.is_symlink() and not info.st_mode & 0o077:
                    value = json.loads(self.sessions_path.read_text())
                    self.sessions = {k: v for k, v in value['sessions'].items()
                                     if re.fullmatch('[0-9a-f]{64}', k) and type(v) in (int, float)}
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
        return self.sessions

    @contextmanager
    def writer(self):
        with self.mutex:
            fd = os.open(self.private / '.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def token(self):
        if (self.token_path.is_symlink() or not self.token_path.is_file()
                or self.token_path.stat().st_mode & 0o077 or self.token_path.stat().st_size > 128):
            raise ValueError('Dashboard token must be a private file with mode 0600.')
        value = self.token_path.read_text()
        if not re.fullmatch('[A-Za-z0-9_-]{43}', value):
            raise ValueError('Invalid dashboard token file.')
        return value

    def login(self, value):
        with self.writer():
            if not secrets.compare_digest(value.encode(), self.token().encode()):
                raise Problem(403, 'This login link is invalid or has already been used. Generate a new link.')
            session = secrets.token_urlsafe(32)
            library.atomic(self.token_path, secrets.token_urlsafe(32).encode())
            with self.session_lock:
                sessions = {s: expiry for s, expiry in self.current_sessions().items() if expiry > self.now()}
                if len(sessions) >= 100:
                    sessions.pop(min(sessions, key=sessions.get))
                sessions[hashlib.sha256(session.encode()).hexdigest()] = self.now() + SESSION_SECONDS
                library.atomic(self.sessions_path, encoded(dict(version=1, sessions=sessions)))
                self.current_sessions()
            return session

    def authenticated(self, header):
        try:
            cookies = SimpleCookie()
            cookies.load(header or '')
            session = cookies[COOKIE].value if COOKIE in cookies else ''
            with self.session_lock:
                return self.current_sessions().get(hashlib.sha256(session.encode()).hexdigest(), 0) > self.now()
        except CookieError:
            return False

    def read_library(self):
        if not self.library_path.exists():
            return dict(version=1, songs=[]), hashlib.sha256(b'').hexdigest()
        if self.library_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('Local library manifest is too large.')
        raw = self.library_path.read_bytes()
        value = library.checked_manifest(json.loads(raw), self.library_path.parent)
        return value, hashlib.sha256(raw).hexdigest()

    def require_revision(self, revision):
        manifest, current = self.read_library()
        if revision != current:
            raise Problem(409, 'The library changed in another window. Reloaded the latest list. Try your edit again.')
        return manifest

    def write_library(self, value):
        library.checked_manifest(value, self.library_path.parent)
        raw = encoded(value)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('Local library manifest is too large.')
        library.atomic(self.library_path, raw)

    def state(self):
        snapshot = self.store.snapshot()
        groups = {}
        try:
            if self.catalog_path.stat().st_size <= 32 * 1024 * 1024:
                catalog = catalogs.checked_catalog(json.loads(self.catalog_path.read_text()))
                groups = {(g['kind'], g['id']): dict(g, verified_at=g.get('verified_at', catalog['generated_at']))
                          for g in catalog['groups']}
        except (OSError, ValueError, TypeError, KeyError):
            pass
        for grant in snapshot['approvals']:
            group = groups.get((grant['kind'], grant['id']))
            grant.update(name=grant['label'] or (group['name'] if group else grant['id']),
                         track_count=len(group['track_ids']) if group else None,
                         verified_at=group['verified_at'] if group else None,
                         status='Cached by the radio' if group else 'Waiting for the radio to load it')
        with self.writer():
            manifest, revision = self.read_library()
            art = visuals.Visuals(self.visuals_path)
            songs = []
            for song in manifest['songs']:
                entry = art.entries.get(song['id'], {})
                songs.append(dict(id=song['id'], title=song['title'], artist=library.tagged_artist(song),
                                  duration=song['duration'], cover=entry.get('art_sha256')
                                  if entry.get('audio_sha256') == song['source_sha256'] else None))
        return dict(version=1, approvals=snapshot, library=dict(revision=revision, songs=songs),
                    spotify_authorized=(self.spotify_directory / 'session.json').is_file(), now=self.now())

    def change_approval(self, value):
        action = value.get('action')
        if action not in ('add', 'rename', 'remove') or type(value.get('revision')) is not int:
            raise ValueError('Choose an approval action and send its revision.')
        kind, identity = value.get('kind'), value.get('identity')
        name = value.get('label')
        if action == 'rename' and name is None:
            raise ValueError('Enter a label to rename this approval.')
        with self.writer():
            snapshot = self.store.snapshot()
            if snapshot['revision'] != value['revision']:
                raise approvals.Conflict()
            normalized = approvals.identity(kind, identity)
            if action == 'rename' and not any(a['kind'] == kind and a['id'] == normalized for a in snapshot['approvals']):
                raise Problem(404, 'This approval is no longer in the list. Reload the page.')
            if action == 'remove':
                self.store.remove(kind, normalized, value['revision'])
            else:
                self.store.add(kind, normalized, name, value['revision'])
        return dict(message='Saved. Approval edits reach the radio within a few seconds.')

    def picker(self, kind, query):
        if not (self.spotify_directory / 'session.json').is_file():
            raise Problem(401, 'Spotify is not connected. You can still paste a link to approve music.')
        q = None
        if kind == 'artists':
            q = query.get('q', [''])[0].strip()
            text(q, 'Search', 256)
        try:
            client = spotify.Client(spotify.Auth(self.spotify_directory, self.transport, self.now), self.transport)
            if kind == 'playlists':
                me = client.me()['id']
                items = list(client.pages('/me/playlists?limit=50'))
            else:
                response = client.get('/search?' + urlencode(dict(type='artist', q=q, limit=10)))
                page = response.get('artists')
                if not isinstance(page, dict) or not isinstance(page.get('items'), list) or len(page['items']) > 10:
                    raise spotify.SpotifyError('Invalid artist results')
                items = page['items']
            return dict(items=[dict(id=approvals.identity('playlist' if kind == 'playlists' else 'artist', item['id']),
                                    name=text(item['name'], 'Name', 4096),
                                    playable=kind == 'artists' or item.get('collaborative') is True
                                    or isinstance(item.get('owner'), dict) and item['owner'].get('id') == me)
                               for item in items])
        except spotify.SpotifyError as exc:
            if exc.status in (400, 401):
                raise Problem(401, 'Spotify needs a new sign-in. Paste a link for now, or reconnect with the Spotify CLI.') from None
            if exc.status == 403:
                raise Problem(403, 'Spotify did not allow this request. Check the account and app access.') from None
            if exc.status == 429:
                raise Problem(503, 'Spotify is busy. Wait a little before trying again.') from None
            raise Problem(503, 'Spotify is unavailable. Check your connection and try again. Paste a link still works.') from None
        except (OSError, ValueError, KeyError, TypeError):
            raise Problem(503, 'Spotify could not load this list. Check the connection and private account setup.') from None

    def change_library(self, value):
        action = value.get('action')
        identity = song_id(value.get('id'))
        op = dict(action=action, id=identity)
        if action == 'edit':
            op.update(title=text(value.get('title'), 'Title'), artist=text(value.get('artist', ''), 'Artist', blank=True))
        elif action == 'move':
            if type(value.get('direction')) is not int or value['direction'] not in (-1, 1):
                raise ValueError('Move a song up or down one place.')
            op['direction'] = value['direction']
        elif action != 'remove':
            raise ValueError('Unknown library action.')
        with self.writer():
            manifest = self.require_revision(value.get('revision'))
            self.write_library(edit_manifest(manifest, op))
        return dict(message='Saved. Restart the radio to use the changed library.')

    def upload_audio(self, path, filename, revision):
        with self.writer():
            self.require_revision(revision)
        song = library.probe_song(path, filename)
        song.pop('source_path', None)
        with self.writer():
            manifest = self.require_revision(revision)
            result = edit_manifest(manifest, dict(action='add', song=song))
            library.checked_manifest(result, self.library_path.parent)
            library.copy_audio(path, self.library_path.parent / song['path'])
            self.write_library(result)
        return dict(message='Song added. Restart the radio to use the changed library.')

    def upload_cover(self, path, identity, revision):
        header = path.read_bytes()[:12]
        if not (header.startswith(b'\x89PNG\r\n\x1a\n') or header.startswith(b'\xff\xd8\xff')
                or header.startswith(b'RIFF') and header[8:12] == b'WEBP'):
            raise ValueError('Choose a PNG, JPEG or WebP cover image.')
        with self.writer():
            manifest = self.require_revision(revision)
        song = next((s for s in manifest['songs'] if s['id'] == identity), None)
        if song is None:
            raise ValueError('This song is no longer in the library.')
        entry = visuals.prepare_song(song, self.library_path.parent, path, self.visuals_path.parent)
        with self.writer():
            manifest = self.require_revision(revision)
            try:
                existing = visuals.read_entries(self.visuals_path) if self.visuals_path.exists() else {}
            except (OSError, ValueError, TypeError, KeyError):
                raise Problem(409, 'The saved cover list could not be read, so it was left unchanged. Check it before adding covers.') from None
            current = {s['id'] for s in manifest['songs']}
            entries = {k: v for k, v in existing.items() if k in current}
            entries[identity] = entry
            library.atomic(self.visuals_path, encoded(dict(version=1, size=visuals.SIZE, hz=visuals.HZ,
                                                         songs=list(entries.values()))))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            library.atomic(self.private / (digest + '.cover'), path.read_bytes())
            self.write_library(edit_manifest(manifest, dict(action='cover', id=identity, sha256=digest)))
        return dict(message='Cover saved. Restart the radio to use the new cover.')

    def thumbnail(self, identity):
        manifest, _ = self.read_library()
        song = next((s for s in manifest['songs'] if s['id'] == identity), None)
        if song is None:
            raise Problem(404, 'Song not found.')
        art, _, _ = visuals.Visuals(self.visuals_path).get(song)
        if not art:
            raise Problem(404, 'No cover for this song.')
        rgb = bytearray()
        for i in range(0, len(art), 2):
            pixel = int.from_bytes(art[i:i + 2], 'big')
            rgb.extend(((pixel >> 11) * 255 // 31, ((pixel >> 5) & 63) * 255 // 63, (pixel & 31) * 255 // 31))
        return png(visuals.SIZE, visuals.SIZE, rgb)


def text(value, name, maximum=4096, blank=False):
    if (not isinstance(value, str) or len(value) > maximum or not blank and not value.strip()
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
        raise ValueError(name + ' must contain plain text within ' + str(maximum) + ' characters.')
    return value.strip()


def song_id(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Invalid song identity.')
    return value


def png(width, height, rgb):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    rows = b''.join(b'\0' + bytes(rgb[i:i + width * 3]) for i in range(0, len(rgb), width * 3))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


class Server(ThreadingHTTPServer):
    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    routes = {('GET', '/'): 'page', ('GET', '/login'): 'login', ('GET', '/api/state'): 'state',
              ('POST', '/api/approvals'): 'approval', ('POST', '/api/library'): 'edit_library',
              ('POST', '/api/library/audio'): 'audio', ('POST', '/api/library/cover'): 'cover',
              ('GET', '/api/library/cover'): 'thumbnail',
              ('GET', '/api/spotify/playlists'): 'playlists', ('GET', '/api/spotify/artists'): 'artists'}

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass  # Login URLs and request headers contain credentials.

    def drain(self):
        """Read an unread body so the browser sees the error instead of a reset connection."""
        lengths = self.headers.get_all('Content-Length', [])
        if (self.unread and len(lengths) == 1 and re.fullmatch('[0-9]{1,12}', lengths[0])
                and int(lengths[0]) <= AUDIO_LIMIT and self.server.app.authenticated(self.headers.get('Cookie'))):
            size = int(lengths[0])
            while size and (chunk := self.rfile.read(min(size, 1024 * 1024))):
                size -= len(chunk)
        self.unread = False

    def respond(self, status, body, content_type='application/json', headers=None):
        if status >= 400 and status != 413:
            self.drain()
        self.send_response(status)
        for key, value in {'Content-Type': content_type, 'Content-Length': str(len(body)),
                           'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                           'Content-Security-Policy': self.server.app.csp, 'Referrer-Policy': 'no-referrer',
                           'X-Frame-Options': 'DENY', **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def origin(self):
        if self.headers.get('Sec-Fetch-Site', '').lower() == 'cross-site':
            raise Problem(403, 'Open this editor directly before making changes.')
        origin = self.headers.get('Origin')
        host = self.headers.get('Host')
        if not host or len(self.headers.get_all('Host', [])) != 1:
            raise Problem(400, 'A single Host header is required.')
        if origin is not None and origin != 'http://' + host:
            raise Problem(403, 'Changes must come from this editor on the same address.')

    def body_length(self, maximum, accepted):
        self.origin()
        if self.headers.get_content_type() not in accepted:
            raise Problem(415, 'Use the expected upload type or application/json for edits.')
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not re.fullmatch('[0-9]{1,12}', lengths[0]):
            raise Problem(411, 'Send one Content-Length header. Chunked uploads are not supported.')
        size = int(lengths[0])
        if size > maximum:
            raise Problem(413, 'This file or request is too large.')
        if size == 0:
            raise Problem(400, 'The request body is empty.')
        self.unread = False
        return size

    def json_body(self):
        size = self.body_length(JSON_LIMIT, ('application/json',))
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise Problem(400, 'The request was interrupted. Try again.')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('Send a JSON object.')
        return value

    @contextmanager
    def upload(self, maximum, accepted):
        size = self.body_length(maximum, accepted)
        with tempfile.TemporaryDirectory(prefix='upload-', dir=self.server.app.private) as folder:
            path = Path(folder) / 'input'
            with path.open('wb') as output:
                while size:
                    chunk = self.rfile.read(min(size, 1024 * 1024))
                    if not chunk:
                        raise Problem(400, 'The upload was interrupted. Try again.')
                    output.write(chunk)
                    size -= len(chunk)
            yield path

    def dispatch(self):
        self.close_connection = True
        self.unread = self.command != 'GET'
        try:
            parsed = urlsplit(self.path)
            self.query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=10)
            if any(len(values) != 1 for values in self.query.values()):
                raise ValueError('Repeated query fields are not supported.')
            route = self.routes.get((self.command, parsed.path))
            if parsed.path.startswith('/api/') and not self.server.app.authenticated(self.headers.get('Cookie')):
                raise Problem(401, 'Open a fresh parent login link to use this editor.')
            if route is None:
                raise Problem(404, 'Page or action not found.')
            if self.command != 'GET':
                self.origin()
            result = getattr(self, route)()
            if result is not None:
                self.respond(200, encoded(result))
        except approvals.Conflict:
            self.respond(409, encoded(dict(error='Approvals changed in another window. Reloaded the latest list. Try your edit again.')))
        except Problem as exc:
            self.respond(exc.status, encoded(dict(error=str(exc))))
        except subprocess.SubprocessError:
            self.respond(422, encoded(dict(error='This file could not be decoded. Upload a valid MP3, WAV, PNG, JPEG or WebP file.')))
        except ValueError as exc:
            self.respond(400, encoded(dict(error=str(exc))))
        except (KeyError, TypeError, RecursionError):
            self.respond(400, encoded(dict(error='Invalid input or music data. Check the fields and file, then try again.')))
        except (OSError, sqlite3.Error):
            self.respond(503, encoded(dict(error='The editor could not read or save its files. Check disk space, permissions and required audio tools.')))

    do_GET = dispatch
    do_POST = dispatch
    do_PUT = dispatch
    do_DELETE = dispatch

    def page(self):
        self.respond(200, self.server.app.page, 'text/html; charset=utf-8')

    def login(self):
        session = self.server.app.login(self.query.get('token', [''])[0])
        self.respond(303, b'', headers={'Location': '/', 'Set-Cookie': COOKIE + '=' + session + f'; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Strict; Path=/'})

    def state(self):
        return self.server.app.state()

    def approval(self):
        return self.server.app.change_approval(self.json_body())

    def edit_library(self):
        return self.server.app.change_library(self.json_body())

    def audio(self):
        filename = unquote(self.headers.get('X-Filename', ''), errors='strict')
        text(filename, 'Filename', 255)
        if '/' in filename or '\\' in filename or Path(filename).suffix.lower() not in ('.mp3', '.wav'):
            raise ValueError('Use an MP3 or WAV filename without folders.')
        revision = self.headers.get('X-Library-Revision')
        with self.upload(AUDIO_LIMIT, ('application/octet-stream',)) as path:
            return self.server.app.upload_audio(path, filename, revision)

    def cover(self):
        identity = song_id(self.headers.get('X-Song-Id'))
        revision = self.headers.get('X-Library-Revision')
        with self.upload(IMAGE_LIMIT, ('image/png', 'image/jpeg', 'image/webp')) as path:
            return self.server.app.upload_cover(path, identity, revision)

    def thumbnail(self):
        with self.server.app.writer():
            data = self.server.app.thumbnail(song_id(self.query.get('id', [''])[0]))
        self.respond(200, data, 'image/png')

    def playlists(self):
        return self.server.app.picker('playlists', self.query)

    def artists(self):
        return self.server.app.picker('artists', self.query)


def demo(directory):
    directory = Path(directory).expanduser().resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('Use an empty directory for the synthetic demo. Existing files are preserved.')
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='synthetic-', dir=directory) as folder:
        source = Path(folder)
        for index, title in enumerate(('Synthetic sunrise', 'Synthetic moonwalk', 'Synthetic rain')):
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'sine=frequency={330 + 110 * index}:duration=2',
                            '-metadata', 'title=' + title, '-metadata', 'artist=Synthetic family',
                            str(source / (title + '.wav'))], check=True, capture_output=True, timeout=30)
        library.import_library(source, directory / 'library')
    manifest = library.load(directory / 'library/manifest.json')
    artwork = directory / 'synthetic-artwork'
    artwork.mkdir()
    covers = []
    for index, song in enumerate(manifest['songs']):
        name = str(index) + '.png'
        library.atomic(artwork / name, png(112, 112, bytes((90 + index * 40, 140, 170)) * (112 * 112)))
        covers.append(dict(song_id=song['id'], master=name))
    library.atomic(artwork / 'artwork.json', encoded(dict(synthetic=True, covers=covers)))
    report = visuals.prepare(directory / 'library/manifest.json', artwork / 'artwork.json', directory / 'visuals')
    if report['skipped']:
        raise ValueError('Synthetic visuals could not be prepared.')
    app = Dashboard(directory)
    now = int(time.time()) - 300
    for kind, identity, label in [('playlist', 'P' * 22, None), ('artist', 'A' * 22, 'Synthetic bedtime artist')]:
        app.store.add(kind, identity, label)
    library.atomic(app.catalog_path, encoded(dict(version=1, synthetic=True, approvals_revision=app.store.snapshot()['revision'],
                   generated_at=now, tracks={}, groups=[dict(kind='playlist', id='P' * 22,
                   name='Synthetic family playlist', track_ids=[], verified_at=now)])))
    print('Synthetic demo ready. No account, hardware or real music is used.')


def login_url(app, host, port):
    host = '127.0.0.1' if host in ('0.0.0.0', '::') else host
    authority = '[' + host + ']' if ':' in host else host
    return f'http://{authority}:{port}/login?' + urlencode(dict(token=app.token()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('serve', 'login-url', 'demo'):
        child = commands.add_parser(name)
        child.add_argument('--data-dir', type=Path, default=DEFAULT_DIRECTORY)
        if name != 'demo':
            child.add_argument('--host', default='127.0.0.1')
            child.add_argument('--port', type=int, default=8090)
    args = parser.parse_args(argv)
    try:
        if args.command == 'demo':
            demo(args.data_dir)
            return 0
        app = Dashboard(args.data_dir)
        if args.command == 'login-url':
            print(login_url(app, args.host, args.port))
            return 0
        try:
            loopback = ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            loopback = args.host.lower() == 'localhost'
        if not loopback:
            print('Warning: this exposes the parent editor on that network over plain HTTP. Login links, sessions and uploads are not encrypted.', file=sys.stderr)
        if ':' in args.host:
            Server.address_family = socket.AF_INET6
        app.sweep_uploads()
        with Server((args.host, args.port), app) as server:
            address = login_url(app, args.host, server.server_port).split('/login?')[0]
            print('Parent dashboard listening on ' + address, flush=True)
            if sys.stdout.isatty():
                print(login_url(app, args.host, server.server_port), flush=True)
            else:
                print(f'Get a login link with: python3 host/dashboard.py login-url --port {server.server_port}', flush=True)
            server.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error) as exc:
        print('Dashboard could not start. Check paths, private permissions, port and ffmpeg setup. ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
