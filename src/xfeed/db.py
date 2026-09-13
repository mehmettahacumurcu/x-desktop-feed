import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = 6


class UnsupportedSchemaVersion(RuntimeError):
    """The database was created by a newer application schema."""


SCHEMA = """
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY, handle TEXT NOT NULL COLLATE NOCASE UNIQUE, profile_url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_refresh_at TEXT
);
CREATE TABLE IF NOT EXISTS refresh_events(
  id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
  started_at TEXT NOT NULL, finished_at TEXT, result TEXT NOT NULL, diagnostic TEXT
);
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE, x_post_id TEXT NOT NULL,
  author_handle TEXT, author_name TEXT, text TEXT NOT NULL, published_at TEXT,
  embed_html TEXT NOT NULL, provider_json TEXT NOT NULL, source_method TEXT NOT NULL,
  has_media INTEGER NOT NULL DEFAULT 0,
  availability TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS label_events(
  id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id),
  label TEXT NOT NULL, origin TEXT NOT NULL, reason TEXT,
  supersedes_id INTEGER REFERENCES label_events(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS topics(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, muted_at TEXT
);
CREATE TABLE IF NOT EXISTS post_topics(
  post_id INTEGER NOT NULL REFERENCES posts(id), topic_id INTEGER NOT NULL REFERENCES topics(id),
  confidence REAL NOT NULL, method TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(post_id, topic_id, method)
);
CREATE TABLE IF NOT EXISTS manual_tags(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS post_manual_tags(
  post_id INTEGER NOT NULL REFERENCES posts(id), tag_id INTEGER NOT NULL REFERENCES manual_tags(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(post_id, tag_id)
);
CREATE TABLE IF NOT EXISTS model_versions(
  id INTEGER PRIMARY KEY, strategy TEXT NOT NULL, version TEXT NOT NULL, config_json TEXT NOT NULL,
  artifact_path TEXT NOT NULL, dataset_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  is_active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS training_examples(
  model_version_id INTEGER NOT NULL REFERENCES model_versions(id), post_id INTEGER NOT NULL REFERENCES posts(id),
  label_event_id INTEGER NOT NULL REFERENCES label_events(id), PRIMARY KEY(model_version_id, post_id)
);
CREATE TABLE IF NOT EXISTS predictions(
  id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id),
  model_version_id INTEGER NOT NULL REFERENCES model_versions(id), predicted_label TEXT NOT NULL,
  unwanted_probability REAL NOT NULL, explanation_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(post_id, model_version_id)
);
CREATE TABLE IF NOT EXISTS evaluation_runs(
  id INTEGER PRIMARY KEY, model_version_id INTEGER NOT NULL REFERENCES model_versions(id),
  method TEXT NOT NULL, sample_count INTEGER NOT NULL, class_balance_json TEXT NOT NULL,
  split_seed INTEGER NOT NULL, metrics_json TEXT NOT NULL, confusion_matrix_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS collection_runs(
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  requested_max INTEGER NOT NULL,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  saved_count INTEGER NOT NULL DEFAULT 0,
  duplicate_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  reason TEXT,
  diagnostic TEXT
);
CREATE TABLE IF NOT EXISTS post_observations(
  run_id INTEGER NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(run_id, post_id)
);
CREATE TABLE IF NOT EXISTS manual_ingestions(
  post_id INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
  recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version(version)
SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
UPDATE schema_version SET version=3;
COMMIT;
"""


