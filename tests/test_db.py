import sqlite3

import pytest
from platformdirs import user_data_path

import xfeed.db as db_module
from xfeed.app import build_database, default_database_path
from xfeed.db import Database


def create_version_three_fixture(database: Database) -> None:
    connection = sqlite3.connect(database.path)
    try:
        connection.executescript(db_module.SCHEMA)
        connection.execute(
            "INSERT INTO sources(id,handle,profile_url) VALUES(?,?,?)",
            (7, "openai", "https://x.com/openai"),
        )
        connection.execute(
            """INSERT INTO posts(
                   id,canonical_url,x_post_id,author_handle,author_name,text,published_at,
                   embed_html,provider_json,source_method,has_media,availability
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                11,
                "https://x.com/openai/status/123",
                "123",
                "openai",
                "OpenAI",
                "A preserved post",
                "2026-07-20 10:00:00",
                "<blockquote></blockquote>",
                "{}",
                "oembed",
                0,
                "available",
            ),
        )
        connection.execute(
            """INSERT INTO collection_runs(
                   id,source_id,started_at,finished_at,requested_max,candidate_count,
                   saved_count,duplicate_count,failed_count,status,reason,diagnostic
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                13,
                7,
                "2026-07-20 10:01:00",
                "2026-07-20 10:02:00",
                30,
                1,
                1,
                0,
                0,
                "completed",
                "limit",
                None,
            ),
        )
        connection.execute(
            """INSERT INTO post_observations(run_id,post_id,observed_at)
               VALUES(?,?,?)""",
            (13, 11, "2026-07-20 10:01:30"),
        )
        connection.execute("INSERT INTO manual_ingestions(post_id) VALUES(?)", (11,))
        connection.commit()
    finally:
        connection.close()


