#!/usr/bin/env python3
"""Prepare optional local artwork and real audio-level history. Standard library only."""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
import urllib.request
from urllib.parse import urlsplit
try:
    from host.library import atomic, load as load_library
except ModuleNotFoundError:
    from library import atomic, load as load_library

SIZE = 112
ART_BYTES = SIZE * SIZE * 2
HZ = 10
MAX_SECONDS = 3600
CHUNK = 512
ACK_TIMEOUT = 0.5
LEVEL_LEAD_SECONDS = 0.0  # Calibrate only from measured audible/display timing.


def level_history(levels, position, playing):
    """Twelve past RMS frames, oldest first. Silence and paused output rest."""
    if not playing or not math.isfinite(position) or position < 0:
        return bytes(12)
    frame = int(max(0, position + LEVEL_LEAD_SECONDS) * HZ)
    return bytes(levels[i] if 0 <= i < len(levels) else 0 for i in range(frame - 11, frame + 1))


def rms_levels(pcm):
    """8kHz signed little-endian stereo, 100ms windows; -48..0 dBFS mapped to bytes."""
    values = array.array('h', pcm[:len(pcm) // 4 * 4])
    if sys.byteorder != 'little':
        values.byteswap()
    result = bytearray()
    for start in range(0, len(values), 1600):
        frame = values[start:start + 1600]
        rms = math.sqrt(sum(v * v for v in frame) / len(frame)) / 32768
        result.append(round(max(0, min(1, (20 * math.log10(max(rms, 1e-9)) + 48) / 48)) * 255))
    return bytes(result)


def checked_file(root, name, maximum):
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError('invalid visual basename')
    path = root / name
    if path.resolve().parent != root.resolve() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('invalid visual file')
    return path.read_bytes()


def read_entries(manifest):
    """Raise on any unreadable manifest; callers decide whether to fall back."""
    manifest = Path(manifest)
    data = json.loads(checked_file(manifest.resolve().parent, manifest.name, 1024 * 1024))
    entries = data['songs']
    if data['version'] != 1 or data.get('size') != SIZE or data.get('hz') != HZ or not isinstance(entries, list) or len(entries) > 1000:
        raise ValueError('invalid visual manifest')
    result = {s['id']: s for s in entries}
    if len(result) != len(entries):
        raise ValueError('duplicate visual ID')
    return result


class Visuals:
    """One-song cache. Optional bad/missing visuals never interrupt music."""
    def __init__(self, manifest):
        self.root = Path(manifest).resolve().parent
        self.entries = {}
        self.loaded = None
        self.asset = (b'', b'', '-')
        try:
            self.entries = read_entries(manifest)
        except (OSError, ValueError, TypeError, KeyError):
            self.entries = {}

    def get(self, song):
        identity = (song['id'], song.get('source_sha256'))
        if identity == self.loaded:
            return self.asset
        self.loaded, self.asset = identity, (b'', b'', '-')
        try:
            entry = self.entries[song['id']]
            if entry['audio_sha256'] != song.get('source_sha256'):
                raise ValueError('stale audio analysis')
            art = checked_file(self.root, entry['art'], ART_BYTES)
            levels = checked_file(self.root, entry['levels'], MAX_SECONDS * HZ)
            if len(art) != ART_BYTES or hashlib.sha256(art).hexdigest() != entry['art_sha256'] or hashlib.sha256(levels).hexdigest() != entry['levels_sha256']:
                raise ValueError('visual checksum mismatch')
            self.asset = art, levels, f'{zlib.crc32(art):08x}'
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return self.asset


class SpotifyVisuals:
    """One image worker; no source audio is fetched or analyzed."""
    def __init__(self, fetch=None, now=time.monotonic):
        self.fetch = fetch or self._fetch
        self.now = now
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='spotify-cover')
        self.future, self.pending = None, None
        self.cache = OrderedDict()

    @staticmethod
    def _fetch(url):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'i.scdn.co' or parsed.fragment:
            raise ValueError('unsupported provider artwork URL')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        with urllib.request.build_opener(NoRedirect).open(url, timeout=10) as response:
            if response.headers.get_content_type() not in ('image/jpeg', 'image/png', 'image/webp'):
                raise ValueError('unsupported provider artwork type')
            image = response.read(2 * 1024 * 1024 + 1)
        if len(image) > 2 * 1024 * 1024:
            raise ValueError('provider artwork exceeds size limit')
        art = subprocess.run(['ffmpeg', '-v', 'error', '-max_alloc', '16777216', '-threads', '1',
                '-protocol_whitelist', 'pipe', '-i', 'pipe:0', '-frames:v', '1', '-vf',
                f'scale={SIZE}:{SIZE}:force_original_aspect_ratio=decrease,pad={SIZE}:{SIZE}:(ow-iw)/2:(oh-ih)/2',
                '-f', 'rawvideo', '-pix_fmt', 'rgb565be', 'pipe:1'], input=image,
                capture_output=True, check=True, timeout=15).stdout
        if len(art) != ART_BYTES:
            raise ValueError('invalid provider artwork dimensions')
        return art

    def get(self, song):
        identity = (song['id'], song.get('album', {}).get('image_url'))
        if self.future and self.future.done():
            try:
                art = self.future.result()
                self.cache[self.pending] = (None, (art, b'', f'{zlib.crc32(art):08x}'))
            except (OSError, ValueError, subprocess.SubprocessError):
                self.cache[self.pending] = (self.now() + 30, (b'', b'', '-'))
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
            self.future, self.pending = None, None
        if identity in self.cache:
            expires, asset = self.cache.pop(identity)
            if expires is None or self.now() < expires:
                self.cache[identity] = (expires, asset)
                return asset
        if identity[1] and not self.future:
            self.pending = identity
            self.future = self.executor.submit(self.fetch, identity[1])
        return b'', b'', '-'

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


def prepare_song(song, source, image, destination):
    """Prepare one local song without replacing the caller's visual manifest."""
    source, image, destination = Path(source), Path(image), Path(destination)
    checked_file(image.parent, image.name, 16 * 1024 * 1024)
    destination.mkdir(parents=True, exist_ok=True)
    if song['duration'] > MAX_SECONDS:
        raise ValueError('visual analysis limited to one-hour songs')
    with (source / song['path']).open('rb') as audio_file:
        if hashlib.file_digest(audio_file, 'sha256').hexdigest() != song['source_sha256']:
            raise ValueError('audio changed since library import')
    art = subprocess.run(['ffmpeg', '-v', 'error', '-max_alloc', '67108864', '-threads', '1', '-protocol_whitelist', 'file,pipe', '-i', str(image), '-frames:v', '1', '-vf', f'scale={SIZE}:{SIZE}', '-f', 'rawvideo', '-pix_fmt', 'rgb565be', 'pipe:1'], check=True, capture_output=True, timeout=30).stdout
    if len(art) != ART_BYTES:
        raise ValueError('invalid resized artwork')
    # Offline decode goes to a temporary file, never retained in RAM in full.
    with tempfile.TemporaryDirectory(prefix='.family-visuals-', dir=destination) as temp:
        pcm = Path(temp) / 'audio.pcm'
        subprocess.run(['ffmpeg', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-i', str(source / song['path']), '-t', str(MAX_SECONDS), '-ac', '2', '-ar', '8000', '-f', 's16le', str(pcm)], check=True, capture_output=True, timeout=1800)
        levels = bytearray()
        with pcm.open('rb') as f:
            while block := f.read(32000):
                levels.extend(rms_levels(block))
    if not levels or len(levels) > MAX_SECONDS * HZ:
        raise ValueError('invalid audio envelope')
    entry = dict(id=song['id'], audio_sha256=song['source_sha256'], art=hashlib.sha256(art).hexdigest()+'.rgb565', levels=song['source_sha256']+'.levels', art_sha256=hashlib.sha256(art).hexdigest(), levels_sha256=hashlib.sha256(levels).hexdigest())
    atomic(destination / entry['art'], art)
    atomic(destination / entry['levels'], bytes(levels))
    return entry


def prepare(library_path, artwork_path, destination):
    library = load_library(library_path)
    source = Path(library_path).resolve().parent
    artwork_path, destination = Path(artwork_path), Path(destination)
    artwork = json.loads(artwork_path.read_text())
    covers = {c['song_id']: c for c in artwork['covers']}
    destination.mkdir(parents=True, exist_ok=True)
    entries, skipped = [], []
    for song in library['songs']:
        try:
            cover = covers[song['id']]
            image = artwork_path.parent / cover['master']
            checked_file(artwork_path.parent, cover['master'], 16 * 1024 * 1024)
            entry = prepare_song(song, source, image, destination)
            entries.append(entry)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            skipped.append(dict(id=song['id'], reason=str(exc)))
    atomic(destination / 'manifest.json', (json.dumps(dict(version=1, size=SIZE, hz=HZ, songs=entries), indent=2)+'\n').encode())
    return dict(prepared=len(entries), skipped=skipped)


class ArtTransfer:
    """One in-flight chunk. Progress ACKs bound data across display blackouts."""
    def __init__(self):
        self.identity = None
        self.offset = 0
        self.attempt = 0
        self.retry_at = 0
        self.sent_end = 0
        self.chunk_retries = 0
        self.active = False

    @staticmethod
    def item(controller):
        return controller.item_token if hasattr(controller, "item_token") else controller.current

    def sync(self, controller):
        art, _, key = controller.asset()
        identity = (controller.session, self.item(controller), key)
        if identity == self.identity:
            return False
        self.identity, self.offset, self.attempt = identity, 0, 0
        self.retry_at = self.sent_end = self.chunk_retries = 0
        self.active = bool(controller.session and art)
        return True

    def acknowledge(self, record, controller):
        fields = record.split('\t')
        identity = [controller.session, str(self.item(controller)), controller.asset()[2]]
        if (not self.active or self.identity != tuple([identity[0], self.item(controller), identity[2]]) or
                len(fields) not in (5, 6) or fields[0] != 'B8' or fields[2:5] != identity):
            return
        if len(fields) == 5 and fields[1] == 'C' and self.sent_end == len(controller.asset()[0]):
            self.active = False
        elif len(fields) == 6 and fields[1] == 'O' and fields[5].isdigit():
            offset = int(fields[5])
            if offset == self.sent_end and self.sent_end > self.offset:
                self.offset = offset
                self.retry_at = self.chunk_retries = 0
            elif offset == 0 and self.sent_end == len(controller.asset()[0]):
                if self.attempt:
                    self.active = False
                else:
                    self.offset = self.sent_end = self.retry_at = self.chunk_retries = 0
                    self.attempt = 1

    def record(self, controller, now):
        self.sync(controller)
        art, _, key = controller.asset()
        if not self.active or now < self.retry_at:
            return None
        if self.retry_at:
            if self.chunk_retries >= 2:
                self.active = False
                return None
            self.chunk_retries += 1
        elif self.offset == len(art):
            return None
        data = art[self.offset:self.offset + CHUNK]
        record = f'B8\tA\t{controller.session}\t{self.item(controller)}\t{key}\t{self.offset}\t{data.hex()}\n'.encode()
        self.sent_end = self.offset + len(data)
        self.retry_at = now + ACK_TIMEOUT
        assert len(record) <= 1153
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('library', type=Path)
    parser.add_argument('artwork', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    report = prepare(args.library, args.artwork, args.destination)
    print(json.dumps(report, indent=2))
    return bool(report['skipped'])


if __name__ == '__main__':
    raise SystemExit(main())
