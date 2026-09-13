from collections.abc import Sequence

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from xfeed.collection import CollectionProgress, CollectionResult, DiscoveryReason
from xfeed.db import Database
from xfeed.domain import (
    Availability,
    CollectionRun,
    CollectionStatus,
    CollectionTarget,
    CollectionTargetKind,
    PostDraft,
    SavedPost,
)
from xfeed.media import (
    MediaAsset,
    MediaOrigin,
    MediaResolutionJob,
    MediaResolutionState,
    PhotoCandidate,
)
from xfeed.media_manifest import MediaManifestService
from xfeed.media_repository import MediaRepository
from xfeed.repositories import PostRepository, SourceRepository
from xfeed.sources import SourceRecord, canonicalize_profile
from xfeed.ui.collection_coordinator import (
    CollectionCoordinator,
    CollectionOrigin,
    CollectionUiState,
    MediaResolutionRequestOutcome,
)
from xfeed.x_session import SessionState


class ControllerDouble(QObject):
    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)
    active_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.starts: list[tuple[CollectionTarget, int]] = []
        self.cancel_count = 0
        self.active = False
        self.active_target: CollectionTarget | None = None

    def start(self, target: CollectionTarget, maximum: int = 30) -> None:
        self.starts.append((target, maximum))
        self.active = True
        self.active_target = target
        self.active_changed.emit(True)

    def cancel(self) -> None:
        self.cancel_count += 1

    def finish(self, result: CollectionResult) -> None:
        self.active = False
        self.active_target = None
        self.active_changed.emit(False)
        self.completed.emit(result)

    def fail(self, message: str) -> None:
        self.active = False
        self.active_target = None
        self.active_changed.emit(False)
        self.failed.emit(message)


class MediaResolverDouble(QObject):
    completed = Signal(object)
    failed = Signal(str)
    active_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.starts: list[MediaResolutionJob] = []
        self.cancel_count = 0
        self.active = False
        self.active_job: MediaResolutionJob | None = None

    def start(self, job: MediaResolutionJob) -> None:
        assert job.state is MediaResolutionState.RESOLVING
        assert not self.active
        self.starts.append(job)
        self.active = True
        self.active_job = job
        self.active_changed.emit(True)

    def cancel(self) -> None:
        self.cancel_count += 1

    def finish(self, photos: tuple[PhotoCandidate, ...]) -> None:
        self.active = False
        self.active_job = None
        self.active_changed.emit(False)
        self.completed.emit(photos)

    def fail(self, message: str) -> None:
        self.active = False
        self.active_job = None
        self.active_changed.emit(False)
        self.failed.emit(message)


class TrackingMediaRepository(MediaRepository):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self.ensure_calls: list[tuple[int, str, MediaOrigin]] = []
        self.claim_calls: list[int] = []
        self.requeue_calls: list[tuple[int, str]] = []
        self.fail_calls: list[tuple[int, str]] = []
        self.pending_calls: list[int] = []
        self.manifest_failures_remaining = 0

    def ensure_resolution_job(
        self,
        post_id: int,
        canonical_url: str,
        origin: MediaOrigin,
    ) -> MediaResolutionJob:
        self.ensure_calls.append((post_id, canonical_url, origin))
        return super().ensure_resolution_job(post_id, canonical_url, origin)

    def claim_resolution(self, job_id: int) -> MediaResolutionJob | None:
        self.claim_calls.append(job_id)
        return super().claim_resolution(job_id)

    def record_manifest(
        self,
        post_id: int,
        canonical_url: str,
        origin: MediaOrigin,
        photos: Sequence[PhotoCandidate],
    ) -> tuple[MediaAsset, ...]:
        if self.manifest_failures_remaining:
            self.manifest_failures_remaining -= 1
            raise RuntimeError("media database unavailable")
        return super().record_manifest(post_id, canonical_url, origin, photos)

    def pending_resolution_jobs(self, limit: int = 100) -> list[MediaResolutionJob]:
        self.pending_calls.append(limit)
        return super().pending_resolution_jobs(limit)

    def requeue_resolution(self, job_id: int, diagnostic: str) -> None:
        self.requeue_calls.append((job_id, diagnostic))
        super().requeue_resolution(job_id, diagnostic)

    def fail_resolution(self, job_id: int, diagnostic: str) -> None:
        self.fail_calls.append((job_id, diagnostic))
        super().fail_resolution(job_id, diagnostic)


