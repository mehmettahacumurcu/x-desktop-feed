from dataclasses import replace

import pytest

from xfeed.collection import (
    CandidateObservation,
    CollectionProgress,
    CollectionService,
    DiscoveryReason,
    DiscoveryResult,
    observation_kind,
    qualifying_candidates,
    qualifying_home_candidates,
    qualifying_for_you_candidates,
)
from xfeed.domain import (
    Availability,
    CollectionRun,
    CollectionStatus,
    CollectionTarget,
    CollectionTargetKind,
    ObservationKind,
    SavedPost,
)
from xfeed.media import PhotoCandidate
from xfeed.posts import SaveResult, SaveStatus


def candidate(
    post_id: str = "123",
    *,
    handle: str = "example",
    order: int = 0,
    is_pinned: bool = False,
    is_reply: bool = False,
    is_repost: bool = False,
    is_quote: bool = False,
    is_promoted: bool = False,
    parent_url: str | None = None,
    photos: tuple[PhotoCandidate, ...] = (),
) -> CandidateObservation:
    return CandidateObservation(
        url=f"https://x.com/{handle}/status/{post_id}",
        post_id=post_id,
        author_handle=handle,
        is_pinned=is_pinned,
        is_reply=is_reply,
        is_repost=is_repost,
        is_quote=is_quote,
        is_promoted=is_promoted,
        parent_url=parent_url,
        discovery_order=order,
        photos=photos,
    )


def test_candidate_observation_defaults_to_an_empty_photo_manifest() -> None:
    observation = CandidateObservation(
        url="https://x.com/example/status/123",
        post_id="123",
        author_handle="example",
    )

    assert observation.photos == ()


def saved_post(post_id: int, x_post_id: str) -> SavedPost:
    return SavedPost(
        canonical_url=f"https://x.com/example/status/{x_post_id}",
        x_post_id=x_post_id,
        author_handle="example",
        author_name="Example",
        text=f"Post {x_post_id}",
        published_at=None,
        embed_html="<blockquote></blockquote>",
        provider_json="{}",
        source_method="oembed",
        availability=Availability.AVAILABLE,
        id=post_id,
    )


def running_run() -> CollectionRun:
    return CollectionRun(
        id=11,
        source_id=7,
        started_at="2026-07-17 10:00:00",
        finished_at=None,
        requested_max=30,
        candidate_count=0,
        saved_count=0,
        duplicate_count=0,
        failed_count=0,
        status=CollectionStatus.RUNNING,
        reason=None,
        diagnostic=None,
    )


def for_you_running_run() -> CollectionRun:
    return replace(
        running_run(),
        source_id=None,
        requested_max=50,
        target_kind=CollectionTargetKind.FOR_YOU,
    )


def following_running_run() -> CollectionRun:
    return replace(
        running_run(),
        source_id=None,
        requested_max=50,
        target_kind=CollectionTargetKind.FOLLOWING,
    )


def test_collection_targets_enforce_exact_source_and_for_you_shapes() -> None:
    source = CollectionTarget.for_source(7, "OpenAI", "https://x.com/openai")
    home = CollectionTarget.for_you()

    assert source == CollectionTarget(
        CollectionTargetKind.SOURCE, 7, "openai", "https://x.com/openai"
    )
    assert home == CollectionTarget(CollectionTargetKind.FOR_YOU, None, None, "https://x.com/home")
    with pytest.raises(ValueError):
        CollectionTarget(CollectionTargetKind.FOR_YOU, 7, None, "https://x.com/home")


@pytest.mark.parametrize("kind", ["for_you", "unknown", 1])
def test_collection_target_kind_must_be_a_collection_target_kind(kind: object) -> None:
    with pytest.raises(ValueError, match="kind"):
        CollectionTarget(kind, None, None, "https://x.com/home")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("handle", "profile_url"),
    [
        ("OpenAI", "https://x.com/openai"),
        ("bad/path", "https://x.com/bad/path"),
        (7, "https://x.com/7"),
        (None, "https://x.com/openai"),
        ("openai", "https://www.x.com/openai"),
        ("openai", "https://x.com/openai/"),
        ("@openai", "https://x.com/openai"),
    ],
)
def test_direct_source_target_requires_normalized_canonical_fields(
    handle: object, profile_url: str
) -> None:
    with pytest.raises(ValueError, match="source target"):
        CollectionTarget(
            CollectionTargetKind.SOURCE,
            7,
            handle,  # type: ignore[arg-type]
            profile_url,
        )


