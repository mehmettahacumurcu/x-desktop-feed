import http.client
import json
import socket
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from xfeed.collection import CandidateObservation, DiscoveryReason
from xfeed.domain import CollectionTarget, CollectionTargetKind
from xfeed.media import PhotoCandidate
from xfeed.opera_bridge import (
    BridgeAuthenticationError,
    BridgeBusyError,
    BridgeError,
    BridgeSnapshot,
    OperaBridge,
)
from xfeed.opera_protocol import (
    PROTOCOL_VERSION,
    ExtensionXState,
    OperaJobResult,
    OperaPhotoJob,
    OperaPhotoResolutionResult,
)
from xfeed.sources import canonicalize_profile as canonicalize_source_profile


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def observation(post_id: str, **changes: object) -> CandidateObservation:
    value = CandidateObservation(
        url=f"https://x.com/openai/status/{post_id}",
        post_id=post_id,
        author_handle="openai",
    )
    return replace(value, **changes)


def source_target(handle: str = "openai") -> CollectionTarget:
    profile = canonicalize_source_profile(handle)
    return CollectionTarget.for_source(1, profile.handle, profile.profile_url)


def paired_bridge(tmp_path, clock=None, *, connected=True):
    bridge = OperaBridge(tmp_path, clock=clock or FakeClock())
    token = bridge.pair(bridge.issue_pairing_code().code)
    if connected:
        bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
    return bridge, token


def request_json(
    bridge: OperaBridge,
    method: str,
    path: str,
    payload: object,
    *,
    token: str | None = None,
) -> dict[str, object]:
    host, port = bridge.server_address
    headers = {"Content-Type": "application/json", "Origin": "chrome-extension://test"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"http://{host}:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method=method,
    )
    with urlopen(request, timeout=2) as response:
        decoded = json.loads(response.read().decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


def request_error(
    bridge: OperaBridge,
    method: str,
    path: str,
    payload: bytes = b"{}",
    *,
    content_type: str = "application/json",
    token: str | None = None,
    origin: str = "chrome-extension://test",
) -> tuple[int, dict[str, object], HTTPError]:
    host, port = bridge.server_address
    headers = {"Content-Type": content_type, "Origin": origin}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"http://{host}:{port}{path}",
        data=payload,
        headers=headers,
        method=method,
    )
    with pytest.raises(HTTPError) as raised:
        urlopen(request, timeout=2)
    body = raised.value.read()
    assert len(body) <= 1_024
    decoded = json.loads(body.decode("utf-8"))
    assert isinstance(decoded, dict)
    assert set(decoded) == {"protocol_version", "error"}
    assert decoded["protocol_version"] == PROTOCOL_VERSION
    assert isinstance(decoded["error"], str)
    return raised.value.code, decoded, raised.value


def raw_http_response(bridge: OperaBridge, request: bytes) -> tuple[int, dict[str, str], bytes]:
    with socket.create_connection(bridge.server_address, timeout=2) as connection:
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := connection.recv(4_096):
            chunks.append(chunk)
    raw = b"".join(chunks)
    head, separator, body = raw.partition(b"\r\n\r\n")
    assert separator, "server closed without a complete HTTP response"
    lines = head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {
        name.strip().lower(): value.strip()
        for line in lines[1:]
        for name, value in [line.split(":", 1)]
    }
    assert len(body) == int(headers["content-length"])
    return status, headers, body


def pair_http_bridge(bridge: OperaBridge) -> str:
    offer = bridge.issue_pairing_code()
    paired = request_json(bridge, "POST", "/v1/pair", {"code": offer.code})
    token = paired["token"]
    assert isinstance(token, str)
    return token


def test_http_bridge_pairs_heartbeats_and_returns_one_job(tmp_path):
    clock = FakeClock(100.0)
    bridge = OperaBridge(tmp_path, port=0, clock=clock)
    bridge.start()
    try:
        assert bridge.server_address[0] == "127.0.0.1"
        offer = bridge.issue_pairing_code()
        paired = request_json(bridge, "POST", "/v1/pair", {"code": offer.code})
        assert paired["protocol_version"] == PROTOCOL_VERSION
        token = paired["token"]
        assert isinstance(token, str)
        heartbeat = request_json(
            bridge,
            "POST",
            "/v1/heartbeat",
            {"protocol_version": 4, "x_state": "signed_in"},
            token=token,
        )
        assert heartbeat == {
            "protocol_version": PROTOCOL_VERSION,
            "connected": True,
            "x_state": "signed_in",
        }
        expected = bridge.enqueue(source_target())
        polled = request_json(bridge, "POST", "/v1/jobs/poll", {}, token=token)
        assert polled == {
            "protocol_version": PROTOCOL_VERSION,
            "job": {
                "id": expected.id,
                "target_kind": "source",
                "handle": "openai",
                "profile_url": "https://x.com/openai",
                "maximum": 30,
                "timeout_ms": 45_000,
            },
            "cancelled": False,
        }
    finally:
        bridge.close()


def test_poll_serializes_exact_source_for_you_and_following_job_payloads(tmp_path):
    clock = FakeClock(100.0)
    bridge, token = paired_bridge(tmp_path, clock)

    source_job = bridge.enqueue(source_target(), maximum=30, timeout_s=45)
    assert bridge._poll_job_payload(token) == {
        "id": source_job.id,
        "target_kind": "source",
        "handle": "openai",
        "profile_url": "https://x.com/openai",
        "maximum": 30,
        "timeout_ms": 45_000,
    }
    bridge.complete_job(token, OperaJobResult(source_job.id, DiscoveryReason.EXHAUSTED))

    home_job = bridge.enqueue(CollectionTarget.for_you(), maximum=50, timeout_s=45)
    assert bridge._poll_job_payload(token) == {
        "id": home_job.id,
        "target_kind": "for_you",
        "handle": None,
        "profile_url": "https://x.com/home",
        "maximum": 50,
        "timeout_ms": 45_000,
    }
    bridge.complete_job(token, OperaJobResult(home_job.id, DiscoveryReason.EXHAUSTED))

    following_job = bridge.enqueue(CollectionTarget.following(), maximum=20, timeout_s=45)
    assert bridge._poll_job_payload(token) == {
        "id": following_job.id,
        "target_kind": "following",
        "handle": None,
        "profile_url": "https://x.com/home",
        "maximum": 20,
        "timeout_ms": 45_000,
    }


