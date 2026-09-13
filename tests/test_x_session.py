import json
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEngineProfile

from xfeed.x_session import (
    SessionSnapshot,
    SessionSnapshotError,
    SessionState,
    XSession,
    build_session_check_script,
    parse_session_snapshot,
)


def snapshot_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "page_scheme": "https:",
        "page_hostname": "x.com",
        "page_port": "",
        "page_path": "/home",
        "signed_in_marker": True,
        "login_wall": False,
    }
    payload.update(overrides)
    return payload


class SignalDouble:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def disconnect(self, callback) -> None:
        if callback in self.callbacks:
            self.callbacks.remove(callback)

    def emit(self, *args) -> None:
        for callback in tuple(self.callbacks):
            callback(*args)


class PageDouble:
    def __init__(self) -> None:
        self.loadFinished = SignalDouble()
        self.loads: list[QUrl] = []
        self.scripts: list[str] = []
        self.callbacks = []
        self.stopped = False
        self.deleted = False

    def load(self, url: QUrl) -> None:
        self.loads.append(url)

    def runJavaScript(self, script: str, callback) -> None:
        self.scripts.append(script)
        self.callbacks.append(callback)

    def complete_script(self, payload: object, index: int = -1) -> None:
        self.callbacks[index](payload)

    def stop(self) -> None:
        self.stopped = True

    def deleteLater(self) -> None:
        self.deleted = True


class TimerDouble:
    def __init__(self) -> None:
        self.timeout = SignalDouble()
        self.single_shot = False
        self.active = False
        self.starts: list[int] = []
        self.stop_count = 0

    def setSingleShot(self, single_shot: bool) -> None:
        self.single_shot = single_shot

    def start(self, interval: int) -> None:
        self.active = True
        self.starts.append(interval)

    def stop(self) -> None:
        self.active = False
        self.stop_count += 1

    def fire(self) -> None:
        if not self.active:
            return
        self.active = False
        self.timeout.emit()


class CookieStoreDouble:
    def __init__(self) -> None:
        self.delete_count = 0

    def deleteAllCookies(self) -> None:
        self.delete_count += 1


class ProfileDouble:
    def __init__(self) -> None:
        self.cookies = CookieStoreDouble()
        self.cache_clear_count = 0
        self.visited_links_clear_count = 0

    def cookieStore(self) -> CookieStoreDouble:
        return self.cookies

    def clearHttpCache(self) -> None:
        self.cache_clear_count += 1

    def clearAllVisitedLinks(self) -> None:
        self.visited_links_clear_count += 1


def build_session(tmp_path):
    page = PageDouble()
    pages = []
    profile = ProfileDouble()
    timer = TimerDouble()

    def page_factory(received_profile):
        pages.append(received_profile)
        return page

    session = XSession(
        tmp_path,
        profile=profile,
        page_factory=page_factory,
        timer=timer,
    )
    state_changes = []
    finished = []
    session.state_changed.connect(state_changes.append)
    session.verification_finished.connect(finished.append)
    return session, page, pages, profile, timer, state_changes, finished


def test_session_state_has_stable_string_values():
    assert [state.value for state in SessionState] == [
        "checking",
        "signed_out",
        "signing_in",
        "signed_in",
        "unavailable",
    ]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            snapshot_payload(),
            SessionSnapshot(
                page_url="https://x.com/home",
                signed_in_marker=True,
                login_wall=False,
            ),
        ),
        (
            snapshot_payload(
                page_hostname="www.x.com",
                page_path="/i/flow/login",
                signed_in_marker=False,
                login_wall=True,
            ),
            SessionSnapshot(
                page_url="https://www.x.com/i/flow/login",
                signed_in_marker=False,
                login_wall=True,
            ),
        ),
    ],
)
def test_parse_session_snapshot_accepts_supported_x_pages(payload, expected):
    assert parse_session_snapshot(payload) == expected


def test_parse_session_snapshot_accepts_explicit_default_https_port():
    payload = snapshot_payload(page_port="443")

    assert parse_session_snapshot(payload).page_url == "https://x.com:443/home"


@pytest.mark.parametrize("payload", [None, [], "snapshot", 1, True])
def test_parse_session_snapshot_rejects_non_dictionary_payloads(payload):
    with pytest.raises(SessionSnapshotError):
        parse_session_snapshot(payload)


