from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from xfeed.collection import DiscoveryReason
from xfeed.media import (
    MediaOrigin,
    MediaResolutionJob,
    MediaResolutionState,
    PhotoCandidate,
)
from xfeed.opera_media_resolver import OperaMediaResolver
from xfeed.opera_protocol import OperaPhotoJob, OperaPhotoResolutionResult


class BridgeDouble(QObject):
    photo_job_finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[OperaPhotoJob] = []
        self.cancelled: list[str] = []
        self.enqueue_error: Exception | None = None

    def enqueue_photo_resolution(
        self,
        *,
        post_id: int,
        post_url: str,
        timeout_s: float = 45.0,
    ) -> OperaPhotoJob:
        if self.enqueue_error is not None:
            raise self.enqueue_error
        job = OperaPhotoJob(
            id=f"job-{len(self.jobs) + 1}",
            post_id=str(post_id),
            post_url=post_url,
            deadline_at=timeout_s,
        )
        self.jobs.append(job)
        return job

    def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)
        self.photo_job_finished.emit(
            OperaPhotoResolutionResult(
                job_id,
                DiscoveryReason.CANCELLED,
                (),
            )
        )


def resolution_job(
    *,
    job_id: int = 7,
    post_id: int = 123,
    state: MediaResolutionState = MediaResolutionState.RESOLVING,
) -> MediaResolutionJob:
    return MediaResolutionJob(
        id=job_id,
        post_id=post_id,
        canonical_url=f"https://x.com/openai/status/{post_id}",
        origin=MediaOrigin.MANUAL,
        state=state,
        manifest_count=None,
        attempts=1,
        diagnostic=None,
        created_at="2026-07-24 00:00:00",
        updated_at="2026-07-24 00:00:00",
    )


def capture(
    resolver: OperaMediaResolver,
) -> tuple[list[tuple[PhotoCandidate, ...]], list[str], list[bool]]:
    completed: list[tuple[PhotoCandidate, ...]] = []
    failed: list[str] = []
    active: list[bool] = []
    resolver.completed.connect(completed.append)
    resolver.failed.connect(failed.append)
    resolver.active_changed.connect(active.append)
    return completed, failed, active


def test_start_forwards_an_already_claimed_job_and_exposes_active_state(qtbot) -> None:
    bridge = BridgeDouble()
    resolver = OperaMediaResolver(bridge, timeout_s=12.5)  # type: ignore[arg-type]
    job = resolution_job()
    _completed, _failed, active = capture(resolver)

    resolver.start(job)

    assert resolver.active
    assert resolver.active_job == job
    assert bridge.jobs == [
        OperaPhotoJob(
            id="job-1",
            post_id="123",
            post_url="https://x.com/openai/status/123",
            deadline_at=12.5,
        )
    ]
    assert active == [True]


def test_success_matches_bridge_job_id_and_emits_one_ordered_manifest(qtbot) -> None:
    bridge = BridgeDouble()
    resolver = OperaMediaResolver(bridge)  # type: ignore[arg-type]
    completed, failed, active = capture(resolver)
    resolver.start(resolution_job())
    browser_job = bridge.jobs[-1]
    photos = (
        PhotoCandidate(0, "https://pbs.twimg.com/media/a?name=large", "A"),
        PhotoCandidate(1, "https://pbs.twimg.com/media/b?name=large", None),
    )

    bridge.photo_job_finished.emit(
        OperaPhotoResolutionResult(
            "stale-job",
            DiscoveryReason.EXHAUSTED,
            photos,
        )
    )
    bridge.photo_job_finished.emit(
        OperaPhotoResolutionResult(
            browser_job.id,
            DiscoveryReason.EXHAUSTED,
            photos,
        )
    )
    bridge.photo_job_finished.emit(
        OperaPhotoResolutionResult(
            browser_job.id,
            DiscoveryReason.ERROR,
            (),
            "duplicate",
        )
    )

    assert completed == [photos]
    assert failed == []
    assert active == [True, False]
    assert not resolver.active
    assert resolver.active_job is None


def test_non_success_terminal_emits_one_bounded_failure(qtbot) -> None:
    bridge = BridgeDouble()
    resolver = OperaMediaResolver(bridge)  # type: ignore[arg-type]
    completed, failed, active = capture(resolver)
    resolver.start(resolution_job())
    browser_job = bridge.jobs[-1]

    bridge.photo_job_finished.emit(
        OperaPhotoResolutionResult(
            browser_job.id,
            DiscoveryReason.ERROR,
            (),
            "x" * 600,
        )
    )

    assert completed == []
    assert failed == ["x" * 500]
    assert active == [True, False]


def test_cancel_delegates_at_most_once_and_ignores_late_terminals(qtbot) -> None:
    bridge = BridgeDouble()
    resolver = OperaMediaResolver(bridge)  # type: ignore[arg-type]
    completed, failed, active = capture(resolver)
    resolver.start(resolution_job())
    browser_job = bridge.jobs[-1]

    resolver.cancel()
    resolver.cancel()
    bridge.photo_job_finished.emit(
        OperaPhotoResolutionResult(
            browser_job.id,
            DiscoveryReason.EXHAUSTED,
            (PhotoCandidate(0, "https://pbs.twimg.com/media/late?name=large"),),
        )
    )

    assert bridge.cancelled == [browser_job.id]
    assert completed == []
    assert failed == ["Photo resolution cancelled"]
    assert active == [True, False]


def test_start_does_not_replace_an_active_job(qtbot) -> None:
    bridge = BridgeDouble()
    resolver = OperaMediaResolver(bridge)  # type: ignore[arg-type]
    first = resolution_job()

    resolver.start(first)
    resolver.start(resolution_job(job_id=8, post_id=456))

    assert resolver.active_job == first
    assert len(bridge.jobs) == 1
    assert bridge.cancelled == []


def test_start_rejects_unclaimed_job_without_mutating_any_repository(qtbot) -> None:
    bridge = BridgeDouble()
    resolver = OperaMediaResolver(bridge)  # type: ignore[arg-type]
    completed, failed, active = capture(resolver)

    resolver.start(
        replace(
            resolution_job(),
            state=MediaResolutionState.PENDING,
        )
    )

    assert completed == []
    assert failed == ["Media resolution job must already be claimed"]
    assert active == []
    assert bridge.jobs == []


def test_enqueue_failure_is_one_terminal_without_active_announcement(qtbot) -> None:
    bridge = BridgeDouble()
    bridge.enqueue_error = RuntimeError("Opera unavailable")
    resolver = OperaMediaResolver(bridge)  # type: ignore[arg-type]
    completed, failed, active = capture(resolver)

    resolver.start(resolution_job())

    assert completed == []
    assert failed == ["Opera unavailable"]
    assert active == []
    assert not resolver.active