class TrackingManifestService(MediaManifestService):
    def __init__(self, repository: MediaRepository) -> None:
        super().__init__(repository)
        self.manual_calls: list[tuple[SavedPost, tuple[PhotoCandidate, ...]]] = []

    def record_manual_manifest(
        self,
        post: SavedPost,
        photos: tuple[PhotoCandidate, ...],
    ):
        candidates = tuple(photos)
        self.manual_calls.append((post, candidates))
        return super().record_manual_manifest(post, candidates)


class SessionDouble(QObject):
    state_changed = Signal(object)
    verification_finished = Signal(object)

    def __init__(self, state: SessionState) -> None:
        super().__init__()
        self.state = state
        self.clear_count = 0
        self.verify_count = 0

    def verify(self) -> None:
        self.verify_count += 1

    def begin_login(self) -> None:
        self.set_state(SessionState.SIGNING_IN)

    def mark_signed_out(self) -> None:
        self.set_state(SessionState.SIGNED_OUT)

    def clear(self) -> None:
        self.clear_count += 1
        self.set_state(SessionState.UNAVAILABLE)

    def set_state(self, state: SessionState) -> None:
        self.state = state
        self.state_changed.emit(state)


class LoginDialogDouble(QWidget):
    signed_in = Signal()
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.show_count = 0
        self.delete_count = 0
        self.reject_count = 0

    def show(self) -> None:
        self.show_count += 1

    def deleteLater(self) -> None:
        self.delete_count += 1

    def reject(self) -> None:
        self.reject_count += 1
        self.cancelled.emit()


class LoginFactory:
    def __init__(self) -> None:
        self.dialogs: list[LoginDialogDouble] = []
        self.parents: list[QWidget | None] = []

    def __call__(self, parent: QWidget | None) -> LoginDialogDouble:
        self.parents.append(parent)
        dialog = LoginDialogDouble(parent)
        self.dialogs.append(dialog)
        return dialog


def source(repository: SourceRepository, handle: str, *, enabled: bool = True) -> SourceRecord:
    record = repository.add(canonicalize_profile(handle))
    if not enabled:
        repository.set_enabled(record.id, False)
        record = next(item for item in repository.list_all() if item.id == record.id)
    return record


def result(
    run_id: int,
    target: CollectionTarget,
    *,
    status: CollectionStatus = CollectionStatus.COMPLETED,
    reason: str | None = DiscoveryReason.LIMIT.value,
    diagnostic: str | None = None,
) -> CollectionResult:
    run = CollectionRun(
        id=run_id,
        source_id=target.source_id,
        started_at="2026-07-21 10:00:00",
        finished_at="2026-07-21 10:01:00",
        requested_max=10,
        candidate_count=2,
        saved_count=1,
        duplicate_count=1,
        failed_count=0,
        status=status,
        reason=reason,
        diagnostic=diagnostic,
        target_kind=target.kind,
    )
    return CollectionResult(run, CollectionProgress(2, 2, 1, 1, 0))


def build_coordinator(tmp_path, state: SessionState = SessionState.SIGNED_IN):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    sources = SourceRepository(database)
    controller = ControllerDouble()
    session = SessionDouble(state)
    login_factory = LoginFactory()
    coordinator = CollectionCoordinator(controller, sources, session, login_factory)
    return sources, controller, session, login_factory, coordinator


def insert_post(posts: PostRepository, post_id: int = 123) -> SavedPost:
    return posts.insert(
        PostDraft(
            canonical_url=f"https://x.com/example/status/{post_id}",
            x_post_id=str(post_id),
            author_handle="example",
            author_name="Example",
            text="A saved post",
            published_at=None,
            embed_html="<blockquote></blockquote>",
            provider_json="{}",
            source_method="oembed",
            has_media=False,
            availability=Availability.AVAILABLE,
        ),
        manual_ingestion=True,
    )


def build_media_coordinator(tmp_path, state: SessionState = SessionState.SIGNED_IN):
    database = Database(tmp_path / "media-feed.sqlite3")
    database.migrate()
    sources = SourceRepository(database)
    posts = PostRepository(database)
    media = TrackingMediaRepository(database)
    manifest = TrackingManifestService(media)
    controller = ControllerDouble()
    resolver = MediaResolverDouble()
    session = SessionDouble(state)
    login_factory = LoginFactory()
    coordinator = CollectionCoordinator(
        controller,
        sources,
        session,
        login_factory,
        media_repository=media,
        media_manifest_service=manifest,
        media_resolver=resolver,
    )
    return (
        sources,
        posts,
        media,
        manifest,
        controller,
        resolver,
        session,
        login_factory,
        coordinator,
    )


