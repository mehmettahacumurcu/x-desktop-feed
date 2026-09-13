from collections.abc import Callable, Sequence
from typing import Protocol, cast

from PySide6.QtCore import QUrl, Qt, SignalInstance, Slot
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from xfeed.i18n import tr
from xfeed.media_repository import MediaRepository
from xfeed.repositories import PostRepository, SourceRepository
from xfeed.sources import SourceRecord, canonicalize_profile
from xfeed.ui.components import PageHeader, StatusBanner
from xfeed.ui.feed_html import FeedHtmlBuilder, FeedSection, media_for_posts
from xfeed.ui.settings import SOURCE_COUNTS, UiSettingsStore
from xfeed.ui.source_card import SourceCard


class _FeedRenderer(Protocol):
    def setHtml(self, html: str, base_url: QUrl) -> None: ...


class _Coordinator(Protocol):
    state_changed: SignalInstance
    request_completed: SignalInstance
    queue_finished: SignalInstance

    @property
    def busy(self) -> bool: ...

    def collect_source(
        self,
        source: SourceRecord,
        maximum: int,
        *,
        parent: QWidget | None,
    ) -> bool: ...

    def collect_all(
        self,
        sources: Sequence[SourceRecord],
        maximum: int,
        *,
        parent: QWidget | None,
    ) -> bool: ...

    def cancel(self) -> None: ...


