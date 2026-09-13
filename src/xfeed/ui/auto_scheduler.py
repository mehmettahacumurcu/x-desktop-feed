import math
import time
from collections.abc import Callable
from typing import Protocol, cast

from PySide6.QtCore import QObject, QTimer, Signal, SignalInstance
from PySide6.QtWidgets import QWidget

from xfeed.domain import CollectionTargetKind
from xfeed.i18n import tr
from xfeed.ui.settings import (
    AUTO_INTERVAL_MINUTES,
    MY_FEED_COUNTS,
    UiSettingsStore,
)
from xfeed.x_session import SessionState


class _Timer(Protocol):
    timeout: SignalInstance

    def setSingleShot(self, single_shot: bool) -> None: ...

    def start(self, milliseconds: int) -> None: ...

    def stop(self) -> None: ...


class _Coordinator(Protocol):
    session_blocked: SignalInstance
    session_state_changed: SignalInstance

    def collect_feed(
        self,
        kind: CollectionTargetKind,
        maximum: int,
        *,
        interactive: bool,
        parent: QWidget | None,
    ) -> bool: ...


class AutoCollectionScheduler(QObject):
    countdown_changed = Signal(object)
    status_changed = Signal(str)

    def __init__(
        self,
        coordinator: _Coordinator,
        settings: UiSettingsStore,
        parent: QObject | None = None,
        *,
        target_kind: CollectionTargetKind = CollectionTargetKind.FOR_YOU,
        clock: Callable[[], float] = time.monotonic,
        due_timer: _Timer | None = None,
        display_timer: _Timer | None = None,
    ) -> None:
        super().__init__(parent)
        self._coordinator = coordinator
        self._settings = settings
        if not isinstance(target_kind, CollectionTargetKind) or target_kind not in (
            CollectionTargetKind.FOR_YOU,
            CollectionTargetKind.FOLLOWING,
        ):
            raise ValueError("target_kind must be a home feed")
        self._target_kind = target_kind
        self._display_name = (
            "For You" if target_kind is CollectionTargetKind.FOR_YOU else "Following"
        )
        self._clock = clock
        self._due_timer = due_timer or cast(_Timer, QTimer(self))
        self._display_timer = display_timer or cast(_Timer, QTimer(self))
        self._due_timer.setSingleShot(True)
        self._display_timer.setSingleShot(False)
        self._due_timer.timeout.connect(self._on_due)
        self._display_timer.timeout.connect(self._emit_countdown)
        coordinator.session_blocked.connect(self._on_session_blocked)
        coordinator.session_state_changed.connect(self._on_session_state)
        self._interval_minutes = settings.feed_auto_interval_minutes(target_kind)
        self._maximum = settings.feed_count(target_kind)
        self._deadline: float | None = None
        self._running = False
        self._paused_for_session = False

    @property
    def remaining_seconds(self) -> int | None:
        if not self._running or self._paused_for_session or self._deadline is None:
            return None
        return max(0, math.ceil(self._deadline - self._clock()))

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._arm_full_interval()

    def stop(self) -> None:
        self._running = False
        self._deadline = None
        self._due_timer.stop()
        self._display_timer.stop()
        self.countdown_changed.emit(None)

    def set_interval(self, minutes: int) -> None:
        if type(minutes) is not int or minutes not in AUTO_INTERVAL_MINUTES:
            raise ValueError("minutes is not a supported automatic interval")
        self._settings.set_feed_auto_interval_minutes(self._target_kind, minutes)
        self._interval_minutes = minutes
        if self._running:
            self._arm_full_interval()

    def set_maximum(self, maximum: int) -> None:
        if type(maximum) is not int or maximum not in MY_FEED_COUNTS:
            raise ValueError("maximum is not a supported My Feed count")
        self._settings.set_feed_count(self._target_kind, maximum)
        self._maximum = maximum

    def _arm_full_interval(self, *, announce: bool = True) -> None:
        self._due_timer.stop()
        self._display_timer.stop()
        self._deadline = None
        if not self._running or self._paused_for_session or self._interval_minutes == 0:
            self.countdown_changed.emit(None)
            if self._interval_minutes == 0:
                self.status_changed.emit(tr("Automatic collection is off"))
            return
        milliseconds = self._interval_minutes * 60 * 1_000
        self._deadline = self._clock() + (self._interval_minutes * 60)
        self._due_timer.start(milliseconds)
        self._display_timer.start(1_000)
        if announce:
            self.status_changed.emit(
                tr("Automatic collection scheduled in {minutes} minutes").format(
                    minutes=self._interval_minutes
                )
            )
        self._emit_countdown()

    def _on_due(self) -> None:
        if not self._running or self._paused_for_session or self._interval_minutes == 0:
            return
        accepted = self._coordinator.collect_feed(
            self._target_kind,
            self._maximum,
            interactive=False,
            parent=None,
        )
        if accepted:
            self.status_changed.emit(
                tr("Automatic {feed} collection queued").format(feed=tr(self._display_name))
            )
        self._arm_full_interval(announce=False)

    def _emit_countdown(self) -> None:
        self.countdown_changed.emit(self.remaining_seconds)

    def _on_session_blocked(self, diagnostic: str) -> None:
        self._paused_for_session = True
        self._due_timer.stop()
        self._display_timer.stop()
        self._deadline = None
        self.status_changed.emit(diagnostic)
        self.countdown_changed.emit(None)

    def _on_session_state(self, state: object) -> None:
        if state is not SessionState.SIGNED_IN or not self._paused_for_session:
            return
        self._paused_for_session = False
        self._arm_full_interval()
