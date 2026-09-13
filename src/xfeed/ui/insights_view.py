from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast

from PySide6.QtCore import QPointF, QRectF, Qt, SignalInstance
from PySide6.QtGui import QColor, QFont, QPaintEvent, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from xfeed.analytics import (
    AnalyticsDataset,
    AnalyticsDatasetKind,
    AnalyticsFeedScope,
    AnalyticsQuery,
    Distribution,
    DistributionItem,
    InsightsSnapshot,
    SessionTrendPoint,
)
from xfeed.domain import AppSession
from xfeed.i18n import tr
from xfeed.topic_analysis import TopicAnalysisRun, TopicAnalysisState


class _AnalyticsReader(Protocol):
    def snapshot(self, query: AnalyticsQuery) -> InsightsSnapshot: ...


class _SessionReader(Protocol):
    def visible(self) -> tuple[AppSession, ...]: ...


class _TopicManager(Protocol):
    status_changed: SignalInstance

    def status(self) -> TopicAnalysisRun | None: ...

    def active_run(self) -> TopicAnalysisRun | None: ...

    def request_analysis(self, *, force: bool = False) -> None: ...


class SessionTrendChart(QWidget):
    """Small deterministic, native chart for multi-session comparisons."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sessionTrend")
        self.setMinimumHeight(176)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(tr("Posts per session trend"))
        self.points: tuple[SessionTrendPoint, ...] = ()

    def set_points(self, points: tuple[SessionTrendPoint, ...]) -> None:
        self.points = points
        self.setAccessibleDescription(
            "; ".join(
                tr("Session {session}: {posts} unique posts, {appearances} appearances").format(
                    session=point.session_id,
                    posts=point.unique_posts,
                    appearances=point.captured_appearances,
                )
                for point in points
            )
        )
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        super().paintEvent(event)
        if not self.points:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        chart = QRectF(44.0, 20.0, max(1.0, self.width() - 62.0), 112.0)
        maximum = max(
            1,
            *(max(point.unique_posts, point.captured_appearances) for point in self.points),
        )

        painter.setPen(QPen(QColor("#E0E5ED"), 1.0))
        for step in range(3):
            y = chart.top() + chart.height() * step / 2
            painter.drawLine(QPointF(chart.left(), y), QPointF(chart.right(), y))

        self._draw_series(
            painter,
            chart,
            maximum,
            lambda point: point.captured_appearances,
            QColor("#AEB5C7"),
            1.5,
        )
        self._draw_series(
            painter,
            chart,
            maximum,
            lambda point: point.unique_posts,
            QColor("#6D5DFC"),
            2.5,
        )

        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(QColor("#697386"))
        for index, point in enumerate(self.points):
            x = self._x(chart, index)
            if len(self.points) <= 6 or index in {0, len(self.points) - 1}:
                label = point.opened_at.split(" ", maxsplit=1)[0]
                painter.drawText(
                    QRectF(x - 40.0, chart.bottom() + 10.0, 80.0, 20.0),
                    Qt.AlignmentFlag.AlignHCenter,
                    label,
                )
        painter.end()

    def _draw_series(
        self,
        painter: QPainter,
        chart: QRectF,
        maximum: int,
        value: Callable[[SessionTrendPoint], int],
        color: QColor,
        width: float,
    ) -> None:
        series_points = [
            QPointF(
                self._x(chart, index),
                chart.bottom() - chart.height() * value(point) / maximum,
            )
            for index, point in enumerate(self.points)
        ]
        points = QPolygonF(series_points)
        painter.setPen(QPen(color, width))
        painter.setBrush(color)
        if len(series_points) > 1:
            painter.drawPolyline(points)
        for point in series_points:
            painter.drawEllipse(point, 3.0, 3.0)

    def _x(self, chart: QRectF, index: int) -> float:
        if len(self.points) == 1:
            return chart.center().x()
        return chart.left() + chart.width() * index / (len(self.points) - 1)


class InsightsView(QScrollArea):
    """Scrollable reading audit over repository-owned analytics snapshots."""

    _DATASETS = (
        ("Current session", AnalyticsDatasetKind.CURRENT_SESSION),
        ("Past session", AnalyticsDatasetKind.PAST_SESSION),
        ("All feed sessions", AnalyticsDatasetKind.ALL_FEED_SESSIONS),
        ("All saved posts", AnalyticsDatasetKind.ALL_SAVED_POSTS),
    )
    _FEED_SCOPES = (
        ("Combined", AnalyticsFeedScope.COMBINED),
        ("For You", AnalyticsFeedScope.FOR_YOU),
        ("Following", AnalyticsFeedScope.FOLLOWING),
    )

    def __init__(
        self,
        analytics: _AnalyticsReader,
        sessions: _SessionReader,
        topic_manager: _TopicManager,
        current_session_id: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._analytics = analytics
        self._session_repository = sessions
        self._topic_manager = topic_manager
        self._current_session_id = current_session_id
        self._snapshot: InsightsSnapshot | None = None
        self._topic_status = self._read_initial_topic_status()
        self._topic_message = self._topic_status_text(self._topic_status)
        self._active_topic_run = self._read_initial_active_run()
        self._last_completed = self._last_completed_text(self._active_topic_run)
        self._data_message = ""
        self._summary_values: dict[str, QLabel] = {}
        self._distribution_values: dict[str, tuple[tuple[str, str], ...]] = {}
        self._sections: dict[str, QWidget] = {}
        self._topic_message_label: QLabel | None = None

        self.setObjectName("insightsView")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("insightsCanvas")
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(0, 0, 8, 0)
        self._content_layout.setSpacing(14)
        self._content_layout.addWidget(self._build_header())
        self._content_layout.addWidget(self._build_filter_band())
        self._body = QWidget()
        self._body.setObjectName("insightsBody")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(14)
        self._content_layout.addWidget(self._body)
        self._content_layout.addStretch()
        self.setWidget(content)

        self._populate_sessions()
        self._dataset.currentIndexChanged.connect(self._dataset_changed)
        self._sessions.currentIndexChanged.connect(self._session_changed)
        self._feed_scope.currentIndexChanged.connect(self._feed_scope_changed)
        self.reanalyze_button.clicked.connect(self._reanalyze)
        topic_manager.status_changed.connect(self._status_changed)
        self._update_session_visibility()
        self.refresh()

    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QVBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self._header_title = QLabel(tr("Insights"))
        self._header_title.setObjectName("pageTitle")
        self._header_subtitle = QLabel(tr("A local audit of what your feeds contained."))
        self._header_subtitle.setObjectName("pageSubtitle")
        layout.addWidget(self._header_title)
        layout.addWidget(self._header_subtitle)
        return header

    def _build_filter_band(self) -> QWidget:
        band = QFrame()
        band.setObjectName("insightsFilterBand")
        layout = QGridLayout(band)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(8)

        self._dataset = QComboBox()
        self._dataset.setObjectName("insightsDataset")
        self._dataset.setAccessibleName(tr("Insights dataset"))
        self._configure_filter_combo(self._dataset)
        for label, kind in self._DATASETS:
            self._dataset.addItem(tr(label), kind)
        dataset_group, self._dataset_label = self._labeled_control(tr("Dataset"), self._dataset)

        self._sessions = QComboBox()
        self._sessions.setObjectName("insightsPastSession")
        self._sessions.setAccessibleName(tr("Past feed session"))
        self._configure_filter_combo(self._sessions)
        self.past_session_selector = self._sessions
        self._session_group, session_label = self._labeled_control(tr("Session"), self._sessions)
        self._session_label = session_label

        self._feed_scope = QComboBox()
        self._feed_scope.setObjectName("insightsFeedScope")
        self._feed_scope.setAccessibleName(tr("Feed scope"))
        self._configure_filter_combo(self._feed_scope)
        for label, scope in self._FEED_SCOPES:
            self._feed_scope.addItem(tr(label), scope)
        feed_group, self._feed_label = self._labeled_control(tr("Feed"), self._feed_scope)

        self._status_caption = QLabel(tr("Topic analysis"))
        self._status_caption.setObjectName("controlLabel")
        self.analysis_status_label = QLabel(self._topic_message)
        self.analysis_status_label.setObjectName("analysisStatus")
        self.analysis_status_label.setWordWrap(True)
        self.analysis_status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.last_completed_label = QLabel(self._last_completed)
        self.last_completed_label.setObjectName("lastCompletedStatus")
        self.last_completed_label.setWordWrap(True)
        self.last_completed_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.reanalyze_button = QPushButton(tr("Reanalyze"))
        self.reanalyze_button.setObjectName("reanalyzeButton")
        self.reanalyze_button.setAccessibleDescription(
            tr("Run local topic analysis again, even when saved posts have not changed")
        )

        status_group = QWidget()
        status_layout = QVBoxLayout(status_group)
        status_layout.setContentsMargins(0, 2, 0, 0)
        status_layout.setSpacing(3)
        status_layout.addWidget(self._status_caption)
        status_layout.addWidget(self.analysis_status_label)
        status_layout.addWidget(self.last_completed_label)

        layout.addWidget(dataset_group, 0, 0)
        layout.addWidget(self._session_group, 0, 1)
        layout.addWidget(feed_group, 1, 0)
        layout.addWidget(
            self.reanalyze_button,
            1,
            1,
            alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
        )
        layout.addWidget(status_group, 2, 0, 1, 2)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return band

    @staticmethod
    def _configure_filter_combo(combo: QComboBox) -> None:
        combo.setMinimumWidth(0)
        combo.setMinimumContentsLength(14)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    @staticmethod
    def _labeled_control(caption: str, control: QWidget) -> tuple[QWidget, QLabel]:
        group = QWidget()
        group.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        label = QLabel(caption)
        label.setObjectName("controlLabel")
        label.setBuddy(control)
        layout.addWidget(label)
        layout.addWidget(control)
        return group, label

    def _populate_sessions(self) -> None:
        self._sessions.clear()
        for session in self._session_repository.visible():
            state = session.state.value.capitalize()
            self._sessions.addItem(
                f"{session.opened_at}  ·  {state}",
                session.id,
            )
        self._sessions.setEnabled(self._sessions.count() > 0)

    def _read_initial_topic_status(self) -> TopicAnalysisRun | None:
        status = self._topic_manager.status()
        return status if isinstance(status, TopicAnalysisRun) else None

    def _read_initial_active_run(self) -> TopicAnalysisRun | None:
        active = self._topic_manager.active_run()
        return active if isinstance(active, TopicAnalysisRun) else None

    def _query(self) -> AnalyticsQuery:
        kind = self._selected_dataset_kind()
        if kind is AnalyticsDatasetKind.CURRENT_SESSION:
            dataset = AnalyticsDataset.current_session(self._current_session_id)
        elif kind is AnalyticsDatasetKind.PAST_SESSION:
            session_id = self._sessions.currentData()
            if not isinstance(session_id, int):
                raise ValueError("No past feed sessions are available")
            dataset = AnalyticsDataset.session(session_id)
        elif kind is AnalyticsDatasetKind.ALL_FEED_SESSIONS:
            dataset = AnalyticsDataset.all_feed_sessions()
        else:
            dataset = AnalyticsDataset.all_saved_posts()
        scope = self._selected_feed_scope()
        return AnalyticsQuery(dataset, scope)

    def _selected_dataset_kind(self) -> AnalyticsDatasetKind:
        return AnalyticsDatasetKind(self._dataset.currentData())

    def _selected_feed_scope(self) -> AnalyticsFeedScope:
        return AnalyticsFeedScope(self._feed_scope.currentData())

    def refresh(self) -> None:
        try:
            snapshot = self._analytics.snapshot(self._query())
        except Exception as error:
            self._show_error(tr(str(error)[:500] or "Insights could not be loaded"))
            return
        self.render_snapshot(snapshot)

    def render_snapshot(self, snapshot: InsightsSnapshot) -> None:
        self._snapshot = snapshot
        self._clear_body()
        if snapshot.unique_posts == 0:
            self._show_message(tr("No posts match these filters"), error=False)
            return

        self._data_message = ""
        self._body_layout.addWidget(self._build_summary(snapshot))
        topic = self._build_distribution_section(
            tr("Primary topics"),
            tr("Primary assignment per unique post"),
            snapshot.topics,
            signature=True,
            message=self._topic_message,
        )
        self._body_layout.addWidget(topic)
        self._body_layout.addWidget(
            self._build_distribution_section(
                tr("Post kinds"),
                tr("Latest observation per unique post"),
                snapshot.post_kinds,
            )
        )
        self._body_layout.addWidget(
            self._build_distribution_section(
                tr("Top authors"),
                tr("Unique posts by author"),
                snapshot.authors,
            )
        )
        if self._selected_feed_scope() is AnalyticsFeedScope.COMBINED:
            self._body_layout.addWidget(
                self._build_distribution_section(
                    tr("Feed balance"),
                    tr("Where unique posts appeared"),
                    snapshot.feed_balance,
                )
            )
        if snapshot.trend:
            self._body_layout.addWidget(self._build_trend(snapshot.trend))

    def _build_summary(self, snapshot: InsightsSnapshot) -> QWidget:
        frame = QFrame()
        frame.setObjectName("summaryStrip")
        layout = QGridLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        items = [
            (tr("Unique posts"), str(snapshot.unique_posts)),
            (tr("Unique authors"), str(snapshot.unique_authors)),
            (tr("With media"), f"{snapshot.media_percentage:.1f}%"),
        ]
        if snapshot.new_posts is not None and snapshot.already_saved is not None:
            items.extend(
                (
                    (tr("New posts"), str(snapshot.new_posts)),
                    (tr("Already saved"), str(snapshot.already_saved)),
                )
            )
        columns = 4 if len(items) == 4 else 3
        for index, (label, value) in enumerate(items):
            card = QFrame()
            card.setObjectName("summaryCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 10, 14, 11)
            card_layout.setSpacing(1)
            caption = QLabel(label)
            caption.setObjectName("summaryLabel")
            number = QLabel(value)
            number.setObjectName("summaryValue")
            number.setAccessibleName(f"{label}: {value}")
            card_layout.addWidget(caption)
            card_layout.addWidget(number)
            layout.addWidget(card, index // columns, index % columns)
            self._summary_values[label] = number
        return frame

    def _build_distribution_section(
        self,
        title: str,
        subtitle: str,
        distribution: Distribution,
        *,
        signature: bool = False,
        message: str | None = None,
    ) -> QWidget:
        card = QFrame()
        card.setObjectName("topicSpectrum" if signature else "insightsSection")
        card.setProperty("signature", signature)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(17, 14, 17, 16)
        layout.setSpacing(8)
        title_label = QLabel(title)
        title_label.setObjectName("sectionTitle")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("sectionSubtitle")
        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        if message is not None:
            message_label = QLabel(message)
            message_label.setObjectName("topicMessage")
            message_label.setWordWrap(True)
            layout.addWidget(message_label)
            self._topic_message_label = message_label

        maximum = max((item.count for item in distribution), default=1)
        rendered_rows: list[tuple[str, str]] = []
        for item in distribution:
            row, rendered = self._bar_row(item, maximum, signature=signature)
            layout.addWidget(row)
            rendered_rows.append((item.label, rendered))
        if not distribution:
            empty = QLabel(tr("No distribution data for this selection"))
            empty.setObjectName("mutedLabel")
            layout.addWidget(empty)
        self._distribution_values[title] = tuple(rendered_rows)
        self._sections[title] = card
        return card

    @staticmethod
    def _bar_row(
        item: DistributionItem,
        maximum: int,
        *,
        signature: bool,
    ) -> tuple[QWidget, str]:
        row = QWidget()
        row.setObjectName("distributionRow")
        layout = QGridLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setHorizontalSpacing(12)
        label = QLabel(item.label)
        label.setObjectName("distributionLabel")
        label.setMinimumWidth(138)
        bar = QProgressBar()
        bar.setObjectName("topicBar" if signature else "distributionBar")
        bar.setRange(0, max(1, maximum))
        bar.setValue(item.count)
        rendered = f"{item.count}  ·  {item.percentage:.1f}%"
        bar.setFormat(rendered)
        bar.setTextVisible(True)
        bar.setAccessibleName(f"{item.label}: {rendered}")
        layout.addWidget(label, 0, 0)
        layout.addWidget(bar, 0, 1)
        layout.setColumnStretch(1, 1)
        return row, rendered

    def _build_trend(self, points: tuple[SessionTrendPoint, ...]) -> QWidget:
        card = QFrame()
        card.setObjectName("insightsSection")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(17, 14, 17, 14)
        layout.setSpacing(5)
        title = QLabel(tr("Session trend"))
        title.setObjectName("sectionTitle")
        subtitle = QLabel(tr("Unique posts in violet; captured appearances in gray"))
        subtitle.setObjectName("sectionSubtitle")
        chart = SessionTrendChart()
        chart.set_points(points)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(chart)
        self._sections["Session trend"] = card
        return card

    def _clear_body(self) -> None:
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._summary_values.clear()
        self._distribution_values.clear()
        self._sections.clear()
        self._topic_message_label = None

    def _show_error(self, message: str) -> None:
        self._snapshot = None
        self._clear_body()
        self._show_message(message, error=True)

    def _show_message(self, message: str, *, error: bool) -> None:
        self._data_message = message
        state = QFrame()
        state.setObjectName("errorState" if error else "emptyState")
        layout = QVBoxLayout(state)
        layout.setContentsMargins(18, 20, 18, 20)
        title = QLabel(message)
        title.setObjectName("stateTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        self._body_layout.addWidget(state)

    def _topic_status_text(self, status: TopicAnalysisRun | None) -> str:
        if status is None:
            return tr("Topics have not been analyzed yet")
        match status.state:
            case TopicAnalysisState.PENDING | TopicAnalysisState.RUNNING:
                return tr("Analyzing saved posts locally…")
            case TopicAnalysisState.INSUFFICIENT:
                return tr("Not enough text for reliable topics")
            case TopicAnalysisState.FAILED:
                diagnostic = (status.diagnostic or tr("No diagnostic was provided"))[:500]
                return tr("Topic analysis failed: {detail}").format(detail=diagnostic)
            case TopicAnalysisState.COMPLETED:
                return tr("Topics updated {time}").format(
                    time=status.completed_at or tr("recently")
                )
            case TopicAnalysisState.CANCELLED:
                return tr("Topic analysis was interrupted")

    @staticmethod
    def _last_completed_text(active: TopicAnalysisRun | None) -> str:
        if active is None or active.completed_at is None:
            return tr("No completed analysis yet")
        return tr("Last completed {time}").format(time=active.completed_at)

    def _status_changed(self, value: object) -> None:
        if not isinstance(value, TopicAnalysisRun):
            return
        self._topic_status = value
        self._topic_message = self._topic_status_text(value)
        self.analysis_status_label.setText(self._topic_message)
        if self._topic_message_label is not None:
            self._topic_message_label.setText(self._topic_message)
        if value.state is TopicAnalysisState.COMPLETED and value.is_active:
            self._active_topic_run = value
            self._last_completed = self._last_completed_text(value)
            self.last_completed_label.setText(self._last_completed)

    def apply_language(self) -> None:
        self._header_title.setText(tr("Insights"))
        self._header_subtitle.setText(tr("A local audit of what your feeds contained."))
        self._dataset_label.setText(tr("Dataset"))
        self._session_label.setText(tr("Session"))
        self._feed_label.setText(tr("Feed"))
        self._status_caption.setText(tr("Topic analysis"))
        self.reanalyze_button.setText(tr("Reanalyze"))
        self.reanalyze_button.setAccessibleDescription(
            tr("Run local topic analysis again, even when saved posts have not changed")
        )
        for index, (label, kind) in enumerate(self._DATASETS):
            self._dataset.setItemText(index, tr(label))
        for index, (label, scope) in enumerate(self._FEED_SCOPES):
            self._feed_scope.setItemText(index, tr(label))
        self._populate_sessions()
        self.refresh()

    def _dataset_changed(self, _index: int) -> None:
        self._update_session_visibility()
        if (
            self._selected_dataset_kind() is AnalyticsDatasetKind.PAST_SESSION
            and self._sessions.count() == 0
        ):
            self._show_error(tr("No past feed sessions are available"))
            return
        self.refresh()

    def _session_changed(self, _index: int) -> None:
        if self._selected_dataset_kind() is AnalyticsDatasetKind.PAST_SESSION:
            self.refresh()

    def _feed_scope_changed(self, _index: int) -> None:
        self.refresh()

    def _update_session_visibility(self) -> None:
        visible = self._selected_dataset_kind() is AnalyticsDatasetKind.PAST_SESSION
        self._session_group.setVisible(visible)

    def _reanalyze(self, _checked: bool = False) -> None:
        self._topic_manager.request_analysis(force=True)

    def select_dataset(self, kind: AnalyticsDatasetKind) -> None:
        index = self._dataset.findData(kind)
        if index < 0:
            raise ValueError(f"Unknown analytics dataset: {kind}")
        if index == self._dataset.currentIndex():
            self.refresh()
        else:
            self._dataset.setCurrentIndex(index)

    def select_feed_scope(self, scope: AnalyticsFeedScope) -> None:
        index = self._feed_scope.findData(scope)
        if index < 0:
            raise ValueError(f"Unknown feed scope: {scope}")
        if index == self._feed_scope.currentIndex():
            self.refresh()
        else:
            self._feed_scope.setCurrentIndex(index)

    def session_ids(self) -> tuple[int, ...]:
        return tuple(
            cast(int, self._sessions.itemData(index)) for index in range(self._sessions.count())
        )

    def summary_value(self, label: str) -> str | None:
        value = self._summary_values.get(label)
        return None if value is None else value.text()

    def topic_message(self) -> str:
        return self._topic_message

    def last_completed_message(self) -> str:
        return self._last_completed

    def data_message(self) -> str:
        return self._data_message

    def distribution_rows(self, title: str) -> tuple[tuple[str, str], ...]:
        return self._distribution_values.get(title, ())

    def section_is_visible(self, title: str) -> bool:
        section = self._sections.get(title)
        return section is not None and not section.isHidden()