@pytest.mark.parametrize(
    "payload",
    [
        snapshot_payload(signed_in_marker="true"),
        snapshot_payload(signed_in_marker=1),
        snapshot_payload(login_wall="false"),
        snapshot_payload(login_wall=0),
        snapshot_payload(page_scheme=""),
        snapshot_payload(page_hostname=None),
        snapshot_payload(page_port=443),
        snapshot_payload(page_path=None),
    ],
)
def test_parse_session_snapshot_rejects_malformed_fields(payload):
    with pytest.raises(SessionSnapshotError):
        parse_session_snapshot(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"page_scheme": "http:"},
        {"page_hostname": "twitter.com"},
        {"page_hostname": "example.com"},
        {"page_hostname": "user@x.com"},
        {"page_port": "8443"},
        {"page_path": "home"},
        {"page_path": "/home?code=secret"},
        {"page_path": "/home#access_token=secret"},
    ],
)
def test_parse_session_snapshot_rejects_unsupported_urls(overrides):
    with pytest.raises(SessionSnapshotError):
        parse_session_snapshot(snapshot_payload(**overrides))


def test_snapshot_is_authenticated_only_without_a_login_wall():
    assert parse_session_snapshot(snapshot_payload()).authenticated
    assert not parse_session_snapshot(snapshot_payload(signed_in_marker=False)).authenticated
    assert not parse_session_snapshot(snapshot_payload(login_wall=True)).authenticated


def test_session_check_script_reads_only_visible_dom_state():
    script = build_session_check_script()

    assert "location.protocol" in script
    assert "location.hostname" in script
    assert "location.port" in script
    assert "location.pathname" in script
    assert "document.querySelector" in script
    assert "SideNav_AccountSwitcher_Button" in script
    assert "AppTabBar_Home_Link" in script
    for forbidden in (
        "cookie",
        "token",
        "password",
        "localStorage",
        "sessionStorage",
        "XMLHttpRequest",
        "location.href",
        "location.search",
        "location.hash",
    ):
        assert forbidden.casefold() not in script.casefold()


def test_query_fragment_and_extra_full_url_never_reach_snapshot_callback(tmp_path, qtbot):
    session, page, _, _, _, _, _ = build_session(tmp_path)
    snapshots = []
    payload = snapshot_payload(
        page_url="https://x.com/home?code=oauth-secret#access_token=fragment-secret",
        query="?code=oauth-secret",
        fragment="#access_token=fragment-secret",
    )

    session.evaluate_page(page, snapshots.append)
    page.complete_script(json.dumps(payload))

    assert snapshots == [
        SessionSnapshot(
            page_url="https://x.com/home",
            signed_in_marker=True,
            login_wall=False,
        )
    ]
    assert "oauth-secret" not in repr(snapshots)
    assert "fragment-secret" not in repr(snapshots)


def test_session_owns_a_persistent_profile_inside_injected_data_directory(tmp_path, qtbot):
    session = XSession(tmp_path)

    assert not session.profile.isOffTheRecord()
    assert Path(session.profile.persistentStoragePath()).is_relative_to(tmp_path)
    assert Path(session.profile.cachePath()).is_relative_to(tmp_path)
    assert Path(session.profile.persistentStoragePath()) == tmp_path / "x-session" / "storage"
    assert Path(session.profile.cachePath()) == tmp_path / "x-session" / "cache"
    assert session.profile.persistentCookiesPolicy() == (
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    )

    session.deleteLater()


def test_default_verification_page_supports_the_session_page_stop_contract(tmp_path, qtbot):
    session = XSession(tmp_path)
    page = session._create_page(session.profile)

    page.stop()

    page.deleteLater()
    session.deleteLater()


def test_verify_is_single_flight_and_starts_a_bounded_home_page_check(tmp_path, qtbot):
    session, page, pages, profile, timer, state_changes, finished = build_session(tmp_path)

    session.verify()
    session.verify()

    assert session.state is SessionState.CHECKING
    assert state_changes == [SessionState.CHECKING]
    assert pages == [profile]
    assert page.loads == [QUrl("https://x.com/home")]
    assert page.scripts == []
    assert timer.single_shot
    assert timer.starts == [15_000]
    assert finished == []


def test_successful_load_evaluates_visible_state_and_finishes_signed_in_once(tmp_path, qtbot):
    session, page, _, _, timer, state_changes, finished = build_session(tmp_path)
    session.verify()

    page.loadFinished.emit(True)
    assert page.scripts == [build_session_check_script()]

    page.complete_script(json.dumps(snapshot_payload()))
    page.loadFinished.emit(False)
    page.complete_script(json.dumps(snapshot_payload(login_wall=True)))
    timer.fire()

    assert session.state is SessionState.SIGNED_IN
    assert state_changes == [SessionState.CHECKING, SessionState.SIGNED_IN]
    assert finished == [SessionState.SIGNED_IN]
    assert page.stopped
    assert page.deleted
    assert not timer.active


