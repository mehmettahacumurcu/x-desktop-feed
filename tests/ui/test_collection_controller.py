import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from xfeed.collection import (
    CandidateObservation,
    CollectionProgress,
    CollectionService,
    DiscoveryReason,
    DiscoveryResult,
)
from xfeed.db import Database
from xfeed.domain import CollectionStatus, CollectionTarget
from xfeed.posts import SaveResult, SaveStatus
from xfeed.repositories import CollectionRepository, SourceRepository
from xfeed.session_repository import AppSessionRepository
from xfeed.sources import canonicalize_profile
from xfeed.ui.collection_controller import CollectionController


class CollectorDouble(QObject):
    progress = Signal(object)
    finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.widget = QWidget()
        self.starts: list[tuple[CollectionTarget, int]] = []
        self.cancel_count = 0
        self.events: list[str] = []

    def start(self, target: CollectionTarget, maximum: int = 30) -> None:
        self.events.append("start")
        self.starts.append((target, maximum))

    def cancel(self) -> None:
        self.events.append("cancel")
        self.cancel_count += 1


class RaisingCollectorDouble(CollectorDouble):
    def start(self, target: CollectionTarget, maximum: int = 30) -> None:
        super().start(target, maximum)
        raise RuntimeError("discovery setup failed")


class PostServiceDouble:
    def __init__(self, before_return: Callable[[], None] | None = None) -> None:
        self.before_return = before_return
        self.urls: list[str] = []
        self.thread_ids: list[int] = []

    def save(self, url: str) -> SaveResult:
        self.urls.append(url)
        self.thread_ids.append(threading.get_ident())
        if self.before_return is not None:
            self.before_return()
        return SaveResult(SaveStatus.UNAVAILABLE, None, "Unavailable")


