from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from xfeed.i18n import tr
from xfeed.x_session import SessionState


class PageHeader(QWidget):
    def __init__(
        self,
        title: str,
        subtitle: str,
        action: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("pageTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("pageSubtitle")
        self.subtitle_label.setWordWrap(True)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        copy.addWidget(self.title_label)
        copy.addWidget(self.subtitle_label)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(copy, 1)
        if action is not None:
            layout.addWidget(action)


class StatusBanner(QFrame):
    def __init__(self, text: str = "Ready", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statusBanner")
        self.indicator = QLabel("●")
        self.indicator.setStyleSheet("color: #20B486")
        self.label = QLabel(tr(text))
        self.label.setWordWrap(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.addWidget(self.indicator)
        layout.addWidget(self.label, 1)

    def set_text(self, text: str, *, warning: bool = False) -> None:
        self.label.setText(tr(text))
        self.indicator.setStyleSheet(f"color: {'#E19A3E' if warning else '#20B486'}")


class EmptyState(QFrame):
    def __init__(self, title: str, body: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("emptyState")
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-size: 18px; font-weight: 700; color: #202534")
        self.body_label = QLabel(body)
        self.body_label.setObjectName("mutedLabel")
        self.body_label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(7)
        layout.addWidget(self.title_label)
        layout.addWidget(self.body_label)


class ConnectionStatusWidget(QFrame):
    connect_requested = Signal()
    disconnect_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("connectionStatus")
        self.setStyleSheet(
            "QFrame#connectionStatus { background: #202738; border-radius: 12px; }"
            "QFrame#connectionStatus QLabel { color: #DDE2EE; }"
        )
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.connect_button = QPushButton(tr("Connect"))
        self.disconnect_button = QPushButton(tr("Disconnect"))
        self.connect_button.clicked.connect(self.connect_requested)
        self.disconnect_button.clicked.connect(self.disconnect_requested)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addWidget(self.connect_button)
        actions.addWidget(self.disconnect_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self.status_label)
        layout.addLayout(actions)
        self.set_session_state(SessionState.UNAVAILABLE)

    def set_session_state(self, state: SessionState) -> None:
        self.status_label.setText(
            {
                SessionState.CHECKING: tr("Checking Opera connection…"),
                SessionState.SIGNED_OUT: tr("Opera connected · X signed out"),
                SessionState.SIGNING_IN: tr("Connecting to Opera…"),
                SessionState.SIGNED_IN: tr("Opera connected · X signed in"),
                SessionState.UNAVAILABLE: tr("Opera disconnected"),
            }[state]
        )
        self.connect_button.setEnabled(state in (SessionState.SIGNED_OUT, SessionState.UNAVAILABLE))
        self.disconnect_button.setEnabled(state is SessionState.SIGNED_IN)