def create_version_five_fixture(database: Database) -> None:
    create_version_three_fixture(database)
    connection = database.connect()
    try:
        Database._migrate_3_to_4(connection)
        Database._migrate_4_to_5(connection)
        connection.execute(
            """INSERT INTO collection_runs(
                   target_kind,source_id,requested_max,status
               ) VALUES('for_you',NULL,10,'completed')"""
        )
        connection.execute(
            """INSERT INTO collection_runs(
                   target_kind,source_id,requested_max,status
               ) VALUES('for_you',NULL,20,'completed')"""
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def v5_database(tmp_path) -> Database:
    database = Database(tmp_path / "feed.sqlite3")
    create_version_five_fixture(database)
    return database


def test_migrate_is_idempotent_and_keeps_schema_at_version_six(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")

    database.migrate()
    database.migrate()

    with database.connection() as connection:
        versions = connection.execute("SELECT version FROM schema_version").fetchall()
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert [row["version"] for row in versions] == [6]
    assert {
        "sources",
        "refresh_events",
        "posts",
        "label_events",
        "topics",
        "post_topics",
        "manual_tags",
        "post_manual_tags",
        "model_versions",
        "training_examples",
        "predictions",
        "evaluation_runs",
        "collection_runs",
        "post_observations",
        "manual_ingestions",
        "media_resolution_jobs",
        "post_media",
        "app_sessions",
        "topic_analysis_runs",
        "detected_topics",
        "detected_topic_assignments",
    } <= tables


def test_v5_migration_backfills_one_synthetic_session_per_for_you_run(
    v5_database: Database,
) -> None:
    v5_database.migrate()

    with v5_database.connection() as connection:
        rows = connection.execute(
            """SELECT s.synthetic, s.state, r.target_kind, r.session_id
               FROM collection_runs r JOIN app_sessions s ON s.id=r.session_id
               WHERE r.target_kind='for_you' ORDER BY r.id"""
        ).fetchall()

    assert [(row["synthetic"], row["state"]) for row in rows] == [
        (1, "closed"),
        (1, "closed"),
    ]
    assert len({row["session_id"] for row in rows}) == 2


def test_failed_version_five_migration_rolls_back_and_restores_foreign_keys(
    tmp_path, monkeypatch
) -> None:
    database = Database(tmp_path / "feed.sqlite3")
    create_version_five_fixture(database)
    broken_migration = db_module.MIGRATION_5_TO_6.replace(
        "CREATE TABLE app_sessions(",
        "THIS IS INVALID SQL;\nCREATE TABLE app_sessions(",
    )
    monkeypatch.setattr(db_module, "MIGRATION_5_TO_6", broken_migration)
    connection = database.connect()
    try:
        with pytest.raises(sqlite3.Error):
            Database._migrate_5_to_6(connection)

        version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        app_sessions = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_sessions'"
        ).fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        connection.close()

    assert version == 5
    assert app_sessions is None
    assert foreign_keys == 1


def test_migrate_upgrades_version_two_atomically_with_manual_provenance(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    connection = sqlite3.connect(database.path)
    try:
        connection.executescript(
            "CREATE TABLE schema_version(version INTEGER NOT NULL);"
            "INSERT INTO schema_version(version) VALUES(2);"
        )
    finally:
        connection.close()

    database.migrate()

    with database.connection() as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        manual_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='manual_ingestions'"
        ).fetchone()
    assert version == 6
    assert manual_table is not None


def test_migrate_upgrades_version_three_collection_rows_without_data_loss(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    create_version_three_fixture(database)

    database.migrate()

    with database.connection() as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        run = connection.execute("SELECT * FROM collection_runs WHERE id=13").fetchone()
        observation = connection.execute(
            "SELECT * FROM post_observations WHERE run_id=13 AND post_id=11"
        ).fetchone()

    assert run is not None
    assert observation is not None
    assert version == 6
    assert dict(run) | {} == {
        **dict(run),
        "target_kind": "source",
    }
    assert run["source_id"] == 7
    assert run["status"] == "completed"
    assert observation["discovery_order"] == 0
    assert observation["observation_kind"] == "post"
    assert observation["parent_post_id"] is None
    assert observation["observed_at"] == "2026-07-20 10:01:30"


def test_migrate_upgrades_populated_version_four_fixture_with_media_schema(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    create_version_three_fixture(database)
    connection = database.connect()
    try:
        Database._migrate_3_to_4(connection)
    finally:
        connection.close()

    database.migrate()

    with database.connection() as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        post = connection.execute("SELECT * FROM posts WHERE id=11").fetchone()
        manual = connection.execute(
            "SELECT post_id FROM manual_ingestions WHERE post_id=11"
        ).fetchone()
        run = connection.execute("SELECT * FROM collection_runs WHERE id=13").fetchone()
        observation = connection.execute(
            "SELECT * FROM post_observations WHERE run_id=13 AND post_id=11"
        ).fetchone()
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        job_fk = connection.execute("PRAGMA foreign_key_list(media_resolution_jobs)").fetchone()
        asset_fk = connection.execute("PRAGMA foreign_key_list(post_media)").fetchone()
        job_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='media_resolution_jobs'"
        ).fetchone()[0]
        asset_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='post_media'"
        ).fetchone()[0]

    assert version == 6
    assert {"media_resolution_jobs", "post_media"} <= tables
    assert job_fk[2:5] == ("posts", "post_id", "id")
    assert asset_fk[2:5] == ("posts", "post_id", "id")
    assert "CHECK(state IN ('pending','resolving','resolved','failed'))" in job_sql
    assert "CHECK(state IN ('pending','downloading','available','failed'))" in asset_sql
    assert "UNIQUE(post_id, position)" in asset_sql
    assert post is not None and post["canonical_url"] == "https://x.com/openai/status/123"
    assert manual is not None and manual["post_id"] == 11
    assert run is not None and run["target_kind"] == "source"
    assert observation is not None and observation["discovery_order"] == 0


def test_failed_version_three_migration_keeps_original_tables_rows_and_version(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "feed.sqlite3")
    create_version_three_fixture(database)
    broken_migration = db_module.MIGRATION_3_TO_4.replace(
        "CREATE TABLE post_observations_new(",
        "THIS IS INVALID SQL;\nCREATE TABLE post_observations_new(",
    )
    assert broken_migration != db_module.MIGRATION_3_TO_4
    monkeypatch.setattr(db_module, "MIGRATION_3_TO_4", broken_migration)

    with pytest.raises(sqlite3.Error):
        database.migrate()

    with database.connection() as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        run = connection.execute("SELECT * FROM collection_runs WHERE id=13").fetchone()
        observation = connection.execute(
            "SELECT * FROM post_observations WHERE run_id=13 AND post_id=11"
        ).fetchone()
        run_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(collection_runs)")
        }
        observation_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(post_observations)")
        }

    assert version == 3
    assert {"collection_runs", "post_observations"} <= tables
    assert not any(name.endswith(("_new", "_old")) for name in tables)
    assert run is not None and dict(run)["source_id"] == 7
    assert observation is not None and dict(observation) == {
        "run_id": 13,
        "post_id": 11,
        "observed_at": "2026-07-20 10:01:30",
    }
    assert "target_kind" not in run_columns
    assert "discovery_order" not in observation_columns