@pytest.mark.parametrize(
    ("source_id", "handle", "profile_url"),
    [
        (True, None, "https://x.com/home"),
        (None, "", "https://x.com/home"),
        (None, None, "https://x.com/Home"),
        (None, None, "https://x.com/home/"),
    ],
)
def test_direct_for_you_target_requires_exact_home_shape(
    source_id: object, handle: str | None, profile_url: str
) -> None:
    with pytest.raises(ValueError, match="For You target"):
        CollectionTarget(
            CollectionTargetKind.FOR_YOU,
            source_id,  # type: ignore[arg-type]
            handle,
            profile_url,
        )


class FakePostService:
    def __init__(self, results: list[SaveResult | Exception]) -> None:
        self.results = iter(results)
        self.calls: list[str] = []

    def save(self, url: str) -> SaveResult:
        self.calls.append(url)
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


class FakeCollectionRepository:
    def __init__(self) -> None:
        self.observations: list[tuple[int, int, int, ObservationKind, int | None]] = []
        self.finish_calls: list[dict[str, object]] = []
        self.observe_error: Exception | None = None

    def observe(
        self,
        run_id: int,
        post_id: int,
        *,
        discovery_order: int = 0,
        observation_kind: ObservationKind = ObservationKind.POST,
        parent_post_id: int | None = None,
    ) -> None:
        self.observations.append(
            (run_id, post_id, discovery_order, observation_kind, parent_post_id)
        )
        if self.observe_error is not None:
            raise self.observe_error

    def finish(
        self,
        run_id: int,
        *,
        status: CollectionStatus,
        candidate_count: int,
        saved_count: int,
        duplicate_count: int,
        failed_count: int,
        reason: str | None,
        diagnostic: str | None,
    ) -> CollectionRun:
        values: dict[str, object] = {
            "run_id": run_id,
            "status": status,
            "candidate_count": candidate_count,
            "saved_count": saved_count,
            "duplicate_count": duplicate_count,
            "failed_count": failed_count,
            "reason": reason,
            "diagnostic": diagnostic,
        }
        self.finish_calls.append(values)
        return replace(
            running_run(),
            finished_at="2026-07-17 10:01:00",
            status=status,
            candidate_count=candidate_count,
            saved_count=saved_count,
            duplicate_count=duplicate_count,
            failed_count=failed_count,
            reason=reason,
            diagnostic=diagnostic,
        )


class RecordingMediaManifestSink:
    def __init__(self) -> None:
        self.calls: list[tuple[SavedPost, tuple[PhotoCandidate, ...]]] = []

    def record_collection_manifest(
        self, post: SavedPost, photos: tuple[PhotoCandidate, ...]
    ) -> tuple[object, ...]:
        self.calls.append((post, photos))
        return ()


class FailingMediaManifestSink:
    def __init__(self) -> None:
        self.calls: list[tuple[SavedPost, tuple[PhotoCandidate, ...]]] = []

    def record_collection_manifest(
        self, post: SavedPost, photos: tuple[PhotoCandidate, ...]
    ) -> tuple[object, ...]:
        self.calls.append((post, photos))
        raise RuntimeError("media database unavailable")


def collection_service(
    results: list[SaveResult | Exception],
    media: RecordingMediaManifestSink | FailingMediaManifestSink | None = None,
) -> tuple[CollectionService, FakePostService, FakeCollectionRepository]:
    posts = FakePostService(results)
    collections = FakeCollectionRepository()
    if media is None:
        return CollectionService(posts, collections), posts, collections
    return CollectionService(posts, collections, media), posts, collections


def test_qualifying_candidates_compare_author_case_insensitively() -> None:
    observation = replace(
        candidate(handle="Example"),
        url="https://twitter.com/eXaMpLe/status/123?ref_src=twsrc%5Etfw",
    )

    assert qualifying_candidates([observation], "EXAMPLE") == (observation,)


@pytest.mark.parametrize("flag", ["is_pinned", "is_reply", "is_repost", "is_quote", "is_promoted"])
def test_qualifying_candidates_reject_each_excluded_flag(flag: str) -> None:
    observation = candidate(**{flag: True})

    assert qualifying_candidates([observation], "example") == ()


