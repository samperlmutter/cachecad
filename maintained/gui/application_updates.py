"""Explicit application-update consent UI with cancellable, queued workers.

The controller supplies real services. This module has no network endpoints,
credentials, mock fallback, installer launcher, or implicit startup check.

Integration contract:
* ``service.check(cancel_event)`` returns an immutable release offer or None.
* ``service.download(offer, cancel_event, progress)`` returns TUF/hash-verified
  bytes, not an executable authorization. ``service.configured`` is a bool.
* ``verify_download(download, cancel_event)`` runs on a worker and returns a
  Windows-verified object with an idempotent ``close()``. It must retain the
  protected installer handle, bind the offer, and fail closed. Native trust
  checks may finish after cancellation; such late handles are always closed.
* ``request_install(verified)`` runs on the GUI thread only after the explicit
  Install and close click. The controller rechecks/quiesces all application
  work, re-verifies at handoff, and returns a deferral string or None. None
  transfers ownership to that controller, which closes the token only once its
  verified helper owns the file. Exceptions leave ownership here for retry.

Cancel, Escape, Later and window close invalidate the attempt first. Workers
never access widgets, and their objects remain strongly referenced until they
finish. Bounded shutdown never kills a thread or mistakes cancellation for exit.
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)


_SAFE_ERRORS = {
    'not_configured': 'Application updates need the owner’s approved update configuration.',
    'cancelled': 'Update cancelled. You can try again.',
    'clock': 'Check the system date and time, then try updating again.',
    'network': 'The update service could not be reached. Check your connection and try again.',
    'timeout': 'The update request timed out. You can try again.',
    'busy': 'Another update operation is still running. Try again after it finishes.',
    'configuration': 'The owner update configuration could not be verified. Use an approved distributor.',
    'dependencies': 'Required update components are unavailable. Reinstall from an approved distributor.',
    'verification': 'The update could not be verified. No installation was started.',
    'incompatible': 'This update is incompatible with the installed application, Windows version or profile.',
    'rollback': 'An older release or older trust metadata was refused. No installation was started.',
    'changed': 'The available update changed. Check for updates again before downloading.',
    'storage': 'The update could not be saved. Check free disk space and update-cache access, then retry.',
}


def _close_verified(value):
    """Only call for a publisher-verification result; never execute download bytes."""
    if value is not None:
        try:
            value.close()
        except Exception:
            # Closing a best-effort cleanup handle must not expose backend data or
            # allow a stale callback to revive a dismissed dialog.
            pass


def _display(value, limit=1000):
    """Untrusted release text is bounded, plain text, with controls escaped."""
    text = str(value)[:limit]
    return ''.join(character if character.isprintable() else repr(character)[1:-1]
                   for character in text)


class _Events(QObject):
    completed = Signal(int)
    progress = Signal(int, object, object)


class _Task:
    """Python worker and result ownership independent of the dialog's Qt lifetime."""

    def __init__(self, generation, phase, operation, events):
        self.generation = generation
        self.phase = phase
        self.operation = operation
        self.events = events
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()
        self.result = None
        self.code = ''
        self.finished = False
        self.thread = threading.Thread(target=self.run, name='CacheCADUpdate-' + phase,
                                       daemon=True)

    def cancel(self):
        with self.lock:
            self.cancel_event.set()
            stale, self.result = self.result, None
        if self.phase == 'verifying':
            _close_verified(stale)

    def take(self):
        with self.lock:
            result, self.result = self.result, None
            return result, self.code

    def run(self):
        result, code = None, ''
        last_progress = 0.0

        def progress(received, total):
            nonlocal last_progress
            if self.cancel_event.is_set():
                return
            if type(received) is not int or type(total) is not int or total <= 0:
                return
            now = time.monotonic()
            if now - last_progress < 0.05 and received < total:
                return
            last_progress = now
            try:
                self.events.progress.emit(self.generation, received, total)
            except RuntimeError:
                pass  # Application shutdown may already have removed Qt objects.

        try:
            result = self.operation(self.cancel_event, progress)
        except Exception as error:
            candidate = getattr(error, 'code', '')
            code = candidate if isinstance(candidate, str) and candidate in _SAFE_ERRORS else 'failed'
        with self.lock:
            cancelled = self.cancel_event.is_set()
            if not cancelled:
                self.result = result
            self.code = 'cancelled' if cancelled else code
            self.finished = True
        if cancelled and self.phase == 'verifying':
            _close_verified(result)
        try:
            self.events.completed.emit(self.generation)
        except RuntimeError:
            pass


