import json
import sqlite3

import pytest

from xfeed.db import Database
from xfeed.topic_analysis import (
    AnalysisPost,
    DetectedTopicDraft,
    TopicAnalysisMetadata,
    TopicAnalysisState,
    TopicAssignmentDraft,
    TopicAnalyzer,
    TopicModelDraft,
    TopicModelState,
)
from xfeed.topic_repository import TopicAnalysisRepository


def add_post(
    database: Database,
    post_id: int,
    text: str,
    *,
    updated_at: str = "2026-08-01 12:00:00",
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO posts(
                   id,canonical_url,x_post_id,text,embed_html,provider_json,source_method,
                   availability,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                post_id,
                f"https://x.com/example/status/{post_id}",
                str(post_id),
                text,
                "<blockquote></blockquote>",
                "{}",
                "oembed",
                "available",
                updated_at,
            ),
        )


@pytest.fixture
def database(tmp_path) -> Database:
    result = Database(tmp_path / "feed.sqlite3")
    result.migrate()
    add_post(result, 1, "python typing package")
    add_post(result, 2, "sqlite query database", updated_at="2026-08-02 12:00:00")
    return result


@pytest.fixture
def topic_repository(database: Database) -> TopicAnalysisRepository:
    return TopicAnalysisRepository(database)


