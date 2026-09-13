import math

from PySide6.QtCore import QObject, Signal, Slot

from xfeed.collection import DiscoveryReason
from xfeed.media import MediaResolutionJob, MediaResolutionState
from xfeed.opera_bridge import OperaBridge
from xfeed.opera_protocol import OperaPhotoResolutionResult
from xfeed.urls import InvalidPostUrl, canonicalize_post_url


class OperaMediaResolver(QObject):
    completed = Signal(object)
    failed = Signal(str)
    active_changed = Signal(bool)

    def __init__(
        self,
        bridge: OperaBridge,
        *,
        timeout_s: float = 45.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, int | float)
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be a positive finite number")
        self._bridge = bridge
        self._timeout_s = float(timeout_s)
        self._active_job: MediaResolutionJob | None = None
        self._bridge_job_id: str | None = None
        self._cancel_requested = False
        self._bridge.photo_job_finished.connect(self._on_photo_job_finished)

    @property
    def active(self) -> bool:
        return self._active_job is not None

    @property
    def active_job(self) -> MediaResolutionJob | None:
        return self._active_job

    def start(self, job: MediaResolutionJob) -> None:
        if self.active:
            return
        if not isinstance(job, MediaResolutionJob):
            self.failed.emit("Invalid media resolution job")
            return
        if job.state is not MediaResolutionState.RESOLVING:
            self.failed.emit("Media resolution job must already be claimed")
            return

        self._active_job = job
        self._bridge_job_id = None
        self._cancel_requested = False
        try:
            canonical_post = canonicalize_post_url(job.canonical_url)
            bridge_job = self._bridge.enqueue_photo_resolution(
                post_id=int(canonical_post.post_id),
                post_url=canonical_post.url,
                timeout_s=self._timeout_s,
            )
        except (InvalidPostUrl, TypeError, ValueError, RuntimeError) as error:
            self._clear_active()
            self.failed.emit(_diagnostic(error))
            return
        self._bridge_job_id = bridge_job.id
        self.active_changed.emit(True)

    def cancel(self) -> None:
        if not self.active or self._cancel_requested:
            return
        self._cancel_requested = True
        job_id = self._bridge_job_id
        if job_id is None:
            self._terminal_failure("Photo resolution cancelled")
            return
        try:
            self._bridge.cancel(job_id)
        except RuntimeError as error:
            self._terminal_failure(_diagnostic(error))

    @Slot(object)
    def _on_photo_job_finished(self, result: object) -> None:
        if (
            not isinstance(result, OperaPhotoResolutionResult)
            or not self.active
            or result.job_id != self._bridge_job_id
        ):
            return
        if result.reason is DiscoveryReason.EXHAUSTED:
            photos = result.photos
            self._finish_active()
            self.completed.emit(photos)
            return
        self._terminal_failure(result.diagnostic or _reason_diagnostic(result.reason))

    def _terminal_failure(self, diagnostic: str) -> None:
        if not self.active:
            return
        self._finish_active()
        self.failed.emit(_diagnostic(diagnostic))

    def _finish_active(self) -> None:
        self._clear_active()
        self.active_changed.emit(False)

    def _clear_active(self) -> None:
        self._active_job = None
        self._bridge_job_id = None
        self._cancel_requested = False


def _reason_diagnostic(reason: DiscoveryReason) -> str:
    if reason is DiscoveryReason.CANCELLED:
        return "Photo resolution cancelled"
    if reason is DiscoveryReason.TIMEOUT:
        return "Photo resolution timed out"
    if reason is DiscoveryReason.LOGIN_WALL:
        return "X is signed out in Opera GX; sign in and reconnect"
    return "Photo resolution failed"


def _diagnostic(value: object) -> str:
    diagnostic = str(value).replace("\x00", "").strip()
    return (diagnostic or "Photo resolution failed")[:500]
