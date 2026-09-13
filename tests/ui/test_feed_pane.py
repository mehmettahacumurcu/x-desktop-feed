import pytest

from xfeed.domain import CollectionTargetKind
from xfeed.media import MediaAssetState
from xfeed.ui.feed_pane import FeedPane
from xfeed.ui.settings import UiSettingsStore

from tests.ui.test_feed_view import (
    CollectionRepositoryDouble,
    CoordinatorDouble,
    MediaEvents,
    MediaRepositoryDouble,
    SchedulerDouble,
    WebViewDouble,
    media_asset,
    observed,
    saved_post,
)


def build_pane(
    tmp_path,
    qtbot,
    kind: CollectionTargetKind,
    *,
    current=None,
    older=None,
):
    collections = CollectionRepositoryDouble(current, older)
    coordinator = CoordinatorDouble()
    scheduler = SchedulerDouble()
    settings = UiSettingsStore(tmp_path / "ui.ini")
    web = WebViewDouble()
    pane = FeedPane(
        kind,
        collections,  # type: ignore[arg-type]
        coordinator,  # type: ignore[arg-type]
        scheduler,  # type: ignore[arg-type]
        settings,
        web_factory=lambda: web,
        media_repository=None,
        asset_changed=None,
    )
    qtbot.addWidget(pane)
    return pane, collections, coordinator, scheduler, settings, web


def test_following_pane_uses_target_specific_collection_paths(tmp_path, qtbot) -> None:
    pane, collections, coordinator, _scheduler, _settings, web = build_pane(
        tmp_path,
        qtbot,
        CollectionTargetKind.FOLLOWING,
        current=[observed(saved_post(2, text="following now"))],
        older=[observed(saved_post(1, text="following before"))],
    )
    coordinator.current_run_ids_by_kind[CollectionTargetKind.FOLLOWING] = (17,)

    pane.show_older_checkbox.setChecked(True)

    assert collections.current_calls[-1] == (CollectionTargetKind.FOLLOWING, (17,))
    assert collections.older_calls[-1] == (
        CollectionTargetKind.FOLLOWING,
        (17,),
        (2,),
        200,
    )
    assert "following now" in web.rendered[-1][0]
    assert "following before" in web.rendered[-1][0]


def test_following_pane_persists_controls_and_collects_only_following(tmp_path, qtbot) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")
    settings.set_feed_count(CollectionTargetKind.FOLLOWING, 30)
    settings.set_feed_auto_interval_minutes(CollectionTargetKind.FOLLOWING, 10)
    pane, _collections, coordinator, scheduler, _settings, _web = build_pane(
        tmp_path,
        qtbot,
        CollectionTargetKind.FOLLOWING,
    )

    assert pane.selected_count() == 30
    assert pane.interval_combo.currentData() == 10

    pane.set_selected_count(50)
    pane.interval_combo.setCurrentIndex(pane.interval_combo.findData(5))
    pane.collect_button.click()

    assert settings.feed_count(CollectionTargetKind.FOLLOWING) == 50
    assert scheduler.maximums[-1] == 50
    assert scheduler.intervals[-1] == 5
    assert coordinator.feed_calls == [(CollectionTargetKind.FOLLOWING, 50, True, pane)]


def test_feed_pane_rejects_non_home_feed_kind(tmp_path, qtbot) -> None:
    with pytest.raises(ValueError, match="home feed kind"):
        build_pane(tmp_path, qtbot, CollectionTargetKind.SOURCE)


def test_feed_pane_reloads_media_when_asset_changes(tmp_path, qtbot) -> None:
    post = saved_post(31)
    repository = MediaRepositoryDouble([media_asset(99, post.id, MediaAssetState.PENDING)])
    events = MediaEvents()
    collections = CollectionRepositoryDouble([observed(post)])
    coordinator = CoordinatorDouble()
    coordinator.current_run_ids_by_kind[CollectionTargetKind.FOR_YOU] = (8,)
    web = WebViewDouble()
    pane = FeedPane(
        CollectionTargetKind.FOR_YOU,
        collections,  # type: ignore[arg-type]
        coordinator,  # type: ignore[arg-type]
        SchedulerDouble(),  # type: ignore[arg-type]
        UiSettingsStore(tmp_path / "ui.ini"),
        web_factory=lambda: web,
        media_repository=repository,  # type: ignore[arg-type]
        asset_changed=events.asset_changed,
    )
    qtbot.addWidget(pane)
    initial_renders = len(web.rendered)

    events.asset_changed.emit(99)

    assert len(web.rendered) == initial_renders + 1
    assert repository.calls[-1] == (post.id,)
