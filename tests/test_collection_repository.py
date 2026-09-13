import sqlite3

import pytest

from xfeed.db import Database
from xfeed.domain import (
    Availability,
    CollectionStatus,
    CollectionTarget,
    CollectionTargetKind,
    ObservationKind,
    PostDraft,
)
from xfeed.repositories import CollectionRepository, PostRepository, SourceRepository
from xfeed.sources import canonicalize_profile


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


def source_target(source) -> CollectionTarget:
    return CollectionTarget.for_source(source.id, source.handle, source.profile_url)


def test_following_target_uses_home_without_a_source() -> None:
    target = CollectionTarget.following()

    assert target.kind is CollectionTargetKind.FOLLOWING
    assert target.source_id is None
    assert target.handle is None
    assert target.profile_url == "https://x.com/home"


def test_new_run_records_optional_session_id(tmp_path) -> None:
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO app_sessions(state,app_version)
               VALUES('open','test')"""
        )
        open_session_id = cursor.lastrowid
    assert open_session_id is not None

    run = CollectionRepository(database).start(
        CollectionTarget.following(), 20, session_id=open_session_id
    )

    assert run.target_kind is CollectionTargetKind.FOLLOWING
    assert run.session_id == open_session_id


def test_new_run_rejects_an_unknown_session_id(tmp_path) -> None:
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()

    with pytest.raises(sqlite3.IntegrityError):
        CollectionRepository(database).start(CollectionTarget.following(), 20, session_id=1)


def test_following_runs_are_found_by_the_following_target(tmp_path) -> None:
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    collections = CollectionRepository(database)
    following = CollectionTarget.following()

    run = collections.start(following, requested_max=50)

    assert collections.latest_for_target(following) == run


def test_source_run_records_ordered_observation_metadata_once(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    source = SourceRepository(database).add(canonicalize_profile("openai"))
    posts = PostRepository(database)
    saved = posts.insert(post_draft())
    parent = posts.insert(
        PostDraft(
            **{
                **post_draft().__dict__,
                "canonical_url": "https://x.com/example/status/456",
                "x_post_id": "456",
            }
        )
    )
    collections = CollectionRepository(database)

    run = collections.start(source_target(source), requested_max=30)
    collections.observe(
        run.id,
        saved.id,
        discovery_order=4,
        observation_kind=ObservationKind.QUOTE,
        parent_post_id=parent.id,
    )
    collections.observe(
        run.id,
        saved.id,
        discovery_order=0,
        observation_kind=ObservationKind.POST,
    )
    finished = collections.finish(
        run.id,
        status=CollectionStatus.COMPLETED,
        candidate_count=1,
        saved_count=1,
        duplicate_count=0,
        failed_count=0,
        reason="limit",
        diagnostic=None,
    )

    assert run.status is CollectionStatus.RUNNING
    assert run.requested_max == 30
    assert run.target_kind is CollectionTargetKind.SOURCE
    assert collections.latest_for_source(source.id) == finished
    with database.connection() as connection:
        observation = connection.execute(
            """SELECT discovery_order, observation_kind, parent_post_id
               FROM post_observations WHERE run_id=? AND post_id=?""",
            (run.id, saved.id),
        ).fetchone()
    assert tuple(observation) == (4, "quote", parent.id)


def test_source_runs_are_target_scoped_and_capped_at_thirty(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    sources = SourceRepository(database)
    alpha = sources.add(canonicalize_profile("alpha"))
    beta = sources.add(canonicalize_profile("beta"))
    collections = CollectionRepository(database)

    assert collections.latest_for_source(alpha.id) is None
    alpha_run = collections.start(source_target(alpha), requested_max=30)
    assert collections.latest_for_source(alpha.id) == alpha_run
    assert collections.latest_for_target(source_target(alpha)) == alpha_run
    alpha_run = collections.finish(
        alpha_run.id,
        status=CollectionStatus.COMPLETED,
        candidate_count=0,
        saved_count=0,
        duplicate_count=0,
        failed_count=0,
        reason="limit",
        diagnostic=None,
    )
    collections.start(source_target(beta))
    assert collections.latest_for_source(alpha.id) == alpha_run
    for requested_max in (0, 31, True, "30"):
        with pytest.raises(ValueError, match="requested_max"):
            collections.start(source_target(alpha), requested_max=requested_max)


def test_for_you_runs_are_capped_at_fifty_and_found_by_target(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    collections = CollectionRepository(database)
    home = CollectionTarget.for_you()

    run = collections.start(home, requested_max=50)

    assert run.target_kind is CollectionTargetKind.FOR_YOU
    assert run.source_id is None
    assert collections.latest_for_target(home) == run
    assert collections.latest_for_source(None) is None
    with pytest.raises(ValueError, match="target"):
        collections.start("for_you")
    for requested_max in (0, 51, True, "50"):
        with pytest.raises(ValueError, match="requested_max"):
            collections.start(home, requested_max=requested_max)


@pytest.mark.parametrize("second_target_kind", ["source", "for_you"])
def test_start_rejects_a_second_target_while_a_source_run_is_active(tmp_path, second_target_kind):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    sources = SourceRepository(database)
    alpha = sources.add(canonicalize_profile("alpha"))
    beta = sources.add(canonicalize_profile("beta"))
    collections = CollectionRepository(database)
    active = collections.start(source_target(alpha))
    second_target = (
        source_target(beta) if second_target_kind == "source" else CollectionTarget.for_you()
    )

    with pytest.raises(RuntimeError, match="already running"):
        collections.start(second_target)

    collections.finish(
        active.id,
        status=CollectionStatus.COMPLETED,
        candidate_count=0,
        saved_count=0,
        duplicate_count=0,
        failed_count=0,
        reason="limit",
        diagnostic=None,
    )
    assert collections.start(second_target).target_kind is second_target.kind


@pytest.mark.parametrize("discovery_order", [-1, True, "0"])
def test_observation_rejects_non_integer_or_negative_discovery_order(tmp_path, discovery_order):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    source = SourceRepository(database).add(canonicalize_profile("openai"))
    post = PostRepository(database).insert(post_draft())
    collections = CollectionRepository(database)
    run = collections.start(source_target(source))

    with pytest.raises(ValueError, match="discovery_order"):
        collections.observe(
            run.id,
            post.id,
            discovery_order=discovery_order,
            observation_kind=ObservationKind.POST,
        )


def test_finish_if_running_finalizes_a_running_run(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    source = SourceRepository(database).add(canonicalize_profile("openai"))
    collections = CollectionRepository(database)
    run = collections.start(source_target(source))

    finished = collections.finish_if_running(
        run.id,
        status=CollectionStatus.FAILED,
        candidate_count=2,
        saved_count=1,
        duplicate_count=0,
        failed_count=1,
        reason="error",
        diagnostic="processing failed",
    )

    assert finished.status is CollectionStatus.FAILED
    assert finished.finished_at is not None
    assert finished.candidate_count == 2
    assert finished.saved_count == 1
    assert collections.latest_for_source(source.id) == finished


def test_recover_abandoned_runs_retains_observations_and_derives_partial_counts(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    duplicate = posts.insert(post_draft())
    collections = CollectionRepository(database)
    run = collections.start(CollectionTarget.for_you())
    saved = posts.insert(
        PostDraft(
            **{
                **post_draft().__dict__,
                "canonical_url": "https://x.com/example/status/456",
                "x_post_id": "456",
            }
        )
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE posts SET created_at='2026-07-20 10:00:00' WHERE id=?",
            (duplicate.id,),
        )
        connection.execute(
            "UPDATE collection_runs SET started_at='2026-07-21 10:00:00' WHERE id=?",
            (run.id,),
        )
        connection.execute(
            "UPDATE posts SET created_at='2026-07-21 10:01:00' WHERE id=?",
            (saved.id,),
        )
    collections.observe(run.id, duplicate.id, discovery_order=0)
    collections.observe(run.id, saved.id, discovery_order=1)

    recovered = collections.recover_abandoned_runs()

    assert len(recovered) == 1
    assert recovered[0].id == run.id
    assert recovered[0].status is CollectionStatus.PARTIAL
    assert recovered[0].candidate_count == 2
    assert recovered[0].saved_count == 1
    assert recovered[0].duplicate_count == 1
    assert recovered[0].failed_count == 0
    assert recovered[0].reason == "error"
    assert recovered[0].diagnostic is not None
    assert "interrupted" in recovered[0].diagnostic.casefold()
    assert recovered[0].finished_at is not None
    assert [item.post.id for item in collections.latest_for_you_posts()] == [
        duplicate.id,
        saved.id,
    ]


def test_recover_abandoned_runs_marks_an_empty_run_failed(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    collections = CollectionRepository(database)
    run = collections.start(CollectionTarget.for_you())

    recovered = collections.recover_abandoned_runs()

    assert len(recovered) == 1
    assert recovered[0].id == run.id
    assert recovered[0].status is CollectionStatus.FAILED
    assert recovered[0].candidate_count == 0
    assert recovered[0].saved_count == 0
    assert recovered[0].duplicate_count == 0


def test_recover_abandoned_runs_is_atomic(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    collections = CollectionRepository(database)
    first = collections.start(CollectionTarget.for_you())
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO collection_runs(target_kind,source_id,requested_max,status)
               VALUES('for_you',NULL,10,'running')"""
        )
        second_id = cursor.lastrowid
        assert second_id is not None
        connection.execute(
            f"""CREATE TRIGGER fail_abandoned_recovery
                BEFORE UPDATE OF status ON collection_runs
                WHEN OLD.id={second_id}
                BEGIN
                  SELECT RAISE(ABORT, 'forced recovery failure');
                END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced recovery failure"):
        collections.recover_abandoned_runs()

    with database.connection() as connection:
        statuses = connection.execute("SELECT status FROM collection_runs ORDER BY id").fetchall()
    assert first.id != second_id
    assert [row["status"] for row in statuses] == ["running", "running"]


@pytest.mark.parametrize("terminal_status", [CollectionStatus.PARTIAL, CollectionStatus.FAILED])
def test_finish_if_running_leaves_a_terminal_run_byte_for_byte_unchanged(tmp_path, terminal_status):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    source = SourceRepository(database).add(canonicalize_profile("openai"))
    collections = CollectionRepository(database)
    run = collections.start(source_target(source))
    terminal = collections.finish(
        run.id,
        status=terminal_status,
        candidate_count=7,
        saved_count=5,
        duplicate_count=1,
        failed_count=1,
        reason="exhausted",
        diagnostic="original diagnostic",
    )
    with database.connection() as connection:
        before = dict(
            connection.execute("SELECT * FROM collection_runs WHERE id=?", (run.id,)).fetchone()
        )

    recovered = collections.finish_if_running(
        run.id,
        status=CollectionStatus.FAILED,
        candidate_count=0,
        saved_count=0,
        duplicate_count=0,
        failed_count=0,
        reason="error",
        diagnostic="replacement diagnostic",
    )

    with database.connection() as connection:
        after = dict(
            connection.execute("SELECT * FROM collection_runs WHERE id=?", (run.id,)).fetchone()
        )
    assert recovered == terminal
    assert after == before


def test_latest_for_you_posts_uses_newest_run_with_observations_in_discovery_order(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    first = posts.insert(post_draft())
    second = posts.insert(
        PostDraft(
            **{
                **post_draft().__dict__,
                "canonical_url": "https://x.com/example/status/456",
                "x_post_id": "456",
            }
        )
    )
    collections = CollectionRepository(database)
    home = CollectionTarget.for_you()
    older = collections.start(home, 30)
    collections.observe(
        older.id, first.id, discovery_order=1, observation_kind=ObservationKind.REPOST
    )
    collections.observe(
        older.id, second.id, discovery_order=0, observation_kind=ObservationKind.POST
    )
    collections.finish(
        older.id,
        status=CollectionStatus.PARTIAL,
        candidate_count=2,
        saved_count=2,
        duplicate_count=0,
        failed_count=0,
        reason="exhausted",
        diagnostic=None,
    )
    failed = collections.start(home, 10)
    collections.finish(
        failed.id,
        status=CollectionStatus.FAILED,
        candidate_count=0,
        saved_count=0,
        duplicate_count=0,
        failed_count=0,
        reason="error",
        diagnostic="no cards",
    )

    latest = collections.latest_for_you_posts()

    assert [row.post.id for row in latest] == [second.id, first.id]
    assert latest[1].observation_kind is ObservationKind.REPOST
    assert latest[1].discovery_order == 1
    assert latest[1].parent_post_id is None


def test_feed_reader_preserves_capture_order_when_publication_times_conflict(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    first = posts.insert(
        PostDraft(
            **{
                **post_draft().__dict__,
                "canonical_url": "https://x.com/example/status/1001",
                "x_post_id": "1001",
                "published_at": "2026-08-02T12:00:00Z",
            }
        )
    )
    second = posts.insert(
        PostDraft(
            **{
                **post_draft().__dict__,
                "canonical_url": "https://x.com/example/status/1002",
                "x_post_id": "1002",
                "published_at": "2026-08-02T14:00:00Z",
            }
        )
    )
    third = posts.insert(
        PostDraft(
            **{
                **post_draft().__dict__,
                "canonical_url": "https://x.com/example/status/1003",
                "x_post_id": "1003",
                "published_at": "2026-08-02T13:00:00Z",
            }
        )
    )
    collections = CollectionRepository(database)
    run = collections.start(CollectionTarget.following(), 10)
    collections.observe(run.id, first.id, discovery_order=0)
    collections.observe(run.id, second.id, discovery_order=1)
    collections.observe(run.id, third.id, discovery_order=2)

    observed = collections.feed_posts_for_runs(CollectionTargetKind.FOLLOWING, (run.id,))

    assert [item.post.id for item in observed] == [first.id, second.id, third.id]


def test_following_history_never_returns_for_you_only_posts(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    for_you_only = posts.insert(post_draft())
    following_only = posts.insert(
        PostDraft(
            **{
                **post_draft().__dict__,
                "canonical_url": "https://x.com/example/status/456",
                "x_post_id": "456",
            }
        )
    )
    collections = CollectionRepository(database)
    for_you = collections.start(CollectionTarget.for_you())
    collections.observe(for_you.id, for_you_only.id)
    collections.finish(
        for_you.id,
        status=CollectionStatus.COMPLETED,
        candidate_count=1,
        saved_count=1,
        duplicate_count=0,
        failed_count=0,
        reason="limit",
        diagnostic=None,
    )
    following = collections.start(CollectionTarget.following())
    collections.observe(following.id, following_only.id)
    collections.finish(
        following.id,
        status=CollectionStatus.COMPLETED,
        candidate_count=1,
        saved_count=1,
        duplicate_count=0,
        failed_count=0,
        reason="limit",
        diagnostic=None,
    )

    latest = collections.latest_feed_posts(CollectionTargetKind.FOLLOWING)
    current = collections.feed_posts_for_runs(
        CollectionTargetKind.FOLLOWING, (for_you.id, following.id)
    )
    older = collections.older_feed_posts(
        CollectionTargetKind.FOLLOWING,
        excluding_run_ids=(),
        excluding_post_ids=(),
    )

    assert [row.post.id for row in latest] == [following_only.id]
    assert [row.post.id for row in current] == [following_only.id]
    assert [row.post.id for row in older] == [following_only.id]


@pytest.mark.parametrize("kind", [CollectionTargetKind.SOURCE, "following"])
def test_generic_feed_queries_reject_non_home_target_kinds(tmp_path, kind: object):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    collections = CollectionRepository(database)

    with pytest.raises(ValueError, match="home feed"):
        collections.latest_feed_posts(kind)  # type: ignore[arg-type]


def test_for_you_session_and_history_queries_deduplicate_at_newest_observation(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    posts = PostRepository(database)
    first_only = posts.insert(
        PostDraft(
            **{
                **post_draft().__dict__,
                "canonical_url": "https://x.com/example/status/1",
                "x_post_id": "1",
            }
        )
    )
    shared = posts.insert(
        PostDraft(
            **{
                **post_draft().__dict__,
                "canonical_url": "https://x.com/example/status/2",
                "x_post_id": "2",
            }
        )
    )
    second_only = posts.insert(
        PostDraft(
            **{
                **post_draft().__dict__,
                "canonical_url": "https://x.com/example/status/3",
                "x_post_id": "3",
            }
        )
    )
    third_first = posts.insert(
        PostDraft(
            **{
                **post_draft().__dict__,
                "canonical_url": "https://x.com/example/status/4",
                "x_post_id": "4",
            }
        )
    )
    collections = CollectionRepository(database)
    home = CollectionTarget.for_you()

    first = collections.start(home, 10)
    collections.observe(first.id, first_only.id, discovery_order=0)
    collections.observe(first.id, shared.id, discovery_order=1)
    collections.finish(
        first.id,
        status=CollectionStatus.COMPLETED,
        candidate_count=2,
        saved_count=2,
        duplicate_count=0,
        failed_count=0,
        reason="limit",
        diagnostic=None,
    )
    second = collections.start(home, 10)
    collections.observe(second.id, shared.id, discovery_order=0)
    collections.observe(second.id, second_only.id, discovery_order=1)
    collections.finish(
        second.id,
        status=CollectionStatus.COMPLETED,
        candidate_count=2,
        saved_count=1,
        duplicate_count=1,
        failed_count=0,
        reason="limit",
        diagnostic=None,
    )
    third = collections.start(home, 10)
    collections.observe(third.id, third_first.id, discovery_order=0)
    collections.observe(third.id, shared.id, discovery_order=1)
    collections.finish(
        third.id,
        status=CollectionStatus.COMPLETED,
        candidate_count=2,
        saved_count=1,
        duplicate_count=1,
        failed_count=0,
        reason="limit",
        diagnostic=None,
    )

    current = collections.for_you_posts_for_runs((second.id, third.id))
    older = collections.older_for_you_posts(
        excluding_run_ids=(second.id, third.id),
        excluding_post_ids=tuple(row.post.id for row in current),
    )

    assert [row.post.id for row in current] == [third_first.id, shared.id, second_only.id]
    assert [row.post.id for row in older] == [first_only.id]


def test_for_you_history_query_validates_limits_and_empty_run_sets(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    collections = CollectionRepository(database)

    assert collections.for_you_posts_for_runs(()) == []
    assert collections.older_for_you_posts((), ()) == []
    with pytest.raises(ValueError, match="limit"):
        collections.older_for_you_posts((), (), limit=0)


def test_removing_source_cascades_only_its_source_runs(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    sources = SourceRepository(database)
    source = sources.add(canonicalize_profile("openai"))
    collections = CollectionRepository(database)
    source_run = collections.start(source_target(source))
    collections.finish(
        source_run.id,
        status=CollectionStatus.COMPLETED,
        candidate_count=0,
        saved_count=0,
        duplicate_count=0,
        failed_count=0,
        reason="limit",
        diagnostic=None,
    )
    home_run = collections.start(CollectionTarget.for_you())

    sources.remove(source.id)

    with database.connection() as connection:
        remaining = connection.execute(
            "SELECT id, target_kind FROM collection_runs ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["target_kind"]) for row in remaining] == [(home_run.id, "for_you")]
    assert source_run.id != home_run.id
