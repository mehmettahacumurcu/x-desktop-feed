from collections.abc import Callable

import pytest
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtWidgets import QWidget

from xfeed.collection import CollectionProgress, CollectionResult
from xfeed.domain import (
    Availability,
    CollectionRun,
    CollectionStatus,
    CollectionTargetKind,
    ObservationKind,
    ObservedPost,
    SavedPost,
)
from xfeed.media import MediaAsset, MediaAssetState
from xfeed.posts import SaveResult, SaveStatus
from xfeed.ui.feed_view import FeedView
from xfeed.ui.collection_coordinator import MediaResolutionRequestOutcome
from xfeed.ui.main_window import MainWindow
from xfeed.ui.manual_saves_panel import ManualSavesPanel
from xfeed.ui.settings import UiSettingsStore
from xfeed.ui.theme import APPLICATION_STYLESHEET
from xfeed.ui.workers import Worker


class PostRepositoryDouble:
    def __init__(self, manual: list[SavedPost] | None = None) -> None:
        self.manual = manual or []
        self.manual_calls = 0

    def list_manual(self, limit: int = 100) -> list[SavedPost]:
        self.manual_calls += 1
        return self.manual[:limit]


class MediaRepositoryDouble:
    def __init__(self, assets: list[MediaAsset] | None = None) -> None:
        self.assets = assets or []
        self.calls: list[tuple[int, ...]] = []

    def assets_for_posts(self, post_ids: tuple[int, ...]) -> dict[int, tuple[MediaAsset, ...]]:
        self.calls.append(post_ids)
        return {
            post_id: tuple(asset for asset in self.assets if asset.post_id == post_id)
            for post_id in post_ids
            if any(asset.post_id == post_id for asset in self.assets)
        }


class MediaEvents(QObject):
    asset_changed = Signal(int)


class CollectionRepositoryDouble:
    def __init__(
        self,
        current: list[ObservedPost] | None = None,
        older: list[ObservedPost] | None = None,
    ) -> None:
        self.current = current or []
        self.older = older or []
        self.current_calls: list[tuple[CollectionTargetKind, tuple[int, ...]]] = []
        self.older_calls: list[
            tuple[CollectionTargetKind, tuple[int, ...], tuple[int, ...], int]
        ] = []

    def feed_posts_for_runs(self, kind, run_ids) -> list[ObservedPost]:
        self.current_calls.append((kind, tuple(run_ids)))
        return self.current

    def older_feed_posts(
        self,
        kind,
        *,
        excluding_run_ids,
        excluding_post_ids,
        limit: int = 200,
    ) -> list[ObservedPost]:
        self.older_calls.append((kind, tuple(excluding_run_ids), tuple(excluding_post_ids), limit))
        return self.older

    def for_you_posts_for_runs(self, run_ids) -> list[ObservedPost]:
        return self.feed_posts_for_runs(CollectionTargetKind.FOR_YOU, run_ids)

    def older_for_you_posts(
        self,
        *,
        excluding_run_ids,
        excluding_post_ids,
        limit: int = 200,
    ) -> list[ObservedPost]:
        return self.older_feed_posts(
            CollectionTargetKind.FOR_YOU,
            excluding_run_ids=excluding_run_ids,
            excluding_post_ids=excluding_post_ids,
            limit=limit,
        )


