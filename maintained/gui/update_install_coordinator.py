"""Keep a consented update handoff off Qt's thread and arm only deliberate exit."""
import threading
from PySide6.QtCore import QObject, QTimer, Signal


class UpdateInstallCoordinator(QObject):
    failed = Signal(str)
    ready_to_exit = Signal()

    def __init__(self, prepare, can_install, parent=None):
        super().__init__(parent)
        self.prepare = prepare
        self.can_install = can_install
        self._thread = None
        self._cancel = threading.Event()
        self._result = None
        self._error = False
        self._armed = False
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._poll)

    @property
    def install_pending(self):
        return self._armed

    @property
    def busy(self):
        return self._thread is not None

    def start(self):
        if self._closed or self.busy:
            return False
        reason = self.can_install()
        if reason:
            self.failed.emit(reason)
            return False
        self._cancel.clear()
        self._result = None
        self._error = False
        def work():
            try:
                self._result = self.prepare(self._cancel)
            except Exception:
                self._error = True
        self._thread = threading.Thread(target=work, name='cachecad-update-handoff', daemon=False)
        self._thread.start()
        self._timer.start()
        return True

    def _poll(self):
        if self._thread is None or self._thread.is_alive():
            return
        self._thread.join()
        self._thread = None
        self._timer.stop()
        handle, self._result = self._result, None
        if self._closed or self._cancel.is_set() or self._error or handle is None:
            if handle is not None:
                handle.abort()
            if not self._closed:
                self.failed.emit('Update preparation did not complete. The installed application was not changed.')
            return
        reason = self.can_install()
        if reason:
            handle.abort()
            self.failed.emit(reason)
            return
        self._armed = True
        self._handoff = handle
        # READY requests orderly shutdown only. COMMIT is sent by the verified
        # launcher after all workers, databases and diagnostics have stopped.
        self.ready_to_exit.emit()

    def commit_after_clean_shutdown(self):
        if not self._armed:
            return False
        handle, self._handoff = self._handoff, None
        self._armed = False
        try:
            handle.commit()
        except Exception:
            handle.abort()
            return False
        finally:
            handle.close()
        return True

    def abort_pending_install(self):
        if self._armed:
            handle, self._handoff = self._handoff, None
            self._armed = False
            handle.abort()

    def shutdown(self, timeout_ms=2000):
        self._closed = True
        self._timer.stop()
        if self._armed:
            # Preserve the uncommitted pipe until launcher confirms clean exit.
            return True
        self._cancel.set()
        if self._thread is not None:
            self._thread.join(max(0, min(timeout_ms, 5000)) / 1000)
            if self._thread.is_alive():
                return False
            self._thread = None
        if self._result is not None:
            self._result.abort()
            self._result = None
        return True