def resolution_row(database: Database, post_id: int):
    with database.connection() as connection:
        return connection.execute(
            "SELECT state,manifest_count,attempts,diagnostic "
            "FROM media_resolution_jobs WHERE post_id=?",
            (post_id,),
        ).fetchone()


def test_manual_requests_are_exact_and_global_busy_state_rejects_overlap(tmp_path) -> None:
    sources, controller, _session, _login, coordinator = build_coordinator(tmp_path)
    openai = source(sources, "openai")

    assert coordinator.collect_for_you(50, interactive=True, parent=None)
    assert coordinator.busy
    assert not coordinator.collect_source(openai, 10, parent=None)
    assert controller.starts == [(CollectionTarget.for_you(), 50)]
    assert isinstance(coordinator.state, CollectionUiState)
    assert coordinator.state.active is not None
    assert coordinator.state.active.origin is CollectionOrigin.MANUAL


def test_automatic_home_feed_requests_are_serialized_in_fifo_order(tmp_path) -> None:
    _sources, controller, _session, _login, coordinator = build_coordinator(tmp_path)
    for_you = CollectionTarget.for_you()
    following = CollectionTarget.following()

    assert coordinator.collect_feed(
        CollectionTargetKind.FOR_YOU,
        20,
        interactive=False,
        parent=None,
    )
    assert coordinator.collect_feed(
        CollectionTargetKind.FOLLOWING,
        50,
        interactive=False,
        parent=None,
    )

    assert controller.starts == [(for_you, 20)]
    controller.finish(result(7, for_you))
    assert controller.starts == [(for_you, 20), (following, 50)]


def test_collection_emits_one_batch_completion_not_one_signal_per_saved_item(tmp_path) -> None:
    _sources, controller, _session, _login, coordinator = build_coordinator(tmp_path)
    completed: list[CollectionResult] = []
    coordinator.request_completed.connect(completed.append)
    target = CollectionTarget.for_you()
    terminal = result(7, target)

    coordinator.collect_for_you(10, interactive=True, parent=None)
    controller.progress.emit(CollectionProgress(3, 1, 1, 0, 0))
    controller.progress.emit(CollectionProgress(3, 2, 2, 0, 0))
    controller.progress.emit(CollectionProgress(3, 3, 3, 0, 0))

    assert completed == []

    controller.finish(terminal)

    assert completed == [terminal]


def test_home_feed_run_ids_are_independent_newest_first_and_unique(tmp_path) -> None:
    _sources, controller, _session, _login, coordinator = build_coordinator(tmp_path)
    for_you = CollectionTarget.for_you()
    following = CollectionTarget.following()

    coordinator.collect_feed(CollectionTargetKind.FOR_YOU, 10, interactive=True, parent=None)
    controller.finish(result(7, for_you))
    coordinator.collect_feed(CollectionTargetKind.FOLLOWING, 10, interactive=True, parent=None)
    controller.finish(result(9, following))
    coordinator.collect_feed(CollectionTargetKind.FOR_YOU, 10, interactive=True, parent=None)
    controller.finish(result(8, for_you))
    coordinator.collect_feed(CollectionTargetKind.FOLLOWING, 10, interactive=True, parent=None)
    controller.finish(result(9, following))

    assert coordinator.current_run_ids(CollectionTargetKind.FOR_YOU) == (8, 7)
    assert coordinator.current_run_ids(CollectionTargetKind.FOLLOWING) == (9,)
    assert coordinator.current_for_you_run_ids == (8, 7)


@pytest.mark.parametrize("raw_kind", ["for_you", "following"])
def test_home_feed_boundaries_reject_raw_string_target_kinds(tmp_path, raw_kind: str) -> None:
    _sources, controller, _session, _login, coordinator = build_coordinator(tmp_path)

    with pytest.raises(ValueError, match="home feed"):
        coordinator.collect_feed(  # type: ignore[arg-type]
            raw_kind, 10, interactive=True, parent=None
        )
    with pytest.raises(ValueError, match="home feed"):
        coordinator.current_run_ids(raw_kind)  # type: ignore[arg-type]

    assert controller.starts == []