def test_qualifying_for_you_candidates_accepts_mixed_authors_and_observation_kinds() -> None:
    qualified = qualifying_for_you_candidates(
        (
            candidate("2", handle="beta", order=1, is_repost=True),
            candidate("1", handle="alpha", order=0),
            candidate("3", handle="gamma", order=2, is_reply=True),
            candidate("4", handle="delta", order=3, is_quote=True),
        ),
        maximum=50,
    )

    assert [value.post_id for value in qualified] == ["1", "2", "3", "4"]
    assert [observation_kind(value) for value in qualified] == [
        ObservationKind.POST,
        ObservationKind.REPOST,
        ObservationKind.REPLY,
        ObservationKind.QUOTE,
    ]


@pytest.mark.parametrize("kind", [CollectionTargetKind.FOR_YOU, CollectionTargetKind.FOLLOWING])
def test_home_feed_accepts_ordered_content_and_rejects_promoted_or_pinned(
    kind: CollectionTargetKind,
) -> None:
    candidates = (
        candidate("0", order=0),
        candidate("1", order=1, is_repost=True),
        candidate("2", order=2, is_reply=True),
        candidate("3", order=3, is_quote=True),
        candidate("4", order=4, is_promoted=True),
        candidate("5", order=5, is_pinned=True),
    )

    qualified = qualifying_home_candidates(candidates, maximum=50)

    assert kind in {CollectionTargetKind.FOR_YOU, CollectionTargetKind.FOLLOWING}
    assert [item.discovery_order for item in qualified] == [0, 1, 2, 3]


def test_following_collection_saves_reply_parent() -> None:
    saved = saved_post(101, "1")
    parent = saved_post(102, "2")
    service, posts, collections = collection_service(
        [SaveResult(SaveStatus.SAVED, saved), SaveResult(SaveStatus.DUPLICATE, parent)]
    )

    service.process(
        following_running_run(),
        DiscoveryResult(
            (candidate("1", is_reply=True, parent_url="https://x.com/example/status/2"),),
            DiscoveryReason.LIMIT,
        ),
    )

    assert posts.calls == ["https://x.com/example/status/1", "https://x.com/example/status/2"]
    assert collections.observations == [(11, 101, 0, ObservationKind.REPLY, 102)]


def test_qualifying_for_you_candidates_rejects_invalid_or_excluded_observations() -> None:
    first_duplicate = candidate("1", handle="alpha", order=4)
    later_duplicate = candidate("1", handle="beta", order=0)
    mismatched_author = replace(candidate("2", handle="alpha"), url="https://x.com/beta/status/2")
    malformed = replace(candidate("3", handle="gamma"), url="not a post URL")

    qualified = qualifying_for_you_candidates(
        (
            candidate("0", is_pinned=True),
            candidate("9", is_promoted=True),
            mismatched_author,
            malformed,
            first_duplicate,
            later_duplicate,
        )
    )

    assert qualified == (first_duplicate,)


@pytest.mark.parametrize("maximum", [0, 51])
def test_qualifying_for_you_candidates_validate_maximum(maximum: int) -> None:
    with pytest.raises(ValueError, match="maximum"):
        qualifying_for_you_candidates([], maximum=maximum)


def test_qualifying_for_you_candidates_accepts_fifty_sorted_observations() -> None:
    observations = [candidate(str(index), order=50 - index) for index in range(1, 51)]

    qualified = qualifying_for_you_candidates(observations, maximum=50)

    assert len(qualified) == 50
    assert [value.discovery_order for value in qualified] == list(range(0, 50))


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({"is_repost": True, "is_reply": True, "is_quote": True}, ObservationKind.REPOST),
        ({"is_reply": True, "is_quote": True}, ObservationKind.REPLY),
        ({"is_quote": True}, ObservationKind.QUOTE),
        ({}, ObservationKind.POST),
    ],
)
def test_observation_kind_uses_repost_reply_quote_post_precedence(
    flags: dict[str, bool], expected: ObservationKind
) -> None:
    assert observation_kind(candidate(**flags)) is expected


