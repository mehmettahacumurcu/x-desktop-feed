import json
from collections.abc import Callable

import pytest

from xfeed.edge_browser import EdgeUnavailableError
from xfeed.edge_session import EdgeXSession
from xfeed.x_session import SessionState, build_session_check_script


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


_DEFAULT_PAYLOAD = object()


class BrowserDouble:
    def __init__(self, *, evaluate_result: object = _DEFAULT_PAYLOAD) -> None:
        self.evaluate_result = (
            snapshot_payload() if evaluate_result is _DEFAULT_PAYLOAD else evaluate_result
        )
        self.opened_urls: list[str] = []
        self.evaluated_scripts: list[str] = []
        self.stop_count = 0
        self.clear_count = 0
        self.close_count = 0
        self.open_error: Exception | None = None

    def open(self, url: str, timeout_s: float = 15.0) -> str:
        del timeout_s
        self.opened_urls.append(url)
        if self.open_error is not None:
            raise self.open_error
        return url

    def evaluate(self, script: str, timeout_s: float = 10.0) -> object:
        del timeout_s
        self.evaluated_scripts.append(script)
        return self.evaluate_result

    def stop(self) -> None:
        self.stop_count += 1

    def clear_x_site_data(self) -> None:
        self.clear_count += 1

    def close(self) -> None:
        self.close_count += 1


def immediate_executor(
    operation: Callable[[], object],
    succeeded: Callable[[object], None],
    failed: Callable[[str], None],
) -> None:
    try:
        result = operation()
    except Exception as error:
        failed(str(error))
        return
    succeeded(result)


class DelayedExecutor:
    def __init__(self) -> None:
        self.calls: list[
            tuple[Callable[[], object], Callable[[object], None], Callable[[str], None]]
        ] = []

    def __call__(
        self,
        operation: Callable[[], object],
        succeeded: Callable[[object], None],
        failed: Callable[[str], None],
    ) -> None:
        self.calls.append((operation, succeeded, failed))


def test_verify_is_single_flight_and_uses_only_the_safe_snapshot_script(qtbot):
    browser = BrowserDouble(evaluate_result=json.dumps(snapshot_payload()))
    executor = DelayedExecutor()
    session = EdgeXSession(browser, executor=executor)
    state_changes: list[SessionState] = []
    finished: list[SessionState] = []
    session.state_changed.connect(state_changes.append)
    session.verification_finished.connect(finished.append)

    session.verify()
    session.verify()

    assert session.state is SessionState.CHECKING
    assert state_changes == [SessionState.CHECKING]
    assert len(executor.calls) == 1
    operation, succeeded, _ = executor.calls[0]
    result = operation()
    assert browser.opened_urls == ["https://x.com/home"]
    assert browser.evaluated_scripts == [build_session_check_script()]
    assert finished == []

    succeeded(result)
    succeeded(result)

    assert session.state is SessionState.SIGNED_IN
    assert state_changes == [SessionState.CHECKING, SessionState.SIGNED_IN]
    assert finished == [SessionState.SIGNED_IN]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (snapshot_payload(), SessionState.SIGNED_IN),
        (snapshot_payload(signed_in_marker=False), SessionState.SIGNED_OUT),
        (snapshot_payload(login_wall=True), SessionState.SIGNED_OUT),
    ],
)
def test_verify_maps_sanitized_snapshot_to_session_state(qtbot, payload, expected):
    browser = BrowserDouble(evaluate_result=payload)
    session = EdgeXSession(browser, executor=immediate_executor)
    finished: list[SessionState] = []
    session.verification_finished.connect(finished.append)

    session.verify()

    assert session.state is expected
    assert finished == [expected]


@pytest.mark.parametrize("payload", ["not json", None, {"page_hostname": "example.com"}])
def test_verify_maps_invalid_snapshot_to_signed_out(qtbot, payload):
    browser = BrowserDouble(evaluate_result=payload)
    session = EdgeXSession(browser, executor=immediate_executor)
    finished: list[SessionState] = []
    session.verification_finished.connect(finished.append)

    session.verify()

    assert session.state is SessionState.SIGNED_OUT
    assert finished == [SessionState.SIGNED_OUT]


def test_edge_error_maps_to_unavailable_and_finishes_once(qtbot):
    browser = BrowserDouble()
    browser.open_error = EdgeUnavailableError("Microsoft Edge is unavailable")
    session = EdgeXSession(browser, executor=immediate_executor)
    finished: list[SessionState] = []
    session.verification_finished.connect(finished.append)

    session.verify()

    assert session.state is SessionState.UNAVAILABLE
    assert finished == [SessionState.UNAVAILABLE]


def test_begin_login_opens_visible_edge_login_without_credentials(qtbot):
    browser = BrowserDouble()
    session = EdgeXSession(browser, executor=immediate_executor)

    session.begin_login()

    assert browser.opened_urls == ["https://x.com/i/flow/login"]
    assert browser.evaluated_scripts == []
    assert session.state is SessionState.SIGNING_IN


def test_begin_login_rejects_stale_verification_callbacks(qtbot):
    browser = BrowserDouble()
    executor = DelayedExecutor()
    session = EdgeXSession(browser, executor=executor)
    finished: list[SessionState] = []
    session.verification_finished.connect(finished.append)

    session.verify()
    _, verify_succeeded, verify_failed = executor.calls[0]
    session.begin_login()
    login_operation, login_succeeded, _ = executor.calls[1]
    login_succeeded(login_operation())
    verify_succeeded(snapshot_payload())
    verify_failed("late failure")

    assert session.state is SessionState.SIGNING_IN
    assert browser.opened_urls == ["https://x.com/i/flow/login"]
    assert finished == []


def test_clear_invalidates_callbacks_and_clears_only_x_site_data(qtbot):
    browser = BrowserDouble()
    executor = DelayedExecutor()
    session = EdgeXSession(browser, executor=executor)
    session.mark_signed_in()
    session.verify()
    _, verify_succeeded, _ = executor.calls[0]

    session.clear()
    clear_operation, clear_succeeded, _ = executor.calls[1]
    clear_succeeded(clear_operation())
    verify_succeeded(snapshot_payload())

    assert browser.clear_count == 1
    assert browser.opened_urls == []
    assert browser.evaluated_scripts == []
    assert browser.stop_count == 0
    assert browser.close_count == 0
    assert session.state is SessionState.SIGNED_OUT


def test_explicit_state_transitions_emit_only_changes(qtbot):
    session = EdgeXSession(BrowserDouble(), executor=immediate_executor)
    state_changes: list[SessionState] = []
    session.state_changed.connect(state_changes.append)

    session.mark_signed_in()
    session.mark_signed_in()
    session.mark_signed_out()
    session.mark_signed_out()

    assert state_changes == [SessionState.SIGNED_IN, SessionState.SIGNED_OUT]