class CoordinatorDouble(QObject):
    state_changed = Signal(object)
    request_completed = Signal(object)
    queue_finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.current_run_ids_by_kind: dict[CollectionTargetKind, tuple[int, ...]] = {
            CollectionTargetKind.FOR_YOU: (),
            CollectionTargetKind.FOLLOWING: (),
        }
        self.calls: list[tuple[int, bool, object]] = []
        self.feed_calls: list[tuple[CollectionTargetKind, int, bool, object]] = []
        self.busy = False

    @property
    def current_for_you_run_ids(self) -> tuple[int, ...]:
        return self.current_run_ids(CollectionTargetKind.FOR_YOU)

    @current_for_you_run_ids.setter
    def current_for_you_run_ids(self, value: tuple[int, ...]) -> None:
        self.current_run_ids_by_kind[CollectionTargetKind.FOR_YOU] = value

    def current_run_ids(self, kind: CollectionTargetKind) -> tuple[int, ...]:
        return self.current_run_ids_by_kind[kind]

    def collect_feed(
        self,
        kind: CollectionTargetKind,
        maximum: int,
        *,
        interactive: bool,
        parent: object,
    ) -> bool:
        self.feed_calls.append((kind, maximum, interactive, parent))
        return True

    def collect_for_you(self, maximum: int, *, interactive: bool, parent: object) -> bool:
        self.calls.append((maximum, interactive, parent))
        return self.collect_feed(
            CollectionTargetKind.FOR_YOU,
            maximum,
            interactive=interactive,
            parent=parent,
        )


class SchedulerDouble(QObject):
    countdown_changed = Signal(object)
    status_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.intervals: list[int] = []
        self.maximums: list[int] = []
        self.remaining_seconds: int | None = None

    def set_interval(self, value: int) -> None:
        self.intervals.append(value)

    def set_maximum(self, value: int) -> None:
        self.maximums.append(value)


class PostServiceDouble:
    def quick_save(self, _url: str) -> SaveResult:
        raise AssertionError("the injected executor should isolate network work")


class QuickSaveServiceDouble:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def quick_save(self, url: str) -> SaveResult:
        self.calls.append(url)
        return SaveResult(SaveStatus.INVALID, message="invalid fixture")


