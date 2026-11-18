#!/usr/bin/env python3
"""Manage parent-approved music sources locally. Does not authorize playback."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit

VERSION = 1
LIMIT = 200
DEFAULT_PATH = Path.home() / '.local/share/family-radio/approvals.sqlite3'
ID_PATTERN = r'[A-Za-z0-9]{22}'


class Conflict(ValueError):
    """The approval list changed since the caller read it."""


def identity(kind, value):
    """Normalize a Spotify ID, URI or canonical share URL without network access."""
    if kind not in ('playlist', 'artist') or not isinstance(value, str):
        raise ValueError('expected playlist or artist and a Spotify identity')
    if any(c.isspace() or unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('Spotify identity must not contain whitespace or control characters')
    if re.fullmatch(ID_PATTERN, value):
        return value
    match = re.fullmatch(r'spotify:(playlist|artist):(' + ID_PATTERN + ')', value)
    if match:
        if match[1] == kind:
            return match[2]
        raise ValueError('Spotify identity has the wrong kind')
    parsed = urlsplit(value)
    match = re.fullmatch(r'/(playlist|artist)/(' + ID_PATTERN + ')/?', parsed.path)
    if (parsed.scheme == 'https' and parsed.netloc == 'open.spotify.com'
            and not parsed.fragment and match and match[1] == kind):
        return match[2]
    raise ValueError('expected a matching Spotify ID, URI or https://open.spotify.com link')


def label(value):
    if value is not None and (not isinstance(value, str) or not value.strip()
            or len(value) > 256 or any(unicodedata.category(c).startswith('C') for c in value)):
        raise ValueError('label must contain 1 to 256 characters without control characters')
    return value


class Store:
    """Small transactional store. Each mutation can require a known revision."""
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path).expanduser()

    def _connect(self, writable=False):
        return sqlite3.connect(self.path.resolve().as_uri() + ('?mode=rw' if writable else '?mode=ro'),
                               uri=True, timeout=5)

    @staticmethod
    def _revision(connection):
        if connection.execute('PRAGMA user_version').fetchone()[0] != VERSION:
            raise ValueError('unsupported approval database version')
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {'metadata', 'approvals'}:
            raise ValueError('invalid approval database schema')
        columns = [tuple(r[1:]) for r in connection.execute('PRAGMA table_info(approvals)')]
        if columns != [('provider', 'TEXT', 1, None, 1), ('kind', 'TEXT', 1, None, 2),
                       ('provider_id', 'TEXT', 1, None, 3), ('label', 'TEXT', 0, None, 0)]:
            raise ValueError('invalid approval database schema')
        if [tuple(r[1:]) for r in connection.execute('PRAGMA table_info(metadata)')] != [('revision', 'INTEGER', 1, None, 0)]:
            raise ValueError('invalid approval database schema')
        rows = connection.execute('SELECT revision FROM metadata').fetchall()
        if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] < 0:
            raise ValueError('invalid approval database revision')
        return rows[0][0]

    @classmethod
    def _snapshot(cls, connection):
        revision = cls._revision(connection)
        entries = []
        for provider, kind, provider_id, name in connection.execute(
                'SELECT provider, kind, provider_id, label FROM approvals ORDER BY provider, kind, provider_id'):
            if provider != 'spotify' or identity(kind, provider_id) != provider_id:
                raise ValueError('invalid approval database entry')
            entries.append(dict(provider=provider, kind=kind, id=provider_id, label=label(name)))
        return dict(version=VERSION, revision=revision, approvals=entries)

    def initialize(self):
        if self.path.exists():
            return self.snapshot()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.approvals-', dir=self.path.parent)
        os.close(fd)
        try:
            with closing(sqlite3.connect(name)) as connection:
                with connection:
                    connection.execute('BEGIN IMMEDIATE')
                    connection.execute('CREATE TABLE metadata (revision INTEGER NOT NULL CHECK(revision >= 0))')
                    connection.execute('INSERT INTO metadata VALUES (0)')
                    connection.execute('''CREATE TABLE approvals (
                        provider TEXT NOT NULL CHECK(provider = 'spotify'),
                        kind TEXT NOT NULL CHECK(kind IN ('playlist', 'artist')),
                        provider_id TEXT NOT NULL,
                        label TEXT,
                        PRIMARY KEY(provider, kind, provider_id))''')
                    connection.execute('PRAGMA user_version = 1')
            try:
                os.link(name, self.path)
            except FileExistsError:
                pass
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(name)
        return self.snapshot()

    def snapshot(self):
        with closing(self._connect()) as connection:
            connection.execute('BEGIN')
            return self._snapshot(connection)

    def _change(self, kind, value, name, remove, expected_revision):
        provider_id = identity(kind, value)
        label(name)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ValueError('expected revision must be a nonnegative integer')
        with closing(self._connect(True)) as connection:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                revision = self._revision(connection)
                if expected_revision is not None and revision != expected_revision:
                    raise Conflict('approval list changed; read it again before editing')
                previous = connection.execute(
                    'SELECT label FROM approvals WHERE provider = ? AND kind = ? AND provider_id = ?',
                    ('spotify', kind, provider_id)).fetchone()
                changed = False
                if remove:
                    if previous is not None:
                        connection.execute('DELETE FROM approvals WHERE provider = ? AND kind = ? AND provider_id = ?',
                                           ('spotify', kind, provider_id))
                        changed = True
                elif previous is None:
                    if connection.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] >= LIMIT:
                        raise ValueError(f'The radio supports up to {LIMIT} approvals. Remove one before adding another.')
                    connection.execute('INSERT INTO approvals VALUES (?, ?, ?, ?)', ('spotify', kind, provider_id, name))
                    changed = True
                elif name is not None and previous[0] != name:
                    connection.execute('UPDATE approvals SET label = ? WHERE provider = ? AND kind = ? AND provider_id = ?',
                                       (name, 'spotify', kind, provider_id))
                    changed = True
                if changed:
                    connection.execute('UPDATE metadata SET revision = revision + 1')
                return self._snapshot(connection)

    def add(self, kind, value, name=None, expected_revision=None):
        return self._change(kind, value, name, False, expected_revision)

    def remove(self, kind, value, expected_revision=None):
        return self._change(kind, value, None, True, expected_revision)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', '--database', dest='database', type=Path, default=DEFAULT_PATH)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='create a private empty database; preserve existing approvals')
    commands.add_parser('list', help='show approvals and current revision as JSON')
    commands.add_parser('export', help='write the versioned approval document to standard output')
    for command in ('add', 'remove'):
        child = commands.add_parser(command)
        child.add_argument('kind', choices=('playlist', 'artist'))
        child.add_argument('identity')
        child.add_argument('--expected-revision', type=int)
        if command == 'add':
            child.add_argument('--label')
    args = parser.parse_args(argv)
    store = Store(args.database)
    try:
        if args.command == 'init':
            result = store.initialize()
        elif args.command in ('list', 'export'):
            result = store.snapshot()
        elif args.command == 'add':
            result = store.add(args.kind, args.identity, args.label, args.expected_revision)
        else:
            result = store.remove(args.kind, args.identity, args.expected_revision)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f'Approval error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
