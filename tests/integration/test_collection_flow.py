from collections.abc import Callable

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QWidget

from xfeed.app import build_services
from xfeed.collection import (
    CandidateObservation,
    DiscoveryReason,
    DiscoveryResult,
    qualifying_candidates,
)
from xfeed.domain import CollectionTarget, CollectionTargetKind, ObservationKind, PostDraft
from xfeed.media import MediaAssetState, MediaOrigin, MediaResolutionState, PhotoCandidate
from xfeed.media_download import DownloadFailure, FetchResponse, PhotoDownloader
from xfeed.media_download_manager import MediaDownloadManager
from xfeed.posts import SaveResult, SaveStatus
from xfeed.ui.collection_controller import CollectionController
from xfeed.ui.collection_coordinator import CollectionCoordinator
from xfeed.ui.feed_view import FeedView
from xfeed.ui.settings import UiSettingsStore
from xfeed.ui.sources_view import SourcesView
from xfeed.urls import CanonicalPostUrl
from xfeed.x_session import SessionState


class FixtureProvider:
    def fetch(self, post_url: CanonicalPostUrl) -> PostDraft:
        return PostDraft(
            canonical_url=post_url.url,
            x_post_id=post_url.post_id,
            author_handle=post_url.handle,
            author_name="Alpha Example",
            text=f"fixture post {post_url.post_id}",
            published_at=f"2026-07-{int(post_url.post_id):02d}",
            embed_html=f"<blockquote>fixture post {post_url.post_id}</blockquote>",
            provider_json='{"fixture": true}',
            source_method="oembed-fixture",
        )


class FakeCollector(QObject):
    progress = Signal(object)
    finished = Signal(object)

    def __init__(self, observations: tuple[CandidateObservation, ...]) -> None:
        super().__init__()
        self.widget = QWidget()
        self.observations = observations

    def start(self, target: CollectionTarget, maximum: int = 30) -> None:
        if target.handle is None:
            raise ValueError("the fixture collector requires a source target")
        candidates = qualifying_candidates(self.observations, target.handle)[:maximum]
        for candidate in candidates:
            self.progress.emit(candidate)
        self.finished.emit(DiscoveryResult(candidates, DiscoveryReason.EXHAUSTED))

    def cancel(self) -> None:
        self.finished.emit(DiscoveryResult((), DiscoveryReason.CANCELLED))


class HtmlViewDouble(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.rendered: list[tuple[str, QUrl]] = []

    def setHtml(self, html: str, base_url: QUrl) -> None:
        self.rendered.append((html, base_url))


class ImageFetcher:
    def __init__(self, body: bytes, *, failure: DownloadFailure | None = None) -> None:
        self.body = body
        self.failure = failure

    def fetch(self, url: str) -> FetchResponse:
        if self.failure is not None:
            raise self.failure
        return FetchResponse(url, "image/jpeg", self.body)


class ImmediateDownloadExecutor(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, object)

    def submit(self, asset_id: int, operation) -> None:
        try:
            self.succeeded.emit(asset_id, operation())
        except Exception as error:
            self.failed.emit(asset_id, error)


class FeedCoordinatorDouble(QObject):
    state_changed = Signal(object)
    request_completed = Signal(object)
    queue_finished = Signal()

    def __init__(self, run_ids: tuple[int, ...]) -> None:
        super().__init__()
        self.run_ids = run_ids
        self.busy = False

    def current_run_ids(self, kind: CollectionTargetKind) -> tuple[int, ...]:
        return self.run_ids if kind is CollectionTargetKind.FOR_YOU else ()

    def collect_feed(self, *_args, **_kwargs) -> bool:
        return True


class FeedSchedulerDouble(QObject):
    countdown_changed = Signal(object)
    status_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.remaining_seconds = None

    def set_interval(self, _value: int) -> None:
        pass

    def set_maximum(self, _value: int) -> None:
        pass


def feed_schedulers() -> dict[CollectionTargetKind, FeedSchedulerDouble]:
    return {
        CollectionTargetKind.FOR_YOU: FeedSchedulerDouble(),
        CollectionTargetKind.FOLLOWING: FeedSchedulerDouble(),
    }


class MediaResolverDouble(QObject):
    completed = Signal(object)
    failed = Signal(str)
    active_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.active_job = None
        self.started = []

    def start(self, job) -> None:
        self.active = True
        self.active_job = job
        self.started.append(job)
        self.active_changed.emit(True)

    def cancel(self) -> None:
        self.active = False
        self.active_changed.emit(False)


def jpeg_bytes() -> bytes:
    image = QImage(12, 8, QImage.Format.Format_RGB32)
    image.fill(QColor(20, 80, 140))
    payload = QByteArray()
    buffer = QBuffer(payload)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "JPEG", 88)
    buffer.close()
    return bytes(payload)


