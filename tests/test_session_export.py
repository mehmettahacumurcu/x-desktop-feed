import csv
import json

import pytest

from xfeed.db import Database
from xfeed.domain import (
    Availability,
    CollectionStatus,
    CollectionTarget,
    PostDraft,
)
from xfeed.repositories import CollectionRepository, PostRepository, SourceRepository
from xfeed.session_export import SessionExportRepository, export_posts
from xfeed.session_repository import AppSessionRepository
from xfeed.sources import canonicalize_profile


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    return database


@pytest.fixture
def sessions(database: Database) -> AppSessionRepository:
    return AppSessionRepository(database)


@pytest.fixture
def collections(database: Database) -> CollectionRepository:
    return CollectionRepository(database)


@pytest.fixture
def exports(database: Database, sessions: AppSessionRepository) -> SessionExportRepository:
    return SessionExportRepository(database, sessions)


def make_post(handle: str, post_id: str, text: str = "post text") -> PostDraft:
    return PostDraft(
        canonical_url=f"https://x.com/{handle}/status/{post_id}",
        x_post_id=post_id,
        author_handle=handle,
        author_name=handle.title(),
        text=text,
        published_at="2026-02-01",
        embed_html="",
        provider_json="{}",
        source_method="fixture",
        availability=Availability.AVAILABLE,
    )


def open_session_at(database: Database, opened_at: str, closed_at: str | None) -> int:
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO app_sessions(opened_at,closed_at,state,app_version,synthetic)
               VALUES(?,?,'closed','0.1.0',0)""",
            (opened_at, closed_at),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def record_manual_at(database: Database, post_id: int, recorded_at: str) -> None:
    with database.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO manual_ingestions(post_id,recorded_at) VALUES(?,?)",
            (post_id, recorded_at),
        )


def test_exportable_hides_empty_session(
    exports: SessionExportRepository, sessions: AppSessionRepository
) -> None:
    sessions.begin("0.1.0")

    assert exports.exportable_sessions() == ()


def test_exportable_includes_source_and_home_sessions(
    exports: SessionExportRepository,
    sessions: AppSessionRepository,
    collections: CollectionRepository,
    database: Database,
) -> None:
    source = SourceRepository(database).add(canonicalize_profile("openai"))
    posts = PostRepository(database)
    first = sessions.begin("0.1.0")
    source_run = collections.start(
        CollectionTarget.for_source(source.id, source.handle, source.profile_url),
        session_id=first.id,
    )
    posts_saved = posts.insert(make_post("openai", "1"))
    collections.observe(source_run.id, posts_saved.id)
    collections.finish_if_running(
        source_run.id,
        status=CollectionStatus.COMPLETED,
        candidate_count=1,
        saved_count=1,
        duplicate_count=0,
        failed_count=0,
        reason=None,
        diagnostic=None,
    )
    second = sessions.begin("0.1.0")
    home_run = collections.start(CollectionTarget.for_you(), session_id=second.id)
    home_saved = posts.insert(make_post("nasa", "2"))
    collections.observe(home_run.id, home_saved.id)

    listed = exports.exportable_sessions()

    assert [entry.session.id for entry in listed] == [second.id, first.id]
    assert [entry.post_count for entry in listed] == [1, 1]


def test_exportable_includes_manual_only_session_by_time_window(
    exports: SessionExportRepository, database: Database
) -> None:
    session_id = open_session_at(database, "2026-03-01 10:00:00", "2026-03-01 11:00:00")
    saved = PostRepository(database).insert(make_post("manual", "9"))
    record_manual_at(database, saved.id, "2026-03-01 10:30:00")

    listed = exports.exportable_sessions()

    assert [entry.session.id for entry in listed] == [session_id]
    assert listed[0].post_count == 1


def test_collect_posts_dedupes_shared_post_and_merges_manual(
    exports: SessionExportRepository,
    sessions: AppSessionRepository,
    collections: CollectionRepository,
    database: Database,
) -> None:
    posts = PostRepository(database)
    shared = posts.insert(make_post("shared", "5", "shared text"))
    only_first = posts.insert(make_post("alpha", "6", "first text"))
    manual = posts.insert(make_post("handmade", "7", "manual text"))
    first = sessions.begin("0.1.0")
    run_one = collections.start(CollectionTarget.for_you(), session_id=first.id)
    collections.observe(run_one.id, shared.id)
    collections.observe(run_one.id, only_first.id)
    collections.finish_if_running(
        run_one.id,
        status=CollectionStatus.COMPLETED,
        candidate_count=2,
        saved_count=2,
        duplicate_count=0,
        failed_count=0,
        reason=None,
        diagnostic=None,
    )
    second = sessions.begin("0.1.0")
    run_two = collections.start(CollectionTarget.following(), session_id=second.id)
    collections.observe(run_two.id, shared.id)
    record_manual_at(database, manual.id, second.opened_at)

    collected = exports.collect_posts([first.id, second.id])

    assert [(post.url, post.author, post.text, post.date) for post in collected] == [
        ("https://x.com/alpha/status/6", "alpha", "first text", "2026-02-01"),
        ("https://x.com/handmade/status/7", "handmade", "manual text", "2026-02-01"),
        ("https://x.com/shared/status/5", "shared", "shared text", "2026-02-01"),
    ]


def test_collect_posts_excludes_manual_outside_selected_windows(
    exports: SessionExportRepository,
    sessions: AppSessionRepository,
    collections: CollectionRepository,
    database: Database,
) -> None:
    posts = PostRepository(database)
    kept = posts.insert(make_post("kept", "11"))
    dropped = posts.insert(make_post("dropped", "12"))
    first_id = open_session_at(database, "2026-01-10 10:00:00", "2026-01-10 11:00:00")
    second_id = open_session_at(database, "2026-02-10 10:00:00", "2026-02-10 11:00:00")
    run = collections.start(CollectionTarget.for_you(), session_id=first_id)
    collections.observe(run.id, kept.id)
    record_manual_at(database, dropped.id, "2026-03-01 10:00:00")

    collected = exports.collect_posts([first_id, second_id])

    assert [post.url for post in collected] == ["https://x.com/kept/status/11"]


def test_collect_posts_rejects_bad_selection(exports: SessionExportRepository) -> None:
    with pytest.raises(ValueError):
        exports.collect_posts([])
    with pytest.raises(ValueError):
        exports.collect_posts([0])


def test_csv_and_json_writers_round_trip(tmp_path, database: Database) -> None:
    posts = PostRepository(database).insert(make_post("writer", "21", "hello, world"))
    manual_session = open_session_at(database, "2026-04-01 10:00:00", "2026-04-01 11:00:00")
    record_manual_at(database, posts.id, "2026-04-01 10:05:00")
    collected = SessionExportRepository(database).collect_posts([manual_session])

    csv_path = tmp_path / "export.csv"
    json_path = tmp_path / "export.json"
    export_posts(collected, csv_path, "csv")
    export_posts(collected, json_path, ".JSON")

    with csv_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["url", "author", "text", "date"]
    assert rows[1] == [
        "https://x.com/writer/status/21",
        "writer",
        "hello, world",
        "2026-02-01",
    ]
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload == [
        {
            "url": "https://x.com/writer/status/21",
            "author": "writer",
            "text": "hello, world",
            "date": "2026-02-01",
        }
    ]

    with pytest.raises(ValueError, match="csv"):
        export_posts(collected, tmp_path / "export.txt", "txt")
