from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import QObject, QPoint, QRect, Qt, Signal

from xfeed.analytics import (
    AnalyticsDatasetKind,
    AnalyticsFeedScope,
    AnalyticsQuery,
    Distribution,
    DistributionItem,
    InsightsSnapshot,
    SessionTrendPoint,
)
from xfeed.domain import AppSession, AppSessionState
from xfeed.topic_analysis import TopicAnalysisRun, TopicAnalysisState
from xfeed.ui.insights_view import InsightsView, SessionTrendChart
from xfeed.ui.main_window import MainWindow
from xfeed.ui.theme import apply_application_theme


def snapshot(**changes: object) -> InsightsSnapshot:
    base = InsightsSnapshot(
        unique_posts=12,
        captured_appearances=17,
        new_posts=8,
        already_saved=4,
        unique_authors=5,
        posts_with_media=3,
        media_percentage=25.0,
        topics=Distribution(
            (
                DistributionItem("Software craft", 7, 58.3333),
                DistributionItem("Local news", 5, 41.6667),
            )
        ),
        post_kinds=Distribution((DistributionItem("Original", 12, 100.0),)),
        authors=Distribution((DistributionItem("reader", 12, 100.0),)),
        feed_balance=Distribution((DistributionItem("Following only", 12, 100.0),)),
        trend=(
            SessionTrendPoint(1, "2026-08-01 08:00:00", 4, 6),
            SessionTrendPoint(2, "2026-08-02 08:00:00", 8, 11),
        ),
    )
    return replace(base, **changes)


def topic_status(
    state: TopicAnalysisState,
    *,
    diagnostic: str | None = None,
    completed_at: str | None = None,
) -> TopicAnalysisRun:
    return TopicAnalysisRun(
        id=1,
        method="tfidf_nmf",
        method_version="1",
        config_json="{}",
        corpus_fingerprint="fingerprint",
        state=state,
        diagnostic=diagnostic,
        created_at="2026-08-02 08:00:00",
        completed_at=completed_at,
        is_active=state is TopicAnalysisState.COMPLETED,
    )


class AnalyticsSpy:
    def __init__(self, result: InsightsSnapshot | Exception | None = None) -> None:
        self.result = result or snapshot()
        self.queries: list[AnalyticsQuery] = []

    @property
    def last_query(self) -> AnalyticsQuery:
        return self.queries[-1]

    def snapshot(self, query: AnalyticsQuery) -> InsightsSnapshot:
        self.queries.append(query)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class SessionRepositorySpy:
    def __init__(self) -> None:
        self.items = (
            AppSession(
                8,
                "2026-08-02 09:00:00",
                None,
                AppSessionState.OPEN,
                "0.1.0",
            ),
            AppSession(
                3,
                "2026-08-01 09:00:00",
                "2026-08-01 10:00:00",
                AppSessionState.CLOSED,
                "0.1.0",
            ),
        )

    def visible(self) -> tuple[AppSession, ...]:
        return self.items


class ManagerSpy(QObject):
    status_changed = Signal(object)

    def __init__(
        self,
        status: TopicAnalysisRun | None = None,
        active: TopicAnalysisRun | None = None,
    ) -> None:
        super().__init__()
        self._status = status
        self._active = active
        self.requests: list[bool] = []

    def status(self) -> TopicAnalysisRun | None:
        return self._status

    def active_run(self) -> TopicAnalysisRun | None:
        return self._active

    def set_status(self, value: TopicAnalysisRun) -> None:
        self._status = value
        if value.state is TopicAnalysisState.COMPLETED and value.is_active:
            self._active = value
        self.status_changed.emit(value)

    def request_analysis(self, *, force: bool = False) -> None:
        self.requests.append(force)


@pytest.fixture
def analytics_spy() -> AnalyticsSpy:
    return AnalyticsSpy()


@pytest.fixture
def session_repository() -> SessionRepositorySpy:
    return SessionRepositorySpy()


@pytest.fixture
def manager_spy() -> ManagerSpy:
    active = topic_status(TopicAnalysisState.COMPLETED, completed_at="2026-08-02")
    return ManagerSpy(active, active)


@pytest.fixture
def view(qtbot, analytics_spy, session_repository, manager_spy) -> InsightsView:
    result = InsightsView(analytics_spy, session_repository, manager_spy, 11)
    qtbot.addWidget(result)
    return result


