"""Real disposable-disk recovery journal tests; no account or application startup."""
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import concurrent.futures
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'maintained/core/transfer_journal.py'
if not SOURCE.is_file():
    raise unittest.SkipTest('Maintained application absent from prototype wheel')
SPEC = importlib.util.spec_from_file_location('transfer_journal_test', SOURCE)
journal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(journal)


def md5(data):
    return hashlib.md5(data).hexdigest()


class DurableTransferJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cachecad-durable-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.root = self.folder / 'project'
        self.root.mkdir()
        self.path = self.root / 'part.sldprt'
        self.old = b'original approved CAD bytes'
        self.new = b'new approved CAD revision'
        self.path.write_bytes(self.old)
        self.incoming = self.folder / 'download.bin'
        self.incoming.write_bytes(self.new)
        info = self.path.stat()
        self.local = {'exists': True, 'identity': list(journal._identity(info)),
                      'md5': md5(self.old), 'size': len(self.old), 'modified_time': info.st_mtime}
        self.remote = {'file_id': 'file-id', 'name': self.path.name,
                       'parents': ['project-id'], 'version': '23',
                       'md5': md5(self.new), 'size': len(self.new)}
        self.store = journal.TransferJournal(self.root, 'project-id')

    def prepare(self, **kwargs):
        args = {'action_kind': 'download_revision', 'path': self.path.name,
                'approved_local': self.local, 'approved_remote': self.remote}
        args.update(kwargs)
        return self.store.prepare_intent(**args)

    def preserve(self, operation):
        operation.preserve_local_backup(self.path, md5=md5(self.old), size=len(self.old))
        operation.preserve_incoming(self.incoming, md5=md5(self.new), size=len(self.new))
        operation.record_phase('prepared')
        return operation

    def test_intent_owns_immutable_copies_and_identity(self):
        operation = self.prepare(operation_id='known-operation')
        self.local['md5'] = '0' * 32
        exposed = operation.intent
        exposed['path'] = 'other.sldprt'
        record = self.store.read(operation.operation_id)
        self.assertEqual(record['intent']['path'], self.path.name)
        self.assertEqual(record['intent']['approved_local']['md5'], md5(self.old))
        self.assertEqual(record['state']['outcome'], 'not_started')
        self.assertEqual(record['state']['phase'], 'intent')
        self.assertIn('fsync', record['intent']['durability'])

    def test_operation_id_is_never_reused_or_clobbered(self):
        first = self.prepare(operation_id='fixed')
        original = (first.directory / 'intent.json').read_bytes()
        with self.assertRaises(FileExistsError):
            self.prepare(operation_id='fixed')
        self.assertEqual((first.directory / 'intent.json').read_bytes(), original)

    def test_local_revision_copies_are_verified_and_independent(self):
        operation = self.preserve(self.prepare())
        self.assertTrue(operation.verify_recovery_material())
        self.assertEqual(operation.backup_path.read_bytes(), self.old)
        self.assertEqual(operation.incoming_path.read_bytes(), self.new)
        self.assertNotEqual(operation.incoming_path.stat().st_ino, operation.publication_path.stat().st_ino)
        self.path.write_bytes(b'edited original')
        self.incoming.write_bytes(b'changed download temp')
        self.assertEqual(operation.backup_path.read_bytes(), self.old)
        self.assertEqual(operation.incoming_path.read_bytes(), self.new)

    def test_existing_incoming_publication_hardlink_alias_is_rejected_before_mutation(self):
        operation = self.preserve(self.prepare())
        operation.incoming_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        operation.incoming_path.unlink()
        os.link(operation.publication_path, operation.incoming_path)
        self.assertEqual(operation.incoming_path.read_bytes(), self.new)
        with self.assertRaises(journal.JournalError):
            operation.verify_recovery_material()
        operation.record_outcome('unknown', phase='mutation_started')
        with self.assertRaises(journal.JournalError):
            operation.publish_incoming()
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assertEqual(operation.backup_path.read_bytes(), self.old)

    def test_backup_hardlinked_to_live_original_is_rejected_before_mutation(self):
        operation = self.preserve(self.prepare())
        operation.backup_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        operation.backup_path.unlink()
        os.link(self.path, operation.backup_path)
        self.assertEqual(operation.backup_path.read_bytes(), self.old)
        with self.assertRaises(journal.JournalError):
            operation.verify_recovery_material()
        self.assertEqual(self.path.read_bytes(), self.old)

    def test_publication_hardlinked_to_unrelated_file_is_rejected_before_mutation(self):
        operation = self.preserve(self.prepare())
        unrelated = self.folder / 'unrelated-publication-copy'
        os.link(operation.publication_path, unrelated)
        with self.assertRaises(journal.JournalError):
            operation.verify_recovery_material()
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assertEqual(unrelated.read_bytes(), self.new)

    def test_identical_bytes_in_independent_old_new_files_are_valid(self):
        self.new = self.old
        self.incoming.write_bytes(self.new)
        self.remote.update(md5=md5(self.new), size=len(self.new))
        operation = self.preserve(self.prepare())
        self.assertTrue(operation.verify_recovery_material())
        identities = {(path.stat().st_dev, path.stat().st_ino) for path in (
            operation.backup_path, operation.incoming_path, operation.publication_path)}
        self.assertEqual(len(identities), 3)

    def test_failure_preparing_backup_preserves_original_and_partial_evidence(self):
        operation = self.prepare()
        with patch.object(journal.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError):
                operation.preserve_local_backup(self.path, md5=md5(self.old), size=len(self.old))
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assertTrue(operation.backup_path.exists())
        self.assertEqual(self.store.read(operation.operation_id)['state']['outcome'], 'not_started')
        with self.assertRaises(journal.JournalError):
            operation.verify_recovery_material()

    def test_publication_copy_prepared_before_destructive_step(self):
        operation = self.prepare()
        operation.preserve_local_backup(self.path, md5=md5(self.old), size=len(self.old))
        original = journal._copy_verified
        def fail_publication(source, target, *args, **kwargs):
            if target.name == 'publication.bin':
                raise OSError(errno.ENOSPC, 'disk full')
            return original(source, target, *args, **kwargs)
        with patch.object(journal, '_copy_verified', side_effect=fail_publication):
            with self.assertRaises(OSError):
                operation.preserve_incoming(self.incoming, md5=md5(self.new), size=len(self.new))
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assertEqual(operation.backup_path.read_bytes(), self.old)
        self.assertEqual(operation.incoming_path.read_bytes(), self.new)
        with self.assertRaises(journal.JournalError):
            operation.verify_recovery_material()

    def test_changed_source_and_approval_override_are_rejected(self):
        operation = self.prepare()
        self.path.write_bytes(b'user changed original')
        with self.assertRaises(journal.JournalError):
            operation.preserve_local_backup(self.path, md5=md5(self.old), size=len(self.old))
        with self.assertRaises(journal.JournalError):
            operation.preserve_incoming(self.incoming, md5='0' * 32, size=len(self.new))
        self.assertFalse(operation.incoming_path.exists())

    def test_incoming_checksum_mismatch_never_publishes(self):
        operation = self.prepare()
        self.incoming.write_bytes(b'wrong server bytes')
        with self.assertRaises(journal.JournalError):
            operation.preserve_incoming(self.incoming, md5=md5(self.new), size=len(self.new))
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assertFalse(operation.publication_path.exists())

    def test_upload_recovery_uses_local_payload_and_needs_no_publication_copy(self):
        operation = self.prepare(action_kind='upload_revision')
        operation.preserve_incoming(self.path, md5=md5(self.old), size=len(self.old))
        self.assertTrue(operation.verify_recovery_material())
        self.assertEqual(operation.incoming_path.read_bytes(), self.old)
        self.assertFalse(operation.publication_path.exists())
        self.assertFalse(operation.incoming_path.stat().st_mode & stat.S_IWUSR)

    def test_local_deletion_recovery_requires_only_backup(self):
        operation = self.prepare(action_kind='delete_local', approved_remote=None)
        operation.preserve_local_backup(self.path, md5=md5(self.old), size=len(self.old))
        self.assertTrue(operation.verify_recovery_material())
        self.assertFalse(operation.incoming_path.exists())
        self.assertFalse(operation.backup_path.stat().st_mode & stat.S_IWUSR)

    def test_selected_payload_fallback_is_immutable_and_only_for_new_files(self):
        payload = {'md5': md5(self.new), 'size': len(self.new)}
        operation = self.prepare(action_kind='download_new', approved_local=None,
                                 approved_remote=None, expected_payload=payload)
        payload['md5'] = '0' * 32
        operation.preserve_incoming(self.incoming, md5=md5(self.new), size=len(self.new))
        self.assertTrue(operation.verify_recovery_material())
        self.assertTrue(operation.publication_path.stat().st_mode & stat.S_IWUSR)
        with self.assertRaises(journal.JournalError):
            self.prepare(approved_local=None, approved_remote=None, expected_payload=payload)

    def test_parallel_threads_serialize_independent_journal_instances(self):
        def prepare(index):
            store = journal.TransferJournal(self.root, 'project-id')
            return store.prepare_intent(action_kind='upload_new', path=f'part-{index}.step',
                                        expected_payload={'md5': md5(b'x'), 'size': 1})
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            operations = list(pool.map(prepare, range(12)))
        self.assertEqual(len(operations), 12)
        self.assertEqual(len(self.store.list_pending()), 12)

    def test_publication_requires_durable_started_state(self):
        operation = self.preserve(self.prepare())
        self.path.unlink()
        with self.assertRaises(journal.JournalError):
            operation.publish_incoming()
        self.assertFalse(self.path.exists())

    def test_success_preserves_independent_incoming_and_backup_after_user_edit(self):
        operation = self.preserve(self.prepare())
        operation.record_outcome('unknown', phase='mutation_started')
        self.path.unlink()  # A disposable fixture stands in for a completed Trash move.
        operation.publish_incoming()
        operation.record_outcome('committed', phase='published')
        self.assertEqual(self.path.read_bytes(), self.new)
        self.path.write_bytes(b'user edited published file')
        self.assertEqual(operation.incoming_path.read_bytes(), self.new)
        self.assertEqual(operation.backup_path.read_bytes(), self.old)
        operation.record_outcome('committed', phase='completed')
        self.assertEqual(self.store.list_pending(), ())

    def test_competing_destination_is_never_overwritten(self):
        operation = self.preserve(self.prepare())
        operation.record_outcome('unknown', phase='mutation_started')
        self.path.unlink()
        self.path.write_bytes(b'competing CAD save')
        with self.assertRaises(FileExistsError):
            operation.publish_incoming()
        self.assertEqual(self.path.read_bytes(), b'competing CAD save')
        self.assertEqual(operation.backup_path.read_bytes(), self.old)
        self.assertEqual(operation.incoming_path.read_bytes(), self.new)
        self.assertEqual(self.store.list_pending()[0]['state']['outcome'], 'unknown')

    def test_link_failure_retains_both_recovery_versions(self):
        operation = self.preserve(self.prepare())
        operation.record_outcome('unknown', phase='mutation_started')
        self.path.unlink()
        with patch.object(journal.os, 'link', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError):
                operation.publish_incoming()
        self.assertFalse(self.path.exists())
        self.assertEqual(operation.backup_path.read_bytes(), self.old)
        self.assertEqual(operation.incoming_path.read_bytes(), self.new)

    def test_postpublication_flush_error_reports_committed_fact(self):
        operation = self.preserve(self.prepare())
        operation.record_outcome('unknown', phase='mutation_started')
        self.path.unlink()
        with patch.object(journal, 'fsync_directory', side_effect=OSError(errno.EIO, 'flush failed')):
            with self.assertRaises(journal.PublicationCommittedError) as raised:
                operation.publish_incoming()
        self.assertTrue(raised.exception.committed)
        self.assertEqual(self.path.read_bytes(), self.new)
        self.assertEqual(operation.incoming_path.read_bytes(), self.new)

    def test_atomic_record_file_fsync_failure_preserves_previous_valid_state(self):
        operation = self.prepare()
        prior = self.store.read(operation.operation_id)
        with patch.object(journal.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError):
                operation.record_outcome('unknown', phase='mutation_started')
        self.assertEqual(self.store.read(operation.operation_id), prior)
        self.assertTrue(list(operation.directory.glob('.write-*')))
        self.assertEqual(self.path.read_bytes(), self.old)

    def test_atomic_record_replace_failure_keeps_old_valid_json(self):
        operation = self.prepare()
        with patch.object(journal.os, 'replace', side_effect=OSError(errno.EACCES, 'cannot replace')):
            with self.assertRaises(OSError):
                operation.record_outcome('unknown', phase='mutation_started')
        self.assertEqual(self.store.read(operation.operation_id)['state']['outcome'], 'not_started')

    def test_postreplace_directory_flush_failure_keeps_conservative_unknown_record(self):
        operation = self.prepare()
        with patch.object(journal, 'fsync_directory', side_effect=OSError(errno.EIO, 'flush failed')):
            with self.assertRaises(OSError):
                operation.record_outcome('unknown', phase='mutation_started')
        self.assertEqual(self.store.read(operation.operation_id)['state']['outcome'], 'unknown')
        self.assertEqual(self.path.read_bytes(), self.old)
        with self.assertRaises(journal.PendingTransferRecovery):
            self.prepare()

    def test_committed_recording_failure_is_pending_and_cannot_downgrade(self):
        operation = self.prepare()
        operation.record_outcome('unknown', phase='mutation_started')
        operation.record_outcome('committed', phase='published')
        operation.record_outcome('committed_recording_failed', error='sync log unavailable')
        self.assertEqual(self.store.list_pending()[0]['state']['outcome'], 'committed_recording_failed')
        with self.assertRaises(journal.JournalError):
            operation.record_outcome('unknown')
        with self.assertRaises(journal.PendingTransferRecovery):
            self.prepare()

    def test_unknown_operation_blocks_restart_retry_but_not_started_does_not(self):
        first = self.prepare()
        second = self.prepare()
        first.record_outcome('unknown', phase='mutation_started')
        with self.assertRaises(journal.PendingTransferRecovery):
            second.record_outcome('unknown', phase='mutation_started')
        reopened = journal.TransferJournal(self.root, 'project-id', create=False)
        with self.assertRaises(journal.PendingTransferRecovery):
            reopened.prepare_intent(action_kind='download_revision', path=self.path.name,
                approved_local=self.local, approved_remote=self.remote)
        with self.assertRaises(journal.JournalError):
            first.record_outcome('not_started')

    def test_pending_target_match_is_case_insensitive(self):
        operation = self.prepare()
        operation.record_outcome('unknown', phase='mutation_started')
        with self.assertRaises(journal.PendingTransferRecovery):
            self.prepare(path=self.path.name.upper())

    def test_other_path_can_prepare_while_one_operation_needs_recovery(self):
        operation = self.prepare()
        operation.record_outcome('unknown', phase='mutation_started')
        other = self.prepare(path='other.sldprt')
        self.assertEqual(other.intent['path'], 'other.sldprt')

    def test_readonly_discovery_never_creates_recovery_directory(self):
        other = self.folder / 'unseen'
        other.mkdir()
        store = journal.TransferJournal(other, 'project-id', create=False)
        self.assertEqual(store.list_pending(), ())
        self.assertEqual(list(other.iterdir()), [])

    def test_foreign_project_or_mutated_intent_is_rejected(self):
        operation = self.prepare()
        other = journal.TransferJournal(self.root, 'different-project-id', create=False)
        with self.assertRaises(journal.JournalError):
            other.list_pending()
        path = operation.directory / 'intent.json'
        value = json.loads(path.read_text())
        value['path'] = 'other.sldprt'
        path.write_text(json.dumps(value))
        with self.assertRaises(journal.JournalError):
            self.store.read(operation.operation_id)

    def test_truncated_duplicate_and_forged_history_records_fail_closed(self):
        operation = self.prepare()
        path = operation.directory / 'state.json'
        original = path.read_bytes()
        for bad in (b'{', b'{"schema_version":1,"schema_version":1}', b'{"bad":NaN}'):
            with self.subTest(bad=bad):
                path.write_bytes(bad)
                with self.assertRaises(journal.JournalError):
                    self.store.list_pending()
        path.write_bytes(original)
        data = json.loads(original)
        data['outcome'] = 'committed'
        data['phase'] = 'completed'
        path.write_text(json.dumps(data))
        with self.assertRaises(journal.JournalError):
            self.store.list_pending()

    def test_path_and_operation_identifier_escapes_are_rejected(self):
        for path in ('../escape', '/absolute', '.cachecad/recovery/evil', '.CACHECAD/x',
                     'C:\\escape', 'nested/../escape', 'name:stream', '.', 'nested//file',
                     '.cachecad./recovery', 'part.step ', 'NUL', 'part\x00.step'):
            with self.subTest(path=path), self.assertRaises(journal.JournalError):
                self.prepare(path=path)
        for identifier in ('../outside', '/tmp/x', '', '.hidden', 'x' * 97):
            with self.subTest(identifier=identifier), self.assertRaises(journal.JournalError):
                self.prepare(operation_id=identifier)

    def test_symlinked_recovery_component_and_material_are_rejected(self):
        operation = self.preserve(self.prepare())
        outside = self.folder / 'outside'
        outside.mkdir()
        link = self.root / 'linked'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('OS does not permit fixture symlinks')
        with self.assertRaises(journal.JournalError):
            journal.TransferJournal(link, 'project-id')
        operation.incoming_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        operation.incoming_path.unlink()
        operation.incoming_path.symlink_to(self.incoming)
        with self.assertRaises(journal.JournalError):
            operation.verify_recovery_material()
        with self.assertRaises(journal.JournalError):
            self.store.list_pending()

    def test_reparse_point_attribute_is_rejected_even_without_symlink(self):
        actual = self.root.lstat()
        original = Path.lstat
        fake = SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=0x400)
        def lstat(path, *args, **kwargs):
            return fake if path == self.root else original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', lstat):
            with self.assertRaises(journal.JournalError):
                journal.TransferJournal(self.root, 'project-id')

    def test_second_store_cannot_write_while_process_lock_is_held(self):
        other = journal.TransferJournal(self.root, 'project-id')
        with self.store._exclusive():
            with self.assertRaises(OSError):
                other.prepare_intent(action_kind='upload_new', path='new.step',
                                     expected_payload={'md5': md5(b'x'), 'size': 1})
        self.assertEqual(self.store.list_pending(), ())

    def test_lock_hardlink_cannot_modify_an_unrelated_file(self):
        unrelated = self.folder / 'unrelated'
        unrelated.write_bytes(b'untouched')
        os.link(unrelated, self.store.directory / '.journal.lock')
        with self.assertRaises(journal.JournalError):
            self.prepare()
        self.assertEqual(unrelated.read_bytes(), b'untouched')

    def test_malformed_partial_operation_never_disappears_from_pending_check(self):
        incomplete = self.store.directory / 'crashed-before-intent'
        incomplete.mkdir()
        with self.assertRaises(journal.JournalError):
            self.store.list_pending()
        with self.assertRaises(journal.JournalError):
            self.prepare()
        self.assertTrue(incomplete.exists())

    def test_completed_evidence_is_retained_without_automatic_pruning(self):
        operation = self.preserve(self.prepare())
        operation.record_outcome('unknown', phase='mutation_started')
        operation.record_outcome('committed', phase='completed')
        self.assertEqual(self.store.list_pending(), ())
        reopened = journal.TransferJournal(self.root, 'project-id')
        self.assertEqual(reopened.read(operation.operation_id)['state']['phase'], 'completed')
        self.assertEqual(operation.backup_path.read_bytes(), self.old)
        self.assertEqual(operation.incoming_path.read_bytes(), self.new)

    def test_process_exit_after_destructive_step_retains_recovery_and_retry_block(self):
        operation = self.preserve(self.prepare())
        script = '''import importlib.util, os, sys
spec = importlib.util.spec_from_file_location('recovery', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
store = module.TransferJournal(sys.argv[2], 'project-id', create=False)
operation = store.open_operation(sys.argv[3])
operation.record_outcome('unknown', phase='mutation_started')
os.unlink(sys.argv[4])
os._exit(23)
'''
        result = subprocess.run([sys.executable, '-c', script, str(SOURCE), str(self.root),
                                 operation.operation_id, str(self.path)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 23, result.stderr.decode())
        reopened = journal.TransferJournal(self.root, 'project-id', create=False)
        self.assertFalse(self.path.exists())
        record = reopened.list_pending()[0]
        self.assertEqual(record['state']['outcome'], 'unknown')
        self.assertEqual(operation.backup_path.read_bytes(), self.old)
        self.assertEqual(operation.incoming_path.read_bytes(), self.new)
        with self.assertRaises(journal.PendingTransferRecovery):
            self.prepare()

    def test_process_exit_after_publication_keeps_unknown_instead_of_false_success(self):
        operation = self.preserve(self.prepare())
        script = '''import importlib.util, os, sys
spec = importlib.util.spec_from_file_location('recovery', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
store = module.TransferJournal(sys.argv[2], 'project-id', create=False)
operation = store.open_operation(sys.argv[3])
operation.record_outcome('unknown', phase='mutation_started')
os.unlink(sys.argv[4])
operation.publish_incoming()
os._exit(24)
'''
        result = subprocess.run([sys.executable, '-c', script, str(SOURCE), str(self.root),
                                 operation.operation_id, str(self.path)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 24, result.stderr.decode())
        self.assertEqual(self.path.read_bytes(), self.new)
        self.assertEqual(self.store.list_pending()[0]['state']['outcome'], 'unknown')
        self.assertEqual(operation.backup_path.read_bytes(), self.old)
        self.assertEqual(operation.incoming_path.read_bytes(), self.new)
        with self.assertRaises(journal.PendingTransferRecovery):
            self.prepare()


if __name__ == '__main__':
    unittest.main()
