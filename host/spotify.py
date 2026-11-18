#!/usr/bin/env python3
"""Private Spotify PKCE authorization and bounded, read-only music catalog access."""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from host.library import atomic
except ModuleNotFoundError:
    from library import atomic

REDIRECT = 'http://127.0.0.1:8765/callback'
SCOPES = 'playlist-read-private playlist-read-collaborative'
TOKEN_URL = 'https://accounts.spotify.com/api/token'
API = 'https://api.spotify.com/v1'
DEFAULT_DIRECTORY = Path.home() / '.local/share/family-radio/spotify'
MAX_RESPONSE = 2 * 1024 * 1024
MAX_PAGES = 200
MAX_TRACKS = 20000


class SpotifyError(ValueError):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def request_json(method, url, headers, body=None):
    """No redirect following, bounded response, and no server payload in errors."""
    request = urllib.request.Request(url, body, headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        retry = exc.headers.get('Retry-After', '')
        seconds = min(int(retry), 86400) if retry.isdigit() else None
        exc.close()
        raise SpotifyError('Spotify request rejected (HTTP ' + str(exc.code) + ')', exc.code, seconds) from None
    except (OSError, urllib.error.URLError):
        raise SpotifyError('Spotify network request failed; try again later') from None
    if len(raw) > MAX_RESPONSE:
        raise SpotifyError('Spotify response exceeds the size limit')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise SpotifyError('Spotify returned invalid JSON') from None
    if not isinstance(value, dict):
        raise SpotifyError('Spotify returned an invalid response')
    return value


def _private_json(path):
    path = Path(path)
    if path.stat().st_mode & 0o077 or path.stat().st_size > MAX_RESPONSE:
        raise SpotifyError('Spotify private file permissions or size are invalid')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('version') != 1:
        raise SpotifyError('Unsupported Spotify private file format')
    return value


def _save(path, value):
    atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def _text(value, maximum=4096):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)


def _id(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9]{22}', value):
        raise SpotifyError('Invalid Spotify catalog ID')
    return value