def draft(fingerprint: str = "fingerprint") -> TopicModelDraft:
    config_json = json.dumps(
        {
            "max_df": 0.9,
            "max_features": 5000,
            "max_iter": 400,
            "method": "tfidf_nmf",
            "method_version": "1",
            "random_state": 42,
            "secondary_threshold": 0.35,
            "stop_words_version": "en-tr-1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return TopicModelDraft(
        state=TopicModelState.BUILT,
        config_json=config_json,
        corpus_fingerprint=fingerprint,
        topics=(
            DetectedTopicDraft(
                cluster_index=0,
                display_label="python · typing",
                keywords_json='["python","typing"]',
            ),
        ),
        assignments=(
            TopicAssignmentDraft(
                post_id=1,
                cluster_index=0,
                rank=1,
                normalized_score=1.0,
            ),
        ),
    )


def test_corpus_returns_saved_posts_in_stable_id_order(
    topic_repository: TopicAnalysisRepository,
) -> None:
    assert topic_repository.corpus() == (
        AnalysisPost(id=1, text="python typing package", updated_at="2026-08-01 12:00:00"),
        AnalysisPost(id=2, text="sqlite query database", updated_at="2026-08-02 12:00:00"),
    )


def test_corpus_and_fingerprint_exclude_whitespace_only_saved_posts(
    topic_repository: TopicAnalysisRepository,
    database: Database,
) -> None:
    posts = topic_repository.corpus()
    expected_fingerprint = topic_repository.fingerprint(posts, "config")
    add_post(database, 3, " \t\n ", updated_at="changed")

    filtered = topic_repository.corpus()

    assert filtered == posts
    assert (
        topic_repository.fingerprint(
            (*posts, AnalysisPost(id=3, text=" \t\n ", updated_at="changed")), "config"
        )
        == expected_fingerprint
    )


def test_candidate_fingerprint_includes_stop_words_but_model_assignments_do_not(
    topic_repository: TopicAnalysisRepository,
    database: Database,
) -> None:
    for post_id in range(3, 11):
        add_post(database, post_id, "python package typing desktop")
    add_post(database, 11, "the ve bir bu için")
    analyzer = TopicAnalyzer()
    candidates = topic_repository.corpus()

    result = analyzer.analyze(candidates)

    assert result.corpus_fingerprint == topic_repository.fingerprint(
        candidates, analyzer.config_json
    )
    assert 11 not in {assignment.post_id for assignment in result.assignments}


def test_fingerprint_is_order_independent_and_includes_configuration(
    topic_repository: TopicAnalysisRepository,
) -> None:
    posts = topic_repository.corpus()

    expected = topic_repository.fingerprint(posts, "config-a")

    assert topic_repository.fingerprint(tuple(reversed(posts)), "config-a") == expected
    assert topic_repository.fingerprint(posts, "config-b") != expected


def test_begin_persists_a_running_run_and_returns_it(
    topic_repository: TopicAnalysisRepository,
) -> None:
    metadata = TopicAnalysisMetadata("tfidf_nmf", "1", "{}", "abc")

    run = topic_repository.begin(metadata)

    assert run.id > 0
    assert run.state is TopicAnalysisState.RUNNING
    assert run.method == metadata.method
    assert run.method_version == metadata.method_version
    assert run.config_json == metadata.config_json
    assert run.corpus_fingerprint == metadata.corpus_fingerprint
    assert run.completed_at is None
    assert not run.is_active


def test_completed_fingerprint_matches_only_the_active_completed_run(
    topic_repository: TopicAnalysisRepository,
) -> None:
    first_draft = draft("first")
    first_run = topic_repository.begin(first_draft.metadata)
    assert not topic_repository.has_completed_fingerprint("first")
    topic_repository.publish(first_run.id, first_draft)
    assert topic_repository.has_completed_fingerprint("first")
    assert not topic_repository.has_completed_fingerprint("missing")

    second_draft = draft("second")
    second_run = topic_repository.begin(second_draft.metadata)
    assert not topic_repository.has_completed_fingerprint("second")
    topic_repository.publish(second_run.id, second_draft)

    assert topic_repository.has_completed_fingerprint("second")
    assert not topic_repository.has_completed_fingerprint("first")

    failed_draft = draft("failed")
    failed_run = topic_repository.begin(failed_draft.metadata)
    topic_repository.fail(failed_run.id, "expected")
    assert not topic_repository.has_completed_fingerprint("failed")


def test_publish_persists_topics_and_assignments_and_activates_run(
    topic_repository: TopicAnalysisRepository,
    database: Database,
) -> None:
    valid = draft()
    run = topic_repository.begin(valid.metadata)

    published = topic_repository.publish(run.id, valid)

    assert published.state is TopicAnalysisState.COMPLETED
    assert published.completed_at is not None
    assert published.is_active
    assert topic_repository.active_run() == published
    with database.connection() as connection:
        topic = connection.execute("SELECT * FROM detected_topics").fetchone()
        assignment = connection.execute("SELECT * FROM detected_topic_assignments").fetchone()
    assert topic is not None
    assert topic["display_label"] == "python · typing"
    assert assignment is not None
    assert assignment["post_id"] == 1
    assert assignment["rank"] == 1
    assert assignment["normalized_score"] == pytest.approx(1.0)


def test_publish_leaves_legacy_topic_tables_untouched(
    topic_repository: TopicAnalysisRepository,
    database: Database,
) -> None:
    with database.transaction() as connection:
        topic_id = int(connection.execute("INSERT INTO topics(name) VALUES('legacy')").lastrowid)
        connection.execute(
            """INSERT INTO post_topics(post_id,topic_id,confidence,method)
               VALUES(?,?,0.75,'legacy')""",
            (1, topic_id),
        )
    valid = draft()
    run = topic_repository.begin(valid.metadata)

    topic_repository.publish(run.id, valid)

    with database.connection() as connection:
        legacy = connection.execute(
            """SELECT topics.name,post_topics.confidence,post_topics.method
               FROM topics JOIN post_topics ON post_topics.topic_id=topics.id"""
        ).fetchall()
    assert [tuple(row) for row in legacy] == [("legacy", 0.75, "legacy")]


def test_failed_publish_keeps_previous_active_model(
    topic_repository: TopicAnalysisRepository,
    database: Database,
) -> None:
    valid = draft()
    first_run = topic_repository.begin(valid.metadata)
    first = topic_repository.publish(first_run.id, valid)
    broken_draft = TopicModelDraft(
        state=TopicModelState.BUILT,
        config_json=valid.config_json,
        corpus_fingerprint="changed",
        topics=valid.topics,
        assignments=(
            TopicAssignmentDraft(
                post_id=999,
                cluster_index=0,
                rank=1,
                normalized_score=1.0,
            ),
        ),
    )
    broken = topic_repository.begin(broken_draft.metadata)

    with pytest.raises(sqlite3.IntegrityError):
        topic_repository.publish(broken.id, broken_draft)

    assert topic_repository.active_run() == first
    assert topic_repository.by_id(broken.id).state is TopicAnalysisState.RUNNING
    with database.connection() as connection:
        topic_count = connection.execute(
            "SELECT COUNT(*) FROM detected_topics WHERE analysis_run_id=?", (broken.id,)
        ).fetchone()[0]
    assert topic_count == 0


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("config_json", "{}"),
        ("corpus_fingerprint", "changed"),
        ("method", "other_method"),
        ("method_version", "2"),
    ),
)
def test_publish_rejects_mismatched_running_metadata_before_any_writes(
    topic_repository: TopicAnalysisRepository,
    database: Database,
    field: str,
    changed: str,
) -> None:
    valid = draft()
    active_run = topic_repository.begin(valid.metadata)
    active = topic_repository.publish(active_run.id, valid)
    metadata_fields = {
        "method": valid.metadata.method,
        "method_version": valid.metadata.method_version,
        "config_json": valid.metadata.config_json,
        "corpus_fingerprint": valid.metadata.corpus_fingerprint,
    }
    metadata_fields[field] = changed
    mismatched = topic_repository.begin(TopicAnalysisMetadata(**metadata_fields))

    with pytest.raises(ValueError, match="metadata does not match"):
        topic_repository.publish(mismatched.id, valid)

    assert topic_repository.active_run() == active
    assert topic_repository.by_id(mismatched.id).state is TopicAnalysisState.RUNNING
    with database.connection() as connection:
        topic_count = connection.execute(
            "SELECT COUNT(*) FROM detected_topics WHERE analysis_run_id=?", (mismatched.id,)
        ).fetchone()[0]
    assert topic_count == 0