def test_past_session_selector_uses_only_nonempty_sessions(
    view: InsightsView,
    session_repository: SessionRepositorySpy,
    analytics_spy: AnalyticsSpy,
) -> None:
    view.select_dataset(AnalyticsDatasetKind.PAST_SESSION)

    assert view.session_ids() == tuple(item.id for item in session_repository.visible())
    assert analytics_spy.last_query.dataset.session_id == 8
    assert view.past_session_selector.isVisibleTo(view)

    view.past_session_selector.setCurrentIndex(1)
    assert analytics_spy.last_query.dataset.session_id == 3


def test_past_session_controls_fit_real_minimum_main_window(
    qapp,
    qtbot,
    analytics_spy: AnalyticsSpy,
    manager_spy: ManagerSpy,
) -> None:
    apply_application_theme(qapp)
    sessions = SessionRepositorySpy()
    sessions.items = (
        AppSession(
            8,
            "2026-08-02 09:00:00.123456 +03:00 Europe/Istanbul",
            "2026-08-02 10:00:00",
            AppSessionState.INTERRUPTED,
            "0.1.0",
        ),
    )
    subject = InsightsView(analytics_spy, sessions, manager_spy, 11)
    window = MainWindow(pages={"Insights": subject})
    qtbot.addWidget(window)
    window.resize(960, 640)
    window.show()
    window.navigate("Insights")
    subject.select_dataset(AnalyticsDatasetKind.PAST_SESSION)
    qapp.processEvents()

    viewport = subject.viewport()
    controls = (
        subject._dataset,
        subject.past_session_selector,
        subject._feed_scope,
        subject.analysis_status_label,
        subject.reanalyze_button,
    )
    assert (window.width(), window.height()) == (960, 640)
    assert subject.widget().width() <= viewport.width()
    assert subject.horizontalScrollBar().maximum() == 0
    for control in controls:
        top_left = control.mapTo(viewport, QPoint(0, 0))
        assert viewport.rect().contains(QRect(top_left, control.size()))
        assert control.isVisibleTo(subject)


def test_current_session_is_queryable_when_snapshot_is_empty(
    qtbot,
    session_repository: SessionRepositorySpy,
    manager_spy: ManagerSpy,
) -> None:
    analytics = AnalyticsSpy(snapshot(unique_posts=0))
    subject = InsightsView(analytics, session_repository, manager_spy, 27)
    qtbot.addWidget(subject)

    assert analytics.last_query.dataset.kind is AnalyticsDatasetKind.CURRENT_SESSION
    assert analytics.last_query.dataset.session_id == 27
    assert subject.data_message() == "No posts match these filters"


def test_following_filter_requests_following_snapshot(
    view: InsightsView,
    analytics_spy: AnalyticsSpy,
) -> None:
    view.select_feed_scope(AnalyticsFeedScope.FOLLOWING)

    assert analytics_spy.last_query.feed_scope is AnalyticsFeedScope.FOLLOWING


def test_dashboard_keeps_non_topic_stats_when_topics_are_insufficient(
    view: InsightsView,
    manager_spy: ManagerSpy,
) -> None:
    manager_spy.set_status(topic_status(TopicAnalysisState.INSUFFICIENT))

    assert view.summary_value("Unique posts") == "12"
    assert view.topic_message() == "Not enough text for reliable topics"
    assert view.section_is_visible("Post kinds")


def test_reanalyze_forces_manager(
    view: InsightsView,
    manager_spy: ManagerSpy,
    qtbot,
) -> None:
    qtbot.mouseClick(view.reanalyze_button, Qt.MouseButton.LeftButton)

    assert manager_spy.requests == [True]


def test_all_saved_selector_hides_new_and_duplicate_evidence(
    view: InsightsView,
    analytics_spy: AnalyticsSpy,
) -> None:
    analytics_spy.result = snapshot(new_posts=None, already_saved=None)
    view.select_dataset(AnalyticsDatasetKind.ALL_SAVED_POSTS)

    assert analytics_spy.last_query.dataset.kind is AnalyticsDatasetKind.ALL_SAVED_POSTS
    assert view.summary_value("Unique posts") == "12"
    assert view.summary_value("New posts") is None
    assert view.summary_value("Already saved") is None


def test_topic_spectrum_renders_precise_local_rows(view: InsightsView) -> None:
    view.render_snapshot(snapshot())

    assert view.distribution_rows("Primary topics") == (
        ("Software craft", "7  ·  58.3%"),
        ("Local news", "5  ·  41.7%"),
    )


