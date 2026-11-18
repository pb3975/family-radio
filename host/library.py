#!/usr/bin/env python3
"""Validate and import a private local audio library. Originals remain untouched."""
import argparse
import hashlib
import json
import os
import math
import re
from pathlib import Path
import subprocess
import shutil
import tempfile

LIMIT = 1000


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load(path):
    path = Path(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('manifest too large')
    return checked_manifest(json.loads(path.read_text()), path.resolve().parent)


def checked_manifest(value, root):
    root = Path(root).resolve()
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('songs'), list) or not 1 <= len(value['songs']) <= LIMIT:
        raise ValueError('invalid library version or size')
    ids, paths = set(), set()
    for song in value['songs']:
        if not isinstance(song, dict):
            raise ValueError('invalid song')
        name = song.get('path')
        identity = song.get('id')
        if not isinstance(name, str) or Path(name).name != name or name in ('.', '..') or (root / name).resolve().parent != root or Path(name).suffix.lower() not in ('.mp3', '.wav'):
            raise ValueError('invalid song path')
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity) or identity in ids or name in paths:
            raise ValueError('duplicate or invalid identity')
        if not isinstance(song.get('title'), str) or not song['title'].strip() or len(song['title']) > 4096:
            raise ValueError('invalid title')
        if 'artist' in song and (not isinstance(song['artist'], str) or not song['artist'].strip() or len(song['artist']) > 4096):
            raise ValueError('invalid artist')
        duration = song.get('duration')
        if type(duration) not in (float, int) or not math.isfinite(duration) or not 0 < duration <= 604800:
            raise ValueError('invalid duration')
        if not isinstance(song.get('source_filename'), str) or not isinstance(song.get('source_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', song['source_sha256']) or not isinstance(song.get('metadata'), dict):
            raise ValueError('invalid source metadata')
        ids.add(identity)
        paths.add(name)
    return value


def tagged_artist(song):
    """The explicit artist, else the first artist tag; empty when none is present."""
    if isinstance(song.get('artist'), str) and song['artist'].strip():
        return song['artist'].strip()
    for metadata in [song.get('metadata', {}), *song.get('stream_metadata', [])]:
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                if isinstance(key, str) and key.casefold() == 'artist' and isinstance(value, str) and value.strip():
                    return value.strip()
    return ''


def probe_song(path, filename=None):
    """Full decode and probe shared by the importer and HTTP uploads."""
    path = Path(path)
    filename = filename or path.name
    if Path(filename).name != filename or Path(filename).suffix.lower() not in ('.mp3', '.wav'):
        raise ValueError('expected an MP3 or WAV filename')
    subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-protocol_whitelist', 'file,pipe',
                    '-f', Path(filename).suffix.lower()[1:], '-i', str(path), '-map', '0:a', '-f', 'null', '-'],
                   check=True, capture_output=True, timeout=1800)
    probe = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                     '-f', Path(filename).suffix.lower()[1:], '-show_format', '-show_streams', '-of', 'json', str(path)],
                    check=True, capture_output=True, timeout=30).stdout)
    streams = [stream.get('tags', {}) for stream in probe.get('streams', []) if stream.get('codec_type') == 'audio']
    if not streams:
        raise ValueError('file has no audio stream')
    with path.open('rb') as audio:
        digest = hashlib.file_digest(audio, 'sha256').hexdigest()
    identity = hashlib.sha256(filename.encode() + b'\0' + bytes.fromhex(digest)).hexdigest()
    tags = probe['format'].get('tags', {})
    tags_lower = {k.lower(): v for k, v in tags.items()}
    song = dict(id=identity, path=identity + Path(filename).suffix.lower(), source_filename=filename,
                source_path=str(path.resolve()), source_sha256=digest, duration=float(probe['format']['duration']),
                title=tags_lower.get('title', '').strip() or Path(filename).stem,
                metadata=tags, stream_metadata=streams)
    checked_manifest(dict(version=1, songs=[song]), path.parent)
    return song


def copy_audio(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as output, Path(source).open('rb') as audio:
            shutil.copyfileobj(audio, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def import_library(source, destination):
    destination = Path(destination).expanduser()
    inputs = sorted((p for p in Path(source).iterdir() if p.is_file() and p.suffix.lower() in ('.mp3', '.wav')), key=lambda p: (p.stem.casefold(), p.name))
    if len(inputs) > LIMIT * 2:
        raise ValueError('too many inputs')
    good, errors = {}, []
    for path in inputs:
        try:
            song = probe_song(path)
            good.setdefault(path.stem, []).append((path, song))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            errors.append(dict(source_filename=path.name, error=str(exc)))
    selected = []
    for stem in sorted(good, key=lambda s: (s.casefold(), s)):
        path, song = min(good[stem], key=lambda item: (item[0].suffix.lower() != '.mp3', item[0].name))
        target = destination / song['path']
        copy_audio(path, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != song['source_sha256']:
            raise ValueError('copy hash mismatch')
        selected.append(song)
    if not selected or len(selected) > LIMIT:
        raise ValueError('empty or oversized library')
    manifest = dict(version=1, songs=selected)
    data = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    atomic(destination / 'manifest.json', data)
    return dict(inputs=len(inputs), songs=len(selected), errors=errors, manifest=str(destination / 'manifest.json'), sha256=hashlib.sha256(data).hexdigest())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--destination', type=Path, default=Path.home() / '.local/share/family-radio/library')
    args = parser.parse_args()
    print(json.dumps(import_library(args.source, args.destination), indent=2, ensure_ascii=False))
