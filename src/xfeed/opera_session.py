from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from xfeed.opera_bridge import BridgeSnapshot, OperaBridge
from xfeed.opera_pairing import PairingOffer
from xfeed.opera_protocol import ExtensionXState
from xfeed.x_session import SessionState


UrlOpener = Callable[[str], object]


def _open_url(url: str) -> bool:
    return QDesktopServices.openUrl(QUrl(url))


class OperaXSession(QObject):
    state_changed = Signal(object)
    verification_finished = Signal(object)

    def __init__(
        self,
        bridge: OperaBridge,
        extension_path: Path,
        parent: QObject | None = None,
        *,
        url_opener: UrlOpener | None = None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self.extension_path = Path(extension_path)
        self._url_opener = url_opener or _open_url
        self._state = SessionState.UNAVAILABLE
        self._bridge.snapshot_changed.connect(self._on_snapshot_changed)
        self._apply_snapshot(self._bridge.snapshot())

    @property
    def state(self) -> SessionState:
        return self._state

    def verify(self) -> None:
        self._apply_snapshot(self._bridge.snapshot())
        self.verification_finished.emit(self._state)

    def begin_login(self) -> None:
        self._set_state(SessionState.SIGNING_IN)
        self._url_opener("https://x.com/home")
        self._apply_snapshot(self._bridge.snapshot())

    def mark_signed_in(self) -> None:
        self._set_state(SessionState.SIGNED_IN)

    def mark_signed_out(self) -> None:
        self._set_state(SessionState.SIGNED_OUT)

    def pairing_offer(self) -> PairingOffer:
        return self._bridge.issue_pairing_code()

    def clear(self) -> None:
        self.revoke()

    def revoke(self) -> None:
        self._bridge.revoke()
        self._set_state(SessionState.UNAVAILABLE)

    def _on_snapshot_changed(self, snapshot: object) -> None:
        if isinstance(snapshot, BridgeSnapshot):
            self._apply_snapshot(snapshot)

    def _apply_snapshot(self, snapshot: BridgeSnapshot) -> None:
        if not snapshot.connected:
            state = SessionState.UNAVAILABLE
        elif snapshot.x_state is ExtensionXState.SIGNED_IN:
            state = SessionState.SIGNED_IN
        else:
            state = SessionState.SIGNED_OUT
        self._set_state(state)

    def _set_state(self, state: SessionState) -> None:
        if state is self._state:
            return
        self._state = state
        self.state_changed.emit(state)