def test_evaluate_page_returns_a_snapshot_without_exposing_raw_payload(tmp_path, qtbot):
    session, page, _, _, _, _, _ = build_session(tmp_path)
    snapshots = []

    session.evaluate_page(page, snapshots.append)
    page.complete_script(json.dumps(snapshot_payload()))

    assert snapshots == [
        SessionSnapshot(
            page_url="https://x.com/home",
            signed_in_marker=True,
            login_wall=False,
        )
    ]


def test_evaluate_page_maps_malformed_payload_to_none(tmp_path, qtbot):
    session, page, _, _, _, _, _ = build_session(tmp_path)
    snapshots = []

    session.evaluate_page(page, snapshots.append)
    page.complete_script("not JSON")

    assert snapshots == [None]


def test_load_failure_finishes_signed_out_without_evaluation(tmp_path, qtbot):
    session, page, _, _, _, state_changes, finished = build_session(tmp_path)
    session.verify()

    page.loadFinished.emit(False)
    page.loadFinished.emit(True)

    assert session.state is SessionState.SIGNED_OUT
    assert state_changes == [SessionState.CHECKING, SessionState.SIGNED_OUT]
    assert finished == [SessionState.SIGNED_OUT]
    assert page.scripts == []


def test_timeout_finishes_signed_out_and_ignores_late_page_callbacks(tmp_path, qtbot):
    session, page, _, _, timer, state_changes, finished = build_session(tmp_path)
    session.verify()
    page.loadFinished.emit(True)
    late_callback = page.callbacks[-1]

    timer.fire()
    late_callback(json.dumps(snapshot_payload()))

    assert session.state is SessionState.SIGNED_OUT
    assert state_changes == [SessionState.CHECKING, SessionState.SIGNED_OUT]
    assert finished == [SessionState.SIGNED_OUT]


@pytest.mark.parametrize(
    "payload",
    [
        "not JSON",
        json.dumps(snapshot_payload(page_hostname="example.com")),
        json.dumps(snapshot_payload(login_wall=True)),
        json.dumps(snapshot_payload(signed_in_marker=False, login_wall=False)),
    ],
)
def test_untrusted_or_ambiguous_snapshot_finishes_signed_out_without_diagnostics(
    payload, tmp_path, qtbot
):
    session, page, _, _, _, state_changes, finished = build_session(tmp_path)
    session.verify()
    page.loadFinished.emit(True)

    page.complete_script(payload)

    assert session.state is SessionState.SIGNED_OUT
    assert state_changes == [SessionState.CHECKING, SessionState.SIGNED_OUT]
    assert finished == [SessionState.SIGNED_OUT]
    assert all(isinstance(value, SessionState) for value in state_changes + finished)


def test_clear_invalidates_active_verification_and_clears_only_owned_profile_state(tmp_path, qtbot):
    session, page, _, profile, timer, state_changes, finished = build_session(tmp_path)
    session.verify()
    page.loadFinished.emit(True)
    late_callback = page.callbacks[-1]

    session.clear()
    session.clear()
    late_callback(json.dumps(snapshot_payload()))

    assert page.stopped
    assert page.deleted
    assert not timer.active
    assert profile.cookies.delete_count == 2
    assert profile.cache_clear_count == 2
    assert profile.visited_links_clear_count == 2
    assert session.state is SessionState.SIGNED_OUT
    assert state_changes == [SessionState.CHECKING, SessionState.SIGNED_OUT]
    assert finished == [SessionState.SIGNED_OUT]


def test_begin_login_cancels_checking_and_ignores_all_delayed_verification_callbacks(
    tmp_path, qtbot
):
    session, page, _, _, timer, state_changes, finished = build_session(tmp_path)
    session.verify()
    delayed_load = page.loadFinished.callbacks[-1]
    delayed_timeout = timer.timeout.callbacks[-1]
    page.loadFinished.emit(True)
    delayed_script = page.callbacks[-1]

    session.begin_login()
    delayed_load(False)
    delayed_script(json.dumps(snapshot_payload()))
    delayed_timeout()

    assert page.stopped
    assert page.deleted
    assert not timer.active
    assert session.state is SessionState.SIGNING_IN
    assert state_changes == [SessionState.CHECKING, SessionState.SIGNING_IN]
    assert finished == []


def test_explicit_state_transitions_emit_only_public_enum_values(tmp_path, qtbot):
    session, _, _, _, _, state_changes, finished = build_session(tmp_path)

    session.begin_login()
    session.begin_login()
    session.mark_signed_in()
    session.mark_signed_in()
    session.mark_signed_out()
    session.mark_signed_out()

    assert session.state is SessionState.SIGNED_OUT
    assert state_changes == [
        SessionState.SIGNING_IN,
        SessionState.SIGNED_IN,
        SessionState.SIGNED_OUT,
    ]
    assert finished == []
    assert all(isinstance(value, SessionState) for value in state_changes)
