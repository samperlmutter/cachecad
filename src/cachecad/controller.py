"""Headless reconstructed sync workflow with offline adapters and explicit state."""
from threading import Event
from .models import ActionKind as K, ResultStatus as S, SyncAction, ActionResult, ControllerState
from .services import ConflictError
from .signals import SignalManager
from .safety.onedrive_guard import assess_project_path

class AppController:
    def __init__(self,local,remote,*,allow_deletes=False,baseline=None):
        self.local,self.remote=local,remote
        self.allow_deletes=allow_deletes
        self.baseline=dict(baseline or {})
        self.signals=SignalManager()
        self.state=ControllerState(project_directory=str(local.root))
        self._cancelled=Event();self._generation=0
    def _changed(self):self.signals.state_changed.emit(self.state)
    def select_project_directory(self,path):
        advisory=assess_project_path(path)
        if advisory.warning:self.state.warnings.append(advisory.warning)
        self._changed()
        # Selection advice does not relocate/open projects or move files.
        return advisory.warning
    def cancel(self):self._cancelled.set()
    def analyze(self):
        if self.state.phase=='executing':raise RuntimeError('Cannot analyze during execution')
        self.state.phase='analyzing';self.state.actions=[];self.state.results={}
        self._cancelled.clear();self._generation+=1;self._changed()
        local,remote=self.local.scan(),self.remote.scan()
        if not local.complete or local.errors or not remote.complete or remote.errors:
            self.state.phase='blocked';self.state.warnings.extend(local.errors+remote.errors)
            self.signals.error_occurred.emit('Incomplete scan; sync planning blocked');self._changed();return []
        for path in sorted(local.files.keys()|remote.files.keys()|self.baseline.keys()):
            l,r=local.files.get(path),remote.files.get(path);old=self.baseline.get(path)
            if l and r and l.digest==r.digest:
                self.baseline[path]=(l.digest,r.digest);continue
            kind=None
            if not old:
                kind=K.UPLOAD_NEW if l and not r else K.DOWNLOAD_NEW if r and not l else K.CONFLICT
                if not l and not r:continue
                reason='New file' if kind!=K.CONFLICT else 'Both copies differ without a shared baseline'
            elif l and r:
                if l.digest==old[0] and r.digest!=old[1]:kind=K.DOWNLOAD_REVISION
                elif r.digest==old[1] and l.digest!=old[0]:kind=K.UPLOAD_REVISION
                else:kind=K.CONFLICT
                reason='Changed since last shared baseline'
            elif l:
                kind=K.DELETE_LOCAL if l.digest==old[0] else K.CONFLICT
                reason='Remote missing from complete scan'
            elif r:
                kind=K.DELETE_CLOUD if r.digest==old[1] else K.CONFLICT
                reason='Local missing from complete scan'
            else:
                self.baseline.pop(path,None);continue
            self.state.actions.append(SyncAction(f'{self._generation}:{len(self.state.actions)}',path,kind,reason,l,r))
        self.state.phase='ready';self._changed();return list(self.state.actions)
    def _execute_one(self,action):
        if self._cancelled.is_set():return S.CANCELLED,'Cancelled before action'
        if action.kind==K.CONFLICT:return S.CONFLICT,action.reason
        if action.kind in (K.DELETE_LOCAL,K.DELETE_CLOUD) and not self.allow_deletes:
            return S.CONFLICT,'Destructive operations are disabled'
        lscan,rscan=self.local.scan(),self.remote.scan()
        if not lscan.complete or lscan.errors or not rscan.complete or rscan.errors:
            return S.CONFLICT,'Scan became incomplete'
        if lscan.files.get(action.path)!=action.expected_local or rscan.files.get(action.path)!=action.expected_remote:
            return S.CONFLICT,'File changed after analysis; reanalyze'
        if self._cancelled.is_set():return S.CANCELLED,'Cancelled before commit'
        if action.kind in (K.UPLOAD_NEW,K.UPLOAD_REVISION):
            staged=self.local.read_staged(action.path,action.expected_local)
            result=self.remote.put(action.path,staged,action.expected_remote)
            if result.digest!=action.expected_local.digest:raise OSError('Provider digest mismatch')
            self.baseline[action.path]=(result.digest,result.digest)
        elif action.kind in (K.DOWNLOAD_NEW,K.DOWNLOAD_REVISION):
            staged=self.remote.read(action.path,action.expected_remote)
            result=self.local.put(action.path,staged,action.expected_local)
            if result.digest!=action.expected_remote.digest:raise OSError('Local digest mismatch')
            self.baseline[action.path]=(result.digest,result.digest)
        elif action.kind==K.DELETE_CLOUD:
            if self.remote.delete(action.path,action.expected_remote) is not True:raise OSError('Cloud deletion failed')
            self.baseline.pop(action.path,None)
        elif action.kind==K.DELETE_LOCAL:
            if self.local.delete(action.path,action.expected_local) is not True:raise OSError('Local deletion failed')
            self.baseline.pop(action.path,None)
        else:return S.CONFLICT,'Unsupported action'
        return S.SUCCESS,''
    def execute(self):
        if self.state.phase!='ready':raise RuntimeError('Analyze successfully before executing')
        self.state.phase='executing';self._changed()
        for action in tuple(self.state.actions):
            try:status,detail=self._execute_one(action)
            except ConflictError as exc:status,detail=S.CONFLICT,str(exc)
            except Exception as exc:status,detail=S.FAILED,f'{type(exc).__name__}: {exc}'
            result=ActionResult(action.action_id,status,detail)
            self.state.results[action.action_id]=result
            self.signals.action_result.emit(result);self._changed()
        self.state.phase='finished';self._changed();self.signals.sync_execution_finished.emit()
        return list(self.state.results.values())