def test_qualifying_candidates_require_valid_matching_url_metadata() -> None:
    mismatched_author = replace(
        candidate("1"),
        url="https://x.com/other/status/1",
    )
    mismatched_id = replace(
        candidate("2"),
        url="https://x.com/example/status/999",
    )
    malformed = replace(candidate("3"), url="not a post URL")

    assert (
        qualifying_candidates(
            [mismatched_author, mismatched_id, malformed],
            "example",
        )
        == ()
    )


def test_qualifying_candidates_keep_first_duplicate_and_sort_by_discovery_order() -> None:
    first_duplicate = candidate("1", order=20)
    later_duplicate = replace(
        candidate("1", order=0),
        url="https://twitter.com/example/status/1",
    )
    earliest_unique = candidate("2", order=10)

    result = qualifying_candidates(
        [first_duplicate, later_duplicate, earliest_unique],
        "example",
    )

    assert result == (earliest_unique, first_duplicate)


def test_qualifying_candidates_cap_sorted_results_at_thirty() -> None:
    observations = [candidate(str(index), order=40 - index) for index in range(1, 41)]

    result = qualifying_candidates(observations, "example")

    assert len(result) == 30
    assert [item.discovery_order for item in result] == list(range(0, 30))


@pytest.mark.parametrize("maximum", [0, 31])
def test_qualifying_candidates_validate_maximum(maximum: int) -> None:
    with pytest.raises(ValueError, match="maximum"):
        qualifying_candidates([], "example", maximum=maximum)


def test_process_saves_sequentially_observes_posts_and_reports_exact_progress() -> None:
    saved = saved_post(101, "1")
    duplicate = saved_post(102, "2")
    service, posts, collections = collection_service(
        [
            SaveResult(SaveStatus.SAVED, saved, "Saved"),
            SaveResult(SaveStatus.DUPLICATE, duplicate, "Already saved"),
            SaveResult(SaveStatus.UNAVAILABLE, message="offline"),
        ]
    )
    candidates = (candidate("1"), candidate("2"), candidate("3"))
    progress_updates: list[CollectionProgress] = []

    result = service.process(
        running_run(),
        DiscoveryResult(candidates, DiscoveryReason.LIMIT),
        on_progress=progress_updates.append,
    )

    assert posts.calls == [item.url for item in candidates]
    assert collections.observations == [
        (11, 101, 0, ObservationKind.POST, None),
        (11, 102, 0, ObservationKind.POST, None),
    ]
    assert progress_updates == [
        CollectionProgress(found=3, processed=1, saved=1, duplicates=0, failed=0),
        CollectionProgress(found=3, processed=2, saved=1, duplicates=1, failed=0),
        CollectionProgress(found=3, processed=3, saved=1, duplicates=1, failed=1),
    ]
    assert result.progress == progress_updates[-1]
    assert result.run.status is CollectionStatus.COMPLETED
    assert result.run.reason == DiscoveryReason.LIMIT.value
    assert collections.finish_calls == [
        {
            "run_id": 11,
            "status": CollectionStatus.COMPLETED,
            "candidate_count": 3,
            "saved_count": 1,
            "duplicate_count": 1,
            "failed_count": 1,
            "reason": "limit",
            "diagnostic": None,
        }
    ]


def test_process_records_photo_manifests_for_saved_duplicate_and_empty_observations() -> None:
    saved = saved_post(101, "1")
    duplicate = saved_post(102, "2")
    media = RecordingMediaManifestSink()
    service, _, collections = collection_service(
        [
            SaveResult(SaveStatus.SAVED, saved),
            SaveResult(SaveStatus.DUPLICATE, duplicate),
        ],
        media,
    )
    photos = (PhotoCandidate(0, "https://pbs.twimg.com/media/one?format=jpg&name=small", None),)

    result = service.process(
        running_run(),
        DiscoveryResult((candidate("1", photos=photos), candidate("2")), DiscoveryReason.LIMIT),
    )

    assert collections.observations == [
        (11, 101, 0, ObservationKind.POST, None),
        (11, 102, 0, ObservationKind.POST, None),
    ]
    assert media.calls == [(saved, photos), (duplicate, ())]
    assert result.progress == CollectionProgress(2, 2, 1, 1, 0)


