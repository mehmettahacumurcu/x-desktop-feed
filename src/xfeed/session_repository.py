import sqlite3

from xfeed.db import Database
from xfeed.domain import AppSession, AppSessionState


class AppSessionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def begin(self, app_version: str) -> AppSession:
        if not app_version.strip():
            raise ValueError("app_version is required")
        with self._database.transaction() as connection:
            connection.execute(
                """UPDATE app_sessions
                   SET state='interrupted', closed_at=CURRENT_TIMESTAMP
                   WHERE state='open'"""
            )
            cursor = connection.execute(
                "INSERT INTO app_sessions(state, app_version, synthetic) VALUES('open', ?, 0)",
                (app_version,),
            )
            row = connection.execute(
                "SELECT * FROM app_sessions WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._session(row)

    def close(self, session_id: int) -> AppSession:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE app_sessions SET state='closed', closed_at=CURRENT_TIMESTAMP
                   WHERE id=? AND state='open'""",
                (session_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("open app session was not found")
            row = connection.execute(
                "SELECT * FROM app_sessions WHERE id=?", (session_id,)
            ).fetchone()
        assert row is not None
        return self._session(row)

    def visible(self) -> tuple[AppSession, ...]:
        with self._database.connection() as connection:
            rows = connection.execute(
                """SELECT app_sessions.* FROM app_sessions
                   WHERE EXISTS(
                       SELECT 1 FROM collection_runs
                       JOIN post_observations ON post_observations.run_id=collection_runs.id
                       WHERE collection_runs.session_id=app_sessions.id
                         AND collection_runs.target_kind IN ('for_you', 'following')
                   )
                   ORDER BY app_sessions.opened_at DESC, app_sessions.id DESC"""
            ).fetchall()
        return tuple(self._session(row) for row in rows)

    def by_id(self, session_id: int) -> AppSession | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM app_sessions WHERE id=?", (session_id,)
            ).fetchone()
        return None if row is None else self._session(row)

    @staticmethod
    def _session(row: sqlite3.Row) -> AppSession:
        return AppSession(
            id=int(row["id"]),
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
            state=AppSessionState(row["state"]),
            app_version=row["app_version"],
            synthetic=bool(row["synthetic"]),
        )
