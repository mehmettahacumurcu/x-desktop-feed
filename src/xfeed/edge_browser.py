import json
import math
import os
import subprocess
import threading
import time
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, cast, runtime_checkable
from urllib.parse import SplitResult, urlsplit

import websocket


class EdgeBrowserError(RuntimeError):
    """A dedicated Edge browser operation could not be completed."""


class EdgeUnavailableError(EdgeBrowserError):
    """Microsoft Edge could not be found or contacted."""


class EdgeProtocolError(EdgeBrowserError):
    """Microsoft Edge returned an invalid DevTools response."""


@dataclass(frozen=True)
class EdgeEndpoint:
    port: int
    browser_path: str


@runtime_checkable
class EdgeBrowserPort(Protocol):
    def open(self, url: str, timeout_s: float = 15.0) -> str: ...

    def evaluate(self, script: str, timeout_s: float = 10.0) -> object: ...

    def stop(self) -> None: ...

    def clear_x_site_data(self) -> None: ...

    def close(self) -> None: ...


class EdgeProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class EdgeSocket(Protocol):
    def send(self, payload: str) -> object: ...

    def recv(self) -> str | bytes: ...

    def settimeout(self, timeout_s: float) -> None: ...

    def close(self) -> object: ...


ProcessFactory = Callable[[list[str]], EdgeProcess]
HttpJson = Callable[[str, float], object]
WebSocketFactory = Callable[[str, float], EdgeSocket]


RATE_LIMIT_SCRIPT = r"""
(() => {
    const visibleText = (document.body?.innerText || '').toLowerCase();
    return ['rate limit', 'temporarily limited', 'try again later'].some(
        (marker) => visibleText.includes(marker)
    );
})()
""".strip()


