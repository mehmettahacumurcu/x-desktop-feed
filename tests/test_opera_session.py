from collections.abc import Callable

import pytest
from PySide6.QtCore import QObject, Signal

from xfeed.opera_bridge import BridgeSnapshot
from xfeed.opera_pairing import PairingOffer
from xfeed.opera_protocol import ExtensionXState
from xfeed.opera_session import OperaXSession
from xfeed.x_session import SessionState


class BridgeDouble(QObject):
    snapshot_changed = Signal(object)

    def __init__(self, value: BridgeSnapshot) -> None:
        super().__init__()
        self.value = value
        self.revoked = 0

    def snapshot(self) -> BridgeSnapshot:
        return self.value

    def issue_pairing_code(self) -> PairingOffer:
        return PairingOffer("ABCD2345", 120.0)

    def revoke(self) -> None:
        self.revoked += 1


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (BridgeSnapshot(False, None), SessionState.UNAVAILABLE),
        (BridgeSnapshot(True, ExtensionXState.SIGNED_OUT), SessionState.SIGNED_OUT),
        (BridgeSnapshot(True, ExtensionXState.SIGNED_IN), SessionState.SIGNED_IN),
        (BridgeSnapshot(True, ExtensionXState.CHALLENGE), SessionState.SIGNED_OUT),
        (BridgeSnapshot(True, ExtensionXState.RATE_LIMITED), SessionState.SIGNED_OUT),
    ],
)
def test_session_maps_bridge_snapshots(qtbot, tmp_path, snapshot, expected):
    bridge = BridgeDouble(snapshot)
    session = OperaXSession(bridge, tmp_path / "extension", url_opener=lambda _url: True)

    assert session.state is expected

    bridge.value = BridgeSnapshot(True, ExtensionXState.SIGNED_IN)
    bridge.snapshot_changed.emit(bridge.value)
    assert session.state is SessionState.SIGNED_IN


def test_session_maps_authenticated_heartbeat_and_verifies(qtbot, tmp_path):
    bridge = BridgeDouble(BridgeSnapshot(True, ExtensionXState.SIGNED_IN))
    session = OperaXSession(bridge, tmp_path / "extension", url_opener=lambda _url: True)
    finished_values: list[SessionState] = []
    session.verification_finished.connect(finished_values.append)

    with qtbot.waitSignal(session.verification_finished) as finished:
        session.verify()
    session.verify()

    assert session.state is SessionState.SIGNED_IN
    assert finished.args == [SessionState.SIGNED_IN]
    assert finished_values == [SessionState.SIGNED_IN, SessionState.SIGNED_IN]


def test_login_and_explicit_state_transitions(qtbot, tmp_path):
    opened: list[str] = []

    def open_url(url: str) -> bool:
        opened.append(url)
        return True

    bridge = BridgeDouble(BridgeSnapshot(True, ExtensionXState.SIGNED_OUT))
    session = OperaXSession(bridge, tmp_path / "extension", url_opener=open_url)
    changes: list[SessionState] = []
    session.state_changed.connect(changes.append)

    session.begin_login()
    assert opened == ["https://x.com/home"]
    assert session.state is SessionState.SIGNED_OUT

    session.mark_signed_in()
    session.mark_signed_out()

    assert changes == [
        SessionState.SIGNING_IN,
        SessionState.SIGNED_OUT,
        SessionState.SIGNED_IN,
        SessionState.SIGNED_OUT,
    ]


def test_pairing_offer_and_extension_path_delegate_to_bridge(qtbot, tmp_path):
    bridge = BridgeDouble(BridgeSnapshot(False, None))
    extension_path = tmp_path / "extension"
    session = OperaXSession(bridge, extension_path, url_opener=lambda _url: True)

    assert session.extension_path == extension_path
    assert session.pairing_offer() == PairingOffer("ABCD2345", 120.0)


@pytest.mark.parametrize("operation", ["clear", "revoke"])
def test_clear_and_revoke_only_revoke_pairing(qtbot, tmp_path, operation):
    bridge = BridgeDouble(BridgeSnapshot(True, ExtensionXState.SIGNED_IN))
    session = OperaXSession(bridge, tmp_path / "extension", url_opener=lambda _url: True)

    method: Callable[[], None] = getattr(session, operation)
    method()

    assert bridge.revoked == 1
    assert session.state is SessionState.UNAVAILABLE
