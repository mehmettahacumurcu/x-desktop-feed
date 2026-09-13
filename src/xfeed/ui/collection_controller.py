import threading
from collections.abc import Callable
from typing import Protocol

from PySide6.QtCore import QObject, QThreadPool, Signal, SignalInstance
from PySide6.QtWidgets import QWidget

from xfeed.collection import (
    CandidateObservation,
    CollectionProgress,
    CollectionResult,
    CollectionService,
    DiscoveryReason,
    DiscoveryResult,
)
from xfeed.domain import CollectionRun, CollectionStatus, CollectionTarget
from xfeed.repositories import CollectionRepository
from xfeed.ui.workers import Worker


class _TargetCollector(Protocol):
    progress: SignalInstance
    finished: SignalInstance

    @property
    def widget(self) -> QWidget: ...

    def start(self, target: CollectionTarget, maximum: int = 30) -> None: ...

    def cancel(self) -> None: ...


class CollectionController(QObject):
    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)
    active_changed = Signal(bool)

    def __init__(
        self,
        collector: _TargetCollector,
        service: CollectionService,
        collections: CollectionRepository,
        session_id: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if type(session_id) is not int or session_id < 1:
            raise ValueError("session_id must be a positive integer")
        self._collector = collector
        self._service = service
        self._collections = collections
        self._session_id = session_id
        self._pool = QThreadPool.globalInstance()
        self._active = False
        self._active_announced = False
        self._phase = "idle"
        self._target: CollectionTarget | None = None
        self._run: CollectionRun | None = None
        self._cancelled = threading.Event()
        self._cancel_requested = False
        self._progress = CollectionProgress(0, 0, 0, 0, 0)
        self._worker: Worker | None = None
        self._discovery_progress_callback: Callable[..., object] | None = None
        self._discovery_finished_callback: Callable[..., object] | None = None

    @property
    def widget(self) -> QWidget:
        return self._collector.widget

    @property
    def active(self) -> bool:
        return self._active

    @property
    def active_target(self) -> CollectionTarget | None:
        return self._target

    def start(self, target: CollectionTarget, maximum: int = 30) -> None:
        if self._active:
            return
        try:
            run = self._collections.start(target, maximum, session_id=self._session_id)
        except Exception as error:
            self.failed.emit(str(error))
            return

        self._target = target
        self._run = run
        self._cancelled = threading.Event()
        self._cancel_requested = False
        self._progress = CollectionProgress(0, 0, 0, 0, 0)
        self._active = True
        self._active_announced = False
        self._phase = "discovery"
        self._connect_discovery()

        try:
            self._collector.start(target, maximum)
        except Exception as error:
            self._disconnect_discovery()
            self._terminal_failure(str(error))
            return
        if self._active:
            self._active_announced = True
            self.active_changed.emit(True)

    def _connect_discovery(self) -> None:
        self._discovery_progress_callback = self._on_discovery_progress
        self._discovery_finished_callback = self._on_discovery_finished
        self._collector.progress.connect(self._discovery_progress_callback)
        self._collector.finished.connect(self._discovery_finished_callback)

    def cancel(self) -> None:
        if not self._active or self._cancel_requested:
            return
        self._cancel_requested = True
        self._cancelled.set()
        if self._phase == "discovery":
            try:
                self._collector.cancel()
            except Exception as error:
                self._disconnect_discovery()
                self._terminal_failure(str(error))

    def _on_discovery_progress(self, observation: object) -> None:
        if not self._active or self._phase != "discovery":
            return
        if not isinstance(observation, CandidateObservation):
            return
        self._progress = CollectionProgress(
            found=self._progress.found + 1,
            processed=0,
            saved=0,
            duplicates=0,
            failed=0,
        )
        self.progress.emit(self._progress)

    def _on_discovery_finished(self, result: object) -> None:
        if not self._active or self._phase != "discovery":
            return
        self._disconnect_discovery()
        if not isinstance(result, DiscoveryResult):
            self._terminal_failure("collector returned an invalid discovery result")
            return
        run = self._run
        if run is None:
            self._terminal_failure("collection run is unavailable")
            return

        self._phase = "processing"

        def process() -> CollectionResult:
            return self._service.process(
                run,
                result,
                on_progress=self._emit_processing_progress,
                cancelled=self._cancelled.is_set,
            )

        worker = Worker(process)
        worker.result.connect(self._on_processing_completed)
        worker.failed.connect(self._terminal_failure)
        self._worker = worker
        self._pool.start(worker)

    def _emit_processing_progress(self, progress: CollectionProgress) -> None:
        self._progress = progress
        self.progress.emit(progress)

    def _on_processing_completed(self, result: object) -> None:
        if not self._active or self._phase != "processing":
            return
        if not isinstance(result, CollectionResult):
            self._terminal_failure("collection worker returned an invalid result")
            return
        self._progress = result.progress
        self._finish_active()
        self.completed.emit(result)

    def _terminal_failure(self, message: str) -> None:
        if not self._active:
            return
        run = self._run
        target = self._target
        failure_message = message or "Collection processing failed"
        try:
            if run is not None and target is not None:
                should_finish = True
                try:
                    latest = self._collections.latest_for_target(target)
                except Exception:
                    failure_message += "; collection state could not be verified"
                else:
                    should_finish = latest is None or latest.status is CollectionStatus.RUNNING
                if should_finish:
                    try:
                        self._collections.finish_if_running(
                            run.id,
                            status=(
                                CollectionStatus.PARTIAL
                                if self._progress.processed
                                else CollectionStatus.FAILED
                            ),
                            candidate_count=self._progress.found,
                            saved_count=self._progress.saved,
                            duplicate_count=self._progress.duplicates,
                            failed_count=self._progress.failed,
                            reason=DiscoveryReason.ERROR.value,
                            diagnostic=message,
                        )
                    except Exception:
                        failure_message += "; collection run finalization failed"
        except Exception:
            failure_message += "; collection run finalization failed"
        finally:
            try:
                self._disconnect_discovery()
            finally:
                try:
                    self._finish_active()
                finally:
                    self.failed.emit(failure_message)

    def _finish_active(self) -> None:
        active_was_announced = self._active_announced
        self._active = False
        self._active_announced = False
        self._phase = "idle"
        self._target = None
        self._run = None
        self._worker = None
        if active_was_announced:
            self.active_changed.emit(False)

    def _disconnect_discovery(self) -> None:
        if self._discovery_progress_callback is not None:
            try:
                self._collector.progress.disconnect(self._discovery_progress_callback)
            except (RuntimeError, TypeError):
                pass
            self._discovery_progress_callback = None
        if self._discovery_finished_callback is not None:
            try:
                self._collector.finished.disconnect(self._discovery_finished_callback)
            except (RuntimeError, TypeError):
                pass
            self._discovery_finished_callback = None