def test_process_logs_media_manifest_failure_without_changing_collection_outcomes(caplog) -> None:
    saved = saved_post(101, "1")
    media = FailingMediaManifestSink()
    service, _, collections = collection_service([SaveResult(SaveStatus.SAVED, saved)], media)

    result = service.process(
        running_run(), DiscoveryResult((candidate("1"),), DiscoveryReason.LIMIT)
    )

    assert media.calls == [(saved, ())]
    assert collections.observations == [(11, 101, 0, ObservationKind.POST, None)]
    assert result.progress == CollectionProgress(1, 1, 1, 0, 0)
    assert result.run.status is CollectionStatus.COMPLETED
    assert "Failed to record media manifest for post 101" in caplog.messages


def test_process_does_not_record_manifests_for_candidates_cancelled_before_save() -> None:
    saved = saved_post(101, "1")
    media = RecordingMediaManifestSink()
    service, posts, _ = collection_service([SaveResult(SaveStatus.SAVED, saved)], media)

    result = service.process(
        running_run(),
        DiscoveryResult((candidate("1"), candidate("2")), DiscoveryReason.LIMIT),
        cancelled=lambda: len(posts.calls) == 1,
    )

    assert media.calls == [(saved, ())]
    assert result.progress == CollectionProgress(2, 1, 1, 0, 0)
    assert result.run.status is CollectionStatus.CANCELLED


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (DiscoveryReason.EXHAUSTED, CollectionStatus.PARTIAL),
        (DiscoveryReason.NO_PROGRESS, CollectionStatus.PARTIAL),
        (DiscoveryReason.LOGIN_WALL, CollectionStatus.PARTIAL),
        (DiscoveryReason.TIMEOUT, CollectionStatus.PARTIAL),
        (DiscoveryReason.ERROR, CollectionStatus.PARTIAL),
    ],
)
def test_process_retains_candidate_successes_for_incomplete_discovery(
    reason: DiscoveryReason,
    expected: CollectionStatus,
) -> None:
    saved = saved_post(101, "1")
    service, _, collections = collection_service([SaveResult(SaveStatus.SAVED, saved)])

    result = service.process(
        running_run(),
        DiscoveryResult((candidate("1"),), reason, diagnostic="stopped early"),
    )

    assert result.run.status is expected
    assert result.run.diagnostic == "stopped early"
    assert result.progress == CollectionProgress(1, 1, 1, 0, 0)
    assert collections.observations == [(11, 101, 0, ObservationKind.POST, None)]
    assert len(collections.finish_calls) == 1


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (DiscoveryReason.EXHAUSTED, CollectionStatus.PARTIAL),
        (DiscoveryReason.NO_PROGRESS, CollectionStatus.PARTIAL),
        (DiscoveryReason.LOGIN_WALL, CollectionStatus.FAILED),
        (DiscoveryReason.TIMEOUT, CollectionStatus.FAILED),
        (DiscoveryReason.ERROR, CollectionStatus.FAILED),
    ],
)
def test_process_maps_zero_candidate_terminal_reason(
    reason: DiscoveryReason,
    expected: CollectionStatus,
) -> None:
    service, posts, collections = collection_service([])

    result = service.process(running_run(), DiscoveryResult((), reason))

    assert result.run.status is expected
    assert result.progress == CollectionProgress(0, 0, 0, 0, 0)
    assert posts.calls == []
    assert len(collections.finish_calls) == 1


def test_excluded_only_profile_finishes_partial_without_save_failures() -> None:
    service, posts, _ = collection_service([])
    excluded = [
        candidate("1", is_pinned=True),
        candidate("2", is_reply=True),
        candidate("3", is_repost=True),
        candidate("4", is_quote=True),
    ]
    discovery = DiscoveryResult(
        qualifying_candidates(excluded, "example"),
        DiscoveryReason.EXHAUSTED,
    )

    result = service.process(running_run(), discovery)

    assert result.run.status is CollectionStatus.PARTIAL
    assert result.progress == CollectionProgress(0, 0, 0, 0, 0)
    assert posts.calls == []


def test_process_maps_discovery_cancellation_without_saving() -> None:
    service, posts, collections = collection_service([])

    result = service.process(
        running_run(),
        DiscoveryResult((), DiscoveryReason.CANCELLED, "window closed"),
    )

    assert result.run.status is CollectionStatus.CANCELLED
    assert result.run.reason == DiscoveryReason.CANCELLED.value
    assert result.run.diagnostic == "window closed"
    assert posts.calls == []
    assert len(collections.finish_calls) == 1


