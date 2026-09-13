from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, Signal
from PySide6.QtGui import QColor, QImage

from xfeed.db import Database
from xfeed.domain import Availability, PostDraft
from xfeed.media import MediaAsset, MediaAssetState, MediaOrigin, PhotoCandidate
from xfeed.media_download import (
    DownloadedPhoto,
    DownloadFailure,
    FetchResponse,
    PhotoDownloader,
)
from xfeed.media_download_manager import MediaDownloadManager
from xfeed.media_repository import MediaRepository
from xfeed.repositories import PostRepository


class SignalDouble:
    def __init__(self, connect_error: Exception | None = None) -> None:
        self.callbacks: list[Callable[..., None]] = []
        self.connect_error = connect_error

    def connect(self, callback: Callable[..., None]) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.callbacks.append(callback)

    def disconnect(self, callback: Callable[..., None]) -> None:
        self.callbacks.remove(callback)

    def emit(self, *args: object) -> None:
        for callback in tuple(self.callbacks):
            callback(*args)


class RepositoryDouble:
    def __init__(self, assets: list[MediaAsset]) -> None:
        self.assets = {asset.id: asset for asset in assets}
        self.reset_count = 0
        self.available: list[tuple[int, DownloadedPhoto]] = []
        self.requeued: list[tuple[int, str]] = []
        self.failed: list[tuple[int, str]] = []

    def reset_interrupted(self) -> None:
        self.reset_count += 1
        for asset_id, asset in tuple(self.assets.items()):
            if asset.state is MediaAssetState.DOWNLOADING:
                self.assets[asset_id] = replace(asset, state=MediaAssetState.PENDING)

    def pending_assets(self, limit: int = 100) -> list[MediaAsset]:
        return [
            asset
            for asset in sorted(
                self.assets.values(), key=lambda item: (item.post_id, item.position)
            )
            if asset.state is MediaAssetState.PENDING
        ][:limit]

    def claim_asset(self, asset_id: int) -> MediaAsset | None:
        asset = self.assets[asset_id]
        if asset.state is not MediaAssetState.PENDING:
            return None
        claimed = replace(
            asset,
            state=MediaAssetState.DOWNLOADING,
            attempts=asset.attempts + 1,
        )
        self.assets[asset_id] = claimed
        return claimed

    @contextmanager
    def guard_asset_publication(self, asset: MediaAsset) -> Iterator[bool]:
        yield self.assets.get(asset.id) == asset

    def mark_asset_available(
        self,
        asset_id: int,
        *,
        local_path: str,
        mime_type: str,
        width: int,
        height: int,
    ) -> MediaAsset:
        if self.assets[asset_id].state is not MediaAssetState.DOWNLOADING:
            raise RuntimeError("asset is not being downloaded")
        photo = DownloadedPhoto(local_path, mime_type, width, height)
        self.available.append((asset_id, photo))
        available = replace(
            self.assets[asset_id],
            state=MediaAssetState.AVAILABLE,
            local_path=local_path,
            mime_type=mime_type,
            width=width,
            height=height,
        )
        self.assets[asset_id] = available
        return available

    def requeue_asset(self, asset_id: int, diagnostic: str) -> None:
        self.requeued.append((asset_id, diagnostic))
        self.assets[asset_id] = replace(
            self.assets[asset_id],
            state=MediaAssetState.PENDING,
            diagnostic=diagnostic,
        )

    def fail_asset(self, asset_id: int, diagnostic: str) -> MediaAsset:
        if self.assets[asset_id].state is not MediaAssetState.DOWNLOADING:
            raise RuntimeError("asset is not being downloaded")
        self.failed.append((asset_id, diagnostic))
        failed = replace(
            self.assets[asset_id],
            state=MediaAssetState.FAILED,
            diagnostic=diagnostic,
        )
        self.assets[asset_id] = failed
        return failed


