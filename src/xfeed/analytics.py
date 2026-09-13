import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from xfeed.db import Database
from xfeed.domain import AppSession
from xfeed.session_repository import AppSessionRepository


class AnalyticsDatasetKind(StrEnum):
    CURRENT_SESSION = "current_session"
    PAST_SESSION = "past_session"
    ALL_FEED_SESSIONS = "all_feed_sessions"
    ALL_SAVED_POSTS = "all_saved_posts"


class AnalyticsFeedScope(StrEnum):
    COMBINED = "combined"
    FOR_YOU = "for_you"
    FOLLOWING = "following"


@dataclass(frozen=True)
class AnalyticsDataset:
    kind: AnalyticsDatasetKind
    session_id: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AnalyticsDatasetKind):
            raise ValueError("analytics dataset kind is invalid")
        session_kind = self.kind in {
            AnalyticsDatasetKind.CURRENT_SESSION,
            AnalyticsDatasetKind.PAST_SESSION,
        }
        valid_session_id = type(self.session_id) is int and self.session_id > 0
        if session_kind != valid_session_id:
            raise ValueError("analytics dataset shape is invalid")

    @classmethod
    def current_session(cls, session_id: int) -> "AnalyticsDataset":
        return cls(AnalyticsDatasetKind.CURRENT_SESSION, session_id)

    @classmethod
    def session(cls, session_id: int) -> "AnalyticsDataset":
        return cls(AnalyticsDatasetKind.PAST_SESSION, session_id)

    @classmethod
    def all_feed_sessions(cls) -> "AnalyticsDataset":
        return cls(AnalyticsDatasetKind.ALL_FEED_SESSIONS, None)

    @classmethod
    def all_saved_posts(cls) -> "AnalyticsDataset":
        return cls(AnalyticsDatasetKind.ALL_SAVED_POSTS, None)


@dataclass(frozen=True)
class AnalyticsQuery:
    dataset: AnalyticsDataset
    feed_scope: AnalyticsFeedScope

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, AnalyticsDataset):
            raise ValueError("analytics dataset is invalid")
        if not isinstance(self.feed_scope, AnalyticsFeedScope):
            raise ValueError("analytics feed scope is invalid")


@dataclass(frozen=True)
class DistributionItem:
    label: str
    count: int
    percentage: float


class Distribution(tuple[DistributionItem, ...]):
    def by_label(self, label: str) -> DistributionItem:
        for item in self:
            if item.label == label:
                return item
        raise KeyError(label)


@dataclass(frozen=True)
class SessionTrendPoint:
    session_id: int
    opened_at: str
    unique_posts: int
    captured_appearances: int


@dataclass(frozen=True)
class InsightsSnapshot:
    unique_posts: int
    captured_appearances: int
    new_posts: int | None
    already_saved: int | None
    unique_authors: int
    posts_with_media: int
    media_percentage: float
    topics: Distribution
    post_kinds: Distribution
    authors: Distribution
    feed_balance: Distribution
    trend: tuple[SessionTrendPoint, ...]


@dataclass(frozen=True)
class _SqlFilter:
    predicate: str
    parameters: tuple[object, ...]


