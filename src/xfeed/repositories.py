import sqlite3
from collections.abc import Sequence

from xfeed.db import Database
from xfeed.domain import (
    AuthorCount,
    Availability,
    CollectionRun,
    CollectionStatus,
    CollectionTarget,
    CollectionTargetKind,
    LabelEvent,
    LabelKind,
    LabelOrigin,
    ObservationKind,
    ObservedPost,
    PostDraft,
    SavedPost,
)
from xfeed.sources import SourceProfile, SourceRecord, canonicalize_profile


class SourceRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, profile: SourceProfile) -> SourceRecord:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO sources(handle,profile_url) VALUES(?,?)",
                (profile.handle, profile.profile_url),
            )
            row = connection.execute(
                "SELECT * FROM sources WHERE id=?",
                (cursor.lastrowid,),
            ).fetchone()
        assert row is not None
        return self._record(row)

    def list_enabled(self) -> list[SourceRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM sources WHERE enabled=1 ORDER BY handle COLLATE NOCASE"
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_all(self) -> list[SourceRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM sources ORDER BY handle COLLATE NOCASE"
            ).fetchall()
        return [self._record(row) for row in rows]

    def set_enabled(self, source_id: int, enabled: bool) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE sources SET enabled=? WHERE id=?",
                (int(enabled), source_id),
            )

    def remove(self, source_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM refresh_events WHERE source_id=?", (source_id,))
            connection.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def record_refresh(
        self,
        source_id: int,
        result: str,
        diagnostic: str | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO refresh_events(source_id,started_at,finished_at,result,diagnostic)
                   VALUES(?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?,?)""",
                (source_id, result, diagnostic),
            )
            connection.execute(
                "UPDATE sources SET last_refresh_at=CURRENT_TIMESTAMP WHERE id=?",
                (source_id,),
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> SourceRecord:
        return SourceRecord(
            id=int(row["id"]),
            handle=row["handle"],
            profile_url=row["profile_url"],
            enabled=bool(row["enabled"]),
            last_refresh_at=row["last_refresh_at"],
        )


class PostRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def insert(self, draft: PostDraft, *, manual_ingestion: bool = False) -> SavedPost:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO posts(canonical_url,x_post_id,author_handle,author_name,text,
                   published_at,embed_html,provider_json,source_method,has_media,availability)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    draft.canonical_url,
                    draft.x_post_id,
                    draft.author_handle,
                    draft.author_name,
                    draft.text,
                    draft.published_at,
                    draft.embed_html,
                    draft.provider_json,
                    draft.source_method,
                    int(draft.has_media),
                    draft.availability.value,
                ),
            )
            post_id = cursor.lastrowid
            assert post_id is not None
            if manual_ingestion:
                connection.execute(
                    "INSERT INTO manual_ingestions(post_id) VALUES(?)",
                    (post_id,),
                )
            return SavedPost(**draft.__dict__, id=post_id)

    def record_manual_ingestion(self, post_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO manual_ingestions(post_id) VALUES(?)",
                (post_id,),
            )

    def was_manually_ingested(self, post_id: int) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM manual_ingestions WHERE post_id=?",
                (post_id,),
            ).fetchone()
        return row is not None

    def by_url(self, canonical_url: str) -> SavedPost | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM posts WHERE canonical_url=?",
                (canonical_url,),
            ).fetchone()
        return None if row is None else self._saved_post(row)

    def by_id(self, post_id: int) -> SavedPost | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM posts WHERE id=?",
                (post_id,),
            ).fetchone()
        return None if row is None else self._saved_post(row)

    def list_recent(
        self,
        author_handle: str | None = None,
        limit: int = 100,
    ) -> list[SavedPost]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query = "SELECT * FROM posts"
        parameters: list[str | int] = []
        if author_handle is not None:
            query += " WHERE author_handle = ? COLLATE NOCASE"
            parameters.append(author_handle)
        query += " ORDER BY published_at DESC, id DESC LIMIT ?"
        parameters.append(limit)
        with self.database.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._saved_post(row) for row in rows]

    def list_manual(self, limit: int = 100) -> list[SavedPost]:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be at least 1")
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT posts.* FROM manual_ingestions
                   JOIN posts ON posts.id=manual_ingestions.post_id
                   ORDER BY manual_ingestions.recorded_at DESC, posts.id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._saved_post(row) for row in rows]

    def list_for_authors(
        self,
        handles: Sequence[str],
        limit: int = 200,
    ) -> list[SavedPost]:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be at least 1")
        normalized = tuple(dict.fromkeys(canonicalize_profile(handle).handle for handle in handles))
        if not normalized:
            return []
        placeholders = ",".join("?" for _handle in normalized)
        query = (
            "SELECT * FROM posts WHERE author_handle COLLATE NOCASE IN ("
            f"{placeholders}) ORDER BY published_at DESC, id DESC LIMIT ?"
        )
        with self.database.connection() as connection:
            rows = connection.execute(query, (*normalized, limit)).fetchall()
        return [self._saved_post(row) for row in rows]

    def author_counts(self) -> list[AuthorCount]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT author_handle, COUNT(*) AS post_count
                   FROM posts
                   WHERE author_handle IS NOT NULL
                   GROUP BY author_handle COLLATE NOCASE
                   ORDER BY post_count DESC, author_handle COLLATE NOCASE"""
            ).fetchall()
        return [AuthorCount(handle=row["author_handle"], count=row["post_count"]) for row in rows]

    def count_by_author(self, handle: str) -> int:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS post_count FROM posts WHERE author_handle = ? COLLATE NOCASE",
                (handle,),
            ).fetchone()
        assert row is not None
        return int(row["post_count"])

    def update_provider(self, post_id: int, draft: PostDraft) -> SavedPost:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE posts
                   SET author_handle=?,author_name=?,text=?,published_at=?,embed_html=?,
                       provider_json=?,source_method=?,has_media=?,availability=?,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    draft.author_handle,
                    draft.author_name,
                    draft.text,
                    draft.published_at,
                    draft.embed_html,
                    draft.provider_json,
                    draft.source_method,
                    int(draft.has_media),
                    draft.availability.value,
                    post_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM posts WHERE id=?",
                (post_id,),
            ).fetchone()
        assert row is not None
        return self._saved_post(row)

    @staticmethod
    def _saved_post(row: sqlite3.Row) -> SavedPost:
        values = {
            name: row[name]
            for name in (
                "id",
                "canonical_url",
                "x_post_id",
                "author_handle",
                "author_name",
                "text",
                "published_at",
                "embed_html",
                "provider_json",
                "source_method",
                "has_media",
                "availability",
            )
        }
        values["availability"] = Availability(values["availability"])
        values["has_media"] = bool(values["has_media"])
        return SavedPost(**values)


class LabelRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def append(
        self,
        post_id: int,
        label: LabelKind,
        origin: LabelOrigin,
        reason: str | None,
    ) -> LabelEvent:
        active = self.active_for_post(post_id)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO label_events(post_id,label,origin,reason,supersedes_id) "
                "VALUES(?,?,?,?,?)",
                (post_id, label.value, origin.value, reason, active.id if active else None),
            )
            row = connection.execute(
                "SELECT * FROM label_events WHERE id=?",
                (cursor.lastrowid,),
            ).fetchone()
        assert row is not None
        return self._event(row)

    def active_for_post(self, post_id: int) -> LabelEvent | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM label_events WHERE post_id=? ORDER BY id DESC LIMIT 1",
                (post_id,),
            ).fetchone()
        return None if row is None else self._event(row)

    def history_for_post(self, post_id: int) -> list[LabelEvent]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM label_events WHERE post_id=? ORDER BY id",
                (post_id,),
            ).fetchall()
        return [self._event(row) for row in rows]

    @staticmethod
    def _event(row: sqlite3.Row) -> LabelEvent:
        return LabelEvent(
            id=int(row["id"]),
            post_id=int(row["post_id"]),
            label=LabelKind(row["label"]),
            origin=LabelOrigin(row["origin"]),
            reason=row["reason"],
            created_at=row["created_at"],
        )


class CollectionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def start(
        self,
        target: CollectionTarget | int,
        requested_max: int = 30,
        *,
        session_id: int | None = None,
    ) -> CollectionRun:
        if isinstance(target, CollectionTarget):
            target_kind = target.kind
            source_id = target.source_id
        elif type(target) is int and target >= 1:
            target_kind = CollectionTargetKind.SOURCE
            source_id = target
        else:
            raise ValueError("target must be a collection target")
        upper = 30 if target_kind is CollectionTargetKind.SOURCE else 50
        if type(requested_max) is not int or not 1 <= requested_max <= upper:
            raise ValueError(f"requested_max must be between 1 and {upper}")
        if session_id is not None and (type(session_id) is not int or session_id < 1):
            raise ValueError("session_id must be a positive integer or None")
        with self.database.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active_run = connection.execute(
                "SELECT 1 FROM collection_runs WHERE status=? LIMIT 1",
                (CollectionStatus.RUNNING.value,),
            ).fetchone()
            if active_run is not None:
                raise RuntimeError("a collection run is already running")
            cursor = connection.execute(
                """INSERT INTO collection_runs(
                       target_kind,source_id,session_id,requested_max,status
                   ) VALUES(?,?,?,?,?)""",
                (
                    target_kind.value,
                    source_id,
                    session_id,
                    requested_max,
                    CollectionStatus.RUNNING.value,
                ),
            )
            row = connection.execute(
                "SELECT * FROM collection_runs WHERE id=?",
                (cursor.lastrowid,),
            ).fetchone()
        assert row is not None
        return self._run(row)

    def recover_abandoned_runs(self) -> list[CollectionRun]:
        recovered: list[CollectionRun] = []
        with self.database.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running = connection.execute(
                "SELECT * FROM collection_runs WHERE status=? ORDER BY id",
                (CollectionStatus.RUNNING.value,),
            ).fetchall()
            for row in running:
                counts = connection.execute(
                    """SELECT COUNT(*) AS observed_count,
                              COALESCE(SUM(
                                  CASE WHEN posts.created_at >= ? THEN 1 ELSE 0 END
                              ), 0) AS estimated_saved_count
                       FROM post_observations
                       JOIN posts ON posts.id=post_observations.post_id
                       WHERE post_observations.run_id=?""",
                    (row["started_at"], row["id"]),
                ).fetchone()
                assert counts is not None
                observed_count = int(counts["observed_count"])
                estimated_saved_count = int(counts["estimated_saved_count"])
                saved_count = max(int(row["saved_count"]), estimated_saved_count)
                duplicate_count = max(
                    int(row["duplicate_count"]),
                    observed_count - estimated_saved_count,
                )
                failed_count = int(row["failed_count"])
                candidate_count = max(
                    int(row["candidate_count"]),
                    observed_count,
                    saved_count + duplicate_count + failed_count,
                )
                status = (
                    CollectionStatus.PARTIAL if candidate_count > 0 else CollectionStatus.FAILED
                )
                connection.execute(
                    """UPDATE collection_runs
                       SET finished_at=CURRENT_TIMESTAMP,status=?,candidate_count=?,saved_count=?,
                           duplicate_count=?,failed_count=?,reason=?,diagnostic=?
                       WHERE id=? AND status=?""",
                    (
                        status.value,
                        candidate_count,
                        saved_count,
                        duplicate_count,
                        failed_count,
                        "error",
                        "Collection was interrupted before the previous desktop app exited.",
                        row["id"],
                        CollectionStatus.RUNNING.value,
                    ),
                )
                recovered_row = connection.execute(
                    "SELECT * FROM collection_runs WHERE id=?",
                    (row["id"],),
                ).fetchone()
                assert recovered_row is not None
                recovered.append(self._run(recovered_row))
        return recovered

    def finish(
        self,
        run_id: int,
        *,
        status: CollectionStatus,
        candidate_count: int,
        saved_count: int,
        duplicate_count: int,
        failed_count: int,
        reason: str | None,
        diagnostic: str | None,
    ) -> CollectionRun:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE collection_runs
                   SET finished_at=CURRENT_TIMESTAMP,status=?,candidate_count=?,saved_count=?,
                       duplicate_count=?,failed_count=?,reason=?,diagnostic=?
                   WHERE id=?""",
                (
                    status.value,
                    candidate_count,
                    saved_count,
                    duplicate_count,
                    failed_count,
                    reason,
                    diagnostic,
                    run_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM collection_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        assert row is not None
        return self._run(row)

    def finish_if_running(
        self,
        run_id: int,
        *,
        status: CollectionStatus,
        candidate_count: int,
        saved_count: int,
        duplicate_count: int,
        failed_count: int,
        reason: str | None,
        diagnostic: str | None,
    ) -> CollectionRun:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE collection_runs
                   SET finished_at=CURRENT_TIMESTAMP,status=?,candidate_count=?,saved_count=?,
                       duplicate_count=?,failed_count=?,reason=?,diagnostic=?
                   WHERE id=? AND status=?""",
                (
                    status.value,
                    candidate_count,
                    saved_count,
                    duplicate_count,
                    failed_count,
                    reason,
                    diagnostic,
                    run_id,
                    CollectionStatus.RUNNING.value,
                ),
            )
            row = connection.execute(
                "SELECT * FROM collection_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        assert row is not None
        return self._run(row)

    def observe(
        self,
        run_id: int,
        post_id: int,
        *,
        discovery_order: int = 0,
        observation_kind: ObservationKind = ObservationKind.POST,
        parent_post_id: int | None = None,
    ) -> None:
        if type(discovery_order) is not int or discovery_order < 0:
            raise ValueError("discovery_order must be non-negative")
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO post_observations(
                       run_id,post_id,discovery_order,observation_kind,parent_post_id
                   ) VALUES(?,?,?,?,?)""",
                (
                    run_id,
                    post_id,
                    discovery_order,
                    observation_kind.value,
                    parent_post_id,
                ),
            )

    def latest_for_target(self, target: CollectionTarget) -> CollectionRun | None:
        if target.kind is CollectionTargetKind.SOURCE:
            return self.latest_for_source(target.source_id)
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT * FROM collection_runs
                   WHERE target_kind=? ORDER BY id DESC LIMIT 1""",
                (target.kind.value,),
            ).fetchone()
        return None if row is None else self._run(row)

    def latest_for_source(self, source_id: int | None) -> CollectionRun | None:
        if type(source_id) is not int or source_id < 1:
            return None
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT * FROM collection_runs
                   WHERE target_kind='source' AND source_id=? ORDER BY id DESC LIMIT 1""",
                (source_id,),
            ).fetchone()
        return None if row is None else self._run(row)

    def latest_for_you_posts(self) -> list[ObservedPost]:
        return self.latest_feed_posts(CollectionTargetKind.FOR_YOU)

    def latest_feed_posts(self, kind: CollectionTargetKind) -> list[ObservedPost]:
        self._validate_home_kind(kind)
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT id FROM collection_runs
                   WHERE target_kind=?
                     AND EXISTS(
                         SELECT 1 FROM post_observations
                         WHERE post_observations.run_id=collection_runs.id
                     )
                   ORDER BY id DESC LIMIT 1""",
                (kind.value,),
            ).fetchone()
        return [] if row is None else self.feed_posts_for_runs(kind, (int(row["id"]),))

    def for_you_posts_for_runs(self, run_ids: Sequence[int]) -> list[ObservedPost]:
        return self.feed_posts_for_runs(CollectionTargetKind.FOR_YOU, run_ids)

    def feed_posts_for_runs(
        self, kind: CollectionTargetKind, run_ids: Sequence[int]
    ) -> list[ObservedPost]:
        self._validate_home_kind(kind)
        ids = self._positive_ids(run_ids, "run_ids")
        if not ids:
            return []
        return self._observed_for_where(
            "r.target_kind=? AND r.id IN ({})".format(",".join("?" for _ in ids)),
            (kind.value, *ids),
        )

    def older_for_you_posts(
        self,
        excluding_run_ids: Sequence[int],
        excluding_post_ids: Sequence[int],
        limit: int = 200,
    ) -> list[ObservedPost]:
        return self.older_feed_posts(
            CollectionTargetKind.FOR_YOU,
            excluding_run_ids=excluding_run_ids,
            excluding_post_ids=excluding_post_ids,
            limit=limit,
        )

    def older_feed_posts(
        self,
        kind: CollectionTargetKind,
        *,
        excluding_run_ids: Sequence[int],
        excluding_post_ids: Sequence[int],
        limit: int = 200,
    ) -> list[ObservedPost]:
        self._validate_home_kind(kind)
        run_ids = self._positive_ids(excluding_run_ids, "excluding_run_ids")
        post_ids = self._positive_ids(excluding_post_ids, "excluding_post_ids")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        predicates = ["r.target_kind=?"]
        parameters: list[object] = [kind.value]
        if run_ids:
            predicates.append("r.id NOT IN ({})".format(",".join("?" for _ in run_ids)))
            parameters.extend(run_ids)
        if post_ids:
            predicates.append("p.id NOT IN ({})".format(",".join("?" for _ in post_ids)))
            parameters.extend(post_ids)
        return self._observed_for_where(" AND ".join(predicates), parameters)[:limit]

    def _observed_for_where(
        self, where_sql: str, parameters: Sequence[object]
    ) -> list[ObservedPost]:
        sql = f"""SELECT p.*,
                          o.discovery_order AS observation_order,
                          o.observation_kind AS observed_kind,
                          o.parent_post_id AS observed_parent_post_id
                   FROM post_observations o
                   JOIN collection_runs r ON r.id=o.run_id
                   JOIN posts p ON p.id=o.post_id
                   WHERE {where_sql}
                   ORDER BY r.id DESC,o.discovery_order ASC,
                            o.observed_at DESC,p.id DESC"""
        with self.database.connection() as connection:
            rows = connection.execute(sql, tuple(parameters)).fetchall()
        return self._deduplicated_observed_posts(rows)

    @staticmethod
    def _validate_home_kind(kind: CollectionTargetKind) -> None:
        if not isinstance(kind, CollectionTargetKind) or kind not in (
            CollectionTargetKind.FOR_YOU,
            CollectionTargetKind.FOLLOWING,
        ):
            raise ValueError("kind must be a home feed target")

    @staticmethod
    def _positive_ids(values: Sequence[int], field: str) -> tuple[int, ...]:
        normalized = tuple(values)
        if any(type(value) is not int or value < 1 for value in normalized):
            raise ValueError(f"{field} must contain positive integers")
        return tuple(dict.fromkeys(normalized))

    @staticmethod
    def _deduplicated_observed_posts(
        rows: Sequence[sqlite3.Row],
        *,
        limit: int | None = None,
    ) -> list[ObservedPost]:
        observed: list[ObservedPost] = []
        seen_post_ids: set[int] = set()
        for row in rows:
            post = PostRepository._saved_post(row)
            if post.id in seen_post_ids:
                continue
            seen_post_ids.add(post.id)
            observed.append(
                ObservedPost(
                    post=post,
                    discovery_order=int(row["observation_order"]),
                    observation_kind=ObservationKind(row["observed_kind"]),
                    parent_post_id=(
                        None
                        if row["observed_parent_post_id"] is None
                        else int(row["observed_parent_post_id"])
                    ),
                )
            )
            if limit is not None and len(observed) >= limit:
                break
        return observed

    @staticmethod
    def _run(row: sqlite3.Row) -> CollectionRun:
        return CollectionRun(
            id=int(row["id"]),
            source_id=None if row["source_id"] is None else int(row["source_id"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            requested_max=int(row["requested_max"]),
            candidate_count=int(row["candidate_count"]),
            saved_count=int(row["saved_count"]),
            duplicate_count=int(row["duplicate_count"]),
            failed_count=int(row["failed_count"]),
            status=CollectionStatus(row["status"]),
            reason=row["reason"],
            diagnostic=row["diagnostic"],
            target_kind=CollectionTargetKind(row["target_kind"]),
            session_id=None if row["session_id"] is None else int(row["session_id"]),
        )
