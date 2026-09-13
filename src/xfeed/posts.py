from dataclasses import dataclass
from enum import StrEnum
import sqlite3

from xfeed.domain import SavedPost
from xfeed.i18n import tr
from xfeed.oembed import PostMetadataProvider, ProviderUnavailable
from xfeed.repositories import PostRepository
from xfeed.urls import InvalidPostUrl, canonicalize_post_url


class SaveStatus(StrEnum):
    SAVED = "saved"
    DUPLICATE = "duplicate"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SaveResult:
    status: SaveStatus
    post: SavedPost | None = None
    message: str = ""


class RefreshStatus(StrEnum):
    REFRESHED = "refreshed"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RefreshResult:
    status: RefreshStatus
    post: SavedPost | None = None
    message: str = ""


class PostService:
    def __init__(
        self,
        posts: PostRepository,
        provider: PostMetadataProvider,
    ) -> None:
        self.posts = posts
        self.provider = provider

    def save(self, raw_url: str) -> SaveResult:
        return self._save(raw_url, manual_ingestion=False)

    def quick_save(self, raw_url: str) -> SaveResult:
        return self._save(raw_url, manual_ingestion=True)

    def _save(self, raw_url: str, *, manual_ingestion: bool) -> SaveResult:
        try:
            canonical = canonicalize_post_url(raw_url)
        except InvalidPostUrl as error:
            return SaveResult(SaveStatus.INVALID, message=str(error))
        existing = self.posts.by_url(canonical.url)
        if existing is not None:
            if manual_ingestion:
                self.posts.record_manual_ingestion(existing.id)
            return SaveResult(SaveStatus.DUPLICATE, existing, tr("Already saved"))
        try:
            draft = self.provider.fetch(canonical)
        except ProviderUnavailable as error:
            return SaveResult(SaveStatus.UNAVAILABLE, message=str(error))
        try:
            saved = self.posts.insert(draft, manual_ingestion=manual_ingestion)
        except sqlite3.IntegrityError as error:
            if getattr(error, "sqlite_errorcode", None) != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                raise
            existing = self.posts.by_url(canonical.url)
            if existing is None:
                raise
            if manual_ingestion:
                self.posts.record_manual_ingestion(existing.id)
            return SaveResult(SaveStatus.DUPLICATE, existing, tr("Already saved"))
        return SaveResult(SaveStatus.SAVED, saved, tr("Saved"))

    def refresh(self, post_id: int) -> RefreshResult:
        existing = self.posts.by_id(post_id)
        if existing is None:
            return RefreshResult(RefreshStatus.NOT_FOUND, message="Post not found")
        try:
            draft = self.provider.fetch(canonicalize_post_url(existing.canonical_url))
        except ProviderUnavailable as error:
            return RefreshResult(RefreshStatus.UNAVAILABLE, existing, str(error))
        refreshed = self.posts.update_provider(post_id, draft)
        return RefreshResult(RefreshStatus.REFRESHED, refreshed, "Refreshed")
