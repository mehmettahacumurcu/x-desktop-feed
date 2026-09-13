import pytest

from xfeed.db import Database
from xfeed.domain import Availability, PostDraft
from xfeed.media import (
    MediaAssetState,
    MediaOrigin,
    MediaResolutionState,
    PhotoCandidate,
    normalize_photo_url,
)
from xfeed.media_repository import MediaRepository
from xfeed.repositories import PostRepository


def photo(identifier: str, position: int = 0, alt_text: str | None = None) -> PhotoCandidate:
    return PhotoCandidate(
        position,
        f"https://pbs.twimg.com/media/{identifier}?name=small&format=jpg",
        alt_text,
    )


def repository_with_post(tmp_path) -> tuple[MediaRepository, int]:
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    saved = PostRepository(database).insert(
        PostDraft(
            canonical_url="https://x.com/example/status/123",
            x_post_id="123",
            author_handle="example",
            author_name="Example",
            text="A photo post",
            published_at=None,
            embed_html="<blockquote></blockquote>",
            provider_json="{}",
            source_method="oembed",
            has_media=True,
            availability=Availability.AVAILABLE,
        )
    )
    return MediaRepository(database), saved.id


def test_photo_candidate_requires_bounded_nonnegative_position() -> None:
    assert (
        PhotoCandidate(0, "https://pbs.twimg.com/media/abc?format=jpg&name=large", None).position
        == 0
    )
    for position in (-1, 4, True, "0"):
        with pytest.raises(ValueError, match="position"):
            PhotoCandidate(position, "https://pbs.twimg.com/media/abc?format=jpg&name=large", None)


def test_photo_url_normalization_requires_exact_x_photo_host() -> None:
    assert (
        normalize_photo_url("https://pbs.twimg.com/media/abc?name=small&format=jpg")
        == "https://pbs.twimg.com/media/abc?format=jpg&name=large"
    )
    for value in (
        "https://example.com/media/abc",
        "http://pbs.twimg.com/media/abc",
        "https://user@pbs.twimg.com/media/abc",
        "https://pbs.twimg.com:444/media/abc",
        "https://pbs.twimg.com/media/abc#fragment",
        "https://pbs.twimg.com/not-media/abc",
        "https://pbs.twimg.com/media/",
    ):
        with pytest.raises(ValueError, match="photo URL"):
            normalize_photo_url(value)


def test_photo_url_normalization_preserves_format_replaces_name_and_sorts_query() -> None:
    assert (
        normalize_photo_url("https://pbs.twimg.com/media/abc?z=one&format=png&name=small&a=two")
        == "https://pbs.twimg.com/media/abc?a=two&format=png&name=large&z=one"
    )
    with pytest.raises(ValueError, match="photo URL"):
        normalize_photo_url("https://pbs.twimg.com/media/abc?format=jpg&format=png")


@pytest.mark.parametrize(
    "value",
    (
        "https://pbs.twimg.com/foo/../media/abc?format=jpg",
        "https://pbs.twimg.com/media/../not-media?format=jpg",
        "https://pbs.twimg.com/media/./abc?format=jpg",
        "https://pbs.twimg.com/foo/%2e%2e/media/abc?format=jpg",
        "https://pbs.twimg.com/media/%2e%2e/not-media?format=jpg",
        "https://pbs.twimg.com/media/%2E/abc?format=jpg",
    ),
)
def test_photo_url_normalization_rejects_literal_and_encoded_dot_segments(value: str) -> None:
    with pytest.raises(ValueError, match="photo URL"):
        normalize_photo_url(value)


@pytest.mark.parametrize(
    "value",
    (
        "https://pbs.twimg.com/media/abc/extra?format=jpg",
        r"https://pbs.twimg.com/media/abc\..\secret?format=jpg",
        "https://pbs.twimg.com/media/abc%2f..%2fsecret?format=jpg",
        "https://pbs.twimg.com/media/abc%5c..%5csecret?format=jpg",
        "https://pbs.twimg.com/media/%252e%252e%252fsecret?format=jpg",
        "https://pbs.twimg.com/media/%25252e%25252e%25252fsecret?format=jpg",
        "https://pbs.twimg.com/media/abc%?format=jpg",
        "https://pbs.twimg.com/media/abc%2?format=jpg",
    ),
)
def test_photo_url_normalization_rejects_separator_smuggling_and_bad_escapes(
    value: str,
) -> None:
    with pytest.raises(ValueError, match="photo URL"):
        normalize_photo_url(value)


