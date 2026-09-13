from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from PySide6.QtCore import QObject, Signal, SignalInstance
from PySide6.QtWidgets import QWidget

from xfeed.collection import CollectionProgress, CollectionResult, DiscoveryReason
from xfeed.domain import CollectionTarget, CollectionTargetKind, SavedPost
from xfeed.i18n import tr
from xfeed.media import (
    MediaOrigin,
    MediaResolutionJob,
    MediaResolutionState,
    PhotoCandidate,
)
from xfeed.media_manifest import MediaManifestService
from xfeed.media_repository import MediaRepository
from xfeed.repositories import PostRepository, SourceRepository
from xfeed.sources import SourceRecord
from xfeed.ui.settings import MY_FEED_COUNTS, SOURCE_COUNTS
from xfeed.x_session import SessionState


class _Controller(Protocol):
    progress: SignalInstance
    completed: SignalInstance
    failed: SignalInstance
    active_changed: SignalInstance

    @property
    def active(self) -> bool: ...

    @property
    def active_target(self) -> CollectionTarget | None: ...

    def start(self, target: CollectionTarget, maximum: int = 30) -> None: ...

    def cancel(self) -> None: ...


class _Session(Protocol):
    state_changed: SignalInstance
    verification_finished: SignalInstance

    @property
    def state(self) -> SessionState: ...

    def verify(self) -> None: ...

    def begin_login(self) -> None: ...

    def clear(self) -> None: ...


class _MediaResolver(Protocol):
    completed: SignalInstance
    failed: SignalInstance
    active_changed: SignalInstance

    @property
    def active(self) -> bool: ...

    @property
    def active_job(self) -> MediaResolutionJob | None: ...

    def start(self, job: MediaResolutionJob) -> None: ...

    def cancel(self) -> None: ...


class _LoginDialog(Protocol):
    signed_in: SignalInstance
    cancelled: SignalInstance

    def show(self) -> None: ...

    def deleteLater(self) -> None: ...


