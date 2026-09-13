from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from xfeed.i18n import tr
from xfeed.sources import SourceRecord
from xfeed.ui.settings import SOURCE_COUNTS, UiSettingsStore


class SourceCard(QFrame):
    selection_changed = Signal(int, bool)
    collect_requested = Signal(object, int)
    enabled_requested = Signal(int, bool)
    remove_requested = Signal(int)

    def __init__(
        self,
        source: SourceRecord,
        settings: UiSettingsStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.source = source
        self.settings = settings
        self.setObjectName("sourceCard")
        self.setMinimumWidth(280)
        self.setProperty("muted", not source.enabled)
        self.setStyleSheet(
            "QFrame#sourceCard { background: #FFFFFF; border: 1px solid #E0E5ED; "
            "border-radius: 13px; }"
            'QFrame#sourceCard[muted="true"] { background: #F2F4F7; color: #8B93A3; }'
        )

        self.selected_checkbox = QCheckBox()
        self.handle_label = QLabel(f"@{source.handle}")
        self.handle_label.setStyleSheet("font-size: 16px; font-weight: 700")
        state_text = tr("Enabled") if source.enabled else tr("Disabled")
        self.state_label = QLabel(state_text)
        self.state_label.setObjectName("mutedLabel")
        self.primary_row = QHBoxLayout()
        self.primary_row.addWidget(self.selected_checkbox)
        self.primary_row.addWidget(self.handle_label, 1)
        self.primary_row.addWidget(self.state_label)

        refresh_copy = (
            tr("Last collected {time}").format(time=source.last_refresh_at)
            if source.last_refresh_at
            else tr("No collection yet")
        )
        self.last_refresh_label = QLabel(refresh_copy)
        self.last_refresh_label.setObjectName("mutedLabel")

        self.amount_combo = QComboBox()
        for value in SOURCE_COUNTS:
            self.amount_combo.addItem(str(value), value)
        self.amount_combo.setCurrentIndex(
            self.amount_combo.findData(settings.source_count(source.id))
        )
        self.collect_button = QPushButton(tr("Collect"))
        self.collect_button.setProperty("primary", True)
        self.collect_button.setEnabled(source.enabled)
        self.action_row = QHBoxLayout()
        self.action_row.addWidget(QLabel(tr("Latest")))
        self.action_row.addWidget(self.amount_combo)
        self.action_row.addWidget(self.collect_button, 1)

        self.enabled_button = QPushButton(tr("Disable") if source.enabled else tr("Enable"))
        self.remove_button = QPushButton(tr("Remove"))
        self.remove_button.setProperty("danger", True)
        secondary_row = QHBoxLayout()
        secondary_row.addWidget(self.enabled_button)
        secondary_row.addStretch()
        secondary_row.addWidget(self.remove_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 13, 14, 13)
        layout.setSpacing(10)
        layout.addLayout(self.primary_row)
        layout.addWidget(self.last_refresh_label)
        layout.addLayout(self.action_row)
        layout.addLayout(secondary_row)

        self.selected_checkbox.toggled.connect(
            lambda selected: self.selection_changed.emit(source.id, selected)
        )
        self.amount_combo.currentIndexChanged.connect(self._amount_changed)
        self.collect_button.clicked.connect(self._collect)
        self.enabled_button.clicked.connect(
            lambda: self.enabled_requested.emit(source.id, not source.enabled)
        )
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(source.id))

    def set_selected(self, selected: bool) -> None:
        blocked = self.selected_checkbox.blockSignals(True)
        self.selected_checkbox.setChecked(selected)
        self.selected_checkbox.blockSignals(blocked)

    def set_collection_locked(self, locked: bool) -> None:
        self.collect_button.setEnabled(self.source.enabled and not locked)
        self.amount_combo.setEnabled(not locked)
        self.enabled_button.setEnabled(not locked)
        self.remove_button.setEnabled(not locked)

    def _amount_changed(self, _index: int) -> None:
        value = self.amount_combo.currentData()
        if isinstance(value, int):
            self.settings.set_source_count(self.source.id, value)

    def _collect(self) -> None:
        maximum = self.amount_combo.currentData()
        if isinstance(maximum, int):
            self.collect_requested.emit(self.source, maximum)
