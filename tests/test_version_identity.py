"""The maintained product release has one validated identity and generation path."""
import ast
import copy
import json
from pathlib import Path
import tempfile
import unittest

try:
    from tooling.version import generated_source, load_identity, previous_fixture, pe_resource, validate_identity
except ImportError:
    raise unittest.SkipTest('Release tooling is absent from scaffold wheel')

ROOT = Path(__file__).resolve().parents[1]


class VersionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.identity = load_identity()
        self.raw = {key: value for key, value in self.identity.items() if key != 'windows_version'}

    def test_identity_maps_unambiguously_to_four_pe_components(self):
        self.assertEqual(self.identity['windows_version'], self.identity['version'] + '.' + str(self.identity['release_number']))
        namespace = {}
        source = generated_source(self.identity)
        tree = ast.parse(source)
        self.assertTrue(all(isinstance(node, (ast.Expr, ast.Assign)) for node in tree.body))
        exec(compile(tree, '<generated-version>', 'exec'), namespace)
        self.assertEqual(namespace['APP_VERSION'], self.identity['version'])
        self.assertEqual(namespace['APP_WINDOWS_VERSION'], self.identity['windows_version'])
        self.assertEqual(namespace['APP_PRODUCT_ID'], self.identity['product_id'])
        self.assertEqual(source, generated_source(load_identity()))

    def test_version_schema_rejects_ambiguous_unsafe_or_unrepresentable_values(self):
        cases = [('version', '1.2'), ('version', '1.2.3.4'), ('version', '01.2.3'),
                 ('version', '1.2.3-beta'), ('version', '65536.0.0'),
                 ('version', "1.2.3'; raise Exception()"), ('version', 123),
                 ('release_number', True), ('release_number', 0), ('release_number', 65536),
                 ('product_id', 'other'), ('channel', 'nightly'),
                 ('build_date', '2026-99-99'), ('build_date', '20261018'), ('schema_version', True)]
        for field, value in cases:
            record = copy.deepcopy(self.raw)
            record[field] = value
            with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                validate_identity(record)
        with self.assertRaises(ValueError):
            validate_identity(dict(self.raw, unknown=True))

    def test_version_file_rejects_duplicates_symlinks_and_large_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'version.json'
            path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                load_identity(path)
            path.write_text(' ' * 4097)
            with self.assertRaisesRegex(ValueError, 'small regular'):
                load_identity(path)
            path.write_text(json.dumps(self.raw))
            link = root / 'linked.json'
            try:
                link.symlink_to(path)
            except OSError:
                return
            with self.assertRaises(ValueError):
                load_identity(link)

    def test_ci_fixture_is_explicit_and_previous_without_mutating_product(self):
        before = (ROOT / 'version.json').read_bytes()
        fixture = validate_identity(previous_fixture(self.identity))
        self.assertEqual(fixture['release_number'], self.identity['release_number'] - 1)
        self.assertLess(tuple(map(int, fixture['version'].split('.'))),
                           tuple(map(int, self.identity['version'].split('.'))))
        self.assertEqual(before, (ROOT / 'version.json').read_bytes())

    def test_pe_resource_uses_same_numeric_and_display_versions(self):
        resource = pe_resource(self.identity)
        ast.parse(resource)
        numbers = tuple(map(int, self.identity['windows_version'].split('.')))
        self.assertIn('filevers=' + repr(numbers), resource)
        self.assertIn('prodvers=' + repr(numbers), resource)
        self.assertIn(repr(self.identity['version']), resource)

    @unittest.skipUnless((ROOT / "packaging/windows/build.py").is_file(), "Application packaging absent from scaffold wheel fixture")
    def test_build_and_installer_require_generated_identity_and_exact_repair(self):
        build = (ROOT / 'packaging/windows/build.py').read_text()
        recipe = (ROOT / 'packaging/windows/cachecad.iss').read_text()
        self.assertIn('load_identity(args.version_file)', build)
        self.assertIn("pe_file_version(installer) != expected_pe", build)
        for required in ('AppVersion={#ProductVersion}', 'VersionInfoVersion={#WindowsVersion}',
                         'PayloadIdentity', 'CACHECAD_REPAIR_REFUSED', 'CACHECAD_DOWNGRADE_REFUSED',
                         'ComparePackedVersion', 'ExistingRelease > {#ReleaseNumber}',
                         'function InitializeSetup', 'function InitializeUninstall',
                         'function PrepareToInstall', 'ReleaseInstallGate', 'AppMutex={#RunningMutex}'):
            self.assertIn(required, recipe)
        self.assertNotIn('TerminateProcess', recipe)
        self.assertNotIn('taskkill', recipe)

    def test_workflow_gates_upload_on_real_upgrade_and_dpapi_preservation(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('Optional PyYAML parser not installed')
        workflow = yaml.safe_load((ROOT / '.github/workflows/linux-ci.yml').read_text())
        steps = workflow['jobs']['windows-installer']['steps']
        build_next = next(i for i, step in enumerate(steps) if '--output dist/windows-previous' in step.get('run', ''))
        upgrade = next(i for i, step in enumerate(steps) if 'python packaging/windows/test_upgrade.py' in step.get('run', ''))
        upload = next(i for i, step in enumerate(steps) if 'dist/windows/installer/*.exe' in step.get('with', {}).get('path', ''))
        self.assertLess(build_next, upgrade)
        self.assertLess(upgrade, upload)
        self.assertNotIn('continue-on-error', steps[upgrade])
        self.assertIn('dist/windows/upgrade-results/*.json', steps[upload]['with']['path'])


if __name__ == '__main__':
    unittest.main()
