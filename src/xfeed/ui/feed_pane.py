from collections.abc import Callable, Sequence
from typing import Protocol, cast

from PySide6.QtCore import QUrl, SignalInstance, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from xfeed.domain import CollectionStatus, CollectionTargetKind, ObservedPost
from xfeed.i18n import tr
from xfeed.media_repository import MediaRepository
from xfeed.repositories import CollectionRepository
from xfeed.ui.components import StatusBanner
from xfeed.ui.feed_html import FeedHtmlBuilder, FeedSection, media_for_posts
from xfeed.ui.settings import AUTO_INTERVAL_MINUTES, MY_FEED_COUNTS, UiSettingsStore


class _JavaScriptPage(Protocol):
    def runJavaScript(self, script: str) -> None: ...


class _FeedRenderer(Protocol):
    def setHtml(self, html: str, base_url: QUrl) -> None: ...

    def page(self) -> _JavaScriptPage: ...


class _Coordinator(Protocol):
    state_changed: SignalInstance
    request_completed: SignalInstance
    queue_finished: SignalInstance

    @property
    def busy(self) -> bool: ...

    def current_run_ids(self, kind: CollectionTargetKind) -> tuple[int, ...]: ...

    def collect_feed(
        self,
        kind: CollectionTargetKind,
        maximum: int,
        *,
        interactive: bool,
        parent: QWidget | None,
    ) -> bool: ...


class _Scheduler(Protocol):
    countdown_changed: SignalInstance
    status_changed: SignalInstance

    @property
    def remaining_seconds(self) -> int | None: ...

    def set_interval(self, value: int) -> None: ...

    def set_maximum(self, value: int) -> None: ...


_DISPLAY_NAMES = {
    CollectionTargetKind.FOR_YOU: "For You",
    CollectionTargetKind.FOLLOWING: "Following",
}


