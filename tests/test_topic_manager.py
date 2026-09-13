import threading

import pytest
from PySide6.QtCore import QThreadPool

from xfeed.collection import CollectionProgress, CollectionResult
from xfeed.db import Database
from xfeed.domain import CollectionRun, CollectionStatus, CollectionTargetKind
from xfeed.posts import SaveResult, SaveStatus
from xfeed.topic_analysis import (
    AnalysisPost,
    TopicAnalysisState,
    TopicAnalyzer,
    TopicModelDraft,
    TopicModelState,
    make_corpus_fingerprint,
)
from xfeed.topic_manager import TopicAnalysisManager
from xfeed.topic_repository import TopicAnalysisRepository


class RecordingAnalyzer(TopicAnalyzer):
    def __init__(
        self,
        *,
        state: TopicModelState = TopicModelState.INSUFFICIENT,
        failure: str | None = None,
        block_first: bool = False,
    ) -> None:
        super().__init__()
        self.state = state
        self.failure = failure
        self.block_first = block_first
        self.calls: list[tuple[AnalysisPost, ...]] = []
        self.thread_ids: list[int] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def analyze(self, posts: tuple[AnalysisPost, ...]) -> TopicModelDraft:
        self.calls.append(posts)
        self.thread_ids.append(threading.get_ident())
        self.started.set()
        if self.block_first and len(self.calls) == 1:
            assert self.release.wait(3)
        if self.failure is not None:
            raise RuntimeError(self.failure)
        return TopicModelDraft(
            state=self.state,
            config_json=self.config_json,
            corpus_fingerprint=make_corpus_fingerprint(posts, self.config_json),
            topics=(),
            assignments=(),
        )


def add_post(
    database: Database,
    post_id: int,
    text: str,
    *,
    updated_at: str = "2026-08-01 12:00:00",
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO posts(
                   id,canonical_url,x_post_id,text,embed_html,provider_json,source_method,
                   availability,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                post_id,
                f"https://x.com/example/status/{post_id}",
                str(post_id),
                text,
                "<blockquote></blockquote>",
                "{}",
                "oembed",
                "available",
                updated_at,
            ),
        )


@pytest.fixture
def database(tmp_path) -> Database:
    result = Database(tmp_path / "feed.sqlite3")
    result.migrate()
    add_post(result, 1, "python typing package")
    add_post(result, 2, "the and or")
    add_post(result, 3, " \t\n ")
    return result


@pytest.fixture
def repository(database: Database) -> TopicAnalysisRepository:
    return TopicAnalysisRepository(database)


@pytest.fixture
def pool() -> QThreadPool:
    result = QThreadPool()
    result.setMaxThreadCount(1)
    yield result
    assert result.waitForDone(5_000)


def manager(
    repository: TopicAnalysisRepository,
    analyzer: TopicAnalyzer,
    pool: QThreadPool,
    *,
    debounce_ms: int = 10,
) -> TopicAnalysisManager:
    return TopicAnalysisManager(
        repository,
        analyzer,
        thread_pool=pool,
        debounce_ms=debounce_ms,
    )


def seed_active(
    repository: TopicAnalysisRepository,
    analyzer: TopicAnalyzer,
):
    posts = repository.corpus()
    fingerprint = repository.fingerprint(posts, analyzer.config_json)
    run = repository.begin(analyzer.metadata(fingerprint))
    return repository.publish(
        run.id,
        TopicModelDraft(
            state=TopicModelState.BUILT,
            config_json=analyzer.config_json,
            corpus_fingerprint=fingerprint,
            topics=(),
            assignments=(),
        ),
    )


def collection_result(*, saved: int) -> CollectionResult:
    run = CollectionRun(
        id=1,
        source_id=None,
        started_at="2026-08-02 10:00:00",
        finished_at="2026-08-02 10:01:00",
        requested_max=10,
        candidate_count=saved,
        saved_count=saved,
        duplicate_count=0,
        failed_count=0,
        status=CollectionStatus.COMPLETED,
        reason="limit",
        diagnostic=None,
        target_kind=CollectionTargetKind.FOLLOWING,
    )
    return CollectionResult(run, CollectionProgress(saved, saved, saved, 0, 0))


def test_many_requests_debounce_to_one_background_analysis(
    qtbot,
    repository: TopicAnalysisRepository,
    pool: QThreadPool,
) -> None:
    analyzer = RecordingAnalyzer()
    subject = manager(repository, analyzer, pool, debounce_ms=20)

    with qtbot.waitSignal(subject.completed, timeout=3_000):
        subject.request_analysis()
        subject.request_analysis()
        subject.request_analysis()

    assert len(analyzer.calls) == 1


def test_matching_active_fingerprint_skips_unforced_but_force_runs(
    qtbot,
    repository: TopicAnalysisRepository,
    pool: QThreadPool,
) -> None:
    analyzer = RecordingAnalyzer()
    active = seed_active(repository, analyzer)
    statuses = []
    subject = manager(repository, analyzer, pool)
    subject.status_changed.connect(statuses.append)

    subject.request_analysis()
    qtbot.waitUntil(lambda: statuses == [active])
    assert analyzer.calls == []

    with qtbot.waitSignal(subject.completed, timeout=3_000):
        subject.request_analysis(force=True)

    assert len(analyzer.calls) == 1