class SourcesView(QWidget):
    def __init__(
        self,
        sources: SourceRepository,
        posts: PostRepository,
        coordinator: _Coordinator,
        settings: UiSettingsStore,
        *,
        web_factory: Callable[[], QWidget] | None = None,
        media_repository: MediaRepository | None = None,
        asset_changed: SignalInstance | None = None,
    ) -> None:
        super().__init__()
        self.sources = sources
        self.posts = posts
        self.coordinator = coordinator
        self.settings = settings
        self._media_repository = media_repository
        self.builder = FeedHtmlBuilder()
        self.cards: dict[int, SourceCard] = {}
        self._records: list[SourceRecord] = []
        self._selected_ids: set[int] = set()

        header = PageHeader(
            tr("Sources"),
            tr("Build a small library of profiles and read their saved posts together."),
        )
        self._page_header = header
        self.add_field = QLineEdit()
        self.add_field.setPlaceholderText(tr("@handle or X profile URL"))
        self.add_button = QPushButton(tr("Add profile"))
        self.add_button.setProperty("primary", True)
        self.collect_all_amount = QComboBox()
        for value in SOURCE_COUNTS:
            self.collect_all_amount.addItem(str(value), value)
        self.collect_all_amount.setCurrentIndex(
            self.collect_all_amount.findData(settings.collect_all_count())
        )
        self.collect_all_button = QPushButton(tr("Collect all"))
        self.collect_all_button.setProperty("primary", True)
        self.cancel_button = QPushButton(tr("Cancel"))
        self.cancel_button.setProperty("danger", True)
        self.cancel_button.hide()

        toolbar = QFrame()
        toolbar.setObjectName("card")
        self._each_profile_label = QLabel(tr("Each profile"))
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(14, 12, 14, 12)
        toolbar_layout.addWidget(self.add_field, 1)
        toolbar_layout.addWidget(self.add_button)
        toolbar_layout.addSpacing(16)
        toolbar_layout.addWidget(self._each_profile_label)
        toolbar_layout.addWidget(self.collect_all_amount)
        toolbar_layout.addWidget(self.collect_all_button)
        toolbar_layout.addWidget(self.cancel_button)
        self.error_label = QLabel()
        self.error_label.setObjectName("mutedLabel")
        self.error_label.setWordWrap(True)
        self.status_banner = StatusBanner(tr("Choose profiles to inspect or collect them all"))

        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 8, 0)
        self.cards_layout.setSpacing(10)
        self.cards_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.cards_container)
        self.source_panel = QWidget()
        self.source_panel.setMinimumWidth(300)
        source_layout = QVBoxLayout(self.source_panel)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.addWidget(scroll)

        self.selection_label = QLabel(tr("Saved posts"))
        self.selection_label.setStyleSheet("font-size: 16px; font-weight: 700")
        web_widget = QWebEngineView() if web_factory is None else web_factory()
        self.web_view = cast(_FeedRenderer, web_widget)
        self.posts_panel = QWidget()
        self.posts_panel.setMinimumWidth(520)
        posts_layout = QVBoxLayout(self.posts_panel)
        posts_layout.setContentsMargins(8, 0, 0, 0)
        posts_layout.setSpacing(8)
        posts_layout.addWidget(self.selection_label)
        posts_layout.addWidget(web_widget, 1)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.source_panel)
        self.splitter.addWidget(self.posts_panel)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setSizes(list(settings.source_splitter_sizes()))
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(11)
        layout.addWidget(header)
        layout.addWidget(toolbar)
        layout.addWidget(self.error_label)
        layout.addWidget(self.status_banner)
        layout.addWidget(self.splitter, 1)

        self.add_button.clicked.connect(self.add_source)
        self.add_field.returnPressed.connect(self.add_source)
        self.collect_all_amount.currentIndexChanged.connect(self._collect_all_amount_changed)
        self.collect_all_button.clicked.connect(self._collect_all)
        self.cancel_button.clicked.connect(coordinator.cancel)
        coordinator.state_changed.connect(self._coordinator_state_changed)
        coordinator.request_completed.connect(lambda _result: self.refresh_from_storage())
        coordinator.queue_finished.connect(self.refresh_from_storage)
        if asset_changed is not None:
            asset_changed.connect(self._asset_changed)

        self._rebuild_cards()

    @Slot()
    def add_source(self) -> None:
        try:
            profile = canonicalize_profile(self.add_field.text())
            self.sources.add(profile)
        except (ValueError, RuntimeError) as error:
            self.error_label.setText(str(error))
            return
        self.add_field.clear()
        self.error_label.clear()
        self._rebuild_cards()

    @Slot()
    def refresh_from_storage(self) -> None:
        self._rebuild_cards()

    @Slot()
    def apply_language(self) -> None:
        self._page_header.title_label.setText(tr("Sources"))
        self._page_header.subtitle_label.setText(
            tr("Build a small library of profiles and read their saved posts together.")
        )
        self.add_field.setPlaceholderText(tr("@handle or X profile URL"))
        self.add_button.setText(tr("Add profile"))
        self.collect_all_button.setText(tr("Collect all"))
        self.cancel_button.setText(tr("Cancel"))
        self._each_profile_label.setText(tr("Each profile"))
        self.status_banner.set_text(tr("Choose profiles to inspect or collect them all"))
        self._rebuild_cards()
        self._render_selection()

    def save_ui_state(self) -> None:
        sizes = self.splitter.sizes()
        if len(sizes) == 2 and all(value > 0 for value in sizes):
            self.settings.set_source_splitter_sizes(sizes[0], sizes[1])

    def _rebuild_cards(self) -> None:
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.cards.clear()
        self._records = self.sources.list_all()
        available_ids = {record.id for record in self._records}
        self._selected_ids.intersection_update(available_ids)
        for record in self._records:
            card = SourceCard(record, self.settings)
            card.set_selected(record.id in self._selected_ids)
            card.set_collection_locked(self.coordinator.busy)
            card.selection_changed.connect(self._selection_changed)
            card.collect_requested.connect(self._collect_source)
            card.enabled_requested.connect(self._set_enabled)
            card.remove_requested.connect(self._remove_source)
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
            self.cards[record.id] = card
        self._render_selection()
        self._update_actions()

    def _selection_changed(self, source_id: int, selected: bool) -> None:
        if selected:
            self._selected_ids.add(source_id)
        else:
            self._selected_ids.discard(source_id)
        self._render_selection()

    def _render_selection(self) -> None:
        if not self._records:
            self.selection_label.setText(tr("Saved posts"))
            rendered = self.builder.render_sections(
                (),
                empty_title=tr("Add your first profile"),
                empty_body=tr("Use the field above to start a focused source library."),
            )
        else:
            selected = [item for item in self._records if item.id in self._selected_ids]
            if not selected:
                self.selection_label.setText(tr("Saved posts"))
                rendered = self.builder.render_sections(
                    (),
                    empty_title=tr("Select one or more profiles"),
                    empty_body=tr("Checked profiles are combined here from newest to oldest."),
                )
            else:
                handles = tuple(item.handle for item in selected)
                self.selection_label.setText(" + ".join(f"@{handle}" for handle in handles))
                posts = self.posts.list_for_authors(handles, limit=200)
                media_by_post = (
                    media_for_posts(self._media_repository, posts)
                    if self._media_repository is not None
                    else {}
                )
                rendered = self.builder.render_sections(
                    (FeedSection(tr("Saved profile posts"), posts),) if posts else (),
                    media_by_post=media_by_post,
                    empty_title=tr("No saved posts for this selection"),
                    empty_body=tr("Collect posts from a selected profile to populate this reader."),
                )
        self.web_view.setHtml(rendered, QUrl("https://x.com"))

    @Slot(int)
    def _asset_changed(self, _asset_id: int) -> None:
        self._render_selection()

    def _collect_source(self, source: SourceRecord, maximum: int) -> None:
        self.coordinator.collect_source(source, maximum, parent=self)

    def _collect_all(self) -> None:
        maximum = self.collect_all_amount.currentData()
        if not isinstance(maximum, int):
            return
        enabled = tuple(item for item in self._records if item.enabled)
        self.coordinator.collect_all(enabled, maximum, parent=self)

    def _collect_all_amount_changed(self, _index: int) -> None:
        maximum = self.collect_all_amount.currentData()
        if isinstance(maximum, int):
            self.settings.set_collect_all_count(maximum)

    def _set_enabled(self, source_id: int, enabled: bool) -> None:
        self.sources.set_enabled(source_id, enabled)
        self._rebuild_cards()

    def _remove_source(self, source_id: int) -> None:
        self.sources.remove(source_id)
        self._selected_ids.discard(source_id)
        self._rebuild_cards()

    def _coordinator_state_changed(self, state: object) -> None:
        self.status_banner.set_text(str(getattr(state, "message", "Collection state changed")))
        self._update_actions()

    def _update_actions(self) -> None:
        busy = self.coordinator.busy
        self.add_field.setEnabled(not busy)
        self.add_button.setEnabled(not busy)
        self.collect_all_amount.setEnabled(not busy)
        self.collect_all_button.setEnabled(not busy and any(item.enabled for item in self._records))
        self.cancel_button.setVisible(busy)
        for card in self.cards.values():
            card.set_collection_locked(busy)
