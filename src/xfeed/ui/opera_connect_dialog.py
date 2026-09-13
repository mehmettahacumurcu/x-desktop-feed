from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from xfeed.i18n import tr
from xfeed.opera_session import OperaXSession
from xfeed.x_session import SessionState


class OperaConnectDialog(QDialog):
    signed_in = Signal()
    cancelled = Signal()

    def __init__(self, session: OperaXSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._offer_generated = False
        self._closed = False
        self._torn_down = False
        self._signed_in_completed = False
        self._cancelled_emitted = False

        self.setWindowTitle(tr("Connect Opera GX"))
        self.resize(680, 380)

        self.instructions = QLabel(
            tr(
                "Load the X Desktop Feed extension in Opera GX, then pair it with the code "
                "shown here. Sign in to X directly in Opera if needed."
            )
        )
        self.instructions.setWordWrap(True)
        self.extension_path = QLabel(str(Path(session.extension_path).resolve()))
        self.extension_path.setWordWrap(True)
        self.extension_error = QLabel()
        self.extension_error.setWordWrap(True)
        self.copy_extension_button = QPushButton(tr("Copy extension path"))

        self.pairing_code = QLabel()
        self.pairing_guidance = QLabel(
            tr("The pairing code expires 2 minutes after it is generated.")
        )
        self.pairing_guidance.setWordWrap(True)
        self.copy_code_button = QPushButton(tr("Copy pairing code"))
        self.refresh_code_button = QPushButton(tr("Generate new code"))

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.open_button = QPushButton(tr("Open X in Opera"))
        self.check_button = QPushButton(tr("Check connection"))
        self.cancel_button = QPushButton(tr("Cancel"))

        extension_row = QHBoxLayout()
        extension_row.addWidget(self.extension_path, 1)
        extension_row.addWidget(self.copy_extension_button)
        pairing_row = QHBoxLayout()
        pairing_row.addWidget(self.pairing_code, 1)
        pairing_row.addWidget(self.copy_code_button)
        pairing_row.addWidget(self.refresh_code_button)
        action_row = QHBoxLayout()
        action_row.addWidget(self.open_button)
        action_row.addWidget(self.check_button)
        action_row.addStretch()
        action_row.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.instructions)
        layout.addWidget(QLabel(tr("Unpacked extension directory:")))
        layout.addLayout(extension_row)
        layout.addWidget(self.extension_error)
        layout.addWidget(QLabel(tr("Pairing code:")))
        layout.addLayout(pairing_row)
        layout.addWidget(self.pairing_guidance)
        layout.addWidget(self.status_label)
        layout.addStretch()
        layout.addLayout(action_row)

        self._copy_extension_callback = self._copy_extension_path
        self._copy_code_callback = self._copy_pairing_code
        self._refresh_callback = self._generate_pairing_offer
        self._open_callback = self._session.begin_login
        self._check_callback = self._session.verify
        self._cancel_callback = self.reject
        self._state_callback = self._on_session_state
        self._verification_callback = self._on_session_state
        self.copy_extension_button.clicked.connect(self._copy_extension_callback)
        self.copy_code_button.clicked.connect(self._copy_code_callback)
        self.refresh_code_button.clicked.connect(self._refresh_callback)
        self.open_button.clicked.connect(self._open_callback)
        self.check_button.clicked.connect(self._check_callback)
        self.cancel_button.clicked.connect(self._cancel_callback)
        self._session.state_changed.connect(self._state_callback)
        self._session.verification_finished.connect(self._verification_callback)

        self._update_extension_availability()
        self._update_status(self._session.state)

    def showEvent(self, event: QShowEvent) -> None:
        if not self._closed and not self._offer_generated:
            self._generate_pairing_offer()
        super().showEvent(event)

    def accept(self) -> None:
        if not self._signed_in_completed:
            return
        super().accept()

    def done(self, result: int) -> None:
        if result == QDialog.DialogCode.Accepted and not self._signed_in_completed:
            return
        super().done(result)

    def reject(self) -> None:
        if self._closed:
            return
        self._finish_cancelled()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._closed:
            self._finish_cancelled()
        super().closeEvent(event)

    def _update_extension_availability(self) -> None:
        path = Path(self._session.extension_path).resolve()
        if path.is_dir():
            self.extension_error.clear()
            self.copy_extension_button.setEnabled(True)
            return
        self.extension_error.setText(
            tr(
                "Extension directory not found: {path}. Restore extension/opera-xfeed "
                "before loading it in Opera GX."
            ).format(path=path)
        )
        self.copy_extension_button.setEnabled(False)

    def _generate_pairing_offer(self) -> None:
        if self._closed:
            return
        try:
            offer = self._session.pairing_offer()
        except RuntimeError as error:
            self.pairing_code.clear()
            self.copy_code_button.setEnabled(False)
            self.pairing_guidance.setText(
                tr("Could not generate a pairing code: {error}").format(error=error)
            )
            return
        self._offer_generated = True
        self.pairing_code.setText(offer.code)
        self.copy_code_button.setEnabled(True)
        self.pairing_guidance.setText(
            tr(
                "The pairing code expires 2 minutes after it is generated. "
                "Enter it in the extension popup and select Pair."
            )
        )

    def _copy_extension_path(self) -> None:
        QApplication.clipboard().setText(str(Path(self._session.extension_path).resolve()))

    def _copy_pairing_code(self) -> None:
        QApplication.clipboard().setText(self.pairing_code.text())

    def _on_session_state(self, state: object) -> None:
        if self._closed or not isinstance(state, SessionState):
            return
        self._update_status(state)
        if state is SessionState.SIGNED_IN:
            self._finish_signed_in()

    def _update_status(self, state: SessionState) -> None:
        messages = {
            SessionState.CHECKING: tr("Checking connection to the Opera extension..."),
            SessionState.SIGNING_IN: tr("X opened in Opera. Complete sign-in there if needed."),
            SessionState.SIGNED_IN: tr("Opera connected / X signed in"),
            SessionState.SIGNED_OUT: tr(
                "Opera connected / X signed out. Open X in Opera, sign in, then check again."
            ),
            SessionState.UNAVAILABLE: tr(
                "Opera disconnected. Load and pair the extension, then check again."
            ),
        }
        self.status_label.setText(messages[state])

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
            (self.copy_extension_button.clicked, self._copy_extension_callback),
            (self.copy_code_button.clicked, self._copy_code_callback),
            (self.refresh_code_button.clicked, self._refresh_callback),
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