class DownloaderDouble:
    def __init__(self) -> None:
        self.outcomes: dict[int, DownloadedPhoto | Exception] = {}
        self.downloaded: list[int] = []

    def download(self, asset: MediaAsset, **_kwargs: object) -> DownloadedPhoto:
        self.downloaded.append(asset.id)
        outcome = self.outcomes.get(
            asset.id,
            DownloadedPhoto(f"{asset.post_id}/photo-{asset.position}.jpg", "image/jpeg", 80, 60),
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ExecutorDouble:
    def __init__(self) -> None:
        self.succeeded = SignalDouble()
        self.failed = SignalDouble()
        self.operations: dict[int, Callable[[], DownloadedPhoto]] = {}
        self.submitted: list[int] = []
        self.submit_errors: dict[int, Exception] = {}

    def submit(self, asset_id: int, operation: Callable[[], DownloadedPhoto]) -> None:
        self.submitted.append(asset_id)
        if asset_id in self.submit_errors:
            raise self.submit_errors[asset_id]
        self.operations[asset_id] = operation

    def finish(self, asset_id: int) -> None:
        operation = self.operations.pop(asset_id)
        try:
            result = operation()
        except Exception as error:
            self.failed.emit(asset_id, error)
        else:
            self.succeeded.emit(asset_id, result)


class RetryTimerDouble:
    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        fire_on_start: bool = False,
        connect_error: Exception | None = None,
    ) -> None:
        self.timeout = SignalDouble(connect_error)
        self.single_shot: bool | None = None
        self.delay_ms: int | None = None
        self.start_error = start_error
        self.fire_on_start = fire_on_start
        self.stop_count = 0
        self.delete_later_count = 0

    def setSingleShot(self, single_shot: bool) -> None:
        self.single_shot = single_shot

    def start(self, delay_ms: int) -> None:
        self.delay_ms = delay_ms
        if self.start_error is not None:
            raise self.start_error
        if self.fire_on_start:
            self.timeout.emit()

    def stop(self) -> None:
        self.stop_count += 1

    def deleteLater(self) -> None:
        self.delete_later_count += 1

    def fire(self) -> None:
        self.timeout.emit()


class TimerFactory:
    def __init__(
        self,
        *,
        timer: RetryTimerDouble | None = None,
        error: Exception | None = None,
    ) -> None:
        self.timers: list[RetryTimerDouble] = []
        self.timer = timer
        self.error = error

    def __call__(self) -> RetryTimerDouble:
        if self.error is not None:
            raise self.error
        timer = self.timer or RetryTimerDouble()
        self.timers.append(timer)
        return timer


class ManifestEmitter(QObject):
    manifest_recorded = Signal(object)


def asset(
    asset_id: int,
    *,
    state: MediaAssetState = MediaAssetState.PENDING,
    attempts: int = 0,
) -> MediaAsset:
    return MediaAsset(
        id=asset_id,
        post_id=100 + asset_id,
        position=0,
        source_url=f"https://pbs.twimg.com/media/{asset_id}?format=jpg&name=large",
        alt_text=None,
        state=state,
        local_path=None,
        mime_type=None,
        width=None,
        height=None,
        attempts=attempts,
        diagnostic=None,
        created_at="2026-01-01 00:00:00",
        updated_at="2026-01-01 00:00:00",
    )


def manager(
    repository: RepositoryDouble,
    downloader: DownloaderDouble | None = None,
    *,
    executor: ExecutorDouble | None = None,
    timers: TimerFactory | None = None,
) -> tuple[MediaDownloadManager, DownloaderDouble, ExecutorDouble, TimerFactory]:
    selected_downloader = downloader or DownloaderDouble()
    selected_executor = executor or ExecutorDouble()
    selected_timers = timers or TimerFactory()
    result = MediaDownloadManager(
        repository,  # type: ignore[arg-type]
        selected_downloader,  # type: ignore[arg-type]
        executor=selected_executor,  # type: ignore[arg-type]
        retry_timer_factory=selected_timers,  # type: ignore[arg-type]
    )
    return result, selected_downloader, selected_executor, selected_timers


def test_start_resets_interrupted_work_and_claims_at_most_two_assets() -> None:
    repository = RepositoryDouble([asset(1), asset(2), asset(3)])
    download_manager, _downloader, executor, _timers = manager(repository)

    download_manager.start()

    assert repository.reset_count == 1
    assert executor.submitted == [1, 2]
    assert repository.assets[3].state is MediaAssetState.PENDING


