from collections.abc import Callable, Mapping

from PySide6.QtCore import SignalInstance, Slot
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QPushButton, QStackedWidget, QTabWidget, QVBoxLayout, QWidget

from xfeed.domain import CollectionTargetKind
from xfeed.i18n import tr
from xfeed.media_repository import MediaRepository
from xfeed.posts import PostService
from xfeed.repositories import CollectionRepository, PostRepository
from xfeed.ui.components import PageHeader
from xfeed.ui.feed_pane import FeedPane, _Coordinator, _Scheduler
from xfeed.ui.manual_saves_panel import (
    ManualSavesPanel,
    MediaResolutionRequester,
    SaveExecutor,
)
from xfeed.ui.saved_posts_pane import SavedPostsPane
from xfeed.ui.settings import UiSettingsStore


_HOME_FEED_KINDS = (
    CollectionTargetKind.FOR_YOU,
    CollectionTargetKind.FOLLOWING,
)
_TAB_NAMES = {
    CollectionTargetKind.FOR_YOU: "For You",
    CollectionTargetKind.FOLLOWING: "Following",
}


class FeedView(QWidget):
    def __init__(
        self,
        posts: PostRepository,
        post_service: PostService,
        collections: CollectionRepository,
        web_factory: Callable[[], QWidget] | None = None,
        save_executor: SaveExecutor | None = None,
        *,
        coordinator: _Coordinator,
        schedulers: Mapping[CollectionTargetKind, _Scheduler],
        settings: UiSettingsStore,
        manual_web_factory: Callable[[], QWidget] | None = None,
        saved_web_factory: Callable[[], QWidget] | None = None,
        media_repository: MediaRepository | None = None,
        asset_changed: SignalInstance | None = None,
        media_resolution_requester: MediaResolutionRequester | None = None,
    ) -> None:
        super().__init__()

        missing = tuple(kind for kind in _HOME_FEED_KINDS if kind not in schedulers)
        if missing:
            raise ValueError("schedulers must contain both home feed kinds")

        self.posts = posts
        self.collections = collections
        self.coordinator = coordinator
        self.schedulers = schedulers
        self.settings = settings

        self.save_url_button = QPushButton(tr("Save URL"))
        self.save_url_button.setProperty("primary", True)
        header = PageHeader(
            tr("My Feed"),
            tr("Switch X timelines, collect deliberately, and read each channel without mixing."),
            self.save_url_button,
        )

        feed_web_factory = web_factory or (lambda: QWebEngineView())
        self._tabs = QTabWidget()
        self._tabs.setObjectName("feedTabs")
        self._tabs.setDocumentMode(True)
        self._tabs.setUsesScrollButtons(False)
        self._tabs.tabBar().setExpanding(True)
        self._tabs.tabBar().setAccessibleName(tr("X feed channels"))
        self._panes = {
            kind: FeedPane(
                kind,
                collections,
                coordinator,
                schedulers[kind],
                settings,
                web_factory=feed_web_factory,
                media_repository=media_repository,
                asset_changed=asset_changed,
            )
            for kind in _HOME_FEED_KINDS
        }
        for kind in _HOME_FEED_KINDS:
            self._tabs.addTab(self._panes[kind], tr(_TAB_NAMES[kind]))

        self._saved_pane = SavedPostsPane(
            posts,
            web_factory=saved_web_factory or feed_web_factory,
            media_repository=media_repository,
            asset_changed=asset_changed,
        )
        self._tabs.addTab(self._saved_pane, tr("Saved posts"))

        self._manual_panel = ManualSavesPanel(
            posts,
            post_service,
            web_factory=manual_web_factory,
            save_executor=save_executor,
            media_resolution_requester=media_resolution_requester,
            media_repository=media_repository,
            asset_changed=asset_changed,
        )
        self._manual_panel.setMaximumWidth(self._tabs.maximumWidth())

        self._workspace = QStackedWidget()
        self._workspace.addWidget(self._tabs)
        self._workspace.addWidget(self._manual_panel)
        self._workspace.setCurrentWidget(self._tabs)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(header)
        layout.addWidget(self._workspace, 1)

        self.save_url_button.clicked.connect(self._open_manual_saves)
        self._manual_panel.close_requested.connect(self._close_manual_saves)
        self._manual_panel.post_saved.connect(self._manual_post_saved)

    @property
    def manual_saves(self) -> ManualSavesPanel:
        return self._manual_panel

    @property
    def saved_pane(self) -> SavedPostsPane:
        return self._saved_pane

    def tab_names(self) -> tuple[str, ...]:
        return tuple(self._tabs.tabText(index) for index in range(self._tabs.count()))

    @Slot()
    def apply_language(self) -> None:
        for index, kind in enumerate(_HOME_FEED_KINDS):
            self._tabs.setTabText(index, tr(_TAB_NAMES[kind]))
        self._tabs.setTabText(len(_HOME_FEED_KINDS), tr("Saved posts"))
        self.save_url_button.setText(tr("Save URL"))
        self._saved_pane.apply_language()
        self.refresh_from_storage()

    def feed_pane(self, kind: CollectionTargetKind) -> FeedPane:
        if not isinstance(kind, CollectionTargetKind) or kind not in self._panes:
            raise ValueError("kind must be a home feed")
        return self._panes[kind]

    def select_feed(self, kind: CollectionTargetKind) -> None:
        pane = self.feed_pane(kind)
        self._tabs.setCurrentWidget(pane)

    @Slot()
    def refresh_from_storage(self) -> None:
        for pane in self._panes.values():
            pane.reload()
        self._saved_pane.reload()
        self._manual_panel.reload()

    def _manual_post_saved(self, result: object) -> None:
        post = getattr(result, "post", None)
        self._saved_pane.reload(getattr(post, "id", None))

    def _open_manual_saves(self) -> None:
        if self._workspace.currentWidget() is self._tabs:
            self._manual_panel.open_panel()
            self._workspace.setCurrentWidget(self._manual_panel)
        else:
            self._close_manual_saves()

    def _close_manual_saves(self) -> None:
        self._workspace.setCurrentWidget(self._tabs)
