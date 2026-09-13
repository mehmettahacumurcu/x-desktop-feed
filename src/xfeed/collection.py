from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
import logging
from typing import Protocol

from xfeed.domain import (
    CollectionRun,
    CollectionStatus,
    CollectionTargetKind,
    ObservationKind,
)
from xfeed.domain import SavedPost
from xfeed.media import MediaAsset, PhotoCandidate
from xfeed.posts import PostService, SaveStatus
from xfeed.repositories import CollectionRepository
from xfeed.urls import InvalidPostUrl, canonicalize_post_url


logger = logging.getLogger(__name__)

HOME_FEED_KINDS = frozenset({CollectionTargetKind.FOR_YOU, CollectionTargetKind.FOLLOWING})


@dataclass(frozen=True)
class CandidateObservation:
    url: str
    post_id: str
    author_handle: str
    is_pinned: bool = False
    is_reply: bool = False
    is_repost: bool = False
    is_quote: bool = False
    is_promoted: bool = False
    parent_url: str | None = None
    discovery_order: int = 0
    photos: tuple[PhotoCandidate, ...] = ()


class DiscoveryReason(StrEnum):
    LIMIT = "limit"
    EXHAUSTED = "exhausted"
    NO_PROGRESS = "no_progress"
    LOGIN_WALL = "login_wall"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: tuple[CandidateObservation, ...]
    reason: DiscoveryReason
    diagnostic: str | None = None


@dataclass(frozen=True)
class CollectionProgress:
    found: int
    processed: int
    saved: int
    duplicates: int
    failed: int


@dataclass(frozen=True)
class CollectionResult:
    run: CollectionRun
    progress: CollectionProgress


class MediaManifestSink(Protocol):
    def record_collection_manifest(
        self, post: SavedPost, photos: Sequence[PhotoCandidate]
    ) -> tuple[MediaAsset, ...]: ...


def qualifying_candidates(
    observations: Iterable[CandidateObservation],
    selected_handle: str,
    maximum: int = 30,
) -> tuple[CandidateObservation, ...]:
    if not 1 <= maximum <= 30:
        raise ValueError("maximum must be between 1 and 30")

    selected_author = selected_handle.casefold()
    seen_post_ids: set[str] = set()
    candidates: list[CandidateObservation] = []
    for observation in observations:
        if (
            observation.is_pinned
            or observation.is_reply
            or observation.is_repost
            or observation.is_quote
            or observation.is_promoted
        ):
            continue
        try:
            canonical = canonicalize_post_url(observation.url)
        except InvalidPostUrl:
            continue
        if (
            canonical.handle != selected_author
            or observation.author_handle.casefold() != selected_author
            or canonical.post_id != observation.post_id
            or observation.post_id in seen_post_ids
        ):
            continue
        seen_post_ids.add(observation.post_id)
        candidates.append(observation)

    candidates.sort(key=lambda candidate: candidate.discovery_order)
    return tuple(candidates[:maximum])


def observation_kind(candidate: CandidateObservation) -> ObservationKind:
    if candidate.is_repost:
        return ObservationKind.REPOST
    if candidate.is_reply:
        return ObservationKind.REPLY
    if candidate.is_quote:
        return ObservationKind.QUOTE
    return ObservationKind.POST


def qualifying_for_you_candidates(
    observations: Iterable[CandidateObservation], maximum: int = 30
) -> tuple[CandidateObservation, ...]:
    return tuple(qualifying_home_candidates(tuple(observations), maximum))


def qualifying_home_candidates(
    observations: Sequence[CandidateObservation], maximum: int = 30
) -> list[CandidateObservation]:
    if not 1 <= maximum <= 50:
        raise ValueError("maximum must be between 1 and 50")

    accepted: list[CandidateObservation] = []
    seen_post_ids: set[str] = set()
    for observation in observations:
        if observation.is_pinned or observation.is_promoted:
            continue
        try:
            canonical = canonicalize_post_url(observation.url)
        except InvalidPostUrl:
            continue
        if (
            canonical.handle != observation.author_handle.casefold()
            or canonical.post_id != observation.post_id
            or observation.post_id in seen_post_ids
        ):
            continue
        seen_post_ids.add(observation.post_id)
        accepted.append(observation)

    accepted.sort(key=lambda value: value.discovery_order)
    return accepted[:maximum]


