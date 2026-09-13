import json
import threading
import time
from pathlib import Path

import pytest

from xfeed.edge_browser import (
    RATE_LIMIT_SCRIPT,
    EdgeBrowser,
    EdgeBrowserError,
    EdgeEndpoint,
    EdgeProtocolError,
    EdgeUnavailableError,
    build_edge_arguments,
    find_edge_executable,
    parse_devtools_active_port,
)
from xfeed.public_collector import SCROLL_SCRIPT, build_extraction_script
from xfeed.x_session import build_session_check_script


class ProcessDouble:
    def __init__(self) -> None:
        self.terminated = False
        self.wait_timeouts: list[float] = []

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            self.wait_timeouts.append(timeout)
        return 0


class ClockDouble:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class SocketDouble:
    def __init__(self, response: object, *, unrelated_event: bool = False) -> None:
        self.response = response
        self.unrelated_event = unrelated_event
        self.sent: dict[str, object] | None = None
        self.closed = False
        self.timeouts: list[float] = []
        self._event_sent = False

    def send(self, payload: str) -> None:
        self.sent = json.loads(payload)

    def recv(self) -> str:
        assert self.sent is not None
        if self.unrelated_event and not self._event_sent:
            self._event_sent = True
            return json.dumps({"method": "Page.loadEventFired", "params": {}})
        if isinstance(self.response, dict) and "error" in self.response:
            return json.dumps({"id": self.sent["id"], **self.response})
        return json.dumps({"id": self.sent["id"], "result": self.response})

    def settimeout(self, timeout_s: float) -> None:
        self.timeouts.append(timeout_s)

    def close(self) -> None:
        self.closed = True


class SteadyEventSocketDouble(SocketDouble):
    def __init__(self, clock: ClockDouble) -> None:
        super().__init__({})
        self._clock = clock
        self.receive_count = 0

    def recv(self) -> str:
        self.receive_count += 1
        self._clock.value += 0.1
        if self.receive_count > 4:
            raise AssertionError("CDP receive loop exceeded its total deadline")
        return json.dumps({"method": "Page.frameNavigated", "params": {}})


class BlockingSocketDouble(SocketDouble):
    def __init__(self, response: object) -> None:
        super().__init__(response)
        self.receive_started = threading.Event()
        self.release_receive = threading.Event()

    def recv(self) -> str:
        self.receive_started.set()
        self.release_receive.wait(0.3)
        return super().recv()


def x_target() -> list[dict[str, object]]:
    return [
        {
            "description": "",
            "devtoolsFrontendUrl": "/devtools/inspector.html?page=target-id",
            "id": "target-id",
            "title": "X",
            "type": "page",
            "url": "https://x.com/home",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/target-id",
        }
    ]


def ready_location(path: str = "/openai", *, hostname: str = "x.com") -> dict[str, object]:
    return {
        "result": {
            "type": "object",
            "value": {
                "ready_state": "complete",
                "page_scheme": "https:",
                "page_hostname": hostname,
                "page_port": "",
                "page_path": path,
            },
        }
    }


def install_endpoint(profile: Path) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "DevToolsActivePort").write_text(
        "9222\n/devtools/browser/browser-id\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("environment_name", ["ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"])
def test_find_edge_executable_checks_standard_windows_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
) -> None:
    root = tmp_path / environment_name
    executable = root / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(environment_name, str(root))

    assert find_edge_executable() == executable.resolve()


def test_find_edge_executable_rejects_explicit_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing-msedge.exe"

    with pytest.raises(EdgeUnavailableError, match="Microsoft Edge is not available"):
        find_edge_executable(missing)


def test_build_arguments_use_only_dedicated_profile(tmp_path: Path) -> None:
    profile = tmp_path / "edge-profile"

    arguments = build_edge_arguments(profile)

    assert arguments == [
        f"--user-data-dir={profile.resolve()}",
        "--remote-debugging-port=0",
        "--new-window",
    ]
    assert "--headless" not in " ".join(arguments)
    assert "--enable-automation" not in " ".join(arguments)