def test_http_serializes_only_bounded_relative_job_timeout(tmp_path):
    clock = FakeClock(200.0)
    bridge = OperaBridge(tmp_path, port=0, clock=clock)
    bridge.start()
    try:
        token = pair_http_bridge(bridge)
        bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
        short_job = bridge.enqueue(source_target(), timeout_s=2.5)
        clock.advance(0.5)

        short_payload = request_json(bridge, "POST", "/v1/jobs/poll", {}, token=token)

        assert short_payload["job"] == {
            "id": short_job.id,
            "target_kind": "source",
            "handle": "openai",
            "profile_url": "https://x.com/openai",
            "maximum": 30,
            "timeout_ms": 2_000,
        }
        bridge.complete_job(
            token,
            OperaJobResult(short_job.id, DiscoveryReason.EXHAUSTED),
        )
        long_job = bridge.enqueue(source_target("nasa"), timeout_s=60)

        long_payload = request_json(bridge, "POST", "/v1/jobs/poll", {}, token=token)

        assert long_payload["job"] == {
            "id": long_job.id,
            "target_kind": "source",
            "handle": "nasa",
            "profile_url": "https://x.com/nasa",
            "maximum": 30,
            "timeout_ms": 45_000,
        }
    finally:
        bridge.close()


def test_http_poll_holds_job_state_lock_through_timeout_serialization(tmp_path, monkeypatch):
    clock = FakeClock(300.0)
    bridge = OperaBridge(tmp_path, port=0, clock=clock)
    bridge.start()
    release_serialization = Event()
    serialization_started = Event()
    maintenance_finished = Event()
    original_timeout = bridge._client_timeout_ms

    def paused_timeout(job):
        serialization_started.set()
        assert release_serialization.wait(2)
        return original_timeout(job)

    def maintain_leases():
        try:
            bridge._maintain_leases()
        finally:
            maintenance_finished.set()

    monkeypatch.setattr(bridge, "_client_timeout_ms", paused_timeout)
    try:
        token = pair_http_bridge(bridge)
        bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
        bridge.enqueue(source_target(), timeout_s=2)

        with ThreadPoolExecutor(max_workers=2) as pool:
            response = pool.submit(request_json, bridge, "POST", "/v1/jobs/poll", {}, token=token)
            assert serialization_started.wait(2)
            clock.advance(3)
            maintenance = pool.submit(maintain_leases)
            maintenance_was_blocked = not maintenance_finished.wait(0.2)
            release_serialization.set()
            payload = response.result(timeout=2)
            maintenance.result(timeout=2)

        assert maintenance_was_blocked
        assert payload["job"] is None
    finally:
        release_serialization.set()
        bridge.close()


def test_http_health_is_public_and_pairing_rejects_bad_codes(tmp_path):
    clock = FakeClock()
    bridge = OperaBridge(tmp_path, port=0, clock=clock)
    bridge.start()
    try:
        host, port = bridge.server_address
        request = Request(
            f"http://{host}:{port}/v1/health",
            headers={"Origin": "chrome-extension://health"},
            method="GET",
        )
        with urlopen(request, timeout=2) as response:
            assert response.status == 200
            assert json.load(response) == {"protocol_version": PROTOCOL_VERSION, "status": "ok"}

        assert request_error(bridge, "POST", "/v1/pair")[0] == 400
        assert (
            request_error(
                bridge,
                "POST",
                "/v1/pair",
                json.dumps({"code": "INVALID"}).encode(),
            )[0]
            == 400
        )
        offer = bridge.issue_pairing_code()
        clock.advance(120)
        assert (
            request_error(
                bridge,
                "POST",
                "/v1/pair",
                json.dumps({"code": offer.code}).encode(),
            )[0]
            == 400
        )
    finally:
        bridge.close()


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/heartbeat", {"protocol_version": 4, "x_state": "signed_in"}),
        ("/v1/jobs/poll", {}),
        ("/v1/jobs/job-1/progress", {"protocol_version": 4, "observations": []}),
        ("/v1/jobs/%20/progress", {"protocol_version": 4, "observations": []}),
        ("/v1/jobs//progress", {"protocol_version": 4, "observations": []}),
        (
            "/v1/jobs/job-1/complete",
            {"protocol_version": 4, "reason": "exhausted", "diagnostic": None},
        ),
    ],
)
def test_http_authenticated_routes_reject_missing_and_invalid_bearers(tmp_path, path, payload):
    bridge = OperaBridge(tmp_path, port=0)
    bridge.start()
    try:
        token = pair_http_bridge(bridge)
        encoded = json.dumps(payload).encode()
        assert request_error(bridge, "POST", path, encoded)[0] == 401
        assert request_error(bridge, "POST", path, encoded, token=f"{token}x")[0] == 401
    finally:
        bridge.close()