def test_multi_session_trend_keeps_repository_order(view: InsightsView) -> None:
    view.render_snapshot(snapshot())

    chart = view.findChild(SessionTrendChart, "sessionTrend")
    assert chart is not None
    assert chart.points == snapshot().trend


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (TopicAnalysisState.PENDING, "Analyzing saved posts locally…"),
        (TopicAnalysisState.RUNNING, "Analyzing saved posts locally…"),
    ],
)
def test_pending_topic_states_do_not_hide_other_statistics(
    view: InsightsView,
    manager_spy: ManagerSpy,
    state: TopicAnalysisState,
    expected: str,
) -> None:
    manager_spy.set_status(topic_status(state))

    assert view.topic_message() == expected
    assert view.section_is_visible("Top authors")


def test_failed_topic_diagnostic_is_bounded_without_hiding_stats(
    view: InsightsView,
    manager_spy: ManagerSpy,
) -> None:
    manager_spy.set_status(topic_status(TopicAnalysisState.FAILED, diagnostic="x" * 700))

    assert view.topic_message().startswith("Topic analysis failed: ")
    assert len(view.topic_message()) <= 523
    assert view.summary_value("Unique posts") == "12"


def test_startup_separates_newer_failure_from_last_completed_analysis(
    qtbot,
    analytics_spy: AnalyticsSpy,
    session_repository: SessionRepositorySpy,
) -> None:
    active = topic_status(
        TopicAnalysisState.COMPLETED,
        completed_at="2026-08-01 07:30:00",
    )
    failed = replace(
        topic_status(TopicAnalysisState.FAILED, diagnostic="fit exploded"),
        id=2,
    )
    manager = ManagerSpy(failed, active)
    subject = InsightsView(analytics_spy, session_repository, manager, 11)
    qtbot.addWidget(subject)

    assert subject.topic_message() == "Topic analysis failed: fit exploded"
    assert subject.last_completed_message() == "Last completed 2026-08-01 07:30:00"
    assert subject.analysis_status_label.text() == "Topic analysis failed: fit exploded"
    assert subject.last_completed_label.text() == "Last completed 2026-08-01 07:30:00"


def test_unsuccessful_transitions_retain_last_completed_analysis(
    qtbot,
    analytics_spy: AnalyticsSpy,
    session_repository: SessionRepositorySpy,
) -> None:
    active = topic_status(
        TopicAnalysisState.COMPLETED,
        completed_at="2026-08-01 07:30:00",
    )
    manager = ManagerSpy(active, active)
    subject = InsightsView(analytics_spy, session_repository, manager, 11)
    qtbot.addWidget(subject)

    for state in (
        TopicAnalysisState.PENDING,
        TopicAnalysisState.RUNNING,
        TopicAnalysisState.INSUFFICIENT,
        TopicAnalysisState.FAILED,
        TopicAnalysisState.CANCELLED,
    ):
        manager.set_status(topic_status(state, diagnostic="fit exploded"))
        assert subject.last_completed_message() == "Last completed 2026-08-01 07:30:00"

    completed = replace(
        topic_status(
            TopicAnalysisState.COMPLETED,
            completed_at="2026-08-02 11:45:00",
        ),
        id=3,
    )
    manager.set_status(completed)
    assert subject.last_completed_message() == "Last completed 2026-08-02 11:45:00"


def test_startup_without_active_analysis_reports_none_completed(
    qtbot,
    analytics_spy: AnalyticsSpy,
    session_repository: SessionRepositorySpy,
) -> None:
    manager = ManagerSpy(topic_status(TopicAnalysisState.PENDING), None)
    subject = InsightsView(analytics_spy, session_repository, manager, 11)
    qtbot.addWidget(subject)

    assert subject.last_completed_message() == "No completed analysis yet"


def test_snapshot_failure_is_bounded_and_does_not_leave_stale_data(
    qtbot,
    session_repository: SessionRepositorySpy,
    manager_spy: ManagerSpy,
) -> None:
    analytics = AnalyticsSpy(RuntimeError("x" * 700))
    subject = InsightsView(analytics, session_repository, manager_spy, 1)
    qtbot.addWidget(subject)

    assert subject.data_message() == "x" * 500
    assert subject.summary_value("Unique posts") is None