def test_manifest_signal_schedules_without_dispatching_an_active_asset_twice() -> None:
    repository = RepositoryDouble([asset(1)])
    download_manager, _downloader, executor, _timers = manager(repository)
    manifest = ManifestEmitter()
    manifest.manifest_recorded.connect(download_manager.schedule)
    download_manager.start()

    manifest.manifest_recorded.emit((asset(1),))

    assert executor.submitted == [1]


def test_success_persists_available_asset_before_emitting_its_identifier() -> None:
    repository = RepositoryDouble([asset(1)])
    download_manager, downloader, executor, _timers = manager(repository)
    downloaded = DownloadedPhoto("101/photo-0.png", "image/png", 40, 30)
    downloader.outcomes[1] = downloaded
    changed_states: list[tuple[int, MediaAssetState]] = []
    download_manager.asset_changed.connect(
        lambda asset_id: changed_states.append((asset_id, repository.assets[asset_id].state))
    )
    download_manager.start()

    executor.finish(1)

    assert repository.available == [(1, downloaded)]
    assert changed_states == [(1, MediaAssetState.AVAILABLE)]


def test_retryable_failure_requeues_and_waits_on_a_one_shot_timer_before_retry() -> None:
    repository = RepositoryDouble([asset(1)])
    downloader = DownloaderDouble()
    downloader.outcomes[1] = DownloadFailure(True, "temporary")
    download_manager, _downloader, executor, timers = manager(repository, downloader)
    download_manager.start()

    executor.finish(1)

    assert repository.requeued == [(1, "temporary")]
    assert executor.submitted == [1]
    assert len(timers.timers) == 1
    assert timers.timers[0].single_shot is True
    assert timers.timers[0].delay_ms == 1500

    downloader.outcomes[1] = DownloadedPhoto("101/photo-0.jpg", "image/jpeg", 80, 60)
    timers.timers[0].fire()
    assert executor.submitted == [1, 1]
    assert timers.timers[0].stop_count == 1
    assert timers.timers[0].delete_later_count == 1
    assert timers.timers[0].timeout.callbacks == []


@pytest.mark.parametrize(
    ("attempts", "failure"),
    (
        (2, DownloadFailure(True, "last transient failure")),
        (0, DownloadFailure(False, "permanent failure")),
    ),
)
def test_third_retryable_attempt_and_permanent_failure_are_terminal(
    attempts: int, failure: DownloadFailure
) -> None:
    repository = RepositoryDouble([asset(1, attempts=attempts)])
    downloader = DownloaderDouble()
    downloader.outcomes[1] = failure
    download_manager, _downloader, executor, timers = manager(repository, downloader)
    changed: list[int] = []
    download_manager.asset_changed.connect(changed.append)
    download_manager.start()

    executor.finish(1)

    assert repository.failed == [(1, failure.diagnostic)]
    assert repository.requeued == []
    assert timers.timers == []
    assert changed == [1]


def test_stop_prevents_new_claims_but_running_workers_still_persist_results() -> None:
    repository = RepositoryDouble([asset(1), asset(2), asset(3)])
    download_manager, _downloader, executor, _timers = manager(repository)
    changed: list[int] = []
    download_manager.asset_changed.connect(changed.append)
    download_manager.start()

    download_manager.stop()
    executor.finish(1)
    executor.finish(2)

    assert [item[0] for item in repository.available] == [1, 2]
    assert repository.assets[3].state is MediaAssetState.PENDING
    assert executor.submitted == [1, 2]
    assert changed == [1, 2]


def test_stop_then_start_does_not_reset_or_duplicate_an_active_download() -> None:
    repository = RepositoryDouble([asset(1)])
    download_manager, _downloader, executor, _timers = manager(repository)
    download_manager.start()

    download_manager.stop()
    download_manager.start()

    assert repository.reset_count == 1
    assert repository.assets[1].state is MediaAssetState.DOWNLOADING
    assert executor.submitted == [1]

    executor.finish(1)
    assert [item[0] for item in repository.available] == [1]
    assert executor.submitted == [1]