MIGRATION_3_TO_4 = """
BEGIN IMMEDIATE;
ALTER TABLE collection_runs RENAME TO collection_runs_old;
ALTER TABLE post_observations RENAME TO post_observations_old;

CREATE TABLE collection_runs_new(
  id INTEGER PRIMARY KEY,
  target_kind TEXT NOT NULL CHECK(target_kind IN ('source','for_you')),
  source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  requested_max INTEGER NOT NULL,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  saved_count INTEGER NOT NULL DEFAULT 0,
  duplicate_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  reason TEXT,
  diagnostic TEXT,
  CHECK(
    (target_kind='source' AND source_id IS NOT NULL) OR
    (target_kind='for_you' AND source_id IS NULL)
  )
);

CREATE TABLE post_observations_new(
  run_id INTEGER NOT NULL REFERENCES collection_runs_new(id) ON DELETE CASCADE,
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  discovery_order INTEGER NOT NULL,
  observation_kind TEXT NOT NULL CHECK(observation_kind IN ('post','repost','reply','quote')),
  parent_post_id INTEGER REFERENCES posts(id) ON DELETE SET NULL,
  observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(run_id, post_id)
);

INSERT INTO collection_runs_new(
  id,target_kind,source_id,started_at,finished_at,requested_max,candidate_count,
  saved_count,duplicate_count,failed_count,status,reason,diagnostic
)
SELECT
  id,'source',source_id,started_at,finished_at,requested_max,candidate_count,
  saved_count,duplicate_count,failed_count,status,reason,diagnostic
FROM collection_runs_old;

INSERT INTO post_observations_new(
  run_id,post_id,discovery_order,observation_kind,parent_post_id,observed_at
)
SELECT run_id,post_id,0,'post',NULL,observed_at FROM post_observations_old;

DROP TABLE post_observations_old;
DROP TABLE collection_runs_old;
ALTER TABLE collection_runs_new RENAME TO collection_runs;
ALTER TABLE post_observations_new RENAME TO post_observations;
UPDATE schema_version SET version=4;
COMMIT;
"""


MIGRATION_4_TO_5 = """
BEGIN IMMEDIATE;
CREATE TABLE media_resolution_jobs(
  id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL UNIQUE REFERENCES posts(id) ON DELETE CASCADE,
  canonical_url TEXT NOT NULL,
  origin TEXT NOT NULL CHECK(origin IN ('collection','manual')),
  state TEXT NOT NULL CHECK(state IN ('pending','resolving','resolved','failed')),
  manifest_count INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  diagnostic TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK(manifest_count IS NULL OR manifest_count BETWEEN 0 AND 4)
);
CREATE TABLE post_media(
  id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 3),
  source_url TEXT NOT NULL,
  alt_text TEXT,
  state TEXT NOT NULL CHECK(state IN ('pending','downloading','available','failed')),
  local_path TEXT,
  mime_type TEXT,
  width INTEGER,
  height INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  diagnostic TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(post_id, position),
  UNIQUE(post_id, source_url)
);
UPDATE schema_version SET version=5;
COMMIT;
"""


