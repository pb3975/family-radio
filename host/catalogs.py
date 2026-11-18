"""Bounded background catalog refresh and exact parent-grant decisions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import math
import re
from pathlib import Path
import sqlite3
import threading
import time

try:
    from host.approvals import Store, LIMIT as APPROVAL_LIMIT
    from host.library import atomic
    from host.spotify import Auth, Client, SpotifyError, request_json, MAX_TRACKS
except ModuleNotFoundError:
    from approvals import Store, LIMIT as APPROVAL_LIMIT
    from library import atomic
    from spotify import Auth, Client, SpotifyError, request_json, MAX_TRACKS

PLAYLIST_LEASE = 60
ARTIST_REFRESH = 86400


def allowed(track, approvals, catalog, now, fresh=True):
    """One current artist grant or fresh, complete playlist membership is enough."""
    if not approvals or not track:
        return False
    grants = {(a['kind'], a['id']) for a in approvals['approvals']}
    if any(('artist', credit['id']) in grants for credit in track['artists']):
        return True
    return any(group['kind'] == 'playlist' and ('playlist', group['id']) in grants
               and (not fresh or 0 <= now - group.get('verified_at', catalog['generated_at']) < PLAYLIST_LEASE)
               and track['id'] in group['track_ids'] for group in catalog['groups'])


def read_approvals(path):
    # A locked/damaged parent store must not stall the USB heartbeat or open access.
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.05)) as connection:
        connection.execute('BEGIN')
        value = Store._snapshot(connection)
        if len(value['approvals']) > APPROVAL_LIMIT:
            raise ValueError('approval group limit exceeded')
        return value


def checked_catalog(value):
    def identity(candidate):
        return isinstance(candidate, str) and re.fullmatch('[A-Za-z0-9]{22}', candidate)
    def text(candidate, maximum=4096):
        return isinstance(candidate, str) and 0 < len(candidate) <= maximum and not any(ord(c) < 32 for c in candidate)
    def timestamp(candidate):
        return type(candidate) in (int, float) and math.isfinite(candidate) and candidate >= 0
    if (not isinstance(value, dict) or value.get('version') != 1
            or type(value.get('approvals_revision')) is not int or value['approvals_revision'] < 0
            or not timestamp(value.get('generated_at'))
            or not isinstance(value.get('groups'), list) or len(value['groups']) > 200
            or not isinstance(value.get('tracks'), dict) or len(value['tracks']) > MAX_TRACKS):
        raise ValueError('invalid private Spotify catalog')
    tracks = value['tracks']
    for track_id, track in tracks.items():
        if (not identity(track_id) or not isinstance(track, dict) or track.get('id') != track_id
                or track.get('uri') != 'spotify:track:' + track_id or not text(track.get('title'))
                or not isinstance(track.get('artists'), list) or not 1 <= len(track['artists']) <= 100
                or type(track.get('duration_ms')) is not int or not 0 < track['duration_ms'] <= 86400000
                or track.get('explicit') is not None and type(track.get('explicit')) is not bool
                or track.get('url') != 'https://open.spotify.com/track/' + track_id):
            raise ValueError('invalid private Spotify track')
        for artist in track['artists']:
            if not isinstance(artist, dict) or not identity(artist.get('id')) or not text(artist.get('name'), 1024):
                raise ValueError('invalid private Spotify artist')
        album = track.get('album')
        if (not isinstance(album, dict) or not identity(album.get('id')) or not text(album.get('name'))
                or album.get('image_url') is not None and not text(album.get('image_url'))
                or album.get('url') != 'https://open.spotify.com/album/' + album['id']):
            raise ValueError('invalid private Spotify album')
    seen = set()
    for group in value['groups']:
        if not isinstance(group, dict):
            raise ValueError('invalid private Spotify group')
        key = (group.get('kind'), group.get('id'))
        members = group.get('track_ids')
        if (key[0] not in ('artist', 'playlist') or not identity(key[1]) or key in seen
                or not text(group.get('name')) or not isinstance(members, list) or len(members) > MAX_TRACKS
                or any(not identity(member) or member not in tracks for member in members)
                or len(set(members)) != len(members)
                or not timestamp(group.get('verified_at', value['generated_at']))):
            raise ValueError('invalid private Spotify group')
        seen.add(key)
    return value


class Catalogs:
    """At most two group jobs; a slow artist cannot starve playlist updates."""
    def __init__(self, approvals_path, credentials, cache_path, now=time.time, client_factory=None):
        self.approvals_path, self.cache_path = Path(approvals_path), Path(cache_path)
        self.now = now
        self.approvals = None
        self.readable, self.read_failed_at = False, None
        self.catalog = dict(version=1, approvals_revision=-1, generated_at=0, groups=[], tracks={})
        self.status, self.error = 'AUTH', None
        self.serial, self.next_read = 0, 0
        self.jobs, self.next_group, self.group_status = {}, {}, {}
        self.cache_job = self.cache_pending = None
        self.cache_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix='spotify-cache')
        self.closed = threading.Event()
        def transport(*args, **kwargs):
            if self.closed.is_set():
                raise SpotifyError('Catalog refresh cancelled')
            return request_json(*args, **kwargs)
        self.client_factory = client_factory or (lambda: Client(Auth(credentials), transport))
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='spotify-catalog')
        try:
            if self.cache_path.stat().st_size <= 32 * 1024 * 1024:
                self.catalog = checked_catalog(json.loads(self.cache_path.read_text()))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    @property
    def future(self):
        return bool(self.jobs)

    def _fetch(self, grant):
        client = self.client_factory()
        try:
            fetched = client.artist(grant['id']) if grant['kind'] == 'artist' else client.playlist(grant['id'])
        except FileNotFoundError:
            raise SpotifyError('Spotify sign in required', status=401) from None
        facts = fetched.pop('tracks')
        group = dict(fetched, kind=grant['kind'], track_ids=[track['id'] for track in facts], verified_at=self.now())
        return group, facts

    def _publish(self, group, facts):
        groups = [g for g in self.catalog['groups'] if (g['kind'], g['id']) != (group['kind'], group['id'])]
        groups.append(group)
        grants = {(a['kind'], a['id']) for a in self.approvals['approvals']}
        groups = sorted((g for g in groups if (g['kind'], g['id']) in grants), key=lambda g: (g['kind'], g['id']))
        tracks = dict(self.catalog['tracks'])
        tracks.update((track['id'], track) for track in facts)
        identities = dict.fromkeys(identity for g in groups for identity in g['track_ids'])
        tracks = {identity: tracks[identity] for identity in identities}
        value = checked_catalog(dict(version=1, approvals_revision=self.approvals['revision'], generated_at=self.now(), groups=groups, tracks=tracks))
        self.catalog, self.serial = value, self.serial + 1
        self.cache_pending = value

    def _save_cache(self, value):
        atomic(self.cache_path, (json.dumps(value, ensure_ascii=False) + '\n').encode())

    def tick(self, selected=False, force=False):
        timestamp = self.now()
        if timestamp >= self.next_read or force:
            self.next_read = timestamp + 1
            try:
                approvals = read_approvals(self.approvals_path)
                self.readable, self.read_failed_at = True, None
                if approvals != self.approvals:
                    self.serial += 1
                    self.approvals = approvals
            except (OSError, ValueError, sqlite3.Error):
                self.readable = False
                if self.read_failed_at is None:
                    self.read_failed_at = timestamp
                self.error = 'CATALOG'
        grants = {(a['kind'], a['id']): a for a in self.approvals['approvals']} if self.approvals else {}
        for key, future in list(self.jobs.items()):
            if not future.done():
                continue
            del self.jobs[key]
            try:
                group, facts = future.result()
                if key in grants:
                    self._publish(group, facts)
                    self.group_status[key] = 'READY'
                    self.next_group[key] = timestamp + (45 if key[0] == 'playlist' else ARTIST_REFRESH)
            except SpotifyError as exc:
                self.group_status[key] = 'AUTH' if exc.status in (400, 401) else 'OFFLINE' if exc.status is None else 'ERROR'
                self.error = 'ACCOUNT' if exc.status in (400, 401) else 'NETWORK' if exc.status is None else 'CATALOG'
                self.next_group[key] = timestamp + max(30, exc.retry_after or 0)
            except (OSError, ValueError, KeyError, TypeError):
                self.group_status[key], self.error, self.next_group[key] = 'ERROR', 'CATALOG', timestamp + 30
        if selected and self.readable:
            existing = {(g['kind'], g['id']): g for g in self.catalog['groups']}
            for key in sorted(grants, key=lambda k: (k[0] != 'playlist', k[1])):
                if len(self.jobs) >= 2:
                    break
                if key in self.jobs:
                    continue
                if key[0] == 'artist' and any(active[0] == 'artist' for active in self.jobs):
                    continue  # Keep one worker available for live playlist edits.
                group = existing.get(key)
                verified = group.get('verified_at', self.catalog['generated_at']) if group else 0
                interval = 45 if key[0] == 'playlist' else ARTIST_REFRESH
                due = self.next_group.get(key, verified + interval if group else 0)
                if timestamp >= due or (force and key not in self.group_status):
                    self.jobs[key] = self.executor.submit(self._fetch, dict(grants[key]))
                    self.group_status[key] = 'LOADING'
        if not self.readable:
            self.status = 'ERROR'
        elif not grants:
            self.status, self.error = 'EMPTY', None
        elif any((g['kind'], g['id']) in grants for g in self.catalog['groups']):
            available = any(g['track_ids'] for g in self.catalog['groups'] if (g['kind'], g['id']) in grants)
            self.status = 'READY' if available else 'LOADING' if self.jobs else 'EMPTY'
        elif self.jobs:
            self.status = 'LOADING'
        else:
            self.status = next((s for s in self.group_status.values() if s in ('AUTH', 'OFFLINE', 'ERROR')), 'AUTH')
        if self.cache_job and self.cache_job.done():
            try:
                self.cache_job.result()
            except (OSError, ValueError):
                pass  # The live complete view remains usable; persistence is optional.
            self.cache_job = None
        if self.cache_pending is not None and not self.cache_job:
            self.cache_job = self.cache_writer.submit(self._save_cache, self.cache_pending)
            self.cache_pending = None
        return self.serial

    def pending_for(self, identity):
        return any((group['kind'], group['id']) in self.jobs and identity in group['track_ids']
                   for group in self.catalog['groups'] if group['kind'] == 'playlist')

    def permitted(self, identity, start=True):
        if start and not self.readable:
            return False
        if not start and self.read_failed_at is not None and self.now() - self.read_failed_at > 2:
            return False
        return allowed(self.catalog['tracks'].get(identity), self.approvals, self.catalog, self.now(), fresh=start)

    def close(self):
        self.closed.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.cache_writer.shutdown(wait=False, cancel_futures=True)
