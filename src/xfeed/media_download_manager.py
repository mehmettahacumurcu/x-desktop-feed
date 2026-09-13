from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Protocol, cast

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, SignalInstance, Slot

from xfeed.media import MediaAsset
from xfeed.media_download import (
    DownloadedPhoto,
    DownloadFailure,
    DownloadSuperseded,
    PhotoDownloader,
)
from xfeed.media_repository import MediaRepository


class DownloadExecutor(Protocol):
    succeeded: SignalInstance
    failed: SignalInstance

    def submit(
        self,
        asset_id: int,
        operation: Callable[[], DownloadedPhoto],
    ) -> None: ...


class RetryTimer(Protocol):
    timeout: SignalInstance

    def setSingleShot(self, single_shot: bool) -> None: ...

    def start(self, delay_ms: int) -> None: ...

    def stop(self) -> None: ...

    def deleteLater(self) -> None: ...


class _DownloadRunnable(QRunnable):
    def __init__(
        self,
        asset_id: int,
        operation: Callable[[], DownloadedPhoto],
        succeeded: SignalInstance,
        failed: SignalInstance,
    ) -> None:
        super().__init__()
        self._asset_id = asset_id
        self._operation = operation
        self._succeeded = succeeded
        self._failed = failed

    def run(self) -> None:
        try:
            result = self._operation()
        except Exception as error:
            self._failed.emit(self._asset_id, error)
            return
        self._succeeded.emit(self._asset_id, result)


class _QtDownloadExecutor(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, object)

    def submit(
        self,
        asset_id: int,
        operation: Callable[[], DownloadedPhoto],
    ) -> None:
        worker = _DownloadRunnable(asset_id, operation, self.succeeded, self.failed)
        QThreadPool.globalInstance().start(worker)