def test_build_arguments_rejects_normal_edge_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_app_data = tmp_path / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    normal_profile = local_app_data / "Microsoft" / "Edge" / "User Data"

    with pytest.raises(EdgeBrowserError, match="dedicated Edge profile"):
        build_edge_arguments(normal_profile)


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "9222",
        "9222\n",
        "9222\n/devtools/browser/abc\nextra",
        "9222\n\n/devtools/browser/abc",
        "0\n/devtools/browser/abc",
        "65536\n/devtools/browser/abc",
        "not-a-port\n/devtools/browser/abc",
        "9222\n/devtools/page/abc",
        "9222\n/devtools/browser/",
    ],
)
def test_parse_devtools_active_port_rejects_invalid_payload(payload: str) -> None:
    with pytest.raises(EdgeProtocolError):
        parse_devtools_active_port(payload)


def test_parse_devtools_active_port_returns_validated_endpoint() -> None:
    assert parse_devtools_active_port("9222\n/devtools/browser/session-id\n") == EdgeEndpoint(
        port=9222,
        browser_path="/devtools/browser/session-id",
    )


def test_open_launches_edge_discovers_x_target_and_returns_sanitized_final_url(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "edge-profile"
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    process = ProcessDouble()
    process_arguments: list[str] = []
    requested_http_urls: list[tuple[str, float]] = []
    browser_diagnostics: list[str] = []
    sockets: list[SocketDouble] = []

    def process_factory(arguments: list[str]) -> ProcessDouble:
        process_arguments.extend(arguments)
        install_endpoint(profile)
        return process

    def http_json(url: str, timeout_s: float) -> object:
        requested_http_urls.append((url, timeout_s))
        return x_target()

    responses: list[object] = [{}, ready_location()]

    def websocket_factory(url: str, timeout_s: float) -> SocketDouble:
        assert url == "ws://127.0.0.1:9222/devtools/page/target-id"
        assert 0 < timeout_s <= 15.0
        socket = SocketDouble(responses.pop(0), unrelated_event=True)
        sockets.append(socket)
        return socket

    browser = EdgeBrowser(
        profile,
        edge_path=edge_path,
        process_factory=process_factory,
        http_json=http_json,
        websocket_factory=websocket_factory,
        diagnostics=browser_diagnostics.append,
    )

    assert browser.open("https://x.com/openai?password=secret#private") == "https://x.com/openai"
    assert process_arguments == [
        str(edge_path.resolve()),
        *build_edge_arguments(profile),
        "https://x.com/openai?password=secret#private",
    ]
    assert requested_http_urls[0][0] == "http://127.0.0.1:9222/json/list"
    assert [socket.sent["id"] for socket in sockets if socket.sent is not None] == [1, 2]
    assert all(socket.closed for socket in sockets)
    assert all("password" not in message.casefold() for message in browser_diagnostics)
    assert all("secret" not in message.casefold() for message in browser_diagnostics)


@pytest.mark.parametrize(
    "url",
    [
        "http://x.com/openai",
        "https://example.com/openai",
        "https://api.x.com/openai",
        "https://user:password@x.com/openai",
        "https://x.com:444/openai",
        "https://x.com\\@example.com/openai",
    ],
)
def test_open_accepts_only_safe_x_urls(tmp_path: Path, url: str) -> None:
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    browser = EdgeBrowser(tmp_path / "edge-profile", edge_path=edge_path)

    with pytest.raises(EdgeBrowserError, match="supported X URL"):
        browser.open(url)


def test_open_accepts_bare_x_origin(tmp_path: Path) -> None:
    profile = tmp_path / "edge-profile"
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    responses: list[object] = [{}, ready_location("/")]

    def process_factory(arguments: list[str]) -> ProcessDouble:
        install_endpoint(profile)
        return ProcessDouble()

    browser = EdgeBrowser(
        profile,
        edge_path=edge_path,
        process_factory=process_factory,
        http_json=lambda url, timeout_s: x_target(),
        websocket_factory=lambda url, timeout_s: SocketDouble(responses.pop(0)),
    )

    assert browser.open("https://x.com") == "https://x.com/"


@pytest.mark.parametrize("timeout_s", [float("nan"), float("inf"), 0.0, -1.0])
def test_open_rejects_unbounded_timeout(tmp_path: Path, timeout_s: float) -> None:
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()

    def unexpected_process(arguments: list[str]) -> ProcessDouble:
        pytest.fail("invalid timeout must be rejected before launching Edge")

    browser = EdgeBrowser(
        tmp_path / "edge-profile",
        edge_path=edge_path,
        process_factory=unexpected_process,
    )

    with pytest.raises(ValueError, match="finite and positive"):
        browser.open("https://x.com/home", timeout_s=timeout_s)


@pytest.mark.parametrize("poll_interval_s", [float("nan"), float("inf"), 0.0, -1.0])
def test_browser_rejects_unbounded_poll_interval(tmp_path: Path, poll_interval_s: float) -> None:
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()

    with pytest.raises(ValueError, match="finite and positive"):
        EdgeBrowser(
            tmp_path / "edge-profile",
            edge_path=edge_path,
            poll_interval_s=poll_interval_s,
        )


def test_open_rejects_malformed_json_list_payload(tmp_path: Path) -> None:
    profile = tmp_path / "edge-profile"
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()

    def process_factory(arguments: list[str]) -> ProcessDouble:
        install_endpoint(profile)
        return ProcessDouble()

    browser = EdgeBrowser(
        profile,
        edge_path=edge_path,
        process_factory=process_factory,
        http_json=lambda url, timeout_s: {"not": "a target list"},
    )

    with pytest.raises(EdgeProtocolError, match="target list"):
        browser.open("https://x.com/home")


def test_open_timeout_is_bounded_while_waiting_for_endpoint(tmp_path: Path) -> None:
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    clock = ClockDouble()
    browser = EdgeBrowser(
        tmp_path / "edge-profile",
        edge_path=edge_path,
        process_factory=lambda arguments: ProcessDouble(),
        clock=clock,
        sleep=clock.sleep,
        poll_interval_s=0.1,
    )

    with pytest.raises(EdgeUnavailableError, match="timed out"):
        browser.open("https://x.com/home", timeout_s=0.25)

    assert clock.value == pytest.approx(0.25)
    assert clock.sleeps == pytest.approx([0.1, 0.1, 0.05])


def test_open_removes_only_dedicated_profiles_stale_endpoint_before_launch(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "edge-profile"
    profile.mkdir()
    endpoint_file = profile / "DevToolsActivePort"
    endpoint_file.write_text(
        "1111\n/devtools/browser/stale-browser-id\n",
        encoding="utf-8",
    )
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    endpoint_existed_at_launch: list[bool] = []
    responses: list[object] = [{}, ready_location("/home")]

    def process_factory(arguments: list[str]) -> ProcessDouble:
        endpoint_existed_at_launch.append(endpoint_file.exists())
        install_endpoint(profile)
        return ProcessDouble()

    browser = EdgeBrowser(
        profile,
        edge_path=edge_path,
        process_factory=process_factory,
        http_json=lambda url, timeout_s: x_target(),
        websocket_factory=lambda url, timeout_s: SocketDouble(responses.pop(0)),
    )

    assert browser.open("https://x.com/home") == "https://x.com/home"
    assert endpoint_existed_at_launch == [False]


def test_evaluate_returns_runtime_by_value_result_and_rejects_protocol_error(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "edge-profile"
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    sockets: list[SocketDouble] = []
    responses: list[object] = [
        {},
        ready_location("/home"),
        {"result": {"type": "number", "value": 42}},
        {"error": {"code": -32_000, "message": "rejected secret value"}},
    ]

    def process_factory(arguments: list[str]) -> ProcessDouble:
        install_endpoint(profile)
        return ProcessDouble()

    def websocket_factory(url: str, timeout_s: float) -> SocketDouble:
        socket = SocketDouble(responses.pop(0))
        sockets.append(socket)
        return socket

    browser = EdgeBrowser(
        profile,
        edge_path=edge_path,
        process_factory=process_factory,
        http_json=lambda url, timeout_s: x_target(),
        websocket_factory=websocket_factory,
    )
    browser.open("https://x.com/home")

    safe_script = build_session_check_script()
    assert browser.evaluate(safe_script) == 42
    evaluate_request = sockets[-1].sent
    assert evaluate_request is not None
    assert evaluate_request["method"] == "Runtime.evaluate"
    assert evaluate_request["params"] == {
        "expression": safe_script,
        "returnByValue": True,
        "awaitPromise": True,
    }
    with pytest.raises(EdgeProtocolError, match="rejected a browser command") as error:
        browser.evaluate(safe_script)
    assert "secret" not in str(error.value).casefold()


def test_evaluate_accepts_only_exact_internal_scripts(tmp_path: Path) -> None:
    profile = tmp_path / "edge-profile"
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    sockets: list[SocketDouble] = []
    allowed_scripts = [
        build_session_check_script(),
        build_extraction_script("openai"),
        SCROLL_SCRIPT,
        RATE_LIMIT_SCRIPT,
    ]
    responses: list[object] = [
        {},
        ready_location("/home"),
        *[
            {"result": {"type": "string", "value": f"safe-{index}"}}
            for index in range(len(allowed_scripts))
        ],
    ]

    def process_factory(arguments: list[str]) -> ProcessDouble:
        install_endpoint(profile)
        return ProcessDouble()

    def websocket_factory(url: str, timeout_s: float) -> SocketDouble:
        socket = SocketDouble(responses.pop(0))
        sockets.append(socket)
        return socket

    browser = EdgeBrowser(
        profile,
        edge_path=edge_path,
        process_factory=process_factory,
        http_json=lambda url, timeout_s: x_target(),
        websocket_factory=websocket_factory,
    )
    browser.open("https://x.com/home")

    assert [browser.evaluate(script) for script in allowed_scripts] == [
        "safe-0",
        "safe-1",
        "safe-2",
        "safe-3",
    ]
    sent_expressions = [
        socket.sent["params"]["expression"]
        for socket in sockets[2:]
        if socket.sent is not None and isinstance(socket.sent["params"], dict)
    ]
    assert sent_expressions == allowed_scripts


@pytest.mark.parametrize(
    "script",
    [
        "21 * 2",
        "document.querySelector('input').value",
        "document.documentElement.outerHTML",
        "localStorage.getItem('token')",
        "document.cookie",
        "document.body.innerText",
        f"{SCROLL_SCRIPT}\n",
    ],
)
def test_evaluate_rejects_unapproved_scripts_before_transport(tmp_path: Path, script: str) -> None:
    profile = tmp_path / "edge-profile"
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    responses: list[object] = [{}, ready_location("/home")]
    websocket_calls = 0

    def process_factory(arguments: list[str]) -> ProcessDouble:
        install_endpoint(profile)
        return ProcessDouble()

    def websocket_factory(url: str, timeout_s: float) -> SocketDouble:
        nonlocal websocket_calls
        websocket_calls += 1
        return SocketDouble(responses.pop(0))

    browser = EdgeBrowser(
        profile,
        edge_path=edge_path,
        process_factory=process_factory,
        http_json=lambda url, timeout_s: x_target(),
        websocket_factory=websocket_factory,
    )
    browser.open("https://x.com/home")
    calls_before_evaluation = websocket_calls

    with pytest.raises(EdgeBrowserError, match="approved internal browser script"):
        browser.evaluate(script)

    assert websocket_calls == calls_before_evaluation


def test_evaluate_has_total_deadline_across_unrelated_cdp_events(tmp_path: Path) -> None:
    profile = tmp_path / "edge-profile"
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    clock = ClockDouble()
    event_socket = SteadyEventSocketDouble(clock)
    sockets: list[SocketDouble] = [
        SocketDouble({}),
        SocketDouble(ready_location("/home")),
        event_socket,
    ]

    def process_factory(arguments: list[str]) -> ProcessDouble:
        install_endpoint(profile)
        return ProcessDouble()

    browser = EdgeBrowser(
        profile,
        edge_path=edge_path,
        process_factory=process_factory,
        http_json=lambda url, timeout_s: x_target(),
        websocket_factory=lambda url, timeout_s: sockets.pop(0),
        clock=clock,
        sleep=clock.sleep,
    )
    browser.open("https://x.com/home")

    with pytest.raises(EdgeUnavailableError, match="command timed out"):
        browser.evaluate(build_session_check_script(), timeout_s=0.25)

    assert event_socket.closed
    assert event_socket.receive_count == 3
    assert event_socket.timeouts == pytest.approx([0.25, 0.15, 0.05])


def test_stop_returns_promptly_when_another_command_holds_lock(tmp_path: Path) -> None:
    profile = tmp_path / "edge-profile"
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    blocking_socket = BlockingSocketDouble({"result": {"type": "boolean", "value": False}})
    sockets: list[SocketDouble] = [
        SocketDouble({}),
        SocketDouble(ready_location("/home")),
        blocking_socket,
        SocketDouble({}),
    ]
    evaluation_results: list[object] = []

    def process_factory(arguments: list[str]) -> ProcessDouble:
        install_endpoint(profile)
        return ProcessDouble()

    browser = EdgeBrowser(
        profile,
        edge_path=edge_path,
        process_factory=process_factory,
        http_json=lambda url, timeout_s: x_target(),
        websocket_factory=lambda url, timeout_s: sockets.pop(0),
    )
    browser.open("https://x.com/home")
    evaluation_thread = threading.Thread(
        target=lambda: evaluation_results.append(
            browser.evaluate(build_session_check_script(), timeout_s=1.0)
        )
    )
    evaluation_thread.start()
    assert blocking_socket.receive_started.wait(0.2)

    started = time.monotonic()
    browser.stop()
    elapsed = time.monotonic() - started
    blocking_socket.release_receive.set()
    evaluation_thread.join(timeout=1.0)

    assert elapsed < 0.15
    assert not evaluation_thread.is_alive()
    assert evaluation_results == [False]


def test_open_rejects_non_x_final_location(tmp_path: Path) -> None:
    profile = tmp_path / "edge-profile"
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    responses: list[object] = [{}, ready_location("/stolen", hostname="example.com")]

    def process_factory(arguments: list[str]) -> ProcessDouble:
        install_endpoint(profile)
        return ProcessDouble()

    browser = EdgeBrowser(
        profile,
        edge_path=edge_path,
        process_factory=process_factory,
        http_json=lambda url, timeout_s: x_target(),
        websocket_factory=lambda url, timeout_s: SocketDouble(responses.pop(0)),
    )

    with pytest.raises(EdgeProtocolError, match="supported X URL"):
        browser.open("https://x.com/home")


def test_clear_x_site_data_targets_only_two_x_origins(tmp_path: Path) -> None:
    profile = tmp_path / "edge-profile"
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    sockets: list[SocketDouble] = []
    responses: list[object] = [{}, ready_location("/home"), {}, {}]

    def process_factory(arguments: list[str]) -> ProcessDouble:
        install_endpoint(profile)
        return ProcessDouble()

    def websocket_factory(url: str, timeout_s: float) -> SocketDouble:
        socket = SocketDouble(responses.pop(0))
        sockets.append(socket)
        return socket

    browser = EdgeBrowser(
        profile,
        edge_path=edge_path,
        process_factory=process_factory,
        http_json=lambda url, timeout_s: x_target(),
        websocket_factory=websocket_factory,
    )
    browser.open("https://x.com/home")

    browser.clear_x_site_data()

    clear_requests = [socket.sent for socket in sockets[2:]]
    assert [request["method"] for request in clear_requests if request is not None] == [
        "Storage.clearDataForOrigin",
        "Storage.clearDataForOrigin",
    ]
    assert [request["params"] for request in clear_requests if request is not None] == [
        {"origin": "https://x.com", "storageTypes": "all"},
        {"origin": "https://www.x.com", "storageTypes": "all"},
    ]


def test_stop_and_close_affect_only_launched_process(tmp_path: Path) -> None:
    profile = tmp_path / "edge-profile"
    edge_path = tmp_path / "msedge.exe"
    edge_path.touch()
    process = ProcessDouble()
    sockets: list[SocketDouble] = []
    responses: list[object] = [{}, ready_location("/home"), {}]

    def process_factory(arguments: list[str]) -> ProcessDouble:
        install_endpoint(profile)
        return process

    def websocket_factory(url: str, timeout_s: float) -> SocketDouble:
        socket = SocketDouble(responses.pop(0))
        sockets.append(socket)
        return socket

    browser = EdgeBrowser(
        profile,
        edge_path=edge_path,
        process_factory=process_factory,
        http_json=lambda url, timeout_s: x_target(),
        websocket_factory=websocket_factory,
    )
    browser.open("https://x.com/home")

    browser.stop()
    browser.close()

    assert sockets[-1].sent is not None
    assert sockets[-1].sent["method"] == "Page.stopLoading"
    assert process.terminated
    assert process.wait_timeouts == [2.0]