class SignedInSession(QObject):
    state_changed = Signal(object)
    verification_finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.state = SessionState.SIGNED_IN

    def verify(self) -> None:
        pass

    def begin_login(self) -> None:
        pass

    def clear(self) -> None:
        self.state = SessionState.UNAVAILABLE
        self.state_changed.emit(self.state)


def immediate_save(
    service,
) -> Callable[[str, Callable[[SaveResult], None], Callable[[str], None]], None]:
    def execute(
        url: str,
        on_result: Callable[[SaveResult], None],
        _on_error: Callable[[str], None],
    ) -> None:
        on_result(service.quick_save(url))

    return execute


def candidate(
    post_id: int,
    *,
    pinned: bool = False,
    reply: bool = False,
    repost: bool = False,
    quote: bool = False,
    photos: tuple[PhotoCandidate, ...] = (),
) -> CandidateObservation:
    return CandidateObservation(
        url=f"https://x.com/alpha/status/{post_id}",
        post_id=str(post_id),
        author_handle="alpha",
        is_pinned=pinned,
        is_reply=reply,
        is_repost=repost,
        is_quote=quote,
        discovery_order=post_id,
        photos=photos,
    )


def test_public_collection_and_quick_save_share_one_duplicate_safe_feed(tmp_path, qtbot) -> None:
    services = build_services(tmp_path / "feed.sqlite3", provider=FixtureProvider())
    collector = FakeCollector(
        (
            candidate(1),
            candidate(2),
            candidate(3, pinned=True),
            candidate(4, reply=True),
            candidate(5, repost=True),
            candidate(6, quote=True),
        )
    )
    controller = CollectionController(
        collector,
        services.collection_service,
        services.collections,
        session_id=services.sessions.begin("test").id,
    )
    settings = UiSettingsStore(tmp_path / "ui.ini")
    session = SignedInSession()
    coordinator = CollectionCoordinator(
        controller,
        services.sources,
        session,
        lambda _parent: (_ for _ in ()).throw(AssertionError("login is not expected")),
    )
    timeline = HtmlViewDouble()
    sources = SourcesView(
        services.sources,
        services.posts,
        coordinator,
        settings,
        web_factory=lambda: timeline,
    )
    feed_html = HtmlViewDouble()
    manual_html = HtmlViewDouble()
    feed = FeedView(
        services.posts,
        services.post_service,
        services.collections,
        coordinator=coordinator,
        schedulers=feed_schedulers(),
        settings=settings,
        web_factory=lambda: feed_html,
        manual_web_factory=lambda: manual_html,
        saved_web_factory=HtmlViewDouble,
        save_executor=immediate_save(services.post_service),
    )
    qtbot.addWidget(sources)
    qtbot.addWidget(feed)

    sources.add_field.setText("@alpha")
    sources.add_button.click()
    source_id = next(iter(sources.cards))

    with qtbot.waitSignal(controller.completed, timeout=3000) as first_collection:
        sources.cards[source_id].collect_button.click()
    first_result = first_collection.args[0]

    assert first_result.run.saved_count == 2
    assert [post.x_post_id for post in services.posts.list_recent()] == ["2", "1"]

    with qtbot.waitSignal(controller.completed, timeout=3000) as second_collection:
        sources.cards[source_id].collect_button.click()
    second_result = second_collection.args[0]

    assert second_result.run.saved_count == 0
    assert second_result.run.duplicate_count == 2
    assert len(services.posts.list_recent()) == 2

    existing = services.posts.by_url("https://x.com/alpha/status/1")
    assert existing is not None
    feed.manual_saves.url_field.setText(existing.canonical_url)
    feed.manual_saves.save_button.click()

    assert feed.manual_saves.status_label.text() == "Already saved"
    assert services.posts.author_counts()[0].handle == "alpha"
    assert services.posts.author_counts()[0].count == 2
    assert f'id="post-{existing.id}" class="post-card focused"' in manual_html.rendered[-1][0]
    assert "@alpha" in manual_html.rendered[-1][0]
    assert "fixture post 1" in manual_html.rendered[-1][0]
    assert "fixture post 1" not in feed_html.rendered[-1][0]
    assert services.post_service.save(existing.canonical_url).status is SaveStatus.DUPLICATE