def test_failed_version_four_migration_rolls_back_media_tables_and_restores_foreign_keys(
    tmp_path, monkeypatch
) -> None:
    database = Database(tmp_path / "feed.sqlite3")
    create_version_three_fixture(database)
    connection = database.connect()
    try:
        Database._migrate_3_to_4(connection)
        broken_migration = db_module.MIGRATION_4_TO_5.replace(
            "CREATE TABLE post_media(",
            "THIS IS INVALID SQL;\nCREATE TABLE post_media(",
        )
        assert broken_migration != db_module.MIGRATION_4_TO_5
        monkeypatch.setattr(db_module, "MIGRATION_4_TO_5", broken_migration)

        with pytest.raises(sqlite3.Error):
            Database._migrate_4_to_5(connection)

        version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        connection.close()

    assert version == 4
    assert "media_resolution_jobs" not in tables
    assert "post_media" not in tables
    assert foreign_keys == 1


def test_migrate_rejects_database_newer_than_application_without_mutating_it(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    connection = sqlite3.connect(database.path)
    try:
        connection.executescript(
            "CREATE TABLE schema_version(version INTEGER NOT NULL);"
            "INSERT INTO schema_version(version) VALUES(99);"
        )
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="99"):
        database.migrate()

    connection = sqlite3.connect(database.path)
    try:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 99
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='posts'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


def test_failed_migration_rolls_back_schema_and_version(tmp_path, monkeypatch):
    database = Database(tmp_path / "feed.sqlite3")
    broken_schema = db_module.SCHEMA.replace(
        "CREATE TABLE IF NOT EXISTS refresh_events(",
        """
        CREATE TABLE partial_table(id INTEGER PRIMARY KEY);
        THIS IS INVALID SQL;
        CREATE TABLE IF NOT EXISTS refresh_events(
        """,
    )
    assert broken_schema != db_module.SCHEMA
    monkeypatch.setattr(
        db_module,
        "SCHEMA",
        broken_schema,
    )

    with pytest.raises(sqlite3.Error):
        database.migrate()

    connection = sqlite3.connect(database.path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        versions = (
            connection.execute("SELECT version FROM schema_version").fetchall()
            if "schema_version" in tables
            else []
        )
    finally:
        connection.close()

    assert "partial_table" not in tables
    assert versions == []


def test_transaction_commits_successful_work_and_rolls_back_failures(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()

    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO manual_tags(name) VALUES(?)",
            ("committed",),
        )

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO manual_tags(name) VALUES(?)",
                ("rolled-back",),
            )
            connection.execute(
                "INSERT INTO manual_tags(name) VALUES(?)",
                ("committed",),
            )

    with database.connection() as connection:
        names = [
            row["name"]
            for row in connection.execute("SELECT name FROM manual_tags ORDER BY id").fetchall()
        ]

    assert names == ["committed"]


def test_connection_context_closes_the_connection(tmp_path):
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()

    with database.connection() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_build_database_migrates_the_requested_path(tmp_path):
    path = tmp_path / "nested" / "feed.sqlite3"

    database = build_database(path)

    assert database.path == path
    with database.connection() as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
    assert version is not None
    assert version["version"] == 6


def test_default_database_path_uses_the_application_data_directory():
    assert default_database_path() == (
        user_data_path("XDesktopFeed", appauthor=False) / "feed.sqlite3"
    )
