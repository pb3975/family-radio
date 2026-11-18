import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


CHECKER = Path(__file__).resolve().parents[1] / 'tools' / 'check_public_tree.py'


class PublicTreeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.nested = self.root / 'nested'
        self.nested.mkdir()

    def run_check(self):
        return subprocess.run(
            [sys.executable, str(CHECKER), str(self.root)],
            capture_output=True,
            text=True,
        )

    def test_clean_public_text_and_git_metadata(self):
        (self.nested / 'README.md').write_text('Open http://127.0.0.1:8080 on your computer.')
        metadata = self.root / '.git'
        metadata.mkdir()
        (metadata / 'AgEnTs.Md').touch()
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_nested_instruction_filenames_are_rejected_in_mixed_case(self):
        (self.nested / 'AgEnTs.Md').touch()
        (self.nested / 'cLaUdE.mD').touch()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        for name in ('AgEnTs.Md', 'cLaUdE.mD'):
            self.assertIn(f'nested/{name}: private instruction filename', result.stderr)

    @unittest.skipUnless(os.name == 'posix', 'Requires symbolic link support')
    def test_symbolic_link_is_rejected(self):
        link = self.nested / 'link'
        link.symlink_to(self.root / 'missing')
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn('nested/link: symbolic link', result.stderr)

    def test_personal_paths_email_and_private_hosts_are_rejected(self):
        (self.nested / 'notes.txt').write_text('\n'.join((
            'demo@' + 'example.com',
            '/home' + '/example/notes',
            'example' + '.internal',
        )))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        for issue in ('email address', 'personal home path', 'private host address'):
            self.assertIn(f'nested/notes.txt: {issue}', result.stderr)


if __name__ == '__main__':
    unittest.main()