class FeedPane(QWidget):
    def __init__(
        self,
        kind: CollectionTargetKind,
        collections: CollectionRepository,
        coordinator: _Coordinator,
        scheduler: _Scheduler,
        settings: UiSettingsStore,
        *,
        web_factory: Callable[[], QWidget],
        media_repository: MediaRepository | None,
        asset_changed: SignalInstance | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(kind, CollectionTargetKind) or kind not in _DISPLAY_NAMES:
            raise ValueError("FeedPane requires a home feed kind")
        self._kind = kind
        self._display_name = _DISPLAY_NAMES[kind]
        self._collections = collections
        self._coordinator = coordinator
        self._scheduler = scheduler
        self._settings = settings
        self._media_repository = media_repository
        self._builder = FeedHtmlBuilder()
        self._reader_scroll = 0
        self._owns_active_status = False
        self._displayed_asset_ids: set[int] = set()

        self.amount_combo = QComboBox()
        for value in MY_FEED_COUNTS:
            self.amount_combo.addItem(str(value), value)
        self.interval_combo = QComboBox()
        labels = {
            0: tr("Never"),
            5: tr("5 min"),
            10: tr("10 min"),
            30: tr("30 min"),
            60: tr("60 min"),
        }
        for value in AUTO_INTERVAL_MINUTES:
            self.interval_combo.addItem(labels[value], value)
        self.amount_combo.setCurrentIndex(self.amount_combo.findData(settings.feed_count(kind)))
        self.interval_combo.setCurrentIndex(
            self.interval_combo.findData(settings.feed_auto_interval_minutes(kind))
        )
        self.collect_button = QPushButton(tr("Collect {feed}").format(feed=tr(self._display_name)))
        self.collect_button.setProperty("primary", True)
        self.countdown_label = QLabel(tr("Automatic collection is off"))
        self.countdown_label.setObjectName("mutedLabel")

        control_card = QFrame()
        control_card.setObjectName("card")
        controls = QVBoxLayout(control_card)
        controls.setContentsMargins(16, 14, 16, 14)
        controls.setSpacing(8)
        choices = QHBoxLayout()
        choices.setSpacing(10)
        choices.addWidget(QLabel(tr("Posts")))
        choices.addWidget(self.amount_combo)
        choices.addSpacing(8)
        choices.addWidget(QLabel(tr("Automatic")))
        choices.addWidget(self.interval_combo)
        choices.addStretch()
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addWidget(self.countdown_label)
        actions.addStretch()
        actions.addWidget(self.collect_button)
        controls.addLayout(choices)
        controls.addLayout(actions)

        self.status_banner = StatusBanner(
            tr("Ready to collect a fresh {feed} feed").format(feed=tr(self._display_name))
        )
        self.show_older_checkbox = QCheckBox(tr("Show older posts"))
        self.jump_older_button = QPushButton(tr("Jump to older sessions"))
        self.jump_older_button.setEnabled(False)
        history_row = QHBoxLayout()
        history_row.setContentsMargins(0, 0, 0, 0)
        history_row.addWidget(self.show_older_checkbox)
        history_row.addStretch()
        history_row.addWidget(self.jump_older_button)

        web_widget = web_factory()
        self.web_view = cast(_FeedRenderer, web_widget)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(control_card)
        layout.addWidget(self.status_banner)
        layout.addLayout(history_row)
        layout.addWidget(web_widget, 1)

        self.amount_combo.currentIndexChanged.connect(self._amount_changed)
        self.interval_combo.currentIndexChanged.connect(self._interval_changed)
        self.collect_button.clicked.connect(self._collect_now)
        self.show_older_checkbox.toggled.connect(lambda _checked: self.reload())
        self.jump_older_button.clicked.connect(self._jump_to_older)
        coordinator.state_changed.connect(self._coordinator_state_changed)
        coordinator.request_completed.connect(self._request_completed)
        scheduler.countdown_changed.connect(self._countdown_changed)
        scheduler.status_changed.connect(self.status_banner.set_text)
        if asset_changed is not None:
            asset_changed.connect(self._asset_changed)

        self.reload()

    @property
    def kind(self) -> CollectionTargetKind:
        return self._kind

    def selected_count(self) -> int:
        value = self.amount_combo.currentData()
        return value if isinstance(value, int) else MY_FEED_COUNTS[0]

    def set_selected_count(self, value: int) -> None:
        index = self.amount_combo.findData(value)
        if index < 0:
            raise ValueError("value is not a supported My Feed count")
        self.amount_combo.setCurrentIndex(index)

    def set_reader_scroll_for_test(self, value: int) -> None:
        self._reader_scroll = value
        self.web_view.page().runJavaScript(f"window.scrollTo(0, {value});")

    def reader_scroll_for_test(self) -> int:
        return self._reader_scroll

    @Slot()
    def reload(self) -> None:
        run_ids = self._coordinator.current_run_ids(self._kind)
        current_rows: Sequence[ObservedPost] = ()
        if run_ids:
            current_rows = self._collections.feed_posts_for_runs(self._kind, run_ids)
        current_posts = [row.post for row in current_rows]
        sections: list[FeedSection] = []
        if current_posts:
            sections.append(FeedSection(tr("This session"), current_posts))

        older_rows: Sequence[ObservedPost] = ()
        if self.show_older_checkbox.isChecked():
            older_rows = self._collections.older_feed_posts(
                self._kind,
                excluding_run_ids=run_ids,
                excluding_post_ids=tuple(post.id for post in current_posts),
                limit=200,
            )
        older_posts = [row.post for row in older_rows]
        if older_posts:
            sections.append(
                FeedSection(
                    tr("Older app sessions"),
                    older_posts,
                    anchor="older-sessions",
                    subdued=True,
                )
            )
        self.jump_older_button.setEnabled(bool(older_posts))
        displayed_posts = (*current_posts, *older_posts)
        media_by_post = (
            media_for_posts(self._media_repository, displayed_posts)
            if self._media_repository is not None
            else {}
        )
        self._displayed_asset_ids = {
            asset.id for assets in media_by_post.values() for asset in assets
        }
        rendered = self._builder.render_sections(
            sections,
            media_by_post=media_by_post,
            empty_title=(
                tr("Your fresh feed starts here")
                if self._kind is CollectionTargetKind.FOR_YOU
                else tr("Your Following feed starts here")
            ),
            empty_body=(
                tr("Choose a post count and collect from your X {feed} timeline.").format(
                    feed=tr(self._display_name)
                )
            ),
        )
        self.web_view.setHtml(rendered, QUrl("https://x.com"))

    @Slot(int)
    def _asset_changed(self, asset_id: int) -> None:
        if asset_id in self._displayed_asset_ids:
            self.reload()

    def _amount_changed(self, _index: int) -> None:
        value = self.amount_combo.currentData()
        if not isinstance(value, int):
            return
        self._settings.set_feed_count(self._kind, value)
        self._scheduler.set_maximum(value)

    def _interval_changed(self, _index: int) -> None:
        value = self.interval_combo.currentData()
        if isinstance(value, int):
            self._scheduler.set_interval(value)

    def _collect_now(self) -> None:
        maximum = self.amount_combo.currentData()
        if isinstance(maximum, int):
            self._coordinator.collect_feed(
                self._kind,
                maximum,
                interactive=True,
                parent=self,
            )

    def _coordinator_state_changed(self, state: object) -> None:
        self.collect_button.setEnabled(not self._coordinator.busy)
        active = getattr(state, "active", None)
        if active is not None:
            target = getattr(active, "target", None)
            self._owns_active_status = (
                target is not None and getattr(target, "kind", None) is self._kind
            )
            if not self._owns_active_status:
                return
        elif not self._owns_active_status:
            return
        else:
            self._owns_active_status = False
        message = getattr(state, "message", tr("Collection state changed"))
        self.status_banner.set_text(str(message))

    def _request_completed(self, result: object) -> None:
        run = getattr(result, "run", None)
        if getattr(run, "target_kind", None) is self._kind:
            self.reload()
            self._owns_active_status = False
            status = getattr(run, "status", None)
            if status is CollectionStatus.CANCELLED:
                outcome = tr("collection cancelled")
            elif status is CollectionStatus.FAILED:
                outcome = tr("collection failed")
            else:
                outcome = tr("collection complete")
            self.status_banner.set_text(
                tr("{feed} {outcome}").format(feed=tr(self._display_name), outcome=outcome),
                warning=status is not CollectionStatus.COMPLETED,
            )

    def _countdown_changed(self, remaining: object) -> None:
        if not isinstance(remaining, int):
            self.countdown_label.setText(tr("Automatic collection is off"))
            return
        minutes, seconds = divmod(remaining, 60)
        self.countdown_label.setText(
            tr("Next capture in {minutes}:{seconds:02d}").format(minutes=minutes, seconds=seconds)
        )

    def _jump_to_older(self) -> None:
        self.web_view.page().runJavaScript(
            "document.getElementById('older-sessions')?.scrollIntoView({block: 'start'});"
        )
