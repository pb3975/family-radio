"""Real local HTTP with synthetic audio and Spotify responses. No hardware or account."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import http.client
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import urlencode, urlsplit, parse_qs

from host import catalogs, dashboard, library, spotify, visuals
from host.radio import Controller


class FakeSpotify:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.repeated = False

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url))
        if self.failure:
            raise spotify.SpotifyError('Synthetic secret must never reach the browser', status=self.failure if self.failure != 'offline' else None)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.path == '/api/token':
            return dict(access_token='synthetic-access-refreshed', token_type='Bearer', expires_in=3600)
        if parsed.path == '/v1/me':
            return dict(id='synthetic-parent', display_name='Synthetic parent')
        if parsed.path == '/v1/me/playlists':
            if 'offset' not in query or self.repeated:
                return dict(items=[dict(id='P' * 22, name='Synthetic first playlist', owner=dict(id='synthetic-parent')),
                                   dict(id='F' * 22, name='Synthetic followed playlist', owner=dict(id='someone-else')),
                                   dict(id='C' * 22, name='Synthetic shared playlist', owner=dict(id='someone-else'), collaborative=True)],
                            next='https://api.spotify.com/v1/me/playlists?offset=1&limit=50')
            return dict(items=[dict(id='Q' * 22, name='Synthetic second playlist', owner=dict(id='synthetic-parent'))], next=None)
        if parsed.path == '/v1/search':
            return dict(artists=dict(items=[dict(id='A' * 22, name='Synthetic artist')], next=None))
        raise AssertionError('Unexpected Spotify endpoint: ' + url)


class DashboardHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.TemporaryDirectory()
        cls.tones = {}
        for name, frequency in [('Synthetic one.wav', 440), ('Synthetic two.mp3', 660)]:
            path = Path(cls.media.name) / name
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'sine=frequency={frequency}:duration=0.4',
                            '-metadata', 'title=' + Path(name).stem, '-metadata', 'artist=Synthetic artist', str(path)],
                           check=True, capture_output=True, timeout=30)
            cls.tones[name] = path.read_bytes()
        cls.image = dashboard.png(2, 2, b'\x40\x80\xc0' * 4)

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake = FakeSpotify()
        self.time = 1700001000
        self.app = dashboard.Dashboard(self.root, self.fake, now=lambda: self.time)
        self.server = dashboard.Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.cookie = None
        self.login()

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, value=None, raw=None, headers=None, cookie=True):
        fields = dict(headers or {})
        if cookie and self.cookie:
            fields['Cookie'] = self.cookie
        if value is not None:
            raw = json.dumps(value).encode()
            fields.setdefault('Content-Type', 'application/json')
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=15)
        try:
            connection.request(method, path, body=raw, headers=fields)
            response = connection.getresponse()
            body = response.read()
            if body and response.getheader('Content-Type', '').startswith('application/json'):
                body = json.loads(body)
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def login(self):
        token = self.app.token()
        status, headers, _ = self.request('GET', '/login?' + urlencode(dict(token=token)), cookie=False)
        self.assertEqual(status, 303)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        return token, headers

    def state(self):
        status, _, value = self.request('GET', '/api/state')
        self.assertEqual(status, 200, value)
        if self.app.library_path.exists():
            self.assertEqual(len(library.load(self.app.library_path)['songs']), len(value['library']['songs']))
            self.assertEqual(value['library']['revision'], hashlib.sha256(self.app.library_path.read_bytes()).hexdigest())
        return value

    def approval(self, action, kind='playlist', identity='P' * 22, **fields):
        revision = fields.pop('revision', self.state()['approvals']['revision'])
        return self.request('POST', '/api/approvals', dict(action=action, kind=kind, identity=identity, revision=revision, **fields))

    def audio(self, name='Synthetic one.wav', revision=None, raw=None):
        return self.request('POST', '/api/library/audio', raw=self.tones[name] if raw is None else raw,
                            headers={'Content-Type': 'application/octet-stream', 'X-Filename': name,
                                     'X-Library-Revision': revision or self.state()['library']['revision']})

    def library_edit(self, action, identity, **fields):
        revision = fields.pop('revision', self.state()['library']['revision'])
        return self.request('POST', '/api/library', dict(action=action, id=identity, revision=revision, **fields))

    def cover(self, identity, raw=None, revision=None):
        return self.request('POST', '/api/library/cover', raw=self.image if raw is None else raw,
                            headers={'Content-Type': 'image/png', 'X-Song-Id': identity,
                                     'X-Library-Revision': revision or self.state()['library']['revision']})

    def authorize_spotify(self):
        directory = self.app.spotify_directory
        directory.mkdir(mode=0o700)
        library.atomic(directory / 'session.json', dashboard.encoded(dict(version=1, client_id='a' * 32,
                       access_token='synthetic-access', refresh_token='synthetic-refresh',
                       expires_at=self.time + 3600, scope=spotify.SCOPES)))

    def test_login_cookie_gate_single_use_expiry_and_security_headers(self):
        for path in ('/api/state', '/api/spotify/playlists', '/api/library/cover?id=' + 'a' * 64):
            self.assertEqual(self.request('GET', path, cookie=False)[0], 401)
        self.assertEqual(self.request('POST', '/api/approvals', value={}, cookie=False)[0], 401)
        self.assertEqual(self.request('GET', '/login?token=incorrect')[0], 403)
        self.assertEqual(self.request('GET', '/login?token=%E2%98%83')[0], 403)
        token, headers = self.login()
        for option in ('HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(option, headers['Set-Cookie'])
        self.assertNotIn(token, headers['Set-Cookie'])
        self.assertEqual(headers['Location'], '/')
        self.assertEqual(self.request('GET', '/login?' + urlencode(dict(token=token)))[0], 403)
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'Local library', body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
        self.assertIn("'sha256-", headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-inline', headers['Content-Security-Policy'])
        self.time += dashboard.SESSION_SECONDS
        self.assertEqual(self.request('GET', '/api/state')[0], 401)

    def test_private_token_permissions_and_restart(self):
        self.assertEqual(self.app.private.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.app.token_path.stat().st_mode & 0o777, 0o600)
        app = dashboard.Dashboard(self.root, now=lambda: self.time)
        self.assertTrue(app.authenticated(self.cookie), 'session survives a dashboard restart')
        self.assertNotIn(self.cookie.split('=', 1)[1], self.app.sessions_path.read_text(), 'only session hashes are stored')
        self.assertEqual(self.app.sessions_path.stat().st_mode & 0o777, 0o600)
        self.app.sessions_path.unlink()
        self.assertEqual(self.request('GET', '/api/state')[0], 401, 'deleting the session file signs browsers out')
        self.login()
        self.time += dashboard.SESSION_SECONDS
        self.assertFalse(dashboard.Dashboard(self.root, now=lambda: self.time).authenticated(self.cookie))
        leftover = self.app.private / 'upload-interrupted'
        leftover.mkdir()
        (leftover / 'input').write_bytes(b'partial synthetic upload')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dashboard.main(['login-url', '--data-dir', str(self.root)]), 0)
        self.assertTrue(leftover.exists(), 'login-url leaves a running server upload alone')
        dashboard.Dashboard(self.root).sweep_uploads()
        self.assertFalse(leftover.exists(), 'interrupted uploads are removed when serving starts')
        self.app.token_path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private'):
            dashboard.Dashboard(self.root)
        self.app.token_path.chmod(0o600)
        self.app.private.chmod(0o755)
        with self.assertRaisesRegex(ValueError, '0700'):
            dashboard.Dashboard(self.root)
        self.app.private.chmod(0o700)

    def test_cross_origin_and_wrong_content_type_do_not_change_data(self):
        fields = dict(action='add', kind='artist', identity='A' * 22, revision=0)
        for headers in ({'Origin': 'https://other.invalid'}, {'Sec-Fetch-Site': 'cross-site'},
                        {'Origin': 'null'}, {'Origin': 'http://127.0.0.1:' + str(self.server.server_port) + '.other.invalid'}):
            self.assertEqual(self.request('POST', '/api/approvals', fields, headers=headers)[0], 403)
        self.assertEqual(self.request('POST', '/api/approvals', fields, headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.state()['approvals']['revision'], 0)
        origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.assertEqual(self.request('POST', '/api/approvals', fields, headers={'Origin': origin})[0], 200)

    def test_upload_origin_and_media_types_are_checked_before_writes(self):
        revision = self.state()['library']['revision']
        headers = {'X-Filename': 'Synthetic.wav', 'X-Library-Revision': revision}
        self.assertEqual(self.request('POST', '/api/library/audio', raw=b'bad', headers={
            **headers, 'Origin': 'http://other.invalid', 'Content-Type': 'application/octet-stream'})[0], 403)
        self.assertEqual(self.request('POST', '/api/library/audio', raw=b'bad', headers={
            **headers, 'Content-Type': 'audio/wav'})[0], 415)
        self.assertEqual(self.audio()[0], 200)
        song = self.state()['library']['songs'][0]
        self.assertEqual(self.request('POST', '/api/library/cover', raw=self.image, headers={
            'X-Song-Id': song['id'], 'X-Library-Revision': self.state()['library']['revision'],
            'Content-Type': 'application/octet-stream'})[0], 415)
        self.assertEqual(self.cover(song['id'], raw=b'\x89PNG\r\n\x1a\ninvalid')[0], 422)
        self.assertIsNone(self.state()['library']['songs'][0]['cover'])

    def test_body_limits_and_malformed_requests(self):
        for body in (b'[1]', b'{', b'null', b'\xff', b'{"action":true}'):
            self.assertEqual(self.request('POST', '/api/approvals', raw=body, headers={'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(self.request('POST', '/api/approvals', raw=b'x' * (dashboard.JSON_LIMIT + 1),
                                     headers={'Content-Type': 'application/json'})[0], 413)
        revision = self.state()['library']['revision']
        self.assertEqual(self.request('POST', '/api/library/audio', raw=b'', headers={
            'Content-Type': 'application/octet-stream', 'Content-Length': str(dashboard.AUDIO_LIMIT + 1),
            'X-Filename': 'Synthetic.wav', 'X-Library-Revision': revision})[0], 413)
        self.assertEqual(self.audio()[0], 200)
        song = self.state()['library']['songs'][0]
        self.assertEqual(self.request('POST', '/api/library/cover', raw=b'', headers={
            'Content-Type': 'image/png', 'Content-Length': str(dashboard.IMAGE_LIMIT + 1),
            'X-Song-Id': song['id'], 'X-Library-Revision': self.state()['library']['revision']})[0], 413)
        self.assertEqual(self.request('POST', '/api/approvals', raw=b'{}', headers={
            'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked'})[0], 411)
        self.assertEqual(self.request('GET', '/api/unknown')[0], 404)
        self.assertEqual(self.request('POST', '/api/library/audio', raw=b'bad', headers={
            'Content-Type': 'application/octet-stream', 'X-Filename': '..%2Fescape.wav',
            'X-Library-Revision': self.state()['library']['revision']})[0], 400)

    def test_approval_add_label_rename_remove_conflict_and_catalog_join(self):
        self.assertEqual(self.approval('add', identity='https://open.spotify.com/playlist/' + 'P' * 22 + '?si=synthetic')[0], 200)
        self.assertEqual(self.approval('add', 'artist', 'spotify:artist:' + 'A' * 22, label='Synthetic family artist')[0], 200)
        self.assertEqual(self.approval('add', 'artist', 'A' * 22, revision=0)[0], 409)
        library.atomic(self.app.catalog_path, dashboard.encoded(dict(version=1, approvals_revision=2, generated_at=1700000000,
                       tracks={'T' * 22: dict(id='T' * 22, uri='spotify:track:' + 'T' * 22, title='Synthetic track',
                       artists=[dict(id='A' * 22, name='Synthetic artist')], duration_ms=1000, explicit=False,
                       url='https://open.spotify.com/track/' + 'T' * 22, album=dict(id='B' * 22,
                       name='Synthetic album', image_url=None, url='https://open.spotify.com/album/' + 'B' * 22))},
                       groups=[dict(kind='playlist', id='P' * 22, name='Synthetic catalog name',
                                    track_ids=['T' * 22], verified_at=1700000100)])))
        items = self.state()['approvals']['approvals']
        playlist = next(i for i in items if i['kind'] == 'playlist')
        self.assertEqual(playlist['name'], 'Synthetic catalog name')
        self.assertEqual(playlist['track_count'], 1)
        self.assertEqual(playlist['verified_at'], 1700000100)
        self.assertEqual(self.approval('rename', label='Synthetic new label')[0], 200)
        self.assertEqual(next(i for i in self.state()['approvals']['approvals'] if i['kind'] == 'playlist')['name'], 'Synthetic new label')
        self.assertEqual(self.approval('remove')[0], 200)
        self.assertEqual(len(self.state()['approvals']['approvals']), 1)
        self.assertEqual(self.approval('add', identity='wrong')[0], 400)
        self.assertEqual(self.approval('add', revision=None)[0], 400)

    def test_approval_limit_keeps_the_radio_store_readable(self):
        for index in range(200):
            self.app.store.add('playlist', f'{index:022d}')
        status, _, value = self.approval('add')
        self.assertEqual(status, 400)
        self.assertIn('200 approvals', value['error'])
        status, _, value = self.approval('rename', label='Synthetic sneaky label')
        self.assertEqual(status, 404, 'rename never inserts a new approval')
        self.assertEqual(len(self.state()['approvals']['approvals']), 200)
        self.assertEqual(len(catalogs.read_approvals(self.app.store.path)['approvals']), 200, 'the radio can still read every approval')

    def test_missing_corrupt_and_invalid_catalog_keep_paste_available(self):
        self.assertEqual(self.approval('add')[0], 200)
        for content in (None, b'{', b'{"version":1,"groups":[]}', b'[]'):
            if content:
                library.atomic(self.app.catalog_path, content)
            value = self.state()
            item = value['approvals']['approvals'][0]
            self.assertEqual(item['status'], 'Waiting for the radio to load it')
            self.assertIsNone(item['track_count'])
            self.assertFalse(value['spotify_authorized'])
        self.assertEqual(self.request('GET', '/api/spotify/playlists')[0], 401)

    def test_spotify_picker_pages_search_and_errors_with_fake_transport(self):
        self.authorize_spotify()
        self.assertTrue(self.state()['spotify_authorized'])
        status, _, value = self.request('GET', '/api/spotify/playlists')
        self.assertEqual(status, 200)
        self.assertEqual([item['id'] for item in value['items']], ['P' * 22, 'F' * 22, 'C' * 22, 'Q' * 22])
        self.assertEqual([item['playable'] for item in value['items']], [True, False, True, True], 'followed playlists are marked unplayable')
        status, _, value = self.request('GET', '/api/spotify/artists?' + urlencode(dict(q='Synthetic & friends')))
        self.assertEqual(status, 200)
        self.assertEqual(value['items'][0]['name'], 'Synthetic artist')
        query = parse_qs(urlsplit(self.fake.calls[-1][1]).query)
        self.assertEqual(query, dict(type=['artist'], q=['Synthetic & friends'], limit=['10']))
        self.assertEqual(self.request('GET', '/api/spotify/artists?q=')[0], 400)
        for failure, expected in ((401, 401), ('offline', 503), (403, 403), (429, 503)):
            self.fake.failure = failure
            status, _, value = self.request('GET', '/api/spotify/playlists')
            self.assertEqual(status, expected)
            self.assertNotIn('Synthetic secret', value['error'])
            self.assertNotIn('synthetic-access', json.dumps(value))
        self.fake.failure = None
        self.fake.repeated = True
        self.assertEqual(self.request('GET', '/api/spotify/playlists')[0], 503)
        self.assertEqual(self.approval('add')[0], 200)

    def test_upload_edit_move_remove_and_valid_manifest_revisions(self):
        self.assertEqual(self.audio()[0], 200)
        first = self.state()['library']['songs'][0]
        original = library.load(self.app.library_path)['songs'][0]
        digest = hashlib.sha256(self.tones['Synthetic one.wav']).digest()
        self.assertEqual(first['id'], hashlib.sha256(b'Synthetic one.wav\0' + digest).hexdigest())
        self.assertEqual(self.library_edit('edit', first['id'], title='Synthetic edited title', artist='Synthetic parent')[0], 200)
        edited = self.state()['library']['songs'][0]
        self.assertEqual((edited['title'], edited['artist']), ('Synthetic edited title', 'Synthetic parent'))
        self.assertEqual(self.library_edit('edit', first['id'], title='Synthetic edited title', artist='')[0], 200)
        self.assertEqual(self.state()['library']['songs'][0]['artist'], '')
        stored = library.load(self.app.library_path)['songs'][0]
        stored['stream_metadata'] = [dict(ARTIST='Synthetic stream artist')]
        library.atomic(self.app.library_path, dashboard.encoded(dict(version=1, songs=[stored])))
        self.assertEqual(self.library_edit('edit', first['id'], title='Synthetic edited title', artist='')[0], 200)
        self.assertEqual(Controller.raw_artist(library.load(self.app.library_path)['songs'][0]), 'Demo Artist',
                         'a cleared artist uses the radio default, not an old stream tag')
        stored = library.load(self.app.library_path)['songs'][0]
        stored['stream_metadata'] = [dict(artist='Synthetic stream artist')]
        library.atomic(self.app.library_path, dashboard.encoded(dict(version=1, songs=[stored])))
        self.assertEqual(self.state()['library']['songs'][0]['artist'], 'Synthetic stream artist', 'the editor shows the artist the radio uses')
        self.assertEqual(self.audio('Synthetic two.mp3')[0], 200)
        second = self.state()['library']['songs'][1]
        self.assertEqual(self.library_edit('move', second['id'], direction=-1)[0], 200)
        self.assertEqual([s['id'] for s in self.state()['library']['songs']], [second['id'], first['id']])
        self.assertEqual(self.library_edit('move', second['id'], direction=1)[0], 200)
        self.assertEqual(self.state()['library']['songs'][0]['id'], first['id'])
        self.assertEqual(self.library_edit('remove', first['id'])[0], 200)
        self.assertTrue((self.app.library_path.parent / original['path']).exists())
        status, _, value = self.library_edit('remove', second['id'])
        self.assertEqual(status, 400)
        self.assertIn('at least one song', value['error'])
        self.assertEqual(len(self.state()['library']['songs']), 1)

    def test_corrupt_duplicate_and_invalid_metadata_preserve_library(self):
        self.assertEqual(self.audio(raw=b'corrupt synthetic audio')[0], 422)
        self.assertEqual(len(self.state()['library']['songs']), 0)
        self.assertEqual(self.audio()[0], 200)
        previous = self.app.library_path.read_bytes()
        self.assertEqual(self.audio()[0], 400)
        song = self.state()['library']['songs'][0]
        self.assertEqual(self.library_edit('edit', song['id'], title='', artist='')[0], 400)
        self.assertEqual(self.library_edit('move', song['id'], direction=9)[0], 400)
        self.assertEqual(self.cover(song['id'], raw=b'not an image')[0], 400)
        self.assertEqual(self.app.library_path.read_bytes(), previous)
        self.assertFalse(list(self.app.private.glob('upload-*')))

    def test_stale_large_upload_reports_the_conflict(self):
        revision = self.state()['library']['revision']
        self.assertEqual(self.audio()[0], 200)
        status, _, value = self.audio(revision=revision, raw=b'\0' * (8 * 1024 * 1024))
        self.assertEqual(status, 409, value)
        self.assertIn('changed in another window', value['error'])

    def test_editor_stays_responsive_while_a_cover_is_prepared(self):
        self.assertEqual(self.audio()[0], 200)
        song = self.state()['library']['songs'][0]
        started, release = threading.Event(), threading.Event()
        prepare = visuals.prepare_song
        def slow(*args):
            started.set()
            release.wait(10)
            return prepare(*args)
        visuals.prepare_song = slow
        self.addCleanup(setattr, visuals, 'prepare_song', prepare)
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(self.cover, song['id'])
            self.assertTrue(started.wait(10))
            edit = executor.submit(self.approval, 'add')
            try:
                self.assertEqual(edit.result(timeout=5)[0], 200, 'approval edits proceed during cover preparation')
            finally:
                release.set()
            self.assertEqual(pending.result()[0], 200)

    def test_unreadable_cover_list_is_left_unchanged(self):
        for name in self.tones:
            self.assertEqual(self.audio(name)[0], 200)
        songs = self.state()['library']['songs']
        self.assertEqual(self.cover(songs[0]['id'])[0], 200)
        damaged = self.app.visuals_path.read_bytes().replace(b'"hz": 10', b'"hz": 11')
        self.app.visuals_path.write_bytes(damaged)
        status, _, value = self.cover(songs[1]['id'])
        self.assertEqual(status, 409, value)
        self.assertEqual(self.app.visuals_path.read_bytes(), damaged)

    def test_revision_conflicts_reject_stale_edit_upload_and_cover(self):
        revision = self.state()['library']['revision']
        self.assertEqual(self.audio()[0], 200)
        song = self.state()['library']['songs'][0]
        self.assertEqual(self.library_edit('edit', song['id'], title='Stale', artist='', revision=revision)[0], 409)
        self.assertEqual(self.audio('Synthetic two.mp3', revision=revision)[0], 409)
        self.assertEqual(self.cover(song['id'], revision=revision)[0], 409)
        self.assertEqual(len(self.state()['library']['songs']), 1)

    def test_concurrent_writers_do_not_lose_a_song(self):
        revision = self.state()['library']['revision']
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda name: self.audio(name, revision=revision)[0], self.tones))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(len(self.state()['library']['songs']), 1)

    def test_cover_upload_is_playable_thumbnail_and_merges_only_one_entry(self):
        for name in self.tones:
            self.assertEqual(self.audio(name)[0], 200)
        songs = self.state()['library']['songs']
        for song in songs:
            self.assertEqual(self.cover(song['id'])[0], 200)
            self.state()
        before = visuals.Visuals(self.app.visuals_path).get(library.load(self.app.library_path)['songs'][0])
        revision = self.state()['library']['revision']
        image = dashboard.png(2, 2, b'\xff\x00\x40' * 4)
        self.assertEqual(self.cover(songs[1]['id'], image)[0], 200)
        self.assertNotEqual(self.state()['library']['revision'], revision)
        prepared = visuals.Visuals(self.app.visuals_path)
        for song in library.load(self.app.library_path)['songs']:
            art, levels, checksum = prepared.get(song)
            self.assertEqual(len(art), visuals.ART_BYTES)
            self.assertGreater(len(levels), 0)
            self.assertNotEqual(checksum, '-')
        self.assertEqual(prepared.get(library.load(self.app.library_path)['songs'][0]), before)
        status, headers, body = self.request('GET', '/api/library/cover?id=' + songs[1]['id'])
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/png')
        self.assertTrue(body.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertTrue(all(s['cover'] for s in self.state()['library']['songs']))
        self.assertEqual(self.library_edit('remove', songs[0]['id'])[0], 200)
        self.assertEqual(self.cover(songs[1]['id'])[0], 200)
        entries = json.loads(self.app.visuals_path.read_text())['songs']
        self.assertEqual([e['id'] for e in entries], [songs[1]['id']], 'removed songs leave the visual manifest')


if __name__ == '__main__':
    unittest.main()
