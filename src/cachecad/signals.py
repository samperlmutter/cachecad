"""Headless event boundary, replaceable with queued Qt signals later."""
from threading import RLock

class Signal:
    def __init__(self):
        self._callbacks=[]
        self._lock=RLock()
        self.errors=[]
    def connect(self,callback):
        with self._lock:
            if callback not in self._callbacks:self._callbacks.append(callback)
    def disconnect(self,callback):
        with self._lock:self._callbacks.remove(callback)
    def emit(self,*args):
        with self._lock:callbacks=tuple(self._callbacks)
        for callback in callbacks:
            try:callback(*args)
            except Exception as exc:self.errors.append(type(exc).__name__)

class SignalManager:
    def __init__(self):
        self.state_changed=Signal()
        self.action_result=Signal()
        self.sync_execution_finished=Signal()
        self.error_occurred=Signal()
