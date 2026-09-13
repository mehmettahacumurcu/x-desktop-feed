from collections.abc import Mapping

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from xfeed.i18n import LANGUAGES, language_name, set_language, tr
from xfeed.ui.components import ConnectionStatusWidget
from xfeed.ui.settings import UiSettingsStore
from xfeed.x_session import SessionState


class MainWindow(QMainWindow):
    connect_requested = Signal()
    disconnect_requested = Signal()
    language_changed = Signal()
    DESTINATIONS = ("My Feed", "Sources", "Insights", "Export")

    def __init__(
        self,
        pages: Mapping[str, QWidget] | None = None,
        *,
        settings: UiSettingsStore | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self.setWindowTitle("X Desktop Feed")
        self.setMinimumSize(960, 640)
        width, height = settings.window_size() if settings is not None else (1180, 760)
        self.resize(width, height)

        self._navigation = QListWidget()
        self._navigation.setObjectName("primaryNavigation")
        self._navigation.addItems([tr(name) for name in self.DESTINATIONS])
        self._navigation.setSpacing(2)
        self._pages = QStackedWidget()
        injected_pages = pages or {}
        for name in self.DESTINATIONS:
            placeholder = QLabel(
                tr(
                    "Insights will arrive after the collection experience is settled."
                    if name == "Insights"
                    else name
                )
            )
            placeholder.setAlignment(placeholder.alignment())
            self._pages.addWidget(injected_pages.get(name, placeholder))
        self._navigation.currentRowChanged.connect(self._pages.setCurrentIndex)
        self._navigation.setCurrentRow(0)

        brand = QLabel("X Desktop Feed")
        brand.setObjectName("brandMark")
        self._caption = QLabel(tr("Your focused reading space"))
        self._caption.setObjectName("brandCaption")
        self.connection_status = ConnectionStatusWidget()
        self.connection_status.connect_requested.connect(self.connect_requested)
        self.connection_status.disconnect_requested.connect(self.disconnect_requested)

        language_row = QHBoxLayout()
        self._language_label = QLabel(tr("Language"))
        self._language_label.setObjectName("brandCaption")
        self.language_combo = QComboBox()
        self.language_combo.setObjectName("languageSelector")
        for code in LANGUAGES:
            self.language_combo.addItem(language_name(code), code)
        current = settings.language() if settings is not None else "en"
        self.language_combo.setCurrentIndex(self.language_combo.findData(current))
        self.language_combo.currentIndexChanged.connect(self._language_changed)
        language_row.addWidget(self._language_label)
        language_row.addWidget(self.language_combo, 1)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(232)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(18, 24, 18, 18)
        sidebar_layout.setSpacing(10)
        sidebar_layout.addWidget(brand)
        sidebar_layout.addWidget(self._caption)
        sidebar_layout.addSpacing(22)
        sidebar_layout.addWidget(self._navigation, 1)
        sidebar_layout.addLayout(language_row)
        sidebar_layout.addWidget(self.connection_status)

        workspace = QWidget()
        workspace.setObjectName("workspace")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(28, 24, 28, 24)
        workspace_layout.addWidget(self._pages)

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(sidebar)
        layout.addWidget(workspace, 1)
        self.setCentralWidget(container)

    def destination_names(self) -> tuple[str, ...]:
        return self.DESTINATIONS

    def navigate(self, name: str) -> None:
        if name not in self.DESTINATIONS:
            raise ValueError(f"Unknown destination: {name}")
        self._navigation.setCurrentRow(self.DESTINATIONS.index(name))

    def current_destination(self) -> str:
        return self.DESTINATIONS[self._pages.currentIndex()]

    def page(self, name: str) -> QWidget:
        if name not in self.DESTINATIONS:
            raise ValueError(f"Unknown destination: {name}")
        return self._pages.widget(self.DESTINATIONS.index(name))

    def set_session_state(self, state: SessionState) -> None:
        self.connection_status.set_session_state(state)

    def save_ui_state(self) -> None:
        if self._settings is not None:
            self._settings.set_window_size(self.width(), self.height())

    def _language_changed(self, index: int) -> None:
        code = self.language_combo.itemData(index)
        if code not in LANGUAGES:
            return
        set_language(code)
        if self._settings is not None:
            self._settings.set_language(code)
        self._refresh_language()

    def _refresh_language(self) -> None:
        for row in range(self._navigation.count()):
            item = self._navigation.item(row)
            item.setText(tr(self.DESTINATIONS[row]))
        self._caption.setText(tr("Your focused reading space"))
        self._language_label.setText(tr("Language"))
        for index, name in enumerate(self.DESTINATIONS):
            widget = self._pages.widget(index)
            if isinstance(widget, QLabel):
                widget.setText(
                    tr("Insights will arrive after the collection experience is settled.")
                    if name == "Insights"
                    else tr(name)
                )
        self.language_changed.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.save_ui_state()
        super().closeEvent(event)