def test_photo_url_normalization_canonicalizes_safe_encoded_identifier() -> None:
    assert (
        normalize_photo_url("https://pbs.twimg.com/media/%61bc?format=jpg")
        == "https://pbs.twimg.com/media/abc?format=jpg&name=large"
    )


def test_photo_url_normalization_matches_extension_length_ceiling() -> None:
    with pytest.raises(ValueError, match="photo URL"):
        normalize_photo_url(f"https://pbs.twimg.com/media/{'a' * 2_100}")


@pytest.mark.parametrize(
    "value",
    (
        " https://pbs.twimg.com/media/abc?format=jpg",
        "https://pbs.twimg.com/media/abc?format=jpg ",
        "\thttps://pbs.twimg.com/media/abc?format=jpg",
        "https://pbs.twimg.com/\tmedia/abc?format=jpg",
        "https://pbs.twimg.com/media/abc\n?format=jpg",
        "https://pbs.twimg.com/media/abc?\rformat=jpg",
        "https://pbs.twimg.com/media/abc\x00?format=jpg",
        "\x1fhttps://pbs.twimg.com/media/abc?format=jpg",
        "https://pbs.twimg.com/media/abc\x7f?format=jpg",
        "\ufeffhttps://pbs.twimg.com/media/abc?format=jpg",
        "https://pbs.twimg.com/media/abc?format=jpg\ufeff",
        "\u0085https://pbs.twimg.com/media/abc?format=jpg",
        "https://pbs.twimg.com/media/abc?format=jpg\u0085",
    ),
)
def test_photo_url_normalization_rejects_whitespace_and_control_characters(
    value: str,
) -> None:
    with pytest.raises(ValueError, match="photo URL"):
        normalize_photo_url(value)


def test_record_manifest_is_terminal_for_empty_manifests_and_idempotent(tmp_path) -> None:
    repository, post_id = repository_with_post(tmp_path)
    canonical_url = "https://x.com/example/status/123"

    assert repository.record_manifest(post_id, canonical_url, MediaOrigin.COLLECTION, ()) == ()
    job = repository.ensure_resolution_job(post_id, canonical_url, MediaOrigin.COLLECTION)
    assert job.state is MediaResolutionState.RESOLVED
    assert job.manifest_count == 0

    first = repository.record_manifest(
        post_id, canonical_url, MediaOrigin.COLLECTION, (photo("one"),)
    )
    second = repository.record_manifest(
        post_id, canonical_url, MediaOrigin.COLLECTION, (photo("one"),)
    )
    assert second == first
    assert first[0].source_url == "https://pbs.twimg.com/media/one?format=jpg&name=large"
    assert first[0].state is MediaAssetState.PENDING


def test_record_manifest_requires_exactly_contiguous_positions(tmp_path) -> None:
    repository, post_id = repository_with_post(tmp_path)

    with pytest.raises(ValueError, match="positions"):
        repository.record_manifest(
            post_id,
            "https://x.com/example/status/123",
            MediaOrigin.COLLECTION,
            (photo("one", 1),),
        )


def test_changed_manifest_position_never_overwrites_available_asset(tmp_path) -> None:
    repository, post_id = repository_with_post(tmp_path)
    canonical_url = "https://x.com/example/status/123"
    asset = repository.record_manifest(post_id, canonical_url, MediaOrigin.MANUAL, (photo("one"),))[
        0
    ]
    claimed = repository.claim_asset(asset.id)
    assert claimed is not None
    available = repository.mark_asset_available(
        asset.id,
        local_path="123//one.jpg",
        mime_type="image/jpeg",
        width=640,
        height=480,
    )
    assert available.local_path == "123/one.jpg"

    assets = repository.record_manifest(post_id, canonical_url, MediaOrigin.MANUAL, (photo("two"),))

    assert assets[0].source_url.endswith("/two?format=jpg&name=large")
    assert assets[0].state is MediaAssetState.PENDING
    assert assets[0].local_path is None


@pytest.mark.parametrize(
    "local_path",
    (
        "",
        ".",
        "..",
        "../one.jpg",
        "media/../one.jpg",
        "/one.jpg",
        "//server/one.jpg",
        "C:/one.jpg",
        r"C:\one.jpg",
        r"media\one.jpg",
    ),
)
def test_mark_asset_available_rejects_unsafe_local_paths(tmp_path, local_path) -> None:
    repository, post_id = repository_with_post(tmp_path)
    asset = repository.record_manifest(
        post_id, "https://x.com/example/status/123", MediaOrigin.COLLECTION, (photo("one"),)
    )[0]
    assert repository.claim_asset(asset.id) is not None

    with pytest.raises(ValueError, match="local_path"):
        repository.mark_asset_available(
            asset.id,
            local_path=local_path,
            mime_type="image/jpeg",
            width=640,
            height=480,
        )


