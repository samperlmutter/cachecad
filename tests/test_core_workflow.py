import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from cachecad.controller import AppController
from cachecad.models import ActionKind as K, ResultStatus as S
from cachecad.services import LocalWorkspace,MemoryDrive,digest,ConflictError
from cachecad.__main__ import run_demo

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.local=LocalWorkspace(self.root);self.remote=MemoryDrive();self.app=AppController(self.local,self.remote)
    def tearDown(self):self.temp.cleanup()
    def write(self,path,data):
        p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    def test_upload_and_repeat_analysis(self):
        self.write('part',b'a');self.assertEqual(self.app.analyze()[0].kind,K.UPLOAD_NEW)
        self.assertEqual(self.app.execute()[0].status,S.SUCCESS);self.assertEqual(self.app.analyze(),[])
    def test_download(self):
        self.remote.put('part',b'a',None);self.app.analyze();self.app.execute();self.assertEqual((self.root/'part').read_bytes(),b'a')
    def test_stale_local_blocks_upload(self):
        self.write('part',b'a');self.app.analyze();self.write('part',b'b')
        self.assertEqual(self.app.execute()[0].status,S.CONFLICT);self.assertIsNone(self.remote.snapshot('part'))
    def test_stale_remote_blocks_download(self):
        expected=self.remote.put('part',b'a',None);self.app.analyze();self.remote.put('part',b'b',expected)
        self.assertEqual(self.app.execute()[0].status,S.CONFLICT);self.assertFalse((self.root/'part').exists())
    def test_partial_scan_no_actions(self):
        self.write('part',b'a');self.remote.scan_complete=False
        self.assertEqual(self.app.analyze(),[]);self.assertEqual(self.app.state.phase,'blocked')
        with self.assertRaises(RuntimeError):self.app.execute()
    def test_both_changed_conflict(self):
        self.write('part',b'a');self.remote.put('part',b'b',None)
        self.assertEqual(self.app.analyze()[0].kind,K.CONFLICT)
        self.assertEqual(self.app.execute()[0].status,S.CONFLICT)
    def test_revision_and_backup(self):
        self.write('part',b'a');snap=self.remote.put('part',b'a',None);self.app.analyze()
        self.remote.put('part',b'b',snap);self.assertEqual(self.app.analyze()[0].kind,K.DOWNLOAD_REVISION)
        self.app.execute();self.assertEqual((self.root/'part').read_bytes(),b'b')
        self.assertEqual((self.root/'.cachecad-reconstruction/backups'/digest(b'a')).read_bytes(),b'a')
    def test_delete_disabled_by_default(self):
        self.write('part',b'a');self.remote.put('part',b'a',None);self.app.analyze();(self.root/'part').unlink()
        self.app.analyze();self.assertEqual(self.app.execute()[0].status,S.CONFLICT)
        self.assertIsNotNone(self.remote.snapshot('part'))
    def test_delete_failure_does_not_clear_baseline(self):
        self.write('part',b'a');self.remote.put('part',b'a',None);self.app.analyze();(self.root/'part').unlink()
        self.app.allow_deletes=True;self.remote.fail_paths.add('part');self.app.analyze()
        self.assertEqual(self.app.execute()[0].status,S.FAILED);self.assertIn('part',self.app.baseline)
    def test_cancel_all_terminal_once(self):
        self.write('a',b'a');self.write('b',b'b');self.app.analyze();self.app.cancel();seen=[]
        self.app.signals.action_result.connect(seen.append);results=self.app.execute()
        self.assertEqual(len(seen),2);self.assertTrue(all(r.status==S.CANCELLED for r in results))
    def test_signal_failure_does_not_stop_work(self):
        self.write('a',b'a');self.app.analyze();self.app.signals.action_result.connect(lambda r:1/0)
        self.assertEqual(self.app.execute()[0].status,S.SUCCESS);self.assertEqual(self.app.state.phase,'finished')
    def test_provider_exception_terminal(self):
        self.write('a',b'a');self.app.analyze();self.remote.fail_paths.add('a')
        self.assertEqual(self.app.execute()[0].status,S.FAILED)
    def test_remote_cas_rejects_intervening_write(self):
        self.write('a',b'a');self.app.analyze();original=self.remote.put
        def raced(path,data,expected):
            original(path,b'other',None);return original(path,data,expected)
        with patch.object(self.remote,'put',side_effect=raced):
            self.assertEqual(self.app.execute()[0].status,S.CONFLICT)
        snap=self.remote.snapshot('a');self.assertEqual(self.remote.read('a',snap),b'other')
    def test_symlink_blocks_scan(self):
        (self.root/'bad').symlink_to('/tmp');self.assertEqual(self.app.analyze(),[])
        self.assertEqual(self.app.state.phase,'blocked')
    def test_parent_escape_rejected(self):
        with self.assertRaises(ValueError):self.local.snapshot('../outside')
    def test_demo(self):
        d=run_demo();self.assertFalse(d['network_access']);self.assertEqual(d['second_analysis_actions'],0)

    def test_reserved_namespace_rejected_by_both_adapters(self):
        for path in ['.cachecad-reconstruction/x','.CACHECAD-RECONSTRUCTION/x']:
            with self.assertRaises(ValueError):self.remote.put(path,b'x',None)
            with self.assertRaises(ValueError):self.local.snapshot(path)
    def test_internal_symlink_blocks_scan_and_backup(self):
        self.write('part',b'a');expected=self.local.snapshot('part')
        with tempfile.TemporaryDirectory() as other:
            (self.root/'.cachecad-reconstruction').symlink_to(other,target_is_directory=True)
            self.assertFalse(self.local.scan().complete)
            with self.assertRaises(ValueError):self.local.put('part',b'b',expected)
            self.assertEqual((self.root/'part').read_bytes(),b'a')
            self.assertEqual(list(Path(other).iterdir()),[])
    def test_corrupt_existing_backup_blocks_replacement(self):
        self.write('part',b'a');expected=self.local.snapshot('part')
        self.write('.cachecad-reconstruction/backups/'+expected.digest,b'corrupt')
        with self.assertRaises(ConflictError):self.local.put('part',b'b',expected)
        self.assertEqual((self.root/'part').read_bytes(),b'a')
    def test_failed_new_publish_leaves_no_partial_destination(self):
        with patch('cachecad.services.os.link',side_effect=OSError('publish failed')):
            with self.assertRaises(OSError):self.local.put('part',b'a',None)
        self.assertFalse((self.root/'part').exists())
        self.assertEqual(list(self.root.glob('.staged-*')),[])
