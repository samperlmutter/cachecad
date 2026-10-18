"""Synthetic tests only: do not import recovered application code."""
import unittest
from dataclasses import replace

from cachecad.safety.overwrite_guard import (GuardConflict, Operation, Plan, Snapshot,
                             check_preconditions)


class OverwriteGuardTests(unittest.TestCase):
    def setUp(self):
        self.local = Snapshot(True, "local-identity", "mtime-ctime-1", "sha256:a", 8)
        self.remote = Snapshot(True, "drive-id", "version-1", "sha256:b", 9)
        self.absent = Snapshot(False)
        self.plan = Plan(Operation.UPLOAD_REVISION, self.local, self.remote, True)

    def check(self, plan=None, **kwargs):
        args = dict(local_now=self.local, remote_now=self.remote,
                    scan_complete_now=True)
        args.update(kwargs)
        check_preconditions(plan or self.plan, **args)

    def test_unchanged_revision_passes(self):
        self.check()

    def test_all_supported_shapes(self):
        shapes = [(Operation.UPLOAD_NEW, self.local, self.absent),
                  (Operation.DOWNLOAD_NEW, self.absent, self.remote),
                  (Operation.UPLOAD_REVISION, self.local, self.remote),
                  (Operation.DOWNLOAD_REVISION, self.local, self.remote),
                  (Operation.DELETE_LOCAL, self.local, self.absent),
                  (Operation.DELETE_REMOTE, self.absent, self.remote)]
        for op, local, remote in shapes:
            with self.subTest(op=op):
                self.check(Plan(op, local, remote, True),
                           local_now=local, remote_now=remote)

    def test_every_local_fingerprint_change_blocks(self):
        for field, value in [("object_id", "new"), ("generation", "new"),
                             ("digest", "new"), ("size", 10)]:
            with self.subTest(field=field), self.assertRaises(GuardConflict):
                self.check(local_now=replace(self.local, **{field: value}))

    def test_every_remote_fingerprint_change_blocks(self):
        for field, value in [("object_id", "new"), ("generation", "new"),
                             ("digest", "new"), ("size", 10)]:
            with self.subTest(field=field), self.assertRaises(GuardConflict):
                self.check(remote_now=replace(self.remote, **{field: value}))

    def test_local_disappeared(self):
        with self.assertRaises(GuardConflict):
            self.check(local_now=self.absent)

    def test_remote_disappeared(self):
        with self.assertRaises(GuardConflict):
            self.check(remote_now=self.absent)

    def test_download_new_destination_appeared(self):
        with self.assertRaises(GuardConflict):
            self.check(Plan(Operation.DOWNLOAD_NEW, self.absent, self.remote, True))

    def test_upload_new_destination_appeared(self):
        with self.assertRaises(GuardConflict):
            self.check(Plan(Operation.UPLOAD_NEW, self.local, self.absent, True))

    def test_incomplete_scan_at_either_time(self):
        for plan, now in [(replace(self.plan, scan_complete=False), True),
                          (self.plan, False)]:
            with self.subTest(plan=plan, now=now), self.assertRaises(GuardConflict):
                self.check(plan, scan_complete_now=now)

    def test_unknown_is_not_absence(self):
        with self.assertRaises(GuardConflict):
            self.check(remote_now=Snapshot(None))

    def test_missing_metadata_blocks_even_if_unchanged(self):
        for field in ("object_id", "generation", "digest", "size"):
            incomplete = replace(self.remote, **{field: None})
            with self.subTest(field=field), self.assertRaises(GuardConflict):
                self.check(replace(self.plan, remote=incomplete), remote_now=incomplete)

    def test_unknown_action_and_conflict_block(self):
        for plan in [replace(self.plan, operation="UPLOAD_UNKNOWN"),
                     replace(self.plan, unresolved_conflict=True)]:
            with self.subTest(plan=plan), self.assertRaises(GuardConflict):
                self.check(plan)

    def test_cancelled(self):
        with self.assertRaises(GuardConflict):
            self.check(cancelled=True)

    def test_new_action_cannot_overwrite_existing_file(self):
        with self.assertRaises(GuardConflict):
            self.check(replace(self.plan, operation=Operation.UPLOAD_NEW))

    def test_absent_cannot_reuse_old_metadata(self):
        with self.assertRaises(GuardConflict):
            Snapshot(False, object_id="old").validate()

    def test_empty_file_is_valid(self):
        replace(self.local, size=0).validate()


if __name__ == "__main__":
    unittest.main()
