from types import SimpleNamespace

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtWidgets import QWidget

from xfeed.domain import Availability, SavedPost
from xfeed.media import MediaAsset, MediaAssetState
from xfeed.sources import SourceProfile, SourceRecord
from xfeed.ui.settings import UiSettingsStore
from xfeed.ui.sources_view import SourcesView


class SourceRepositoryDouble:
    def __init__(self, records: list[SourceRecord] | None = None) -> None:
        self.records = records or []
        self.enabled_calls: list[tuple[int, bool]] = []
        self.removed: list[int] = []

    def list_all(self) -> list[SourceRecord]:
        return list(self.records)

    def add(self, profile: SourceProfile) -> SourceRecord:
        if any(item.handle == profile.handle for item in self.records):
            raise ValueError("Profile already added")
        item = SourceRecord(
            max((record.id for record in self.records), default=0) + 1,
            profile.handle,
            profile.profile_url,
            True,
            None,
        )
        self.records.append(item)
        return item

    def set_enabled(self, source_id: int, enabled: bool) -> None:
        self.enabled_calls.append((source_id, enabled))
        self.records = [
            SourceRecord(item.id, item.handle, item.profile_url, enabled, item.last_refresh_at)
            if item.id == source_id
            else item
            for item in self.records
        ]

    def remove(self, source_id: int) -> None:
        self.removed.append(source_id)
        self.records = [item for item in self.records if item.id != source_id]


class PostRepositoryDouble:
    def __init__(self, posts: list[SavedPost] | None = None) -> None:
        self.posts = posts or []
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def list_for_authors(self, handles, limit: int = 200) -> list[SavedPost]:
        normalized = tuple(handles)
        self.calls.append((normalized, limit))
        selected = {handle.casefold() for handle in normalized}
        return [post for post in self.posts if (post.author_handle or "").casefold() in selected]


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


class CoordinatorDouble(QObject):
    state_changed = Signal(object)
    request_completed = Signal(object)
    queue_finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.source_calls: list[tuple[SourceRecord, int, object]] = []
        self.all_calls: list[tuple[tuple[SourceRecord, ...], int, object]] = []
        self.cancel_count = 0

    def collect_source(self, source, maximum: int, *, parent) -> bool:
        self.source_calls.append((source, maximum, parent))
        return True

    def collect_all(self, sources, maximum: int, *, parent) -> bool:
        self.all_calls.append((tuple(sources), maximum, parent))
        return True

    def cancel(self) -> None:
        self.cancel_count += 1