MIGRATION_5_TO_6 = """
BEGIN IMMEDIATE;
ALTER TABLE collection_runs RENAME TO collection_runs_old;
ALTER TABLE post_observations RENAME TO post_observations_old;

CREATE TABLE app_sessions(
  id INTEGER PRIMARY KEY,
  opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  closed_at TEXT,
  state TEXT NOT NULL CHECK(state IN ('open','closed','interrupted')),
  app_version TEXT NOT NULL,
  synthetic INTEGER NOT NULL DEFAULT 0 CHECK(synthetic IN (0,1))
);

CREATE TEMP TABLE legacy_run_sessions(
  legacy_run_id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL
);
INSERT INTO app_sessions(opened_at,closed_at,state,app_version,synthetic)
SELECT started_at,COALESCE(finished_at,started_at),'closed','legacy',1
FROM collection_runs_old WHERE target_kind='for_you' ORDER BY id;
INSERT INTO legacy_run_sessions(legacy_run_id,session_id)
SELECT legacy_runs.id,sessions.id
FROM (
  SELECT id,ROW_NUMBER() OVER (ORDER BY id) AS row_number
  FROM collection_runs_old WHERE target_kind='for_you'
) AS legacy_runs
JOIN (
  SELECT id,ROW_NUMBER() OVER (ORDER BY id) AS row_number
  FROM app_sessions
) AS sessions ON sessions.row_number=legacy_runs.row_number;

CREATE TABLE collection_runs_new(
  id INTEGER PRIMARY KEY,
  target_kind TEXT NOT NULL CHECK(target_kind IN ('source','for_you','following')),
  source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
  session_id INTEGER REFERENCES app_sessions(id) ON DELETE SET NULL,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  requested_max INTEGER NOT NULL,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  saved_count INTEGER NOT NULL DEFAULT 0,
  duplicate_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  reason TEXT,
  diagnostic TEXT,
  CHECK((target_kind='source' AND source_id IS NOT NULL) OR
        (target_kind IN ('for_you','following') AND source_id IS NULL))
);

CREATE TABLE post_observations_new(
  run_id INTEGER NOT NULL REFERENCES collection_runs_new(id) ON DELETE CASCADE,
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  discovery_order INTEGER NOT NULL,
  observation_kind TEXT NOT NULL CHECK(observation_kind IN ('post','repost','reply','quote')),
  parent_post_id INTEGER REFERENCES posts(id) ON DELETE SET NULL,
  observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(run_id, post_id)
);

INSERT INTO collection_runs_new(
  id,target_kind,source_id,session_id,started_at,finished_at,requested_max,candidate_count,
  saved_count,duplicate_count,failed_count,status,reason,diagnostic
)
SELECT
  old.id,old.target_kind,old.source_id,mapping.session_id,old.started_at,old.finished_at,
  old.requested_max,old.candidate_count,old.saved_count,old.duplicate_count,old.failed_count,
  old.status,old.reason,old.diagnostic
FROM collection_runs_old AS old
LEFT JOIN legacy_run_sessions AS mapping ON mapping.legacy_run_id=old.id;

INSERT INTO post_observations_new(
  run_id,post_id,discovery_order,observation_kind,parent_post_id,observed_at
)
SELECT run_id,post_id,discovery_order,observation_kind,parent_post_id,observed_at
FROM post_observations_old;

DROP TABLE post_observations_old;
DROP TABLE collection_runs_old;
DROP TABLE legacy_run_sessions;
ALTER TABLE collection_runs_new RENAME TO collection_runs;
ALTER TABLE post_observations_new RENAME TO post_observations;

CREATE TABLE topic_analysis_runs(
  id INTEGER PRIMARY KEY,
  method TEXT NOT NULL,
  method_version TEXT NOT NULL,
  config_json TEXT NOT NULL,
  corpus_fingerprint TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','running','completed','insufficient','failed','cancelled')),
  diagnostic TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1))
);
CREATE UNIQUE INDEX one_active_topic_analysis
ON topic_analysis_runs(is_active) WHERE is_active=1;

CREATE TABLE detected_topics(
  id INTEGER PRIMARY KEY,
  analysis_run_id INTEGER NOT NULL REFERENCES topic_analysis_runs(id) ON DELETE CASCADE,
  cluster_index INTEGER NOT NULL,
  display_label TEXT NOT NULL,
  keywords_json TEXT NOT NULL,
  UNIQUE(analysis_run_id, cluster_index)
);

CREATE TABLE detected_topic_assignments(
  analysis_run_id INTEGER NOT NULL REFERENCES topic_analysis_runs(id) ON DELETE CASCADE,
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  detected_topic_id INTEGER NOT NULL REFERENCES detected_topics(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 3),
  normalized_score REAL NOT NULL CHECK(normalized_score BETWEEN 0.0 AND 1.0),
  PRIMARY KEY(analysis_run_id, post_id, rank),
  UNIQUE(analysis_run_id, post_id, detected_topic_id)
);
UPDATE schema_version SET version=6;
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            current_version = self._current_schema_version(connection)
            if current_version is not None and current_version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(
                    f"Database schema version {current_version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            if current_version is None or current_version < 4:
                connection.executescript(SCHEMA)
                self._migrate_3_to_4(connection)
            if current_version is None or current_version < 5:
                self._migrate_4_to_5(connection)
            if current_version is None or current_version < 6:
                self._migrate_5_to_6(connection)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _migrate_3_to_4(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(MIGRATION_3_TO_4)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrate_4_to_5(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(MIGRATION_4_TO_5)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrate_5_to_6(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(MIGRATION_5_TO_6)
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError("foreign key check failed during v5-to-v6 migration")
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _current_schema_version(connection: sqlite3.Connection) -> int | None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if table is None:
            return None
        row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return None if row is None or row[0] is None else int(row[0])

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