def test_shorter_manifest_removes_omitted_assets_from_queries_and_claims(tmp_path) -> None:
    repository, post_id = repository_with_post(tmp_path)
    canonical_url = "https://x.com/example/status/123"
    first, removed = repository.record_manifest(
        post_id,
        canonical_url,
        MediaOrigin.COLLECTION,
        (photo("one", 0), photo("two", 1)),
    )

    current = repository.record_manifest(
        post_id, canonical_url, MediaOrigin.COLLECTION, (photo("one", 0),)
    )

    assert current == (first,)
    assert repository.assets_for_posts((post_id,))[post_id] == (first,)
    assert [asset.id for asset in repository.pending_assets()] == [first.id]
    assert repository.claim_asset(removed.id) is None


def test_claims_are_compare_and_set_and_increment_attempts_once(tmp_path) -> None:
    repository, post_id = repository_with_post(tmp_path)
    canonical_url = "https://x.com/example/status/123"
    job = repository.ensure_resolution_job(post_id, canonical_url, MediaOrigin.COLLECTION)

    claimed = repository.claim_resolution(job.id)
    assert claimed is not None
    assert claimed.state is MediaResolutionState.RESOLVING
    assert claimed.attempts == 1
    assert repository.claim_resolution(job.id) is None
    repository.requeue_resolution(job.id, "retry")
    assert repository.claim_resolution(job.id).attempts == 2  # type: ignore[union-attr]

    asset = repository.record_manifest(
        post_id, canonical_url, MediaOrigin.COLLECTION, (photo("one"),)
    )[0]
    claimed_asset = repository.claim_asset(asset.id)
    assert claimed_asset is not None and claimed_asset.attempts == 1
    assert repository.claim_asset(asset.id) is None


def test_queries_are_position_ordered_and_post_input_is_deduplicated(tmp_path) -> None:
    repository, first_post_id = repository_with_post(tmp_path)
    database = repository.database
    second_post = PostRepository(database).insert(
        PostDraft(
            canonical_url="https://x.com/example/status/456",
            x_post_id="456",
            author_handle="example",
            author_name="Example",
            text="Another photo post",
            published_at=None,
            embed_html="<blockquote></blockquote>",
            provider_json="{}",
            source_method="oembed",
            has_media=True,
            availability=Availability.AVAILABLE,
        )
    )
    repository.record_manifest(
        first_post_id,
        "https://x.com/example/status/123",
        MediaOrigin.COLLECTION,
        (photo("first", 0), photo("second", 1)),
    )
    repository.record_manifest(
        second_post.id,
        second_post.canonical_url,
        MediaOrigin.COLLECTION,
        (photo("third", 0),),
    )

    grouped = repository.assets_for_posts((second_post.id, first_post_id, first_post_id))

    assert list(grouped) == [second_post.id, first_post_id]
    assert [asset.position for asset in grouped[first_post_id]] == [0, 1]
    assert [asset.position for asset in grouped[second_post.id]] == [0]


def test_reset_interrupted_only_requeues_claimed_work(tmp_path) -> None:
    repository, post_id = repository_with_post(tmp_path)
    canonical_url = "https://x.com/example/status/123"
    asset = repository.record_manifest(
        post_id, canonical_url, MediaOrigin.COLLECTION, (photo("one"),)
    )[0]
    repository.claim_asset(asset.id)
    interrupted_post = PostRepository(repository.database).insert(
        PostDraft(
            canonical_url="https://x.com/example/status/456",
            x_post_id="456",
            author_handle="example",
            author_name="Example",
            text="Another photo post",
            published_at=None,
            embed_html="<blockquote></blockquote>",
            provider_json="{}",
            source_method="oembed",
            has_media=True,
            availability=Availability.AVAILABLE,
        )
    )
    job = repository.ensure_resolution_job(
        interrupted_post.id, interrupted_post.canonical_url, MediaOrigin.COLLECTION
    )
    repository.claim_resolution(job.id)

    repository.reset_interrupted()

    assert repository.pending_resolution_jobs()[0].state is MediaResolutionState.PENDING
    assert repository.pending_assets()[0].state is MediaAssetState.PENDING
