from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QDialog

import xfeed.ui.opera_connect_dialog as connect_dialog_module
from xfeed.app import default_extension_path
from xfeed.opera_pairing import PairingOffer
from xfeed.ui.opera_connect_dialog import OperaConnectDialog
from xfeed.x_session import SessionState


class SessionDouble(QObject):
    state_changed = Signal(object)
    verification_finished = Signal(object)

    def __init__(
        self,
        extension_path: Path,
        state: SessionState = SessionState.UNAVAILABLE,
    ) -> None:
        super().__init__()
        self.extension_path = extension_path
        self.state = state
        self.begin_login_count = 0
        self.verify_count = 0
        self.pairing_offer_count = 0

    def pairing_offer(self) -> PairingOffer:
        self.pairing_offer_count += 1
        return PairingOffer("ABCD2345", 120.0)

    def begin_login(self) -> None:
        self.begin_login_count += 1

    def verify(self) -> None:
        self.verify_count += 1

    def set_state(self, state: SessionState, *, verified: bool = False) -> None:
        self.state = state
        self.state_changed.emit(state)
        if verified:
            self.verification_finished.emit(state)


def test_dialog_explains_extension_pairing_without_credentials(qtbot, tmp_path):
    extension_path = tmp_path / "extension"
    extension_path.mkdir()
    session = SessionDouble(extension_path)
    dialog = OperaConnectDialog(session)
    qtbot.addWidget(dialog)

    assert dialog.windowTitle() == "Connect Opera GX"
    assert str(session.extension_path.resolve()) in dialog.extension_path.text()
    assert dialog.pairing_code.text() == ""
    assert "password" not in dialog.instructions.text().casefold()
    assert dialog.copy_extension_button.text() == "Copy extension path"
    assert dialog.copy_code_button.text() == "Copy pairing code"
    assert dialog.open_button.text() == "Open X in Opera"
    assert dialog.check_button.text() == "Check connection"
    assert dialog.refresh_code_button.text() == "Generate new code"
    assert dialog.cancel_button.text() == "Cancel"
    assert not hasattr(dialog, "web_view")
    assert not hasattr(connect_dialog_module, "QWebEngineView")
    assert session.begin_login_count == 0
    assert session.pairing_offer_count == 0

    dialog.show()

    assert dialog.pairing_code.text() == "ABCD2345"
    assert "2 minutes" in dialog.pairing_guidance.text()
    assert session.pairing_offer_count == 1
    assert session.begin_login_count == 0


def test_pairing_offer_is_generated_only_when_shown_or_refreshed(qtbot, tmp_path):
    extension_path = tmp_path / "extension"
    extension_path.mkdir()
    session = SessionDouble(extension_path)
    dialog = OperaConnectDialog(session)
    qtbot.addWidget(dialog)

    assert session.pairing_offer_count == 0
    dialog.show()
    dialog.show()
    assert session.pairing_offer_count == 1

    dialog.refresh_code_button.click()
    assert session.pairing_offer_count == 2
    assert dialog.pairing_code.text() == "ABCD2345"


def test_copy_and_action_buttons_delegate_without_automatic_login(qtbot, tmp_path):
    extension_path = tmp_path / "extension"
    extension_path.mkdir()
    session = SessionDouble(extension_path)
    dialog = OperaConnectDialog(session)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.copy_extension_button.click()
    assert QApplication.clipboard().text() == str(extension_path.resolve())
    dialog.copy_code_button.click()
    assert QApplication.clipboard().text() == "ABCD2345"

    dialog.open_button.click()
    dialog.check_button.click()
    assert session.begin_login_count == 1
    assert session.verify_count == 1


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (SessionState.CHECKING, "Checking connection"),
        (SessionState.SIGNING_IN, "X opened in Opera"),
        (SessionState.SIGNED_OUT, "Opera connected / X signed out"),
        (SessionState.UNAVAILABLE, "Opera disconnected"),
    ],
)
def test_non_signed_in_states_stay_open_with_actionable_status(qtbot, tmp_path, state, message):
    extension_path = tmp_path / "extension"
    extension_path.mkdir()
    session = SessionDouble(extension_path)
    dialog = OperaConnectDialog(session)
    qtbot.addWidget(dialog)
    dialog.show()
    signed_in: list[bool] = []
    dialog.signed_in.connect(lambda: signed_in.append(True))

    session.set_state(state, verified=True)

    assert dialog.isVisible()
    assert signed_in == []
    assert message in dialog.status_label.text()


