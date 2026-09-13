import json
from collections.abc import Callable
from typing import Any

import pytest

from xfeed.collection import DiscoveryReason
from xfeed.edge_browser import RATE_LIMIT_SCRIPT
from xfeed.edge_collector import EdgeProfileCollector
from xfeed.public_collector import SCROLL_SCRIPT, build_extraction_script
from xfeed.sources import SourceProfile


class BrowserDouble:
    def __init__(
        self,
        snapshots: list[object] | None = None,
        *,
        rate_limits: list[object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.snapshots = snapshots or []
        self.rate_limits = rate_limits or []
        self.error = error
        self.opened_urls: list[str] = []
        self.open_timeouts: list[float] = []
        self.scripts: list[str] = []
        self.stop_calls = 0
        self.close_calls = 0
        self.clear_calls = 0

    def open(self, url: str, timeout_s: float = 15.0) -> str:
        self.opened_urls.append(url)
        self.open_timeouts.append(timeout_s)
        if self.error is not None:
            raise self.error
        return url

    def evaluate(self, script: str, timeout_s: float = 10.0) -> object:
        del timeout_s
        self.scripts.append(script)
        if self.error is not None:
            raise self.error
        if script == RATE_LIMIT_SCRIPT:
            return self.rate_limits.pop(0) if self.rate_limits else False
        if script == SCROLL_SCRIPT:
            return None
        if script == build_extraction_script("openai"):
            if not self.snapshots:
                raise RuntimeError("no snapshot configured")
            payload = self.snapshots[0]
            if len(self.snapshots) > 1:
                self.snapshots.pop(0)
            return json.dumps(payload) if isinstance(payload, dict) else payload
        raise AssertionError(f"unexpected script: {script}")

    def stop(self) -> None:
        self.stop_calls += 1

    def clear_x_site_data(self) -> None:
        self.clear_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class DeferredExecutor:
    def __init__(self) -> None:
        self.jobs: list[
            tuple[Callable[[], object], Callable[[object], None], Callable[[str], None]]
        ] = []

    def __call__(
        self,
        operation: Callable[[], object],
        succeeded: Callable[[object], None],
        failed: Callable[[str], None],
    ) -> None:
        self.jobs.append((operation, succeeded, failed))

    def run(self, index: int) -> None:
        operation, succeeded, failed = self.jobs[index]
        try:
            succeeded(operation())
        except Exception as error:
            failed(str(error))


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlowFailingBrowser(BrowserDouble):
    def __init__(self, clock: ManualClock, command: str) -> None:
        super().__init__([snapshot()])
        self.clock = clock
        self.command = command

    def _fail(self, command: str) -> None:
        if self.command == command:
            self.clock.advance(1.0)
            raise RuntimeError(f"slow {command} failure")

    def open(self, url: str, timeout_s: float = 15.0) -> str:
        self._fail("open")
        return super().open(url, timeout_s)

    def evaluate(self, script: str, timeout_s: float = 10.0) -> object:
        if script == RATE_LIMIT_SCRIPT:
            self._fail("rate_limit")
        elif script == build_extraction_script("openai"):
            self._fail("extraction")
        elif script == SCROLL_SCRIPT:
            self._fail("scroll")
        return super().evaluate(script, timeout_s)


def immediate_executor(
    operation: Callable[[], object],
    succeeded: Callable[[object], None],
    failed: Callable[[str], None],
) -> None:
    try:
        succeeded(operation())
    except Exception as error:
        failed(str(error))


def post(handle: str, post_id: str, **overrides: Any) -> dict[str, object]:
    payload: dict[str, object] = {
        "url": f"https://x.com/{handle}/status/{post_id}",
        "post_id": post_id,
        "author_handle": handle,
        "is_pinned": False,
        "is_reply": False,
        "is_repost": False,
        "is_quote": False,
    }
    payload.update(overrides)
    return payload


def snapshot(
    posts: list[dict[str, object]] | None = None,
    *,
    login_wall: bool = False,
    end_reached: bool = False,
    page_supported: bool = True,
    page_url: str = "https://x.com/openai",
) -> dict[str, object]:
    return {
        "observations": posts or [],
        "login_wall": login_wall,
        "end_reached": end_reached,
        "page_supported": page_supported,
        "page_url": page_url,
    }


def capture(collector: EdgeProfileCollector) -> tuple[list[object], list[object]]:
    progress: list[object] = []
    finished: list[object] = []
    collector.progress.connect(progress.append)
    collector.finished.connect(finished.append)
    return progress, finished


def test_collector_uses_selected_profile_and_emits_only_qualifying_posts(qtbot):
    excluded = [
        post("openai", "2", is_pinned=True),
        post("openai", "3", is_reply=True),
        post("openai", "4", is_repost=True),
        post("openai", "5", is_quote=True),
        post("someone_else", "6"),
        post("openai", "7", url="https://x.com/someone_else/status/7"),
    ]
    browser = BrowserDouble([snapshot([post("openai", "1"), *excluded], end_reached=True)])
    collector = EdgeProfileCollector(browser, executor=immediate_executor, settle_ms=0)
    progress, finished = capture(collector)

    collector.start(SourceProfile("OpenAI", "https://example.com/trap"))

    assert browser.opened_urls == ["https://x.com/openai"]
    assert [candidate.post_id for candidate in progress] == ["1"]
    assert [candidate.post_id for candidate in finished[0].candidates] == ["1"]
    assert finished[0].reason is DiscoveryReason.EXHAUSTED
    assert browser.scripts.count(SCROLL_SCRIPT) == 3


def test_collector_suppresses_duplicates_across_scroll_passes(qtbot):
    browser = BrowserDouble(
        [
            snapshot([post("openai", "1")]),
            snapshot([post("openai", "1"), post("openai", "2")]),
            snapshot([post("openai", "2")], end_reached=True),
        ]
    )
    collector = EdgeProfileCollector(
        browser,
        executor=immediate_executor,
        settle_ms=0,
        maximum_no_progress=2,
    )
    progress, finished = capture(collector)

    collector.start(SourceProfile("openai", "https://x.com/openai"))

    assert [candidate.post_id for candidate in progress] == ["1", "2"]
    assert finished[0].reason is DiscoveryReason.EXHAUSTED
    assert browser.scripts.count(SCROLL_SCRIPT) == 3


def test_collector_caps_progress_and_result_at_thirty(qtbot):
    browser = BrowserDouble([snapshot([post("openai", str(index)) for index in range(35)])])
    collector = EdgeProfileCollector(browser, executor=immediate_executor, settle_ms=0)
    progress, finished = capture(collector)

    collector.start(SourceProfile("openai", "https://x.com/openai"))

    assert len(progress) == 30
    assert len(finished[0].candidates) == 30
    assert finished[0].reason is DiscoveryReason.LIMIT
    assert SCROLL_SCRIPT not in browser.scripts


def test_collector_stops_after_bounded_no_progress(qtbot):
    browser = BrowserDouble([snapshot()])
    collector = EdgeProfileCollector(
        browser,
        executor=immediate_executor,
        settle_ms=0,
        maximum_no_progress=2,
    )
    _, finished = capture(collector)

    collector.start(SourceProfile("openai", "https://x.com/openai"))

    assert finished[0].reason is DiscoveryReason.NO_PROGRESS
    assert browser.scripts.count(SCROLL_SCRIPT) == 1


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (snapshot(login_wall=True), DiscoveryReason.LOGIN_WALL),
        (snapshot(page_supported=False), DiscoveryReason.ERROR),
        (snapshot(page_url="https://x.com/home"), DiscoveryReason.ERROR),
        ({"observations": "malformed"}, DiscoveryReason.ERROR),
    ],
)
def test_collector_rejects_blocked_or_invalid_snapshots_before_progress(qtbot, payload, reason):
    payload = payload | {"observations": payload.get("observations", []) or [post("openai", "1")]}
    browser = BrowserDouble([payload])
    collector = EdgeProfileCollector(browser, executor=immediate_executor, settle_ms=0)
    progress, finished = capture(collector)

    collector.start(SourceProfile("openai", "https://x.com/openai"))

    assert progress == []
    assert len(finished) == 1
    assert finished[0].reason is reason
    assert SCROLL_SCRIPT not in browser.scripts


