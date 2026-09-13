import threading

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal, Slot

from xfeed.collection import CollectionResult
from xfeed.posts import SaveResult
from xfeed.topic_analysis import (
    AnalysisPost,
    TopicAnalysisRun,
    TopicAnalyzer,
    TopicModelDraft,
    TopicModelState,
)
from xfeed.topic_repository import TopicAnalysisRepository
from xfeed.ui.workers import Worker


class TopicAnalysisManager(QObject):
    status_changed = Signal(object)
    completed = Signal(object)

    def __init__(
        self,
        repository: TopicAnalysisRepository,
        analyzer: TopicAnalyzer,
        *,
        thread_pool: QThreadPool | None = None,
        debounce_ms: int = 750,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._analyzer = analyzer
        self._pool = thread_pool or QThreadPool.globalInstance()
        self._debounce_ms = debounce_ms
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._start_pending)
        self._cancelled = threading.Event()
        self._force_pending = False
        self._rerun_requested = False
        self._running_run_id: int | None = None
        self._worker: Worker | None = None
        self._stopped = False

    def status(self) -> TopicAnalysisRun | None:
        return self._repository.status()

    def active_run(self) -> TopicAnalysisRun | None:
        return self._repository.active_run()

    def request_analysis(self, *, force: bool = False) -> None:
        if self._stopped:
            return
        self._force_pending = self._force_pending or force
        if self._running_run_id is not None:
            self._rerun_requested = True
            return
        self._debounce.start(self._debounce_ms)

    def request_startup_analysis(self) -> None:
        if self._stopped:
            return
        self._repository.recover_incomplete()
        if self._repository.corpus():
            self.request_analysis()

    def on_collection_completed(self, value: object) -> None:
        if isinstance(value, CollectionResult) and value.progress.saved > 0:
            self.request_analysis()

    def on_manual_save_completed(self, value: object) -> None:
        if isinstance(value, SaveResult) and value.post is not None:
            self.request_analysis()

    def stop(self) -> None:
        self._stopped = True
        self._cancelled.set()
        self._debounce.stop()
        self._force_pending = False
        self._rerun_requested = False

    @Slot()
    def _start_pending(self) -> None:
        if self._stopped:
            return
        if self._running_run_id is not None:
            self._rerun_requested = True
            return

        force = self._force_pending
        self._force_pending = False
        posts = self._repository.corpus()
        fingerprint = self._repository.fingerprint(posts, self._analyzer.config_json)
        if not force and self._repository.has_completed_fingerprint(fingerprint):
            self.status_changed.emit(self._repository.status())
            return

        run = self._repository.begin(self._analyzer.metadata(fingerprint))
        self._running_run_id = run.id
        self.status_changed.emit(run)
        worker = Worker(lambda: self._analyze(posts))
        self._worker = worker
        worker.result.connect(self._worker_completed)
        worker.failed.connect(self._worker_failed)
        self._pool.start(worker)

    def _analyze(self, posts: tuple[AnalysisPost, ...]) -> TopicModelDraft | None:
        if self._cancelled.is_set():
            return None
        draft = self._analyzer.analyze(posts)
        if self._cancelled.is_set():
            return None
        return draft

    @Slot(object)
    def _worker_completed(self, value: object) -> None:
        run_id = self._running_run_id
        if run_id is None:
            return
        if self._stopped or value is None:
            self._discard_worker()
            return
        if not isinstance(value, TopicModelDraft):
            self._record_failure(run_id, "Topic analyzer returned an invalid result")
            return
        try:
            if value.state is TopicModelState.INSUFFICIENT:
                run = self._repository.mark_insufficient(run_id)
            else:
                run = self._repository.publish(run_id, value)
        except Exception as error:
            self._record_failure(run_id, str(error))
            return
        self._finish(run)

    @Slot(str)
    def _worker_failed(self, message: str) -> None:
        run_id = self._running_run_id
        if run_id is None:
            return
        if self._stopped:
            self._discard_worker()
            return
        self._record_failure(run_id, message)

    def _record_failure(self, run_id: int, message: str) -> None:
        try:
            run = self._repository.fail(run_id, message)
        except Exception:
            self._discard_worker()
            raise
        self._finish(run)

    def _finish(self, run: TopicAnalysisRun) -> None:
        rerun = self._rerun_requested
        self._discard_worker()
        self._rerun_requested = False
        self.status_changed.emit(run)
        self.completed.emit(run)
        if rerun and not self._stopped:
            self._start_pending()

    def _discard_worker(self) -> None:
        self._running_run_id = None
        self._worker = None