class CollectionService:
    def __init__(
        self,
        posts: PostService,
        collections: CollectionRepository,
        media: MediaManifestSink | None = None,
    ) -> None:
        self.posts = posts
        self.collections = collections
        self.media = media

    def process(
        self,
        run: CollectionRun,
        discovery: DiscoveryResult,
        on_progress: Callable[[CollectionProgress], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> CollectionResult:
        candidates = discovery.candidates[: run.requested_max]
        found = len(candidates)
        processed = 0
        saved = 0
        duplicates = 0
        failed = 0
        reason = discovery.reason

        try:
            for candidate in candidates:
                if cancelled is not None and cancelled():
                    reason = DiscoveryReason.CANCELLED
                    break
                processed += 1
                try:
                    save_result = self.posts.save(candidate.url)
                except Exception:
                    failed += 1
                    raise
                if save_result.status is SaveStatus.SAVED and save_result.post is not None:
                    saved += 1
                elif save_result.status is SaveStatus.DUPLICATE and save_result.post is not None:
                    duplicates += 1
                else:
                    failed += 1
                    progress = CollectionProgress(found, processed, saved, duplicates, failed)
                    if on_progress is not None:
                        on_progress(progress)
                    continue

                parent_post_id = None
                parent_url = None
                if cancelled is not None and cancelled():
                    reason = DiscoveryReason.CANCELLED
                else:
                    parent_url = self._reply_parent_url(run, candidate)
                if reason is not DiscoveryReason.CANCELLED and parent_url is not None:
                    try:
                        parent_result = self.posts.save(parent_url)
                    except Exception:
                        parent_result = None
                    if parent_result is not None and parent_result.status in (
                        SaveStatus.SAVED,
                        SaveStatus.DUPLICATE,
                    ):
                        parent_post_id = (
                            parent_result.post.id if parent_result.post is not None else None
                        )
                self.collections.observe(
                    run.id,
                    save_result.post.id,
                    discovery_order=candidate.discovery_order,
                    observation_kind=observation_kind(candidate),
                    parent_post_id=parent_post_id,
                )
                if self.media is not None:
                    try:
                        self.media.record_collection_manifest(save_result.post, candidate.photos)
                    except Exception:
                        logger.exception(
                            "Failed to record media manifest for post %s", save_result.post.id
                        )

                progress = CollectionProgress(found, processed, saved, duplicates, failed)
                if on_progress is not None:
                    on_progress(progress)
                if reason is DiscoveryReason.CANCELLED:
                    break
        except Exception:
            progress = CollectionProgress(found, processed, saved, duplicates, failed)
            self.collections.finish(
                run.id,
                status=(CollectionStatus.PARTIAL if processed else CollectionStatus.FAILED),
                candidate_count=found,
                saved_count=saved,
                duplicate_count=duplicates,
                failed_count=failed,
                reason=DiscoveryReason.ERROR.value,
                diagnostic="Collection processing failed",
            )
            raise

        progress = CollectionProgress(found, processed, saved, duplicates, failed)
        status = self._status(reason, found)
        finished = self.collections.finish(
            run.id,
            status=status,
            candidate_count=found,
            saved_count=saved,
            duplicate_count=duplicates,
            failed_count=failed,
            reason=reason.value,
            diagnostic=discovery.diagnostic,
        )
        return CollectionResult(finished, progress)

    @staticmethod
    def _reply_parent_url(run: CollectionRun, candidate: CandidateObservation) -> str | None:
        if (
            run.target_kind not in HOME_FEED_KINDS
            or not candidate.is_reply
            or candidate.parent_url is None
        ):
            return None
        try:
            top_level = canonicalize_post_url(candidate.url)
            parent = canonicalize_post_url(candidate.parent_url)
        except InvalidPostUrl:
            return None
        if candidate.parent_url != parent.url or parent.post_id == top_level.post_id:
            return None
        return parent.url

    @staticmethod
    def _status(reason: DiscoveryReason, found: int) -> CollectionStatus:
        if reason is DiscoveryReason.LIMIT:
            return CollectionStatus.COMPLETED
        if reason is DiscoveryReason.CANCELLED:
            return CollectionStatus.CANCELLED
        if reason in (DiscoveryReason.EXHAUSTED, DiscoveryReason.NO_PROGRESS):
            return CollectionStatus.PARTIAL
        if found:
            return CollectionStatus.PARTIAL
        return CollectionStatus.FAILED
