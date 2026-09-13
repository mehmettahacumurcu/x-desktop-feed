import sqlite3
from collections.abc import Callable
from dataclasses import FrozenInstanceError

import pytest

from xfeed.analytics import (
    AnalyticsDataset,
    AnalyticsDatasetKind,
    AnalyticsFeedScope,
    AnalyticsQuery,
    AnalyticsRepository,
    DistributionItem,
)
from xfeed.db import Database


class _CommitBetweenReadsRepository(AnalyticsRepository):
    def __init__(self, database: Database, commit: Callable[[], None]) -> None:
        super().__init__(database)
        self._commit = commit

    def _appearance_count(
        self,
        connection: sqlite3.Connection,
        ctes: str,
        parameters: tuple[object, ...],
    ) -> int:
        commit, self._commit = self._commit, None
        if commit is not None:
            commit()
        return super()._appearance_count(connection, ctes, parameters)


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "analytics.sqlite3")
    database.migrate()
    return database


@pytest.fixture
def analytics(database: Database) -> AnalyticsRepository:
    with database.transaction() as connection:
        connection.executemany(
            """INSERT INTO app_sessions(
                   id,opened_at,closed_at,state,app_version,synthetic
               ) VALUES(?,?,?,?,?,?)""",
            [
                (1, "2026-08-01 08:00:00", "2026-08-01 09:00:00", "closed", "test", 0),
                (2, "2026-08-02 08:00:00", None, "open", "test", 0),
                (3, "2026-08-02 10:00:00", None, "open", "test", 0),
            ],
        )
        connection.executemany(
            """INSERT INTO collection_runs(
                   id,target_kind,source_id,session_id,started_at,finished_at,requested_max,
                   candidate_count,saved_count,duplicate_count,failed_count,status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    11,
                    "for_you",
                    None,
                    1,
                    "2026-08-01 08:10:00",
                    "2026-08-01 08:11:00",
                    10,
                    2,
                    2,
                    0,
                    0,
                    "completed",
                ),
                (
                    12,
                    "for_you",
                    None,
                    1,
                    "2026-08-01 08:20:00",
                    "2026-08-01 08:21:00",
                    10,
                    1,
                    0,
                    1,
                    0,
                    "completed",
                ),
                (
                    21,
                    "for_you",
                    None,
                    2,
                    "2026-08-02 08:10:00",
                    "2026-08-02 08:11:00",
                    10,
                    2,
                    1,
                    1,
                    0,
                    "completed",
                ),
                (
                    22,
                    "following",
                    None,
                    2,
                    "2026-08-02 08:20:00",
                    "2026-08-02 08:21:00",
                    10,
                    2,
                    1,
                    1,
                    0,
                    "completed",
                ),
            ],
        )
        connection.executemany(
            """INSERT INTO posts(
                   id,canonical_url,x_post_id,author_handle,author_name,text,published_at,
                   embed_html,provider_json,source_method,has_media,availability
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    1,
                    "https://x.com/alice/status/1",
                    "1",
                    "alice",
                    "Alice",
                    "one",
                    None,
                    "",
                    "{}",
                    "test",
                    1,
                    "available",
                ),
                (
                    2,
                    "https://x.com/bob/status/2",
                    "2",
                    "bob",
                    "Bob",
                    "two",
                    None,
                    "",
                    "{}",
                    "test",
                    0,
                    "available",
                ),
                (
                    3,
                    "https://x.com/carol/status/3",
                    "3",
                    "carol",
                    "Carol",
                    "three",
                    None,
                    "",
                    "{}",
                    "test",
                    0,
                    "available",
                ),
                (
                    4,
                    "https://x.com/alice/status/4",
                    "4",
                    "alice",
                    "Alice",
                    "four",
                    None,
                    "",
                    "{}",
                    "test",
                    0,
                    "available",
                ),
                (
                    5,
                    "https://x.com/unknown/status/5",
                    "5",
                    None,
                    None,
                    "  ",
                    None,
                    "",
                    "{}",
                    "test",
                    0,
                    "available",
                ),
                (
                    6,
                    "https://x.com/unknown/status/6",
                    "6",
                    None,
                    None,
                    "manual",
                    None,
                    "",
                    "{}",
                    "manual",
                    0,
                    "available",
                ),
            ],
        )
        connection.executemany(
            """INSERT INTO post_observations(
                   run_id,post_id,discovery_order,observation_kind,parent_post_id,observed_at
               ) VALUES(?,?,?,?,?,?)""",
            [
                (11, 1, 0, "post", None, "2026-08-01 08:10:01"),
                (11, 2, 1, "quote", None, "2026-08-01 08:10:02"),
                (12, 1, 0, "repost", None, "2026-08-01 08:20:01"),
                (21, 3, 0, "reply", None, "2026-08-02 08:10:01"),
                (21, 4, 1, "post", None, "2026-08-02 08:10:02"),
                (22, 4, 0, "repost", None, "2026-08-02 08:20:01"),
                (22, 5, 1, "quote", None, "2026-08-02 08:20:02"),
            ],
        )
        connection.execute(
            "INSERT INTO manual_ingestions(post_id,recorded_at) VALUES(6,'2026-08-02 09:00:00')"
        )
        connection.executemany(
            """INSERT INTO post_media(
                   post_id,position,source_url,state
               ) VALUES(?,?,?,?)""",
            [
                (2, 0, "https://media.example/2.jpg", "available"),
                (3, 0, "https://media.example/3.jpg", "failed"),
            ],
        )
        connection.execute(
            """INSERT INTO topic_analysis_runs(
                   id,method,method_version,config_json,corpus_fingerprint,state,
                   completed_at,is_active
               ) VALUES(1,'tfidf_nmf','1','{}','truth','completed',
                        '2026-08-02 09:00:00',1)"""
        )
        connection.executemany(
            """INSERT INTO detected_topics(
                   id,analysis_run_id,cluster_index,display_label,keywords_json
               ) VALUES(?,?,?,?,?)""",
            [
                (101, 1, 0, "Science", "[]"),
                (102, 1, 1, "Technology", "[]"),
            ],
        )
        connection.executemany(
            """INSERT INTO detected_topic_assignments(
                   analysis_run_id,post_id,detected_topic_id,rank,normalized_score
               ) VALUES(?,?,?,?,?)""",
            [
                (1, 1, 101, 1, 0.9),
                (1, 2, 101, 1, 0.8),
                (1, 3, 102, 1, 0.7),
                (1, 4, 102, 1, 0.6),
                (1, 5, 101, 1, 0.5),
            ],
        )
    return AnalyticsRepository(database)