def test_process_checks_cancellation_before_each_save_and_keeps_partial_success() -> None:
    saved = saved_post(101, "1")
    service, posts, collections = collection_service([SaveResult(SaveStatus.SAVED, saved)])
    progress_updates: list[CollectionProgress] = []

    def cancelled() -> bool:
        return len(posts.calls) == 1

    result = service.process(
        running_run(),
        DiscoveryResult((candidate("1"), candidate("2")), DiscoveryReason.LIMIT),
        on_progress=progress_updates.append,
        cancelled=cancelled,
    )

    assert posts.calls == [candidate("1").url]
    assert collections.observations == [(11, 101, 0, ObservationKind.POST, None)]
    assert progress_updates == [CollectionProgress(2, 1, 1, 0, 0)]
    assert result.run.status is CollectionStatus.CANCELLED
    assert result.run.reason == DiscoveryReason.CANCELLED.value
    assert result.progress == CollectionProgress(2, 1, 1, 0, 0)
    assert len(collections.finish_calls) == 1


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (replace(running_run(), requested_max=12), 12),
        (for_you_running_run(), 50),
    ],
)
def test_process_caps_candidates_at_requested_maximum(
    run: CollectionRun,
    expected: int,
) -> None:
    candidates = tuple(candidate(str(index), order=index) for index in range(1, 61))
    results = [SaveResult(SaveStatus.UNAVAILABLE, message="offline") for _ in range(expected)]
    service, posts, collections = collection_service(results)

    result = service.process(
        run,
        DiscoveryResult(candidates, DiscoveryReason.LIMIT),
    )

    assert posts.calls == [item.url for item in candidates[:expected]]
    assert result.progress == CollectionProgress(expected, expected, 0, 0, expected)
    assert result.run.candidate_count == expected
    assert len(collections.finish_calls) == 1


def test_process_for_you_observes_discovery_metadata_for_saved_and_duplicate_posts() -> None:
    saved = saved_post(101, "1")
    duplicate = saved_post(102, "2")
    service, _, collections = collection_service(
        [
            SaveResult(SaveStatus.SAVED, saved, "Saved"),
            SaveResult(SaveStatus.DUPLICATE, duplicate, "Already saved"),
        ]
    )
    candidates = (
        candidate("1", handle="alpha", order=3),
        candidate("2", handle="beta", order=7, is_repost=True),
    )

    result = service.process(
        for_you_running_run(), DiscoveryResult(candidates, DiscoveryReason.LIMIT)
    )

    assert collections.observations == [
        (11, 101, 3, ObservationKind.POST, None),
        (11, 102, 7, ObservationKind.REPOST, None),
    ]
    assert result.progress == CollectionProgress(2, 2, 1, 1, 0)


def test_process_for_you_saves_canonical_reply_parent_without_changing_top_level_counts() -> None:
    reply = candidate(
        "2",
        handle="beta",
        order=4,
        is_reply=True,
        parent_url="https://x.com/alpha/status/1",
    )
    top_level = saved_post(102, "2")
    parent = saved_post(101, "1")
    service, posts, collections = collection_service(
        [
            SaveResult(SaveStatus.SAVED, top_level, "Saved"),
            SaveResult(SaveStatus.DUPLICATE, parent, "Already saved"),
        ]
    )

    result = service.process(
        for_you_running_run(), DiscoveryResult((reply,), DiscoveryReason.LIMIT)
    )

    assert posts.calls == [reply.url, reply.parent_url]
    assert collections.observations == [(11, 102, 4, ObservationKind.REPLY, 101)]
    assert result.progress == CollectionProgress(1, 1, 1, 0, 0)
    assert result.run.saved_count == 1
    assert result.run.duplicate_count == 0