class Auth:
    def __init__(self, directory=DEFAULT_DIRECTORY, transport=request_json, now=time.time):
        self.directory = Path(directory).expanduser()
        self.transport = transport
        self.now = now

    @contextmanager
    def _lock(self):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise SpotifyError('Spotify credential directory must have mode 0700')
        fd = os.open(self.directory / '.lock', os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def start(self, client_id):
        if not isinstance(client_id, str) or not re.fullmatch('[a-fA-F0-9]{32}', client_id):
            raise SpotifyError('Invalid Spotify client ID')
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        with self._lock():
            _save(self.directory / 'pending.json', dict(version=1, client_id=client_id, verifier=verifier,
                                                       state=state, created_at=self.now()))
        return 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode(dict(
            client_id=client_id, response_type='code', redirect_uri=REDIRECT, scope=SCOPES,
            state=state, code_challenge_method='S256', code_challenge=challenge))

    def _token(self, fields, client_id, previous=None):
        result = self.transport('POST', TOKEN_URL, {'Content-Type': 'application/x-www-form-urlencoded'},
                                urllib.parse.urlencode(fields).encode())
        if (not _text(result.get('access_token'), 16384) or not isinstance(result.get('token_type'), str) or result['token_type'].lower() != 'bearer'
                or type(result.get('expires_in')) is not int or not 0 < result['expires_in'] <= 86400):
            raise SpotifyError('Invalid Spotify token response')
        refresh = result.get('refresh_token', (previous or {}).get('refresh_token'))
        scope = result.get('scope', (previous or {}).get('scope', ''))
        if not _text(refresh, 16384) or not isinstance(scope, str) or not set(SCOPES.split()) <= set(scope.split()):
            raise SpotifyError('Spotify authorization is missing required playlist access')
        return dict(version=1, client_id=client_id, access_token=result['access_token'], refresh_token=refresh,
                    expires_at=self.now() + result['expires_in'], scope=scope)

    def complete(self, callback):
        if not isinstance(callback, str) or len(callback) > 16384 or any(c.isspace() for c in callback):
            raise SpotifyError('Invalid Spotify callback URL')
        parsed = urllib.parse.urlsplit(callback)
        if (parsed.scheme, parsed.netloc, parsed.path) != ('http', '127.0.0.1:8765', '/callback') or parsed.fragment:
            raise SpotifyError('Spotify callback must match the registered loopback address')
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if any(len(values) != 1 for values in query.values()):
            raise SpotifyError('Invalid repeated Spotify callback parameters')
        with self._lock():
            pending = _private_json(self.directory / 'pending.json')
            created = pending.get('created_at')
            if type(created) not in (int, float) or not math.isfinite(created) or not 0 <= self.now() - created <= 600:
                raise SpotifyError('Spotify login expired; start authorization again')
            state = query.get('state', [''])[0]
            if not _text(pending.get('state')) or not secrets.compare_digest(state, pending['state']):
                raise SpotifyError('Spotify login state does not match')
            if 'error' in query or not _text(query.get('code', [''])[0]):
                raise SpotifyError('Spotify authorization was declined or returned no code')
            if (not isinstance(pending.get('client_id'), str) or not re.fullmatch('[a-fA-F0-9]{32}', pending['client_id'])
                    or not isinstance(pending.get('verifier'), str) or not re.fullmatch('[A-Za-z0-9_.~-]{43,128}', pending['verifier'])):
                raise SpotifyError('Invalid pending Spotify authorization; start again')
            result = self._token(dict(grant_type='authorization_code', code=query['code'][0],
                                      redirect_uri=REDIRECT, client_id=pending['client_id'],
                                      code_verifier=pending['verifier']), pending['client_id'])
            _save(self.directory / 'session.json', result)
            (self.directory / 'pending.json').unlink()

    def access_token(self, force=False):
        with self._lock():
            session = _private_json(self.directory / 'session.json')
            expiry = session.get('expires_at')
            if (not _text(session.get('access_token'), 16384) or not _text(session.get('refresh_token'), 16384)
                    or not isinstance(session.get('client_id'), str) or not re.fullmatch('[a-fA-F0-9]{32}', session['client_id'])
                    or type(expiry) not in (int, float) or not math.isfinite(expiry)):
                raise SpotifyError('Invalid Spotify session; authorize again')
            if force or expiry <= self.now() + 60:
                session = self._token(dict(grant_type='refresh_token', refresh_token=session['refresh_token'],
                                           client_id=session['client_id']), session['client_id'], session)
                _save(self.directory / 'session.json', session)
            return session['access_token']



def _api_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise SpotifyError('Invalid Spotify API URL')
    url = API + value if value.startswith('/') and not value.startswith('//') else value
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'api.spotify.com'
            or not parsed.path.startswith('/v1/') or parsed.fragment):
        raise SpotifyError('Spotify API requests must stay on the official HTTPS API')
    return url


def _image(album):
    images = album.get('images', [])
    if not isinstance(images, list):
        return None
    for image in images:
        if not isinstance(image, dict):
            continue
        url = image.get('url')
        if isinstance(url, str):
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme == 'https' and parsed.hostname == 'i.scdn.co' and not parsed.username and not parsed.password:
                return url
    return None


def track_fact(value, album=None):
    """Keep the returned playable identity and credits, never a removed original alias.

    Spotify can relink a recording for the account's market. Its returned URI
    is the playback target; linked_from is absent in current development mode.
    """
    if (not isinstance(value, dict) or value.get('type') != 'track' or value.get('is_local')
            or value.get('is_playable') is False or value.get('restrictions')):
        return None
    identity = value.get('id')
    if not isinstance(identity, str) or not re.fullmatch('[A-Za-z0-9]{22}', identity):
        return None
    if value.get('uri') != 'spotify:track:' + identity or not _text(value.get('name')):
        return None
    duration = value.get('duration_ms')
    if type(duration) is not int or not 0 < duration <= 86400000:
        return None
    artists = value.get('artists')
    if not isinstance(artists, list) or not 1 <= len(artists) <= 100:
        return None
    credits = []
    for artist in artists:
        if not isinstance(artist, dict) or not _text(artist.get('name'), 1024):
            return None
        try:
            credits.append(dict(id=_id(artist.get('id')), name=artist['name']))
        except SpotifyError:
            return None
    album = value.get('album', album)
    if not isinstance(album, dict) or not _text(album.get('name')):
        return None
    try:
        album_id = _id(album.get('id'))
    except SpotifyError:
        return None
    return dict(id=identity, uri=value['uri'], title=value['name'], artists=credits,
                duration_ms=duration, explicit=value.get('explicit') if type(value.get('explicit')) is bool else None,
                album=dict(id=album_id, name=album['name'], image_url=_image(album),
                           url='https://open.spotify.com/album/' + album_id),
                url='https://open.spotify.com/track/' + identity)