def test_collect_all_runs_enabled_sources_sequentially_and_revalidates_each(tmp_path) -> None:
    sources, controller, _session, _login, coordinator = build_coordinator(tmp_path)
    alpha = source(sources, "alpha")
    beta = source(sources, "beta")
    disabled = source(sources, "disabled", enabled=False)

    assert coordinator.collect_all((alpha, beta, disabled), 20, parent=None)
    alpha_target = CollectionTarget.for_source(alpha.id, alpha.handle, alpha.profile_url)
    assert controller.starts == [(alpha_target, 20)]

    sources.set_enabled(beta.id, False)
    controller.finish(result(1, alpha_target))

    assert controller.starts == [(alpha_target, 20)]
    assert not coordinator.busy
    assert all(target.handle != disabled.handle for target, _maximum in controller.starts)
    assert all(target.handle != beta.handle for target, _maximum in controller.starts)


def test_collect_all_continues_after_ordinary_terminal_failure(tmp_path) -> None:
    sources, controller, _session, _login, coordinator = build_coordinator(tmp_path)
    alpha = source(sources, "alpha")
    beta = source(sources, "beta")
    alpha_target = CollectionTarget.for_source(alpha.id, alpha.handle, alpha.profile_url)
    beta_target = CollectionTarget.for_source(beta.id, beta.handle, beta.profile_url)

    coordinator.collect_all((alpha, beta), 10, parent=None)
    controller.finish(
        result(
            1,
            alpha_target,
            status=CollectionStatus.FAILED,
            reason=DiscoveryReason.ERROR.value,
            diagnostic="provider failed",
        )
    )

    assert controller.starts == [(alpha_target, 10), (beta_target, 10)]


@pytest.mark.parametrize(
    ("reason", "diagnostic"),
    [
        (DiscoveryReason.LOGIN_WALL.value, "X is signed out; reconnect"),
        (DiscoveryReason.ERROR.value, "X presented an account challenge in Opera GX"),
        (DiscoveryReason.ERROR.value, "X rate-limited the Opera GX session"),
    ],
)
def test_session_blocking_result_stops_queue_and_emits_exact_pause(
    tmp_path,
    reason: str,
    diagnostic: str,
) -> None:
    sources, controller, session, _login, coordinator = build_coordinator(tmp_path)
    alpha = source(sources, "alpha")
    beta = source(sources, "beta")
    alpha_target = CollectionTarget.for_source(alpha.id, alpha.handle, alpha.profile_url)
    blocked: list[str] = []
    coordinator.session_blocked.connect(blocked.append)

    coordinator.collect_all((alpha, beta), 10, parent=None)
    controller.finish(
        result(
            1,
            alpha_target,
            status=CollectionStatus.FAILED,
            reason=reason,
            diagnostic=diagnostic,
        )
    )

    assert controller.starts == [(alpha_target, 10)]
    assert blocked == [diagnostic]
    assert coordinator.busy

    session.set_state(SessionState.SIGNED_OUT)
    session.set_state(SessionState.SIGNED_IN)

    beta_target = CollectionTarget.for_source(beta.id, beta.handle, beta.profile_url)
    assert controller.starts == [(alpha_target, 10), (beta_target, 10)]


def test_cancel_cancels_active_once_and_clears_collect_all_queue(tmp_path) -> None:
    sources, controller, _session, _login, coordinator = build_coordinator(tmp_path)
    alpha = source(sources, "alpha")
    beta = source(sources, "beta")

    coordinator.collect_all((alpha, beta), 10, parent=None)
    coordinator.cancel()
    coordinator.cancel()

    assert controller.cancel_count == 1
    assert coordinator.state.queue_total == 0


def test_for_you_run_ids_are_newest_first_and_unique(tmp_path) -> None:
    _sources, controller, _session, _login, coordinator = build_coordinator(tmp_path)
    target = CollectionTarget.for_you()

    coordinator.collect_for_you(10, interactive=True, parent=None)
    controller.finish(result(7, target))
    coordinator.collect_for_you(10, interactive=True, parent=None)
    controller.finish(result(8, target))
    coordinator.collect_for_you(10, interactive=True, parent=None)
    controller.finish(result(8, target))

    assert coordinator.current_for_you_run_ids == (8, 7)