@pytest.mark.parametrize(
    ("run", "reply", "parent_url"),
    [
        (
            running_run(),
            True,
            "https://x.com/alpha/status/1",
        ),
        (
            for_you_running_run(),
            False,
            "https://x.com/alpha/status/1",
        ),
        (
            for_you_running_run(),
            True,
            "https://x.com/beta/status/2",
        ),
        (
            for_you_running_run(),
            True,
            "https://x.com/other/status/2",
        ),
        (
            for_you_running_run(),
            True,
            "not a canonical parent",
        ),
    ],
)
def test_process_ignores_inapplicable_or_ambiguous_parent_metadata(
    run: CollectionRun, reply: bool, parent_url: str
) -> None:
    top_level_candidate = candidate(
        "2",
        handle="beta",
        order=4,
        is_reply=reply,
        parent_url=parent_url,
    )
    top_level = saved_post(102, "2")
    service, posts, collections = collection_service(
        [SaveResult(SaveStatus.SAVED, top_level, "Saved")]
    )

    result = service.process(
        run,
        DiscoveryResult((top_level_candidate,), DiscoveryReason.LIMIT),
    )

    assert posts.calls == [top_level_candidate.url]
    assert collections.observations == [(11, 102, 4, observation_kind(top_level_candidate), None)]
    assert result.progress == CollectionProgress(1, 1, 1, 0, 0)


def test_protocol_observations_with_inapplicable_parents_do_not_enrich_during_processing() -> None:
    from xfeed.opera_protocol import parse_observations

    parsed = parse_observations(
        {
            "protocol_version": 4,
            "observations": [
                {
                    "url": "https://x.com/beta/status/2",
                    "post_id": "2",
                    "author_handle": "beta",
                    "is_pinned": False,
                    "is_reply": False,
                    "is_repost": False,
                    "is_quote": False,
                    "is_promoted": False,
                    "parent_url": "https://x.com/alpha/status/1",
                    "discovery_order": 0,
                    "photos": [],
                },
                {
                    "url": "https://x.com/gamma/status/3",
                    "post_id": "3",
                    "author_handle": "gamma",
                    "is_pinned": False,
                    "is_reply": True,
                    "is_repost": False,
                    "is_quote": False,
                    "is_promoted": False,
                    "parent_url": "https://x.com/gamma/status/3",
                    "discovery_order": 1,
                    "photos": [],
                },
            ],
        }
    )
    first = saved_post(102, "2")
    second = saved_post(103, "3")
    service, posts, collections = collection_service(
        [
            SaveResult(SaveStatus.SAVED, first, "Saved"),
            SaveResult(SaveStatus.SAVED, second, "Saved"),
        ]
    )

    result = service.process(
        for_you_running_run(),
        DiscoveryResult(parsed, DiscoveryReason.LIMIT),
    )

    assert posts.calls == [parsed[0].url, parsed[1].url]
    assert collections.observations == [
        (11, 102, 0, ObservationKind.POST, None),
        (11, 103, 1, ObservationKind.REPLY, None),
    ]
    assert result.progress == CollectionProgress(2, 2, 2, 0, 0)


@pytest.mark.parametrize(
    ("top_level_status", "saved", "duplicates"),
    [(SaveStatus.SAVED, 2, 0), (SaveStatus.DUPLICATE, 1, 1)],
)
def test_process_keeps_top_level_observations_when_parent_save_raises(
    top_level_status: SaveStatus, saved: int, duplicates: int
) -> None:
    reply = candidate(
        "2",
        handle="beta",
        order=4,
        is_reply=True,
        parent_url="https://x.com/alpha/status/1",
    )
    next_candidate = candidate("3", handle="gamma", order=5)
    top_level = saved_post(102, "2")
    next_post = saved_post(103, "3")
    service, posts, collections = collection_service(
        [
            SaveResult(top_level_status, top_level),
            RuntimeError("parent provider failed"),
            SaveResult(SaveStatus.SAVED, next_post),
        ]
    )

    result = service.process(
        for_you_running_run(), DiscoveryResult((reply, next_candidate), DiscoveryReason.LIMIT)
    )

    assert posts.calls == [reply.url, reply.parent_url, next_candidate.url]
    assert collections.observations == [
        (11, 102, 4, ObservationKind.REPLY, None),
        (11, 103, 5, ObservationKind.POST, None),
    ]
    assert result.progress == CollectionProgress(2, 2, saved, duplicates, 0)


