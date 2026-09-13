from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from xfeed.edge_session import EdgeXSession
from xfeed.i18n import tr
from xfeed.x_session import SessionState, XSession


class XLoginDialog(QDialog):
    signed_in = Signal()
    cancelled = Signal()

    def __init__(
        self,
        session: EdgeXSession | XSession,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._closed = False
        self._torn_down = False
        self._signed_in_completed = False
        self._cancelled_emitted = False

        self.setWindowTitle(tr("Sign in to X"))
        self.resize(560, 240)

        self.instructions = QLabel(
            tr(
                "Sign in manually in the dedicated Edge window. Return here when X is ready, "
                "then check the login status."
            )
        )
        self.instructions.setWordWrap(True)
        self.status_label = QLabel(
            tr("Complete sign-in in the dedicated Edge window, then check again here.")
        )
        self.status_label.setWordWrap(True)

        self.open_button = QPushButton(tr("Open X in Edge"))
        self.check_button = QPushButton(tr("Check login status"))
        self.cancel_button = QPushButton(tr("Cancel"))

        button_row = QHBoxLayout()
        button_row.addWidget(self.open_button)
        button_row.addWidget(self.check_button)
        button_row.addStretch()
        button_row.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.instructions)
        layout.addWidget(self.status_label)
        layout.addStretch()
        layout.addLayout(button_row)

        self._open_callback = self._session.begin_login
        self._check_callback = self._session.verify
        self._cancel_callback = self.reject
        self._state_callback = self._on_state_changed
        self._verification_callback = self._on_verification_finished
        self.open_button.clicked.connect(self._open_callback)
        self.check_button.clicked.connect(self._check_callback)
        self.cancel_button.clicked.connect(self._cancel_callback)
        self._session.state_changed.connect(self._state_callback)
        self._session.verification_finished.connect(self._verification_callback)

        self._session.begin_login()

    def reject(self) -> None:
        if self._closed:
            return
        self._finish_cancelled()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._closed:
            self._finish_cancelled()
        super().closeEvent(event)

    def _on_state_changed(self, state: object) -> None:
        if self._closed or not isinstance(state, SessionState):
            return
        messages = {
            SessionState.CHECKING: tr("Checking the login status in the dedicated Edge window..."),
            SessionState.SIGNED_OUT: tr(
                "X is not signed in in the dedicated Edge window. Finish sign-in there, "
                "then check again."
            ),
            SessionState.SIGNING_IN: tr(
                "Complete sign-in in the dedicated Edge window, then check again here."
            ),
            SessionState.SIGNED_IN: tr("The dedicated Edge window is signed in to X."),
            SessionState.UNAVAILABLE: tr(
                "Microsoft Edge is unavailable. Install or reopen Edge, then try again."
            ),
        }
        self.status_label.setText(messages[state])

    def _on_verification_finished(self, state: object) -> None:
        if self._closed or not isinstance(state, SessionState):
            return
        self._on_state_changed(state)
        if state is SessionState.SIGNED_IN:
            self._finish_signed_in()

    def _finish_signed_in(self) -> None:
        if self._closed or self._signed_in_completed:
            return
        self._signed_in_completed = True
        self._teardown()
        self.signed_in.emit()
        super().accept()

    def _finish_cancelled(self) -> None:
        if self._signed_in_completed or self._cancelled_emitted:
            return
        self._cancelled_emitted = True
        self._teardown()
        self.cancelled.emit()

    def _teardown(self) -> None:
        if self._torn_down:
            return
        self._torn_down = True
        self._closed = True

        for signal, callback in (
            (self.open_button.clicked, self._open_callback),
            (self.check_button.clicked, self._check_callback),
            (self.cancel_button.clicked, self._cancel_callback),
            (self._session.state_changed, self._state_callback),
            (self._session.verification_finished, self._verification_callback),
        ):
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