def test_unexpected_worker_exception_releases_the_slot_for_another_asset() -> None:
    repository = RepositoryDouble([asset(1), asset(2), asset(3)])
    downloader = DownloaderDouble()
    downloader.outcomes[1] = RuntimeError("unexpected")
    download_manager, _downloader, executor, _timers = manager(repository, downloader)
    download_manager.start()

    executor.finish(1)

    assert repository.failed == [(1, "unexpected")]
    assert executor.submitted == [1, 2, 3]


def test_synchronous_executor_submission_failure_releases_and_refills_the_slot() -> None:
    repository = RepositoryDouble([asset(1), asset(2), asset(3)])
    executor = ExecutorDouble()
    executor.submit_errors[1] = RuntimeError("executor rejected work")
    download_manager, _downloader, _executor, _timers = manager(
        repository,
        executor=executor,
    )

    download_manager.start()

    assert repository.failed == [(1, "executor rejected work")]
    assert executor.submitted == [1, 2, 3]


def test_retry_timer_factory_failure_leaves_requeued_asset_schedulable() -> None:
    repository = RepositoryDouble([asset(1)])
    downloader = DownloaderDouble()
    downloader.outcomes[1] = DownloadFailure(True, "temporary")
    timers = TimerFactory(error=RuntimeError("timer factory failed"))
    download_manager, _downloader, executor, _timers = manager(
        repository,
        downloader,
        timers=timers,
    )
    statuses: list[str] = []
    download_manager.status_changed.connect(statuses.append)
    download_manager.start()

    executor.finish(1)

    assert repository.assets[1].state is MediaAssetState.DOWNLOADING
    assert executor.submitted == [1, 1]
    assert any("retry timer" in status.lower() for status in statuses)
    assert any("timer factory failed" in status for status in statuses)


@pytest.mark.parametrize(
    ("timer", "diagnostic"),
    (
        (RetryTimerDouble(start_error=RuntimeError("timer start failed")), "timer start failed"),
        (
            RetryTimerDouble(connect_error=RuntimeError("timer connect failed")),
            "timer connect failed",
        ),
    ),
)
def test_retry_timer_setup_failure_disposes_timer_and_keeps_asset_schedulable(
    timer: RetryTimerDouble,
    diagnostic: str,
) -> None:
    repository = RepositoryDouble([asset(1)])
    downloader = DownloaderDouble()
    downloader.outcomes[1] = DownloadFailure(True, "temporary")
    download_manager, _downloader, executor, _timers = manager(
        repository,
        downloader,
        timers=TimerFactory(timer=timer),
    )
    statuses: list[str] = []
    download_manager.status_changed.connect(statuses.append)
    download_manager.start()

    executor.finish(1)

    assert executor.submitted == [1, 1]
    assert timer.stop_count == 1
    assert timer.delete_later_count == 1
    assert timer.timeout.callbacks == []
    assert any(diagnostic in status for status in statuses)


def test_synchronous_retry_timeout_disposes_once_and_retries_without_duplication() -> None:
    repository = RepositoryDouble([asset(1)])
    downloader = DownloaderDouble()
    downloader.outcomes[1] = DownloadFailure(True, "temporary")
    timer = RetryTimerDouble(fire_on_start=True)
    download_manager, _downloader, executor, _timers = manager(
        repository,
        downloader,
        timers=TimerFactory(timer=timer),
    )
    download_manager.start()

    executor.finish(1)

    assert executor.submitted == [1, 1]
    assert timer.stop_count == 1
    assert timer.delete_later_count == 1
    assert timer.timeout.callbacks == []


def test_restart_resumes_pending_and_interrupted_assets_but_not_terminal_assets() -> None:
    repository = RepositoryDouble(
        [
            asset(1, state=MediaAssetState.AVAILABLE),
            asset(2, state=MediaAssetState.FAILED),
            asset(3, state=MediaAssetState.DOWNLOADING),
            asset(4),
        ]
    )
    download_manager, _downloader, executor, _timers = manager(repository)

    download_manager.start()

    assert executor.submitted == [3, 4]