class WebViewDouble(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.rendered: list[tuple[str, QUrl]] = []

    def setHtml(self, html: str, base_url: QUrl) -> None:
        self.rendered.append((html, base_url))


def source(source_id: int, handle: str, *, enabled: bool = True) -> SourceRecord:
    return SourceRecord(source_id, handle, f"https://x.com/{handle}", enabled, None)


def post(post_id: int, author: str, published: str) -> SavedPost:
    return SavedPost(
        canonical_url=f"https://x.com/{author}/status/{post_id}",
        x_post_id=str(post_id),
        author_handle=author,
        author_name=author.title(),
        text=f"{author} post {post_id}",
        published_at=published,
        embed_html="",
        provider_json="{}",
        source_method="public-dom",
        availability=Availability.AVAILABLE,
        id=post_id,
    )


def media_asset(asset_id: int, post_id: int, state: MediaAssetState) -> MediaAsset:
    available = state is MediaAssetState.AVAILABLE
    return MediaAsset(
        id=asset_id,
        post_id=post_id,
        position=0,
        source_url=f"https://pbs.twimg.com/media/{asset_id}?format=jpg&name=large",
        alt_text=None,
        state=state,
        local_path=f"{post_id}/photo-0.jpg" if available else None,
        mime_type="image/jpeg" if available else None,
        width=80 if available else None,
        height=60 if available else None,
        attempts=1,
        diagnostic="fixture failure" if state is MediaAssetState.FAILED else None,
        created_at="2026-07-24 10:00:00",
        updated_at="2026-07-24 10:00:00",
    )


def build_view(
    tmp_path,
    qtbot,
    *,
    records=None,
    posts=None,
    media_repository: MediaRepositoryDouble | None = None,
    media_events: MediaEvents | None = None,
):
    sources = SourceRepositoryDouble(records)
    post_repository = PostRepositoryDouble(posts)
    coordinator = CoordinatorDouble()
    settings = UiSettingsStore(tmp_path / "ui.ini")
    web = WebViewDouble()
    view = SourcesView(
        sources,
        post_repository,
        coordinator,
        settings,
        web_factory=lambda: web,
        media_repository=media_repository,
        asset_changed=media_events.asset_changed if media_events is not None else None,
    )
    qtbot.addWidget(view)
    return view, sources, post_repository, coordinator, settings, web


def test_sources_toolbar_owns_add_profile_global_count_and_collect_all(tmp_path, qtbot) -> None:
    view, *_ = build_view(tmp_path, qtbot, records=[source(1, "alpha")])

    assert view.add_field.placeholderText() == "@handle or X profile URL"
    assert view.add_button.text() == "Add profile"
    assert [
        view.collect_all_amount.itemData(i) for i in range(view.collect_all_amount.count())
    ] == [
        5,
        10,
        20,
        30,
    ]
    assert view.collect_all_button.text() == "Collect all"
    assert not hasattr(view, "for_you_button")


def test_add_profile_rebuilds_visible_cards_and_reports_duplicates(tmp_path, qtbot) -> None:
    view, sources, *_ = build_view(tmp_path, qtbot)
    assert view.cards == {}

    view.add_field.setText("@OpenAI")
    view.add_button.click()

    assert [item.handle for item in sources.records] == ["openai"]
    assert list(view.cards) == [sources.records[0].id]
    view.add_field.setText("openai")
    view.add_button.click()
    assert "already" in view.error_label.text().casefold()


def test_per_profile_and_collect_all_counts_remain_independent(tmp_path, qtbot) -> None:
    alpha = source(1, "alpha")
    beta = source(2, "beta")
    view, _sources, _posts, coordinator, settings, _web = build_view(
        tmp_path, qtbot, records=[alpha, beta]
    )

    view.cards[1].amount_combo.setCurrentIndex(view.cards[1].amount_combo.findData(30))
    view.collect_all_amount.setCurrentIndex(view.collect_all_amount.findData(20))
    view.cards[1].collect_button.click()
    view.collect_all_button.click()

    assert settings.source_count(1) == 30
    assert settings.collect_all_count() == 20
    assert coordinator.source_calls == [(alpha, 30, view)]
    assert coordinator.all_calls == [((alpha, beta), 20, view)]


def test_collect_all_skips_disabled_profiles_but_keeps_their_cards(tmp_path, qtbot) -> None:
    alpha = source(1, "alpha")
    beta = source(2, "beta", enabled=False)
    view, _sources, _posts, coordinator, _settings, _web = build_view(
        tmp_path, qtbot, records=[alpha, beta]
    )

    view.collect_all_button.click()

    assert set(view.cards) == {1, 2}
    assert coordinator.all_calls[0][0] == (alpha,)
    assert not view.cards[2].collect_button.isEnabled()


def test_multi_selection_queries_combined_feed_without_changing_enabled_state(
    tmp_path, qtbot
) -> None:
    alpha = source(1, "alpha")
    beta = source(2, "beta", enabled=False)
    rows = [post(3, "beta", "2026-07-21"), post(2, "alpha", "2026-07-20")]
    view, sources, posts, _coordinator, _settings, web = build_view(
        tmp_path, qtbot, records=[alpha, beta], posts=rows
    )

    view.cards[1].selected_checkbox.setChecked(True)
    view.cards[2].selected_checkbox.setChecked(True)

    assert posts.calls[-1] == (("alpha", "beta"), 200)
    html = web.rendered[-1][0]
    assert html.index("beta post 3") < html.index("alpha post 2")
    assert sources.enabled_calls == []
    assert not sources.records[1].enabled


def test_selected_reader_queries_only_displayed_posts_and_keeps_selection_on_media_event(
    tmp_path, qtbot
) -> None:
    alpha = source(1, "alpha")
    beta = source(2, "beta")
    alpha_post = post(41, "alpha", "2026-07-21")
    beta_post = post(42, "beta", "2026-07-20")
    repository = MediaRepositoryDouble(
        [
            media_asset(301, alpha_post.id, MediaAssetState.AVAILABLE),
            media_asset(302, alpha_post.id, MediaAssetState.PENDING),
            media_asset(303, beta_post.id, MediaAssetState.FAILED),
            media_asset(304, 999, MediaAssetState.PENDING),
        ]
    )
    events = MediaEvents()
    view, _sources, _posts, _coordinator, _settings, web = build_view(
        tmp_path,
        qtbot,
        records=[alpha, beta],
        posts=[alpha_post, beta_post],
        media_repository=repository,
        media_events=events,
    )
    view.cards[1].selected_checkbox.setChecked(True)
    view.cards[2].selected_checkbox.setChecked(True)

    assert repository.calls[-1] == (alpha_post.id, beta_post.id)
    assert "xfeed-media://asset/301" in web.rendered[-1][0]
    assert "Saving photos" in web.rendered[-1][0]
    assert "couldn&#x27;t be saved" in web.rendered[-1][0]
    assert "asset/304" not in web.rendered[-1][0]

    events.asset_changed.emit(304)

    assert view._selected_ids == {1, 2}
    assert view.cards[1].selected_checkbox.isChecked()
    assert view.cards[2].selected_checkbox.isChecked()
    assert repository.calls[-1] == (alpha_post.id, beta_post.id)


def test_enable_and_remove_are_secondary_card_actions(tmp_path, qtbot) -> None:
    beta = source(2, "beta", enabled=False)
    view, sources, *_ = build_view(tmp_path, qtbot, records=[beta])

    view.cards[2].enabled_button.click()
    assert sources.enabled_calls == [(2, True)]
    view.cards[2].remove_button.click()

    assert sources.removed == [2]
    assert view.cards == {}


def test_splitter_has_readable_minimums_and_persists_sizes(tmp_path, qtbot) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")
    settings.set_source_splitter_sizes(390, 790)
    view, *_ = build_view(tmp_path, qtbot, records=[source(1, "alpha")])
    view.show()
    qtbot.wait(20)

    assert view.source_panel.minimumWidth() >= 300
    assert view.posts_panel.minimumWidth() >= 520
    assert view.splitter.sizes()[0] > 0
    view.splitter.setSizes([410, 770])
    view.save_ui_state()
    left, right = settings.source_splitter_sizes()
    assert left > 0 and right > 0


def test_empty_states_cover_no_profiles_no_selection_and_no_posts(tmp_path, qtbot) -> None:
    view, _sources, _posts, _coordinator, _settings, web = build_view(tmp_path, qtbot)
    assert "Add your first profile" in web.rendered[-1][0]

    view, _sources, _posts, _coordinator, _settings, web = build_view(
        tmp_path, qtbot, records=[source(1, "alpha")]
    )
    assert "Select one or more profiles" in web.rendered[-1][0]
    view.cards[1].selected_checkbox.setChecked(True)
    assert "No saved posts for this selection" in web.rendered[-1][0]


def test_active_coordinator_disables_conflicts_and_exposes_cancel(tmp_path, qtbot) -> None:
    view, _sources, _posts, coordinator, _settings, _web = build_view(
        tmp_path, qtbot, records=[source(1, "alpha")]
    )
    coordinator.busy = True

    coordinator.state_changed.emit(SimpleNamespace(message="Collecting @alpha · 1 of 2"))

    assert not view.add_button.isEnabled()
    assert not view.collect_all_button.isEnabled()
    assert view.cancel_button.isVisibleTo(view)
    view.cancel_button.click()
    assert coordinator.cancel_count == 1
