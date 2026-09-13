import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWidgets import QWidget

from xfeed.collection import DiscoveryReason
from xfeed.public_collector import (
    AuthenticatedProfileCollector,
    EXTRACTION_SCRIPT,
    PageSnapshot,
    PublicProfileCollector,
    SnapshotParseError,
    is_allowed_profile_url,
    parse_page_snapshot,
)
from xfeed.sources import SourceProfile


class SignalDouble:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in tuple(self.callbacks):
            callback(*args)

    def disconnect(self, callback) -> None:
        if callback in self.callbacks:
            self.callbacks.remove(callback)


class PageDouble:
    def __init__(self) -> None:
        self.loadFinished = SignalDouble()
        self.urlChanged = SignalDouble()
        self.navigationDenied = SignalDouble()
        self.loads: list[QUrl] = []
        self.set_urls: list[QUrl] = []
        self.scripts: list[str] = []
        self.callbacks = []
        self.stopped = False
        self.deleted = False

    def load(self, url: QUrl) -> None:
        self.loads.append(url)

    def setUrl(self, url: QUrl) -> None:
        self.set_urls.append(url)

    def runJavaScript(self, script: str, callback) -> None:
        self.scripts.append(script)
        self.callbacks.append(callback)

    def complete_script(self, payload: object = None, index: int = -1) -> None:
        self.callbacks[index](payload)

    def stop(self) -> None:
        self.stopped = True

    def deleteLater(self) -> None:
        self.deleted = True


class TimerDouble:
    def __init__(self) -> None:
        self.timeout = SignalDouble()
        self.single_shot = False
        self.active = False
        self.starts: list[int] = []
        self.stop_count = 0

    def setSingleShot(self, single_shot: bool) -> None:
        self.single_shot = single_shot

    def start(self, interval: int) -> None:
        self.active = True
        self.starts.append(interval)

    def stop(self) -> None:
        self.active = False
        self.stop_count += 1

    def fire(self) -> None:
        if not self.active:
            return
        self.active = False
        self.timeout.emit()


class DeletionTrackingProfile(QWebEngineProfile):
    def __init__(self, storage_path: str) -> None:
        super().__init__("authenticated-collector-deletion-test")
        self.setPersistentStoragePath(storage_path)
        self.delete_later_calls = 0

    def deleteLater(self) -> None:
        self.delete_later_calls += 1


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, milliseconds: int) -> None:
        self.now += milliseconds / 1000


def build_collector(*, maximum: int = 30, maximum_no_progress: int = 2, timeout_ms: int = 10_000):
    page = PageDouble()
    timer = TimerDouble()
    clock = ManualClock()
    widget = QWidget()
    collector = PublicProfileCollector(
        page_factory=lambda _url: page,
        widget=widget,
        timer=timer,
        clock=clock,
        maximum=maximum,
        settle_ms=0,
        maximum_no_progress=maximum_no_progress,
        timeout_ms=timeout_ms,
    )
    progress = []
    finished = []
    collector.progress.connect(progress.append)
    collector.finished.connect(finished.append)
    return collector, page, timer, clock, progress, finished


def begin_extraction(collector, page, timer, profile=None) -> None:
    collector.start(profile or SourceProfile("openai", "https://x.com/openai"))
    page.loadFinished.emit(True)
    timer.fire()


def extraction_payload(
    post_ids: tuple[str, ...] = (),
    *,
    login_wall: bool = False,
    end_reached: bool = False,
    page_supported: bool = True,
) -> dict[str, object]:
    return snapshot_payload(
        observations=[
            observation_payload(
                url=f"https://x.com/openai/status/{post_id}",
                post_id=post_id,
                is_pinned=False,
                is_reply=False,
                is_repost=False,
                is_quote=False,
            )
            for post_id in post_ids
        ],
        login_wall=login_wall,
        end_reached=end_reached,
        page_supported=page_supported,
    )