class RaisingCollectionService:
    def process(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("worker failed before finalization")


class LookupRaisingCollectionRepository(CollectionRepository):
    def latest_for_target(self, target: CollectionTarget):
        del target
        raise RuntimeError("secret-database-path")


class TargetTrackingCollectionRepository(CollectionRepository):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.latest_targets: list[CollectionTarget] = []

    def latest_for_target(self, target: CollectionTarget):
        self.latest_targets.append(target)
        return super().latest_for_target(target)

    def latest_for_source(self, source_id: int | None):
        raise AssertionError(f"latest_for_source must not be called directly: {source_id}")


class FinalizingThenRaisingCollectionService:
    def __init__(self, collections: CollectionRepository) -> None:
        self.collections = collections
        self.finished = None

    def process(self, run, *_args: object, **_kwargs: object) -> object:
        self.finished = self.collections.finish(
            run.id,
            status=CollectionStatus.PARTIAL,
            candidate_count=7,
            saved_count=5,
            duplicate_count=1,
            failed_count=1,
            reason="exhausted",
            diagnostic="original diagnostic",
        )
        raise RuntimeError("processing failed after finalization")


def candidate(post_id: str) -> CandidateObservation:
    return CandidateObservation(
        url=f"https://x.com/openai/status/{post_id}",
        post_id=post_id,
        author_handle="openai",
    )


def build_controller(tmp_path, posts: PostServiceDouble | None = None):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    sources = SourceRepository(database)
    source = sources.add(canonicalize_profile("openai"))
    collections = CollectionRepository(database)
    collector = CollectorDouble()
    post_service = posts or PostServiceDouble()
    service = CollectionService(post_service, collections)  # type: ignore[arg-type]
    session_id = AppSessionRepository(database).begin("test").id
    controller = CollectionController(collector, service, collections, session_id=session_id)
    target = CollectionTarget.for_source(source.id, source.handle, source.profile.profile_url)
    return source, target, collections, collector, post_service, controller


def test_controller_attaches_active_session_id(tmp_path, qtbot) -> None:
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    collections = CollectionRepository(database)
    session_id = AppSessionRepository(database).begin("test").id
    controller = CollectionController(
        CollectorDouble(),
        CollectionService(PostServiceDouble(), collections),  # type: ignore[arg-type]
        collections,
        session_id=session_id,
    )
    qtbot.addWidget(controller.widget)

    controller.start(CollectionTarget.for_you(), 10)

    latest = collections.latest_for_target(CollectionTarget.for_you())
    assert latest is not None
    assert latest.session_id == session_id


def test_start_creates_one_run_forwards_progress_and_processes_off_gui_thread(tmp_path, qtbot):
    source, target, collections, collector, posts, controller = build_controller(tmp_path)
    progress: list[CollectionProgress] = []
    completed = []
    active: list[bool] = []
    controller.progress.connect(progress.append)
    controller.completed.connect(completed.append)
    controller.active_changed.connect(active.append)
    gui_thread_id = threading.get_ident()

    controller.start(target, 17)
    assert controller.active
    assert controller.active_target == target
    controller.start(CollectionTarget.for_you(), 50)
    collector.progress.emit(candidate("1"))
    collector.finished.emit(
        DiscoveryResult((candidate("1"), candidate("2")), DiscoveryReason.EXHAUSTED)
    )

    qtbot.waitUntil(lambda: len(completed) == 1)
    latest = collections.latest_for_source(source.id)
    assert latest is not None
    assert collector.starts == [(target, 17)]
    assert latest.requested_max == 17
    assert collections.latest_for_target(CollectionTarget.for_you()) is None
    assert latest.status is CollectionStatus.PARTIAL
    assert posts.thread_ids and all(thread_id != gui_thread_id for thread_id in posts.thread_ids)
    assert progress == [
        CollectionProgress(found=1, processed=0, saved=0, duplicates=0, failed=0),
        CollectionProgress(found=2, processed=1, saved=0, duplicates=0, failed=1),
        CollectionProgress(found=2, processed=2, saved=0, duplicates=0, failed=2),
    ]
    assert active == [True, False]
    assert not controller.active
    assert controller.active_target is None

    collector.finished.emit(DiscoveryResult((), DiscoveryReason.ERROR, "late"))
    assert len(completed) == 1


def test_for_you_start_creates_exact_target_aware_run_at_fifty(tmp_path):
    _, _, collections, collector, _, controller = build_controller(tmp_path)
    target = CollectionTarget.for_you()

    controller.start(target, 50)

    latest = collections.latest_for_target(target)
    assert latest is not None
    assert latest.source_id is None
    assert latest.requested_max == 50
    assert collector.starts == [(target, 50)]


def test_cancel_during_discovery_delegates_once_and_reentrant_cancel_is_safe(tmp_path, qtbot):
    _, _, collections, collector, _, controller = build_controller(tmp_path)
    target = CollectionTarget.for_you()
    completed = []
    controller.completed.connect(completed.append)

    controller.start(target, 50)
    controller.cancel()
    controller.cancel()
    collector.finished.emit(DiscoveryResult((), DiscoveryReason.CANCELLED, "collection cancelled"))

    qtbot.waitUntil(lambda: len(completed) == 1)
    latest = collections.latest_for_target(target)
    assert latest is not None
    assert latest.status is CollectionStatus.CANCELLED
    assert collector.cancel_count == 1


def test_cancel_reentered_from_active_signal_reaches_started_collector(tmp_path, qtbot):
    _, target, _, collector, _, controller = build_controller(tmp_path)
    controller.active_changed.connect(lambda active: controller.cancel() if active else None)

    controller.start(target)

    assert collector.events == ["start", "cancel"]


def test_cancel_during_processing_stops_before_the_next_save(tmp_path, qtbot):
    first_save_started = threading.Event()
    release_first_save = threading.Event()

    def block_first_save() -> None:
        first_save_started.set()
        assert release_first_save.wait(timeout=2)

    posts = PostServiceDouble(block_first_save)
    source, target, collections, collector, _, controller = build_controller(tmp_path, posts)
    completed = []
    controller.completed.connect(completed.append)

    controller.start(target)
    collector.finished.emit(
        DiscoveryResult((candidate("1"), candidate("2")), DiscoveryReason.LIMIT)
    )
    assert first_save_started.wait(timeout=2)
    controller.cancel()
    release_first_save.set()

    qtbot.waitUntil(lambda: len(completed) == 1)
    latest = collections.latest_for_source(source.id)
    assert latest is not None
    assert posts.urls == ["https://x.com/openai/status/1"]
    assert collector.cancel_count == 0
    assert latest.status is CollectionStatus.CANCELLED


def test_discovery_error_without_candidates_is_persisted_as_failed(tmp_path, qtbot):
    source, target, collections, collector, _, controller = build_controller(tmp_path)
    completed = []
    controller.completed.connect(completed.append)

    controller.start(target)
    collector.finished.emit(DiscoveryResult((), DiscoveryReason.ERROR, "login redirect"))

    qtbot.waitUntil(lambda: len(completed) == 1)
    latest = collections.latest_for_source(source.id)
    assert latest is not None
    assert latest.status is CollectionStatus.FAILED
    assert latest.reason == DiscoveryReason.ERROR.value
    assert latest.diagnostic == "login redirect"


def test_synchronous_discovery_exception_finishes_the_run_and_emits_failure(tmp_path, qtbot):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    source = SourceRepository(database).add(canonicalize_profile("openai"))
    target = CollectionTarget.for_source(source.id, source.handle, source.profile.profile_url)
    collections = CollectionRepository(database)
    controller = CollectionController(
        RaisingCollectorDouble(),
        CollectionService(PostServiceDouble(), collections),  # type: ignore[arg-type]
        collections,
        session_id=AppSessionRepository(database).begin("test").id,
    )
    failures: list[str] = []
    controller.failed.connect(failures.append)

    controller.start(target)

    qtbot.waitUntil(lambda: len(failures) == 1)
    latest = collections.latest_for_source(source.id)
    assert latest is not None
    assert latest.status is CollectionStatus.FAILED
    assert latest.finished_at is not None


def test_processing_exception_cannot_leave_the_run_running(tmp_path, qtbot):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    source = SourceRepository(database).add(canonicalize_profile("openai"))
    target = CollectionTarget.for_source(source.id, source.handle, source.profile.profile_url)
    collections = CollectionRepository(database)
    collector = CollectorDouble()
    controller = CollectionController(
        collector,
        RaisingCollectionService(),  # type: ignore[arg-type]
        collections,
        session_id=AppSessionRepository(database).begin("test").id,
    )
    failures: list[str] = []
    controller.failed.connect(failures.append)

    controller.start(target)
    collector.finished.emit(DiscoveryResult((), DiscoveryReason.ERROR, "bad page"))

    qtbot.waitUntil(lambda: len(failures) == 1)
    latest = collections.latest_for_source(source.id)
    assert latest is not None
    assert latest.status is CollectionStatus.FAILED
    assert latest.finished_at is not None


def test_latest_lookup_failure_still_cleans_up_emits_failure_and_allows_restart(tmp_path, qtbot):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    source = SourceRepository(database).add(canonicalize_profile("openai"))
    target = CollectionTarget.for_source(source.id, source.handle, source.profile.profile_url)
    collections = LookupRaisingCollectionRepository(database)
    collector = CollectorDouble()
    controller = CollectionController(
        collector,
        RaisingCollectionService(),  # type: ignore[arg-type]
        collections,
        session_id=AppSessionRepository(database).begin("test").id,
    )
    failures: list[str] = []
    active: list[bool] = []
    controller.failed.connect(failures.append)
    controller.active_changed.connect(active.append)

    controller.start(target)
    collector.finished.emit(DiscoveryResult((), DiscoveryReason.ERROR, "bad page"))

    qtbot.waitUntil(lambda: len(failures) == 1)
    first_run = CollectionRepository.latest_for_source(collections, source.id)
    assert first_run is not None
    assert first_run.status is CollectionStatus.FAILED
    assert active == [True, False]
    assert "worker failed before finalization" in failures[0]
    assert "collection state could not be verified" in failures[0]
    assert "secret-database-path" not in failures[0]

    controller.start(target)
    assert collector.starts == [(target, 30), (target, 30)]
    collector.finished.emit(DiscoveryResult((), DiscoveryReason.ERROR, "bad page"))
    qtbot.waitUntil(lambda: len(failures) == 2)
    assert active == [True, False, True, False]


def test_lookup_failure_recovery_does_not_overwrite_a_terminal_run(tmp_path, qtbot):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    source = SourceRepository(database).add(canonicalize_profile("openai"))
    target = CollectionTarget.for_source(source.id, source.handle, source.profile.profile_url)
    collections = LookupRaisingCollectionRepository(database)
    service = FinalizingThenRaisingCollectionService(collections)
    collector = CollectorDouble()
    controller = CollectionController(
        collector,
        service,  # type: ignore[arg-type]
        collections,
        session_id=AppSessionRepository(database).begin("test").id,
    )
    failures: list[str] = []
    active: list[bool] = []
    controller.failed.connect(failures.append)
    controller.active_changed.connect(active.append)

    controller.start(target)
    collector.finished.emit(DiscoveryResult((), DiscoveryReason.ERROR, "bad page"))

    qtbot.waitUntil(lambda: len(failures) == 1)
    persisted = CollectionRepository.latest_for_source(collections, source.id)
    assert persisted == service.finished
    assert persisted is not None
    assert persisted.status is CollectionStatus.PARTIAL
    assert persisted.candidate_count == 7
    assert persisted.saved_count == 5
    assert persisted.duplicate_count == 1
    assert persisted.failed_count == 1
    assert persisted.reason == "exhausted"
    assert persisted.diagnostic == "original diagnostic"
    assert active == [True, False]
    assert "processing failed after finalization" in failures[0]


def test_for_you_terminal_failure_recovers_with_latest_target_lookup(tmp_path, qtbot):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    collections = TargetTrackingCollectionRepository(database)
    collector = CollectorDouble()
    controller = CollectionController(
        collector,
        RaisingCollectionService(),  # type: ignore[arg-type]
        collections,
        session_id=AppSessionRepository(database).begin("test").id,
    )
    target = CollectionTarget.for_you()
    failures: list[str] = []
    controller.failed.connect(failures.append)

    controller.start(target, 50)
    collector.finished.emit(DiscoveryResult((), DiscoveryReason.ERROR, "bad home page"))

    qtbot.waitUntil(lambda: len(failures) == 1)
    latest = CollectionRepository.latest_for_target(collections, target)
    assert latest is not None
    assert latest.status is CollectionStatus.FAILED
    assert collections.latest_targets == [target]