def test_collector_stops_on_exact_rate_limit_probe_without_extracting(qtbot):
    browser = BrowserDouble([snapshot()], rate_limits=[True])
    collector = EdgeProfileCollector(browser, executor=immediate_executor, settle_ms=0)
    _, finished = capture(collector)

    collector.start(SourceProfile("openai", "https://x.com/openai"))

    assert browser.scripts == [RATE_LIMIT_SCRIPT]
    assert finished[0].reason is DiscoveryReason.ERROR
    assert finished[0].diagnostic == "X temporarily limited this browser session"


def test_collector_times_out_before_first_probe(qtbot):
    times = iter((0.0, 0.0, 1.0))
    browser = BrowserDouble([snapshot()])
    collector = EdgeProfileCollector(
        browser,
        executor=immediate_executor,
        clock=lambda: next(times),
        settle_ms=0,
        timeout_ms=10,
    )
    _, finished = capture(collector)

    collector.start(SourceProfile("openai", "https://x.com/openai"))

    assert browser.opened_urls == ["https://x.com/openai"]
    assert browser.scripts == []
    assert finished[0].reason is DiscoveryReason.TIMEOUT


def test_queued_expired_run_times_out_without_navigation(qtbot):
    clock = ManualClock()
    executor = DeferredExecutor()
    browser = BrowserDouble([snapshot()])
    collector = EdgeProfileCollector(
        browser,
        executor=executor,
        clock=clock,
        settle_ms=0,
        timeout_ms=100,
    )
    _, finished = capture(collector)
    collector.start(SourceProfile("openai", "https://x.com/openai"))

    clock.advance(0.1)
    executor.run(0)

    assert browser.opened_urls == []
    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.TIMEOUT