class Client:
    def __init__(self, auth, transport=request_json):
        self.auth = auth
        self.transport = transport
        self.requests_remaining = 2000
        self.album_cache = {}
        self.cached_tracks = 0

    def get(self, path_or_url):
        url = _api_url(path_or_url)
        for attempt in range(2):
            if self.requests_remaining <= 0:
                raise SpotifyError('Spotify catalog request limit reached')
            self.requests_remaining -= 1
            token = self.auth.access_token(force=attempt == 1)
            try:
                result = self.transport('GET', url, {'Authorization': 'Bearer ' + token})
            except SpotifyError as exc:
                if exc.status == 401 and attempt == 0:
                    continue
                raise
            if not isinstance(result, dict):
                raise SpotifyError('Invalid Spotify API response')
            return result
        raise SpotifyError('Spotify authorization failed; sign in again')

    def pages(self, path_or_url):
        url = _api_url(path_or_url)
        seen = set()
        endpoint = urllib.parse.urlsplit(url).path
        market = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get('market')
        count = 0
        while url:
            if url in seen or len(seen) >= MAX_PAGES:
                raise SpotifyError('Spotify pagination is repeated or exceeds its limit')
            seen.add(url)
            page = self.get(url)
            items = page.get('items')
            if not isinstance(items, list) or len(items) > 100:
                raise SpotifyError('Invalid Spotify catalog page')
            count += len(items)
            if count > MAX_TRACKS:
                raise SpotifyError('Spotify catalog item limit reached')
            yield from items
            next_url = page.get('next')
            if next_url is not None and not isinstance(next_url, str):
                raise SpotifyError('Invalid Spotify pagination URL')
            url = _api_url(next_url) if next_url else None
            if url and urllib.parse.urlsplit(url).path != endpoint:
                raise SpotifyError('Spotify pagination changed its catalog endpoint')
            if url and market:
                parts = urllib.parse.urlsplit(url)
                query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if key != 'market']
                query.append(('market', market))
                url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))

    def me(self):
        value = self.get('/me')
        if not _text(value.get('id'), 256):
            raise SpotifyError('Invalid Spotify account response')
        return dict(id=value['id'], display_name=value.get('display_name'))

    def playlist(self, identity):
        identity = _id(identity)
        metadata_path = '/playlists/' + identity + '?fields=id,name,snapshot_id'
        metadata = self.get(metadata_path)
        if metadata.get('id') != identity or not _text(metadata.get('name')) or not _text(metadata.get('snapshot_id')):
            raise SpotifyError('Invalid Spotify playlist metadata')
        tracks = {}
        for entry in self.pages('/playlists/' + identity + '/items?limit=50&market=from_token'):
            if not isinstance(entry, dict) or entry.get('is_local'):
                continue
            fact = track_fact(entry.get('item'))
            if fact:
                tracks.setdefault(fact['id'], fact)
        after = self.get(metadata_path)
        if after.get('snapshot_id') != metadata['snapshot_id']:
            raise SpotifyError('Spotify playlist changed during refresh; try again')
        return dict(id=identity, name=metadata['name'], snapshot_id=metadata['snapshot_id'], tracks=list(tracks.values()))

    def artist(self, identity):
        identity = _id(identity)
        metadata = self.get('/artists/' + identity)
        if metadata.get('id') != identity or not _text(metadata.get('name')):
            raise SpotifyError('Invalid Spotify artist metadata')
        tracks = {}
        seen_albums = set()
        path = '/artists/' + identity + '/albums?include_groups=album,single,appears_on,compilation&limit=10&market=from_token'
        for album in self.pages(path):
            if not isinstance(album, dict):
                raise SpotifyError('Invalid Spotify album response')
            album_id = _id(album.get('id'))
            if album_id in seen_albums:
                continue
            seen_albums.add(album_id)
            if len(seen_albums) > 1000:
                raise SpotifyError('Spotify artist album limit reached')
            if album_id not in self.album_cache:
                facts = []
                for item in self.pages('/albums/' + album_id + '/tracks?limit=50&market=from_token'):
                    fact = track_fact(item, album)
                    if fact:
                        facts.append(fact)
                if self.cached_tracks + len(facts) > MAX_TRACKS:
                    raise SpotifyError('Spotify album cache limit reached')
                self.cached_tracks += len(facts)
                self.album_cache[album_id] = facts
            for fact in self.album_cache[album_id]:
                if any(credit['id'] == identity for credit in fact['artists']):
                    tracks.setdefault(fact['id'], fact)
            if len(tracks) > MAX_TRACKS:
                raise SpotifyError('Spotify artist track limit reached')
        return dict(id=identity, name=metadata['name'], tracks=list(tracks.values()))

    def catalog(self, approvals, now=time.time):
        if (not isinstance(approvals, dict) or approvals.get('version') != 1
                or type(approvals.get('revision')) is not int or approvals['revision'] < 0
                or not isinstance(approvals.get('approvals'), list) or len(approvals['approvals']) > 200):
            raise SpotifyError('Invalid or oversized approval snapshot')
        groups = []
        tracks = {}
        seen = set()
        for approval in approvals['approvals']:
            if not isinstance(approval, dict) or approval.get('provider') != 'spotify' or approval.get('kind') not in ('playlist', 'artist'):
                raise SpotifyError('Invalid Spotify approval')
            identity = _id(approval.get('id'))
            kind = approval['kind']
            if (kind, identity) in seen:
                raise SpotifyError('Duplicate Spotify approval')
            seen.add((kind, identity))
            group = self.playlist(identity) if kind == 'playlist' else self.artist(identity)
            group_tracks = group.pop('tracks')
            group['kind'] = kind
            group['track_ids'] = [item['id'] for item in group_tracks]
            groups.append(group)
            tracks.update((item['id'], item) for item in group_tracks)
            if len(tracks) > MAX_TRACKS:
                raise SpotifyError('Spotify catalog track limit reached')
        return dict(version=1, approvals_revision=approvals['revision'], generated_at=now(), groups=groups, tracks=tracks)

