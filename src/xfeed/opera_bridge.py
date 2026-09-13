import json
import math
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import islice
from pathlib import Path
from typing import Final, cast
from urllib.parse import unquote, urlsplit

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal, Slot

from xfeed.collection import CandidateObservation, DiscoveryReason
from xfeed.domain import CollectionTarget, CollectionTargetKind
from xfeed.media import PhotoCandidate
from xfeed.opera_pairing import PairingError, PairingOffer, PairingStore
from xfeed.opera_protocol import (
    BRIDGE_HOST,
    BRIDGE_PORT,
    MAXIMUM_CANDIDATES,
    PROTOCOL_VERSION,
    ExtensionXState,
    OperaCollectionJob,
    OperaJob,
    OperaJobResult,
    OperaJobUnion,
    OperaPhotoJob,
    OperaPhotoResolutionResult,
    ProtocolError,
    parse_heartbeat,
    parse_job_completion,
    parse_observations,
)
from xfeed.urls import InvalidPostUrl, canonicalize_post_url


_HEARTBEAT_LEASE_S: Final = 60.0
_MAINTENANCE_INTERVAL_MS: Final = 100
_MAXIMUM_CLIENT_TIMEOUT_MS: Final = 45_000
_CANCELLED_JOB_HISTORY: Final = 128
_SNAPSHOT_EVENT: Final = "snapshot"
_CANDIDATE_EVENT: Final = "candidate"
_JOB_EVENT: Final = "job"
_PHOTO_JOB_EVENT: Final = "photo_job"
_MAXIMUM_BODY_BYTES: Final = 65_536
_CONNECTION_TIMEOUT_S: Final = 5.0
_SERVER_THREAD_NAME: Final = "OperaBridgeHTTP"
_EXTENSION_ORIGIN = re.compile(r"chrome-extension://[A-Za-z0-9_-]+\Z")
_JOB_PATH = re.compile(r"/v1/jobs/([^/]*)/(progress|complete)\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, _request: object, _client_address: object) -> None:
        return


class BridgeError(RuntimeError):
    pass


class BridgeAuthenticationError(BridgeError):
    pass


class BridgeBusyError(BridgeError):
    pass


@dataclass(frozen=True)
class BridgeSnapshot:
    connected: bool
    x_state: ExtensionXState | None


class OperaBridge(QObject):
    snapshot_changed = Signal(object)
    candidate_received = Signal(object, object)
    job_finished = Signal(object)
    photo_job_finished = Signal(object)

    def __init__(
        self,
        data_dir: Path,
        *,
        host: str = BRIDGE_HOST,
        port: int = BRIDGE_PORT,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._port = port
        self._clock = clock
        self._pairing = PairingStore(data_dir / "pairing.json", clock=clock)
        self._lock = threading.RLock()
        self._transport_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self.server_address: tuple[str, int]
        self._heartbeat_deadline: float | None = None
        self._x_state: ExtensionXState | None = None
        self._published_snapshot = BridgeSnapshot(False, None)
        self._active_job: OperaJobUnion | None = None
        self._active_job_delivered = False
        self._extension_stop_pending = False
        self._seen_post_ids: set[str] = set()
        self._cancelled_job_ids: deque[str] = deque(maxlen=_CANCELLED_JOB_HISTORY)
        self._pending_signals: deque[tuple[str, tuple[object, ...]]] = deque()
        self._signals_draining = False
        self._maintenance_timer: QTimer | None = None

    def start(self) -> None:
        with self._transport_lock:
            if self._server is not None:
                self._start_maintenance_timer()
                return
            if self._host != BRIDGE_HOST:
                raise ValueError("Opera bridge must bind to IPv4 loopback")
            server = _QuietThreadingHTTPServer((self._host, self._port), _handler_for(self))
            self._server = server
            address = server.server_address
            self.server_address = (str(address[0]), int(address[1]))
            thread = threading.Thread(
                target=server.serve_forever,
                name=_SERVER_THREAD_NAME,
                daemon=True,
            )
            self._server_thread = thread
            thread.start()
            self._start_maintenance_timer()

    def close(self) -> None:
        if self._maintenance_timer is not None:
            self._maintenance_timer.stop()
        with self._transport_lock:
            server = self._server
            thread = self._server_thread
            if server is not None:
                server.shutdown()
                server.server_close()
                if thread is not None and thread is not threading.current_thread():
                    thread.join(2.0)
                self._server = None
                self._server_thread = None
        with self._lock:
            self._heartbeat_deadline = None
            self._x_state = None
            if self._active_job is not None:
                self._finish_active_locked(DiscoveryReason.CANCELLED, remember_cancellation=True)
            self._extension_stop_pending = False
            self._publish_snapshot_locked()
        self._drain_signals()

    def issue_pairing_code(self) -> PairingOffer:
        return self._pairing.issue()

    def pair(self, code: str) -> str:
        with self._lock:
            token = self._pairing.exchange(code)
            self._heartbeat_deadline = None
            self._x_state = None
            self._extension_stop_pending = False
            self._publish_snapshot_locked()
        self._drain_signals()
        return token

    def authenticate(self, token: str) -> None:
        with self._lock:
            self._authenticate_locked(token)

    def _authenticate_locked(self, token: str) -> None:
        try:
            authenticated = self._pairing.authenticate(token)
        except PairingError as error:
            raise BridgeAuthenticationError("authentication is unavailable") from error
        if not authenticated:
            raise BridgeAuthenticationError("invalid bearer token")

    def snapshot(self) -> BridgeSnapshot:
        with self._lock:
            snapshot = self._snapshot_locked()
            self._publish_snapshot_locked(snapshot)
        self._drain_signals()
        return snapshot

    def heartbeat(self, token: str, x_state: ExtensionXState) -> None:
        if not isinstance(x_state, ExtensionXState):
            raise BridgeError("invalid extension X state")
        with self._lock:
            self._authenticate_locked(token)
            self._heartbeat_deadline = self._clock() + _HEARTBEAT_LEASE_S
            self._x_state = x_state
            self._publish_snapshot_locked()
            if not self._expire_active_locked():
                unsafe = self._unsafe_job_result_locked()
                if self._active_job is not None and unsafe is not None:
                    self._finish_active_locked(*unsafe)
        self._drain_signals()

    def enqueue(
        self,
        target: CollectionTarget,
        maximum: int = 30,
        timeout_s: float = 45.0,
    ) -> OperaJob:
        if not isinstance(target, CollectionTarget):
            raise TypeError("target must be a CollectionTarget")
        upper = 30 if target.kind is CollectionTargetKind.SOURCE else 50
        if type(maximum) is not int or not 1 <= maximum <= upper:
            raise ValueError(f"maximum must be between 1 and {upper}")
        valid_timeout_s = self._validate_timeout_s(timeout_s)
        with self._lock:
            self._expire_active_locked()
        self._drain_signals()
        with self._lock:
            snapshot = self._snapshot_locked()
            self._publish_snapshot_locked(snapshot)
            if not snapshot.connected or snapshot.x_state is not ExtensionXState.SIGNED_IN:
                error: BridgeError | None = BridgeError(
                    "Opera GX must be connected and signed in before collection"
                )
                job = None
            elif self._active_job is not None:
                error = BridgeBusyError("an Opera collection job is already active")
                job = None
            else:
                error = None
                job = OperaCollectionJob(
                    id=secrets.token_urlsafe(18),
                    target_kind=target.kind,
                    handle=target.handle,
                    profile_url=target.profile_url,
                    maximum=maximum,
                    deadline_at=self._clock() + valid_timeout_s,
                )
                self._active_job = job
                self._active_job_delivered = False
                self._seen_post_ids = set()
        self._drain_signals()
        if error is not None:
            raise error
        assert job is not None
        return job

    def enqueue_photo_resolution(
        self,
        *,
        post_id: int,
        post_url: str,
        timeout_s: float = 45.0,
    ) -> OperaPhotoJob:
        if type(post_id) is not int or post_id <= 0 or len(str(post_id)) > 128:
            raise ValueError("post_id must be a positive integer")
        if type(post_url) is not str:
            raise TypeError("post_url must be a canonical X post URL")
        try:
            canonical = canonicalize_post_url(post_url)
        except InvalidPostUrl as url_error:
            raise ValueError("post_url must be a canonical X post URL") from url_error
        if canonical.url != post_url or canonical.post_id != str(post_id):
            raise ValueError("post_url must exactly match post_id")
        valid_timeout_s = self._validate_timeout_s(timeout_s)
        with self._lock:
            self._expire_active_locked()
        self._drain_signals()
        with self._lock:
            snapshot = self._snapshot_locked()
            self._publish_snapshot_locked(snapshot)
            if not snapshot.connected or snapshot.x_state is not ExtensionXState.SIGNED_IN:
                error: BridgeError | None = BridgeError(
                    "Opera GX must be connected and signed in before photo resolution"
                )
                job = None
            elif self._active_job is not None:
                error = BridgeBusyError("an Opera job is already active")
                job = None
            else:
                error = None
                job = OperaPhotoJob(
                    id=secrets.token_urlsafe(18),
                    post_id=str(post_id),
                    post_url=post_url,
                    deadline_at=self._clock() + valid_timeout_s,
                )
                self._active_job = job
                self._active_job_delivered = False
                self._seen_post_ids = set()
        self._drain_signals()
        if error is not None:
            raise error
        assert job is not None
        return job

    def poll_job(self, token: str) -> OperaJobUnion | None:
        with self._lock:
            job = self._poll_job_locked(token)
        self._drain_signals()
        return job

    def _poll_job_payload(self, token: str) -> dict[str, object] | None:
        serialized, _cancelled = self._poll_job_response(token)
        return serialized

    def _poll_job_response(self, token: str) -> tuple[dict[str, object] | None, bool]:
        with self._lock:
            self._authenticate_locked(token)
            self._publish_snapshot_locked()
            self._expire_active_locked()
            unsafe = self._unsafe_job_result_locked()
            if self._active_job is not None and unsafe is not None:
                self._finish_active_locked(*unsafe)

            serialized: dict[str, object] | None = None
            cancelled = self._extension_stop_pending
            if cancelled:
                self._extension_stop_pending = False
                job = None
            elif self._active_job_delivered:
                job = None
            else:
                job = self._active_job
                self._active_job_delivered = job is not None

            if job is not None:
                timeout_ms = self._client_timeout_ms(job)
                if self._active_job is job and self._clock() < job.deadline_at:
                    if isinstance(job, OperaPhotoJob):
                        serialized = {
                            "id": job.id,
                            "kind": "resolve_post_photos",
                            "post_id": job.post_id,
                            "post_url": job.post_url,
                            "timeout_ms": timeout_ms,
                        }
                    else:
                        serialized = {
                            "id": job.id,
                            "target_kind": job.target_kind.value,
                            "handle": job.handle,
                            "profile_url": job.profile_url,
                            "maximum": job.maximum,
                            "timeout_ms": timeout_ms,
                        }
                elif self._active_job is job:
                    self._finish_active_locked(DiscoveryReason.TIMEOUT)
        self._drain_signals()
        return serialized, cancelled

    def report_progress(
        self,
        token: str,
        job_id: str,
        observations: Iterable[CandidateObservation],
    ) -> bool:
        batch = self._bounded_batch(observations)
        with self._lock:
            self._authenticate_locked(token)
            expired = self._expire_active_locked()
            cancelled = job_id in self._cancelled_job_ids
            job = self._active_job
            if cancelled:
                pass
            elif expired or job is None:
                pass
            elif job.id != job_id:
                raise BridgeError("job is not active")
            elif isinstance(job, OperaPhotoJob):
                raise BridgeError("job does not accept collection progress")
            else:
                remaining = min(job.maximum, MAXIMUM_CANDIDATES) - len(self._seen_post_ids)
                for observation in batch:
                    if observation.post_id in self._seen_post_ids:
                        continue
                    if remaining <= 0:
                        break
                    self._seen_post_ids.add(observation.post_id)
                    remaining -= 1
                    self._queue_signal_locked(_CANDIDATE_EVENT, job_id, observation)

        self._drain_signals()
        if cancelled:
            return True
        if expired or job is None:
            raise BridgeError("job is no longer active")
        with self._lock:
            return job_id in self._cancelled_job_ids

    def complete_job(
        self,
        token: str,
        result: OperaJobResult | OperaPhotoResolutionResult,
    ) -> None:
        if not isinstance(result, OperaJobResult | OperaPhotoResolutionResult):
            raise BridgeError("invalid job result")
        with self._lock:
            self._authenticate_locked(token)
            expired = self._expire_active_locked()
            if not expired and (self._active_job is None or self._active_job.id != result.job_id):
                raise BridgeError("job is not active")
            if not expired:
                job = self._active_job
                if isinstance(job, OperaPhotoJob) != isinstance(result, OperaPhotoResolutionResult):
                    raise BridgeError("completion does not match active job")
                photos = result.photos if isinstance(result, OperaPhotoResolutionResult) else ()
                self._finish_active_locked(
                    result.reason,
                    result.diagnostic,
                    photos=photos,
                    notify_extension=False,
                )
        self._drain_signals()
        if expired:
            raise BridgeError("job is no longer active")

    def _complete_job_payload(self, token: str, job_id: str, payload: object) -> None:
        with self._lock:
            self._authenticate_locked(token)
            expired = self._expire_active_locked()
            job = self._active_job
            if not expired and (job is None or job.id != job_id):
                raise BridgeError("job is not active")
            if not expired:
                assert job is not None
                result = parse_job_completion(job, payload)
                photos = result.photos if isinstance(result, OperaPhotoResolutionResult) else ()
                self._finish_active_locked(
                    result.reason,
                    result.diagnostic,
                    photos=photos,
                    notify_extension=False,
                )
        self._drain_signals()
        if expired:
            raise BridgeError("job is no longer active")

    def cancel(self, job_id: str) -> None:
        with self._lock:
            self._expire_active_locked()
            if job_id in self._cancelled_job_ids or self._active_job is None:
                pass
            elif self._active_job.id != job_id:
                raise BridgeError("job is not active")
            else:
                self._finish_active_locked(DiscoveryReason.CANCELLED, remember_cancellation=True)
        self._drain_signals()

    def revoke(self) -> None:
        with self._lock:
            self._pairing.revoke()
            self._heartbeat_deadline = None
            self._x_state = None
            if self._active_job is not None:
                self._finish_active_locked(DiscoveryReason.CANCELLED, remember_cancellation=True)
            self._extension_stop_pending = False
            self._publish_snapshot_locked()
        self._drain_signals()

    @staticmethod
    def _bounded_batch(
        observations: Iterable[CandidateObservation],
    ) -> tuple[CandidateObservation, ...]:
        try:
            batch = tuple(islice(iter(observations), MAXIMUM_CANDIDATES + 1))
        except Exception as error:
            raise BridgeError("invalid candidate observations") from error
        if len(batch) > MAXIMUM_CANDIDATES:
            raise BridgeError("too many candidate observations")
        if any(not isinstance(observation, CandidateObservation) for observation in batch):
            raise BridgeError("invalid candidate observation")
        return batch

    @staticmethod
    def _validate_timeout_s(timeout_s: float) -> float:
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, int | float)
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be a positive finite number")
        return float(timeout_s)

    def _snapshot_locked(self) -> BridgeSnapshot:
        connected = (
            self._heartbeat_deadline is not None and self._clock() < self._heartbeat_deadline
        )
        return BridgeSnapshot(connected, self._x_state if connected else None)

    def _client_timeout_ms(self, job: OperaJobUnion) -> int:
        remaining_ms = math.ceil((job.deadline_at - self._clock()) * 1_000)
        return max(1, min(_MAXIMUM_CLIENT_TIMEOUT_MS, remaining_ms))

    def _poll_job_locked(self, token: str) -> OperaJobUnion | None:
        self._authenticate_locked(token)
        self._publish_snapshot_locked()
        if self._expire_active_locked():
            return None
        unsafe = self._unsafe_job_result_locked()
        if self._active_job is not None and unsafe is not None:
            self._finish_active_locked(*unsafe)
            return None
        if self._active_job_delivered:
            return None
        job = self._active_job
        self._active_job_delivered = job is not None
        return job

    def _publish_snapshot_locked(self, current: BridgeSnapshot | None = None) -> None:
        if current is None:
            current = self._snapshot_locked()
        if current != self._published_snapshot:
            self._published_snapshot = current
            self._queue_signal_locked(_SNAPSHOT_EVENT, current)

    def _unsafe_job_result_locked(self) -> tuple[DiscoveryReason, str] | None:
        snapshot = self._snapshot_locked()
        if not snapshot.connected:
            return DiscoveryReason.ERROR, "Opera GX extension is disconnected"
        if snapshot.x_state is ExtensionXState.SIGNED_IN:
            return None
        if snapshot.x_state is ExtensionXState.SIGNED_OUT:
            return (
                DiscoveryReason.LOGIN_WALL,
                "X is signed out in Opera GX; sign in and reconnect",
            )
        if snapshot.x_state is ExtensionXState.CHALLENGE:
            return DiscoveryReason.ERROR, "X presented an account challenge in Opera GX"
        return DiscoveryReason.ERROR, "X rate-limited the Opera GX session"

    def _expire_active_locked(self) -> bool:
        job = self._active_job
        if job is None or self._clock() < job.deadline_at:
            return False
        self._finish_active_locked(DiscoveryReason.TIMEOUT)
        return True

    def _finish_active_locked(
        self,
        reason: DiscoveryReason,
        diagnostic: str | None = None,
        *,
        remember_cancellation: bool = False,
        photos: tuple[PhotoCandidate, ...] = (),
        notify_extension: bool = True,
    ) -> None:
        job = self._active_job
        if job is None:
            return
        if notify_extension and self._active_job_delivered:
            self._extension_stop_pending = True
        self._active_job = None
        self._active_job_delivered = False
        self._seen_post_ids = set()
        if remember_cancellation:
            self._cancelled_job_ids.append(job.id)
        if isinstance(job, OperaPhotoJob):
            self._queue_signal_locked(
                _PHOTO_JOB_EVENT,
                OperaPhotoResolutionResult(job.id, reason, photos, diagnostic),
            )
        else:
            self._queue_signal_locked(_JOB_EVENT, OperaJobResult(job.id, reason, diagnostic))

    @Slot()
    def _maintain_leases(self) -> None:
        with self._lock:
            self._publish_snapshot_locked()
            if not self._expire_active_locked():
                unsafe = self._unsafe_job_result_locked()
                if self._active_job is not None and unsafe is not None:
                    self._finish_active_locked(*unsafe)
        self._drain_signals()

    def _start_maintenance_timer(self) -> None:
        if QCoreApplication.instance() is None:
            return
        if self._maintenance_timer is None:
            self._maintenance_timer = QTimer(self)
            self._maintenance_timer.setInterval(_MAINTENANCE_INTERVAL_MS)
            self._maintenance_timer.timeout.connect(self._maintain_leases)
        if not self._maintenance_timer.isActive():
            self._maintenance_timer.start()

    def _queue_signal_locked(self, kind: str, *args: object) -> None:
        self._pending_signals.append((kind, args))

    @Slot()
    def _drain_signals(self) -> None:
        with self._lock:
            if self._signals_draining or not self._pending_signals:
                return
            self._signals_draining = True

        while True:
            with self._lock:
                if not self._pending_signals:
                    self._signals_draining = False
                    return
                kind, args = self._pending_signals.popleft()
            try:
                if kind == _SNAPSHOT_EVENT:
                    self.snapshot_changed.emit(*args)
                elif kind == _CANDIDATE_EVENT:
                    self.candidate_received.emit(*args)
                elif kind == _JOB_EVENT:
                    self.job_finished.emit(*args)
                elif kind == _PHOTO_JOB_EVENT:
                    self.photo_job_finished.emit(*args)
                else:
                    raise AssertionError("unknown bridge signal event")
            except BaseException:
                with self._lock:
                    self._signals_draining = False
                raise


def _handler_for(bridge: OperaBridge) -> type[BaseHTTPRequestHandler]:
    class OperaBridgeHandler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(_CONNECTION_TIMEOUT_S)

        def __getattr__(self, name: str) -> object:
            if name.startswith("do_"):
                return self._write_method_or_path_error
            raise AttributeError(name)

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except _RequestFailure as error:
                self.close_connection = True
                self._error(error.status, error.message)

        def do_GET(self) -> None:
            if self._request_path() == "/v1/health":
                self._write_json(
                    200,
                    {"protocol_version": PROTOCOL_VERSION, "status": "ok"},
                )
                return
            self._write_method_or_path_error()

        def do_POST(self) -> None:
            path = self._request_path()
            if path == "/v1/pair":
                self._pair()
                return
            if path == "/v1/heartbeat":
                self._authenticated(self._heartbeat)
                return
            if path == "/v1/jobs/poll":
                self._authenticated(self._poll)
                return
            match = _JOB_PATH.fullmatch(path)
            if match is None:
                self._write_method_or_path_error()
                return
            encoded_job_id, operation = match.groups()
            self._authenticated(lambda token: self._job_request(token, encoded_job_id, operation))

        def do_OPTIONS(self) -> None:
            allowed = self._allowed_method()
            if allowed is None:
                self._error(404, "endpoint not found")
                return
            origin = self.headers.get("Origin")
            requested_method = self.headers.get("Access-Control-Request-Method")
            if (
                origin is None
                or _EXTENSION_ORIGIN.fullmatch(origin) is None
                or requested_method != allowed
            ):
                self._error(403, "origin not permitted")
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", allowed)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_DELETE(self) -> None:
            self._write_method_or_path_error()

        def do_HEAD(self) -> None:
            self._write_method_or_path_error()

        def do_PATCH(self) -> None:
            self._write_method_or_path_error()

        def do_PUT(self) -> None:
            self._write_method_or_path_error()

        def do_TRACE(self) -> None:
            self._write_method_or_path_error()

        def do_CONNECT(self) -> None:
            self._write_method_or_path_error()

        def log_message(self, _format: str, *args: object) -> None:
            return

        def _pair(self) -> None:
            try:
                payload = self._read_json()
                if type(payload) is not dict or set(payload) != {"code"}:
                    raise ProtocolError("pairing request fields are invalid")
                code = cast(dict[str, object], payload)["code"]
                if type(code) is not str:
                    raise ProtocolError("pairing code is invalid")
                token = bridge.pair(code)
            except ProtocolError:
                self._error(400, "invalid request")
                return
            except PairingError:
                self._error(400, "pairing failed")
                return
            except _RequestFailure as error:
                self._error(error.status, error.message)
                return
            except Exception:
                self._error(500, "server failure")
                return
            self._write_json(
                200,
                {"protocol_version": PROTOCOL_VERSION, "token": token},
            )

        def _authenticated(self, operation: Callable[[str], None]) -> None:
            authorization = self.headers.get("Authorization")
            if authorization is None or not authorization.startswith("Bearer "):
                self._error(401, "authentication failed")
                return
            token = authorization.removeprefix("Bearer ")
            if not token or " " in token:
                self._error(401, "authentication failed")
                return
            try:
                bridge.authenticate(token)
                operation(token)
            except BridgeAuthenticationError:
                self._error(401, "authentication failed")
            except _RequestFailure as error:
                self._error(error.status, error.message)
            except ProtocolError:
                self._error(400, "invalid request")
            except BridgeError:
                self._error(409, "bridge state conflict")
            except Exception:
                self._error(500, "server failure")

        def _heartbeat(self, token: str) -> None:
            x_state = parse_heartbeat(self._read_json())
            bridge.heartbeat(token, x_state)
            self._write_json(
                200,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "connected": True,
                    "x_state": x_state.value,
                },
            )

        def _poll(self, token: str) -> None:
            payload = self._read_json()
            if type(payload) is not dict or payload:
                raise ProtocolError("poll request fields are invalid")
            serialized, cancelled = bridge._poll_job_response(token)
            self._write_json(
                200,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "job": serialized,
                    "cancelled": cancelled,
                },
            )

        def _progress(self, token: str, job_id: str) -> None:
            observations = parse_observations(self._read_json())
            cancelled = bridge.report_progress(token, job_id, observations)
            self._write_json(
                200,
                {"protocol_version": PROTOCOL_VERSION, "cancelled": cancelled},
            )

        def _job_request(self, token: str, encoded_job_id: str, operation: str) -> None:
            try:
                job_id = unquote(encoded_job_id, errors="strict")
            except UnicodeError as error:
                raise ProtocolError("job_id is invalid") from error
            if _JOB_ID.fullmatch(job_id) is None:
                raise ProtocolError("job_id is invalid")
            if operation == "progress":
                self._progress(token, job_id)
            else:
                self._complete(token, job_id)

        def _complete(self, token: str, job_id: str) -> None:
            bridge._complete_job_payload(token, job_id, self._read_json())
            self._write_json(
                200,
                {"protocol_version": PROTOCOL_VERSION, "accepted": True},
            )

        def _read_json(self) -> object:
            content_type = self.headers.get("Content-Type")
            if content_type is None or content_type.partition(";")[0].strip().lower() != (
                "application/json"
            ):
                raise _RequestFailure(415, "JSON content type required")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise _RequestFailure(400, "invalid content length")
            raw_length = lengths[0]
            if not raw_length.isascii() or not raw_length.isdigit():
                raise _RequestFailure(400, "invalid content length")
            normalized_length = raw_length.lstrip("0") or "0"
            if len(normalized_length) > 5 or (
                len(normalized_length) == 5 and normalized_length > "65536"
            ):
                self.close_connection = True
                raise _RequestFailure(413, "request body too large")
            length = int(normalized_length)
            try:
                body = self.rfile.read(length)
            except (OSError, TimeoutError) as error:
                raise _RequestFailure(400, "invalid request body") from error
            if len(body) != length:
                raise _RequestFailure(400, "invalid request body")
            try:
                text = body.decode("utf-8", errors="strict")
                return cast(
                    object,
                    json.loads(
                        text,
                        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
                    ),
                )
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                RecursionError,
                ValueError,
            ) as error:
                raise _RequestFailure(400, "invalid JSON") from error

        def _request_path(self) -> str:
            try:
                parsed = urlsplit(self.path)
            except ValueError as error:
                raise _RequestFailure(400, "invalid request target") from error
            if parsed.query or parsed.fragment:
                return ""
            return parsed.path

        def _allowed_method(self) -> str | None:
            path = self._request_path()
            if path == "/v1/health":
                return "GET"
            if path in {"/v1/pair", "/v1/heartbeat", "/v1/jobs/poll"}:
                return "POST"
            if _JOB_PATH.fullmatch(path) is not None:
                return "POST"
            return None

        def _write_method_or_path_error(self) -> None:
            allowed = self._allowed_method()
            if allowed is None:
                self._error(404, "endpoint not found")
            else:
                self._error(405, "method not allowed", {"Allow": f"{allowed}, OPTIONS"})

        def _error(
            self,
            status: int,
            message: str,
            headers: dict[str, str] | None = None,
        ) -> None:
            self._write_json(
                status,
                {"protocol_version": PROTOCOL_VERSION, "error": message},
                headers,
            )

        def _write_json(
            self,
            status: int,
            payload: dict[str, object],
            headers: dict[str, str] | None = None,
        ) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                origin = self.headers.get("Origin")
                if origin is not None and _EXTENSION_ORIGIN.fullmatch(origin) is not None:
                    self.send_header("Access-Control-Allow-Origin", origin)
                if headers is not None:
                    for name, value in headers.items():
                        self.send_header(name, value)
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return OperaBridgeHandler


@dataclass(frozen=True)
class _RequestFailure(Exception):
    status: int
    message: str