class WebViewDouble(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.rendered: list[tuple[str, QUrl]] = []
        self.scripts: list[str] = []

    def setHtml(self, html: str, base_url: QUrl) -> None:
        self.rendered.append((html, base_url))

    def page(self):
        return self

    def runJavaScript(self, script: str) -> None:
        self.scripts.append(script)


@pytest.fixture
def themed_qapp(qapp):
    previous = qapp.styleSheet()
    qapp.setStyleSheet(APPLICATION_STYLESHEET)
    yield qapp
    qapp.setStyleSheet(previous)


def saved_post(post_id: int, author: str = "alpha", *, text: str | None = None) -> SavedPost:
    return SavedPost(
        canonical_url=f"https://x.com/{author}/status/{post_id}",
        x_post_id=str(post_id),
        author_handle=author,
        author_name=author.title(),
        text=text or f"post {post_id}",
        published_at=f"2026-07-{post_id:02d}",
        embed_html=f"<blockquote>post {post_id}</blockquote>",
        provider_json="{}",
        source_method="oembed",
        has_media=False,
        availability=Availability.AVAILABLE,
        id=post_id,
    )


def media_asset(
    asset_id: int,
    post_id: int,
    state: MediaAssetState,
    *,
    position: int = 0,
) -> MediaAsset:
    available = state is MediaAssetState.AVAILABLE
    return MediaAsset(
        id=asset_id,
        post_id=post_id,
        position=position,
        source_url=f"https://pbs.twimg.com/media/{asset_id}?format=jpg&name=large",
        alt_text=f"photo {asset_id}",
        state=state,
        local_path=f"{post_id}/photo-{position}.jpg" if available else None,
        mime_type="image/jpeg" if available else None,
        width=80 if available else None,
        height=60 if available else None,
        attempts=1,
        diagnostic="fixture failure" if state is MediaAssetState.FAILED else None,
        created_at="2026-07-24 10:00:00",
        updated_at="2026-07-24 10:00:00",
    )


def observed(post: SavedPost, order: int = 0) -> ObservedPost:
    return ObservedPost(post, order, ObservationKind.POST, None)


def collection_result(
    run_id: int,
    kind: CollectionTargetKind = CollectionTargetKind.FOR_YOU,
) -> CollectionResult:
    run = CollectionRun(
        id=run_id,
        source_id=None,
        started_at="2026-07-21 10:00:00",
        finished_at="2026-07-21 10:01:00",
        requested_max=10,
        candidate_count=1,
        saved_count=1,
        duplicate_count=0,
        failed_count=0,
        status=CollectionStatus.COMPLETED,
        reason="limit",
        diagnostic=None,
        target_kind=kind,
    )
    return CollectionResult(run, CollectionProgress(1, 1, 1, 0, 0))


def immediate_result(result: SaveResult):
    def execute(
        _url: str,
        on_result: Callable[[SaveResult], None],
        _on_error: Callable[[str], None],
    ) -> None:
        on_result(result)

    return execute


def build_view(
    tmp_path,
    qtbot,
    *,
    current: list[ObservedPost] | None = None,
    older: list[ObservedPost] | None = None,
    manual: list[SavedPost] | None = None,
    executor=None,
    media_repository: MediaRepositoryDouble | None = None,
    media_events: MediaEvents | None = None,
):
    posts = PostRepositoryDouble(manual)
    collections = CollectionRepositoryDouble(current, older)
    coordinator = CoordinatorDouble()
    schedulers = {
        CollectionTargetKind.FOR_YOU: SchedulerDouble(),
        CollectionTargetKind.FOLLOWING: SchedulerDouble(),
    }
    settings = UiSettingsStore(tmp_path / "ui.ini")
    feed_webs = {
        CollectionTargetKind.FOR_YOU: WebViewDouble(),
        CollectionTargetKind.FOLLOWING: WebViewDouble(),
    }
    web_widgets = iter(feed_webs.values())
    manual_web = WebViewDouble()
    saved_web = WebViewDouble()
    view = FeedView(
        posts,
        PostServiceDouble(),
        collections,
        coordinator=coordinator,
        schedulers=schedulers,
        settings=settings,
        web_factory=lambda: next(web_widgets),
        manual_web_factory=lambda: manual_web,
        saved_web_factory=lambda: saved_web,
        save_executor=executor,
        media_repository=media_repository,
        asset_changed=media_events.asset_changed if media_events is not None else None,
    )
    qtbot.addWidget(view)
    return (
        view,
        posts,
        collections,
        coordinator,
        schedulers[CollectionTargetKind.FOR_YOU],
        settings,
        feed_webs[CollectionTargetKind.FOR_YOU],
        manual_web,
    )


def for_you(view: FeedView):
    return view.feed_pane(CollectionTargetKind.FOR_YOU)


def test_controls_offer_approved_counts_and_restore_settings(tmp_path, qtbot) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")
    settings.set_my_feed_count(30)
    settings.set_auto_interval_minutes(10)
    view, *_rest = build_view(tmp_path, qtbot)

    pane = for_you(view)
    assert [pane.amount_combo.itemData(i) for i in range(pane.amount_combo.count())] == [
        10,
        20,
        30,
        50,
    ]
    assert pane.amount_combo.currentData() == 30
    assert [pane.interval_combo.itemText(i) for i in range(pane.interval_combo.count())] == [
        "Never",
        "5 min",
        "10 min",
        "30 min",
        "60 min",
    ]
    assert pane.interval_combo.currentData() == 10


def test_controls_update_scheduler_and_collect_now_uses_exact_count(tmp_path, qtbot) -> None:
    view, _posts, _collections, coordinator, scheduler, *_ = build_view(tmp_path, qtbot)

    pane = for_you(view)
    pane.amount_combo.setCurrentIndex(pane.amount_combo.findData(50))
    pane.interval_combo.setCurrentIndex(pane.interval_combo.findData(5))
    pane.collect_button.click()

    assert scheduler.maximums[-1] == 50
    assert scheduler.intervals[-1] == 5
    assert coordinator.feed_calls == [(CollectionTargetKind.FOR_YOU, 50, True, pane)]


def test_new_process_starts_empty_without_querying_history(tmp_path, qtbot) -> None:
    view, _posts, collections, _coordinator, _scheduler, _settings, web, _manual = build_view(
        tmp_path, qtbot, older=[observed(saved_post(1))]
    )

    assert collections.current_calls == []
    assert collections.older_calls == []
    assert "Your fresh feed starts here" in web.rendered[-1][0]
    assert not for_you(view).jump_older_button.isEnabled()


def test_current_session_runs_render_in_repository_order(tmp_path, qtbot) -> None:
    view, _posts, collections, coordinator, _scheduler, _settings, web, _manual = build_view(
        tmp_path,
        qtbot,
        current=[
            observed(saved_post(2, text="newest run first")),
            observed(saved_post(1, text="then X order"), 1),
        ],
    )
    coordinator.current_for_you_run_ids = (8, 7)

    view.refresh_from_storage()

    assert collections.current_calls == [(CollectionTargetKind.FOR_YOU, (8, 7))]
    html = web.rendered[-1][0]
    assert html.index("newest run first") < html.index("then X order")
    assert "This session" in html


def test_show_older_appends_excluded_history_and_jump_targets_divider(tmp_path, qtbot) -> None:
    current_post = saved_post(3, text="current")
    older_post = saved_post(1, text="older")
    view, _posts, collections, coordinator, _scheduler, _settings, web, _manual = build_view(
        tmp_path,
        qtbot,
        current=[observed(current_post)],
        older=[observed(older_post)],
    )
    coordinator.current_for_you_run_ids = (9,)
    pane = for_you(view)
    pane.show_older_checkbox.setChecked(True)

    assert collections.older_calls == [(CollectionTargetKind.FOR_YOU, (9,), (3,), 200)]
    html = web.rendered[-1][0]
    assert html.index(">current</p>") < html.index("Older app sessions") < html.index(">older</p>")
    assert 'id="older-sessions"' in html
    assert pane.jump_older_button.isEnabled()

    pane.jump_older_button.click()
    assert "older-sessions" in web.scripts[-1]


def test_duplicate_global_post_still_appears_when_observed_this_session(tmp_path, qtbot) -> None:
    duplicate = saved_post(4, text="already existed globally")
    view, _posts, _collections, coordinator, _scheduler, _settings, web, _manual = build_view(
        tmp_path, qtbot, current=[observed(duplicate)]
    )
    coordinator.current_for_you_run_ids = (11,)

    view.refresh_from_storage()

    assert "already existed globally" in web.rendered[-1][0]


def test_feed_queries_only_displayed_posts_and_reloads_without_changing_history_state(
    tmp_path, qtbot
) -> None:
    current = saved_post(21)
    older = saved_post(22)
    repository = MediaRepositoryDouble(
        [
            media_asset(101, current.id, MediaAssetState.AVAILABLE),
            media_asset(102, current.id, MediaAssetState.PENDING, position=1),
            media_asset(103, older.id, MediaAssetState.FAILED),
            media_asset(104, 999, MediaAssetState.FAILED),
        ]
    )
    events = MediaEvents()
    view, _posts, _collections, coordinator, _scheduler, _settings, web, _manual = build_view(
        tmp_path,
        qtbot,
        current=[observed(current)],
        older=[observed(older)],
        media_repository=repository,
        media_events=events,
    )
    coordinator.current_for_you_run_ids = (8,)
    pane = for_you(view)
    pane.show_older_checkbox.setChecked(True)

    assert repository.calls[-1] == (current.id, older.id)
    assert "xfeed-media://asset/101" in web.rendered[-1][0]
    assert "Saving photos" in web.rendered[-1][0]
    assert "1 photo couldn&#x27;t be saved." in web.rendered[-1][0]
    assert "asset/104" not in web.rendered[-1][0]

    render_count = len(web.rendered)
    feed_media_queries = repository.calls.count((current.id, older.id))
    events.asset_changed.emit(104)

    assert len(web.rendered) == render_count
    assert pane.show_older_checkbox.isChecked()
    assert repository.calls.count((current.id, older.id)) == feed_media_queries

    events.asset_changed.emit(101)

    assert len(web.rendered) == render_count + 1
    assert repository.calls.count((current.id, older.id)) == feed_media_queries + 1


def test_manual_saves_drawer_is_separate_and_opens_from_header(tmp_path, qtbot) -> None:
    manual_post = saved_post(6, text="manual only")
    view, posts, collections, _coordinator, _scheduler, _settings, web, manual_web = build_view(
        tmp_path, qtbot, manual=[manual_post]
    )

    assert not view.manual_saves.isVisible()
    view.show()
    view.save_url_button.click()

    assert view.manual_saves.isVisible()
    assert posts.manual_calls == 2
    assert "manual only" in manual_web.rendered[-1][0]
    assert "manual only" not in web.rendered[-1][0]
    assert collections.current_calls == []

    view.manual_saves.close_button.click()
    assert not view.manual_saves.isVisible()


@pytest.mark.parametrize("status", [SaveStatus.SAVED, SaveStatus.DUPLICATE])
def test_manual_save_success_refreshes_and_focuses_only_manual_feed(
    tmp_path, qtbot, status
) -> None:
    saved = saved_post(12, text="new manual save")
    view, posts, _collections, _coordinator, _scheduler, _settings, main_web, manual_web = (
        build_view(
            tmp_path,
            qtbot,
            manual=[saved],
            executor=immediate_result(SaveResult(status, saved, "Saved")),
        )
    )
    initial_main_renders = len(main_web.rendered)
    view.manual_saves.url_field.setText(saved.canonical_url)

    view.manual_saves.save_button.click()

    assert view.manual_saves.url_field.text() == ""
    assert posts.manual_calls == 3
    assert 'id="post-12" class="post-card focused"' in manual_web.rendered[-1][0]
    assert len(main_web.rendered) == initial_main_renders


@pytest.mark.parametrize("status", [SaveStatus.INVALID, SaveStatus.UNAVAILABLE])
def test_manual_save_failure_retains_input(tmp_path, qtbot, status) -> None:
    view, *_ = build_view(
        tmp_path,
        qtbot,
        executor=immediate_result(SaveResult(status, message="service detail")),
    )
    url = "not saved"
    view.manual_saves.url_field.setText(url)

    view.manual_saves.save_button.click()

    assert view.manual_saves.url_field.text() == url
    assert view.manual_saves.status_label.text() == "service detail"


@pytest.mark.parametrize(
    ("status", "accepted", "expected"),
    [
        (
            SaveStatus.SAVED,
            MediaResolutionRequestOutcome.QUEUED,
            "Saved · photo lookup queued",
        ),
        (
            SaveStatus.DUPLICATE,
            MediaResolutionRequestOutcome.RESOLVED,
            "Already saved · photos already resolved",
        ),
        (
            SaveStatus.DUPLICATE,
            MediaResolutionRequestOutcome.FAILED,
            "Already saved · photo lookup failed",
        ),
    ],
)
def test_manual_save_requests_photo_resolution_before_reload_and_uses_plain_status(
    qtbot,
    status: SaveStatus,
    accepted: MediaResolutionRequestOutcome,
    expected: str,
) -> None:
    saved = saved_post(12, text="new manual save")
    posts = PostRepositoryDouble([saved])
    web = WebViewDouble()
    requests: list[tuple[SavedPost, QWidget | None, int]] = []

    def request(post: SavedPost, parent: QWidget | None) -> MediaResolutionRequestOutcome:
        requests.append((post, parent, posts.manual_calls))
        return accepted

    panel = ManualSavesPanel(
        posts,  # type: ignore[arg-type]
        PostServiceDouble(),  # type: ignore[arg-type]
        web_factory=lambda: web,
        save_executor=immediate_result(SaveResult(status, saved, status.value)),
        media_resolution_requester=request,
    )
    qtbot.addWidget(panel)
    panel.url_field.setText(saved.canonical_url)

    panel.save_button.click()

    assert requests == [(saved, panel, 0)]
    assert panel.status_label.text() == expected
    assert panel.url_field.text() == ""
    assert posts.manual_calls == 1
    assert 'id="post-12" class="post-card focused"' in web.rendered[-1][0]


def test_manual_reader_queries_only_manual_posts_and_reloads_media_state(qtbot) -> None:
    saved = saved_post(31)
    repository = MediaRepositoryDouble(
        [
            media_asset(201, saved.id, MediaAssetState.AVAILABLE),
            media_asset(202, saved.id, MediaAssetState.PENDING, position=1),
            media_asset(203, saved.id, MediaAssetState.FAILED, position=2),
            media_asset(204, 999, MediaAssetState.FAILED),
        ]
    )
    events = MediaEvents()
    web = WebViewDouble()
    panel = ManualSavesPanel(
        PostRepositoryDouble([saved]),  # type: ignore[arg-type]
        PostServiceDouble(),  # type: ignore[arg-type]
        web_factory=lambda: web,
        media_repository=repository,  # type: ignore[arg-type]
        asset_changed=events.asset_changed,
    )
    qtbot.addWidget(panel)

    panel.reload(saved.id)
    assert repository.calls[-1] == (saved.id,)
    assert "xfeed-media://asset/201" in web.rendered[-1][0]
    assert "Saving photos" in web.rendered[-1][0]
    assert "1 photo couldn&#x27;t be saved." in web.rendered[-1][0]
    assert "asset/204" not in web.rendered[-1][0]

    events.asset_changed.emit(204)

    assert repository.calls[-1] == (saved.id,)
    assert 'id="post-31" class="post-card focused"' in web.rendered[-1][0]


def test_manual_save_without_photo_requester_keeps_provider_status(qtbot) -> None:
    saved = saved_post(13)
    panel = ManualSavesPanel(
        PostRepositoryDouble([saved]),  # type: ignore[arg-type]
        PostServiceDouble(),  # type: ignore[arg-type]
        web_factory=WebViewDouble,
        save_executor=immediate_result(SaveResult(SaveStatus.DUPLICATE, saved, "Already saved")),
    )
    qtbot.addWidget(panel)

    panel.save_button.click()

    assert panel.status_label.text() == "Already saved"


@pytest.mark.parametrize("status", [SaveStatus.SAVED, SaveStatus.DUPLICATE])
def test_manual_save_emits_persisted_post_once(qtbot, status: SaveStatus) -> None:
    saved = saved_post(14)
    result = SaveResult(status, saved, status.value)
    panel = ManualSavesPanel(
        PostRepositoryDouble([saved]),  # type: ignore[arg-type]
        PostServiceDouble(),  # type: ignore[arg-type]
        web_factory=WebViewDouble,
        save_executor=immediate_result(result),
    )
    qtbot.addWidget(panel)
    emitted: list[SaveResult] = []
    panel.post_saved.connect(emitted.append)

    panel.save_button.click()

    assert emitted == [result]


def test_manual_save_does_not_emit_without_a_persisted_post(qtbot) -> None:
    panel = ManualSavesPanel(
        PostRepositoryDouble(),  # type: ignore[arg-type]
        PostServiceDouble(),  # type: ignore[arg-type]
        web_factory=WebViewDouble,
        save_executor=immediate_result(SaveResult(SaveStatus.INVALID, message="bad URL")),
    )
    qtbot.addWidget(panel)
    emitted: list[SaveResult] = []
    panel.post_saved.connect(emitted.append)

    panel.save_button.click()

    assert emitted == []


def test_default_manual_executor_uses_quick_save(tmp_path, qtbot) -> None:
    posts = PostRepositoryDouble()
    service = QuickSaveServiceDouble()
    view = FeedView(
        posts,
        service,  # type: ignore[arg-type]
        CollectionRepositoryDouble(),
        coordinator=CoordinatorDouble(),
        schedulers={
            CollectionTargetKind.FOR_YOU: SchedulerDouble(),
            CollectionTargetKind.FOLLOWING: SchedulerDouble(),
        },
        settings=UiSettingsStore(tmp_path / "ui.ini"),
        web_factory=WebViewDouble,
        manual_web_factory=WebViewDouble,
    )
    qtbot.addWidget(view)
    url = "https://x.com/alpha/status/123"
    view.manual_saves.url_field.setText(url)

    view.manual_saves.save_button.click()

    qtbot.waitUntil(view.manual_saves.save_button.isEnabled, timeout=3_000)
    assert service.calls == [url]


def test_feed_view_has_three_full_width_tabs(tmp_path, qtbot) -> None:
    view, *_ = build_view(tmp_path, qtbot)
    view.show()

    assert view.tab_names() == ("For You", "Following", "Saved posts")
    assert view.feed_pane(CollectionTargetKind.FOR_YOU).isVisible()


def test_switching_tabs_preserves_controls_and_reader_scroll(tmp_path, qtbot) -> None:
    view, *_ = build_view(tmp_path, qtbot)
    for_you = view.feed_pane(CollectionTargetKind.FOR_YOU)
    following = view.feed_pane(CollectionTargetKind.FOLLOWING)
    for_you.set_selected_count(20)
    for_you.set_reader_scroll_for_test(430)

    view.select_feed(CollectionTargetKind.FOLLOWING)
    following.set_selected_count(50)
    view.select_feed(CollectionTargetKind.FOR_YOU)

    assert for_you.selected_count() == 20
    assert for_you.reader_scroll_for_test() == 430


def test_minimum_window_keeps_feed_controls_at_usable_widths(tmp_path, qtbot, themed_qapp) -> None:
    view, *_ = build_view(tmp_path, qtbot)
    window = MainWindow(pages={"My Feed": view})
    window.resize(960, 640)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: view.feed_pane(CollectionTargetKind.FOR_YOU).width() > 0)
    pane = view.feed_pane(CollectionTargetKind.FOR_YOU)

    assert window.width() == 960
    assert pane.width() >= 650
    for control in (
        pane.amount_combo,
        pane.interval_combo,
        pane.countdown_label,
        pane.collect_button,
    ):
        assert control.width() >= control.minimumSizeHint().width()
    assert pane.countdown_label.y() > pane.amount_combo.y()
    assert pane.collect_button.y() == pane.countdown_label.y()