def observation_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "url": "https://x.com/openai/status/123",
        "post_id": "123",
        "author_handle": "openai",
        "is_pinned": True,
        "is_reply": True,
        "is_repost": True,
        "is_quote": True,
    }
    payload.update(overrides)
    return payload


def snapshot_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "observations": [observation_payload()],
        "login_wall": False,
        "end_reached": False,
        "page_supported": True,
        "page_url": "https://x.com/openai",
    }
    payload.update(overrides)
    return payload


def test_parse_page_snapshot_converts_valid_rendered_card_fields():
    snapshot = parse_page_snapshot(snapshot_payload())

    assert snapshot == PageSnapshot(
        observations=(
            snapshot.observations[0].__class__(
                url="https://x.com/openai/status/123",
                post_id="123",
                author_handle="openai",
                is_pinned=True,
                is_reply=True,
                is_repost=True,
                is_quote=True,
                discovery_order=0,
            ),
        ),
        login_wall=False,
        end_reached=False,
        page_supported=True,
        page_url="https://x.com/openai",
    )


@pytest.mark.parametrize("payload", [None, [], "not a mapping", 3])
def test_parse_page_snapshot_rejects_non_dictionary_top_levels(payload):
    with pytest.raises(SnapshotParseError):
        parse_page_snapshot(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", 123),
        ("post_id", None),
        ("author_handle", []),
        ("is_pinned", "false"),
        ("is_reply", 0),
        ("is_repost", None),
        ("is_quote", "true"),
    ],
)
def test_parse_page_snapshot_rejects_malformed_observation_fields(field, value):
    payload = snapshot_payload(observations=[observation_payload(**{field: value})])

    with pytest.raises(SnapshotParseError):
        parse_page_snapshot(payload)


@pytest.mark.parametrize(
    "payload",
    [
        snapshot_payload(observations={}),
        snapshot_payload(observations=[None]),
        snapshot_payload(login_wall="false"),
        snapshot_payload(end_reached=0),
        snapshot_payload(page_supported=None),
        snapshot_payload(page_url=None),
    ],
)
def test_parse_page_snapshot_rejects_malformed_snapshot_fields(payload):
    with pytest.raises(SnapshotParseError):
        parse_page_snapshot(payload)


def test_start_uses_canonical_x_profile_url_instead_of_supplied_profile_url(qtbot):
    collector, page, timer, _, _, _ = build_collector()

    collector.start(SourceProfile("OpenAI", "https://example.com/trap"))

    assert page.loads == [QUrl("https://x.com/openai")]
    assert page.set_urls == []
    assert timer.single_shot


def test_authenticated_collector_never_deletes_injected_persistent_profile(tmp_path, qtbot):
    profile = DeletionTrackingProfile(str(tmp_path / "storage"))
    pages: list[PageDouble] = []

    def offline_page_factory(_url):
        page = PageDouble()
        pages.append(page)
        return page

    collector = AuthenticatedProfileCollector(
        profile,
        settle_ms=10_000,
        page_factory=offline_page_factory,
        widget=QWidget(),
    )
    qtbot.addWidget(collector.widget)

    collector.start(SourceProfile("openai", "https://x.com/openai"))
    collector.cancel()
    collector.start(SourceProfile("openai", "https://x.com/openai"))
    collector.cancel()

    assert profile.delete_later_calls == 0
    assert [page.loads for page in pages] == [
        [QUrl("https://x.com/openai")],
        [QUrl("https://x.com/openai")],
    ]
    QWebEngineProfile.deleteLater(profile)


def test_progress_is_unique_and_qualifying_across_rerenders(qtbot):
    collector, page, timer, _, progress, finished = build_collector()
    begin_extraction(collector, page, timer)

    payload = extraction_payload(("1",))
    payload["observations"].extend(
        [observation_payload(post_id="2", url="https://x.com/openai/status/2")]
    )
    page.complete_script(payload)
    page.complete_script(None)
    timer.fire()
    page.complete_script(extraction_payload(("1", "3")))

    assert [candidate.post_id for candidate in progress] == ["1", "3"]
    assert finished == []


