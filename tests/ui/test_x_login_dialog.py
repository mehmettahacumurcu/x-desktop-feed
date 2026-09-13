import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog

import xfeed.ui.x_login_dialog as login_dialog_module
from xfeed.ui.x_login_dialog import XLoginDialog
from xfeed.x_session import SessionState


class SessionDouble(QObject):
    state_changed = Signal(object)
    verification_finished = Signal(object)

    def __init__(self, state: SessionState = SessionState.SIGNED_OUT) -> None:
        super().__init__()
        self.state = state
        self.begin_login_count = 0
        self.verify_count = 0
        self.mark_signed_in_count = 0
        self.mark_signed_out_count = 0

    def begin_login(self) -> None:
        self.begin_login_count += 1

    def verify(self) -> None:
        self.verify_count += 1

    def mark_signed_in(self) -> None:
        self.mark_signed_in_count += 1

    def mark_signed_out(self) -> None:
        self.mark_signed_out_count += 1

    def emit_state(self, state: SessionState) -> None:
        self.state = state
        self.state_changed.emit(state)

    def finish_verification(self, state: SessionState) -> None:
        self.state = state
        self.state_changed.emit(state)
        self.verification_finished.emit(state)


def build_dialog(state: SessionState = SessionState.SIGNED_OUT):
    session = SessionDouble(state)
    dialog = XLoginDialog(session)
    return dialog, session


def test_dialog_is_external_edge_instructions_without_embedded_web_engine(qtbot):
    dialog, session = build_dialog()
    dialog.show()

    assert dialog.windowTitle() == "Sign in to X"
    assert "dedicated Edge window" in dialog.instructions.text()
    assert dialog.instructions.wordWrap()
    assert dialog.open_button.text() == "Open X in Edge"
    assert dialog.check_button.text() == "Check login status"
    assert dialog.cancel_button.text() == "Cancel"
    assert session.begin_login_count == 1
    assert not hasattr(dialog, "web_view")
    assert not hasattr(login_dialog_module, "QWebEngineView")
    assert not hasattr(login_dialog_module, "QWebEnginePage")
    assert not hasattr(login_dialog_module, "QWebEngineProfile")


def test_open_and_check_buttons_delegate_to_session(qtbot):
    dialog, session = build_dialog()
    dialog.show()

    dialog.open_button.click()
    dialog.check_button.click()

    assert session.begin_login_count == 2
    assert session.verify_count == 1


def test_checking_state_is_visible_and_signed_in_verification_accepts_once(qtbot):
    dialog, session = build_dialog()
    signed_in: list[bool] = []
    cancelled: list[bool] = []
    dialog.signed_in.connect(lambda: signed_in.append(True))
    dialog.cancelled.connect(lambda: cancelled.append(True))
    dialog.show()

    session.emit_state(SessionState.CHECKING)
    assert "checking" in dialog.status_label.text().casefold()

    session.finish_verification(SessionState.SIGNED_IN)
    session.verification_finished.emit(SessionState.SIGNED_IN)
    dialog.reject()

    assert signed_in == [True]
    assert cancelled == []
    assert session.mark_signed_in_count == 0
    assert session.mark_signed_out_count == 0
    assert dialog.result() == QDialog.DialogCode.Accepted


@pytest.mark.parametrize("state", [SessionState.SIGNED_OUT, SessionState.UNAVAILABLE])
def test_non_authenticated_verification_stays_open_with_actionable_status(qtbot, state):
    dialog, session = build_dialog()
    signed_in: list[bool] = []
    dialog.signed_in.connect(lambda: signed_in.append(True))
    dialog.show()

    session.finish_verification(state)

    assert dialog.isVisible()
    assert signed_in == []
    assert "edge" in dialog.status_label.text().casefold()
    assert "again" in dialog.status_label.text().casefold()


@pytest.mark.parametrize("closure", ["cancel_button", "close", "reject"])
def test_cancel_emits_once_without_erasing_a_valid_session(qtbot, closure):
    dialog, session = build_dialog(SessionState.SIGNED_IN)
    cancelled: list[bool] = []
    dialog.cancelled.connect(lambda: cancelled.append(True))
    dialog.show()

    if closure == "cancel_button":
        dialog.cancel_button.click()
    elif closure == "close":
        dialog.close()
    else:
        dialog.reject()
    dialog.reject()

    assert cancelled == [True]
    assert session.mark_signed_in_count == 0
    assert session.mark_signed_out_count == 0
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_late_session_signals_after_cancellation_are_ignored(qtbot):
    dialog, session = build_dialog()
    signed_in: list[bool] = []
    cancelled: list[bool] = []
    dialog.signed_in.connect(lambda: signed_in.append(True))
    dialog.cancelled.connect(lambda: cancelled.append(True))
    dialog.show()

    dialog.reject()
    status_at_cancel = dialog.status_label.text()
    session.emit_state(SessionState.CHECKING)
    session.finish_verification(SessionState.SIGNED_IN)

    assert signed_in == []
    assert cancelled == [True]
    assert dialog.status_label.text() == status_at_cancel
