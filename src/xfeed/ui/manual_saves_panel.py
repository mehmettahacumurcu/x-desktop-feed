from collections.abc import Callable
from typing import Protocol, cast

from PySide6.QtCore import QThreadPool, QUrl, Signal, SignalInstance, Slot
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from xfeed.posts import PostService, SaveResult, SaveStatus
from xfeed.domain import SavedPost
from xfeed.i18n import tr
from xfeed.media_repository import MediaRepository
from xfeed.repositories import PostRepository
from xfeed.ui.collection_coordinator import MediaResolutionRequestOutcome
from xfeed.ui.feed_html import FeedHtmlBuilder, media_for_posts
from xfeed.ui.workers import Worker


SaveExecutor = Callable[[str, Callable[[SaveResult], None], Callable[[str], None]], None]
MediaResolutionRequester = Callable[
    [SavedPost, QWidget | None],
    bool | MediaResolutionRequestOutcome,
]


class _FeedRenderer(Protocol):
    def setHtml(self, html: str, base_url: QUrl) -> None: ...


class ManualSavesPanel(QWidget):
    close_requested = Signal()
    post_saved = Signal(object)

    def __init__(
        self,
        posts: PostRepository,
        post_service: PostService,
        *,
        web_factory: Callable[[], QWidget] | None = None,
        save_executor: SaveExecutor | None = None,
        media_resolution_requester: MediaResolutionRequester | None = None,
        media_repository: MediaRepository | None = None,
        asset_changed: SignalInstance | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("manualSavesPanel")
        self.setMinimumWidth(340)
        self.setMaximumWidth(430)
        self._posts = posts
        self._post_service = post_service
        self._builder = FeedHtmlBuilder()
        self._workers: set[Worker] = set()
        self._save_executor = save_executor or self._execute_save
        self._media_resolution_requester = media_resolution_requester
        self._media_repository = media_repository
        self._focus_post_id: int | None = None

        title = QLabel(tr("Manual Saves"))
        title.setStyleSheet("font-size: 19px; font-weight: 700; color: #151A27")
        self.close_button = QPushButton(tr("Close"))
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.close_button)

        self.url_field = QLineEdit()
        self.url_field.setPlaceholderText(tr("Paste an X post URL"))
        self.save_button = QPushButton(tr("Save"))
        self.save_button.setProperty("primary", True)
        save_row = QHBoxLayout()
        save_row.addWidget(self.url_field, 1)
        save_row.addWidget(self.save_button)
        self.status_label = QLabel()
        self.status_label.setObjectName("mutedLabel")
        self.status_label.setWordWrap(True)

        web_widget = QWebEngineView() if web_factory is None else web_factory()
        self.web_view = cast(_FeedRenderer, web_widget)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 0, 0)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addLayout(save_row)
        layout.addWidget(self.status_label)
        layout.addWidget(web_widget, 1)

        self.close_button.clicked.connect(self.close_requested)
        self.save_button.clicked.connect(self.save_post)
        self.url_field.returnPressed.connect(self.save_post)
        if asset_changed is not None:
            asset_changed.connect(self._asset_changed)

    def open_panel(self) -> None:
        self.reload()
        self.show()

    def reload(self, focus_post_id: int | None = None) -> None:
        self._focus_post_id = focus_post_id
        manual_posts = self._posts.list_manual()
        media_by_post = (
            media_for_posts(self._media_repository, manual_posts)
            if self._media_repository is not None
            else {}
        )
        rendered = self._builder.render_sections(
            (),
            focus_post_id=focus_post_id,
            media_by_post=media_by_post,
            empty_title=tr("No manual saves yet"),
            empty_body=tr("Paste a post URL above to keep it in this separate collection."),
        )
        if manual_posts:
            rendered = self._builder.render(
                manual_posts,
                focus_post_id,
                media_by_post=media_by_post,
            )
        self.web_view.setHtml(rendered, QUrl("https://x.com"))

    @Slot(int)
    def _asset_changed(self, _asset_id: int) -> None:
        self.reload(self._focus_post_id)

    def save_post(self) -> None:
        if not self.save_button.isEnabled():
            return
        url = self.url_field.text().strip()
        self.save_button.setEnabled(False)
        self.status_label.setText(tr("Saving…"))
        try:
            self._save_executor(url, self._save_finished, self._save_failed)
        except Exception as error:
            self._save_failed(str(error))

    def _execute_save(
        self,
        url: str,
        on_result: Callable[[SaveResult], None],
        on_error: Callable[[str], None],
    ) -> None:
        worker = Worker(lambda: self._post_service.quick_save(url))
        self._workers.add(worker)

        def completed(result: object) -> None:
            self._workers.discard(worker)
            on_result(cast(SaveResult, result))

        def failed(message: str) -> None:
            self._workers.discard(worker)
            on_error(message)

        worker.result.connect(completed)
        worker.failed.connect(failed)
        QThreadPool.globalInstance().start(worker)

    def _save_finished(self, result: SaveResult) -> None:
        self.save_button.setEnabled(True)
        self.status_label.setText(result.message)
        if result.status not in (SaveStatus.SAVED, SaveStatus.DUPLICATE):
            return
        if result.post is None:
            self._save_failed(tr("The provider did not return a saved post"))
            return
        self.post_saved.emit(result)
        requester = self._media_resolution_requester
        if requester is not None:
            try:
                outcome = requester(result.post, self)
            except Exception:
                outcome = None
            if outcome is True or outcome is MediaResolutionRequestOutcome.QUEUED:
                self.status_label.setText(tr("Saved · photo lookup queued"))
            elif outcome is False or outcome is MediaResolutionRequestOutcome.RESOLVED:
                self.status_label.setText(tr("Already saved · photos already resolved"))
            elif outcome is MediaResolutionRequestOutcome.FAILED:
                self.status_label.setText(tr("Already saved · photo lookup failed"))
        self.url_field.clear()
        self.reload(result.post.id)

    def _save_failed(self, message: str) -> None:
        self.save_button.setEnabled(True)
        detail = message or tr("Unknown error")
        self.status_label.setText(
            tr("Could not save the post: {detail}. Please try again.").format(detail=detail)
        )