def main(argv=None, transport=request_json, now=time.time):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=DEFAULT_DIRECTORY)
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('login-start')
    start.add_argument('--client-id-file', type=Path, required=True)
    complete = commands.add_parser('login-complete')
    complete.add_argument('--callback-file', type=Path, required=True)
    commands.add_parser('account', help='verify authorization without printing credentials')
    catalog = commands.add_parser('catalog')
    catalog.add_argument('--approvals-db', type=Path, required=True)
    catalog.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    auth = Auth(args.directory, transport, now)
    try:
        if args.command == 'login-start':
            print(auth.start(args.client_id_file.read_text().strip()))
        elif args.command == 'login-complete':
            auth.complete(args.callback_file.read_text().strip())
            print('Spotify authorization saved privately.')
        elif args.command == 'account':
            Client(auth, transport).me()
            print('Spotify account access verified.')
        else:
            try:
                from host.approvals import Store
            except ModuleNotFoundError:
                from approvals import Store
            store = Store(args.approvals_db)
            approvals = store.snapshot()
            result = Client(auth, transport).catalog(approvals, now)
            if store.snapshot()['revision'] != approvals['revision']:
                raise SpotifyError('Approvals changed during refresh; try again')
            _save(args.output, result)
            print('Spotify catalog saved: ' + str(len(result['groups'])) + ' groups, ' + str(len(result['tracks'])) + ' tracks.')
        return 0
    except SpotifyError as exc:
        detail = str(exc)
        if exc.retry_after is not None:
            detail += '; retry after ' + str(exc.retry_after) + ' seconds'
        print(detail, file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print('Spotify authorization failed. Check private inputs and restart login if needed.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
