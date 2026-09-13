import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from xfeed.db import Database
from xfeed.domain import Availability, LabelKind, LabelOrigin, PostDraft
from xfeed.oembed import OEmbedClient, ProviderUnavailable
from xfeed.posts import PostService, RefreshStatus, SaveStatus
from xfeed.repositories import LabelRepository, PostRepository
from xfeed.urls import CanonicalPostUrl


MALFORMED_NETLOC_URLS = [
    "https://[x.com/example/status/123",
    "https://x.com:not-a-port/example/status/123",
    "https://x.com:99999/example/status/123",
]

MALFORMED_OEMBED_PAYLOADS = [
    None,
    [],
    "not an object",
    {"html": None},
    {"html": []},
    {"html": ""},
    {"html": "   "},
    {"html": "<p>valid text</p>", "author_name": []},
    {"html": "<p>valid text</p>", "author_name": {}},
]


def post_draft() -> PostDraft:
    return PostDraft(
        canonical_url="https://x.com/example/status/123",
        x_post_id="123",
        author_handle="example",
        author_name="Example",
        text="original text",
        published_at="2014-05-05",
        embed_html="<blockquote>original</blockquote>",
        provider_json='{"version":"original"}',
        source_method="oembed",
        has_media=False,
    )


class FixtureProvider:
    def __init__(self, draft: PostDraft) -> None:
        self.draft = draft
        self.error: str | None = None
        self.calls: list[CanonicalPostUrl] = []

    def fetch(self, post_url: CanonicalPostUrl) -> PostDraft:
        self.calls.append(post_url)
        if self.error is not None:
            message = self.error
            self.error = None
            raise ProviderUnavailable(message)
        return replace(
            self.draft,
            canonical_url=post_url.url,
            x_post_id=post_url.post_id,
            author_handle=post_url.handle,
        )

    def fail_next(self, message: str) -> None:
        self.error = message

    def return_next(self, **changes: object) -> None:
        self.draft = replace(self.draft, **changes)