def find_edge_executable(
    explicit_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    if explicit_path is not None:
        candidate = Path(explicit_path).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise EdgeUnavailableError("Microsoft Edge is not available at the configured location")

    environment = os.environ if environ is None else environ
    candidates = (
        ("ProgramFiles(x86)", Path("Microsoft/Edge/Application/msedge.exe")),
        ("ProgramFiles", Path("Microsoft/Edge/Application/msedge.exe")),
        ("LOCALAPPDATA", Path("Microsoft/Edge/Application/msedge.exe")),
    )
    for variable, suffix in candidates:
        root = environment.get(variable)
        if not root:
            continue
        candidate = (Path(root) / suffix).resolve()
        if candidate.is_file():
            return candidate
    raise EdgeUnavailableError(
        "Microsoft Edge is not available in a standard installation location"
    )


def build_edge_arguments(
    profile_path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    profile = Path(profile_path).expanduser().resolve()
    environment = os.environ if environ is None else environ
    local_app_data = environment.get("LOCALAPPDATA")
    if local_app_data:
        normal_profile = (Path(local_app_data) / "Microsoft" / "Edge" / "User Data").resolve()
        if os.path.normcase(profile) == os.path.normcase(normal_profile):
            raise EdgeBrowserError("Edge requires a dedicated Edge profile directory")
    return [
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        "--new-window",
    ]


def parse_devtools_active_port(payload: str) -> EdgeEndpoint:
    lines = payload.splitlines()
    if len(lines) != 2 or any(not line.strip() for line in lines):
        raise EdgeProtocolError("Edge DevTools endpoint data is malformed")
    try:
        port = int(lines[0])
    except ValueError as error:
        raise EdgeProtocolError("Edge DevTools port is invalid") from error
    browser_path = lines[1].strip()
    if not 1 <= port <= 65_535:
        raise EdgeProtocolError("Edge DevTools port is outside the valid range")
    prefix = "/devtools/browser/"
    if not browser_path.startswith(prefix) or browser_path == prefix:
        raise EdgeProtocolError("Edge DevTools browser path is invalid")
    return EdgeEndpoint(port=port, browser_path=browser_path)


def _start_process(arguments: list[str]) -> EdgeProcess:
    return cast(EdgeProcess, subprocess.Popen(arguments))


def _read_http_json(url: str, timeout_s: float) -> object:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def _open_websocket(url: str, timeout_s: float) -> EdgeSocket:
    return cast(
        EdgeSocket,
        websocket.create_connection(url, timeout=timeout_s, suppress_origin=True),
    )


def _ignore_diagnostic(message: str) -> None:
    del message


class EdgeBrowser:
    _STOP_LOCK_TIMEOUT_S = 0.05
    _READY_STATE_SCRIPT = r"""
(() => ({
    ready_state: document.readyState,
    page_scheme: location.protocol,
    page_hostname: location.hostname,
    page_port: location.port,
    page_path: location.pathname
}))()
""".strip()

    def __init__(
        self,
        profile_path: str | Path,
        *,
        edge_path: str | Path | None = None,
        process_factory: ProcessFactory = _start_process,
        http_json: HttpJson = _read_http_json,
        websocket_factory: WebSocketFactory = _open_websocket,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        diagnostics: Callable[[str], None] = _ignore_diagnostic,
        poll_interval_s: float = 0.05,
    ) -> None:
        if not math.isfinite(poll_interval_s) or poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be finite and positive")
        self._profile_path = Path(profile_path).expanduser().resolve()
        self._edge_path = find_edge_executable(edge_path)
        self._process_factory = process_factory
        self._http_json = http_json
        self._websocket_factory = websocket_factory
        self._clock = clock
        self._sleep = sleep
        self._diagnostics = diagnostics
        self._poll_interval_s = poll_interval_s
        self._process: EdgeProcess | None = None
        self._endpoint: EdgeEndpoint | None = None
        self._target_websocket: str | None = None
        self._request_id = 0
        self._command_timeout_s = 10.0
        self._command_deadline: float | None = None
        self._command_lock = threading.Lock()

    def open(self, url: str, timeout_s: float = 15.0) -> str:
        self._validate_x_url(url)
        deadline = self._deadline(timeout_s)
        self._ensure_process(url)
        self._endpoint = self._wait_for_endpoint(deadline)
        self._target_websocket = self._wait_for_x_target(deadline)
        self._diagnostics("Connected to the dedicated Edge browser")

        navigation = self._call_with_timeout(
            "Page.navigate",
            {"url": url},
            self._remaining(deadline, "Edge page navigation timed out"),
        )
        if isinstance(navigation, dict) and navigation.get("errorText"):
            raise EdgeProtocolError("Edge could not navigate to the requested X page")

        while True:
            remaining = self._remaining(deadline, "Edge page navigation timed out")
            payload = self._evaluate_with_timeout(self._READY_STATE_SCRIPT, remaining)
            location = self._parse_ready_location(payload)
            if location.get("ready_state") == "complete":
                self._diagnostics("The dedicated Edge page finished loading")
                return cast(str, location["sanitized_url"])
            self._bounded_sleep(deadline)

    def evaluate(self, script: str, timeout_s: float = 10.0) -> object:
        if not isinstance(script, str):
            raise TypeError("script must be a string")
        self._deadline(timeout_s)
        return self._evaluate_with_timeout(script, timeout_s)

    def stop(self) -> None:
        if self._target_websocket is None:
            return
        try:
            self._call_with_timeout(
                "Page.stopLoading",
                timeout_s=2.0,
                lock_timeout_s=self._STOP_LOCK_TIMEOUT_S,
            )
        except EdgeBrowserError:
            pass

    def clear_x_site_data(self) -> None:
        if self._target_websocket is None:
            self.open("https://x.com/home", timeout_s=10.0)
        for origin in ("https://x.com", "https://www.x.com"):
            self._call_with_timeout(
                "Storage.clearDataForOrigin",
                {"origin": origin, "storageTypes": "all"},
                10.0,
            )

    def close(self) -> None:
        process = self._process
        self._process = None
        self._endpoint = None
        self._target_websocket = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            pass

    def _ensure_process(self, url: str) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        edge_arguments = build_edge_arguments(self._profile_path)
        try:
            self._profile_path.mkdir(parents=True, exist_ok=True)
            (self._profile_path / "DevToolsActivePort").unlink(missing_ok=True)
        except OSError as error:
            raise EdgeUnavailableError("Microsoft Edge profile could not be prepared") from error
        arguments = [
            str(self._edge_path),
            *edge_arguments,
            url,
        ]
        try:
            self._process = self._process_factory(arguments)
        except (OSError, subprocess.SubprocessError) as error:
            raise EdgeUnavailableError("Microsoft Edge could not be started") from error
        self._endpoint = None
        self._target_websocket = None
        self._diagnostics("Started the dedicated Edge browser")

    def _wait_for_endpoint(self, deadline: float) -> EdgeEndpoint:
        endpoint_file = self._profile_path / "DevToolsActivePort"
        while True:
            process = self._process
            if process is not None and process.poll() is not None:
                raise EdgeUnavailableError("Microsoft Edge exited before becoming available")
            try:
                return parse_devtools_active_port(endpoint_file.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._remaining(deadline, "Microsoft Edge startup timed out")
                self._bounded_sleep(deadline)
            except OSError as error:
                raise EdgeUnavailableError("Microsoft Edge endpoint could not be read") from error

    def _wait_for_x_target(self, deadline: float) -> str:
        endpoint = self._endpoint
        if endpoint is None:
            raise EdgeUnavailableError("Microsoft Edge endpoint is not available")
        url = f"http://127.0.0.1:{endpoint.port}/json/list"
        while True:
            remaining = self._remaining(deadline, "Microsoft Edge target discovery timed out")
            try:
                payload = self._http_json(url, remaining)
            except (OSError, ValueError) as error:
                if self._clock() >= deadline:
                    raise EdgeUnavailableError(
                        "Microsoft Edge target discovery timed out"
                    ) from error
                self._bounded_sleep(deadline)
                continue
            target = self._parse_target_list(payload, endpoint.port)
            if target is not None:
                return target
            self._bounded_sleep(deadline)

    def _parse_target_list(self, payload: object, endpoint_port: int) -> str | None:
        if not isinstance(payload, list):
            raise EdgeProtocolError("Edge DevTools target list is malformed")
        for item in payload:
            if not isinstance(item, dict):
                raise EdgeProtocolError("Edge DevTools target list is malformed")
            target_type = item.get("type")
            target_url = item.get("url")
            if not isinstance(target_type, str) or not isinstance(target_url, str):
                raise EdgeProtocolError("Edge DevTools target list is malformed")
            if target_type != "page" or not self._is_x_url(target_url):
                continue
            websocket_url = item.get("webSocketDebuggerUrl")
            if not isinstance(websocket_url, str):
                raise EdgeProtocolError("Edge page target is missing its DevTools endpoint")
            self._validate_target_websocket(websocket_url, endpoint_port)
            return websocket_url
        return None

    def _validate_target_websocket(self, url: str, endpoint_port: int) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise EdgeProtocolError("Edge page target endpoint is invalid") from error
        if (
            parsed.scheme != "ws"
            or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or port != endpoint_port
            or not parsed.path.startswith("/devtools/page/")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise EdgeProtocolError("Edge page target endpoint is invalid")

    def _call_with_timeout(
        self,
        method: str,
        params: dict[str, object] | None = None,
        timeout_s: float = 10.0,
        *,
        lock_timeout_s: float | None = None,
    ) -> object:
        if timeout_s <= 0:
            raise EdgeUnavailableError("Edge browser command timed out")
        deadline = self._clock() + timeout_s
        lock_timeout = timeout_s if lock_timeout_s is None else min(timeout_s, lock_timeout_s)
        if not self._command_lock.acquire(timeout=lock_timeout):
            raise EdgeUnavailableError("Edge browser command is busy")
        try:
            previous_timeout = self._command_timeout_s
            previous_deadline = self._command_deadline
            self._command_timeout_s = timeout_s
            self._command_deadline = deadline
            try:
                return self._call(method, params)
            finally:
                self._command_timeout_s = previous_timeout
                self._command_deadline = previous_deadline
        finally:
            self._command_lock.release()

    def _call(self, method: str, params: dict[str, object] | None = None) -> object:
        request_id = self._next_request_id()
        deadline = self._command_deadline
        if deadline is None:
            deadline = self._clock() + self._command_timeout_s
        try:
            socket = self._websocket_factory(
                self._target_websocket_url(),
                self._remaining(deadline, "Edge browser command timed out"),
            )
        except Exception as error:
            raise EdgeUnavailableError("Edge browser command connection failed") from error
        try:
            socket.send(
                json.dumps(
                    {"id": request_id, "method": method, "params": params or {}},
                    separators=(",", ":"),
                )
            )
            while True:
                socket.settimeout(self._remaining(deadline, "Edge browser command timed out"))
                message = self._decode_message(socket.recv())
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise EdgeProtocolError("Edge rejected a browser command")
                return message.get("result", {})
        except EdgeBrowserError:
            raise
        except (TimeoutError, websocket.WebSocketTimeoutException) as error:
            raise EdgeUnavailableError("Edge browser command timed out") from error
        except Exception as error:
            raise EdgeUnavailableError("Edge browser command failed") from error
        finally:
            try:
                socket.close()
            except Exception:
                pass

    def _evaluate_with_timeout(self, script: str, timeout_s: float) -> object:
        self._validate_evaluation_script(script)
        response = self._call_with_timeout(
            "Runtime.evaluate",
            {
                "expression": script,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout_s,
        )
        if not isinstance(response, dict) or "exceptionDetails" in response:
            raise EdgeProtocolError("Edge could not evaluate the browser script")
        remote_object = response.get("result")
        if not isinstance(remote_object, dict):
            raise EdgeProtocolError("Edge returned a malformed script result")
        if "value" in remote_object:
            return remote_object["value"]
        if remote_object.get("type") == "undefined":
            return None
        raise EdgeProtocolError("Edge returned a malformed script result")

    def _validate_evaluation_script(self, script: str) -> None:
        from xfeed.public_collector import SCROLL_SCRIPT, build_extraction_script
        from xfeed.x_session import build_session_check_script

        approved_scripts = (
            self._READY_STATE_SCRIPT,
            build_session_check_script(),
            build_extraction_script("openai"),
            SCROLL_SCRIPT,
            RATE_LIMIT_SCRIPT,
        )
        if script not in approved_scripts:
            raise EdgeBrowserError("Edge accepts only an approved internal browser script")

    def _parse_ready_location(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise EdgeProtocolError("Edge returned a malformed page location")
        components: dict[str, str] = {}
        for field in ("ready_state", "page_scheme", "page_hostname", "page_port", "page_path"):
            value = payload.get(field)
            if not isinstance(value, str):
                raise EdgeProtocolError("Edge returned a malformed page location")
            components[field] = value
        url = self._sanitize_location(
            components["page_scheme"],
            components["page_hostname"],
            components["page_port"],
            components["page_path"],
        )
        return {**payload, "sanitized_url": url}

    def _sanitize_location(self, scheme: str, hostname: str, port: str, path: str) -> str:
        normalized_scheme = scheme.casefold().removesuffix(":")
        normalized_host = hostname.casefold()
        if (
            normalized_scheme != "https"
            or normalized_host not in ("x.com", "www.x.com")
            or port not in ("", "443")
            or not path.startswith("/")
            or any(character in path for character in ("?", "#", "\\", "\r", "\n", "\0"))
        ):
            raise EdgeProtocolError("Edge did not finish on a supported X URL")
        return f"https://{normalized_host}{f':{port}' if port else ''}{path}"

    def _validate_x_url(self, url: str) -> SplitResult:
        if not isinstance(url, str) or any(
            character in url for character in ("\\", "\r", "\n", "\0")
        ):
            raise EdgeBrowserError("Edge requires a supported X URL")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise EdgeBrowserError("Edge requires a supported X URL") from error
        if (
            parsed.scheme.casefold() != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() not in ("x.com", "www.x.com")
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or (parsed.path and not parsed.path.startswith("/"))
        ):
            raise EdgeBrowserError("Edge requires a supported X URL")
        return parsed

    def _is_x_url(self, url: str) -> bool:
        try:
            self._validate_x_url(url)
        except EdgeBrowserError:
            return False
        return True

    def _decode_message(self, payload: str | bytes) -> dict[str, Any]:
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError as error:
                raise EdgeProtocolError("Edge returned a malformed browser message") from error
        if not isinstance(payload, str):
            raise EdgeProtocolError("Edge returned a malformed browser message")
        try:
            message = json.loads(payload)
        except json.JSONDecodeError as error:
            raise EdgeProtocolError("Edge returned a malformed browser message") from error
        if not isinstance(message, dict):
            raise EdgeProtocolError("Edge returned a malformed browser message")
        return cast(dict[str, Any], message)

    def _target_websocket_url(self) -> str:
        if self._target_websocket is None:
            raise EdgeUnavailableError("Edge page target is not available")
        return self._target_websocket

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _deadline(self, timeout_s: float) -> float:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        return self._clock() + timeout_s

    def _remaining(self, deadline: float, message: str) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise EdgeUnavailableError(message)
        return remaining

    def _bounded_sleep(self, deadline: float) -> None:
        remaining = self._remaining(deadline, "Microsoft Edge operation timed out")
        self._sleep(min(self._poll_interval_s, remaining))