def test_signed_in_state_accepts_once_and_disconnects_callbacks(qtbot, tmp_path):
    extension_path = tmp_path / "extension"
    extension_path.mkdir()
    session = SessionDouble(extension_path)
    dialog = OperaConnectDialog(session)
    qtbot.addWidget(dialog)
    signed_in: list[bool] = []
    cancelled: list[bool] = []
    dialog.signed_in.connect(lambda: signed_in.append(True))
    dialog.cancelled.connect(lambda: cancelled.append(True))
    dialog.show()

    session.set_state(SessionState.SIGNED_IN)
    session.verification_finished.emit(SessionState.SIGNED_IN)
    dialog.reject()

    assert signed_in == [True]
    assert cancelled == []
    assert dialog.result() == QDialog.DialogCode.Accepted
    begin_count = session.begin_login_count
    verify_count = session.verify_count
    dialog.open_button.click()
    dialog.check_button.click()
    assert session.begin_login_count == begin_count
    assert session.verify_count == verify_count


@pytest.mark.parametrize("state", [SessionState.SIGNED_OUT, SessionState.UNAVAILABLE])
@pytest.mark.parametrize("completion", ["accept", "done_accepted"])
def test_public_accepted_routes_wait_for_a_real_signed_in_verification(
    qtbot, tmp_path, state, completion
):
    extension_path = tmp_path / "extension"
    extension_path.mkdir()
    session = SessionDouble(extension_path, state)
    dialog = OperaConnectDialog(session)
    qtbot.addWidget(dialog)
    signed_in: list[bool] = []
    cancelled: list[bool] = []
    dialog.signed_in.connect(lambda: signed_in.append(True))
    dialog.cancelled.connect(lambda: cancelled.append(True))
    dialog.show()

    if completion == "accept":
        dialog.accept()
    else:
        dialog.done(QDialog.DialogCode.Accepted)

    assert dialog.isVisible()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert signed_in == []
    assert cancelled == []
    dialog.check_button.click()
    assert session.verify_count == 1

    session.set_state(SessionState.SIGNED_IN, verified=True)
    session.verification_finished.emit(SessionState.SIGNED_IN)

    assert signed_in == [True]
    assert cancelled == []
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.check_button.click()
    assert session.verify_count == 1


@pytest.mark.parametrize("closure", ["cancel_button", "close", "reject"])
def test_cancel_emits_once_on_each_closure_route(qtbot, tmp_path, closure):
    extension_path = tmp_path / "extension"
    extension_path.mkdir()
    session = SessionDouble(extension_path, SessionState.SIGNED_OUT)
    dialog = OperaConnectDialog(session)
    qtbot.addWidget(dialog)
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
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_late_session_signals_after_cancel_are_ignored(qtbot, tmp_path):
    extension_path = tmp_path / "extension"
    extension_path.mkdir()
    session = SessionDouble(extension_path)
    dialog = OperaConnectDialog(session)
    qtbot.addWidget(dialog)
    signed_in: list[bool] = []
    dialog.signed_in.connect(lambda: signed_in.append(True))
    dialog.show()

    dialog.reject()
    status = dialog.status_label.text()
    session.set_state(SessionState.SIGNED_IN, verified=True)

    assert signed_in == []
    assert dialog.status_label.text() == status


def test_missing_extension_directory_is_actionable_and_does_not_crash(qtbot, tmp_path):
    session = SessionDouble(tmp_path / "missing-extension")
    dialog = OperaConnectDialog(session)
    qtbot.addWidget(dialog)

    dialog.show()

    assert "not found" in dialog.extension_error.text().casefold()
    assert str(session.extension_path.resolve()) in dialog.extension_error.text()
    assert not dialog.copy_extension_button.isEnabled()


def test_default_extension_path_points_to_development_manifest():
    path = default_extension_path()

    assert path.is_absolute()
    assert (path / "manifest.json").is_file()
