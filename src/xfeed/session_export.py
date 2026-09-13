"""Session-scoped dataset export.

Collects the unique posts attributable to a selection of application
sessions and serializes them to minimal CSV or JSON. Collection posts are
linked through ``collection_runs.session_id``; manual saves carry only a
``recorded_at`` timestamp, so they are attributed by time overlap with the
selected sessions' ``[opened_at, closed_at]`` windows (an open session
matches everything since ``opened_at``).
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from xfeed.db import Database
from xfeed.domain import AppSession
from xfeed.session_repository import AppSessionRepository


@dataclass(frozen=True)
class ExportPost:
    url: str
    author: str
    text: str
    date: str


@dataclass(frozen=True)
class ExportableSession:
    session: AppSession
    post_count: int


def _session_window_predicate(alias: str = "s") -> str:
    return (
        f"m.recorded_at >= {alias}.opened_at "
        f"AND ({alias}.closed_at IS NULL OR m.recorded_at <= {alias}.closed_at)"
    )


class SessionExportRepository:
    """Queries backing the Export tab. No schema changes required."""

    def __init__(
        self,
        database: Database,
        sessions: AppSessionRepository | None = None,
    ) -> None:
        self._database = database
        self._sessions = sessions or AppSessionRepository(database)

    def exportable_sessions(self) -> tuple[ExportableSession, ...]:
        """Sessions holding collected posts from any origin, newest first.

        A session qualifies when it owns collection observations (any
        ``target_kind``) or when manual saves fall inside its time window.
        Empty sessions are hidden.
        """
        with self._database.connection() as connection:
            rows = connection.execute(
                f"""SELECT s.* FROM app_sessions AS s
                    WHERE EXISTS(
                        SELECT 1 FROM collection_runs AS r
                        JOIN post_observations AS o ON o.run_id=r.id
                        WHERE r.session_id=s.id
                    ) OR EXISTS(
                        SELECT 1 FROM manual_ingestions AS m
                        WHERE {_session_window_predicate("s")}
                    )
                    ORDER BY s.opened_at DESC, s.id DESC"""
            ).fetchall()
        result: list[ExportableSession] = []
        for row in rows:
            session = AppSessionRepository._session(row)
            result.append(ExportableSession(session, self.count_for_session(session.id)))
        return tuple(result)

    def count_for_session(self, session_id: int) -> int:
        """Unique collected posts attributable to one session."""
        if type(session_id) is not int or session_id < 1:
            raise ValueError("session_id must be a positive integer")
        with self._database.connection() as connection:
            row = connection.execute(
                f"""SELECT COUNT(*) AS total FROM (
                        SELECT p.canonical_url FROM post_observations AS o
                        JOIN collection_runs AS r ON r.id=o.run_id
                        JOIN posts AS p ON p.id=o.post_id
                        WHERE r.session_id=?
                    UNION
                        SELECT p.canonical_url FROM manual_ingestions AS m
                        JOIN posts AS p ON p.id=m.post_id
                        WHERE EXISTS(
                            SELECT 1 FROM app_sessions AS s
                            WHERE s.id=? AND {_session_window_predicate("s")}
                        )
                    )""",
                (session_id, session_id),
            ).fetchone()
        assert row is not None
        return int(row["total"])

    def collect_posts(self, session_ids: tuple[int, ...] | list[int]) -> list[ExportPost]:
        """Unique posts for the selected sessions, ordered deterministically."""
        ids = tuple(dict.fromkeys(session_ids))
        if not ids or any(type(value) is not int or value < 1 for value in ids):
            raise ValueError("session_ids must be non-empty positive integers")
        placeholders = ",".join("?" for _ in ids)
        with self._database.connection() as connection:
            rows = connection.execute(
                f"""SELECT p.canonical_url, p.author_handle, p.author_name,
                           p.text, p.published_at
                    FROM posts AS p
                    WHERE EXISTS(
                        SELECT 1 FROM post_observations AS o
                        JOIN collection_runs AS r ON r.id=o.run_id
                        WHERE o.post_id=p.id AND r.session_id IN ({placeholders})
                    ) OR EXISTS(
                        SELECT 1 FROM manual_ingestions AS m
                        JOIN app_sessions AS s ON s.id IN ({placeholders})
                        WHERE m.post_id=p.id AND {_session_window_predicate("s")}
                    )
                    GROUP BY p.canonical_url
                    ORDER BY p.published_at IS NULL, p.published_at, p.canonical_url""",
                (*ids, *ids),
            ).fetchall()
        return [
            ExportPost(
                url=row["canonical_url"],
                author=row["author_handle"] or row["author_name"] or "",
                text=row["text"] or "",
                date=row["published_at"] or "",
            )
            for row in rows
        ]


CSV_HEADER = ("url", "author", "text", "date")


def write_csv(posts: list[ExportPost], destination: Path) -> None:
    with destination.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for post in posts:
            writer.writerow((post.url, post.author, post.text, post.date))


def write_json(posts: list[ExportPost], destination: Path) -> None:
    payload = [
        {"url": post.url, "author": post.author, "text": post.text, "date": post.date}
        for post in posts
    ]
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def export_posts(posts: list[ExportPost], destination: Path, kind: str) -> Path:
    normalized = kind.casefold().removeprefix(".")
    if normalized == "csv":
        write_csv(posts, destination)
    elif normalized == "json":
        write_json(posts, destination)
    else:
        raise ValueError("kind must be 'csv' or 'json'")
    return destination