def test_failure_diagnostic_is_bounded_before_it_is_stored() -> None:
    repository = RepositoryDouble([asset(1)])
    downloader = DownloaderDouble()
    downloader.outcomes[1] = RuntimeError("x" * 1_000)
    download_manager, _downloader, executor, _timers = manager(repository, downloader)
    download_manager.start()

    executor.finish(1)

    assert len(repository.failed[0][1]) == 500


class ColorFetcher:
    def fetch(self, url: str) -> FetchResponse:
        color = QColor(210, 20, 30, 120) if "/old?" in url else QColor(20, 40, 220, 120)
        image = QImage(12, 8, QImage.Format.Format_ARGB32)
        image.fill(color)
        payload = QByteArray()
        buffer = QBuffer(payload)
        assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        assert image.save(buffer, "PNG")
        buffer.close()
        return FetchResponse(url, "image/png", bytes(payload))


def real_repository_with_post(tmp_path: Path) -> tuple[MediaRepository, int]:
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    post = PostRepository(database).insert(
        PostDraft(
            canonical_url="https://x.com/example/status/123",
            x_post_id="123",
            author_handle="example",
            author_name="Example",
            text="A photo post",
            published_at=None,
            embed_html="<blockquote></blockquote>",
            provider_json="{}",
            source_method="oembed",
            has_media=True,
            availability=Availability.AVAILABLE,
        )
    )
    return MediaRepository(database), post.id


def test_authoritative_successor_finishing_first_cannot_be_overwritten_by_stale_worker(
    tmp_path: Path,
) -> None:
    repository, post_id = real_repository_with_post(tmp_path)
    canonical_url = "https://x.com/example/status/123"
    old = repository.record_manifest(
        post_id,
        canonical_url,
        MediaOrigin.COLLECTION,
        (PhotoCandidate(0, "https://pbs.twimg.com/media/old?format=png", "old"),),
    )[0]
    keeper_post = PostRepository(repository.database).insert(
        PostDraft(
            canonical_url="https://x.com/example/status/456",
            x_post_id="456",
            author_handle="example",
            author_name="Example",
            text="Keeps the stale asset ID from being reused",
            published_at=None,
            embed_html="<blockquote></blockquote>",
            provider_json="{}",
            source_method="oembed",
            has_media=True,
            availability=Availability.AVAILABLE,
        )
    )
    keeper = repository.record_manifest(
        keeper_post.id,
        keeper_post.canonical_url,
        MediaOrigin.COLLECTION,
        (PhotoCandidate(0, "https://pbs.twimg.com/media/keeper?format=png", None),),
    )[0]
    assert repository.claim_asset(keeper.id) is not None
    repository.mark_asset_available(
        keeper.id,
        local_path=f"{keeper_post.id}/photo-0.png",
        mime_type="image/png",
        width=1,
        height=1,
    )
    media_root = tmp_path / "media"
    executor = ExecutorDouble()
    manager = MediaDownloadManager(
        repository,
        PhotoDownloader(media_root, ColorFetcher()),
        maximum_concurrency=2,
        executor=executor,  # type: ignore[arg-type]
    )
    changed: list[int] = []
    manager.asset_changed.connect(changed.append)
    manager.start()
    assert executor.submitted == [old.id]

    successor = repository.record_manifest(
        post_id,
        canonical_url,
        MediaOrigin.COLLECTION,
        (PhotoCandidate(0, "https://pbs.twimg.com/media/new?format=png", "new"),),
    )[0]
    manager.schedule()
    assert executor.submitted == [old.id, successor.id]

    executor.finish(successor.id)
    executor.finish(old.id)

    current = repository.assets_for_posts((post_id,))[post_id][0]
    assert current.id == successor.id
    assert current.source_url == "https://pbs.twimg.com/media/new?format=png&name=large"
    assert current.state is MediaAssetState.AVAILABLE
    assert current.local_path == f"{post_id}/photo-0.png"
    stored = QImage(str(media_root / current.local_path))
    assert stored.pixelColor(0, 0) == QColor(20, 40, 220, 120)
    assert changed == [successor.id]
