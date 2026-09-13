import sqlite3
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import Iterator

from xfeed.db import Database
from xfeed.media import (
    MediaAsset,
    MediaAssetState,
    MediaOrigin,
    MediaResolutionJob,
    MediaResolutionState,
    PhotoCandidate,
    normalize_photo_url,
)


class MediaRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_resolution_job(
        self, post_id: int, canonical_url: str, origin: MediaOrigin
    ) -> MediaResolutionJob:
        with self.database.transaction() as connection:
            self._ensure_resolution_job(connection, post_id, canonical_url, origin)
            row = connection.execute(
                "SELECT * FROM media_resolution_jobs WHERE post_id=?", (post_id,)
            ).fetchone()
        assert row is not None
        return self._job(row)

    def record_manifest(
        self,
        post_id: int,
        canonical_url: str,
        origin: MediaOrigin,
        photos: Sequence[PhotoCandidate],
    ) -> tuple[MediaAsset, ...]:
        candidates = tuple(photos)
        if tuple(photo.position for photo in candidates) != tuple(range(len(candidates))):
            raise ValueError("photo positions must be exactly contiguous from zero")
        normalized = tuple((photo, normalize_photo_url(photo.url)) for photo in candidates)
        with self.database.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_resolution_job(connection, post_id, canonical_url, origin)
            current_urls = {photo.position: source_url for photo, source_url in normalized}
            existing = connection.execute(
                "SELECT id,position,source_url FROM post_media WHERE post_id=?", (post_id,)
            ).fetchall()
            for row in existing:
                if current_urls.get(int(row["position"])) != row["source_url"]:
                    connection.execute("DELETE FROM post_media WHERE id=?", (row["id"],))
            connection.execute(
                """UPDATE media_resolution_jobs
                   SET state=?,manifest_count=?,diagnostic=NULL,updated_at=CURRENT_TIMESTAMP
                   WHERE post_id=?""",
                (MediaResolutionState.RESOLVED.value, len(normalized), post_id),
            )
            assets: list[MediaAsset] = []
            for candidate, source_url in normalized:
                row = connection.execute(
                    "SELECT * FROM post_media WHERE post_id=? AND position=?",
                    (post_id, candidate.position),
                ).fetchone()
                if row is None:
                    cursor = connection.execute(
                        """INSERT INTO post_media(post_id,position,source_url,alt_text,state)
                           VALUES(?,?,?,?,?)""",
                        (
                            post_id,
                            candidate.position,
                            source_url,
                            candidate.alt_text,
                            MediaAssetState.PENDING.value,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM post_media WHERE id=?", (cursor.lastrowid,)
                    ).fetchone()
                assert row is not None
                assets.append(self._asset(row))
        return tuple(assets)

    def pending_resolution_jobs(self, limit: int = 100) -> list[MediaResolutionJob]:
        self._validate_limit(limit)
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM media_resolution_jobs WHERE state=? ORDER BY id LIMIT ?""",
                (MediaResolutionState.PENDING.value, limit),
            ).fetchall()
        return [self._job(row) for row in rows]

    def claim_resolution(self, job_id: int) -> MediaResolutionJob | None:
        with self.database.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE media_resolution_jobs
                   SET state=?,attempts=attempts+1,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND state=?""",
                (MediaResolutionState.RESOLVING.value, job_id, MediaResolutionState.PENDING.value),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM media_resolution_jobs WHERE id=?", (job_id,)
            ).fetchone()
        assert row is not None
        return self._job(row)

    def requeue_resolution(self, job_id: int, diagnostic: str) -> None:
        self._transition_resolution(job_id, MediaResolutionState.PENDING, diagnostic)

    def fail_resolution(self, job_id: int, diagnostic: str) -> None:
        self._transition_resolution(job_id, MediaResolutionState.FAILED, diagnostic)

    def pending_assets(self, limit: int = 100) -> list[MediaAsset]:
        self._validate_limit(limit)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM post_media WHERE state=? ORDER BY post_id,position LIMIT ?",
                (MediaAssetState.PENDING.value, limit),
            ).fetchall()
        return [self._asset(row) for row in rows]

    def claim_asset(self, asset_id: int) -> MediaAsset | None:
        with self.database.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE post_media SET state=?,attempts=attempts+1,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND state=?""",
                (MediaAssetState.DOWNLOADING.value, asset_id, MediaAssetState.PENDING.value),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute("SELECT * FROM post_media WHERE id=?", (asset_id,)).fetchone()
        assert row is not None
        return self._asset(row)

    @contextmanager
    def guard_asset_publication(self, asset: MediaAsset) -> Iterator[bool]:
        with self.database.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT 1 FROM post_media
                   WHERE id=? AND post_id=? AND position=? AND source_url=? AND state=?""",
                (
                    asset.id,
                    asset.post_id,
                    asset.position,
                    asset.source_url,
                    MediaAssetState.DOWNLOADING.value,
                ),
            ).fetchone()
            yield row is not None

    def requeue_asset(self, asset_id: int, diagnostic: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE post_media SET state=?,diagnostic=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND state=?""",
                (
                    MediaAssetState.PENDING.value,
                    diagnostic,
                    asset_id,
                    MediaAssetState.DOWNLOADING.value,
                ),
            )

    def mark_asset_available(
        self,
        asset_id: int,
        *,
        local_path: str,
        mime_type: str,
        width: int,
        height: int,
    ) -> MediaAsset:
        normalized_local_path = self._normalize_local_path(local_path)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE post_media
                   SET state=?,local_path=?,mime_type=?,width=?,height=?,diagnostic=NULL,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND state=?""",
                (
                    MediaAssetState.AVAILABLE.value,
                    normalized_local_path,
                    mime_type,
                    width,
                    height,
                    asset_id,
                    MediaAssetState.DOWNLOADING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("asset is not being downloaded")
            row = connection.execute("SELECT * FROM post_media WHERE id=?", (asset_id,)).fetchone()
        assert row is not None
        return self._asset(row)

    def fail_asset(self, asset_id: int, diagnostic: str) -> MediaAsset:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE post_media SET state=?,diagnostic=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND state=?""",
                (
                    MediaAssetState.FAILED.value,
                    diagnostic,
                    asset_id,
                    MediaAssetState.DOWNLOADING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("asset is not being downloaded")
            row = connection.execute("SELECT * FROM post_media WHERE id=?", (asset_id,)).fetchone()
        assert row is not None
        return self._asset(row)

    def assets_for_posts(self, post_ids: Sequence[int]) -> dict[int, tuple[MediaAsset, ...]]:
        identifiers = tuple(dict.fromkeys(post_ids))
        if not identifiers:
            return {}
        placeholders = ",".join("?" for _post_id in identifiers)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""SELECT * FROM post_media WHERE post_id IN ({placeholders})
                    ORDER BY post_id,position""",
                identifiers,
            ).fetchall()
        grouped: dict[int, list[MediaAsset]] = {post_id: [] for post_id in identifiers}
        for row in rows:
            grouped[int(row["post_id"])].append(self._asset(row))
        return {post_id: tuple(assets) for post_id, assets in grouped.items()}

    def reset_interrupted(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE media_resolution_jobs SET state=?,updated_at=CURRENT_TIMESTAMP
                   WHERE state=?""",
                (MediaResolutionState.PENDING.value, MediaResolutionState.RESOLVING.value),
            )
            connection.execute(
                """UPDATE post_media SET state=?,updated_at=CURRENT_TIMESTAMP WHERE state=?""",
                (MediaAssetState.PENDING.value, MediaAssetState.DOWNLOADING.value),
            )

    @staticmethod
    def _ensure_resolution_job(
        connection: sqlite3.Connection, post_id: int, canonical_url: str, origin: MediaOrigin
    ) -> None:
        connection.execute(
            """INSERT INTO media_resolution_jobs(post_id,canonical_url,origin,state)
               VALUES(?,?,?,?) ON CONFLICT(post_id) DO NOTHING""",
            (post_id, canonical_url, origin.value, MediaResolutionState.PENDING.value),
        )

    def _transition_resolution(
        self, job_id: int, state: MediaResolutionState, diagnostic: str
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE media_resolution_jobs SET state=?,diagnostic=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND state=?""",
                (state.value, diagnostic, job_id, MediaResolutionState.RESOLVING.value),
            )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be at least 1")

    @staticmethod
    def _normalize_local_path(local_path: str) -> str:
        if (
            type(local_path) is not str
            or not local_path
            or "\\" in local_path
            or ":" in local_path
            or "\x00" in local_path
        ):
            raise ValueError("local_path must be a safe media-relative POSIX path")
        path = PurePosixPath(local_path)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError("local_path must be a safe relative POSIX path")
        return path.as_posix()

    @staticmethod
    def _job(row: sqlite3.Row) -> MediaResolutionJob:
        return MediaResolutionJob(
            id=int(row["id"]),
            post_id=int(row["post_id"]),
            canonical_url=row["canonical_url"],
            origin=MediaOrigin(row["origin"]),
            state=MediaResolutionState(row["state"]),
            manifest_count=(None if row["manifest_count"] is None else int(row["manifest_count"])),
            attempts=int(row["attempts"]),
            diagnostic=row["diagnostic"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _asset(row: sqlite3.Row) -> MediaAsset:
        return MediaAsset(
            id=int(row["id"]),
            post_id=int(row["post_id"]),
            position=int(row["position"]),
            source_url=row["source_url"],
            alt_text=row["alt_text"],
            state=MediaAssetState(row["state"]),
            local_path=row["local_path"],
            mime_type=row["mime_type"],
            width=None if row["width"] is None else int(row["width"]),
            height=None if row["height"] is None else int(row["height"]),
            attempts=int(row["attempts"]),
            diagnostic=row["diagnostic"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
