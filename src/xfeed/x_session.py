import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, cast

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile


class SessionState(str, Enum):
    CHECKING = "checking"
    SIGNED_OUT = "signed_out"
    SIGNING_IN = "signing_in"
    SIGNED_IN = "signed_in"
    UNAVAILABLE = "unavailable"


class SessionSnapshotError(ValueError):
    """A visible X page snapshot does not match the session boundary."""


@dataclass(frozen=True)
class SessionSnapshot:
    page_url: str
    signed_in_marker: bool
    login_wall: bool

    @property
    def authenticated(self) -> bool:
        return self.signed_in_marker and not self.login_wall


def parse_session_snapshot(payload: object) -> SessionSnapshot:
    if not isinstance(payload, dict):
        raise SessionSnapshotError("session snapshot must be a dictionary")

    components: dict[str, str] = {}
    for field in ("page_scheme", "page_hostname", "page_port", "page_path"):
        value = payload.get(field)
        if not isinstance(value, str):
            raise SessionSnapshotError(f"snapshot {field} must be a string")
        components[field] = value

    flags: dict[str, bool] = {}
    for field in ("signed_in_marker", "login_wall"):
        value = payload.get(field)
        if not isinstance(value, bool):
            raise SessionSnapshotError(f"snapshot {field} must be a boolean")
        flags[field] = value

    scheme = components["page_scheme"].casefold().removesuffix(":")
    hostname = components["page_hostname"].casefold()
    port = components["page_port"]
    path = components["page_path"]
    if (
        scheme != "https"
        or hostname not in ("x.com", "www.x.com")
        or port not in ("", "443")
        or not path.startswith("/")
        or any(character in path for character in ("?", "#", "\r", "\n", "\0"))
    ):
        raise SessionSnapshotError("snapshot page_url is not a supported X URL")

    page_url = f"https://{hostname}{f':{port}' if port else ''}{path}"
    return SessionSnapshot(
        page_url=page_url,
        signed_in_marker=flags["signed_in_marker"],
        login_wall=flags["login_wall"],
    )


def build_session_check_script() -> str:
    return r"""
(() => JSON.stringify({
    page_scheme: location.protocol,
    page_hostname: location.hostname,
    page_port: location.port,
    page_path: location.pathname,
    signed_in_marker: Boolean(document.querySelector(
        '[data-testid="SideNav_AccountSwitcher_Button"], '
        + '[data-testid="AppTabBar_Home_Link"]'
    )),
    login_wall: location.pathname.startsWith('/i/flow/login') ||
        Boolean(document.querySelector(
            'input[autocomplete="username"], form[action*="login"]'
        ))
}))()
""".strip()


def is_supported_x_page_url(value: str | QUrl) -> bool:
    url = QUrl(value)
    return (
        url.isValid()
        and url.scheme().casefold() == "https"
        and url.host().casefold() in ("x.com", "www.x.com")
        and url.port(-1) in (-1, 443)
        and not url.userInfo()
    )


class ConnectableSignal(Protocol):
    def connect(self, callback: Callable[..., object]) -> object: ...

    def disconnect(self, callback: Callable[..., object]) -> object: ...


class SessionPage(Protocol):
    loadFinished: ConnectableSignal

    def load(self, url: QUrl) -> None: ...

    def url(self) -> QUrl: ...

    def runJavaScript(self, script: str, callback: Callable[[object], None]) -> None: ...

    def stop(self) -> None: ...

    def deleteLater(self) -> None: ...


class SessionWebPage(QWebEnginePage):
    def stop(self) -> None:
        self.triggerAction(QWebEnginePage.WebAction.Stop)


class SessionTimer(Protocol):
    timeout: ConnectableSignal

    def setSingleShot(self, single_shot: bool) -> None: ...

    def start(self, interval: int) -> None: ...

    def stop(self) -> None: ...