def test_process_cancels_after_top_level_save_before_parent_enrichment() -> None:
    reply = candidate(
        "2",
        handle="beta",
        order=4,
        is_reply=True,
        parent_url="https://x.com/alpha/status/1",
    )
    next_candidate = candidate("3", handle="gamma", order=5)
    top_level = saved_post(102, "2")
    service, posts, collections = collection_service([SaveResult(SaveStatus.SAVED, top_level)])
    progress_updates: list[CollectionProgress] = []

    def cancelled() -> bool:
        return bool(posts.calls)

    result = service.process(
        for_you_running_run(),
        DiscoveryResult((reply, next_candidate), DiscoveryReason.LIMIT),
        on_progress=progress_updates.append,
        cancelled=cancelled,
    )

    assert posts.calls == [reply.url]
    assert collections.observations == [(11, 102, 4, ObservationKind.REPLY, None)]
    assert progress_updates == [CollectionProgress(2, 1, 1, 0, 0)]
    assert result.run.status is CollectionStatus.CANCELLED
    assert result.progress == CollectionProgress(2, 1, 1, 0, 0)


def test_process_finalizes_failed_then_reraises_when_cancellation_check_raises() -> None:
    service, posts, collections = collection_service([])

    def cancelled() -> bool:
        raise RuntimeError("secret cancellation detail")

    with pytest.raises(RuntimeError, match="secret cancellation detail"):
        service.process(
            running_run(),
            DiscoveryResult((candidate("1"),), DiscoveryReason.LIMIT),
            cancelled=cancelled,
        )

    assert posts.calls == []
    assert collections.finish_calls == [
        {
            "run_id": 11,
            "status": CollectionStatus.FAILED,
            "candidate_count": 1,
            "saved_count": 0,
            "duplicate_count": 0,
            "failed_count": 0,
            "reason": DiscoveryReason.ERROR.value,
            "diagnostic": "Collection processing failed",
        }
    ]


def test_process_counts_failed_save_finalizes_partial_then_reraises() -> None:
    service, _, collections = collection_service([RuntimeError("secret provider detail")])

    with pytest.raises(RuntimeError, match="secret provider detail"):
        service.process(
            running_run(),
            DiscoveryResult((candidate("1"),), DiscoveryReason.LIMIT),
        )

    assert collections.observations == []
    assert collections.finish_calls == [
        {
            "run_id": 11,
            "status": CollectionStatus.PARTIAL,
            "candidate_count": 1,
            "saved_count": 0,
            "duplicate_count": 0,
            "failed_count": 1,
            "reason": DiscoveryReason.ERROR.value,
            "diagnostic": "Collection processing failed",
        }
    ]


def test_process_preserves_saved_count_and_observation_when_observe_raises() -> None:
    saved = saved_post(101, "1")
    service, _, collections = collection_service([SaveResult(SaveStatus.SAVED, saved)])
    collections.observe_error = RuntimeError("secret database detail")

    with pytest.raises(RuntimeError, match="secret database detail"):
        service.process(
            running_run(),
            DiscoveryResult((candidate("1"),), DiscoveryReason.LIMIT),
        )

    assert collections.observations == [(11, 101, 0, ObservationKind.POST, None)]
    assert collections.finish_calls == [
        {
            "run_id": 11,
            "status": CollectionStatus.PARTIAL,
            "candidate_count": 1,
            "saved_count": 1,
            "duplicate_count": 0,
            "failed_count": 0,
            "reason": DiscoveryReason.ERROR.value,
            "diagnostic": "Collection processing failed",
        }
    ]


def test_process_preserves_progress_when_callback_raises_then_reraises() -> None:
    saved = saved_post(101, "1")
    service, _, collections = collection_service([SaveResult(SaveStatus.SAVED, saved)])

    def on_progress(progress: CollectionProgress) -> None:
        assert progress == CollectionProgress(1, 1, 1, 0, 0)
        raise RuntimeError("secret callback detail")

    with pytest.raises(RuntimeError, match="secret callback detail"):
        service.process(
            running_run(),
            DiscoveryResult((candidate("1"),), DiscoveryReason.LIMIT),
            on_progress=on_progress,
        )

    assert collections.observations == [(11, 101, 0, ObservationKind.POST, None)]
    assert collections.finish_calls == [
        {
            "run_id": 11,
            "status": CollectionStatus.PARTIAL,
            "candidate_count": 1,
            "saved_count": 1,
            "duplicate_count": 0,
            "failed_count": 0,
            "reason": DiscoveryReason.ERROR.value,
            "diagnostic": "Collection processing failed",
        }
    ]