@pytest.mark.parametrize(
    ("dataset", "scope", "unique", "appearances"),
    [
        (AnalyticsDataset.session(1), AnalyticsFeedScope.COMBINED, 2, 3),
        (AnalyticsDataset.session(1), AnalyticsFeedScope.FOR_YOU, 2, 3),
        (AnalyticsDataset.session(1), AnalyticsFeedScope.FOLLOWING, 0, 0),
        (AnalyticsDataset.current_session(2), AnalyticsFeedScope.COMBINED, 3, 4),
        (AnalyticsDataset.current_session(2), AnalyticsFeedScope.FOR_YOU, 2, 2),
        (AnalyticsDataset.current_session(2), AnalyticsFeedScope.FOLLOWING, 2, 2),
        (AnalyticsDataset.all_feed_sessions(), AnalyticsFeedScope.COMBINED, 5, 7),
        (AnalyticsDataset.all_feed_sessions(), AnalyticsFeedScope.FOR_YOU, 4, 5),
        (AnalyticsDataset.all_feed_sessions(), AnalyticsFeedScope.FOLLOWING, 2, 2),
        (AnalyticsDataset.all_saved_posts(), AnalyticsFeedScope.COMBINED, 6, 7),
        (AnalyticsDataset.all_saved_posts(), AnalyticsFeedScope.FOR_YOU, 4, 5),
        (AnalyticsDataset.all_saved_posts(), AnalyticsFeedScope.FOLLOWING, 2, 2),
    ],
)
def test_dataset_scope_truth_table(
    analytics: AnalyticsRepository,
    dataset: AnalyticsDataset,
    scope: AnalyticsFeedScope,
    unique: int,
    appearances: int,
) -> None:
    snapshot = analytics.snapshot(AnalyticsQuery(dataset, scope))

    assert snapshot.unique_posts == unique
    assert snapshot.captured_appearances == appearances


