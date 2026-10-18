"""Fail on high-confidence credential patterns; never print matched values."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(r'AIza[0-9A-Za-z_-]{35}'),
    re.compile(r'GOCSPX-[0-9A-Za-z_-]{20,}'),
    re.compile(r'gh[pousr]_[0-9A-Za-z]{30,}'),
    re.compile(r'github_pat_[0-9A-Za-z_]{40,}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
]


class CredentialHygieneTests(unittest.TestCase):
    def test_recovered_sources_reports_do_not_embed_credentials(self):
        if not (ROOT / 'recovered').is_dir():
            self.skipTest('Recovery source is not included in replacement wheel fixture')
        hits = []
        paths = [path for directory in ('recovered', 'maintained', 'development')
                 for path in (ROOT / directory).rglob('*')]
        for path in paths:
            if path.is_file() and path.suffix in {'.py', '.json', '.md'}:
                text = path.read_text(encoding='utf-8')
                if any(pattern.search(text) for pattern in PATTERNS):
                    hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [], 'Credential-pattern hits; filenames only, values suppressed')

    def test_private_key_marker_is_detected(self):
        marker = '-----BEGIN ' + 'PRIVATE KEY-----'
        self.assertTrue(any(pattern.search(marker) for pattern in PATTERNS))