def test_interactive_signed_out_request_opens_one_login_and_resumes_exactly_once(
    tmp_path, qtbot
) -> None:
    _sources, controller, session, login, coordinator = build_coordinator(
        tmp_path, SessionState.SIGNED_OUT
    )
    parent = QWidget()
    qtbot.addWidget(parent)

    assert coordinator.collect_for_you(20, interactive=True, parent=parent)
    assert coordinator.busy
    assert controller.starts == []
    assert len(login.dialogs) == 1
    assert login.parents == [parent]

    session.set_state(SessionState.SIGNED_IN)
    login.dialogs[0].signed_in.emit()
    login.dialogs[0].signed_in.emit()

    assert controller.starts == [(CollectionTarget.for_you(), 20)]
    assert login.dialogs[0].delete_count == 1


@pytest.mark.parametrize("state", [SessionState.SIGNED_OUT, SessionState.UNAVAILABLE])
def test_automatic_request_skips_without_opening_login(tmp_path, state: SessionState) -> None:
    _sources, controller, _session, login, coordinator = build_coordinator(tmp_path, state)
    skipped: list[str] = []
    coordinator.request_skipped.connect(skipped.append)

    accepted = coordinator.collect_for_you(10, interactive=False, parent=None)

    assert not accepted
    assert controller.starts == []
    assert login.dialogs == []
    assert skipped == ["Automatic collection skipped because Opera/X is not signed in"]


def test_progress_state_exposes_target_count_and_collect_all_position(tmp_path) -> None:
    sources, controller, _session, _login, coordinator = build_coordinator(tmp_path)
    alpha = source(sources, "alpha")
    beta = source(sources, "beta")
    states: list[CollectionUiState] = []
    coordinator.state_changed.connect(states.append)

    coordinator.collect_all((alpha, beta), 20, parent=None)
    controller.progress.emit(CollectionProgress(7, 4, 3, 1, 0))

    assert states[-1].active is not None
    assert states[-1].active.target.handle == "alpha"
    assert states[-1].active.maximum == 20
    assert states[-1].queue_index == 1
    assert states[-1].queue_total == 2
    assert states[-1].progress == CollectionProgress(7, 4, 3, 1, 0)
    assert "@alpha" in states[-1].message
    assert "1 of 2" in states[-1].message