@pytest.fixture
def pipeline(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    labels = LabelRepository(database)
    provider = FixtureProvider(post_draft())
    service = PostService(posts, provider)
    return database, posts, labels, provider, service


def test_save_persists_provider_metadata(pipeline):
    _, posts, _, provider, service = pipeline

    result = service.save("https://twitter.com/Example/status/123?s=20")

    assert result.status is SaveStatus.SAVED
    assert result.message == "Saved"
    assert result.post is not None
    assert result.post == posts.by_id(result.post.id)
    assert result.post.canonical_url == "https://x.com/example/status/123"
    assert provider.calls == [
        CanonicalPostUrl("https://x.com/example/status/123", "example", "123")
    ]


def test_save_returns_existing_post_for_duplicate_without_refetching(pipeline):
    _, _, _, provider, service = pipeline
    saved = service.save("https://x.com/example/status/123").post
    assert saved is not None

    result = service.save("https://twitter.com/EXAMPLE/status/123/photo/1")

    assert result.status is SaveStatus.DUPLICATE
    assert result.post == saved
    assert result.message == "Already saved"
    assert len(provider.calls) == 1


def test_concurrent_save_of_same_canonical_url_returns_saved_and_duplicate(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    barrier = threading.Barrier(2)

    class ConcurrentProvider(FixtureProvider):
        def fetch(self, post_url: CanonicalPostUrl) -> PostDraft:
            result = super().fetch(post_url)
            barrier.wait(timeout=2)
            return result

    service = PostService(posts, ConcurrentProvider(post_draft()))
    urls = (
        "https://x.com/example/status/123",
        "https://twitter.com/EXAMPLE/status/123?s=20",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(service.save, urls))

    assert sorted(result.status for result in results) == [
        SaveStatus.DUPLICATE,
        SaveStatus.SAVED,
    ]
    assert {result.post.id for result in results if result.post is not None} == {1}
    assert len(posts.list_recent()) == 1


def test_save_reraises_unrelated_integrity_failure(pipeline):
    _, posts, _, provider, service = pipeline
    provider.return_next(text=None)  # type: ignore[arg-type]

    with pytest.raises(sqlite3.IntegrityError):
        service.save("https://x.com/example/status/123")

    assert posts.by_url("https://x.com/example/status/123") is None


def test_quick_save_records_manual_provenance_for_new_and_collected_duplicates(pipeline):
    _, posts, _, _, service = pipeline

    collected = service.save("https://x.com/example/status/123")

    assert collected.status is SaveStatus.SAVED
    assert collected.post is not None
    assert not posts.was_manually_ingested(collected.post.id)

    duplicate = service.quick_save("https://twitter.com/EXAMPLE/status/123?s=20")

    assert duplicate.status is SaveStatus.DUPLICATE
    assert duplicate.post == collected.post
    assert posts.was_manually_ingested(collected.post.id)
    assert posts.by_id(collected.post.id).source_method == "oembed"

    manual = service.quick_save("https://x.com/example/status/456")

    assert manual.status is SaveStatus.SAVED
    assert manual.post is not None
    assert posts.was_manually_ingested(manual.post.id)
    assert manual.post.source_method == "oembed"


def test_save_reports_invalid_url_without_fetching(pipeline):
    _, _, _, provider, service = pipeline

    result = service.save("https://example.com/example/status/123")

    assert result.status is SaveStatus.INVALID
    assert result.post is None
    assert result.message
    assert provider.calls == []


@pytest.mark.parametrize("raw_url", MALFORMED_NETLOC_URLS)
def test_save_reports_malformed_netloc_as_invalid_without_fetching(pipeline, raw_url):
    _, posts, _, provider, service = pipeline

    result = service.save(raw_url)

    assert result.status is SaveStatus.INVALID
    assert result.post is None
    assert result.message
    assert provider.calls == []
    assert posts.by_url("https://x.com/example/status/123") is None


def test_save_reports_provider_unavailable_without_partial_insert(pipeline):
    _, posts, _, provider, service = pipeline
    provider.fail_next("offline")

    result = service.save("https://x.com/example/status/123")

    assert result.status is SaveStatus.UNAVAILABLE
    assert result.post is None
    assert result.message == "offline"
    assert posts.by_url("https://x.com/example/status/123") is None


@pytest.mark.parametrize("payload", MALFORMED_OEMBED_PAYLOADS)
def test_save_reports_malformed_provider_shape_without_partial_insert(pipeline, payload):
    _, posts, _, _, _ = pipeline

    def malformed_transport(url: str, timeout: float) -> bytes:
        return json.dumps(payload).encode()

    service = PostService(posts, OEmbedClient(transport=malformed_transport))

    result = service.save("https://x.com/example/status/123")

    assert result.status is SaveStatus.UNAVAILABLE
    assert result.post is None
    assert result.message
    assert posts.by_url("https://x.com/example/status/123") is None


def test_refresh_reports_missing_post_without_fetching(pipeline):
    _, _, _, provider, service = pipeline

    result = service.refresh(999)

    assert result.status is RefreshStatus.NOT_FOUND
    assert result.post is None
    assert result.message == "Post not found"
    assert provider.calls == []


def test_refresh_failure_keeps_snapshot_and_success_keeps_local_data(pipeline):
    database, posts, labels, provider, service = pipeline
    saved = service.save("https://x.com/example/status/123").post
    assert saved is not None
    label = labels.append(
        saved.id,
        LabelKind.UNWANTED,
        LabelOrigin.USER,
        "clickbait",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE posts SET updated_at=? WHERE id=?",
            ("2000-01-01 00:00:00", saved.id),
        )

    provider.fail_next("offline")
    failed = service.refresh(saved.id)

    assert failed.status is RefreshStatus.UNAVAILABLE
    assert failed.post == saved
    assert failed.message == "offline"
    assert posts.by_id(saved.id) == saved

    provider.return_next(
        author_name="Updated Name",
        text="updated text",
        published_at="2026-07-16",
        embed_html="<blockquote>updated</blockquote>",
        provider_json='{"version":"updated"}',
        source_method="oembed-refresh",
        has_media=True,
        availability=Availability.OFFLINE,
    )
    refreshed = service.refresh(saved.id)

    assert refreshed.status is RefreshStatus.REFRESHED
    assert refreshed.message == "Refreshed"
    assert refreshed.post is not None
    assert refreshed.post.id == saved.id
    assert refreshed.post.canonical_url == saved.canonical_url
    assert refreshed.post.x_post_id == saved.x_post_id
    assert refreshed.post.author_handle == "example"
    assert refreshed.post.author_name == "Updated Name"
    assert refreshed.post.text == "updated text"
    assert refreshed.post.published_at == "2026-07-16"
    assert refreshed.post.embed_html == "<blockquote>updated</blockquote>"
    assert refreshed.post.provider_json == '{"version":"updated"}'
    assert refreshed.post.source_method == "oembed-refresh"
    assert refreshed.post.has_media is True
    assert refreshed.post.availability is Availability.OFFLINE
    assert posts.by_id(saved.id) == refreshed.post
    assert labels.active_for_post(saved.id) == label
    assert labels.history_for_post(saved.id) == [label]
    with database.connection() as connection:
        updated_at = connection.execute(
            "SELECT updated_at FROM posts WHERE id=?",
            (saved.id,),
        ).fetchone()["updated_at"]
    assert updated_at != "2000-01-01 00:00:00"
