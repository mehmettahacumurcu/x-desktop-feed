import pytest
from PySide6.QtCore import QObject, Signal

from xfeed.domain import CollectionTargetKind
from xfeed.ui.auto_scheduler import AutoCollectionScheduler
from xfeed.ui.settings import UiSettingsStore
from xfeed.x_session import SessionState


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TimerDouble(QObject):
    timeout = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.single_shot = False
        self.started: list[int] = []
        self.stop_count = 0
        self.active = False

    def setSingleShot(self, single_shot: bool) -> None:
        self.single_shot = single_shot

    def start(self, milliseconds: int) -> None:
        self.started.append(milliseconds)
        self.active = True

    def stop(self) -> None:
        self.stop_count += 1
        self.active = False

    def fire(self) -> None:
        if not self.active:
            return
        if self.single_shot:
            self.active = False
        self.timeout.emit()


class CoordinatorDouble(QObject):
    session_blocked = Signal(str)
    session_state_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.calls: list[tuple[CollectionTargetKind, int, bool, object]] = []

    def collect_feed(
        self,
        kind: CollectionTargetKind,
        maximum: int,
        *,
        interactive: bool,
        parent: object,
    ) -> bool:
        self.calls.append((kind, maximum, interactive, parent))
        return True


def build_scheduler(
    tmp_path,
    *,
    interval: int = 0,
    maximum: int = 10,
    target_kind: CollectionTargetKind = CollectionTargetKind.FOR_YOU,
):
    settings = UiSettingsStore(tmp_path / "ui.ini")
    settings.set_feed_auto_interval_minutes(target_kind, interval)
    settings.set_feed_count(target_kind, maximum)
    coordinator = CoordinatorDouble()
    clock = Clock()
    due = TimerDouble()
    display = TimerDouble()
    scheduler = AutoCollectionScheduler(
        coordinator,
        settings,
        target_kind=target_kind,
        clock=clock,
        due_timer=due,
        display_timer=display,
    )
    return settings, coordinator, clock, due, display, scheduler


def test_never_interval_does_not_arm_or_submit(tmp_path) -> None:
    _settings, coordinator, _clock, due, display, scheduler = build_scheduler(tmp_path)

    scheduler.start()

    assert due.started == []
    assert display.started == []
    assert scheduler.remaining_seconds is None
    assert coordinator.calls == []


def test_remembered_interval_waits_one_full_interval_after_start(tmp_path) -> None:
    _settings, coordinator, clock, due, display, scheduler = build_scheduler(tmp_path, interval=10)

    scheduler.start()

    assert due.started == [600_000]
    assert display.started == [1_000]
    assert scheduler.remaining_seconds == 600
    clock.advance(1.2)
    display.fire()
    assert scheduler.remaining_seconds == 599
    assert coordinator.calls == []


def test_changing_never_to_five_minutes_waits_instead_of_collecting(tmp_path) -> None:
    settings, coordinator, _clock, due, _display, scheduler = build_scheduler(tmp_path)
    scheduler.start()

    scheduler.set_interval(5)

    assert settings.auto_interval_minutes() == 5
    assert due.started == [300_000]
    assert coordinator.calls == []


def test_due_idle_tick_collects_once_and_rearms_full_interval(tmp_path) -> None:
    _settings, coordinator, clock, due, _display, scheduler = build_scheduler(
        tmp_path, interval=5, maximum=20
    )
    scheduler.start()
    clock.advance(300)

    due.fire()

    assert coordinator.calls == [(CollectionTargetKind.FOR_YOU, 20, False, None)]
    assert due.started == [300_000, 300_000]
    assert scheduler.remaining_seconds == 300


def test_due_busy_tick_queues_and_rearms(tmp_path) -> None:
    _settings, coordinator, clock, due, _display, scheduler = build_scheduler(tmp_path, interval=5)
    statuses: list[str] = []
    scheduler.status_changed.connect(statuses.append)
    coordinator.busy = True
    scheduler.start()
    clock.advance(300)

    due.fire()

    assert coordinator.calls == [(CollectionTargetKind.FOR_YOU, 10, False, None)]
    assert statuses[-1] == "Automatic For You collection queued"
    assert due.started == [300_000, 300_000]


def test_two_due_schedulers_submit_fifo_requests_with_independent_settings(tmp_path) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")
    settings.set_feed_auto_interval_minutes(CollectionTargetKind.FOR_YOU, 5)
    settings.set_feed_auto_interval_minutes(CollectionTargetKind.FOLLOWING, 10)
    settings.set_feed_count(CollectionTargetKind.FOR_YOU, 20)
    settings.set_feed_count(CollectionTargetKind.FOLLOWING, 50)
    coordinator = CoordinatorDouble()
    first_due = TimerDouble()
    second_due = TimerDouble()
    for_you = AutoCollectionScheduler(
        coordinator,
        settings,
        target_kind=CollectionTargetKind.FOR_YOU,
        due_timer=first_due,
        display_timer=TimerDouble(),
    )
    following = AutoCollectionScheduler(
        coordinator,
        settings,
        target_kind=CollectionTargetKind.FOLLOWING,
        due_timer=second_due,
        display_timer=TimerDouble(),
    )
    for_you.start()
    following.start()
    coordinator.busy = True

    first_due.fire()
    second_due.fire()

    assert coordinator.calls == [
        (CollectionTargetKind.FOR_YOU, 20, False, None),
        (CollectionTargetKind.FOLLOWING, 50, False, None),
    ]


@pytest.mark.parametrize("raw_kind", ["for_you", "following"])
def test_scheduler_rejects_raw_string_target_kind(tmp_path, raw_kind: str) -> None:
    settings = UiSettingsStore(tmp_path / "ui.ini")
    coordinator = CoordinatorDouble()

    with pytest.raises(ValueError, match="home feed"):
        AutoCollectionScheduler(
            coordinator,
            settings,
            target_kind=raw_kind,  # type: ignore[arg-type]
            due_timer=TimerDouble(),
            display_timer=TimerDouble(),
        )


def test_session_block_pauses_then_signed_in_rearms_full_interval(tmp_path) -> None:
    _settings, coordinator, clock, due, display, scheduler = build_scheduler(tmp_path, interval=10)
    scheduler.start()
    clock.advance(90)

    coordinator.session_blocked.emit("X rate-limited the Opera GX session")

    assert scheduler.remaining_seconds is None
    assert not due.active
    assert not display.active

    coordinator.session_state_changed.emit(SessionState.SIGNED_IN)

    assert due.started == [600_000, 600_000]
    assert scheduler.remaining_seconds == 600


def test_stop_prevents_later_callbacks(tmp_path) -> None:
    _settings, coordinator, clock, due, display, scheduler = build_scheduler(tmp_path, interval=5)
    scheduler.start()
    scheduler.stop()
    clock.advance(300)

    due.fire()
    display.fire()

    assert coordinator.calls == []
    assert scheduler.remaining_seconds is None


def test_maximum_change_persists_and_is_used_by_next_due_tick(tmp_path) -> None:
    settings, coordinator, clock, due, _display, scheduler = build_scheduler(tmp_path, interval=5)
    scheduler.start()

    scheduler.set_maximum(50)
    clock.advance(300)
    due.fire()

    assert settings.my_feed_count() == 50
    assert coordinator.calls == [(CollectionTargetKind.FOR_YOU, 50, False, None)]