def test_scroll_is_requested_only_after_extraction_callback(qtbot):
    collector, page, timer, _, _, _ = build_collector()
    begin_extraction(collector, page, timer)

    assert page.scripts == [EXTRACTION_SCRIPT]
    page.complete_script(extraction_payload(("1",)))

    assert len(page.scripts) == 2
    assert "window.scrollBy" in page.scripts[-1]
    assert timer.starts[-1] > 0
    page.complete_script(None)
    assert timer.active
    assert timer.starts[-1] == 0


def test_limit_finishes_at_configured_maximum_and_never_exceeds_thirty(qtbot):
    collector, page, timer, _, progress, finished = build_collector(maximum=30)
    begin_extraction(collector, page, timer)

    page.complete_script(extraction_payload(tuple(str(index) for index in range(35))))

    assert len(progress) == 30
    assert len(finished) == 1
    assert len(finished[0].candidates) == 30
    assert finished[0].reason is DiscoveryReason.LIMIT
    assert not timer.active


def test_no_progress_finishes_after_bounded_number_of_passes(qtbot):
    collector, page, timer, _, _, finished = build_collector(maximum_no_progress=2)
    begin_extraction(collector, page, timer)

    page.complete_script(extraction_payload())
    page.complete_script(None)
    timer.fire()
    page.complete_script(extraction_payload())

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.NO_PROGRESS


def test_timeout_finishes_when_load_never_completes(qtbot):
    collector, _, timer, clock, _, finished = build_collector(timeout_ms=50)
    collector.start(SourceProfile("openai", "https://x.com/openai"))

    clock.advance_ms(50)
    timer.fire()

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.TIMEOUT


def test_login_wall_finishes_without_scrolling(qtbot):
    collector, page, timer, _, _, finished = build_collector()
    begin_extraction(collector, page, timer)

    page.complete_script(extraction_payload(login_wall=True))

    assert finished[0].reason is DiscoveryReason.LOGIN_WALL
    assert len(page.scripts) == 1


def test_end_reached_finishes_as_exhausted(qtbot):
    collector, page, timer, _, _, finished = build_collector()
    begin_extraction(collector, page, timer)

    page.complete_script(extraction_payload(("1",), end_reached=True))
    assert finished == []
    page.complete_script(None)
    timer.fire()
    page.complete_script(extraction_payload(("1",), end_reached=True))
    assert finished == []
    page.complete_script(None)
    timer.fire()
    page.complete_script(extraction_payload(("1",), end_reached=True))

    assert finished[0].reason is DiscoveryReason.EXHAUSTED
    assert [candidate.post_id for candidate in finished[0].candidates] == ["1"]


def test_initial_empty_end_snapshot_waits_for_delayed_qualifying_render(qtbot):
    collector, page, timer, _, progress, finished = build_collector(maximum_no_progress=2)
    begin_extraction(collector, page, timer)

    page.complete_script(extraction_payload(end_reached=True))

    assert finished == []
    page.complete_script(None)
    timer.fire()
    page.complete_script(extraction_payload(("1",), end_reached=True))

    assert [candidate.post_id for candidate in progress] == ["1"]
    assert finished == []


def test_end_confirmation_requires_two_empty_passes_at_minimum_threshold(qtbot):
    collector, page, timer, _, _, finished = build_collector(maximum_no_progress=1)
    begin_extraction(collector, page, timer)

    page.complete_script(extraction_payload(end_reached=True))

    assert finished == []


def test_cancel_emits_once_and_ignores_late_callbacks(qtbot):
    collector, page, timer, _, _, finished = build_collector()
    begin_extraction(collector, page, timer)

    collector.cancel()
    collector.cancel()
    page.complete_script(extraction_payload(("1",)))
    page.loadFinished.emit(False)
    timer.fire()

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.CANCELLED
    assert finished[0].candidates == ()


def test_malformed_payload_finishes_as_error_without_escaping_callback(qtbot):
    collector, page, timer, _, _, finished = build_collector()
    begin_extraction(collector, page, timer)

    page.complete_script({"observations": "bad"})

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.ERROR
    assert finished[0].diagnostic