def test_http_preflight_allows_only_extension_origins(tmp_path):
    bridge = OperaBridge(tmp_path, port=0)
    bridge.start()
    try:
        host, port = bridge.server_address
        request = Request(
            f"http://{host}:{port}/v1/jobs/poll",
            headers={
                "Origin": "chrome-extension://abc123",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
            method="OPTIONS",
        )
        with urlopen(request, timeout=2) as response:
            assert response.status == 204
            assert response.headers["Access-Control-Allow-Origin"] == "chrome-extension://abc123"
            assert response.headers["Access-Control-Allow-Methods"] == "POST"
            assert response.headers["Access-Control-Allow-Headers"] == (
                "Authorization, Content-Type"
            )

        assert (
            request_error(
                bridge,
                "OPTIONS",
                "/v1/jobs/poll",
                origin="https://attacker.example",
            )[0]
            == 403
        )
    finally:
        bridge.close()


def test_http_rejects_unbounded_or_malformed_requests_with_bounded_json(tmp_path):
    bridge = OperaBridge(tmp_path, port=0)
    bridge.start()
    try:
        token = pair_http_bridge(bridge)
        assert (
            request_error(
                bridge, "POST", "/v1/heartbeat", b"{}", content_type="text/plain", token=token
            )[0]
            == 415
        )
        assert request_error(bridge, "POST", "/v1/heartbeat", b"not-json", token=token)[0] == 400
        assert request_error(bridge, "POST", "/v1/heartbeat", b"\xff", token=token)[0] == 400
        assert request_error(bridge, "POST", "/v1/heartbeat", b"x" * 65_537, token=token)[0] == 413
        status, _body, error = request_error(bridge, "PUT", "/v1/health")
        assert status == 405
        assert error.headers["Allow"] == "GET, OPTIONS"
        status, _body, error = request_error(bridge, "POST", "/v1/unknown")
        assert status == 404
        assert error.headers["Allow"] is None
        assert (
            request_error(
                bridge,
                "POST",
                "/v1/heartbeat",
                json.dumps({"protocol_version": 1, "x_state": "signed_in"}).encode(),
                token=token,
            )[0]
            == 400
        )
        progress = json.dumps({"protocol_version": 4, "observations": []}).encode()
        assert (
            request_error(bridge, "POST", "/v1/jobs/%20/progress", progress, token=token)[0] == 400
        )
        assert request_error(bridge, "POST", "/v1/jobs//progress", progress, token=token)[0] == 400
        assert (
            request_error(
                bridge,
                "POST",
                f"/v1/jobs/{'x' * 129}/progress",
                progress,
                token=token,
            )[0]
            == 400
        )
    finally:
        bridge.close()


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [("BREW", "/v1/health", "GET, OPTIONS"), ("PROPFIND", "/v1/pair", "POST, OPTIONS")],
)
def test_http_arbitrary_unsupported_verbs_return_bounded_json_405(tmp_path, method, path, allow):
    bridge = OperaBridge(tmp_path, port=0)
    bridge.start()
    try:
        host, port = bridge.server_address
        request = Request(
            f"http://{host}:{port}{path}",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with pytest.raises(HTTPError) as raised:
            urlopen(request, timeout=2)
        assert raised.value.code == 405
        assert raised.value.headers["Allow"] == allow
        body = raised.value.read()
        assert len(body) <= 1_024
        assert json.loads(body) == {
            "protocol_version": PROTOCOL_VERSION,
            "error": "method not allowed",
        }
    finally:
        bridge.close()


def test_http_malformed_absolute_target_returns_json_without_stderr(tmp_path, capsys, caplog):
    bridge = OperaBridge(tmp_path, port=0)
    bridge.start()
    try:
        capsys.readouterr()
        caplog.clear()
        status, _headers, body = raw_http_response(
            bridge,
            b"GET http://[malformed/v1/health HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n",
        )
        assert status == 400
        assert json.loads(body) == {
            "protocol_version": PROTOCOL_VERSION,
            "error": "invalid request target",
        }
        assert capsys.readouterr().err == ""
        assert not caplog.records
    finally:
        bridge.close()


def test_http_deeply_nested_json_is_a_bounded_client_error(tmp_path):
    bridge = OperaBridge(tmp_path, port=0)
    bridge.start()
    try:
        token = pair_http_bridge(bridge)
        deeply_nested = ("[" * 5_000 + "0" + "]" * 5_000).encode()
        assert len(deeply_nested) <= 65_536

        status, body, _error = request_error(
            bridge, "POST", "/v1/heartbeat", deeply_nested, token=token
        )

        assert status == 400
        assert body == {"protocol_version": PROTOCOL_VERSION, "error": "invalid JSON"}
    finally:
        bridge.close()


@pytest.mark.parametrize(
    ("content_length", "expected_status"),
    [("invalid", 400), ("-1", 400), ("65537", 413), ("9" * 5_000, 413)],
)
def test_http_validates_content_length_before_reading(tmp_path, content_length, expected_status):
    bridge = OperaBridge(tmp_path, port=0)
    bridge.start()
    try:
        host, port = bridge.server_address
        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.putrequest("POST", "/v1/pair")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Origin", "chrome-extension://test")
        connection.putheader("Content-Length", content_length)
        connection.endheaders()
        response = connection.getresponse()
        decoded = json.loads(response.read().decode())
        assert response.status == expected_status
        assert set(decoded) == {"protocol_version", "error"}
        connection.close()
    finally:
        bridge.close()


def test_http_progress_completion_and_cancellation_responses(tmp_path):
    bridge = OperaBridge(tmp_path, port=0)
    bridge.start()
    try:
        token = pair_http_bridge(bridge)
        request_json(
            bridge,
            "POST",
            "/v1/heartbeat",
            {"protocol_version": 4, "x_state": "signed_in"},
            token=token,
        )
        job = bridge.enqueue(source_target())
        payload = {
            "protocol_version": 4,
            "observations": [
                {
                    "url": "https://x.com/openai/status/123",
                    "post_id": "123",
                    "author_handle": "openai",
                    "is_pinned": False,
                    "is_reply": False,
                    "is_repost": False,
                    "is_quote": False,
                    "is_promoted": False,
                    "parent_url": None,
                    "discovery_order": 0,
                    "photos": [],
                }
            ],
        }
        assert request_json(
            bridge, "POST", f"/v1/jobs/{job.id}/progress", payload, token=token
        ) == {"protocol_version": PROTOCOL_VERSION, "cancelled": False}
        assert (
            request_error(
                bridge,
                "POST",
                "/v1/jobs/foreign-job/progress",
                json.dumps(payload).encode(),
                token=token,
            )[0]
            == 409
        )
        assert request_json(
            bridge,
            "POST",
            f"/v1/jobs/{job.id}/complete",
            {"protocol_version": 4, "reason": "exhausted", "diagnostic": None},
            token=token,
        ) == {"protocol_version": PROTOCOL_VERSION, "accepted": True}

        cancelled = bridge.enqueue(source_target("nasa"))
        bridge.cancel(cancelled.id)
        payload["observations"] = []
        assert request_json(
            bridge,
            "POST",
            f"/v1/jobs/{cancelled.id}/progress",
            payload,
            token=token,
        ) == {"protocol_version": PROTOCOL_VERSION, "cancelled": True}
    finally:
        bridge.close()


def test_concurrent_http_polls_claim_job_exactly_once(tmp_path):
    bridge = OperaBridge(tmp_path, port=0)
    bridge.start()
    try:
        token = pair_http_bridge(bridge)
        request_json(
            bridge,
            "POST",
            "/v1/heartbeat",
            {"protocol_version": 4, "x_state": "signed_in"},
            token=token,
        )
        job = bridge.enqueue(source_target())
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda _index: request_json(bridge, "POST", "/v1/jobs/poll", {}, token=token),
                    range(8),
                )
            )

        delivered = [result["job"] for result in results if result["job"] is not None]
        assert len(delivered) == 1
        assert delivered[0]["id"] == job.id
        assert request_json(bridge, "POST", "/v1/jobs/poll", {}, token=token)["job"] is None
    finally:
        bridge.close()


