from xfeed.db import Database
import pytest

from xfeed.domain import (
    AuthorCount,
    Availability,
    CollectionTarget,
    LabelKind,
    LabelOrigin,
    ObservationKind,
    PostDraft,
)
from xfeed.repositories import CollectionRepository, LabelRepository, PostRepository


def post_draft() -> PostDraft:
    return PostDraft(
        canonical_url="https://x.com/example/status/123",
        x_post_id="123",
        author_handle="example",
        author_name="Example",
        text="A useful post",
        published_at=None,
        embed_html="<blockquote></blockquote>",
        provider_json="{}",
        source_method="oembed",
        has_media=True,
        availability=Availability.OFFLINE,
    )


def authored_post_draft(
    x_post_id: str,
    author_handle: str,
    published_at: str,
) -> PostDraft:
    return PostDraft(
        canonical_url=f"https://x.com/{author_handle}/status/{x_post_id}",
        x_post_id=x_post_id,
        author_handle=author_handle,
        author_name=author_handle.title(),
        text=f"Post {x_post_id}",
        published_at=published_at,
        embed_html="<blockquote></blockquote>",
        provider_json="{}",
        source_method="oembed",
    )


def test_inserted_post_can_be_reloaded_by_url_and_id(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)

    inserted = posts.insert(post_draft())

    assert inserted.id > 0
    assert posts.by_url(inserted.canonical_url) == inserted
    assert posts.by_id(inserted.id) == inserted


def test_manual_ingestion_marker_is_separate_and_idempotent(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    ordinary = posts.insert(post_draft())
    manual = posts.insert(
        authored_post_draft("456", "example", "2026-07-17T00:00:00Z"),
        manual_ingestion=True,
    )

    assert not posts.was_manually_ingested(ordinary.id)
    assert posts.was_manually_ingested(manual.id)
    posts.record_manual_ingestion(ordinary.id)
    posts.record_manual_ingestion(ordinary.id)
    assert posts.was_manually_ingested(ordinary.id)
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT post_id FROM manual_ingestions ORDER BY post_id"
        ).fetchall()
    assert [row["post_id"] for row in rows] == [ordinary.id, manual.id]


def test_missing_posts_return_none(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)

    assert posts.by_url("https://x.com/example/status/missing") is None
    assert posts.by_id(999) is None


def test_latest_label_is_active_and_history_is_preserved(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    labels = LabelRepository(database)
    post = posts.insert(post_draft())

    first = labels.append(post.id, LabelKind.USEFUL, LabelOrigin.USER, None)
    second = labels.append(post.id, LabelKind.UNWANTED, LabelOrigin.USER, "off_topic")

    assert labels.active_for_post(post.id) == second
    assert [event.id for event in labels.history_for_post(post.id)] == [first.id, second.id]

    with database.connection() as connection:
        rows = connection.execute(
            "SELECT id, supersedes_id FROM label_events WHERE post_id=? ORDER BY id",
            (post.id,),
        ).fetchall()

    assert [(row["id"], row["supersedes_id"]) for row in rows] == [
        (first.id, None),
        (second.id, first.id),
    ]


def test_post_and_label_data_survive_repository_reconstruction(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    post = PostRepository(database).insert(post_draft())
    first = LabelRepository(database).append(
        post.id,
        LabelKind.NEUTRAL,
        LabelOrigin.IMPORTED,
        "legacy",
    )

    reloaded_post = PostRepository(Database(database.path)).by_id(post.id)
    reloaded_label = LabelRepository(Database(database.path)).active_for_post(post.id)

    assert reloaded_post == post
    assert reloaded_label == first


def test_recent_posts_and_author_counts_are_case_insensitive(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    posts.insert(authored_post_draft("1", "alpha", "2025-01-01T00:00:00Z"))
    posts.insert(authored_post_draft("2", "beta", "2025-01-02T00:00:00Z"))
    posts.insert(authored_post_draft("3", "alpha", "2025-01-03T00:00:00Z"))

    assert posts.author_counts() == [AuthorCount("alpha", 2), AuthorCount("beta", 1)]
    assert [post.x_post_id for post in posts.list_recent()] == ["3", "2", "1"]
    assert [post.author_handle for post in posts.list_recent("ALPHA")] == [
        "alpha",
        "alpha",
    ]
    assert posts.count_by_author("ALPHA") == 2


def test_recent_posts_respect_limit_and_reject_invalid_limits(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    posts.insert(authored_post_draft("1", "alpha", "2025-01-01T00:00:00Z"))
    posts.insert(authored_post_draft("2", "alpha", "2025-01-02T00:00:00Z"))

    assert [post.x_post_id for post in posts.list_recent(limit=1)] == ["2"]
    with pytest.raises(ValueError, match="limit"):
        posts.list_recent(limit=0)


def test_observed_post_join_uses_only_saved_post_columns(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    post = posts.insert(post_draft())
    collections = CollectionRepository(database)
    run = collections.start(CollectionTarget.for_you())
    collections.observe(
        run.id,
        post.id,
        discovery_order=0,
        observation_kind=ObservationKind.POST,
    )

    observed = collections.latest_for_you_posts()

    assert observed[0].post == post


def test_manual_posts_are_isolated_newest_first_and_bounded(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    ordinary = posts.insert(authored_post_draft("1", "alpha", "2025-01-01T00:00:00Z"))
    older = posts.insert(
        authored_post_draft("2", "alpha", "2025-01-02T00:00:00Z"),
        manual_ingestion=True,
    )
    newer = posts.insert(
        authored_post_draft("3", "beta", "2025-01-03T00:00:00Z"),
        manual_ingestion=True,
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE manual_ingestions SET recorded_at='2025-01-01 10:00:00' WHERE post_id=?",
            (older.id,),
        )
        connection.execute(
            "UPDATE manual_ingestions SET recorded_at='2025-01-02 10:00:00' WHERE post_id=?",
            (newer.id,),
        )

    assert [post.id for post in posts.list_manual()] == [newer.id, older.id]
    assert [post.id for post in posts.list_manual(limit=1)] == [newer.id]
    assert ordinary.id not in {post.id for post in posts.list_manual()}
    with pytest.raises(ValueError, match="limit"):
        posts.list_manual(limit=0)


def test_selected_author_posts_are_case_insensitive_unique_and_newest_first(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    older = posts.insert(authored_post_draft("1", "alpha", "2025-01-01T00:00:00Z"))
    posts.insert(authored_post_draft("2", "gamma", "2025-01-04T00:00:00Z"))
    newer = posts.insert(authored_post_draft("3", "beta", "2025-01-03T00:00:00Z"))

    selected = posts.list_for_authors(("ALPHA", "@beta", "alpha"))

    assert [post.id for post in selected] == [newer.id, older.id]
    assert posts.list_for_authors(()) == []
    assert [post.id for post in posts.list_for_authors(("alpha",), limit=1)] == [older.id]
    with pytest.raises(ValueError, match="limit"):
        posts.list_for_authors(("alpha",), limit=0)
    with pytest.raises(ValueError, match="handle"):
        posts.list_for_authors(("bad/path",))