def test_insufficient_run_preserves_previous_active_model_and_is_current_status(
    topic_repository: TopicAnalysisRepository,
) -> None:
    valid = draft()
    completed_run = topic_repository.begin(valid.metadata)
    completed = topic_repository.publish(completed_run.id, valid)
    insufficient_metadata = valid.metadata.with_fingerprint("too-small")
    insufficient_run = topic_repository.begin(insufficient_metadata)

    insufficient = topic_repository.mark_insufficient(insufficient_run.id)

    assert insufficient.state is TopicAnalysisState.INSUFFICIENT
    assert insufficient.completed_at is not None
    assert not insufficient.is_active
    assert topic_repository.active_run() == completed
    assert topic_repository.status() == insufficient


def test_fail_truncates_diagnostic_and_never_changes_active_run(
    topic_repository: TopicAnalysisRepository,
) -> None:
    valid = draft()
    completed_run = topic_repository.begin(valid.metadata)
    completed = topic_repository.publish(completed_run.id, valid)
    failed_run = topic_repository.begin(valid.metadata.with_fingerprint("failed"))

    failed = topic_repository.fail(failed_run.id, "x" * 600)

    assert failed.state is TopicAnalysisState.FAILED
    assert failed.completed_at is not None
    assert failed.diagnostic == "x" * 500
    assert not failed.is_active
    assert topic_repository.active_run() == completed
    assert topic_repository.status() == failed


def test_recover_incomplete_cancels_pending_and_running_without_changing_active(
    topic_repository: TopicAnalysisRepository,
    database: Database,
) -> None:
    valid = draft()
    completed_run = topic_repository.begin(valid.metadata)
    completed = topic_repository.publish(completed_run.id, valid)
    running = topic_repository.begin(valid.metadata.with_fingerprint("running"))
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO topic_analysis_runs(
                   method,method_version,config_json,corpus_fingerprint,state
               ) VALUES('tfidf_nmf','1','{}','pending','pending')"""
        )
        pending_id = int(cursor.lastrowid)

    recovered = topic_repository.recover_incomplete()

    assert [item.id for item in recovered] == [running.id, pending_id]
    assert all(item.state is TopicAnalysisState.CANCELLED for item in recovered)
    assert topic_repository.active_run() == completed
    assert topic_repository.status() == completed


def test_publish_rejects_non_running_run_without_partial_rows(
    topic_repository: TopicAnalysisRepository,
    database: Database,
) -> None:
    valid = draft()
    run = topic_repository.begin(valid.metadata)
    topic_repository.fail(run.id, "cancelled before publish")

    with pytest.raises(ValueError, match="running topic analysis was not found"):
        topic_repository.publish(run.id, valid)

    with database.connection() as connection:
        topic_count = connection.execute(
            "SELECT COUNT(*) FROM detected_topics WHERE analysis_run_id=?", (run.id,)
        ).fetchone()[0]
    assert topic_count == 0


def test_publish_rejects_an_insufficient_draft_without_deactivating_current_model(
    topic_repository: TopicAnalysisRepository,
) -> None:
    valid = draft()
    active_run = topic_repository.begin(valid.metadata)
    active = topic_repository.publish(active_run.id, valid)
    insufficient_draft = TopicModelDraft(
        state=TopicModelState.INSUFFICIENT,
        config_json=valid.config_json,
        corpus_fingerprint="too-small",
        topics=(),
        assignments=(),
    )
    insufficient_run = topic_repository.begin(insufficient_draft.metadata)

    with pytest.raises(ValueError, match="only a built topic model can be published"):
        topic_repository.publish(insufficient_run.id, insufficient_draft)

    assert topic_repository.active_run() == active
    assert topic_repository.by_id(insufficient_run.id).state is TopicAnalysisState.RUNNING
