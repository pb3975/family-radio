"""Offline auth and catalog behavior with synthetic data only."""
import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from host.approvals import Store
from host import spotify

ARTIST = 'A' * 22
OTHER = 'O' * 22
ALBUM = 'L' * 22
SECOND_ALBUM = 'M' * 22
TRACK = 'T' * 22
SECOND_TRACK = 'U' * 22
PLAYLIST = 'P' * 22
SCOPES = spotify.SCOPES


def album(identity=ALBUM):
    return dict(id=identity, name='Synthetic album', images=[dict(url='https://i.scdn.co/image/synthetic')])


def track(identity=TRACK, artists=None, **kwargs):
    return dict(id=identity, uri='spotify:track:' + identity, type='track', name='Synthetic song',
                duration_ms=100000, artists=artists or [dict(id=ARTIST, name='Synthetic artist')],
                album=album(), explicit=False, **kwargs)


def page(items, next_url=None):
    return dict(items=items, next=next_url)


def token(**kwargs):
    value = dict(access_token='synthetic-access', refresh_token='synthetic-refresh', token_type='Bearer',
                 expires_in=3600, scope=SCOPES)
    value.update(kwargs)
    return value


class FakeAuth:
    def __init__(self):
        self.calls = []

    def access_token(self, force=False):
        self.calls.append(force)
        return 'synthetic-refreshed' if force else 'synthetic-access'


class SpotifyAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'credentials'
        self.clock = 1000
        self.requests = []
        self.reply = token()
        def transport(method, url, headers, body=None):
            self.requests.append((method, url, headers, body))
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
        self.auth = spotify.Auth(self.directory, transport, lambda: self.clock)

    def start(self):
        url = self.auth.start('a' * 32)
        return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)

    def complete(self, state):
        self.auth.complete(spotify.REDIRECT + '?' + urllib.parse.urlencode(dict(code='synthetic-code', state=state)))

    def test_start_complete_pkce_permissions_and_session_reuse(self):
        fields = self.start()
        pending = json.loads((self.directory / 'pending.json').read_text())
        digest = base64.urlsafe_b64encode(hashlib.sha256(pending['verifier'].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(fields['code_challenge'], [digest])
        self.assertEqual(fields['redirect_uri'], [spotify.REDIRECT])
        self.assertEqual(fields['scope'], [SCOPES])
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory / 'pending.json').stat().st_mode & 0o777, 0o600)
        self.complete(fields['state'][0])
        self.assertFalse((self.directory / 'pending.json').exists())
        self.assertEqual((self.directory / 'session.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(spotify.Auth(self.directory, self.auth.transport, lambda: self.clock).access_token(), 'synthetic-access')
        form = urllib.parse.parse_qs(self.requests[0][3].decode())
        self.assertEqual(form['code_verifier'], [pending['verifier']])
        self.assertEqual(form['redirect_uri'], [spotify.REDIRECT])
        self.assertEqual(self.requests[0][:2], ('POST', spotify.TOKEN_URL))
        with self.assertRaises(FileNotFoundError):
            self.complete(fields['state'][0])

    def test_wrong_callback_state_error_duplicates_and_expiry_make_no_network_call(self):
        state = self.start()['state'][0]
        for url in [spotify.REDIRECT + '?code=synthetic-code&state=wrong',
                    'https://example.test/callback?code=synthetic-code&state=' + state,
                    spotify.REDIRECT + '?code=synthetic-code&state=' + state + '&state=' + state,
                    spotify.REDIRECT + '?error=access_denied&state=' + state,
                    spotify.REDIRECT + '?state=' + state,
                    spotify.REDIRECT + '?code=synthetic-code&state=' + state + '#fragment',
                    spotify.REDIRECT + '\n?code=synthetic-code&state=' + state]:
            with self.subTest(url=url), self.assertRaises(spotify.SpotifyError):
                self.auth.complete(url)
        self.clock += 601
        with self.assertRaisesRegex(spotify.SpotifyError, 'expired'):
            self.complete(state)
        self.assertEqual(self.requests, [])
        self.assertTrue((self.directory / 'pending.json').exists())

    def test_refresh_preserves_or_rotates_refresh_token_and_refreshes_once(self):
        self.complete(self.start()['state'][0])
        self.clock += 3590
        self.reply = token(access_token='synthetic-second')
        self.reply.pop('refresh_token')
        self.reply.pop('scope')
        self.assertEqual(self.auth.access_token(), 'synthetic-second')
        form = urllib.parse.parse_qs(self.requests[-1][3].decode())
        self.assertEqual(form['refresh_token'], ['synthetic-refresh'])
        saved = json.loads((self.directory / 'session.json').read_text())
        self.assertEqual(saved['refresh_token'], 'synthetic-refresh')
        self.assertEqual(len(self.requests), 2)
        self.auth.access_token()
        self.assertEqual(len(self.requests), 2)
        self.reply = token(refresh_token='synthetic-rotated')
        self.auth.access_token(force=True)
        self.assertEqual(json.loads((self.directory / 'session.json').read_text())['refresh_token'], 'synthetic-rotated')

    def test_malformed_token_response_fails_without_publishing_session(self):
        state = self.start()['state'][0]
        for changes in [dict(token_type=None), dict(access_token=''), dict(expires_in=True),
                        dict(refresh_token=None), dict(scope=[]), dict(expires_in=9999999)]:
            self.reply = token(**changes)
            with self.subTest(changes=changes), self.assertRaises(spotify.SpotifyError):
                self.complete(state)
            self.assertFalse((self.directory / 'session.json').exists())

    def test_failed_exchange_or_refresh_preserves_existing_credentials(self):
        self.complete(self.start()['state'][0])
        original = (self.directory / 'session.json').read_bytes()
        self.reply = spotify.SpotifyError('Spotify request rejected (HTTP 400)', 400)
        with self.assertRaises(spotify.SpotifyError):
            self.auth.access_token(force=True)
        self.assertEqual((self.directory / 'session.json').read_bytes(), original)
        self.reply = token(expires_in=0)
        with self.assertRaises(spotify.SpotifyError):
            self.auth.access_token(force=True)
        self.assertEqual((self.directory / 'session.json').read_bytes(), original)
        self.reply = token(scope='playlist-read-private')
        with self.assertRaises(spotify.SpotifyError):
            self.auth.access_token(force=True)
        self.assertEqual((self.directory / 'session.json').read_bytes(), original)

    def test_cli_entrypoints_do_not_emit_tokens_or_callback(self):
        client_file = self.root / 'client'
        client_file.write_text('a' * 32)
        stdout, stderr = io.StringIO(), io.StringIO()
        base = ['--directory', str(self.directory)]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(spotify.main(base + ['login-start', '--client-id-file', str(client_file)], self.auth.transport, lambda: self.clock), 0)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(stdout.getvalue().strip()).query)['state'][0]
        callback = self.root / 'callback'
        callback.write_text(spotify.REDIRECT + '?' + urllib.parse.urlencode(dict(code='synthetic-code', state=state)))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(spotify.main(base + ['login-complete', '--callback-file', str(callback)], self.auth.transport, lambda: self.clock), 0)
        for secret in ['synthetic-code', 'synthetic-access', 'synthetic-refresh', state]:
            self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(spotify.main(base + ['login-complete', '--callback-file', str(callback)], self.auth.transport), 2)
        self.assertNotIn('Traceback', stderr.getvalue())
        self.assertEqual(stdout.getvalue(), '')


class SpotifyCatalogTests(unittest.TestCase):
    def setUp(self):
        self.auth = FakeAuth()
        self.calls = []
        self.responses = {}
        def transport(method, url, headers, body=None):
            self.calls.append((method, url, headers))
            result = self.responses[url]
            if isinstance(result, Exception):
                raise result
            return result
        self.client = spotify.Client(self.auth, transport)

    def test_unauthorized_refreshes_once_and_429_does_not_sleep(self):
        count = 0
        def transport(method, url, headers, body=None):
            nonlocal count
            count += 1
            if count == 1:
                raise spotify.SpotifyError('Spotify request rejected (HTTP 401)', 401)
            self.assertEqual(headers['Authorization'], 'Bearer synthetic-refreshed')
            return dict(id='synthetic-user')
        client = spotify.Client(self.auth, transport)
        self.assertEqual(client.me()['id'], 'synthetic-user')
        self.assertEqual(self.auth.calls, [False, True])
        self.responses[spotify.API + '/me'] = spotify.SpotifyError('Spotify request rejected (HTTP 429)', 429, 60)
        with patch('time.sleep', side_effect=AssertionError('must not sleep')):
            with self.assertRaises(spotify.SpotifyError) as caught:
                self.client.me()
        self.assertEqual(caught.exception.retry_after, 60)
        self.assertEqual(len(self.calls), 1)

    def test_offorigin_redirect_and_pagination_never_receive_bearer(self):
        for url in ['http://api.spotify.com/v1/me', 'https://evil.test/v1/me',
                    'https://api.spotify.com.evil.test/v1/me', '//evil.test/v1/me',
                    'https://user@api.spotify.com/v1/me', 'https://api.spotify.com:443/v1/me',
                    'https://api.spotify.com/v1/me\n']:
            with self.subTest(url=url), self.assertRaises(spotify.SpotifyError):
                self.client.get(url)
        self.assertEqual(self.auth.calls, [])
        self.responses[spotify.API + '/synthetic'] = page([], 'https://evil.test/v1/synthetic')
        with self.assertRaises(spotify.SpotifyError):
            list(self.client.pages('/synthetic'))
        self.assertEqual(len(self.calls), 1)
        self.responses[spotify.API + '/synthetic'] = page([], spotify.API + '/me')
        with self.assertRaises(spotify.SpotifyError):
            list(self.client.pages('/synthetic'))
        self.assertEqual(len(self.calls), 2)
        handler = spotify.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, '', {}, 'https://evil.test'))

    def test_playlist_current_item_shape_full_pagination_and_stable_snapshot(self):
        meta_url = spotify.API + '/playlists/' + PLAYLIST + '?fields=id,name,snapshot_id'
        self.responses[meta_url] = dict(id=PLAYLIST, name='Synthetic playlist', snapshot_id='synthetic-snapshot')
        first = spotify.API + '/playlists/' + PLAYLIST + '/items?limit=50&market=from_token'
        second = spotify.API + '/playlists/' + PLAYLIST + '/items?offset=50&limit=50&market=from_token'
        self.responses[first] = page([dict(item=track()), dict(item=None), dict(item=dict(type='episode')),
                                      dict(is_local=True, item=track(SECOND_TRACK)),
                                      dict(item=track('X' * 22, is_playable=False)),
                                      dict(item=track('Y' * 22, restrictions=dict(reason='market')))], second)
        self.responses[second] = page([dict(item=track(SECOND_TRACK)), dict(item=track())])
        result = self.client.playlist(PLAYLIST)
        self.assertEqual([t['id'] for t in result['tracks']], [TRACK, SECOND_TRACK])
        self.assertEqual(result['snapshot_id'], 'synthetic-snapshot')
        self.assertEqual(result['tracks'][0]['album']['image_url'], 'https://i.scdn.co/image/synthetic')
        self.assertEqual(len(self.calls), 4)

    def test_playlist_change_during_paging_fails(self):
        snapshots = iter(['before', 'after'])
        def transport(method, url, headers, body=None):
            if '/items?' in url:
                return page([dict(item=track())])
            return dict(id=PLAYLIST, name='Synthetic', snapshot_id=next(snapshots))
        with self.assertRaisesRegex(spotify.SpotifyError, 'changed'):
            spotify.Client(self.auth, transport).playlist(PLAYLIST)

    def test_artist_pages_all_albums_and_tracks_filters_exact_track_credits(self):
        self.responses[spotify.API + '/artists/' + ARTIST] = dict(id=ARTIST, name='Synthetic artist')
        first = spotify.API + '/artists/' + ARTIST + '/albums?include_groups=album,single,appears_on,compilation&limit=10&market=from_token'
        second = spotify.API + '/artists/' + ARTIST + '/albums?offset=10&limit=10&market=from_token'
        self.responses[first] = page([album()], second)
        self.responses[second] = page([album(SECOND_ALBUM), album()])
        next_tracks = spotify.API + '/albums/' + ALBUM + '/tracks?offset=50&limit=50&market=from_token'
        a = track()
        a.pop('album')
        b = track(SECOND_TRACK, artists=[dict(id=OTHER, name='Other'), dict(id=ARTIST, name='Synthetic artist')])
        b.pop('album')
        unrelated = track('Z' * 22, artists=[dict(id=OTHER, name='Other')])
        unrelated.pop('album')
        self.responses[spotify.API + '/albums/' + ALBUM + '/tracks?limit=50&market=from_token'] = page([a, unrelated], next_tracks)
        self.responses[next_tracks] = page([b])
        self.responses[spotify.API + '/albums/' + SECOND_ALBUM + '/tracks?limit=50&market=from_token'] = page([unrelated])
        result = self.client.artist(ARTIST)
        self.assertEqual([item['id'] for item in result['tracks']], [TRACK, SECOND_TRACK])
        self.assertEqual(result['tracks'][0]['album']['id'], ALBUM)
        self.assertEqual(sum('/albums/' + ALBUM + '/tracks?limit=50&market=from_token' in c[1] for c in self.calls), 1)

    def test_http_transport_bounds_json_and_sanitizes_server_errors(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, limit):
                self.limit = limit
                return b'{}'
        class Opener:
            def open(self, request, timeout):
                self.request, self.timeout = request, timeout
                return response
        response, opener = Response(), Opener()
        with patch('urllib.request.build_opener', return_value=opener) as build:
            self.assertEqual(spotify.request_json('GET', spotify.API + '/synthetic', {}), {})
        self.assertEqual(build.call_args.args, (spotify.NoRedirect,))
        self.assertEqual(opener.timeout, 15)
        self.assertEqual(response.limit, spotify.MAX_RESPONSE + 1)
        error = urllib.error.HTTPError(spotify.API + '/synthetic', 429, 'synthetic-private-payload',
                                       {'Retry-After': '60'}, io.BytesIO(b'synthetic-private-token'))
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.side_effect = error
            with self.assertRaises(spotify.SpotifyError) as caught:
                spotify.request_json('GET', spotify.API + '/synthetic', {})
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 60)
        self.assertNotIn('synthetic-private', str(caught.exception))

    def test_market_follows_user_token_across_pages_and_rejects_original_alias(self):
        first = spotify.API + '/playlists/' + PLAYLIST + '/items?limit=50&market=from_token'
        next_without_market = spotify.API + '/playlists/' + PLAYLIST + '/items?offset=50&limit=50'
        next_with_market = next_without_market + '&market=from_token'
        original = 'R' * 22
        playable = track(is_playable=True, linked_from=dict(id=original, uri='spotify:track:' + original))
        self.responses[first] = page([dict(item=playable)], next_without_market)
        self.responses[next_with_market] = page([])
        metadata = dict(id=PLAYLIST, name='Synthetic playlist', snapshot_id='unchanged')
        self.responses[spotify.API + '/playlists/' + PLAYLIST + '?fields=id,name,snapshot_id'] = metadata
        result = self.client.playlist(PLAYLIST)
        self.assertEqual([item['id'] for item in result['tracks']], [TRACK])
        self.assertEqual(result['tracks'][0]['uri'], 'spotify:track:' + TRACK)
        self.assertNotIn('linked_from', result['tracks'][0])
        self.assertNotIn(original, json.dumps(result))
        self.assertTrue(all('market=from_token' in call[1] for call in self.calls if '/items?' in call[1]))
        without_alias = dict(playable)
        without_alias.pop('linked_from')
        self.assertEqual(spotify.track_fact(without_alias), result['tracks'][0])

    def test_relinked_artist_uses_returned_track_credits_not_album_or_original(self):
        original = 'R' * 22
        self.responses[spotify.API + '/artists/' + ARTIST] = dict(id=ARTIST, name='Synthetic artist')
        album_path = spotify.API + '/artists/' + ARTIST + '/albums?include_groups=album,single,appears_on,compilation&limit=10&market=from_token'
        credited_album = album()
        credited_album['artists'] = [dict(id=ARTIST, name='Synthetic artist')]
        self.responses[album_path] = page([credited_album])
        permitted = track(is_playable=True, linked_from=dict(id=original, uri='spotify:track:' + original))
        unrelated = track(SECOND_TRACK, artists=[dict(id=OTHER, name='Unapproved artist')],
                          is_playable=True, linked_from=dict(id=original, uri='spotify:track:' + original))
        self.responses[spotify.API + '/albums/' + ALBUM + '/tracks?limit=50&market=from_token'] = page([permitted, unrelated])
        result = self.client.artist(ARTIST)
        self.assertEqual([item['id'] for item in result['tracks']], [TRACK])
        self.assertEqual(result['tracks'][0]['artists'][0]['id'], ARTIST)
        self.assertNotIn(original, json.dumps(result))

    def test_pagination_cycle_and_limits_fail(self):
        url = spotify.API + '/synthetic'
        self.responses[url] = page([], url)
        with self.assertRaisesRegex(spotify.SpotifyError, 'pagination'):
            list(self.client.pages(url))
        self.assertEqual(len(self.calls), 1)
        self.responses[url] = page([])
        self.client.requests_remaining = 0
        with self.assertRaisesRegex(spotify.SpotifyError, 'request limit'):
            self.client.get(url)

    def test_catalog_cli_atomic_publication_and_failure_preserves_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / 'approvals.db')
            store.initialize()
            store.add('playlist', PLAYLIST)
            auth = spotify.Auth(root / 'credentials', lambda *a: token(), lambda: 100)
            fields = urllib.parse.parse_qs(urllib.parse.urlsplit(auth.start('a' * 32)).query)
            auth.complete(spotify.REDIRECT + '?' + urllib.parse.urlencode(dict(code='synthetic', state=fields['state'][0])))
            def transport(method, url, headers, body=None):
                if '/items?' in url:
                    return page([dict(item=track())])
                return dict(id=PLAYLIST, name='Synthetic playlist', snapshot_id='synthetic-snapshot')
            output = root / 'catalog.json'
            args = ['--directory', str(auth.directory), 'catalog', '--approvals-db', str(store.path), '--output', str(output)]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(spotify.main(args, transport, lambda: 100), 0)
            value = json.loads(output.read_text())
            self.assertEqual(value['version'], 1)
            self.assertEqual(value['approvals_revision'], 1)
            self.assertEqual(value['groups'][0]['track_ids'], [TRACK])
            self.assertEqual(value['tracks'][TRACK]['artists'][0]['id'], ARTIST)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            previous = output.read_bytes()
            def failing(*args):
                raise spotify.SpotifyError('Spotify request rejected (HTTP 403)', 403)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(spotify.main(args, failing, lambda: 100), 2)
            self.assertIn('HTTP 403', stderr.getvalue())
            self.assertEqual(output.read_bytes(), previous)
            self.assertNotIn('synthetic-access', stdout.getvalue() + stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
