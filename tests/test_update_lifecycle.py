"""Actual update coordinator with disposable, non-executing handoff doubles."""
import importlib.util
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from recovered_harness import load_function, deny_external_effects

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'maintained/gui/update_install_coordinator.py').is_file():
    raise unittest.SkipTest('Actual update source absent')
try:
    from PySide6.QtCore import QCoreApplication
except ImportError:
    QCoreApplication = None


class UpdateBusyTests(unittest.TestCase):
    def test_transfer_scan_account_and_pool_work_each_defer(self):
        pools = SimpleNamespace(thread_pool=SimpleNamespace(activeThreadCount=lambda: 0),
                                network_pool=SimpleNamespace(activeThreadCount=lambda: 0))
        method = load_function('maintained/app/app_controller.py', 'AppController._update_busy_reason', {})
        method.__globals__['__builtins__']['__import__'] = lambda name,*a,**k: pools if name == 'core.thread_manager' else None
        controller = SimpleNamespace(sync_executing=False, _executor_unsettled=False,
             sync_analyzer=SimpleNamespace(_settled=True, is_background_running=lambda: False),
             auth_manager=SimpleNamespace(_auth_in_progress=False, _thread=None))
        with deny_external_effects():
            self.assertIsNone(method(controller))
            controller.sync_executing=True;self.assertIn('transfer',method(controller));controller.sync_executing=False
            controller._executor_unsettled=True;self.assertIn('transfer',method(controller));controller._executor_unsettled=False
            controller.sync_analyzer._settled=False;self.assertIn('scan',method(controller));controller.sync_analyzer._settled=True
            controller.auth_manager._auth_in_progress=True;self.assertIn('account',method(controller));controller.auth_manager._auth_in_progress=False
            pools.network_pool.activeThreadCount=lambda: 1;self.assertIn('Background',method(controller))


@unittest.skipUnless(QCoreApplication, 'Requires pinned Qt runtime')
class UpdateCoordinatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])
        spec=importlib.util.spec_from_file_location('update_coordinator_test', ROOT/'maintained/gui/update_install_coordinator.py')
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        cls.Coordinator=module.UpdateInstallCoordinator

    def pump(self, predicate):
        deadline=time.monotonic()+3
        while not predicate() and time.monotonic()<deadline:
            self.app.processEvents();time.sleep(.005)
        self.assertTrue(predicate())

    def test_idle_consent_commits_once_then_closes_parent_handles(self):
        handle=Mock();coordinator=self.Coordinator(lambda cancel: handle,lambda: None)
        ready=[];coordinator.ready_to_exit.connect(lambda:ready.append(True))
        self.assertTrue(coordinator.start());self.assertFalse(coordinator.start())
        self.pump(lambda: bool(ready));handle.commit.assert_not_called();handle.abort.assert_not_called()
        self.assertTrue(coordinator.shutdown());handle.commit.assert_not_called()
        self.assertTrue(coordinator.commit_after_clean_shutdown());handle.commit.assert_called_once();handle.close.assert_called_once()
        self.assertFalse(coordinator.commit_after_clean_shutdown())

    def test_new_work_during_preparation_aborts_without_exit(self):
        started=threading.Event();release=threading.Event();handle=Mock();reason=[None]
        def prepare(cancel):started.set();release.wait(2);return handle
        coordinator=self.Coordinator(prepare,lambda:reason[0]);failed=[];ready=[]
        coordinator.failed.connect(failed.append);coordinator.ready_to_exit.connect(lambda:ready.append(True))
        coordinator.start();self.assertTrue(started.wait(1));reason[0]='A scan started';release.set()
        self.pump(lambda:bool(failed));handle.abort.assert_called_once();handle.commit.assert_not_called();self.assertFalse(ready)
        coordinator.shutdown()

    def test_close_cancels_unarmed_helper_and_never_commits(self):
        handle=Mock();started=threading.Event()
        def prepare(cancel):started.set();cancel.wait(2);return handle
        coordinator=self.Coordinator(prepare,lambda:None);coordinator.start();self.assertTrue(started.wait(1))
        self.assertTrue(coordinator.shutdown());handle.abort.assert_called_once();handle.commit.assert_not_called()

    def test_preparation_failure_leaves_retry_available(self):
        calls=[0];handle=Mock()
        def prepare(cancel):
            calls[0]+=1
            if calls[0]==1:raise OSError('fixture')
            return handle
        coordinator=self.Coordinator(prepare,lambda:None);failed=[];ready=[]
        coordinator.failed.connect(failed.append);coordinator.ready_to_exit.connect(lambda:ready.append(True))
        coordinator.start();self.pump(lambda:bool(failed));self.assertTrue(coordinator.start())
        self.pump(lambda:bool(ready));handle.commit.assert_not_called();coordinator.shutdown();coordinator.abort_pending_install();handle.abort.assert_called_once()