class _Lifetime:
    """Resource cleanup also survives a parent QObject being deleted directly."""

    def __init__(self):
        self.task = None
        self.verified = None

    def close(self):
        if self.task is not None:
            self.task.cancel()
        verified, self.verified = self.verified, None
        _close_verified(verified)


class ApplicationUpdateDialog(QDialog):
    """Manual Check → Download → Windows verification → explicit installation."""

    state_changed = Signal(str)
    attempt_finished = Signal(str)

    def __init__(self, service, *, verify_download=None, request_install=None,
                 installed_version='Unavailable', parent=None):
        super().__init__(parent)
        self._lifetime = _Lifetime()
        self.destroyed.connect(self._lifetime.close)
        self.setWindowTitle('Application updates')
        self.setMinimumWidth(520)
        self.setModal(False)
        # Do not use WA_DeleteOnClose: the controller retains this dialog until
        # shutdown() confirms all cooperative workers have really exited.
        self._service = service
        self._verify_download = verify_download
        self._request_install = request_install
        self._installed_version = _display(installed_version, 100)
        self._generation = 0
        self._task = None
        self._offer = None
        self._download = None
        self._verified = None
        self._closed = False
        self._installing = False
        self._handoff_failure = None
        self._state = 'idle'
        # No Qt parent: an in-flight Python task owns this signal bridge even if
        # the dialog's parent window is deleted. Receivers disconnect via Qt.
        self._events = _Events()
        self._events.completed.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        self._events.progress.connect(self._progress, Qt.ConnectionType.QueuedConnection)
        self._poll = QTimer(self)
        self._poll.setInterval(20)
        self._poll.timeout.connect(self._settle)
        self._reflowing = False
        self._label_measurements = {}
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._reflow)
        self._build_ui()
        self.resize(600, 300)
        if not self.configured:
            self._set_state('setup_required', _SAFE_ERRORS['not_configured'] +
                            ' No update server will be contacted.')
        else:
            self._set_state('idle', 'Click Check for updates when you’re ready.')

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self.content_scroll = QScrollArea(self)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.content_scroll.setWidget(self._content)
        self.content_scroll.viewport().installEventFilter(self)
        layout.addWidget(self.content_scroll, 1)
        self.description_label = QLabel(
            'Updates are checked only when you choose. Downloading does not install an update. '
            'Install and close closes CacheCAD only after active work has safely stopped. '
            'Your settings, project list, project files and Google session must be preserved. '
            'Reopen CacheCAD normally after installation. A prior sync will not restart automatically.', self)
        self.description_label.setTextFormat(Qt.TextFormat.PlainText)
        self.description_label.setWordWrap(True)
        self._content_layout.addWidget(self.description_label)
        self.details_label = QLabel('Installed version: ' + self._installed_version, self)
        self.details_label.setObjectName('applicationUpdateDetails')
        self.details_label.setTextFormat(Qt.TextFormat.PlainText)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details_label.setWordWrap(True)
        self._content_layout.addWidget(self.details_label)
        self.status_label = QLabel(self)
        self.status_label.setObjectName('applicationUpdateStatus')
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self._content_layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.hide()
        self._content_layout.addWidget(self.progress_bar)
        for label in (self.description_label, self.details_label, self.status_label):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        buttons = QGridLayout()
        self._button_layout = buttons
        self._button_columns = None
        self.check_button = QPushButton('Check for updates', self)
        self.check_button.clicked.connect(self.check_for_updates)
        self.download_button = QPushButton('Download update', self)
        self.download_button.clicked.connect(self.download_update)
        self.install_button = QPushButton('Install and close', self)
        self.install_button.clicked.connect(self.install_update)
        self.cancel_button = QPushButton('Cancel', self)
        self.cancel_button.clicked.connect(self.cancel_update)
        self.later_button = QPushButton('Later', self)
        self.later_button.clicked.connect(self.reject)
        self._buttons = (self.check_button, self.download_button, self.install_button,
                         self.cancel_button, self.later_button)
        for column, button in enumerate(self._buttons):
            button.setAutoDefault(False)
            buttons.addWidget(button, 0, column)
        # Enter must never start a check, download or installation accidentally.
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        layout.addLayout(buttons)

    @property
    def _task(self):
        return self._lifetime.task

    @_task.setter
    def _task(self, value):
        self._lifetime.task = value

    @property
    def _verified(self):
        return self._lifetime.verified

    @_verified.setter
    def _verified(self, value):
        self._lifetime.verified = value

    @property
    def configured(self):
        return self._service is not None and getattr(self._service, 'configured', False) is True

    @property
    def state(self):
        return self._state

    @property
    def busy(self):
        return self._task is not None or self._installing

    @property
    def is_ready(self):
        return self._verified is not None and not self.busy and not self._closed

    @property
    def closed(self):
        return self._closed

    @property
    def offer(self):
        return self._offer

    def _set_state(self, state, message):
        self._state = state
        self.status_label.setText(message)
        self._update_controls()
        # Use the actual viewport width, not a wider sizeHint that adjustSize()
        # may subsequently clamp to the screen. Large fonts/small windows retain
        # every consent line in a scrollable body, with actions outside it.
        for _ in range(3):
            self.layout().activate()
            self._reflow()
            margins = self.layout().contentsMargins()
            chrome = (margins.top() + margins.bottom() + self.layout().spacing()
                      + self._button_layout.sizeHint().height())
            available = self.screen().availableGeometry().height() - 80
            wanted = self._content_layout.minimumSize().height() + chrome
            self.resize(self.width(), min(wanted, max(200, available)))
        self.layout().activate()
        self._reflow()
        self.state_changed.emit(state)

    def eventFilter(self, watched, event):
        if (watched is self.content_scroll.viewport()
                and event.type() in (QEvent.Type.Resize, QEvent.Type.FontChange)):
            self._layout_timer.start(0)
        return super().eventFilter(watched, event)

    @Slot()
    def _reflow(self):
        if self._reflowing:
            return
        self._reflowing = True
        try:
            margins = self.layout().contentsMargins()
            available = self.width() - margins.left() - margins.right()
            spacing = self._button_layout.horizontalSpacing()
            widths = [button.sizeHint().width() for button in self._buttons]
            columns = len(widths)
            while columns > 1 and max(
                    sum(widths[start:start + columns]) + spacing * (columns - 1)
                    for start in range(0, len(widths), columns)) > available:
                columns -= 1
            if columns != self._button_columns:
                self._button_columns = columns
                for button in self._buttons:
                    self._button_layout.removeWidget(button)
                for column in range(len(self._buttons)):
                    self._button_layout.setColumnStretch(column, int(column < columns))
                for index, button in enumerate(self._buttons):
                    self._button_layout.addWidget(button, index // columns, index % columns)
            width = max(1, self.content_scroll.viewport().width())
            for label in (self.description_label, self.details_label, self.status_label):
                metrics = label.fontMetrics()
                key = (width, label.text(), label.font().toString(),
                       metrics.height(), metrics.averageCharWidth())
                if self._label_measurements.get(label) != key:
                    # heightForWidth includes an explicit minimumHeight; clear
                    # the old value before measuring at this new width/font.
                    label.setMinimumHeight(0)
                    label.setMinimumHeight(max(0, label.heightForWidth(width)))
                    self._label_measurements[label] = key
            self._content_layout.invalidate()
            self._content_layout.activate()
        finally:
            self._reflowing = False

    def _update_controls(self):
        available = not self._closed and not self.busy and self._state != 'handoff'
        self.check_button.setEnabled(available)
        self.download_button.setEnabled(available and self._offer is not None
                                        and self._verified is None)
        self.install_button.setEnabled(available and self._verified is not None
                                       and callable(self._request_install))
        self.cancel_button.setEnabled(not self._closed and not self._installing
                                     and self._state not in ('cancelling', 'handoff'))
        self.later_button.setEnabled(not self._closed and not self._installing
                                    and self._state != 'handoff')
        self.progress_bar.setVisible(self._state in ('checking', 'downloading', 'verifying',
                                                    'cancelling'))
        if self._state in ('checking', 'verifying', 'cancelling'):
            self.progress_bar.setRange(0, 0)
        elif self._state == 'downloading':
            self.progress_bar.setRange(0, 100)

    def _release_verified(self):
        verified, self._verified = self._verified, None
        _close_verified(verified)

    def _show_offer(self):
        offer = self._offer
        lines = ['Installed version: ' + self._installed_version]
        if offer is not None:
            lines.extend((
                'Available version: ' + _display(offer.version, 100) +
                ' (build ' + _display(offer.build, 40) + ')',
                'Channel: ' + _display(offer.channel, 80),
                'Download size: ' + _display(offer.length, 40) + ' bytes',
                'Publisher: owner-approved Windows signing certificate required',
                'Release notes: ' + _display(offer.release_notes_url),
                'CacheCAD will close for installation. Reopen it normally after installation. The current profile schema must be preserved.',
            ))
        if self._verified is not None:
            lines.append('Windows publisher: verified against the owner-approved signing policy')
            signer = getattr(self._verified, 'signer_certificate_sha256', None)
            if signer:
                lines.append('Signing certificate SHA-256: ' + _display(signer, 100))
        self.details_label.setText('\n'.join(lines))

    def _start(self, phase, operation, message):
        if self._closed or self.busy:
            return False
        self._generation += 1
        task = _Task(self._generation, phase, operation, self._events)
        self._task = task
        self.progress_bar.setValue(0)
        try:
            task.thread.start()
        except RuntimeError:
            self._task = None
            self._poll.stop()
            self._set_state('failed', 'The update operation could not start. Please try again.')
            self.attempt_finished.emit('failed')
            return False
        self._poll.start()
        self._set_state(phase, message)
        return True

    @Slot()
    def check_for_updates(self):
        if self._closed or self.busy or self._state == 'handoff':
            return
        self._release_verified()
        self._offer = self._download = None
        self._show_offer()
        if not self.configured:
            self._set_state('setup_required', _SAFE_ERRORS['not_configured'] +
                            ' No update server will be contacted.')
            return
        service = self._service
        self._start('checking', lambda cancel, progress: service.check(cancel),
                    'Checking authenticated update metadata…')

    @Slot()
    def download_update(self):
        if self._closed or self.busy or self._offer is None or self._verified is not None:
            return
        service, offer = self._service, self._offer
        self._download = None
        self._start('downloading', lambda cancel, progress: service.download(offer, cancel, progress),
                    'Downloading the selected update. Installation has not been authorized.')

    @Slot(int)
    def _receive(self, generation):
        # Only the GUI thread executes this slot; results remain in the task until
        # is_alive() confirms the worker is completely finished.
        if generation == self._generation or self._state == 'cancelling' or self._closed:
            self._settle()

    @Slot(int, object, object)
    def _progress(self, generation, received, total):
        if (self._closed or generation != self._generation or self._state != 'downloading'
                or type(received) is not int or type(total) is not int or total <= 0):
            return
        self.progress_bar.setValue(max(0, min(100, received * 100 // total)))

    @Slot()
    def _settle(self):
        task = self._task
        if task is None or task.thread.is_alive():
            return
        result, code = task.take()
        self._task = None
        self._poll.stop()
        stale = self._closed or task.generation != self._generation or task.cancel_event.is_set()
        if stale:
            if task.phase == 'verifying':
                _close_verified(result)
            if not self._closed:
                self._set_state('cancelled', _SAFE_ERRORS['cancelled'])
                self.attempt_finished.emit('cancelled')
            return
        if code:
            self._download = None
            if code in ('changed', 'incompatible', 'rollback', 'configuration', 'not_configured'):
                self._offer = None
                self._show_offer()
            if task.phase == 'verifying':
                _close_verified(result)
            message = _SAFE_ERRORS.get(code, (
                'Windows publisher verification failed. Installation is disabled.'
                if task.phase == 'verifying' else
                'The update could not be authenticated or downloaded. No installation was started.'))
            self._set_state('setup_required' if code == 'not_configured' else 'failed', message)
            self.attempt_finished.emit(code)
            return
        if task.phase == 'checking':
            self._offer = result
            self._show_offer()
            if result is None:
                self._set_state('current', 'No newer compatible update was offered by the verified channel.')
            else:
                self._set_state('offered', 'An authenticated update is available. Choose Download to continue.')
            self.attempt_finished.emit('checked')
        elif task.phase == 'downloading':
            if result is None or getattr(result, 'offer', None) != self._offer:
                self._set_state('failed', 'The downloaded update did not match your selection. Installation is disabled.')
                self.attempt_finished.emit('failed')
                return
            self._download = result
            verifier = self._verify_download
            if not callable(verifier):
                self._download = None
                self._set_state('failed', 'Download integrity is verified, but Windows publisher verification '
                                'is unavailable. Installation is disabled.')
                self.attempt_finished.emit('failed')
                return
            self._start('verifying', lambda cancel, progress: verifier(result, cancel),
                        'Download integrity verified. Checking the Windows publisher and protected installer…')
        elif task.phase == 'verifying':
            self._download = None
            if result is None or not callable(getattr(result, 'close', None)):
                _close_verified(result)
                self._set_state('failed', 'Windows publisher verification did not return a protected installer. '
                                'Installation is disabled.')
                self.attempt_finished.emit('failed')
                return
            self._verified = result
            self._show_offer()
            message = 'Download integrity and Windows publisher verified. Choose Install and close, or Later.'
            if not callable(self._request_install):
                message = 'Download integrity and Windows publisher verified. Installer handoff is not configured.'
            self._set_state('ready', message)
            self.attempt_finished.emit('verified')

    @Slot()
    def cancel_update(self):
        if self._closed or self._installing or self._state == 'handoff':
            return
        if self._task is None:
            self.reject()
            return
        if self._state == 'cancelling':
            return
        self._generation += 1
        self._download = None
        self._release_verified()
        self._task.cancel()
        self._set_state('cancelling', 'Cancellation requested. Waiting for the update operation to stop safely…')
        self._settle()

    @Slot()
    def install_update(self):
        if not self.is_ready or not callable(self._request_install) or self._state == 'handoff':
            return
        self._installing = True
        self._set_state('installing', 'Checking that active work has stopped safely before installer handoff…')
        try:
            # This callback is deliberately synchronous and GUI-thread-only. It
            # must defer instead of blocking to wait for sync/account workers.
            reason = self._request_install(self._verified)
        except Exception:
            reason = 'Installation could not start safely. You can retry or choose Later.'
        self._installing = False
        if self._handoff_failure is not None:
            message, self._handoff_failure = self._handoff_failure, None
            self._reset_after_handoff_failure(message)
            return
        if reason is None:
            self._verified = None  # Ownership accepted by the controller.
            self._set_state('handoff', 'Installer handoff accepted. CacheCAD will close when the verified helper is ready.')
            self.attempt_finished.emit('handoff')
        else:
            message = (_display(reason, 1200) if isinstance(reason, str) and reason else
                       'Installation was deferred. You can retry or choose Later.')
            self._set_state('ready', message)
            self.attempt_finished.emit('deferred')

    def _reset_after_handoff_failure(self, message):
        self._release_verified()
        self._offer = self._download = None
        self._show_offer()
        self._set_state('failed', message + ' Check for updates again to retry safely.')
        self.attempt_finished.emit('handoff_failed')

    @Slot(str)
    def handoff_failed(self, message):
        """Controller reports a terminal handoff failure after cleaning its token.

        Never reuse a transferred/closed installer handle. A retry requires a
        fresh check, download and Windows verification. A dismissed dialog is
        not reopened by a late failure. The controller may create a new one.
        """
        if self._closed or self._state not in ('installing', 'handoff'):
            return
        safe = (_display(message, 1200) if isinstance(message, str) and message else
                'The installer could not be started safely.')
        if self._installing:
            self._handoff_failure = safe
        else:
            self._reset_after_handoff_failure(safe)

    def shutdown(self, timeout_ms=1500):
        """Cancel and join for at most 1.5 s; False means a worker is still alive.

        Keep this dialog/controller alive and defer application exit on False.
        Do not delete a cache/profile while a cancelled worker still owns it.
        Workers are daemon Python threads, never forcibly terminated QThreads.
        """
        if self._installing:
            return False  # The controller must accept ownership before shutdown.
        if not self._closed:
            self._closed = True
            self._generation += 1
            self._download = self._offer = None
            self._release_verified()
            if self._task is not None:
                self._task.cancel()
            self._set_state('closed', 'Application updates closed.')
        task = self._task
        if task is not None:
            task.thread.join(max(0, min(float(timeout_ms), 1500)) / 1000)
            if not task.thread.is_alive():
                self._settle()
        if self._task is None:
            self._poll.stop()
        return self._task is None and not self._installing

    def reject(self):
        if self._installing:
            return
        self.shutdown(0)
        super().reject()

    def accept(self):
        # QDialog.accept is never a second route to installer consent.
        self.reject()

    def closeEvent(self, event):
        if self._installing:
            event.ignore()
            return
        self.shutdown(0)
        super().closeEvent(event)