class MediaDownloadManager(QObject):
    asset_changed = Signal(int)
    status_changed = Signal(str)

    def __init__(
        self,
        repository: MediaRepository,
        downloader: PhotoDownloader,
        parent: QObject | None = None,
        *,
        maximum_concurrency: int = 2,
        retry_delay_ms: int = 1500,
        executor: DownloadExecutor | None = None,
        retry_timer_factory: Callable[[], RetryTimer] | None = None,
    ) -> None:
        super().__init__(parent)
        if type(maximum_concurrency) is not int or maximum_concurrency < 1:
            raise ValueError("maximum_concurrency must be a positive integer")
        if type(retry_delay_ms) is not int or retry_delay_ms < 1:
            raise ValueError("retry_delay_ms must be a positive integer")
        self.repository = repository
        self.downloader = downloader
        self.maximum_concurrency = maximum_concurrency
        self.retry_delay_ms = retry_delay_ms
        self._executor = executor or _QtDownloadExecutor(self)
        self._retry_timer_factory = retry_timer_factory or self._new_retry_timer
        self._active: dict[int, MediaAsset] = {}
        self._retry_waiting: set[int] = set()
        self._retry_timers: dict[int, RetryTimer] = {}
        self._retry_callbacks: dict[int, Callable[[], None]] = {}
        self._running = False
        self._initial_reset_done = False
        self._scheduling = False
        self._schedule_requested = False
        self._executor.succeeded.connect(self._download_succeeded)
        self._executor.failed.connect(self._download_failed)

    @Slot()
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        if not self._initial_reset_done:
            self._initial_reset_done = True
            try:
                self.repository.reset_interrupted()
            except Exception as error:
                self.status_changed.emit(f"Could not resume photo downloads: {_diagnostic(error)}")
        self.schedule()

    @Slot()
    def stop(self) -> None:
        self._running = False

    @Slot()
    def schedule(self) -> None:
        if not self._running:
            return
        if self._scheduling:
            self._schedule_requested = True
            return
        self._scheduling = True
        try:
            while self._running and len(self._active) < self.maximum_concurrency:
                claimable = self.repository.pending_assets(
                    limit=max(100, len(self._retry_waiting) + self.maximum_concurrency)
                )
                dispatched = False
                for pending in claimable:
                    if len(self._active) >= self.maximum_concurrency or not self._running:
                        break
                    if pending.id in self._active or pending.id in self._retry_waiting:
                        continue
                    claimed = self.repository.claim_asset(pending.id)
                    if claimed is None:
                        continue
                    self._active[claimed.id] = claimed
                    dispatched = True
                    try:
                        self._executor.submit(
                            claimed.id,
                            partial(
                                self.downloader.download,
                                claimed,
                                publication_guard=self.repository.guard_asset_publication,
                            ),
                        )
                    except Exception as error:
                        self._download_failed(claimed.id, error)
                if not dispatched:
                    break
        except Exception as error:
            self.status_changed.emit(f"Could not schedule photo downloads: {_diagnostic(error)}")
        finally:
            self._scheduling = False
        if self._schedule_requested:
            self._schedule_requested = False
            self.schedule()

    @Slot(int, object)
    def _download_succeeded(self, asset_id: int, result: object) -> None:
        if asset_id not in self._active:
            return
        try:
            if not isinstance(result, DownloadedPhoto):
                raise TypeError("photo downloader returned an invalid result")
            self.repository.mark_asset_available(
                asset_id,
                local_path=result.relative_path,
                mime_type=result.mime_type,
                width=result.width,
                height=result.height,
            )
        except Exception as error:
            self.status_changed.emit(f"Could not save photo state: {_diagnostic(error)}")
        else:
            self.asset_changed.emit(asset_id)
        finally:
            self._release_slot(asset_id)

    @Slot(int, object)
    def _download_failed(self, asset_id: int, error: object) -> None:
        claimed = self._active.get(asset_id)
        if claimed is None:
            return
        failure = error if isinstance(error, DownloadFailure) else None
        diagnostic = _diagnostic(failure.diagnostic if failure is not None else error)
        try:
            if isinstance(failure, DownloadSuperseded):
                return
            if failure is not None and failure.retryable and claimed.attempts < 3:
                self.repository.requeue_asset(asset_id, diagnostic)
                if self._wait_before_retry(asset_id):
                    self.status_changed.emit(f"Retrying photo download: {diagnostic}")
            else:
                self.repository.fail_asset(asset_id, diagnostic)
                self.asset_changed.emit(asset_id)
                self.status_changed.emit(f"Photo download failed: {diagnostic}")
        except Exception as persistence_error:
            self.status_changed.emit(
                f"Could not save photo failure state: {_diagnostic(persistence_error)}"
            )
        finally:
            self._release_slot(asset_id)

    def _wait_before_retry(self, asset_id: int) -> bool:
        try:
            timer = self._retry_timer_factory()
        except Exception as error:
            self.status_changed.emit(f"Could not start photo retry timer: {_diagnostic(error)}")
            return False
        callback = partial(self._retry_ready, asset_id)
        self._retry_waiting.add(asset_id)
        self._retry_timers[asset_id] = timer
        self._retry_callbacks[asset_id] = callback
        try:
            timer.setSingleShot(True)
            timer.timeout.connect(callback)
            timer.start(self.retry_delay_ms)
        except Exception as error:
            if self._retry_timers.get(asset_id) is timer:
                self._dispose_retry_timer(asset_id)
            self.status_changed.emit(f"Could not start photo retry timer: {_diagnostic(error)}")
            return False
        return True

    def _retry_ready(self, asset_id: int) -> None:
        self._dispose_retry_timer(asset_id)
        self.schedule()

    def _dispose_retry_timer(self, asset_id: int) -> None:
        timer = self._retry_timers.pop(asset_id, None)
        callback = self._retry_callbacks.pop(asset_id, None)
        self._retry_waiting.discard(asset_id)
        if timer is None:
            return
        if callback is not None:
            try:
                timer.timeout.disconnect(callback)
            except (RuntimeError, TypeError, ValueError):
                pass
        try:
            timer.stop()
        except RuntimeError:
            pass
        try:
            timer.deleteLater()
        except RuntimeError:
            pass

    def _release_slot(self, asset_id: int) -> None:
        self._active.pop(asset_id, None)
        self.schedule()

    def _new_retry_timer(self) -> RetryTimer:
        return cast(RetryTimer, QTimer(self))


def _diagnostic(value: object) -> str:
    diagnostic = str(value).replace("\x00", "").strip()
    return (diagnostic or "photo download failed")[:500]