def test_navigation_timeout_uses_only_remaining_run_time_after_queue_delay(qtbot):
    clock = ManualClock()
    executor = DeferredExecutor()
    browser = BrowserDouble([snapshot()], rate_limits=[True])
    collector = EdgeProfileCollector(
        browser,
        executor=executor,
        clock=clock,
        settle_ms=0,
        timeout_ms=100,
    )
    collector.start(SourceProfile("openai", "https://x.com/openai"))

    clock.advance(0.04)
    executor.run(0)

    assert browser.open_timeouts == [pytest.approx(0.06)]


@pytest.mark.parametrize("command", ["open", "rate_limit", "extraction", "scroll"])
def test_slow_command_exception_reports_timeout_at_each_browser_boundary(qtbot, command):
    clock = ManualClock()
    browser = SlowFailingBrowser(clock, command)
    collector = EdgeProfileCollector(
        browser,
        executor=immediate_executor,
        clock=clock,
        settle_ms=0,
        timeout_ms=100,
    )
    _, finished = capture(collector)

    collector.start(SourceProfile("openai", "https://x.com/openai"))

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.TIMEOUT


def test_browser_failure_emits_one_error_terminal(qtbot):
    browser = BrowserDouble(error=RuntimeError("Edge unavailable"))
    collector = EdgeProfileCollector(browser, executor=immediate_executor, settle_ms=0)
    _, finished = capture(collector)

    collector.start(SourceProfile("openai", "https://x.com/openai"))
    collector.cancel()

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.ERROR


def test_cancel_stops_browser_and_worker_finishes_once(qtbot):
    executor = DeferredExecutor()
    browser = BrowserDouble([snapshot()])
    collector = EdgeProfileCollector(browser, executor=executor, settle_ms=0)
    _, finished = capture(collector)
    collector.start(SourceProfile("openai", "https://x.com/openai"))

    collector.cancel()
    collector.cancel()
    executor.run(0)

    assert browser.stop_calls == 1
    assert browser.close_calls == 0
    assert browser.clear_calls == 0
    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.CANCELLED


def test_cancel_during_browser_probe_wins_over_late_probe_result(qtbot):
    browser = BrowserDouble([snapshot()], rate_limits=[True])
    collector = EdgeProfileCollector(browser, executor=immediate_executor, settle_ms=0)
    _, finished = capture(collector)
    original_evaluate = browser.evaluate

    def evaluate_and_cancel(script: str, timeout_s: float = 10.0) -> object:
        result = original_evaluate(script, timeout_s)
        if script == RATE_LIMIT_SCRIPT:
            collector.cancel()
        return result

    browser.evaluate = evaluate_and_cancel  # type: ignore[method-assign]

    collector.start(SourceProfile("openai", "https://x.com/openai"))

    assert browser.stop_calls == 1
    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.CANCELLED


def test_superseded_and_late_worker_results_are_ignored(qtbot):
    executor = DeferredExecutor()
    browser = BrowserDouble([snapshot(end_reached=True)])
    collector = EdgeProfileCollector(
        browser,
        executor=executor,
        settle_ms=0,
        maximum_no_progress=1,
    )
    _, finished = capture(collector)

    collector.start(SourceProfile("first", "https://x.com/first"))
    collector.start(SourceProfile("openai", "https://x.com/openai"))
    executor.run(0)
    executor.run(1)

    assert browser.opened_urls == ["https://x.com/openai"]
    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.EXHAUSTED


def test_cancel_after_discovery_before_success_callback_replaces_stale_limit(qtbot):
    executor = DeferredExecutor()
    browser = BrowserDouble([snapshot([post("openai", "1")])])
    collector = EdgeProfileCollector(
        browser,
        executor=executor,
        settle_ms=0,
        maximum=1,
    )
    _, finished = capture(collector)
    collector.start(SourceProfile("openai", "https://x.com/openai"))
    operation, succeeded, failed = executor.jobs[0]
    result = operation()

    collector.cancel()
    succeeded(result)
    succeeded(result)
    failed("late executor failure")

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.CANCELLED
    assert [candidate.post_id for candidate in finished[0].candidates] == ["1"]


def test_cancel_before_failure_callback_replaces_stale_error(qtbot):
    executor = DeferredExecutor()
    browser = BrowserDouble([snapshot()])
    collector = EdgeProfileCollector(browser, executor=executor, settle_ms=0)
    _, finished = capture(collector)
    collector.start(SourceProfile("openai", "https://x.com/openai"))
    _, succeeded, failed = executor.jobs[0]

    collector.cancel()
    failed("late executor failure")
    failed("duplicate late failure")
    succeeded(object())

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.CANCELLED


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("maximum", 0),
        ("maximum", 31),
        ("settle_ms", -1),
        ("maximum_no_progress", 0),
        ("timeout_ms", 0),
    ],
)
def test_collector_validates_bounded_options(qtbot, option, value):
    with pytest.raises(ValueError):
        EdgeProfileCollector(BrowserDouble(), **{option: value})
