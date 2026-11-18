import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('library', Path(__file__).resolve().parents[1] / 'host/library.py')
library = importlib.util.module_from_spec(spec)
spec.loader.exec_module(library)


class LibraryTests(unittest.TestCase):
    def test_atomic_replace(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'state.json'
            library.atomic(path, b'first')
            library.atomic(path, b'second')
            self.assertEqual(path.read_bytes(), b'second')
            self.assertEqual(list(Path(root).iterdir()), [path])

    def test_reject_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'manifest.json'
            library.atomic(path, b'{"version":1,"songs":[{"id":"a","path":"../outside"}]}')
            with self.assertRaises(ValueError):
                library.load(path)

    def test_optional_artist_override_validation_preserves_existing_manifest(self):
        song=dict(id='a'*64,path='a.wav',title='Synthetic',duration=1,source_filename='a.wav',source_sha256='b'*64,metadata={'artist':'Other'})
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'manifest.json'
            for artist in ('Demo Artist',None,'','x'*4097):
                data=dict(version=1,songs=[dict(song,artist=artist)])
                path.write_text(json.dumps(data))
                if artist=='Demo Artist':self.assertEqual(library.load(path)['songs'][0]['artist'],'Demo Artist')
                else:
                    with self.assertRaises(ValueError):library.load(path)
            path.write_text(json.dumps(dict(version=1,songs=[song])))
            self.assertEqual(library.load(path)['songs'][0]['metadata']['artist'],'Other')

    def test_library_size_boundary_accepts_1000_and_rejects_empty_or_1001(self):
        song=dict(title='Synthetic',duration=1,source_filename='synthetic.wav',source_sha256='b'*64,metadata={})
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'manifest.json'
            for count in (0,1000,1001):
                songs=[dict(song,id=f'{i:064x}',path=f'{i}.wav') for i in range(count)]
                path.write_text(json.dumps(dict(version=1,songs=songs)))
                if count==1000:self.assertEqual(len(library.load(path)['songs']),1000)
                else:
                    with self.assertRaises(ValueError):library.load(path)