def test_combined_distributions_reconcile_while_appearances_preserve_duplicates(
    analytics: AnalyticsRepository,
) -> None:
    snapshot = analytics.snapshot(
        AnalyticsQuery(
            AnalyticsDataset.all_feed_sessions(),
            AnalyticsFeedScope.COMBINED,
        )
    )

    for distribution in (
        snapshot.post_kinds,
        snapshot.topics,
        snapshot.authors,
        snapshot.feed_balance,
    ):
        assert sum(item.count for item in distribution) == snapshot.unique_posts
    assert snapshot.captured_appearances > snapshot.unique_posts
    assert snapshot.feed_balance.by_label("Both feeds").count == 1


def test_all_saved_uses_direct_save_unclassified_and_unknown_buckets(
    analytics: AnalyticsRepository,
) -> None:
    snapshot = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.all_saved_posts(), AnalyticsFeedScope.COMBINED)
    )

    assert snapshot.new_posts is None
    assert snapshot.already_saved is None
    assert snapshot.post_kinds.by_label("Direct save").count == 1
    assert snapshot.topics.by_label("Unclassified").count == 2
    assert snapshot.authors.by_label("Unknown").count == 2
    assert snapshot.feed_balance.by_label("Neither feed").count == 1


def _add_sessionless_source_observations(database: Database) -> None:
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO sources(id,handle,profile_url)
               VALUES(1,'source','https://x.com/source')"""
        )
        connection.execute(
            """INSERT INTO posts(
                   id,canonical_url,x_post_id,author_handle,author_name,text,published_at,
                   embed_html,provider_json,source_method,has_media,availability
               ) VALUES(7,'https://x.com/source/status/7','7','source','Source','seven',
                        NULL,'','{}','test',0,'available')"""
        )
        connection.execute(
            """INSERT INTO collection_runs(
                   id,target_kind,source_id,session_id,started_at,finished_at,requested_max,
                   candidate_count,saved_count,duplicate_count,failed_count,status
               ) VALUES(31,'source',1,NULL,'2026-08-02 09:10:00','2026-08-02 09:11:00',
                        10,2,1,1,0,'completed')"""
        )
        connection.executemany(
            """INSERT INTO post_observations(
                   run_id,post_id,discovery_order,observation_kind,parent_post_id,observed_at
               ) VALUES(?,?,?,?,?,?)""",
            [
                (31, 7, 0, "post", None, "2026-08-02 09:10:01"),
                (31, 2, 1, "reply", None, "2026-08-02 09:10:02"),
            ],
        )


def test_sessionless_source_observations_remain_without_a_synthetic_session(
    analytics: AnalyticsRepository,
    database: Database,
) -> None:
    _add_sessionless_source_observations(database)

    all_saved = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.all_saved_posts(), AnalyticsFeedScope.COMBINED)
    )
    all_feed = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.all_feed_sessions(), AnalyticsFeedScope.COMBINED)
    )
    with database.connection() as connection:
        session_count = connection.execute("SELECT COUNT(*) FROM app_sessions").fetchone()[0]

    assert (all_saved.unique_posts, all_saved.captured_appearances) == (7, 9)
    assert all_saved.post_kinds.by_label("Direct save").count == 1
    assert all_saved.post_kinds.by_label("Reply").count == 2
    assert (all_feed.unique_posts, all_feed.captured_appearances) == (5, 7)
    assert all_feed.post_kinds.by_label("Quote").count == 2
    assert session_count == 3


def test_all_saved_following_excludes_posts_observed_only_through_source(
    analytics: AnalyticsRepository,
    database: Database,
) -> None:
    _add_sessionless_source_observations(database)

    following = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.all_saved_posts(), AnalyticsFeedScope.FOLLOWING)
    )

    assert (following.unique_posts, following.captured_appearances) == (2, 2)


def test_topics_use_only_active_run_rank_one_assignments(
    analytics: AnalyticsRepository,
    database: Database,
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO topic_analysis_runs(
                   id,method,method_version,config_json,corpus_fingerprint,state,
                   completed_at,is_active
               ) VALUES(2,'tfidf_nmf','1','{}','inactive','completed',
                        '2026-08-02 10:00:00',0)"""
        )
        connection.executemany(
            """INSERT INTO detected_topics(
                   id,analysis_run_id,cluster_index,display_label,keywords_json
               ) VALUES(?,?,?,?,?)""",
            [
                (103, 1, 2, "Secondary only", "[]"),
                (201, 2, 0, "Inactive", "[]"),
            ],
        )
        connection.executemany(
            """INSERT INTO detected_topic_assignments(
                   analysis_run_id,post_id,detected_topic_id,rank,normalized_score
               ) VALUES(?,?,?,?,?)""",
            [
                (1, 1, 103, 2, 0.4),
                (2, 1, 201, 1, 1.0),
                (2, 5, 201, 1, 1.0),
            ],
        )

    snapshot = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.all_saved_posts(), AnalyticsFeedScope.COMBINED)
    )

    assert snapshot.topics.by_label("Science").count == 2
    assert snapshot.topics.by_label("Technology").count == 2
    assert snapshot.topics.by_label("Unclassified").count == 2
    assert all(item.label not in {"Secondary only", "Inactive"} for item in snapshot.topics)


