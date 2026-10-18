import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from cachecad.safety.sync_safety import *

class ScanTests(unittest.TestCase):
    def test_partial_listing_blocks_deletion(self):
        with self.assertRaises(IncompleteScan):
            deletion_candidates({'part'}, {'part'}, RemoteSnapshot(frozenset(),False))
    def test_error_overrides_complete_claim(self):
        with self.assertRaises(IncompleteScan):
            deletion_candidates({'part'}, {'part'}, RemoteSnapshot(frozenset(),True,('page failed',)))
    def test_complete_listing_only_deletes_logged_unchanged_missing(self):
        self.assertEqual(deletion_candidates({'a','b','c'}, {'a','b','new'},RemoteSnapshot(frozenset({'b'}),True)), {'a'})

class ExecutionTests(unittest.TestCase):
    def test_none_result_becomes_failure(self):
        l=Ledger(['a']); r=l.run('a',lambda:None)
        self.assertEqual(r.status,Status.FAILED);self.assertTrue(l.complete)
    def test_exception_becomes_failure(self):
        l=Ledger(['a']);r=l.run('a',Mock(side_effect=OSError('disk full')))
        self.assertEqual(r.status,Status.FAILED);self.assertTrue(l.complete)
    def test_cancelled_terminal(self):
        l=Ledger(['a']);r=l.run('a',Mock(side_effect=Cancelled('cancel')))
        self.assertEqual(r.status,Status.CANCELLED);self.assertTrue(l.complete)
    def test_existing_destination_terminal_conflict(self):
        transfer=Mock();l=Ledger(['a']);r=l.run('a',lambda:download_new(lambda:True,transfer))
        self.assertEqual(r.status,Status.CONFLICT);self.assertTrue(l.complete);transfer.assert_not_called()
    def test_cloud_failure_never_journaled(self):
        for response in [False,None]:
            journal=Mock();l=Ledger(['a']);r=l.run('a',lambda:delete_cloud(lambda:response,journal))
            self.assertEqual(r.status,Status.FAILED);self.assertTrue(l.complete);journal.assert_not_called()
    def test_cloud_exception_terminal_and_unjournaled(self):
        journal=Mock();l=Ledger(['a']);r=l.run('a',lambda:delete_cloud(Mock(side_effect=OSError()),journal))
        self.assertEqual(r.status,Status.FAILED);self.assertTrue(l.complete);journal.assert_not_called()
    def test_cloud_success_journaled_once(self):
        journal=Mock();l=Ledger(['a']);r=l.run('a',lambda:delete_cloud(lambda:True,journal))
        self.assertEqual(r.status,Status.SUCCESS);journal.assert_called_once_with()
    def test_duplicate_claim_does_not_execute_twice(self):
        work=Mock(return_value=Status.SUCCESS);l=Ledger(['a']);l.run('a',work)
        with self.assertRaises(ValueError):l.run('a',work)
        work.assert_called_once_with()
    def test_unknown_id(self):
        with self.assertRaises(KeyError):Ledger([]).run('x',lambda:Status.SUCCESS)
    def test_duplicate_manifest(self):
        with self.assertRaises(ValueError):Ledger(['a','a'])
    def test_parallel_accounting(self):
        l=Ledger(map(str,range(100)))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i:l.run(str(i),lambda:Status.SUCCESS),range(100)))
        self.assertTrue(l.complete);self.assertEqual(len(l.results),100)
    def test_pending_not_complete(self):
        l=Ledger(['a','b']);l.run('a',lambda:Status.SUCCESS);self.assertFalse(l.complete)
    def test_journal_error_is_terminal_failure(self):
        l=Ledger(['a']);r=l.run('a',lambda:delete_cloud(lambda:True,Mock(side_effect=OSError())))
        self.assertEqual(r.status,Status.FAILED);self.assertTrue(l.complete)

if __name__=='__main__':unittest.main()

class CollectorTests(unittest.TestCase):
    def test_pending_root_not_complete(self):
        self.assertFalse(ScanCollector(['root']).snapshot().complete)
    def test_pagination_incomplete_until_last_page(self):
        c=ScanCollector(['root']);c.page_succeeded('root',['a'],['page2'])
        self.assertFalse(c.snapshot().complete)
        c.page_succeeded('page2',['b']);self.assertTrue(c.snapshot().complete)
        self.assertEqual(c.snapshot().paths,{'a','b'})
    def test_failed_page_never_authorizes_deletes(self):
        c=ScanCollector(['root']);c.page_succeeded('root',['a'],['page2']);c.page_failed('page2','network')
        with self.assertRaises(IncompleteScan):deletion_candidates({'b'},{'b'},c.snapshot())
    def test_unknown_page_rejected(self):
        with self.assertRaises(ValueError):ScanCollector(['root']).page_succeeded('unknown',[])
    def test_empty_root_set_rejected(self):
        with self.assertRaises(ValueError):ScanCollector([])