def test_manager_exposes_current_status_and_active_completed_run(
    repository: TopicAnalysisRepository,
    pool: QThreadPool,
) -> None:
    analyzer = RecordingAnalyzer()
    active = seed_active(repository, analyzer)
    newer = repository.begin(analyzer.metadata(active.corpus_fingerprint))
    failed = repository.fail(newer.id, "fit exploded")
    subject = manager(repository, analyzer, pool)

    assert subject.status() == failed
    assert subject.active_run() == active


def test_analysis_runs_on_thread_pool_not_calling_thread(
    qtbot,
    repository: TopicAnalysisRepository,
    pool: QThreadPool,
) -> None:
    analyzer = RecordingAnalyzer()
    subject = manager(repository, analyzer, pool)
    calling_thread = threading.get_ident()

    with qtbot.waitSignal(subject.completed, timeout=3_000):
        subject.request_analysis(force=True)

    assert analyzer.thread_ids != [calling_thread]


def test_manager_uses_repository_nonblank_corpus_and_matching_fingerprint(
    qtbot,
    repository: TopicAnalysisRepository,
    pool: QThreadPool,
) -> None:
    analyzer = RecordingAnalyzer(state=TopicModelState.BUILT)
    subject = manager(repository, analyzer, pool)

    with qtbot.waitSignal(subject.completed, timeout=3_000) as completed:
        subject.request_analysis(force=True)

    posts = repository.corpus()
    assert analyzer.calls == [posts]
    assert tuple(post.id for post in posts) == (1, 2)
    assert completed.args[0].corpus_fingerprint == repository.fingerprint(
        posts, analyzer.config_json
    )
    assert repository.active_run() == completed.args[0]


def test_failed_analysis_preserves_old_active_run(
    qtbot,
    repository: TopicAnalysisRepository,
    pool: QThreadPool,
) -> None:
    analyzer = RecordingAnalyzer(failure="fit exploded")
    active = seed_active(repository, analyzer)
    subject = manager(repository, analyzer, pool)

    with qtbot.waitSignal(subject.completed, timeout=3_000) as completed:
        subject.request_analysis(force=True)

    assert completed.args[0].state is TopicAnalysisState.FAILED
    assert completed.args[0].diagnostic == "fit exploded"
    assert repository.active_run() == active


def test_insufficient_analysis_preserves_old_active_run(
    qtbot,
    database: Database,
    repository: TopicAnalysisRepository,
    pool: QThreadPool,
) -> None:
    analyzer = RecordingAnalyzer()
    active = seed_active(repository, analyzer)
    add_post(database, 4, "new corpus item")
    subject = manager(repository, analyzer, pool)

    with qtbot.waitSignal(subject.completed, timeout=3_000) as completed:
        subject.request_analysis()

    assert completed.args[0].state is TopicAnalysisState.INSUFFICIENT
    assert repository.active_run() == active


def test_requests_while_running_coalesce_to_one_rerun(
    qtbot,
    repository: TopicAnalysisRepository,
    pool: QThreadPool,
) -> None:
    analyzer = RecordingAnalyzer(block_first=True)
    subject = manager(repository, analyzer, pool)
    completed = []
    subject.completed.connect(completed.append)

    subject.request_analysis(force=True)
    qtbot.waitUntil(analyzer.started.is_set)
    subject.request_analysis()
    subject.request_analysis(force=True)
    subject.request_analysis()
    analyzer.release.set()

    qtbot.waitUntil(lambda: len(completed) == 2, timeout=5_000)
    assert len(analyzer.calls) == 2


def test_startup_recovers_incomplete_run_and_queues_existing_corpus(
    qtbot,
    repository: TopicAnalysisRepository,
    pool: QThreadPool,
) -> None:
    analyzer = RecordingAnalyzer()
    posts = repository.corpus()
    stale = repository.begin(analyzer.metadata(repository.fingerprint(posts, analyzer.config_json)))
    subject = manager(repository, analyzer, pool)

    with qtbot.waitSignal(subject.completed, timeout=3_000):
        subject.request_startup_analysis()

    assert repository.by_id(stale.id).state is TopicAnalysisState.CANCELLED
    assert len(analyzer.calls) == 1


def test_stop_during_fit_leaves_run_incomplete_for_next_startup_recovery(
    qtbot,
    repository: TopicAnalysisRepository,
    pool: QThreadPool,
) -> None:
    analyzer = RecordingAnalyzer(block_first=True)
    subject = manager(repository, analyzer, pool)
    completed = []
    subject.completed.connect(completed.append)

    subject.request_analysis(force=True)
    qtbot.waitUntil(analyzer.started.is_set)
    subject.stop()
    analyzer.release.set()
    assert pool.waitForDone(3_000)
    qtbot.wait(20)

    assert completed == []
    assert repository.status().state is TopicAnalysisState.RUNNING


def test_collection_and_manual_triggers_require_persisted_posts(
    qtbot,
    repository: TopicAnalysisRepository,
    pool: QThreadPool,
) -> None:
    analyzer = RecordingAnalyzer()
    subject = manager(repository, analyzer, pool, debounce_ms=20)
    post = object()

    subject.on_collection_completed(collection_result(saved=0))
    subject.on_collection_completed(object())
    subject.on_manual_save_completed(SaveResult(SaveStatus.INVALID, message="bad"))
    subject.on_manual_save_completed(object())
    qtbot.wait(40)
    assert analyzer.calls == []

    with qtbot.waitSignal(subject.completed, timeout=3_000):
        subject.on_collection_completed(collection_result(saved=2))
        subject.on_manual_save_completed(
            SaveResult(SaveStatus.DUPLICATE, post=post)  # type: ignore[arg-type]
        )

    assert len(analyzer.calls) == 1
