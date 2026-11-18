"""Synthetic identifiers and labels only. No account, network or music is used."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from host.approvals import Conflict, Store, identity

SCRIPT = Path(__file__).resolve().parents[1] / 'host/approvals.py'
ARTIST = 'A' * 22
PLAYLIST = 'P' * 22


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'private' / 'approvals.sqlite3'
        self.store = Store(self.path)

    def cli(self, *args, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), '--db', str(self.path), *args],
                                text=True, capture_output=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(result.stdout, '')
        return result.stderr

    def test_cli_persistence_labels_duplicates_removal_and_export(self):
        self.assertEqual(self.cli('init'), dict(version=1, revision=0, approvals=[]))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        first = self.cli('add', 'artist', 'spotify:artist:' + ARTIST, '--label', 'Synthetic artist')
        self.assertEqual(first['revision'], 1)
        self.assertEqual(self.cli('add', 'artist', ARTIST), first)
        self.assertEqual(self.cli('init'), first)
        playlist = self.cli('add', 'playlist', 'https://open.spotify.com/playlist/' + PLAYLIST + '?si=synthetic')
        self.assertEqual(playlist['revision'], 2)
        self.assertEqual(self.cli('list'), playlist)
        self.assertEqual(self.cli('export'), playlist)
        self.assertEqual(playlist['approvals'][0], dict(provider='spotify', kind='artist', id=ARTIST, label='Synthetic artist'))
        updated = self.cli('add', 'artist', ARTIST, '--label', 'Renamed', '--expected-revision', '2')
        self.assertEqual(updated['revision'], 3)
        removed = self.cli('remove', 'artist', ARTIST, '--expected-revision', '3')
        self.assertEqual(removed['revision'], 4)
        self.assertEqual([a['kind'] for a in removed['approvals']], ['playlist'])
        self.assertEqual(self.cli('remove', 'artist', ARTIST), removed)

    def test_read_and_edit_missing_database_do_not_create_paths(self):
        for command in [('list',), ('export',), ('add', 'artist', ARTIST), ('remove', 'artist', ARTIST)]:
            self.cli(*command, success=False)
            self.assertFalse(self.path.parent.exists())

    def test_identity_normalization_and_rejection(self):
        for value in [ARTIST, 'spotify:artist:' + ARTIST, 'https://open.spotify.com/artist/' + ARTIST,
                      'https://open.spotify.com/artist/' + ARTIST + '/?si=synthetic']:
            self.assertEqual(identity('artist', value), ARTIST)
        for value in ['', 'A' * 21, 'A' * 23, '/' * 22, 'spotify:playlist:' + ARTIST,
                      'https://open.spotify.com/playlist/' + ARTIST,
                      'http://open.spotify.com/artist/' + ARTIST,
                      'https://open.spotify.com.evil/artist/' + ARTIST,
                      'https://user@open.spotify.com/artist/' + ARTIST,
                      'https://open.spotify.com:443/artist/' + ARTIST,
                      'https://open.spotify.com/artist/' + ARTIST + '#fragment',
                      'https://open.spotify.com/artist/%41' + ARTIST[1:],
                      'https://open.spotify.com/artist/' + ARTIST + '\n']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                identity('artist', value)

    def test_invalid_input_and_labels_preserve_database(self):
        self.store.initialize()
        original = self.path.read_bytes()
        for name in ['', ' ', '\n', '\x00', 'x' * 257, 'Invisible\u200b']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.add('artist', ARTIST, name)
            self.assertEqual(self.path.read_bytes(), original)
        self.cli('add', 'artist', 'not-an-id', success=False)
        self.cli('add', 'artist', ARTIST, '--expected-revision', '-1', success=False)
        self.assertEqual(self.store.snapshot()['revision'], 0)

    def test_competing_writers_only_one_can_use_revision(self):
        self.store.initialize()
        def write(value):
            try:
                return Store(self.path).add('artist', value, expected_revision=0)
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, [ARTIST, 'B' * 22]))
        self.assertEqual(sum(r is not None for r in results), 1)
        result = self.store.snapshot()
        self.assertEqual(result['revision'], 1)
        self.assertEqual(len(result['approvals']), 1)
        self.cli('remove', 'artist', result['approvals'][0]['id'], '--expected-revision', '0', success=False)
        self.assertEqual(self.store.snapshot(), result)

    def test_unsupported_corrupt_and_incompatible_databases_are_not_reset(self):
        for kind in ['version', 'corrupt', 'schema']:
            with self.subTest(kind=kind):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.unlink(missing_ok=True)
                if kind == 'corrupt':
                    self.path.write_bytes(b'synthetic invalid database')
                else:
                    self.store.initialize()
                    with sqlite3.connect(self.path) as connection:
                        if kind == 'version':
                            connection.execute('PRAGMA user_version = 2')
                        else:
                            connection.execute('ALTER TABLE approvals ADD COLUMN unexpected TEXT')
                original = self.path.read_bytes()
                for args in [('init',), ('list',), ('add', 'artist', ARTIST), ('remove', 'artist', ARTIST)]:
                    self.cli(*args, success=False)
                    self.assertEqual(self.path.read_bytes(), original)

    def test_concurrent_initialization_publishes_complete_schema(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: Store(self.path).initialize(), range(2)))
        self.assertEqual(results, [dict(version=1, revision=0, approvals=[])] * 2)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_export_is_deterministic_and_sorted(self):
        self.store.initialize()
        self.store.add('playlist', PLAYLIST)
        self.store.add('artist', ARTIST, 'Synthetic')
        first = subprocess.check_output([sys.executable, str(SCRIPT), '--db', str(self.path), 'export'])
        second = subprocess.check_output([sys.executable, str(SCRIPT), '--db', str(self.path), 'export'])
        self.assertEqual(first, second)
        self.assertEqual([a['kind'] for a in json.loads(first)['approvals']], ['artist', 'playlist'])


if __name__ == '__main__':
    unittest.main()
