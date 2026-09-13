from dataclasses import dataclass
from enum import StrEnum

from xfeed.sources import canonicalize_profile


class Availability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    OFFLINE = "offline"


class LabelKind(StrEnum):
    USEFUL = "useful"
    NEUTRAL = "neutral"
    UNWANTED = "unwanted"


class LabelOrigin(StrEnum):
    USER = "user"
    IMPORTED = "imported"


class SortOrder(StrEnum):
    NEWEST = "newest"
    OLDEST = "oldest"
    UNWANTED_DESC = "unwanted_desc"
    AUTHOR = "author"


class CollectionStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AppSessionState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    INTERRUPTED = "interrupted"


class CollectionTargetKind(StrEnum):
    SOURCE = "source"
    FOR_YOU = "for_you"
    FOLLOWING = "following"


class ObservationKind(StrEnum):
    POST = "post"
    REPOST = "repost"
    REPLY = "reply"
    QUOTE = "quote"


@dataclass(frozen=True)
class CollectionTarget:
    kind: CollectionTargetKind
    source_id: int | None
    handle: str | None
    profile_url: str

    @classmethod
    def for_source(cls, source_id: int, handle: str, profile_url: str) -> "CollectionTarget":
        if type(source_id) is not int or source_id < 1:
            raise ValueError("source_id must be a positive integer")
        canonical = canonicalize_profile(handle)
        if profile_url != canonical.profile_url:
            raise ValueError("source profile URL is not canonical")
        return cls(CollectionTargetKind.SOURCE, source_id, canonical.handle, profile_url)

    @classmethod
    def for_you(cls) -> "CollectionTarget":
        return cls(CollectionTargetKind.FOR_YOU, None, None, "https://x.com/home")

    @classmethod
    def following(cls) -> "CollectionTarget":
        return cls(CollectionTargetKind.FOLLOWING, None, None, "https://x.com/home")

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CollectionTargetKind):
            raise ValueError("collection target kind is invalid")
        if self.kind is CollectionTargetKind.SOURCE:
            if type(self.handle) is not str:
                raise ValueError("source target shape is invalid")
            try:
                canonical = canonicalize_profile(self.handle)
            except ValueError as error:
                raise ValueError("source target shape is invalid") from error
            if (
                type(self.source_id) is not int
                or self.source_id < 1
                or self.handle != canonical.handle
                or self.profile_url != canonical.profile_url
            ):
                raise ValueError("source target shape is invalid")
        elif (
            self.source_id is not None
            or self.handle is not None
            or self.profile_url != "https://x.com/home"
        ):
            raise ValueError("For You target shape is invalid")


@dataclass(frozen=True)
class AppSession:
    id: int
    opened_at: str
    closed_at: str | None
    state: AppSessionState
    app_version: str
    synthetic: bool = False


@dataclass(frozen=True)
class AuthorCount:
    handle: str
    count: int


@dataclass(frozen=True)
class CollectionRun:
    id: int
    source_id: int | None
    started_at: str
    finished_at: str | None
    requested_max: int
    candidate_count: int
    saved_count: int
    duplicate_count: int
    failed_count: int
    status: CollectionStatus
    reason: str | None
    diagnostic: str | None
    target_kind: CollectionTargetKind = CollectionTargetKind.SOURCE
    session_id: int | None = None


@dataclass(frozen=True)
class PostDraft:
    canonical_url: str
    x_post_id: str
    author_handle: str | None
    author_name: str | None
    text: str
    published_at: str | None
    embed_html: str
    provider_json: str
    source_method: str
    has_media: bool = False
    availability: Availability = Availability.AVAILABLE


@dataclass(frozen=True)
class SavedPost(PostDraft):
    id: int = 0


@dataclass(frozen=True)
class ObservedPost:
    post: SavedPost
    discovery_order: int
    observation_kind: ObservationKind
    parent_post_id: int | None


@dataclass(frozen=True)
class LabelEvent:
    id: int
    post_id: int
    label: LabelKind
    origin: LabelOrigin
    reason: str | None
    created_at: str