def test_run_counts_are_summed_once_per_selected_run(
    analytics: AnalyticsRepository,
) -> None:
    session = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.session(1), AnalyticsFeedScope.FOR_YOU)
    )
    combined = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.all_feed_sessions(), AnalyticsFeedScope.COMBINED)
    )

    assert (session.new_posts, session.already_saved) == (2, 1)
    assert (combined.new_posts, combined.already_saved) == (4, 3)


def test_snapshot_uses_one_read_transaction_across_all_aggregates(
    analytics: AnalyticsRepository,
    database: Database,
) -> None:
    with database.connection() as connection:
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    assert mode is not None and mode[0] == "wal"

    def commit_new_observation() -> None:
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO posts(
                       id,canonical_url,x_post_id,author_handle,author_name,text,published_at,
                       embed_html,provider_json,source_method,has_media,availability
                   ) VALUES(8,'https://x.com/new/status/8','8','new','New','eight',NULL,
                            '','{}','test',0,'available')"""
            )
            connection.execute(
                """INSERT INTO collection_runs(
                       id,target_kind,source_id,session_id,started_at,finished_at,requested_max,
                       candidate_count,saved_count,duplicate_count,failed_count,status
                   ) VALUES(23,'for_you',NULL,2,'2026-08-02 08:30:00',
                            '2026-08-02 08:31:00',10,1,1,0,0,'completed')"""
            )
            connection.execute(
                """INSERT INTO post_observations(
                       run_id,post_id,discovery_order,observation_kind,parent_post_id,observed_at
                   ) VALUES(23,8,0,'post',NULL,'2026-08-02 08:30:01')"""
            )

    repository = _CommitBetweenReadsRepository(database, commit_new_observation)
    query = AnalyticsQuery(
        AnalyticsDataset.all_feed_sessions(),
        AnalyticsFeedScope.COMBINED,
    )

    old_snapshot = repository.snapshot(query)
    new_snapshot = repository.snapshot(query)

    assert (
        old_snapshot.unique_posts,
        old_snapshot.captured_appearances,
        old_snapshot.new_posts,
        old_snapshot.already_saved,
    ) == (5, 7, 4, 3)
    assert [
        (point.session_id, point.unique_posts, point.captured_appearances)
        for point in old_snapshot.trend
    ] == [(1, 2, 3), (2, 3, 4)]
    assert (
        new_snapshot.unique_posts,
        new_snapshot.captured_appearances,
        new_snapshot.new_posts,
        new_snapshot.already_saved,
    ) == (6, 8, 5, 3)
    assert [
        (point.session_id, point.unique_posts, point.captured_appearances)
        for point in new_snapshot.trend
    ] == [(1, 2, 3), (2, 4, 5)]


def test_newest_observation_deterministically_sets_unique_post_kind(
    analytics: AnalyticsRepository,
) -> None:
    first_session = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.session(1), AnalyticsFeedScope.COMBINED)
    )
    current_session = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.current_session(2), AnalyticsFeedScope.COMBINED)
    )

    assert first_session.post_kinds.by_label("Repost").count == 1
    assert first_session.post_kinds.by_label("Quote").count == 1
    assert current_session.post_kinds.by_label("Repost").count == 1
    assert current_session.post_kinds.by_label("Reply").count == 1
    assert current_session.post_kinds.by_label("Quote").count == 1


def test_media_topics_authors_and_percentages_are_per_unique_post(
    analytics: AnalyticsRepository,
) -> None:
    snapshot = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.all_saved_posts(), AnalyticsFeedScope.COMBINED)
    )

    assert snapshot.unique_authors == 4
    assert snapshot.posts_with_media == 2
    assert snapshot.media_percentage == pytest.approx(100 * 2 / 6)
    assert snapshot.topics.by_label("Science").count == 2
    assert snapshot.topics.by_label("Technology").count == 2
    assert snapshot.authors.by_label("alice").percentage == pytest.approx(100 * 2 / 6)


def test_empty_scope_has_zero_percentages_and_no_distribution_rows(
    analytics: AnalyticsRepository,
) -> None:
    snapshot = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.session(1), AnalyticsFeedScope.FOLLOWING)
    )

    assert snapshot.unique_posts == 0
    assert snapshot.media_percentage == 0.0
    assert snapshot.topics == ()
    assert snapshot.post_kinds == ()
    assert snapshot.authors == ()
    assert snapshot.feed_balance == ()


def test_multi_session_trends_are_chronological_and_preserve_zero_scope_points(
    analytics: AnalyticsRepository,
) -> None:
    combined = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.all_feed_sessions(), AnalyticsFeedScope.COMBINED)
    )
    following = analytics.snapshot(
        AnalyticsQuery(AnalyticsDataset.all_saved_posts(), AnalyticsFeedScope.FOLLOWING)
    )

    assert [point.session_id for point in combined.trend] == [1, 2]
    assert [(point.unique_posts, point.captured_appearances) for point in combined.trend] == [
        (2, 3),
        (3, 4),
    ]
    assert [
        (point.session_id, point.unique_posts, point.captured_appearances)
        for point in following.trend
    ] == [(1, 0, 0), (2, 2, 2)]


def test_sessions_are_visible_feed_sessions_newest_first(
    analytics: AnalyticsRepository,
) -> None:
    assert [session.id for session in analytics.sessions()] == [2, 1]


@pytest.mark.parametrize(
    ("kind", "session_id"),
    [
        (AnalyticsDatasetKind.CURRENT_SESSION, None),
        (AnalyticsDatasetKind.PAST_SESSION, 0),
        (AnalyticsDatasetKind.PAST_SESSION, True),
        (AnalyticsDatasetKind.ALL_FEED_SESSIONS, 1),
        (AnalyticsDatasetKind.ALL_SAVED_POSTS, 1),
    ],
)
def test_dataset_rejects_invalid_session_shapes(
    kind: AnalyticsDatasetKind,
    session_id: int | None,
) -> None:
    with pytest.raises(ValueError, match="dataset shape"):
        AnalyticsDataset(kind, session_id)


def test_query_rejects_untyped_scope_and_dataset() -> None:
    with pytest.raises(ValueError, match="dataset is invalid"):
        AnalyticsQuery("all_saved_posts", AnalyticsFeedScope.COMBINED)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="feed scope is invalid"):
        AnalyticsQuery(AnalyticsDataset.all_saved_posts(), "combined")  # type: ignore[arg-type]


def test_query_and_results_are_immutable(analytics: AnalyticsRepository) -> None:
    query = AnalyticsQuery(AnalyticsDataset.all_saved_posts(), AnalyticsFeedScope.COMBINED)
    snapshot = analytics.snapshot(query)

    with pytest.raises(FrozenInstanceError):
        query.feed_scope = AnalyticsFeedScope.FOR_YOU  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.unique_posts = 0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.topics[0].count = 0  # type: ignore[misc]


def test_distribution_lookup_rejects_missing_label() -> None:
    distribution = AnalyticsRepository.distribution(
        {"Known": 1},
        denominator=1,
    )

    assert distribution == (DistributionItem("Known", 1, 100.0),)
    with pytest.raises(KeyError, match="Missing"):
        distribution.by_label("Missing")