def test_manual_saves_replaces_feed_at_minimum_width_and_restores_tab_state(
    tmp_path, qtbot, themed_qapp
) -> None:
    view, *_ = build_view(tmp_path, qtbot)
    following = view.feed_pane(CollectionTargetKind.FOLLOWING)
    view.select_feed(CollectionTargetKind.FOLLOWING)
    following.set_selected_count(50)
    following.set_reader_scroll_for_test(275)
    window = MainWindow(pages={"My Feed": view})
    window.resize(960, 640)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(following.isVisible)
    feed_width = following.width()

    view.save_url_button.click()
    qtbot.waitUntil(lambda: view.manual_saves.width() >= feed_width)

    assert window.width() == 960
    assert window.minimumSizeHint().width() <= 960
    assert view.manual_saves.isVisible()
    assert not following.isVisible()
    assert view.manual_saves.width() >= feed_width

    view.manual_saves.close_button.click()

    assert not view.manual_saves.isVisible()
    assert following.isVisible()
    assert following.selected_count() == 50
    assert following.reader_scroll_for_test() == 275


def test_queue_finish_does_not_rerender_unaffected_feed_documents(tmp_path, qtbot) -> None:
    view, _posts, _collections, coordinator, _scheduler, _settings, for_you_web, _manual = (
        build_view(tmp_path, qtbot)
    )
    following_web = view.feed_pane(CollectionTargetKind.FOLLOWING).web_view
    assert isinstance(following_web, WebViewDouble)
    for_you_renders = len(for_you_web.rendered)
    following_renders = len(following_web.rendered)

    coordinator.request_completed.emit(collection_result(14, CollectionTargetKind.FOLLOWING))
    coordinator.queue_finished.emit()

    assert len(for_you_web.rendered) == for_you_renders
    assert len(following_web.rendered) == following_renders + 1


