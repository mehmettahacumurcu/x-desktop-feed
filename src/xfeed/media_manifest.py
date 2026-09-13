from collections.abc import Sequence

from PySide6.QtCore import QObject, Signal

from xfeed.domain import SavedPost
from xfeed.media import MediaAsset, MediaOrigin, PhotoCandidate
from xfeed.media_repository import MediaRepository


class MediaManifestService(QObject):
    manifest_recorded = Signal(object)
    manifest_failed = Signal(int, str)

    def __init__(self, repository: MediaRepository, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.repository = repository

    def record_collection_manifest(
        self, post: SavedPost, photos: Sequence[PhotoCandidate]
    ) -> tuple[MediaAsset, ...]:
        return self._record(post, MediaOrigin.COLLECTION, photos)

    def record_manual_manifest(
        self, post: SavedPost, photos: Sequence[PhotoCandidate]
    ) -> tuple[MediaAsset, ...]:
        return self._record(post, MediaOrigin.MANUAL, photos)

    def _record(
        self,
        post: SavedPost,
        origin: MediaOrigin,
        photos: Sequence[PhotoCandidate],
    ) -> tuple[MediaAsset, ...]:
        try:
            assets = self.repository.record_manifest(
                post.id,
                post.canonical_url,
                origin,
                photos,
            )
        except Exception as error:
            self.manifest_failed.emit(post.id, str(error))
            raise
        self.manifest_recorded.emit(assets)
        return assets
