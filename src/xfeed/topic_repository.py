import hashlib
import sqlite3
from collections.abc import Sequence

from xfeed.db import Database
from xfeed.topic_analysis import (
    AnalysisPost,
    TopicAnalysisMetadata,
    TopicAnalysisRun,
    TopicAnalysisState,
    TopicModelDraft,
    TopicModelState,
)


class TopicAnalysisRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def corpus(self) -> tuple[AnalysisPost, ...]:
        with self._database.connection() as connection:
            rows = connection.execute("SELECT id,text,updated_at FROM posts ORDER BY id").fetchall()
        return tuple(
            AnalysisPost(id=int(row["id"]), text=row["text"], updated_at=row["updated_at"])
            for row in rows
            if row["text"].strip()
        )

    def fingerprint(self, posts: Sequence[AnalysisPost], config_json: str) -> str:
        digest = hashlib.sha256(config_json.encode("utf-8"))
        for post in sorted((post for post in posts if post.text.strip()), key=lambda item: item.id):
            digest.update(f"\n{post.id}\0{post.updated_at}\0{post.text}".encode("utf-8"))
        return digest.hexdigest()

    def begin(self, metadata: TopicAnalysisMetadata) -> TopicAnalysisRun:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO topic_analysis_runs(
                       method,method_version,config_json,corpus_fingerprint,state,is_active
                   ) VALUES(?,?,?,?,?,0)""",
                (
                    metadata.method,
                    metadata.method_version,
                    metadata.config_json,
                    metadata.corpus_fingerprint,
                    TopicAnalysisState.RUNNING.value,
                ),
            )
            row = connection.execute(
                "SELECT * FROM topic_analysis_runs WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._run(row)

    def publish(self, run_id: int, draft: TopicModelDraft) -> TopicAnalysisRun:
        if draft.state is not TopicModelState.BUILT:
            raise ValueError("only a built topic model can be published")
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT method,method_version,config_json,corpus_fingerprint
                   FROM topic_analysis_runs WHERE id=? AND state='running'""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("running topic analysis was not found")
            metadata = draft.metadata
            if (
                row["method"] != metadata.method
                or row["method_version"] != metadata.method_version
                or row["config_json"] != metadata.config_json
                or row["corpus_fingerprint"] != metadata.corpus_fingerprint
            ):
                raise ValueError("topic analysis metadata does not match running run")
            topic_ids: dict[int, int] = {}
            for topic in draft.topics:
                cursor = connection.execute(
                    """INSERT INTO detected_topics(
                           analysis_run_id,cluster_index,display_label,keywords_json
                       ) VALUES(?,?,?,?)""",
                    (
                        run_id,
                        topic.cluster_index,
                        topic.display_label,
                        topic.keywords_json,
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("detected topic insert did not return an id")
                topic_ids[topic.cluster_index] = cursor.lastrowid
            connection.executemany(
                """INSERT INTO detected_topic_assignments(
                       analysis_run_id,post_id,detected_topic_id,rank,normalized_score
                   ) VALUES(?,?,?,?,?)""",
                [
                    (
                        run_id,
                        item.post_id,
                        topic_ids[item.cluster_index],
                        item.rank,
                        item.normalized_score,
                    )
                    for item in draft.assignments
                ],
            )
            connection.execute("UPDATE topic_analysis_runs SET is_active=0 WHERE is_active=1")
            connection.execute(
                """UPDATE topic_analysis_runs
                   SET state='completed',completed_at=CURRENT_TIMESTAMP,
                       diagnostic=NULL,is_active=1 WHERE id=? AND state='running'""",
                (run_id,),
            )
            changes = connection.execute("SELECT changes()").fetchone()
            assert changes is not None
            if changes[0] != 1:
                raise ValueError("running topic analysis was not found")
        return self.by_id(run_id)

    def mark_insufficient(self, run_id: int) -> TopicAnalysisRun:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE topic_analysis_runs
                   SET state='insufficient',completed_at=CURRENT_TIMESTAMP,
                       diagnostic=NULL,is_active=0 WHERE id=? AND state='running'""",
                (run_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("running topic analysis was not found")
        return self.by_id(run_id)

    def fail(self, run_id: int, diagnostic: str) -> TopicAnalysisRun:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE topic_analysis_runs
                   SET state='failed',completed_at=CURRENT_TIMESTAMP,diagnostic=?,is_active=0
                   WHERE id=? AND state IN ('pending','running')""",
                (diagnostic[:500], run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("pending or running topic analysis was not found")
        return self.by_id(run_id)

    def recover_incomplete(self) -> tuple[TopicAnalysisRun, ...]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                """SELECT id FROM topic_analysis_runs
                   WHERE state IN ('pending','running') ORDER BY id"""
            ).fetchall()
            run_ids = tuple(int(row["id"]) for row in rows)
            connection.execute(
                """UPDATE topic_analysis_runs
                   SET state='cancelled',completed_at=CURRENT_TIMESTAMP,is_active=0
                   WHERE state IN ('pending','running')"""
            )
        return tuple(self.by_id(run_id) for run_id in run_ids)

    def has_completed_fingerprint(self, fingerprint: str) -> bool:
        with self._database.connection() as connection:
            row = connection.execute(
                """SELECT 1 FROM topic_analysis_runs
                   WHERE state='completed' AND is_active=1 AND corpus_fingerprint=?
                   LIMIT 1""",
                (fingerprint,),
            ).fetchone()
        return row is not None

    def active_run(self) -> TopicAnalysisRun | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """SELECT * FROM topic_analysis_runs
                   WHERE is_active=1 AND state='completed' ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        return None if row is None else self._run(row)

    def status(self) -> TopicAnalysisRun | None:
        active = self.active_run()
        active_id = 0 if active is None else active.id
        with self._database.connection() as connection:
            row = connection.execute(
                """SELECT * FROM topic_analysis_runs
                   WHERE id>? AND state IN ('pending','running','insufficient','failed')
                   ORDER BY id DESC LIMIT 1""",
                (active_id,),
            ).fetchone()
        return active if row is None else self._run(row)

    def by_id(self, run_id: int) -> TopicAnalysisRun:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM topic_analysis_runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise ValueError("topic analysis run was not found")
        return self._run(row)

    @staticmethod
    def _run(row: sqlite3.Row) -> TopicAnalysisRun:
        return TopicAnalysisRun(
            id=int(row["id"]),
            method=row["method"],
            method_version=row["method_version"],
            config_json=row["config_json"],
            corpus_fingerprint=row["corpus_fingerprint"],
            state=TopicAnalysisState(row["state"]),
            diagnostic=row["diagnostic"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            is_active=bool(row["is_active"]),
        )