def test_global_busy_state_disables_both_buttons_without_mixing_statuses(tmp_path, qtbot) -> None:
    view, _posts, _collections, coordinator, *_ = build_view(tmp_path, qtbot)
    for_you = view.feed_pane(CollectionTargetKind.FOR_YOU)
    following = view.feed_pane(CollectionTargetKind.FOLLOWING)
    following_status = following.status_banner.label.text()
    target = type("Target", (), {"kind": CollectionTargetKind.FOR_YOU})()
    request = type("Request", (), {"target": target})()
    active_state = type("State", (), {"active": request, "message": "Collecting For You"})()
    coordinator.busy = True

    coordinator.state_changed.emit(active_state)

    assert not for_you.collect_button.isEnabled()
    assert not following.collect_button.isEnabled()
    assert for_you.status_banner.label.text() == "Collecting For You"
    assert following.status_banner.label.text() == following_status

    coordinator.busy = False
    terminal_state = type("State", (), {"active": None, "message": "Collection complete"})()
    coordinator.state_changed.emit(terminal_state)

    assert for_you.collect_button.isEnabled()
    assert following.collect_button.isEnabled()
    assert for_you.status_banner.label.text() == "Collection complete"
    assert following.status_banner.label.text() == following_status


def test_completed_target_gets_terminal_status_before_next_target_starts(tmp_path, qtbot) -> None:
    view, _posts, _collections, coordinator, *_ = build_view(tmp_path, qtbot)
    for_you = view.feed_pane(CollectionTargetKind.FOR_YOU)
    following = view.feed_pane(CollectionTargetKind.FOLLOWING)
    for_you_target = type("Target", (), {"kind": CollectionTargetKind.FOR_YOU})()
    for_you_request = type("Request", (), {"target": for_you_target})()
    coordinator.busy = True
    coordinator.state_changed.emit(
        type(
            "State",
            (),
            {"active": for_you_request, "message": "Collecting For You"},
        )()
    )

    coordinator.request_completed.emit(collection_result(18))
    following_target = type("Target", (), {"kind": CollectionTargetKind.FOLLOWING})()
    following_request = type("Request", (), {"target": following_target})()
    coordinator.state_changed.emit(
        type(
            "State",
            (),
            {"active": following_request, "message": "Collecting Following"},
        )()
    )

    assert for_you.status_banner.label.text() == "For You collection complete"
    assert following.status_banner.label.text() == "Collecting Following"


