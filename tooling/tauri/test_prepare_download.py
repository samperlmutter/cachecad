import hashlib
from pathlib import Path
import tempfile
import unittest

from prepare_download import PLATFORMS, prepare


class DownloadTests(unittest.TestCase):
    def test_flat_platform_specific_download_preserves_installer(self):
        for platform, (label, _, suffix) in PLATFORMS.items():
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bundle = root / 'bundle' / 'nested'
                bundle.mkdir(parents=True)
                original = bundle / ('old-setup.exe' if platform == 'windows-x64' else 'old.dmg')
                original.write_bytes(b'actual installer bytes\x00\xff')
                output = root / 'download'
                artifact = prepare(root / 'bundle', output, platform, {'version': '2.0.0-alpha.1'})
                name = f'Permafrost-2.0.0-alpha.1-{label}-{suffix}'
                self.assertEqual(artifact, f'Permafrost-2.0.0-alpha.1-{label}-Installer')
                self.assertEqual(sorted(p.name for p in output.iterdir()), sorted([name, 'START-HERE.txt', 'SHA256SUMS.txt']))
                self.assertEqual((output / name).read_bytes(), original.read_bytes())
                self.assertEqual((output / 'SHA256SUMS.txt').read_text().strip(),
                                 f'{hashlib.sha256(original.read_bytes()).hexdigest()}  {name}')
                self.assertIn(name, (output / 'START-HERE.txt').read_text())
                self.assertTrue(original.exists(), 'Updater assets must retain their original names')

    def test_ambiguous_or_missing_installer_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / 'bundle'
            bundle.mkdir()
            for files in ([], ['one-setup.exe', 'two-setup.exe']):
                for name in files:
                    (bundle / name).touch()
                with self.assertRaisesRegex(RuntimeError, 'Expected one'):
                    prepare(bundle, root / 'download', 'windows-x64', {'version': '2.0.0'})
                self.assertFalse((root / 'download').exists())

    def test_unsafe_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'safe application version'):
                prepare(Path(tmp), Path(tmp) / 'download', 'windows-x64', {'version': '../bad'})


if __name__ == '__main__':
    unittest.main()
