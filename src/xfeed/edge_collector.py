import json
import threading
import time
from collections.abc import Callable
from dataclasses import replace

from PySide6.QtCore import QObject, QThreadPool, Signal
from PySide6.QtWidgets import QLabel

from xfeed.collection import (
    CandidateObservation,
    DiscoveryReason,
    DiscoveryResult,
    qualifying_candidates,
)
from xfeed.edge_browser import RATE_LIMIT_SCRIPT, EdgeBrowserPort
from xfeed.edge_session import BackgroundExecutor
from xfeed.i18n import tr
from xfeed.public_collector import (
    PageSnapshot,
    SCROLL_SCRIPT,
    SnapshotParseError,
    build_extraction_script,
    is_allowed_profile_url,
    parse_page_snapshot,
)
from xfeed.sources import SourceProfile, canonicalize_profile
from xfeed.ui.workers import Worker


class EdgeProfileCollector(QObject):
    progress = Signal(object)
    finished = Signal(object)

    def __init__(
        self,
        browser: EdgeBrowserPort,
        *,
        executor: BackgroundExecutor | None = None,
        clock: Callable[[], float] = time.monotonic,
        maximum: int = 30,
        settle_ms: int = 750,
        maximum_no_progress: int = 3,
        timeout_ms: int = 30_000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if not 1 <= maximum <= 30:
            raise ValueError("maximum must be between 1 and 30")
        if settle_ms < 0:
            raise ValueError("settle_ms must not be negative")
        if maximum_no_progress < 1:
            raise ValueError("maximum_no_progress must be positive")
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")

        self.widget = QLabel(tr("Collection is running in the dedicated Microsoft Edge window."))
        self.widget.setWordWrap(True)
        self._browser = browser
        self._executor = executor or self._execute_in_background
        self._clock = clock
        self._maximum = maximum
        self._settle_seconds = settle_ms / 1000
        self._maximum_no_progress = maximum_no_progress
        self._timeout_seconds = timeout_ms / 1000
        self._generation = 0
        self._active_generation: int | None = None
        self._cancel_event: threading.Event | None = None
        self._state_lock = threading.RLock()
        self._active_workers: set[Worker] = set()

    def start(self, profile: SourceProfile) -> None:
        with self._state_lock:
            previous_event = self._cancel_event
            previous_active = self._active_generation is not None
            if previous_event is not None:
                previous_event.set()
            self._generation += 1
            generation = self._generation
            cancel_event = threading.Event()
            self._active_generation = generation
            self._cancel_event = cancel_event

        if previous_active:
            self._stop_browser()

        try:
            canonical_profile = canonicalize_profile(profile.handle)
        except (AttributeError, TypeError, ValueError) as error:
            self._finish(
                generation,
                cancel_event,
                DiscoveryResult((), DiscoveryReason.ERROR, str(error)),
            )
            return

        deadline = self._clock() + self._timeout_seconds

        def operation() -> object:
            return self._discover(canonical_profile, generation, cancel_event, deadline)

        def succeeded(result: object) -> None:
            if not isinstance(result, DiscoveryResult):
                result = DiscoveryResult(
                    (), DiscoveryReason.ERROR, "Edge collection returned an invalid result"
                )
            self._finish(generation, cancel_event, result)

        def failed(message: str) -> None:
            self._finish(
                generation,
                cancel_event,
                DiscoveryResult((), DiscoveryReason.ERROR, f"Edge collection failed: {message}"),
            )

        self._submit(operation, succeeded, failed)

    def cancel(self) -> None:
        with self._state_lock:
            cancel_event = self._cancel_event
            if self._active_generation is None or cancel_event is None or cancel_event.is_set():
                return
            cancel_event.set()
        self._stop_browser()

    def _discover(
        self,
        profile: SourceProfile,
        generation: int,
        cancel_event: threading.Event,
        deadline: float,
    ) -> DiscoveryResult:
        candidates: list[CandidateObservation] = []
        seen_post_ids: set[str] = set()
        no_progress = 0
        empty_end_passes = 0

        if cancel_event.is_set():
            return self._cancelled(candidates)
        remaining = self._remaining(deadline)
        if remaining <= 0:
            return self._timed_out(candidates)
        try:
            self._browser.open(
                profile.profile_url,
                timeout_s=min(15.0, remaining),
            )
        except Exception as error:
            return self._command_failed(
                candidates,
                cancel_event,
                deadline,
                f"profile navigation failed: {error}",
            )

        settled = self._wait_to_settle(cancel_event, deadline)
        if settled is not None:
            return DiscoveryResult(tuple(candidates), settled[0], settled[1])

        while not cancel_event.is_set():
            remaining = self._remaining(deadline)
            if remaining <= 0:
                return self._timed_out(candidates)

            try:
                limited = self._browser.evaluate(RATE_LIMIT_SCRIPT, timeout_s=remaining)
            except Exception as error:
                return self._command_failed(
                    candidates,
                    cancel_event,
                    deadline,
                    f"rate-limit check failed: {error}",
                )
            if cancel_event.is_set():
                return self._cancelled(candidates)
            if not isinstance(limited, bool):
                return DiscoveryResult(
                    tuple(candidates),
                    DiscoveryReason.ERROR,
                    "Edge returned an invalid rate-limit check",
                )
            if limited:
                return DiscoveryResult(
                    tuple(candidates),
                    DiscoveryReason.ERROR,
                    "X temporarily limited this browser session",
                )

            remaining = self._remaining(deadline)
            if remaining <= 0:
                return self._timed_out(candidates)
            try:
                payload = self._browser.evaluate(
                    build_extraction_script(profile.handle), timeout_s=remaining
                )
                if cancel_event.is_set():
                    return self._cancelled(candidates)
                decoded = json.loads(payload) if isinstance(payload, str) else payload
                snapshot = parse_page_snapshot(decoded)
            except (json.JSONDecodeError, SnapshotParseError, TypeError) as error:
                return DiscoveryResult(
                    tuple(candidates),
                    DiscoveryReason.ERROR,
                    f"malformed page snapshot: {error}",
                )
            except Exception as error:
                return self._command_failed(
                    candidates,
                    cancel_event,
                    deadline,
                    f"page extraction failed: {error}",
                )

            invalid = self._validate_snapshot(snapshot, profile)
            if invalid is not None:
                return DiscoveryResult(tuple(candidates), invalid[0], invalid[1])

            added = 0
            normalized = qualifying_candidates(snapshot.observations, profile.handle, maximum=30)
            for candidate in normalized:
                if candidate.post_id in seen_post_ids:
                    continue
                candidate = replace(candidate, discovery_order=len(candidates))
                seen_post_ids.add(candidate.post_id)
                candidates.append(candidate)
                added += 1
                if not self._emit_progress(generation, cancel_event, candidate):
                    return self._cancelled(candidates)
                if len(candidates) >= self._maximum:
                    break

            no_progress = 0 if added else no_progress + 1
            if added or not snapshot.end_reached:
                empty_end_passes = 0
            else:
                empty_end_passes += 1

            if len(candidates) >= self._maximum:
                return DiscoveryResult(tuple(candidates), DiscoveryReason.LIMIT)
            if empty_end_passes >= max(2, self._maximum_no_progress):
                return DiscoveryResult(tuple(candidates), DiscoveryReason.EXHAUSTED)
            if not snapshot.end_reached and no_progress >= self._maximum_no_progress:
                return DiscoveryResult(
                    tuple(candidates),
                    DiscoveryReason.NO_PROGRESS,
                    "no new qualifying posts were rendered",
                )
            if cancel_event.is_set():
                return self._cancelled(candidates)

            remaining = self._remaining(deadline)
            if remaining <= 0:
                return self._timed_out(candidates)
            try:
                self._browser.evaluate(SCROLL_SCRIPT, timeout_s=remaining)
            except Exception as error:
                return self._command_failed(
                    candidates,
                    cancel_event,
                    deadline,
                    f"page scroll failed: {error}",
                )

            settled = self._wait_to_settle(cancel_event, deadline)
            if settled is not None:
                return DiscoveryResult(tuple(candidates), settled[0], settled[1])

        return self._cancelled(candidates)

    def _wait_to_settle(
        self, cancel_event: threading.Event, deadline: float
    ) -> tuple[DiscoveryReason, str] | None:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            return DiscoveryReason.TIMEOUT, "collection timed out"
        wait_seconds = min(self._settle_seconds, remaining)
        if cancel_event.wait(wait_seconds):
            return DiscoveryReason.CANCELLED, "collection cancelled"
        if self._remaining(deadline) <= 0:
            return DiscoveryReason.TIMEOUT, "collection timed out"
        return None

    @staticmethod
    def _validate_snapshot(
        snapshot: PageSnapshot, profile: SourceProfile
    ) -> tuple[DiscoveryReason, str] | None:
        if snapshot.login_wall:
            return DiscoveryReason.LOGIN_WALL, "X displayed a login wall"
        if not snapshot.page_supported:
            return DiscoveryReason.ERROR, "X profile page is not supported"
        if not is_allowed_profile_url(snapshot.page_url, profile.profile_url):
            return DiscoveryReason.ERROR, "page left the selected X profile"
        return None

    def _emit_progress(
        self,
        generation: int,
        cancel_event: threading.Event,
        candidate: CandidateObservation,
    ) -> bool:
        with self._state_lock:
            if (
                generation != self._active_generation
                or cancel_event is not self._cancel_event
                or cancel_event.is_set()
            ):
                return False
            self.progress.emit(candidate)
            return (
                generation == self._active_generation
                and cancel_event is self._cancel_event
                and not cancel_event.is_set()
            )

    def _finish(
        self,
        generation: int,
        cancel_event: threading.Event,
        result: DiscoveryResult,
    ) -> None:
        with self._state_lock:
            if generation != self._active_generation or cancel_event is not self._cancel_event:
                return
            if cancel_event.is_set():
                result = DiscoveryResult(
                    result.candidates,
                    DiscoveryReason.CANCELLED,
                    "collection cancelled",
                )
            self._active_generation = None
            self._cancel_event = None
        self.finished.emit(result)

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - self._clock())

    def _command_failed(
        self,
        candidates: list[CandidateObservation],
        cancel_event: threading.Event,
        deadline: float,
        diagnostic: str,
    ) -> DiscoveryResult:
        if self._remaining(deadline) <= 0:
            return self._timed_out(candidates)
        if cancel_event.is_set():
            return self._cancelled(candidates)
        return DiscoveryResult(tuple(candidates), DiscoveryReason.ERROR, diagnostic)

    @staticmethod
    def _cancelled(candidates: list[CandidateObservation]) -> DiscoveryResult:
        return DiscoveryResult(tuple(candidates), DiscoveryReason.CANCELLED, "collection cancelled")

    @staticmethod
    def _timed_out(candidates: list[CandidateObservation]) -> DiscoveryResult:
        return DiscoveryResult(tuple(candidates), DiscoveryReason.TIMEOUT, "collection timed out")

    def _stop_browser(self) -> None:
        try:
            self._browser.stop()
        except Exception:
            pass

    def _submit(
        self,
        operation: Callable[[], object],
        succeeded: Callable[[object], None],
        failed: Callable[[str], None],
    ) -> None:
        try:
            self._executor(operation, succeeded, failed)
        except Exception as error:
            failed(str(error))

    def _execute_in_background(
        self,
        operation: Callable[[], object],
        succeeded: Callable[[object], None],
        failed: Callable[[str], None],
    ) -> None:
        worker = Worker(operation)
        self._active_workers.add(worker)

        def finish_succeeded(result: object) -> None:
            self._active_workers.discard(worker)
            succeeded(result)

        def finish_failed(message: str) -> None:
            self._active_workers.discard(worker)
            failed(message)

        worker.result.connect(finish_succeeded)
        worker.failed.connect(finish_failed)
        QThreadPool.globalInstance().start(worker)