def test_http_close_joins_server_thread_and_is_idempotent(tmp_path):
    bridge = OperaBridge(tmp_path, port=0)
    bridge.start()
    host, port = bridge.server_address
    thread = bridge._server_thread
    assert thread is not None and thread.is_alive() and thread.daemon

    bridge.close()
    bridge.close()

    assert not thread.is_alive()
    with pytest.raises(OSError):
        socket.create_connection((host, port), timeout=0.2)


def test_heartbeat_requires_authentication_and_renews_the_lease(tmp_path, qtbot):
    clock = FakeClock(10.0)
    bridge, token = paired_bridge(tmp_path, clock, connected=False)
    assert bridge.snapshot() == BridgeSnapshot(False, None)

    with pytest.raises(BridgeAuthenticationError):
        bridge.heartbeat(f"{token}x", ExtensionXState.SIGNED_IN)

    with qtbot.waitSignal(bridge.snapshot_changed) as changed:
        bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
    assert changed.args == [BridgeSnapshot(True, ExtensionXState.SIGNED_IN)]
    assert bridge.snapshot() == BridgeSnapshot(True, ExtensionXState.SIGNED_IN)

    clock.advance(59)
    assert bridge.snapshot() == BridgeSnapshot(True, ExtensionXState.SIGNED_IN)

    clock.advance(2)
    with qtbot.waitSignal(bridge.snapshot_changed) as expired:
        assert bridge.snapshot() == BridgeSnapshot(False, None)
    assert expired.args == [BridgeSnapshot(False, None)]


def test_revoke_is_atomic_with_authenticated_heartbeat(tmp_path, monkeypatch):
    bridge, token = paired_bridge(tmp_path)
    authenticated = Event()
    continue_heartbeat = Event()
    revoked = Event()

    class PausedPairing:
        def authenticate(self, value):
            assert value == token
            authenticated.set()
            assert continue_heartbeat.wait(2)
            return True

        def revoke(self):
            revoked.set()

    monkeypatch.setattr(bridge, "_pairing", PausedPairing())
    heartbeat = Thread(
        target=bridge.heartbeat,
        args=(token, ExtensionXState.SIGNED_IN),
        daemon=True,
    )
    heartbeat.start()
    assert authenticated.wait(2)

    revoke = Thread(target=bridge.revoke, daemon=True)
    revoke.start()
    revoked.wait(0.2)
    continue_heartbeat.set()
    heartbeat.join(2)
    revoke.join(2)

    assert not heartbeat.is_alive()
    assert not revoke.is_alive()
    assert bridge.snapshot() == BridgeSnapshot(False, None)


@pytest.mark.parametrize("maximum", [0, 31, True])
def test_bridge_rejects_maximum_outside_integer_range(tmp_path, maximum):
    bridge, _token = paired_bridge(tmp_path)

    with pytest.raises(ValueError, match="between 1 and 30"):
        bridge.enqueue(source_target(), maximum=maximum)