class AnalyticsRepository:
    _KIND_LABELS = {
        "post": "Original",
        "repost": "Repost",
        "reply": "Reply",
        "quote": "Quote",
    }

    def __init__(
        self,
        database: Database,
        sessions: AppSessionRepository | None = None,
    ) -> None:
        self._database = database
        self._sessions = sessions or AppSessionRepository(database)

    def sessions(self) -> tuple[AppSession, ...]:
        return self._sessions.visible()

    def snapshot(self, query: AnalyticsQuery) -> InsightsSnapshot:
        if not isinstance(query, AnalyticsQuery):
            raise ValueError("analytics query is invalid")
        dataset_filter = self._dataset_filter(query.dataset)
        scope_filter = self._scope_filter(query.feed_scope)
        ctes = self._observation_ctes(query.dataset, dataset_filter, scope_filter)
        selected_posts = self._selected_posts_sql(query)
        with self._database.connection() as connection:
            connection.execute("BEGIN")
            try:
                rows = connection.execute(
                    f"""{ctes},
                           ranked_kind AS (
                             SELECT *, ROW_NUMBER() OVER (
                               PARTITION BY post_id
                               ORDER BY observed_at DESC, run_id DESC,
                                        discovery_order DESC, post_id DESC
                             ) AS kind_rank
                             FROM selected_observations
                           ),
                           selected_posts AS ({selected_posts})
                           SELECT
                             p.id,
                             COALESCE(
                               NULLIF(TRIM(p.author_handle), ''), 'Unknown'
                             ) AS author_label,
                             CASE
                               WHEN p.has_media=1 OR EXISTS(
                                 SELECT 1 FROM post_media pm
                                 WHERE pm.post_id=p.id AND pm.state='available'
                               ) THEN 1 ELSE 0
                             END AS has_media,
                             CASE
                               WHEN TRIM(p.text)='' THEN 'Unclassified'
                               ELSE COALESCE(t.display_label, 'Unclassified')
                             END AS topic_label,
                             rk.observation_kind,
                             EXISTS(
                               SELECT 1 FROM dataset_observations membership
                               WHERE membership.post_id=p.id
                                 AND membership.target_kind='for_you'
                             ) AS has_for_you,
                             EXISTS(
                               SELECT 1 FROM dataset_observations membership
                               WHERE membership.post_id=p.id
                                 AND membership.target_kind='following'
                             ) AS has_following
                           FROM selected_posts sp
                           JOIN posts p ON p.id=sp.post_id
                           LEFT JOIN ranked_kind rk ON rk.post_id=p.id AND rk.kind_rank=1
                           LEFT JOIN topic_analysis_runs ar
                             ON ar.is_active=1 AND ar.state='completed'
                           LEFT JOIN detected_topic_assignments a
                             ON a.analysis_run_id=ar.id AND a.post_id=p.id AND a.rank=1
                           LEFT JOIN detected_topics t ON t.id=a.detected_topic_id
                           ORDER BY p.id""",
                    dataset_filter.parameters,
                ).fetchall()
                appearances = self._appearance_count(
                    connection,
                    ctes,
                    dataset_filter.parameters,
                )
                new_posts, already_saved = self._run_counts(connection, query)
                trend = self._trend(connection, query, dataset_filter, scope_filter)
            finally:
                connection.rollback()
        return self._snapshot(rows, appearances, new_posts, already_saved, trend)

    @staticmethod
    def distribution(
        counts: dict[str, int] | Counter[str],
        denominator: int,
    ) -> Distribution:
        return Distribution(
            DistributionItem(
                label=label,
                count=count,
                percentage=0.0 if denominator == 0 else count * 100.0 / denominator,
            )
            for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            if count > 0
        )

    def _snapshot(
        self,
        rows: Iterable[sqlite3.Row],
        appearances: int,
        new_posts: int | None,
        already_saved: int | None,
        trend: tuple[SessionTrendPoint, ...],
    ) -> InsightsSnapshot:
        row_items = tuple(rows)
        unique_posts = len(row_items)
        topic_counts: Counter[str] = Counter()
        kind_counts: Counter[str] = Counter()
        author_counts: Counter[str] = Counter()
        balance_counts: Counter[str] = Counter()
        posts_with_media = 0
        for row in row_items:
            topic_counts[str(row["topic_label"])] += 1
            author_counts[str(row["author_label"])] += 1
            kind = row["observation_kind"]
            kind_counts["Direct save" if kind is None else self._KIND_LABELS[str(kind)]] += 1
            has_for_you = bool(row["has_for_you"])
            has_following = bool(row["has_following"])
            if has_for_you and has_following:
                balance_counts["Both feeds"] += 1
            elif has_for_you:
                balance_counts["For You only"] += 1
            elif has_following:
                balance_counts["Following only"] += 1
            else:
                balance_counts["Neither feed"] += 1
            posts_with_media += int(row["has_media"])
        return InsightsSnapshot(
            unique_posts=unique_posts,
            captured_appearances=appearances,
            new_posts=new_posts,
            already_saved=already_saved,
            unique_authors=len(author_counts),
            posts_with_media=posts_with_media,
            media_percentage=(
                0.0 if unique_posts == 0 else posts_with_media * 100.0 / unique_posts
            ),
            topics=self.distribution(topic_counts, unique_posts),
            post_kinds=self.distribution(kind_counts, unique_posts),
            authors=self.distribution(author_counts, unique_posts),
            feed_balance=self.distribution(balance_counts, unique_posts),
            trend=trend,
        )

    @staticmethod
    def _dataset_filter(dataset: AnalyticsDataset) -> _SqlFilter:
        if dataset.kind in {
            AnalyticsDatasetKind.CURRENT_SESSION,
            AnalyticsDatasetKind.PAST_SESSION,
        }:
            assert dataset.session_id is not None
            return _SqlFilter("r.session_id=?", (dataset.session_id,))
        if dataset.kind in {
            AnalyticsDatasetKind.ALL_FEED_SESSIONS,
            AnalyticsDatasetKind.ALL_SAVED_POSTS,
        }:
            return _SqlFilter("1=1", ())
        raise ValueError("analytics dataset kind is invalid")

    @staticmethod
    def _scope_filter(scope: AnalyticsFeedScope) -> _SqlFilter:
        if scope is AnalyticsFeedScope.COMBINED:
            return _SqlFilter("1=1", ())
        if scope is AnalyticsFeedScope.FOR_YOU:
            return _SqlFilter("target_kind='for_you'", ())
        if scope is AnalyticsFeedScope.FOLLOWING:
            return _SqlFilter("target_kind='following'", ())
        raise ValueError("analytics feed scope is invalid")

    @staticmethod
    def _observation_ctes(
        dataset: AnalyticsDataset,
        dataset_filter: _SqlFilter,
        scope_filter: _SqlFilter,
    ) -> str:
        target_predicate = (
            "1=1"
            if dataset.kind is AnalyticsDatasetKind.ALL_SAVED_POSTS
            else "r.target_kind IN ('for_you','following')"
        )
        return f"""WITH dataset_observations AS (
                     SELECT
                       o.run_id,o.post_id,o.discovery_order,o.observation_kind,o.observed_at,
                       r.session_id,r.target_kind,r.saved_count,r.duplicate_count,r.started_at
                     FROM post_observations o
                     JOIN collection_runs r ON r.id=o.run_id
                     WHERE {target_predicate}
                       AND {dataset_filter.predicate}
                   ),
                   selected_observations AS (
                     SELECT * FROM dataset_observations WHERE {scope_filter.predicate}
                   )"""

    @staticmethod
    def _selected_posts_sql(query: AnalyticsQuery) -> str:
        if (
            query.dataset.kind is AnalyticsDatasetKind.ALL_SAVED_POSTS
            and query.feed_scope is AnalyticsFeedScope.COMBINED
        ):
            return "SELECT id AS post_id FROM posts"
        return "SELECT DISTINCT post_id FROM selected_observations"

    @staticmethod
    def _appearance_count(
        connection: sqlite3.Connection,
        ctes: str,
        parameters: tuple[object, ...],
    ) -> int:
        row = connection.execute(
            f"{ctes} SELECT COUNT(*) AS count FROM selected_observations",
            parameters,
        ).fetchone()
        assert row is not None
        return int(row["count"])

    def _run_counts(
        self,
        connection: sqlite3.Connection,
        query: AnalyticsQuery,
    ) -> tuple[int | None, int | None]:
        if query.dataset.kind is AnalyticsDatasetKind.ALL_SAVED_POSTS:
            return None, None
        dataset_filter = self._dataset_filter(query.dataset)
        target_filter = self._run_scope_filter(query.feed_scope)
        row = connection.execute(
            f"""SELECT COALESCE(SUM(r.saved_count),0) AS saved_count,
                       COALESCE(SUM(r.duplicate_count),0) AS duplicate_count
                FROM collection_runs r
                WHERE r.target_kind IN ('for_you','following')
                  AND {dataset_filter.predicate}
                  AND {target_filter.predicate}""",
            dataset_filter.parameters,
        ).fetchone()
        assert row is not None
        return int(row["saved_count"]), int(row["duplicate_count"])

    @staticmethod
    def _run_scope_filter(scope: AnalyticsFeedScope) -> _SqlFilter:
        if scope is AnalyticsFeedScope.COMBINED:
            return _SqlFilter("1=1", ())
        if scope is AnalyticsFeedScope.FOR_YOU:
            return _SqlFilter("r.target_kind='for_you'", ())
        if scope is AnalyticsFeedScope.FOLLOWING:
            return _SqlFilter("r.target_kind='following'", ())
        raise ValueError("analytics feed scope is invalid")

    def _trend(
        self,
        connection: sqlite3.Connection,
        query: AnalyticsQuery,
        dataset_filter: _SqlFilter,
        scope_filter: _SqlFilter,
    ) -> tuple[SessionTrendPoint, ...]:
        if query.dataset.kind not in {
            AnalyticsDatasetKind.ALL_FEED_SESSIONS,
            AnalyticsDatasetKind.ALL_SAVED_POSTS,
        }:
            return ()
        ctes = self._observation_ctes(query.dataset, dataset_filter, scope_filter)
        rows = connection.execute(
            f"""{ctes},
                   visible_sessions AS (
                     SELECT s.id,s.opened_at
                     FROM app_sessions s
                     WHERE EXISTS(
                       SELECT 1 FROM collection_runs visible_run
                       JOIN post_observations visible_observation
                         ON visible_observation.run_id=visible_run.id
                       WHERE visible_run.session_id=s.id
                         AND visible_run.target_kind IN ('for_you','following')
                     )
                   )
                   SELECT
                     s.id AS session_id,
                     s.opened_at,
                     COUNT(DISTINCT selected.post_id) AS unique_posts,
                     COUNT(selected.post_id) AS captured_appearances
                   FROM visible_sessions s
                   LEFT JOIN selected_observations selected ON selected.session_id=s.id
                   GROUP BY s.id,s.opened_at
                   ORDER BY s.opened_at,s.id""",
            dataset_filter.parameters,
        ).fetchall()
        return tuple(
            SessionTrendPoint(
                session_id=int(row["session_id"]),
                opened_at=row["opened_at"],
                unique_posts=int(row["unique_posts"]),
                captured_appearances=int(row["captured_appearances"]),
            )
            for row in rows
        )
