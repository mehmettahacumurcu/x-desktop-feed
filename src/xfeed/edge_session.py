import json
from collections.abc import Callable

from PySide6.QtCore import QObject, QThreadPool, Signal

from xfeed.edge_browser import EdgeBrowserPort
from xfeed.ui.workers import Worker
from xfeed.x_session import (
    SessionSnapshot,
    SessionSnapshotError,
    SessionState,
    build_session_check_script,
    parse_session_snapshot,
)


BackgroundExecutor = Callable[
    [Callable[[], object], Callable[[object], None], Callable[[str], None]], None
]


class EdgeXSession(QObject):
    state_changed = Signal(object)
    verification_finished = Signal(object)

    def __init__(
        self,
        browser: EdgeBrowserPort,
        parent: QObject | None = None,
        *,
        executor: BackgroundExecutor | None = None,
    ) -> None:
        super().__init__(parent)
        self._browser = browser
        self._executor = executor or self._execute_in_background
        self._state = SessionState.SIGNED_OUT
        self._generation = 0
        self._verification_generation: int | None = None
        self._active_workers: set[Worker] = set()

    @property
    def state(self) -> SessionState:
        return self._state

    def verify(self) -> None:
        if self._verification_generation is not None:
            return

        generation = self._next_generation()
        self._verification_generation = generation
        self._set_state(SessionState.CHECKING)

        def operation() -> object:
            self._browser.open("https://x.com/home")
            payload = self._browser.evaluate(build_session_check_script())
            try:
                decoded = json.loads(payload) if isinstance(payload, str) else payload
                return parse_session_snapshot(decoded)
            except (json.JSONDecodeError, SessionSnapshotError, TypeError):
                return None

        def succeeded(result: object) -> None:
            if result is None:
                self._finish_verification(generation, SessionState.SIGNED_OUT)
                return
            if not isinstance(result, SessionSnapshot):
                self._finish_verification(generation, SessionState.UNAVAILABLE)
                return
            state = SessionState.SIGNED_IN if result.authenticated else SessionState.SIGNED_OUT
            self._finish_verification(generation, state)

        def failed(message: str) -> None:
            del message
            self._finish_verification(generation, SessionState.UNAVAILABLE)

        self._submit(operation, succeeded, failed)

    def begin_login(self) -> None:
        generation = self._next_generation()
        self._set_state(SessionState.SIGNING_IN)

        def operation() -> object:
            return self._browser.open("https://x.com/i/flow/login")

        def succeeded(result: object) -> None:
            del result

        def failed(message: str) -> None:
            del message
            if generation == self._generation:
                self._set_state(SessionState.UNAVAILABLE)

        self._submit(operation, succeeded, failed)

    def mark_signed_in(self) -> None:
        self._next_generation()
        self._set_state(SessionState.SIGNED_IN)

    def mark_signed_out(self) -> None:
        self._next_generation()
        self._set_state(SessionState.SIGNED_OUT)

    def clear(self) -> None:
        generation = self._next_generation()

        def operation() -> object:
            self._browser.clear_x_site_data()
            return None

        def succeeded(result: object) -> None:
            del result
            if generation == self._generation:
                self._set_state(SessionState.SIGNED_OUT)

        def failed(message: str) -> None:
            del message
            if generation == self._generation:
                self._set_state(SessionState.UNAVAILABLE)

        self._submit(operation, succeeded, failed)

    def _next_generation(self) -> int:
        self._generation += 1
        self._verification_generation = None
        return self._generation

    def _finish_verification(self, generation: int, state: SessionState) -> None:
        if generation != self._verification_generation:
            return
        self._verification_generation = None
        self._set_state(state)
        self.verification_finished.emit(state)

    def _set_state(self, state: SessionState) -> None:
        if state is self._state:
            return
        self._state = state
        self.state_changed.emit(state)

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