def test_target_maximum_validation_precedes_active_job_state_mutation(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    active = bridge.enqueue(source_target(), maximum=30)

    with pytest.raises(ValueError, match="between 1 and 30"):
        bridge.enqueue(source_target("nasa"), maximum=31)
    with pytest.raises(ValueError, match="between 1 and 50"):
        bridge.enqueue(CollectionTarget.for_you(), maximum=51)

    assert bridge.poll_job(token) == active


def test_bridge_rejects_legacy_source_profile_before_active_job_state_mutation(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    active = bridge.enqueue(source_target(), maximum=30)

    with pytest.raises(TypeError, match="target must be a CollectionTarget"):
        bridge.enqueue(canonicalize_source_profile("nasa"), maximum=30)  # type: ignore[arg-type]

    assert bridge.poll_job(token) == active


def test_bridge_enqueues_canonical_job_with_monotonic_deadline(tmp_path):
    clock = FakeClock(50.0)
    bridge, token = paired_bridge(tmp_path, clock)
    bridge.heartbeat(token, ExtensionXState.SIGNED_IN)

    job = bridge.enqueue(source_target("OpenAI"), maximum=12, timeout_s=7.5)

    assert job.target_kind is CollectionTargetKind.SOURCE
    assert job.handle == "openai"
    assert job.profile_url == "https://x.com/openai"
    assert job.maximum == 12
    assert job.deadline_at == 57.5
    assert bridge.poll_job(token) == job


def test_enqueue_requires_a_current_signed_in_lease(tmp_path):
    bridge, token = paired_bridge(tmp_path, connected=False)

    with pytest.raises(BridgeError, match="connected and signed in"):
        bridge.enqueue(source_target())

    bridge.heartbeat(token, ExtensionXState.SIGNED_OUT)
    with pytest.raises(BridgeError, match="connected and signed in"):
        bridge.enqueue(source_target())

    bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
    assert bridge.enqueue(source_target()).handle == "openai"


def test_started_bridge_expires_heartbeat_without_another_request(tmp_path, qtbot):
    clock = FakeClock(10.0)
    bridge = OperaBridge(tmp_path, port=0, clock=clock)
    bridge.start()
    try:
        token = bridge.pair(bridge.issue_pairing_code().code)
        snapshots: list[BridgeSnapshot] = []
        bridge.snapshot_changed.connect(snapshots.append)
        bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
        snapshots.clear()

        clock.advance(61)

        qtbot.waitUntil(
            lambda: snapshots == [BridgeSnapshot(False, None)],
            timeout=1_000,
        )
        qtbot.wait(250)
        assert snapshots == [BridgeSnapshot(False, None)]
    finally:
        bridge.close()


def test_started_bridge_times_out_job_without_another_request(tmp_path, qtbot):
    clock = FakeClock(20.0)
    bridge = OperaBridge(tmp_path, port=0, clock=clock)
    bridge.start()
    try:
        token = bridge.pair(bridge.issue_pairing_code().code)
        bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
        job = bridge.enqueue(source_target(), timeout_s=1)
        finished: list[OperaJobResult] = []
        bridge.job_finished.connect(finished.append)

        clock.advance(1)

        qtbot.waitUntil(
            lambda: finished == [OperaJobResult(job.id, DiscoveryReason.TIMEOUT)],
            timeout=1_000,
        )
        qtbot.wait(250)
        assert finished == [OperaJobResult(job.id, DiscoveryReason.TIMEOUT)]
    finally:
        bridge.close()


def test_autonomous_disconnect_precedes_one_terminal_job_event(tmp_path, qtbot):
    clock = FakeClock(30.0)
    bridge = OperaBridge(tmp_path, port=0, clock=clock)
    bridge.start()
    try:
        token = bridge.pair(bridge.issue_pairing_code().code)
        bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
        job = bridge.enqueue(source_target(), timeout_s=120)
        events: list[tuple[str, object]] = []
        bridge.snapshot_changed.connect(lambda value: events.append(("snapshot", value)))
        bridge.job_finished.connect(lambda value: events.append(("finished", value)))

        clock.advance(61)

        qtbot.waitUntil(lambda: len(events) == 2, timeout=1_000)
        qtbot.wait(250)
        assert events == [
            ("snapshot", BridgeSnapshot(False, None)),
            (
                "finished",
                OperaJobResult(
                    job.id,
                    DiscoveryReason.ERROR,
                    "Opera GX extension is disconnected",
                ),
            ),
        ]
    finally:
        bridge.close()


def test_maintenance_timer_stops_on_close_and_restarts_idempotently(tmp_path, qtbot):
    clock = FakeClock(40.0)
    bridge = OperaBridge(tmp_path, port=0, clock=clock)
    bridge.start()
    token = bridge.pair(bridge.issue_pairing_code().code)
    bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
    snapshots: list[BridgeSnapshot] = []
    bridge.snapshot_changed.connect(snapshots.append)

    bridge.close()
    bridge.close()
    snapshots.clear()
    clock.advance(61)
    qtbot.wait(250)
    assert snapshots == []

    bridge.start()
    try:
        bridge.start()
        bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
        snapshots.clear()
        clock.advance(61)
        qtbot.waitUntil(
            lambda: snapshots == [BridgeSnapshot(False, None)],
            timeout=1_000,
        )
    finally:
        bridge.close()


def test_poll_publishes_expired_heartbeat_before_terminalizing_job(tmp_path):
    clock = FakeClock()
    bridge, token = paired_bridge(tmp_path, clock)
    bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
    job = bridge.enqueue(source_target(), timeout_s=120)
    events: list[tuple[str, object]] = []
    bridge.snapshot_changed.connect(lambda value: events.append(("snapshot", value)))
    bridge.job_finished.connect(lambda value: events.append(("finished", value)))
    clock.advance(61)

    assert bridge.poll_job(token) is None

    assert events == [
        ("snapshot", BridgeSnapshot(False, None)),
        (
            "finished",
            OperaJobResult(
                job.id,
                DiscoveryReason.ERROR,
                "Opera GX extension is disconnected",
            ),
        ),
    ]
    assert bridge.snapshot() == BridgeSnapshot(False, None)
    assert len(events) == 2


@pytest.mark.parametrize(
    ("x_state", "reason", "diagnostic"),
    [
        (
            ExtensionXState.SIGNED_OUT,
            DiscoveryReason.LOGIN_WALL,
            "X is signed out in Opera GX; sign in and reconnect",
        ),
        (
            ExtensionXState.CHALLENGE,
            DiscoveryReason.ERROR,
            "X presented an account challenge in Opera GX",
        ),
        (
            ExtensionXState.RATE_LIMITED,
            DiscoveryReason.ERROR,
            "X rate-limited the Opera GX session",
        ),
    ],
)
def test_unsafe_heartbeat_terminalizes_active_job(tmp_path, qtbot, x_state, reason, diagnostic):
    bridge, token = paired_bridge(tmp_path)
    bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
    job = bridge.enqueue(source_target())

    with qtbot.waitSignal(bridge.job_finished) as finished:
        bridge.heartbeat(token, x_state)

    assert finished.args == [OperaJobResult(job.id, reason, diagnostic)]
    assert bridge.poll_job(token) is None


def test_bridge_serializes_job_progress_and_completion(tmp_path, qtbot):
    bridge, token = paired_bridge(tmp_path)
    job = bridge.enqueue(source_target(), maximum=30, timeout_s=45)
    with pytest.raises(BridgeBusyError):
        bridge.enqueue(source_target("nasa"))

    with qtbot.waitSignal(bridge.candidate_received) as progress:
        assert not bridge.report_progress(token, job.id, (observation("123"),))
    assert progress.args == [job.id, observation("123")]

    with qtbot.waitSignal(bridge.job_finished) as finished:
        bridge.complete_job(token, OperaJobResult(job.id, DiscoveryReason.EXHAUSTED))
    assert finished.args[0] == OperaJobResult(job.id, DiscoveryReason.EXHAUSTED)


def test_bridge_emits_each_post_id_only_once(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    job = bridge.enqueue(source_target())
    received: list[tuple[str, CandidateObservation]] = []
    bridge.candidate_received.connect(lambda job_id, value: received.append((job_id, value)))

    bridge.report_progress(
        token,
        job.id,
        (observation("1"), observation("1", discovery_order=9), observation("2")),
    )
    bridge.report_progress(token, job.id, (observation("2"),))

    assert received == [(job.id, observation("1")), (job.id, observation("2"))]


def test_bridge_enforces_job_maximum_across_progress_batches(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    job = bridge.enqueue(source_target(), maximum=3)
    received: list[CandidateObservation] = []
    bridge.candidate_received.connect(lambda _job_id, value: received.append(value))

    bridge.report_progress(token, job.id, (observation("1"), observation("2")))
    bridge.report_progress(token, job.id, (observation("3"), observation("4")))
    bridge.report_progress(token, job.id, (observation("5"),))

    assert [value.post_id for value in received] == ["1", "2", "3"]


def test_bridge_enforces_maximum_one_within_a_single_batch(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    job = bridge.enqueue(source_target(), maximum=1)
    received: list[CandidateObservation] = []
    bridge.candidate_received.connect(lambda _job_id, value: received.append(value))

    bridge.report_progress(token, job.id, (observation("1"), observation("2")))

    assert received == [observation("1")]


def test_bridge_never_emits_more_than_hard_limit_across_batches(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    job = bridge.enqueue(source_target(), maximum=30)
    received: list[CandidateObservation] = []
    bridge.candidate_received.connect(lambda _job_id, value: received.append(value))

    bridge.report_progress(token, job.id, tuple(observation(str(index)) for index in range(20)))
    bridge.report_progress(token, job.id, tuple(observation(str(index)) for index in range(20, 40)))

    assert len(received) == 30


def test_for_you_job_accepts_fifty_and_bounds_progress_across_batches(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    job = bridge.enqueue(CollectionTarget.for_you(), maximum=50)
    received: list[CandidateObservation] = []
    bridge.candidate_received.connect(lambda _job_id, value: received.append(value))

    bridge.report_progress(token, job.id, tuple(observation(str(index)) for index in range(30)))
    bridge.report_progress(
        token,
        job.id,
        tuple(observation(str(index)) for index in range(30, 60)),
    )

    assert job.target_kind is CollectionTargetKind.FOR_YOU
    assert len(received) == 50
    assert [value.post_id for value in received] == [str(index) for index in range(50)]


def test_bridge_rejects_oversized_or_invalid_batches_before_any_signal(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    job = bridge.enqueue(source_target())
    received: list[CandidateObservation] = []
    bridge.candidate_received.connect(lambda _job_id, value: received.append(value))

    with pytest.raises(BridgeError):
        bridge.report_progress(
            token,
            job.id,
            (observation(str(index)) for index in range(51)),
        )
    with pytest.raises(BridgeError):
        bridge.report_progress(token, job.id, (observation("valid"), object()))

    assert received == []


def test_bridge_rejects_foreign_and_stale_job_replies(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    job = bridge.enqueue(source_target())

    with pytest.raises(BridgeError):
        bridge.report_progress(token, "foreign-job", (observation("1"),))
    with pytest.raises(BridgeError):
        bridge.complete_job(token, OperaJobResult("foreign-job", DiscoveryReason.EXHAUSTED))

    bridge.complete_job(token, OperaJobResult(job.id, DiscoveryReason.EXHAUSTED))
    with pytest.raises(BridgeError):
        bridge.complete_job(token, OperaJobResult(job.id, DiscoveryReason.EXHAUSTED))


def test_cancel_is_idempotent_and_late_progress_observes_cancellation(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    job = bridge.enqueue(source_target())
    finished: list[OperaJobResult] = []
    bridge.job_finished.connect(finished.append)

    bridge.cancel(job.id)
    bridge.cancel(job.id)

    assert finished == [OperaJobResult(job.id, DiscoveryReason.CANCELLED)]
    assert bridge.poll_job(token) is None
    assert bridge.report_progress(token, job.id, (observation("late"),))


def test_cancelling_old_job_remains_idempotent_while_successor_is_active(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    old_job = bridge.enqueue(source_target())
    bridge.cancel(old_job.id)
    successor = bridge.enqueue(source_target("nasa"))

    bridge.cancel(old_job.id)

    bridge.complete_job(token, OperaJobResult(successor.id, DiscoveryReason.EXHAUSTED))


def test_expired_job_clears_before_terminal_signal_and_allows_reentrant_enqueue(tmp_path, qtbot):
    clock = FakeClock()
    bridge, token = paired_bridge(tmp_path, clock)
    expired = bridge.enqueue(source_target(), timeout_s=1)
    replacement = []
    bridge.job_finished.connect(
        lambda _result: replacement.append(bridge.enqueue(source_target("nasa")))
    )
    clock.advance(1)

    with qtbot.waitSignal(bridge.job_finished) as finished:
        assert bridge.poll_job(token) is None

    assert finished.args == [OperaJobResult(expired.id, DiscoveryReason.TIMEOUT)]
    assert replacement[0].handle == "nasa"


def test_completion_clears_active_job_before_reentrant_signal_consumer(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    completed = bridge.enqueue(source_target())
    replacement = []
    bridge.job_finished.connect(
        lambda _result: replacement.append(bridge.enqueue(source_target("nasa")))
    )

    bridge.complete_job(token, OperaJobResult(completed.id, DiscoveryReason.LIMIT))

    assert replacement[0].handle == "nasa"


def test_revoke_disconnects_authentication_and_cancels_active_job(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
    job = bridge.enqueue(source_target())
    finished: list[OperaJobResult] = []
    bridge.job_finished.connect(finished.append)

    bridge.revoke()

    assert bridge.snapshot() == BridgeSnapshot(False, None)
    assert finished == [OperaJobResult(job.id, DiscoveryReason.CANCELLED)]
    with pytest.raises(BridgeAuthenticationError):
        bridge.authenticate(token)


def test_all_bridge_signals_run_after_the_state_lock_is_released(tmp_path, qtbot):
    bridge, token = paired_bridge(tmp_path, connected=False)

    def assert_lock_is_available(*_args):
        available = Event()

        def observe_snapshot():
            bridge.snapshot()
            available.set()

        observer = Thread(target=observe_snapshot, daemon=True)
        observer.start()
        observer.join(1)
        assert available.is_set()

    bridge.snapshot_changed.connect(assert_lock_is_available)
    with qtbot.waitSignal(bridge.snapshot_changed):
        bridge.heartbeat(token, ExtensionXState.SIGNED_IN)

    job = bridge.enqueue(source_target())
    bridge.candidate_received.connect(assert_lock_is_available)
    with qtbot.waitSignal(bridge.candidate_received):
        bridge.report_progress(token, job.id, (observation("1"),))

    bridge.job_finished.connect(assert_lock_is_available)
    with qtbot.waitSignal(bridge.job_finished):
        bridge.complete_job(token, OperaJobResult(job.id, DiscoveryReason.EXHAUSTED))


def test_reentrant_signal_transitions_drain_in_fifo_order(tmp_path):
    bridge, token = paired_bridge(tmp_path)
    job = bridge.enqueue(source_target())
    events: list[str] = []

    def cancel_after_first_candidate(job_id, value):
        events.append(f"candidate:{value.post_id}")
        if value.post_id == "1":
            bridge.cancel(job_id)

    bridge.candidate_received.connect(cancel_after_first_candidate)
    bridge.job_finished.connect(lambda result: events.append(f"finished:{result.reason}"))

    bridge.report_progress(token, job.id, (observation("1"), observation("2")))

    assert events == ["candidate:1", "candidate:2", "finished:cancelled"]


def test_bridge_enqueues_and_serializes_exact_photo_resolution_job(tmp_path) -> None:
    clock = FakeClock(100.0)
    bridge, token = paired_bridge(tmp_path, clock)

    job = bridge.enqueue_photo_resolution(
        post_id=123,
        post_url="https://x.com/openai/status/123",
        timeout_s=45,
    )

    assert job == OperaPhotoJob(
        id=job.id,
        post_id="123",
        post_url="https://x.com/openai/status/123",
        deadline_at=145.0,
    )
    assert bridge._poll_job_payload(token) == {
        "id": job.id,
        "kind": "resolve_post_photos",
        "post_id": "123",
        "post_url": "https://x.com/openai/status/123",
        "timeout_ms": 45_000,
    }


@pytest.mark.parametrize(
    ("post_id", "post_url"),
    [
        (123, "https://x.com/openai/status/456"),
        (123, "https://x.com/OpenAI/status/123"),
        (123, "https://www.x.com/openai/status/123"),
        (123, "https://twitter.com/openai/status/123"),
        (123, "https://evil.example/openai/status/123"),
        (123, "https://x.com/openai/status/123?ref=test"),
        (0, "https://x.com/openai/status/0"),
        (True, "https://x.com/openai/status/1"),
    ],
)
def test_photo_enqueue_rejects_mismatch_or_noncanonical_routes(
    tmp_path,
    post_id: object,
    post_url: str,
) -> None:
    bridge, _token = paired_bridge(tmp_path)

    with pytest.raises((TypeError, ValueError)):
        bridge.enqueue_photo_resolution(  # type: ignore[arg-type]
            post_id=post_id,
            post_url=post_url,
        )


def test_collection_and_photo_jobs_share_one_active_slot(tmp_path) -> None:
    bridge, token = paired_bridge(tmp_path)
    collection = bridge.enqueue(source_target())

    with pytest.raises(BridgeBusyError):
        bridge.enqueue_photo_resolution(
            post_id=123,
            post_url="https://x.com/openai/status/123",
        )

    bridge.complete_job(token, OperaJobResult(collection.id, DiscoveryReason.EXHAUSTED))
    media = bridge.enqueue_photo_resolution(
        post_id=123,
        post_url="https://x.com/openai/status/123",
    )
    with pytest.raises(BridgeBusyError):
        bridge.enqueue(source_target("nasa"))
    assert bridge.poll_job(token) == media


def test_media_job_rejects_collection_progress_and_completion_shapes(tmp_path) -> None:
    bridge, token = paired_bridge(tmp_path)
    job = bridge.enqueue_photo_resolution(
        post_id=123,
        post_url="https://x.com/openai/status/123",
    )

    with pytest.raises(BridgeError):
        bridge.report_progress(token, job.id, ())
    with pytest.raises(BridgeError):
        bridge.complete_job(token, OperaJobResult(job.id, DiscoveryReason.EXHAUSTED))

    result = OperaPhotoResolutionResult(
        job.id,
        DiscoveryReason.EXHAUSTED,
        (PhotoCandidate(0, "https://pbs.twimg.com/media/abc?name=large"),),
    )
    emitted: list[OperaPhotoResolutionResult] = []
    bridge.photo_job_finished.connect(emitted.append)
    bridge.complete_job(token, result)
    assert emitted == [result]


def test_collection_job_rejects_media_completion_shape(tmp_path) -> None:
    bridge, token = paired_bridge(tmp_path)
    job = bridge.enqueue(source_target())

    with pytest.raises(BridgeError):
        bridge.complete_job(
            token,
            OperaPhotoResolutionResult(
                job.id,
                DiscoveryReason.EXHAUSTED,
                (),
            ),
        )

    bridge.complete_job(token, OperaJobResult(job.id, DiscoveryReason.EXHAUSTED))


def test_http_photo_completion_emits_photos_only_on_media_signal(tmp_path, qtbot) -> None:
    bridge = OperaBridge(tmp_path, port=0)
    bridge.start()
    collection_events: list[OperaJobResult] = []
    media_events: list[OperaPhotoResolutionResult] = []
    bridge.job_finished.connect(collection_events.append)
    bridge.photo_job_finished.connect(media_events.append)
    try:
        token = pair_http_bridge(bridge)
        bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
        job = bridge.enqueue_photo_resolution(
            post_id=123,
            post_url="https://x.com/openai/status/123",
        )

        with qtbot.waitSignal(bridge.photo_job_finished):
            response = request_json(
                bridge,
                "POST",
                f"/v1/jobs/{job.id}/complete",
                {
                    "protocol_version": 4,
                    "reason": "exhausted",
                    "diagnostic": None,
                    "photos": [
                        {
                            "position": 0,
                            "url": "https://pbs.twimg.com/media/abc?name=small",
                            "alt_text": None,
                        }
                    ],
                },
                token=token,
            )

        assert response == {"protocol_version": 4, "accepted": True}
        assert collection_events == []
        assert media_events == [
            OperaPhotoResolutionResult(
                job.id,
                DiscoveryReason.EXHAUSTED,
                (PhotoCandidate(0, "https://pbs.twimg.com/media/abc?name=large"),),
            )
        ]
    finally:
        bridge.close()


def test_media_timeout_and_cancel_emit_one_media_terminal_each(tmp_path) -> None:
    clock = FakeClock()
    bridge, token = paired_bridge(tmp_path, clock)
    emitted: list[OperaPhotoResolutionResult] = []
    bridge.photo_job_finished.connect(emitted.append)
    timed_out = bridge.enqueue_photo_resolution(
        post_id=123,
        post_url="https://x.com/openai/status/123",
        timeout_s=1,
    )
    clock.advance(1)

    assert bridge.poll_job(token) is None

    cancelled = bridge.enqueue_photo_resolution(
        post_id=456,
        post_url="https://x.com/openai/status/456",
    )
    bridge.cancel(cancelled.id)
    bridge.cancel(cancelled.id)
    assert emitted == [
        OperaPhotoResolutionResult(
            timed_out.id,
            DiscoveryReason.TIMEOUT,
            (),
        ),
        OperaPhotoResolutionResult(
            cancelled.id,
            DiscoveryReason.CANCELLED,
            (),
        ),
    ]


def test_http_cancel_ack_precedes_delivery_of_a_successor_job(tmp_path) -> None:
    bridge = OperaBridge(tmp_path, port=0, clock=FakeClock(100.0))
    bridge.start()
    try:
        token = pair_http_bridge(bridge)
        bridge.heartbeat(token, ExtensionXState.SIGNED_IN)
        cancelled = bridge.enqueue_photo_resolution(
            post_id=123,
            post_url="https://x.com/openai/status/123",
        )
        assert request_json(bridge, "POST", "/v1/jobs/poll", {}, token=token)["job"] == {
            "id": cancelled.id,
            "kind": "resolve_post_photos",
            "post_id": "123",
            "post_url": "https://x.com/openai/status/123",
            "timeout_ms": 45_000,
        }
        bridge.cancel(cancelled.id)
        successor = bridge.enqueue(source_target())

        acknowledgement = request_json(bridge, "POST", "/v1/jobs/poll", {}, token=token)
        delivered = request_json(bridge, "POST", "/v1/jobs/poll", {}, token=token)

        assert acknowledgement == {
            "protocol_version": 4,
            "job": None,
            "cancelled": True,
        }
        assert delivered == {
            "protocol_version": 4,
            "job": {
                "id": successor.id,
                "target_kind": "source",
                "handle": "openai",
                "profile_url": "https://x.com/openai",
                "maximum": 30,
                "timeout_ms": 45_000,
            },
            "cancelled": False,
        }
    finally:
        bridge.close()