class XSession(QObject):
    state_changed = Signal(object)
    verification_finished = Signal(object)

    def __init__(
        self,
        data_dir: Path,
        parent: QObject | None = None,
        *,
        profile: QWebEngineProfile | None = None,
        page_factory: Callable[[QWebEngineProfile], SessionPage] | None = None,
        timer: SessionTimer | None = None,
    ) -> None:
        super().__init__(parent)
        if profile is None:
            session_dir = data_dir / "x-session"
            profile = QWebEngineProfile("xfeed-x-session", self)
            profile.setPersistentStoragePath(str(session_dir / "storage"))
            profile.setCachePath(str(session_dir / "cache"))
            profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
            )
        self.profile = profile
        self._page_factory = page_factory or self._create_page
        self._timer = timer or QTimer(self)
        self._state = SessionState.SIGNED_OUT
        self._generation = 0
        self._verification_page: SessionPage | None = None
        self._load_callback: Callable[..., object] | None = None
        self._timeout_callback: Callable[..., object] | None = None

    @property
    def state(self) -> SessionState:
        return self._state

    def verify(self) -> None:
        if self._verification_page is not None:
            return

        self._generation += 1
        generation = self._generation
        self._set_state(SessionState.CHECKING)
        try:
            page = self._page_factory(self.profile)
        except (RuntimeError, TypeError):
            self._set_state(SessionState.SIGNED_OUT)
            self.verification_finished.emit(SessionState.SIGNED_OUT)
            return

        self._verification_page = page

        def load_callback(succeeded: object) -> None:
            self._on_load_finished(generation, page, bool(succeeded))

        def timeout_callback() -> None:
            self._finish_verification(generation, page, SessionState.SIGNED_OUT)

        self._load_callback = load_callback
        self._timeout_callback = timeout_callback
        page.loadFinished.connect(load_callback)
        self._timer.timeout.connect(timeout_callback)
        self._timer.setSingleShot(True)
        self._timer.start(15_000)
        try:
            page.load(QUrl("https://x.com/home"))
        except (RuntimeError, TypeError):
            self._finish_verification(generation, page, SessionState.SIGNED_OUT)

    def evaluate_page(
        self,
        page: SessionPage,
        callback: Callable[[SessionSnapshot | None], None],
    ) -> None:
        def parse_payload(payload: object) -> None:
            try:
                decoded = json.loads(payload) if isinstance(payload, str) else payload
                snapshot = parse_session_snapshot(decoded)
            except (json.JSONDecodeError, SessionSnapshotError, TypeError):
                callback(None)
                return
            callback(snapshot)

        try:
            page.runJavaScript(build_session_check_script(), parse_payload)
        except (RuntimeError, TypeError):
            callback(None)

    def begin_login(self) -> None:
        self._generation += 1
        self._release_verification_page()
        self._set_state(SessionState.SIGNING_IN)

    def mark_signed_in(self) -> None:
        self._set_state(SessionState.SIGNED_IN)

    def mark_signed_out(self) -> None:
        self._set_state(SessionState.SIGNED_OUT)

    def clear(self) -> None:
        page = self._verification_page
        had_verification = page is not None
        self._generation += 1
        self._release_verification_page()
        self.profile.cookieStore().deleteAllCookies()
        self.profile.clearHttpCache()
        self.profile.clearAllVisitedLinks()
        self._set_state(SessionState.SIGNED_OUT)
        if had_verification:
            self.verification_finished.emit(SessionState.SIGNED_OUT)

    def _create_page(self, profile: QWebEngineProfile) -> SessionPage:
        return cast(SessionPage, SessionWebPage(profile, self))

    def _set_state(self, state: SessionState) -> None:
        if state is self._state:
            return
        self._state = state
        self.state_changed.emit(state)

    def _on_load_finished(
        self,
        generation: int,
        page: SessionPage,
        succeeded: bool,
    ) -> None:
        if not self._is_current(generation, page):
            return
        self._disconnect_load_callback(page)
        if not succeeded:
            self._finish_verification(generation, page, SessionState.SIGNED_OUT)
            return
        self.evaluate_page(
            page,
            lambda snapshot: self._on_snapshot(generation, page, snapshot),
        )

    def _on_snapshot(
        self,
        generation: int,
        page: SessionPage,
        snapshot: SessionSnapshot | None,
    ) -> None:
        if not self._is_current(generation, page):
            return
        state = (
            SessionState.SIGNED_IN
            if snapshot is not None and snapshot.authenticated
            else SessionState.SIGNED_OUT
        )
        self._finish_verification(generation, page, state)

    def _finish_verification(
        self,
        generation: int,
        page: SessionPage,
        state: SessionState,
    ) -> None:
        if not self._is_current(generation, page):
            return
        self._release_verification_page()
        self._set_state(state)
        self.verification_finished.emit(state)

    def _is_current(self, generation: int, page: SessionPage) -> bool:
        return generation == self._generation and page is self._verification_page

    def _release_verification_page(self) -> None:
        page = self._verification_page
        if page is None:
            return
        self._verification_page = None
        self._timer.stop()
        self._disconnect_timeout_callback()
        self._disconnect_load_callback(page)
        page.stop()
        page.deleteLater()

    def _disconnect_load_callback(self, page: SessionPage) -> None:
        callback = self._load_callback
        self._load_callback = None
        if callback is None:
            return
        try:
            page.loadFinished.disconnect(callback)
        except (RuntimeError, TypeError):
            pass

    def _disconnect_timeout_callback(self) -> None:
        callback = self._timeout_callback
        self._timeout_callback = None
        if callback is None:
            return
        try:
            self._timer.timeout.disconnect(callback)
        except (RuntimeError, TypeError):
            pass