def test_manual_save_starts_an_exact_post_photo_resolution_job(tmp_path, qtbot) -> None:
    services = build_services(tmp_path / "feed.sqlite3", provider=FixtureProvider())
    controller = CollectionController(
        FakeCollector(()),
        services.collection_service,
        services.collections,
        session_id=services.sessions.begin("test").id,
    )
    resolver = MediaResolverDouble()
    coordinator = CollectionCoordinator(
        controller,
        services.sources,
        SignedInSession(),
        lambda _parent: (_ for _ in ()).throw(AssertionError("login is not expected")),
        media_repository=services.media_repository,
        media_manifest_service=services.media_manifest_service,
        media_resolver=resolver,
    )
    feed = FeedView(
        services.posts,
        services.post_service,
        services.collections,
        coordinator=coordinator,
        schedulers=feed_schedulers(),
        settings=UiSettingsStore(tmp_path / "ui.ini"),
        web_factory=HtmlViewDouble,
        manual_web_factory=HtmlViewDouble,
        save_executor=immediate_save(services.post_service),
        media_repository=services.media_repository,
        media_resolution_requester=coordinator.resolve_post_photos,
    )
    qtbot.addWidget(feed)

    feed.manual_saves.url_field.setText("https://x.com/alpha/status/9")
    feed.manual_saves.save_button.click()

    saved = services.posts.by_url("https://x.com/alpha/status/9")
    assert saved is not None
    assert [job.post_id for job in resolver.started] == [saved.id]
    assert resolver.started[0].canonical_url == saved.canonical_url
    with services.database.connection() as connection:
        job = connection.execute(
            "SELECT origin,state FROM media_resolution_jobs WHERE post_id=?", (saved.id,)
        ).fetchone()
    assert job is not None
    assert tuple(job) == (MediaOrigin.MANUAL.value, MediaResolutionState.RESOLVING.value)


def test_for_you_collection_persists_reply_observation_metadata_without_counting_parent(
    tmp_path,
) -> None:
    services = build_services(tmp_path / "feed.sqlite3", provider=FixtureProvider())
    run = services.collections.start(CollectionTarget.for_you(), requested_max=50)
    reply = CandidateObservation(
        url="https://x.com/beta/status/2",
        post_id="2",
        author_handle="beta",
        is_reply=True,
        parent_url="https://x.com/alpha/status/1",
        discovery_order=6,
    )

    result = services.collection_service.process(
        run,
        DiscoveryResult((reply,), DiscoveryReason.LIMIT),
    )

    observed = services.collections.latest_for_you_posts()
    parent = services.posts.by_url(reply.parent_url)
    assert parent is not None
    assert [
        (item.post.x_post_id, item.discovery_order, item.observation_kind) for item in observed
    ] == [("2", 6, ObservationKind.REPLY)]
    assert observed[0].parent_post_id == parent.id
    assert result.progress.saved == 1
    assert result.progress.duplicates == 0


def test_collection_persists_photo_manifest_without_starting_download_work(tmp_path) -> None:
    services = build_services(tmp_path / "feed.sqlite3", provider=FixtureProvider())
    run = services.collections.start(CollectionTarget.for_you(), requested_max=50)
    photos = (PhotoCandidate(0, "https://pbs.twimg.com/media/one?name=small&format=jpg", "one"),)

    result = services.collection_service.process(
        run,
        DiscoveryResult((candidate(1, photos=photos),), DiscoveryReason.LIMIT),
    )

    saved = services.posts.by_url("https://x.com/alpha/status/1")
    assert saved is not None
    assets = services.media_repository.assets_for_posts((saved.id,))
    assert assets[saved.id][0].source_url.endswith("/one?format=jpg&name=large")
    assert assets[saved.id][0].state is MediaAssetState.PENDING
    with services.database.connection() as connection:
        job = connection.execute(
            "SELECT origin,state,manifest_count FROM media_resolution_jobs WHERE post_id=?",
            (saved.id,),
        ).fetchone()
    assert job is not None
    assert tuple(job) == (MediaOrigin.COLLECTION.value, MediaResolutionState.RESOLVED.value, 1)
    assert result.progress.saved == 1