def test_load_failure_finishes_once_and_late_success_is_ignored(qtbot):
    collector, page, timer, _, _, finished = build_collector()
    collector.start(SourceProfile("openai", "https://x.com/openai"))

    page.loadFinished.emit(False)
    page.loadFinished.emit(True)
    timer.fire()

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.ERROR
    assert page.scripts == []


def test_unsupported_page_finishes_as_error(qtbot):
    collector, page, timer, _, _, finished = build_collector()
    begin_extraction(collector, page, timer)

    page.complete_script(extraction_payload(page_supported=False))

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.ERROR


def test_new_run_invalidates_old_javascript_callback(qtbot):
    first_page = PageDouble()
    second_page = PageDouble()
    pages = iter((first_page, second_page))
    timer = TimerDouble()
    collector = PublicProfileCollector(
        page_factory=lambda _url: next(pages),
        widget=QWidget(),
        timer=timer,
        clock=ManualClock(),
        settle_ms=0,
    )
    progress = []
    finished = []
    collector.progress.connect(progress.append)
    collector.finished.connect(finished.append)
    begin_extraction(collector, first_page, timer)
    old_callback = first_page.callbacks[-1]

    collector.start(SourceProfile("second", "https://x.com/second"))
    old_callback(extraction_payload(("1",)))

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.CANCELLED
    assert progress == []
    assert second_page.loads == [QUrl("https://x.com/second")]


def test_extraction_script_only_reads_rendered_tweet_cards(qtbot):
    assert 'article[data-testid="tweet"]' in EXTRACTION_SCRIPT
    assert "time" in EXTRACTION_SCRIPT
    assert "status" in EXTRACTION_SCRIPT
    assert "window.scrollBy" not in EXTRACTION_SCRIPT
    assert "fetch(" not in EXTRACTION_SCRIPT
    assert "cookie" not in EXTRACTION_SCRIPT.casefold()


def test_synchronous_terminal_javascript_callback_does_not_restart_timer(qtbot):
    collector, page, timer, _, _, finished = build_collector(maximum=1)
    collector.start(SourceProfile("openai", "https://x.com/openai"))
    page.loadFinished.emit(True)

    def complete_synchronously(script, callback) -> None:
        page.scripts.append(script)
        callback(extraction_payload(("1",)))

    page.runJavaScript = complete_synchronously
    timer.fire()

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.LIMIT
    assert not timer.active


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"page_supported": False}, DiscoveryReason.ERROR),
        ({"login_wall": True}, DiscoveryReason.LOGIN_WALL),
        ({"page_url": "https://x.com/home"}, DiscoveryReason.ERROR),
        ({"page_url": "https://x.com/someone-else"}, DiscoveryReason.ERROR),
    ],
)
def test_invalid_or_blocked_page_wins_before_candidate_progress(qtbot, overrides, expected_reason):
    collector, page, timer, _, progress, finished = build_collector(maximum=1)
    begin_extraction(collector, page, timer)

    page.complete_script(extraction_payload(("1",)) | overrides)

    assert progress == []
    assert len(finished) == 1
    assert finished[0].reason is expected_reason
    assert finished[0].candidates == ()


def test_cancelling_from_progress_slot_emits_no_later_candidates_or_scroll(qtbot):
    collector, page, timer, _, progress, finished = build_collector()
    collector.progress.connect(lambda _candidate: collector.cancel())
    begin_extraction(collector, page, timer)

    page.complete_script(extraction_payload(("1", "2", "3")))

    assert [candidate.post_id for candidate in progress] == ["1"]
    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.CANCELLED
    assert [candidate.post_id for candidate in finished[0].candidates] == ["1"]
    assert page.scripts == [EXTRACTION_SCRIPT]


