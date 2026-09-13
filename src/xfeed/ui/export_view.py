from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from xfeed.db import Database
from xfeed.i18n import tr
from xfeed.session_export import SessionExportRepository, export_posts
from xfeed.session_repository import AppSessionRepository
from xfeed.ui.components import EmptyState, PageHeader


class ExportView(QWidget):
    """Selectable session list with CSV/JSON dataset download."""

    def __init__(
        self,
        database: Database,
        sessions: AppSessionRepository | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._exports = SessionExportRepository(database, sessions)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self._header = PageHeader(
            tr("Export"),
            tr("Select collected sessions and download them as a dataset."),
        )
        layout.addWidget(self._header)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self._select_all = QPushButton(tr("Select all"))
        self._select_none = QPushButton(tr("Select none"))
        self._select_all.clicked.connect(self._set_all_checked)
        self._select_none.clicked.connect(self._set_none_checked)
        tools.addWidget(self._select_all)
        tools.addWidget(self._select_none)
        tools.addStretch(1)
        layout.addLayout(tools)

        self._empty = EmptyState(
            tr("No collected sessions yet"),
            tr("Collect a feed or save a post, then return here to export."),
        )
        layout.addWidget(self._empty)

        self._list = QListWidget()
        self._list.setObjectName("exportSessionList")
        self._list.itemChanged.connect(self._update_export_enabled)
        layout.addWidget(self._list, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.addStretch(1)
        self._status = QLabel()
        self._status.setObjectName("mutedLabel")
        self._status.setVisible(False)
        bottom.addWidget(self._status, 1)
        self._export_button = QPushButton(tr("Export…"))
        self._export_button.setObjectName("primaryAction")
        self._export_button.clicked.connect(self._on_export_clicked)
        bottom.addWidget(self._export_button)
        layout.addLayout(bottom)

        self.refresh()

    def refresh(self) -> None:
        checked = set(self.selected_session_ids())
        first_load = self._list.count() == 0 and not checked
        self._list.blockSignals(True)
        try:
            self._list.clear()
            for entry in self._exports.exportable_sessions():
                item = QListWidgetItem(
                    tr(
                        "Session {session}: {posts} posts",
                        session=entry.session.opened_at,
                        posts=entry.post_count,
                    )
                )
                item.setData(Qt.ItemDataRole.UserRole, entry.session.id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if (entry.session.id in checked or first_load)
                    else Qt.CheckState.Unchecked
                )
                self._list.addItem(item)
        finally:
            self._list.blockSignals(False)
        has_sessions = self._list.count() > 0
        self._empty.setVisible(not has_sessions)
        self._list.setVisible(has_sessions)
        self._update_export_enabled()

    def apply_language(self) -> None:
        self._header.title_label.setText(tr("Export"))
        self._header.subtitle_label.setText(
            tr("Select collected sessions and download them as a dataset.")
        )
        self._select_all.setText(tr("Select all"))
        self._select_none.setText(tr("Select none"))
        self._empty.title_label.setText(tr("No collected sessions yet"))
        self._empty.body_label.setText(
            tr("Collect a feed or save a post, then return here to export.")
        )
        self._export_button.setText(tr("Export…"))
        self.refresh()

    def selected_session_ids(self) -> tuple[int, ...]:
        selected: list[int] = []
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                value = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(value, int):
                    selected.append(value)
        return tuple(selected)

    def export_selected(self, destination: Path, kind: str) -> int:
        selected = self.selected_session_ids()
        if not selected:
            raise ValueError("Select at least one session")
        posts = self._exports.collect_posts(selected)
        export_posts(posts, destination, kind)
        return len(posts)

    def _set_all_checked(self) -> None:
        self._set_checked(Qt.CheckState.Checked)

    def _set_none_checked(self) -> None:
        self._set_checked(Qt.CheckState.Unchecked)

    def _set_checked(self, state: Qt.CheckState) -> None:
        self._list.blockSignals(True)
        try:
            for index in range(self._list.count()):
                self._list.item(index).setCheckState(state)
        finally:
            self._list.blockSignals(False)
        self._update_export_enabled()

    def _update_export_enabled(self) -> None:
        self._export_button.setEnabled(len(self.selected_session_ids()) > 0)

    def _on_export_clicked(self) -> None:
        selected = self.selected_session_ids()
        if not selected:
            self._show_status(tr("Select at least one session"))
            return
        filename, selected_filter = QFileDialog.getSaveFileName(
            self,
            tr("Export dataset"),
            "xfeed-export",
            "CSV (*.csv);;JSON (*.json)",
        )
        if not filename:
            return
        kind = "json" if "json" in selected_filter.casefold() else "csv"
        destination = Path(filename)
        if not destination.suffix:
            destination = destination.with_suffix(f".{kind}")
        elif destination.suffix.casefold() not in (".csv", ".json"):
            kind = "json" if destination.suffix.casefold() == ".json" else "csv"
        try:
            count = self.export_selected(destination, kind)
        except (OSError, ValueError) as error:
            self._show_status(tr("Export failed: {detail}", detail=error))
            return
        self._show_status(
            tr("Exported {count} posts to {name}", count=count, name=destination.name)
        )

    def _show_status(self, message: str) -> None:
        self._status.setText(message)
        self._status.setVisible(True)