def test_worker_converts_callable_exception_to_failed_signal() -> None:
    def fail() -> object:
        raise RuntimeError("boom")

    worker = Worker(fail)
    failures: list[str] = []
    worker.failed.connect(failures.append)
    worker.run()

    assert failures == ["boom"]


def test_saved_tab_renders_every_manual_post(tmp_path, qtbot) -> None:
    view, *_ = build_view(
        tmp_path,
        qtbot,
        manual=[saved_post(41, text="first kept"), saved_post(42, text="second kept")],
    )

    rendered = view.saved_pane.web_view.rendered[-1][0]

    assert "first kept" in rendered
    assert "second kept" in rendered


def test_saved_tab_shows_empty_state_without_manual_posts(tmp_path, qtbot) -> None:
    view, *_ = build_view(tmp_path, qtbot)

    rendered = view.saved_pane.web_view.rendered[-1][0]

    assert "No saved posts yet" in rendered


def test_manual_save_success_refreshes_saved_tab(tmp_path, qtbot) -> None:
    saved = saved_post(43, text="freshly saved")
    view, *_ = build_view(
        tmp_path,
        qtbot,
        manual=[saved],
        executor=immediate_result(SaveResult(SaveStatus.SAVED, saved, "Saved")),
    )
    renders = len(view.saved_pane.web_view.rendered)
    view.manual_saves.url_field.setText(saved.canonical_url)

    view.manual_saves.save_button.click()

    assert len(view.saved_pane.web_view.rendered) == renders + 1
    assert "freshly saved" in view.saved_pane.web_view.rendered[-1][0]
    assert 'id="post-43" class="post-card focused"' in view.saved_pane.web_view.rendered[-1][0]