def test_old_page_load_failure_cannot_terminate_superseding_run(qtbot):
    first_page = PageDouble()
    second_page = PageDouble()
    pages = iter((first_page, second_page))
    factory_pages = []

    def page_factory(_url):
        page = next(pages)
        factory_pages.append(page)
        return page

    timer = TimerDouble()
    collector = PublicProfileCollector(
        page_factory=page_factory,
        widget=QWidget(),
        timer=timer,
        clock=ManualClock(),
        settle_ms=0,
    )
    finished = []
    collector.finished.connect(finished.append)

    collector.start(SourceProfile("first", "https://x.com/first"))
    collector.start(SourceProfile("second", "https://x.com/second"))
    first_page.loadFinished.emit(False)

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.CANCELLED
    assert first_page.stopped
    assert first_page.deleted
    assert first_page.loadFinished.callbacks == []
    assert factory_pages == [first_page, second_page]
    assert factory_pages[0] is not factory_pages[1]
    assert second_page.loads == [QUrl("https://x.com/second")]

    second_page.loadFinished.emit(False)
    assert len(finished) == 2
    assert finished[-1].reason is DiscoveryReason.ERROR


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://x.com/openai", True),
        ("https://x.com/openai/", True),
        ("https://x.com/openai?lang=tr", True),
        ("https://x.com/login", False),
        ("https://x.com/i/flow/login", False),
        ("https://x.com/home", False),
        ("https://x.com/other", False),
        ("https://twitter.com/openai", False),
        ("http://x.com/openai", False),
        ("https://user@x.com/openai", False),
    ],
)
def test_only_exact_canonical_public_profile_navigation_is_allowed(url, allowed):
    assert is_allowed_profile_url(url, "https://x.com/openai") is allowed


def test_login_url_change_terminates_as_login_wall_before_extraction(qtbot):
    collector, page, _, _, progress, finished = build_collector()
    collector.start(SourceProfile("openai", "https://x.com/openai"))

    page.urlChanged.emit(QUrl("https://x.com/i/flow/login"))

    assert progress == []
    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.LOGIN_WALL
    assert page.scripts == []


def test_login_url_change_during_settle_still_terminates_as_login_wall(qtbot):
    collector, page, _, _, _, finished = build_collector()
    collector.start(SourceProfile("openai", "https://x.com/openai"))
    page.loadFinished.emit(True)

    page.urlChanged.emit(QUrl("https://x.com/i/flow/login"))

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.LOGIN_WALL
    assert page.scripts == []


@pytest.mark.parametrize(
    "path", ["/login", "/i/flow/login", "/i/flow/login?redirect_after_login=%2Fopenai"]
)
def test_denied_login_navigation_wins_before_late_load_failure(qtbot, path):
    collector, page, _, _, _, finished = build_collector()
    collector.start(SourceProfile("openai", "https://x.com/openai"))

    page.navigationDenied.emit(QUrl(f"https://x.com{path}"))
    page.loadFinished.emit(False)

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.LOGIN_WALL


def test_unrelated_denied_navigation_remains_error(qtbot):
    collector, page, _, _, _, finished = build_collector()
    collector.start(SourceProfile("openai", "https://x.com/openai"))

    page.navigationDenied.emit(QUrl("https://x.com/home"))

    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.ERROR


def test_fixed_page_injection_is_rejected_in_favor_of_per_run_factory(qtbot):
    with pytest.raises(TypeError):
        PublicProfileCollector(page=PageDouble(), widget=QWidget())


def test_factory_cannot_reconnect_page_already_scheduled_for_deletion(qtbot):
    page = PageDouble()
    collector = PublicProfileCollector(
        page_factory=lambda _url: page,
        widget=QWidget(),
        timer=TimerDouble(),
        clock=ManualClock(),
    )
    finished = []
    collector.finished.connect(finished.append)

    collector.start(SourceProfile("first", "https://x.com/first"))
    collector.start(SourceProfile("second", "https://x.com/second"))

    assert [result.reason for result in finished] == [
        DiscoveryReason.CANCELLED,
        DiscoveryReason.ERROR,
    ]
    assert page.loads == [QUrl("https://x.com/first")]
    assert page.loadFinished.callbacks == []
