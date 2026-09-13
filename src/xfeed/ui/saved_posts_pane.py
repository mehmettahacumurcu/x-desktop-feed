from collections.abc import Callable
from typing import Protocol, cast

from PySide6.QtCore import QUrl, SignalInstance, Slot
from PySide6.QtWidgets import QVBoxLayout, QWidget

from xfeed.i18n import tr
from xfeed.media_repository import MediaRepository
from xfeed.repositories import PostRepository
from xfeed.ui.feed_html import FeedHtmlBuilder, media_for_posts


class _FeedRenderer(Protocol):
    def setHtml(self, html: str, base_url: QUrl) -> None: ...


class SavedPostsPane(QWidget):
    """Reader-only tab showing every manually saved post."""

    def __init__(
        self,
        posts: PostRepository,
        *,
        web_factory: Callable[[], QWidget],
        media_repository: MediaRepository | None = None,
        asset_changed: SignalInstance | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("savedPostsPane")
        self._posts = posts
        self._media_repository = media_repository
        self._builder = FeedHtmlBuilder()

        web_widget = web_factory()
        self.web_view = cast(_FeedRenderer, web_widget)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(web_widget, 1)

        if asset_changed is not None:
            asset_changed.connect(self._asset_changed)

        self.reload()

    @Slot()
    def reload(self, focus_post_id: int | None = None) -> None:
        saved_posts = self._posts.list_manual()
        media_by_post = (
            media_for_posts(self._media_repository, saved_posts)
            if self._media_repository is not None
            else {}
        )
        if saved_posts:
            rendered = self._builder.render(
                saved_posts,
                focus_post_id,
                media_by_post=media_by_post,
            )
        else:
            rendered = self._builder.render_sections(
                (),
                focus_post_id=focus_post_id,
                media_by_post=media_by_post,
                empty_title=tr("No saved posts yet"),
                empty_body=tr("Save a post by URL to keep it here."),
            )
        self.web_view.setHtml(rendered, QUrl("https://x.com"))

    @Slot()
    def apply_language(self) -> None:
        self.reload()

    @Slot(int)
    def _asset_changed(self, _asset_id: int) -> None:
        self.reload()