def test_manual_resolution_ensures_once_and_claims_only_when_dispatched(tmp_path) -> None:
    (
        _sources,
        posts,
        media,
        _manifest,
        controller,
        resolver,
        _session,
        _login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    saved = insert_post(posts)

    assert coordinator.collect_for_you(10, interactive=True, parent=None)
    assert coordinator.resolve_post_photos(saved, None)

    assert media.ensure_calls == [(saved.id, saved.canonical_url, MediaOrigin.MANUAL)]
    assert media.claim_calls == []
    assert resolver.starts == []

    controller.finish(result(1, CollectionTarget.for_you()))

    assert media.claim_calls == [1]
    assert [job.id for job in resolver.starts] == [1]


def test_terminal_manual_manifest_prevents_another_browser_job(tmp_path) -> None:
    (
        _sources,
        posts,
        media,
        manifest,
        _controller,
        resolver,
        _session,
        _login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    saved = insert_post(posts)
    manifest.record_manual_manifest(saved, ())
    media.ensure_calls.clear()

    assert coordinator.resolve_post_photos(saved, None) is MediaResolutionRequestOutcome.RESOLVED

    assert media.ensure_calls == [(saved.id, saved.canonical_url, MediaOrigin.MANUAL)]
    assert media.claim_calls == []
    assert resolver.starts == []


def test_permanently_failed_manual_manifest_reports_failed_outcome(tmp_path) -> None:
    (
        _sources,
        posts,
        media,
        _manifest,
        _controller,
        resolver,
        _session,
        _login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    saved = insert_post(posts)

    assert coordinator.resolve_post_photos(saved, None)
    resolver.fail("permanent fixture failure")
    resolver.fail("permanent fixture failure")
    resolver.fail("permanent fixture failure")

    assert coordinator.resolve_post_photos(saved, None) is MediaResolutionRequestOutcome.FAILED
    assert not coordinator.resolve_post_photos(saved, None)
    assert len(resolver.starts) == 3


def test_active_resolution_finishes_before_a_later_collection(tmp_path) -> None:
    (
        _sources,
        posts,
        _media,
        _manifest,
        controller,
        resolver,
        _session,
        _login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    saved = insert_post(posts)

    assert coordinator.resolve_post_photos(saved, None)
    assert coordinator.collect_for_you(10, interactive=True, parent=None)
    assert len(resolver.starts) == 1
    assert controller.starts == []

    resolver.finish(())

    assert controller.starts == [(CollectionTarget.for_you(), 10)]
    assert not resolver.active


def test_browser_controllers_never_overlap_across_mixed_fifo_work(tmp_path) -> None:
    (
        sources,
        posts,
        _media,
        _manifest,
        controller,
        resolver,
        _session,
        _login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    saved = insert_post(posts)
    alpha = source(sources, "alpha")
    alpha_target = CollectionTarget.for_source(alpha.id, alpha.handle, alpha.profile_url)

    assert coordinator.collect_source(alpha, 10, parent=None)
    assert coordinator.resolve_post_photos(saved, None)
    assert controller.active and not resolver.active

    controller.finish(result(1, alpha_target))
    assert resolver.active and not controller.active

    assert coordinator.collect_for_you(10, interactive=True, parent=None)
    assert resolver.active and not controller.active
    resolver.finish(())
    assert controller.active and not resolver.active


@pytest.mark.parametrize(
    "photos",
    [
        (),
        (
            PhotoCandidate(
                0,
                "https://pbs.twimg.com/media/example?format=jpg&name=small",
                "Example",
            ),
        ),
    ],
)
def test_successful_resolution_records_manual_manifest_and_resolves_job(
    tmp_path,
    photos: tuple[PhotoCandidate, ...],
) -> None:
    (
        _sources,
        posts,
        media,
        manifest,
        _controller,
        resolver,
        _session,
        _login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    saved = insert_post(posts)

    coordinator.resolve_post_photos(saved, None)
    resolver.finish(photos)

    assert manifest.manual_calls == [(saved, photos)]
    row = resolution_row(media.database, saved.id)
    assert tuple(row) == (
        MediaResolutionState.RESOLVED.value,
        len(photos),
        1,
        None,
    )
    assert not coordinator.busy
    assert coordinator.state.message == "Photo lookup complete"


def test_manifest_persistence_failure_requeues_resolution_instead_of_completing(
    tmp_path,
) -> None:
    (
        _sources,
        posts,
        media,
        manifest,
        _controller,
        resolver,
        _session,
        _login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    saved = insert_post(posts)
    media.manifest_failures_remaining = 1
    diagnostics: list[tuple[int, str]] = []
    manifest.manifest_failed.connect(
        lambda post_id, message: diagnostics.append((post_id, message))
    )

    coordinator.resolve_post_photos(saved, None)
    resolver.finish(())

    row = resolution_row(media.database, saved.id)
    assert tuple(row) == (
        MediaResolutionState.RESOLVING.value,
        None,
        2,
        "media database unavailable",
    )
    assert len(resolver.starts) == 2
    assert coordinator.busy
    assert diagnostics == [(saved.id, "media database unavailable")]

    resolver.finish(())

    row = resolution_row(media.database, saved.id)
    assert tuple(row) == (
        MediaResolutionState.RESOLVED.value,
        0,
        2,
        None,
    )
    assert coordinator.state.message == "Photo lookup complete"


def test_temporary_resolution_failure_retries_three_claims_then_fails(tmp_path) -> None:
    (
        _sources,
        posts,
        media,
        _manifest,
        _controller,
        resolver,
        _session,
        _login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    saved = insert_post(posts)

    coordinator.resolve_post_photos(saved, None)
    resolver.fail("Photo resolution timed out")
    resolver.fail("Photo resolution timed out")

    assert len(resolver.starts) == 3
    assert len(media.requeue_calls) == 2

    resolver.fail("Photo resolution timed out")

    assert len(resolver.starts) == 3
    assert len(media.fail_calls) == 1
    row = resolution_row(media.database, saved.id)
    assert tuple(row) == (
        MediaResolutionState.FAILED.value,
        None,
        3,
        "Photo resolution timed out",
    )
    assert not coordinator.busy
    assert coordinator.state.message == "Photo resolution timed out"


@pytest.mark.parametrize(
    "diagnostic",
    [
        "X presented an account challenge in Opera GX",
        "X rate-limited the Opera GX session",
        "X is signed out in Opera GX; sign in and reconnect",
    ],
)
def test_resolution_session_blocker_pauses_without_bypassing_queue(
    tmp_path,
    diagnostic: str,
) -> None:
    (
        _sources,
        posts,
        media,
        _manifest,
        controller,
        resolver,
        session,
        login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    saved = insert_post(posts)
    blocked: list[str] = []
    coordinator.session_blocked.connect(blocked.append)

    coordinator.resolve_post_photos(saved, None)
    coordinator.collect_for_you(10, interactive=True, parent=None)
    resolver.fail(diagnostic)

    assert len(resolver.starts) == 1
    assert controller.starts == []
    assert blocked == [diagnostic]
    assert len(media.requeue_calls) == 1
    assert login.dialogs == []

    session.set_state(SessionState.SIGNED_OUT)
    session.set_state(SessionState.SIGNED_IN)

    assert len(resolver.starts) == 2
    assert controller.starts == []
    assert login.dialogs == []

    resolver.finish(())
    assert controller.starts == [(CollectionTarget.for_you(), 10)]


def test_signed_out_signal_before_resolution_blocker_terminal_resumes_once(
    tmp_path,
) -> None:
    (
        _sources,
        posts,
        media,
        _manifest,
        controller,
        resolver,
        session,
        login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    saved = insert_post(posts)
    blocked: list[str] = []
    coordinator.session_blocked.connect(blocked.append)

    assert coordinator.resolve_post_photos(saved, None)
    assert coordinator.collect_for_you(10, interactive=True, parent=None)

    session.set_state(SessionState.SIGNED_OUT)
    resolver.fail("X is signed out in Opera GX; sign in and reconnect")

    assert len(resolver.starts) == 1
    assert controller.starts == []
    assert len(media.requeue_calls) == 1

    session.set_state(SessionState.SIGNED_IN)
    session.set_state(SessionState.SIGNED_IN)

    assert len(resolver.starts) == 2
    assert controller.starts == []
    assert blocked == ["X is signed out in Opera GX; sign in and reconnect"]
    assert login.dialogs == []


def test_signed_out_signal_before_collection_blocker_terminal_resumes_once(
    tmp_path,
) -> None:
    (
        sources,
        _posts,
        _media,
        _manifest,
        controller,
        _resolver,
        session,
        login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    alpha = source(sources, "alpha")
    beta = source(sources, "beta")
    alpha_target = CollectionTarget.for_source(alpha.id, alpha.handle, alpha.profile_url)
    blocked: list[str] = []
    coordinator.session_blocked.connect(blocked.append)

    assert coordinator.collect_all((alpha, beta), 10, parent=None)

    session.set_state(SessionState.SIGNED_OUT)
    controller.finish(
        result(
            1,
            alpha_target,
            status=CollectionStatus.FAILED,
            reason=DiscoveryReason.LOGIN_WALL.value,
            diagnostic="X is signed out; reconnect",
        )
    )

    assert [target.handle for target, _maximum in controller.starts] == ["alpha"]

    session.set_state(SessionState.SIGNED_IN)
    session.set_state(SessionState.SIGNED_IN)

    assert [target.handle for target, _maximum in controller.starts] == ["alpha", "beta"]
    assert blocked == ["X is signed out; reconnect"]
    assert login.dialogs == []


def test_third_resolution_claim_fails_even_when_terminal_reason_blocks_session(
    tmp_path,
) -> None:
    (
        _sources,
        posts,
        media,
        _manifest,
        _controller,
        resolver,
        _session,
        _login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    saved = insert_post(posts)
    job = media.ensure_resolution_job(saved.id, saved.canonical_url, MediaOrigin.MANUAL)
    for _attempt in range(2):
        claimed = media.claim_resolution(job.id)
        assert claimed is not None
        media.requeue_resolution(job.id, "Earlier temporary failure")
    media.ensure_calls.clear()
    media.claim_calls.clear()
    media.requeue_calls.clear()

    coordinator.resolve_post_photos(saved, None)
    resolver.fail("X presented an account challenge in Opera GX")

    assert media.requeue_calls == []
    assert media.fail_calls == [(job.id, "X presented an account challenge in Opera GX")]
    row = resolution_row(media.database, saved.id)
    assert tuple(row) == (
        MediaResolutionState.FAILED.value,
        None,
        3,
        "X presented an account challenge in Opera GX",
    )


def test_cancel_active_resolution_once_and_clear_later_browser_work(tmp_path) -> None:
    (
        _sources,
        posts,
        _media,
        _manifest,
        controller,
        resolver,
        _session,
        _login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    saved = insert_post(posts)

    coordinator.resolve_post_photos(saved, None)
    coordinator.collect_for_you(10, interactive=True, parent=None)
    coordinator.cancel()
    coordinator.cancel()

    assert resolver.cancel_count == 1
    assert controller.cancel_count == 0
    assert coordinator.state.queue_total == 0

    resolver.fail("Photo resolution cancelled")
    assert controller.starts == []
    assert coordinator.state.message == "Photo lookup cancelled"


def test_signed_out_pending_resume_waits_without_login_and_reconnects_once(
    tmp_path,
) -> None:
    (
        _sources,
        posts,
        media,
        _manifest,
        _controller,
        resolver,
        session,
        login,
        coordinator,
    ) = build_media_coordinator(tmp_path, SessionState.SIGNED_OUT)
    saved = insert_post(posts)
    job = media.ensure_resolution_job(saved.id, saved.canonical_url, MediaOrigin.MANUAL)
    media.ensure_calls.clear()

    assert coordinator.resume_pending_resolutions() == 1
    assert coordinator.resume_pending_resolutions() == 0
    assert resolver.starts == []
    assert media.claim_calls == []
    assert login.dialogs == []

    session.set_state(SessionState.SIGNED_IN)
    session.set_state(SessionState.SIGNED_IN)

    assert [started.id for started in resolver.starts] == [job.id]
    assert media.claim_calls == [job.id]
    assert login.dialogs == []


def test_persisted_resume_drains_more_than_one_page_before_later_collection(
    tmp_path,
) -> None:
    (
        _sources,
        posts,
        media,
        _manifest,
        controller,
        resolver,
        _session,
        _login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    for post_number in range(1, 102):
        saved = insert_post(posts, post_number)
        media.ensure_resolution_job(
            saved.id,
            saved.canonical_url,
            MediaOrigin.MANUAL,
        )
    media.ensure_calls.clear()

    assert coordinator.resume_pending_resolutions() == 100
    assert coordinator.collect_for_you(10, interactive=True, parent=None)

    for _job in range(100):
        assert resolver.active
        resolver.finish(())

    assert len(resolver.starts) == 101
    assert controller.starts == []

    resolver.finish(())

    assert controller.starts == [(CollectionTarget.for_you(), 10)]
    assert media.pending_resolution_jobs() == []
    assert media.pending_calls == [100, 100, 100, 100]


def test_persisted_resume_drains_snapshot_before_later_manual_resolution(
    tmp_path,
) -> None:
    (
        _sources,
        posts,
        media,
        _manifest,
        _controller,
        resolver,
        _session,
        _login,
        coordinator,
    ) = build_media_coordinator(tmp_path)
    for post_number in range(1, 102):
        saved = insert_post(posts, post_number)
        media.ensure_resolution_job(
            saved.id,
            saved.canonical_url,
            MediaOrigin.MANUAL,
        )
    media.ensure_calls.clear()

    assert coordinator.resume_pending_resolutions() == 100
    later = insert_post(posts, 102)
    assert coordinator.resolve_post_photos(later, None)

    for _job in range(100):
        assert resolver.active
        resolver.finish(())

    assert [job.post_id for job in resolver.starts] == list(range(1, 102))

    resolver.finish(())

    assert resolver.starts[-1].post_id == later.id
    assert [job.post_id for job in resolver.starts] == list(range(1, 103))


def test_signed_out_manual_resolutions_open_one_login_and_wait_unclaimed(
    tmp_path,
    qtbot,
) -> None:
    (
        _sources,
        posts,
        media,
        _manifest,
        _controller,
        resolver,
        session,
        login,
        coordinator,
    ) = build_media_coordinator(tmp_path, SessionState.SIGNED_OUT)
    first = insert_post(posts, 123)
    second = insert_post(posts, 124)
    parent = QWidget()
    qtbot.addWidget(parent)

    assert coordinator.resolve_post_photos(first, parent)
    assert coordinator.resolve_post_photos(second, parent)

    assert len(login.dialogs) == 1
    assert login.parents == [parent]
    assert media.claim_calls == []
    assert resolver.starts == []

    session.set_state(SessionState.SIGNED_IN)
    login.dialogs[0].signed_in.emit()

    assert [job.post_id for job in resolver.starts] == [first.id]
