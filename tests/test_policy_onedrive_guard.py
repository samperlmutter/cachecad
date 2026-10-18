import unittest
from cachecad.safety.onedrive_guard import assess_project_path


class OneDriveGuardTests(unittest.TestCase):
    ROOT = r'C:\Users\Sam\OneDrive'

    def check(self, path, status, **kwargs):
        result = assess_project_path(path, **kwargs)
        self.assertEqual(result.status, status)
        return result

    def test_root_itself(self):
        self.check(self.ROOT, 'configured_root_match', configured_roots=[self.ROOT])

    def test_case_and_slashes(self):
        self.check('c:/USERS/sam/ONEDRIVE/Projects/', 'configured_root_match',
                   configured_roots=[self.ROOT])

    def test_prefix_collision(self):
        self.check(self.ROOT + 'Backup\\Project', 'no_known_match', configured_roots=[self.ROOT])

    def test_parent_is_not_contained(self):
        self.check(r'C:\Users\Sam', 'no_known_match', configured_roots=[self.ROOT])

    def test_dot_segments_inside(self):
        self.check(self.ROOT + r'\a\..\Project', 'configured_root_match', configured_roots=[self.ROOT])

    def test_dot_segments_escape(self):
        self.check(self.ROOT + r'\..\Project', 'no_known_match', configured_roots=[self.ROOT])

    def test_custom_root(self):
        self.check(r'D:\TeamFiles\Job', 'configured_root_match', configured_roots=[r'D:\TeamFiles'])

    def test_multiple_accounts(self):
        result = self.check(r'D:\Work\Job', 'configured_root_match', env={
            'OneDriveConsumer': self.ROOT, 'ONEDRIVECOMMERCIAL': r'D:\Work'})
        self.assertEqual(result.sources, ('environment:ONEDRIVECOMMERCIAL',))

    def test_no_implicit_process_environment(self):
        self.check(r'D:\Projects', 'unknown')

    def test_name_is_only_hint(self):
        self.check(self.ROOT + r'\Project', 'name_hint')

    def test_business_name_hint(self):
        self.check(r'C:\OneDrive - Example\Project', 'name_hint')

    def test_substring_is_not_hint(self):
        self.check(r'C:\NotOneDrive\Project', 'unknown')

    def test_unc_boundary(self):
        self.check(r'\\server\share\Sync\Project', 'configured_root_match',
                   configured_roots=[r'\\server\share\Sync'])
        self.check(r'\\server\share\SyncCopy', 'no_known_match',
                   configured_roots=[r'\\server\share\Sync'])

    def test_extended_drive(self):
        self.check('\\\\?\\' + self.ROOT + '\\Job', 'configured_root_match', configured_roots=[self.ROOT])

    def test_extended_unc(self):
        self.check(r'\\?\UNC\server\share\Sync\Job', 'configured_root_match',
                   configured_roots=[r'\\server\share\Sync'])

    def test_unsupported_paths(self):
        for path in ['', 'relative', r'C:relative', r'\rooted', '/tmp/project',
                     r'\\.\C:\thing', r'\\?\Volume{123}\thing', r'C:\folder.\thing',
                     r'C:\folder \thing', r'C:\%missing%\thing', r'C:\job:stream',
                     'C:\\nul\x00', r'\\server', r'C:\wild*card']:
            with self.subTest(path=path):
                self.check(path, 'unknown', configured_roots=[self.ROOT])

    def test_invalid_discovery_does_not_claim_no_match(self):
        self.check(r'C:\Projects', 'unknown', configured_roots=[self.ROOT, 'relative'])

    def test_empty_env_ignored(self):
        self.check(r'C:\Projects', 'unknown', env={'OneDrive': ''})

    def test_most_specific_root(self):
        result = self.check(r'C:\Sync\Work\Job', 'configured_root_match',
                            configured_roots=[r'C:\Sync', r'C:\Sync\Work'])
        self.assertEqual(result.matched_root, r'c:\sync\work')

    def test_deduplicate_root_provenance(self):
        result = self.check(self.ROOT, 'configured_root_match', configured_roots=[self.ROOT],
                            env={'OneDrive': self.ROOT.lower()})
        self.assertEqual(result.sources, ('configured_root', 'environment:OneDrive'))

    def test_outside_known_roots_not_safety_claim(self):
        result = self.check(r'D:\Projects', 'no_known_match', configured_roots=[self.ROOT])
        self.assertIsNone(result.warning)
        self.assertTrue(result.limitations)

    def test_warning_non_destructive(self):
        result = self.check(self.ROOT, 'configured_root_match', configured_roots=[self.ROOT])
        self.assertIn('continue', result.warning)
        self.assertIn('No files have been moved', result.warning)


if __name__ == '__main__':
    unittest.main()