class CollectionOrigin(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    COLLECT_ALL = "collect_all"


class MediaResolutionRequestOutcome(StrEnum):
    QUEUED = "queued"
    RESOLVED = "resolved"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"

    def __bool__(self) -> bool:
        return self is MediaResolutionRequestOutcome.QUEUED


@dataclass(frozen=True)
class CollectionRequest:
    target: CollectionTarget
    maximum: int
    origin: CollectionOrigin


@dataclass(frozen=True)
class MediaResolutionRequest:
    job: MediaResolutionJob
    origin: CollectionOrigin = CollectionOrigin.MANUAL


BrowserRequest = CollectionRequest | MediaResolutionRequest


@dataclass(frozen=True)
class CollectionUiState:
    active: BrowserRequest | None
    queue_index: int
    queue_total: int
    progress: CollectionProgress
    message: str


_EMPTY_PROGRESS = CollectionProgress(0, 0, 0, 0, 0)
_AUTOMATIC_SIGNED_OUT = tr("Automatic collection skipped because Opera/X is not signed in")


class CollectionCoordinator(QObject):
    state_changed = Signal(object)
    request_completed = Signal(object)
    queue_finished = Signal()
    request_skipped = Signal(str)
    session_blocked = Signal(str)
    session_state_changed = Signal(object)

    def __init__(
        self,
        controller: _Controller,
        sources: SourceRepository,
        session: _Session,
        login_factory: Callable[[QWidget | None], _LoginDialog],
        parent: QObject | None = None,
        *,
        media_repository: MediaRepository | None = None,
        media_manifest_service: MediaManifestService | None = None,
        media_resolver: _MediaResolver | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._sources = sources
        self._session = session
        self._login_factory = login_factory
        self._media_repository = media_repository
        self._media_manifest_service = media_manifest_service
        self._media_resolver = media_resolver
        self._queue: deque[BrowserRequest] = deque()
        self._active: BrowserRequest | None = None
        self._batch_total = 0
        self._batch_index = 0
        self._progress = _EMPTY_PROGRESS
        self._message = tr("Ready")
        self._login_dialog: _LoginDialog | None = None
        self._waiting_parent: QWidget | None = None
        self._cancel_requested = False
        self._current_run_ids: dict[CollectionTargetKind, list[int]] = {
            CollectionTargetKind.FOR_YOU: [],
            CollectionTargetKind.FOLLOWING: [],
        }
        self._resolution_posts: dict[int, SavedPost] = {}
        self._session_paused = False
        self._disconnect_seen = False
        self._drain_pending_resolutions = False
        self._persisted_page_job_ids: set[int] = set()

        controller.progress.connect(self._on_progress)
        controller.completed.connect(self._on_completed)
        controller.failed.connect(self._on_failed)
        session.state_changed.connect(self._on_session_state)
        if media_resolver is not None:
            media_resolver.completed.connect(self._on_media_completed)
            media_resolver.failed.connect(self._on_media_failed)

    @property
    def busy(self) -> bool:
        return self._active is not None or bool(self._queue) or self._login_dialog is not None

    @property
    def state(self) -> CollectionUiState:
        return CollectionUiState(
            self._active,
            self._batch_index,
            self._batch_total,
            self._progress,
            self._message,
        )

    @property
    def current_for_you_run_ids(self) -> tuple[int, ...]:
        return self.current_run_ids(CollectionTargetKind.FOR_YOU)

    def current_run_ids(self, kind: CollectionTargetKind) -> tuple[int, ...]:
        self._validate_home_kind(kind)
        return tuple(self._current_run_ids[kind])

    def collect_for_you(
        self,
        maximum: int,
        *,
        interactive: bool,
        parent: QWidget | None,
    ) -> bool:
        return self.collect_feed(
            CollectionTargetKind.FOR_YOU,
            maximum,
            interactive=interactive,
            parent=parent,
        )

    def collect_feed(
        self,
        kind: CollectionTargetKind,
        maximum: int,
        *,
        interactive: bool,
        parent: QWidget | None,
    ) -> bool:
        self._validate_home_kind(kind)
        self._validate_maximum(maximum, MY_FEED_COUNTS)
        origin = CollectionOrigin.MANUAL if interactive else CollectionOrigin.AUTOMATIC
        target = (
            CollectionTarget.for_you()
            if kind is CollectionTargetKind.FOR_YOU
            else CollectionTarget.following()
        )
        return self._submit(
            (CollectionRequest(target, maximum, origin),),
            interactive=interactive,
            parent=parent,
        )

    def collect_source(
        self,
        source: SourceRecord,
        maximum: int,
        *,
        parent: QWidget | None,
    ) -> bool:
        self._validate_maximum(maximum, SOURCE_COUNTS)
        target = CollectionTarget.for_source(source.id, source.handle, source.profile_url)
        return self._submit(
            (CollectionRequest(target, maximum, CollectionOrigin.MANUAL),),
            interactive=True,
            parent=parent,
        )

    def collect_all(
        self,
        sources: Sequence[SourceRecord],
        maximum: int,
        *,
        parent: QWidget | None,
    ) -> bool:
        self._validate_maximum(maximum, SOURCE_COUNTS)
        requests = tuple(
            CollectionRequest(
                CollectionTarget.for_source(item.id, item.handle, item.profile_url),
                maximum,
                CollectionOrigin.COLLECT_ALL,
            )
            for item in sources
            if item.enabled
        )
        if not requests:
            self.request_skipped.emit(tr("No enabled profiles to collect"))
            return False
        return self._submit(requests, interactive=True, parent=parent)

    def resolve_post_photos(
        self, post: SavedPost, parent: QWidget | None
    ) -> MediaResolutionRequestOutcome:
        repository = self._media_repository
        if (
            repository is None
            or self._media_manifest_service is None
            or self._media_resolver is None
        ):
            return MediaResolutionRequestOutcome.UNAVAILABLE
        job = repository.ensure_resolution_job(
            post.id,
            post.canonical_url,
            MediaOrigin.MANUAL,
        )
        if job.state is MediaResolutionState.RESOLVED:
            return MediaResolutionRequestOutcome.RESOLVED
        if job.state is MediaResolutionState.FAILED:
            return MediaResolutionRequestOutcome.FAILED
        if job.state is MediaResolutionState.RESOLVING:
            return (
                MediaResolutionRequestOutcome.QUEUED
                if self._has_resolution(job.id)
                else MediaResolutionRequestOutcome.UNAVAILABLE
            )
        self._resolution_posts[job.id] = post
        self._enqueue_resolution(job)
        if self._session.state is not SessionState.SIGNED_IN:
            self._waiting_parent = parent
            self._message = tr("Waiting for Opera/X connection")
            self._emit_state()
            self._open_login(parent)
            return MediaResolutionRequestOutcome.QUEUED
        self._pump()
        return MediaResolutionRequestOutcome.QUEUED

    def resume_pending_resolutions(self) -> int:
        repository = self._media_repository
        if (
            repository is None
            or self._media_manifest_service is None
            or self._media_resolver is None
        ):
            return 0
        self._drain_pending_resolutions = True
        added, found = self._load_pending_resolution_page(prepend=False)
        if not found:
            self._drain_pending_resolutions = False
        if added:
            self._pump()
        return added

    def cancel(self) -> None:
        self._queue.clear()
        self._batch_total = 0
        self._batch_index = 0
        if self._login_dialog is not None:
            self._dispose_login()
        self._session_paused = False
        self._disconnect_seen = False
        self._drain_pending_resolutions = False
        self._persisted_page_job_ids.clear()
        if self._active is not None and not self._cancel_requested:
            self._cancel_requested = True
            if isinstance(self._active, CollectionRequest):
                self._message = tr("Cancelling collection...")
                self._controller.cancel()
            else:
                self._message = tr("Cancelling photo lookup...")
                resolver = self._media_resolver
                if resolver is not None:
                    resolver.cancel()
        elif self._active is None:
            self._message = tr("Collection cancelled")
        self._emit_state()

    def connect_session(self, parent: QWidget | None) -> None:
        if self._login_dialog is None:
            self._open_login(parent)

    def disconnect_session(self) -> None:
        self.cancel()
        self._session.clear()

    def _submit(
        self,
        requests: Sequence[CollectionRequest],
        *,
        interactive: bool,
        parent: QWidget | None,
    ) -> bool:
        if self.busy:
            if self._can_enqueue_automatic_home_requests(requests):
                self._extend_queue(requests)
                return True
            if not isinstance(self._active, MediaResolutionRequest):
                self.request_skipped.emit(tr("Another collection is already running"))
                return False
            self._extend_queue(requests)
            return True
        if self._session.state is not SessionState.SIGNED_IN:
            if not interactive:
                self.request_skipped.emit(_AUTOMATIC_SIGNED_OUT)
                return False
            self._extend_queue(requests)
            self._waiting_parent = parent
            self._message = tr("Waiting for Opera/X connection")
            self._emit_state()
            self._open_login(parent)
            return True

        self._extend_queue(requests)
        self._pump()
        return True

    def _pump(self) -> None:
        if self._active is not None or self._login_dialog is not None or self._session_paused:
            return
        if self._session.state is not SessionState.SIGNED_IN:
            if self._queue:
                self._message = tr("Waiting for Opera/X connection")
                self._emit_state()
            return
        checked_pending_page = False
        while True:
            if (
                self._drain_pending_resolutions
                and not checked_pending_page
                and not self._persisted_page_job_ids
            ):
                _added, found = self._load_pending_resolution_page(prepend=True)
                checked_pending_page = True
                if not found:
                    self._drain_pending_resolutions = False
            if not self._queue:
                break
            request = self._queue.popleft()
            self._batch_index += 1
            if isinstance(request, CollectionRequest):
                if not self._still_enabled(request):
                    continue
                self._activate(request)
                self._controller.start(request.target, request.maximum)
                return
            repository = self._media_repository
            resolver = self._media_resolver
            if repository is None or resolver is None:
                self._persisted_page_job_ids.discard(request.job.id)
                continue
            claimed = repository.claim_resolution(request.job.id)
            if claimed is None:
                self._persisted_page_job_ids.discard(request.job.id)
                continue
            active = MediaResolutionRequest(claimed, request.origin)
            self._activate(active)
            resolver.start(claimed)
            return
        self._finish_batch()

    def _activate(self, request: BrowserRequest) -> None:
        self._active = request
        self._cancel_requested = False
        self._progress = _EMPTY_PROGRESS
        self._message = self._describe(request)
        self._emit_state()

    def _still_enabled(self, request: CollectionRequest) -> bool:
        if request.target.kind is not CollectionTargetKind.SOURCE:
            return True
        source_id = request.target.source_id
        return any(item.id == source_id and item.enabled for item in self._sources.list_all())

    def _on_progress(self, value: object) -> None:
        if not isinstance(self._active, CollectionRequest) or not isinstance(
            value, CollectionProgress
        ):
            return
        self._progress = value
        self._message = self._describe(self._active)
        self._emit_state()

    def _on_completed(self, value: object) -> None:
        request = self._active
        if not isinstance(request, CollectionRequest) or not isinstance(value, CollectionResult):
            return
        self._active = None
        self._progress = value.progress
        kind = value.run.target_kind
        if kind in self._current_run_ids:
            run_id = value.run.id
            existing_ids = self._current_run_ids[kind]
            self._current_run_ids[kind] = [
                run_id,
                *(existing for existing in existing_ids if existing != run_id),
            ]
        self.request_completed.emit(value)
        if self._is_session_blocker(value):
            diagnostic = value.run.diagnostic or tr("X is signed out; reconnect")
            self._stop_for_session(diagnostic)
            return
        self._pump()

    def _on_failed(self, message: str) -> None:
        if not isinstance(self._active, CollectionRequest):
            return
        self._active = None
        self._queue.clear()
        self._batch_total = 0
        self._batch_index = 0
        self._drain_pending_resolutions = False
        self._persisted_page_job_ids.clear()
        self._message = message or tr("Collection failed")
        self._emit_state()
        self.session_blocked.emit(self._message)

    def _on_session_state(self, state: object) -> None:
        self.session_state_changed.emit(state)
        if state is not SessionState.SIGNED_IN:
            if self._session_paused:
                self._disconnect_seen = True
            return
        if self._session_paused:
            if not self._disconnect_seen:
                return
            self._session_paused = False
            self._disconnect_seen = False
            self.resume_pending_resolutions()
        self._pump()

    def _open_login(self, parent: QWidget | None) -> None:
        if self._login_dialog is not None:
            return
        dialog = self._login_factory(parent)
        self._login_dialog = dialog
        dialog.signed_in.connect(self._on_login_signed_in)
        dialog.cancelled.connect(self._on_login_cancelled)
        dialog.show()

    def _on_login_signed_in(self) -> None:
        if self._login_dialog is None:
            return
        self._dispose_login()
        if self._session.state is not SessionState.SIGNED_IN:
            self._queue.clear()
            self._drain_pending_resolutions = False
            self._persisted_page_job_ids.clear()
            self._finish_batch(tr("Opera connected, but X is not signed in"))
            return
        self._session_paused = False
        self._disconnect_seen = False
        self.resume_pending_resolutions()
        self._pump()

    def _on_login_cancelled(self) -> None:
        if self._login_dialog is None:
            return
        self._dispose_login()
        self._queue.clear()
        self._drain_pending_resolutions = False
        self._persisted_page_job_ids.clear()
        self._finish_batch(tr("Connection cancelled"))

    def _dispose_login(self) -> None:
        dialog = self._login_dialog
        self._login_dialog = None
        self._waiting_parent = None
        if dialog is None:
            return
        try:
            dialog.signed_in.disconnect(self._on_login_signed_in)
        except (RuntimeError, TypeError):
            pass
        try:
            dialog.cancelled.disconnect(self._on_login_cancelled)
        except (RuntimeError, TypeError):
            pass
        dialog.deleteLater()

    def _stop_for_session(self, diagnostic: str) -> None:
        if not self._queue:
            self._batch_total = 0
            self._batch_index = 0
        self._session_paused = True
        self._disconnect_seen = self._session.state is not SessionState.SIGNED_IN
        self._message = diagnostic
        self._emit_state()
        self.session_blocked.emit(diagnostic)

    def _finish_batch(self, message: str = "Collection complete") -> None:
        had_batch = self._batch_total > 0
        self._drain_pending_resolutions = False
        self._persisted_page_job_ids.clear()
        self._batch_total = 0
        self._batch_index = 0
        self._progress = _EMPTY_PROGRESS
        self._message = tr(message)
        self._emit_state()
        if had_batch:
            self.queue_finished.emit()

    def _describe(self, request: BrowserRequest) -> str:
        if isinstance(request, MediaResolutionRequest):
            position = (
                tr(" · {index} of {total}").format(index=self._batch_index, total=self._batch_total)
                if self._batch_total > 1
                else ""
            )
            return f"{tr('Looking up saved-post photos')}{position}"
        if request.target.kind is CollectionTargetKind.FOR_YOU:
            label = tr("Collecting For You")
        elif request.target.kind is CollectionTargetKind.FOLLOWING:
            label = tr("Collecting Following")
        else:
            label = tr("Collecting @{handle}").format(handle=request.target.handle)
        position = (
            tr(" · {index} of {total}").format(index=self._batch_index, total=self._batch_total)
            if self._batch_total > 1
            else ""
        )
        return f"{label}{position}"

    def _on_media_completed(self, value: object) -> None:
        request = self._active
        service = self._media_manifest_service
        if (
            not isinstance(request, MediaResolutionRequest)
            or service is None
            or not isinstance(value, Sequence)
            or isinstance(value, str | bytes)
            or not all(isinstance(photo, PhotoCandidate) for photo in value)
        ):
            return
        photos = tuple(value)
        post = self._resolution_posts.get(request.job.id)
        if post is None:
            post = PostRepository(self._sources.database).by_id(request.job.post_id)
        if post is None:
            self._active = None
            repository = self._media_repository
            diagnostic = tr("Saved post is unavailable for photo ingestion")
            if repository is not None:
                repository.fail_resolution(
                    request.job.id,
                    diagnostic,
                )
            self._persisted_page_job_ids.discard(request.job.id)
            self._advance_after_media(diagnostic)
            return
        try:
            service.record_manual_manifest(post, photos)
        except Exception as error:
            self._on_media_failed(str(error))
            return
        self._active = None
        self._resolution_posts.pop(request.job.id, None)
        self._persisted_page_job_ids.discard(request.job.id)
        self._advance_after_media(tr("Photo lookup complete"))

    def _on_media_failed(self, message: str) -> None:
        request = self._active
        repository = self._media_repository
        if not isinstance(request, MediaResolutionRequest) or repository is None:
            return
        self._active = None
        diagnostic = message or tr("Photo resolution failed")
        if self._cancel_requested:
            repository.requeue_resolution(request.job.id, diagnostic)
            self._cancel_requested = False
            self._finish_batch(tr("Photo lookup cancelled"))
            return
        if self._is_media_session_blocker(diagnostic):
            if request.job.attempts < 3:
                repository.requeue_resolution(request.job.id, diagnostic)
                self._queue.appendleft(request)
                self._batch_index = max(0, self._batch_index - 1)
            else:
                repository.fail_resolution(request.job.id, diagnostic)
                self._resolution_posts.pop(request.job.id, None)
                self._persisted_page_job_ids.discard(request.job.id)
            self._stop_for_session(diagnostic)
            return
        if request.job.attempts < 3:
            repository.requeue_resolution(request.job.id, diagnostic)
            self._queue.appendleft(request)
            self._batch_index = max(0, self._batch_index - 1)
        else:
            repository.fail_resolution(request.job.id, diagnostic)
            self._resolution_posts.pop(request.job.id, None)
            self._persisted_page_job_ids.discard(request.job.id)
            self._advance_after_media(diagnostic)
            return
        self._pump()

    def _advance_after_media(self, message: str) -> None:
        if not self._queue and self._drain_pending_resolutions and not self._persisted_page_job_ids:
            _added, found = self._load_pending_resolution_page(prepend=False)
            if not found:
                self._drain_pending_resolutions = False
        if self._queue:
            self._pump()
        else:
            self._finish_batch(message)

    def _enqueue_resolution(self, job: MediaResolutionJob) -> bool:
        if self._has_resolution(job.id):
            return False
        self._extend_queue((MediaResolutionRequest(job),))
        return True

    def _load_pending_resolution_page(self, *, prepend: bool) -> tuple[int, bool]:
        repository = self._media_repository
        if repository is None:
            return 0, False
        jobs = repository.pending_resolution_jobs()
        requests = tuple(
            MediaResolutionRequest(job) for job in jobs if not self._has_resolution(job.id)
        )
        self._persisted_page_job_ids.update(request.job.id for request in requests)
        if prepend and requests:
            self._queue.extendleft(reversed(requests))
            self._batch_total += len(requests)
        elif requests:
            self._extend_queue(requests)
        return len(requests), bool(jobs)

    def _has_resolution(self, job_id: int) -> bool:
        active = self._active
        if isinstance(active, MediaResolutionRequest) and active.job.id == job_id:
            return True
        return any(
            isinstance(request, MediaResolutionRequest) and request.job.id == job_id
            for request in self._queue
        )

    def _extend_queue(self, requests: Sequence[BrowserRequest]) -> None:
        if not self._active and not self._queue:
            self._batch_total = 0
            self._batch_index = 0
        self._queue.extend(requests)
        self._batch_total += len(requests)

    def _emit_state(self) -> None:
        self.state_changed.emit(self.state)

    @staticmethod
    def _validate_maximum(maximum: int, allowed: Sequence[int]) -> None:
        if type(maximum) is not int or maximum not in allowed:
            raise ValueError("maximum is not a supported choice")

    @staticmethod
    def _validate_home_kind(kind: CollectionTargetKind) -> None:
        if not isinstance(kind, CollectionTargetKind) or kind not in (
            CollectionTargetKind.FOR_YOU,
            CollectionTargetKind.FOLLOWING,
        ):
            raise ValueError("kind must be a home feed")

    def _can_enqueue_automatic_home_requests(
        self,
        requests: Sequence[CollectionRequest],
    ) -> bool:
        return self._session.state is SessionState.SIGNED_IN and all(
            request.origin is CollectionOrigin.AUTOMATIC
            and request.target.kind
            in (CollectionTargetKind.FOR_YOU, CollectionTargetKind.FOLLOWING)
            for request in requests
        )

    @staticmethod
    def _is_session_blocker(result: CollectionResult) -> bool:
        diagnostic = (result.run.diagnostic or "").casefold()
        return (
            result.run.reason == DiscoveryReason.LOGIN_WALL.value
            or "account challenge" in diagnostic
            or "rate-limit" in diagnostic
        )

    @staticmethod
    def _is_media_session_blocker(diagnostic: str) -> bool:
        normalized = diagnostic.casefold()
        return (
            "account challenge" in normalized
            or "rate-limit" in normalized
            or "rate limit" in normalized
            or "signed out" in normalized
            or "login wall" in normalized
        )