def test_collection_downloads_local_photos_and_reuses_them_after_restart(tmp_path, qtbot) -> None:
    database_path = tmp_path / "feed.sqlite3"
    media_root = tmp_path / "media"
    services = build_services(database_path, provider=FixtureProvider())
    one_photo = (PhotoCandidate(0, "https://pbs.twimg.com/media/one?format=jpg&name=large", "one"),)
    four_photos = tuple(
        PhotoCandidate(
            position,
            f"https://pbs.twimg.com/media/four-{position}?format=jpg&name=large",
            f"four {position}",
        )
        for position in range(4)
    )
    run = services.collections.start(CollectionTarget.for_you(), requested_max=10)
    first_result = services.collection_service.process(
        run,
        DiscoveryResult(
            (candidate(1, photos=one_photo), candidate(2, photos=four_photos)),
            DiscoveryReason.EXHAUSTED,
        ),
    )
    executor = ImmediateDownloadExecutor()
    manager = MediaDownloadManager(
        services.media_repository,
        PhotoDownloader(media_root, ImageFetcher(jpeg_bytes())),
        executor=executor,
    )

    manager.start()

    saved_one = services.posts.by_url("https://x.com/alpha/status/1")
    saved_four = services.posts.by_url("https://x.com/alpha/status/2")
    assert saved_one is not None
    assert saved_four is not None
    assets = services.media_repository.assets_for_posts((saved_one.id, saved_four.id))
    assert [asset.state for asset in assets[saved_one.id]] == [MediaAssetState.AVAILABLE]
    assert [asset.state for asset in assets[saved_four.id]] == [MediaAssetState.AVAILABLE] * 4
    paths_before = [asset.local_path for group in assets.values() for asset in group]
    assert all(path is not None and (media_root / path).is_file() for path in paths_before)

    view = FeedView(
        services.posts,
        services.post_service,
        services.collections,
        coordinator=FeedCoordinatorDouble((run.id,)),
        schedulers=feed_schedulers(),
        settings=UiSettingsStore(tmp_path / "ui.ini"),
        web_factory=HtmlViewDouble,
        manual_web_factory=HtmlViewDouble,
        media_repository=services.media_repository,
    )
    qtbot.addWidget(view)
    original_html = view.feed_pane(CollectionTargetKind.FOR_YOU).web_view.rendered[-1][0]  # type: ignore[attr-defined]
    asset_urls = [f"xfeed-media://asset/{asset.id}" for group in assets.values() for asset in group]
    assert all(url in original_html for url in asset_urls)

    duplicate_run = services.collections.start(CollectionTarget.for_you(), requested_max=10)
    duplicate_result = services.collection_service.process(
        duplicate_run,
        DiscoveryResult(
            (candidate(1, photos=one_photo), candidate(2, photos=four_photos)),
            DiscoveryReason.EXHAUSTED,
        ),
    )
    duplicated_assets = services.media_repository.assets_for_posts((saved_one.id, saved_four.id))
    assert duplicate_result.progress.duplicates == 2
    assert [asset.id for group in duplicated_assets.values() for asset in group] == [
        asset.id for group in assets.values() for asset in group
    ]
    assert [
        asset.local_path for group in duplicated_assets.values() for asset in group
    ] == paths_before

    restarted = build_services(database_path, provider=FixtureProvider())
    restarted_view = FeedView(
        restarted.posts,
        restarted.post_service,
        restarted.collections,
        coordinator=FeedCoordinatorDouble((run.id,)),
        schedulers=feed_schedulers(),
        settings=UiSettingsStore(tmp_path / "restarted-ui.ini"),
        web_factory=HtmlViewDouble,
        manual_web_factory=HtmlViewDouble,
        media_repository=restarted.media_repository,
    )
    qtbot.addWidget(restarted_view)
    restarted_html = restarted_view.feed_pane(CollectionTargetKind.FOR_YOU).web_view.rendered[-1][0]  # type: ignore[attr-defined]
    assert all(url in restarted_html for url in asset_urls)
    assert first_result.progress.saved == 2


def test_terminal_photo_download_failure_keeps_collection_and_feed_post(tmp_path, qtbot) -> None:
    services = build_services(tmp_path / "feed.sqlite3", provider=FixtureProvider())
    run = services.collections.start(CollectionTarget.for_you(), requested_max=10)
    result = services.collection_service.process(
        run,
        DiscoveryResult(
            (
                candidate(
                    3,
                    photos=(
                        PhotoCandidate(
                            0,
                            "https://pbs.twimg.com/media/broken?format=jpg&name=large",
                            "broken",
                        ),
                    ),
                ),
            ),
            DiscoveryReason.EXHAUSTED,
        ),
    )
    manager = MediaDownloadManager(
        services.media_repository,
        PhotoDownloader(
            tmp_path / "media",
            ImageFetcher(jpeg_bytes(), failure=DownloadFailure(False, "fixture broken")),
        ),
        executor=ImmediateDownloadExecutor(),
    )

    manager.start()

    saved = services.posts.by_url("https://x.com/alpha/status/3")
    assert saved is not None
    assets = services.media_repository.assets_for_posts((saved.id,))
    assert assets[saved.id][0].state is MediaAssetState.FAILED
    view = FeedView(
        services.posts,
        services.post_service,
        services.collections,
        coordinator=FeedCoordinatorDouble((run.id,)),
        schedulers=feed_schedulers(),
        settings=UiSettingsStore(tmp_path / "ui.ini"),
        web_factory=HtmlViewDouble,
        manual_web_factory=HtmlViewDouble,
        media_repository=services.media_repository,
    )
    qtbot.addWidget(view)

    rendered = view.feed_pane(CollectionTargetKind.FOR_YOU).web_view.rendered[-1][0]  # type: ignore[attr-defined]
    assert f'id="post-{saved.id}"' in rendered
    assert "1 photo couldn&#x27;t be saved." in rendered
    assert result.progress.saved == 1
    assert result.progress.failed == 0
