from dataclasses import replace

import pytest
from PySide6.QtCore import QObject, Signal

from xfeed.collection import CandidateObservation, DiscoveryReason
from xfeed.domain import CollectionTarget, CollectionTargetKind
from xfeed.opera_collector import OperaExtensionCollector
from xfeed.opera_protocol import OperaJob, OperaJobResult, parse_observations


class BridgeDouble(QObject):
    candidate_received = Signal(object, object)
    job_finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[OperaJob] = []
        self.cancelled: list[str] = []

    def enqueue(
        self,
        target: CollectionTarget,
        maximum: int = 30,
        timeout_s: float = 45.0,
    ) -> OperaJob:
        job = OperaJob(
            f"job-{len(self.jobs) + 1}",
            target.kind,
            target.handle,
            target.profile_url,
            maximum,
            timeout_s,
        )
        self.jobs.append(job)
        return job

    def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)


def source_target(handle: str = "openai", source_id: int = 7) -> CollectionTarget:
    normalized = handle.casefold()
    return CollectionTarget.for_source(source_id, normalized, f"https://x.com/{normalized}")


def observation(post_id: str, author: str = "openai", **changes: object) -> CandidateObservation:
    value = CandidateObservation(
        url=f"https://x.com/{author}/status/{post_id}",
        post_id=post_id,
        author_handle=author,
        discovery_order=int(post_id),
    )
    return replace(value, **changes)


def capture(collector: OperaExtensionCollector):
    progress: list[CandidateObservation] = []
    finished: list[object] = []
    collector.progress.connect(progress.append)
    collector.finished.connect(finished.append)
    return progress, finished


def test_collector_creates_canonical_job_and_exposes_status_widget(qtbot):
    bridge = BridgeDouble()
    collector = OperaExtensionCollector(bridge, timeout_s=7.5)
    target = source_target()

    collector.start(target, 12)

    assert bridge.jobs == [
        OperaJob(
            "job-1",
            CollectionTargetKind.SOURCE,
            "openai",
            "https://x.com/openai",
            12,
            7.5,
        )
    ]
    assert collector.widget.text() == "Collection is running in the dedicated Opera GX tab."


def test_collector_filters_bridge_candidates_and_finishes(qtbot):
    bridge = BridgeDouble()
    collector = OperaExtensionCollector(bridge)
    progress: list[CandidateObservation] = []
    collector.progress.connect(progress.append)
    collector.start(source_target())
    job = bridge.jobs[-1]

    bridge.candidate_received.emit(job.id, observation("1"))
    bridge.candidate_received.emit(job.id, observation("2", is_quote=True))
    with qtbot.waitSignal(collector.finished) as finished:
        bridge.job_finished.emit(OperaJobResult(job.id, DiscoveryReason.EXHAUSTED))

    assert [value.post_id for value in progress] == ["1"]
    assert finished.args[0].candidates == tuple(progress)


def test_source_collector_rejects_promoted_observation_parsed_from_protocol(qtbot):
    bridge = BridgeDouble()
    collector = OperaExtensionCollector(bridge)
    progress, finished = capture(collector)
    collector.start(source_target())
    job = bridge.jobs[-1]
    promoted = parse_observations(
        {
            "protocol_version": 4,
            "observations": [
                {
                    "url": "https://x.com/openai/status/1",
                    "post_id": "1",
                    "author_handle": "openai",
                    "is_pinned": False,
                    "is_reply": False,
                    "is_repost": False,
                    "is_quote": False,
                    "is_promoted": True,
                    "parent_url": None,
                    "discovery_order": 0,
                    "photos": [],
                }
            ],
        }
    )[0]

    bridge.candidate_received.emit(job.id, promoted)
    bridge.job_finished.emit(OperaJobResult(job.id, DiscoveryReason.EXHAUSTED))

    assert progress == []
    assert finished[0].candidates == ()


def test_collector_filters_all_non_authoritative_and_duplicate_candidates(qtbot):
    bridge = BridgeDouble()
    collector = OperaExtensionCollector(bridge)
    progress, finished = capture(collector)
    collector.start(source_target())
    job = bridge.jobs[-1]
    excluded = (
        observation("2", is_reply=True),
        observation("3", is_quote=True),
        observation("4", is_repost=True),
        observation("5", is_pinned=True),
        observation("6", author="someone_else"),
        observation("7", url="https://x.com/someone_else/status/7"),
    )

    bridge.candidate_received.emit("job-not-active", observation("99"))
    bridge.candidate_received.emit(job.id, observation("1"))
    bridge.candidate_received.emit(job.id, observation("1"))
    for value in excluded:
        bridge.candidate_received.emit(job.id, value)
    bridge.job_finished.emit(OperaJobResult("job-not-active", DiscoveryReason.ERROR, "stale"))
    bridge.job_finished.emit(OperaJobResult(job.id, DiscoveryReason.EXHAUSTED))

    assert [value.post_id for value in progress] == ["1"]
    assert len(finished) == 1


def test_collector_preserves_progress_order_and_caps_at_thirty(qtbot):
    bridge = BridgeDouble()
    collector = OperaExtensionCollector(bridge)
    progress, finished = capture(collector)
    collector.start(source_target(), 30)
    job = bridge.jobs[-1]

    for index in range(35, 0, -1):
        bridge.candidate_received.emit(job.id, observation(str(index)))
    bridge.job_finished.emit(OperaJobResult(job.id, DiscoveryReason.LIMIT))

    assert [value.post_id for value in progress] == [str(index) for index in range(35, 5, -1)]
    assert finished[0].candidates == tuple(progress)


@pytest.mark.parametrize(
    ("reason", "diagnostic"),
    [
        (DiscoveryReason.TIMEOUT, "bridge detail"),
        (DiscoveryReason.LOGIN_WALL, "X is signed out; reconnect"),
        (DiscoveryReason.ERROR, "X presented an account challenge"),
        (DiscoveryReason.ERROR, "X rate-limited the session"),
    ],
)
def test_collector_maps_terminal_reason_and_diagnostic(qtbot, reason, diagnostic):
    bridge = BridgeDouble()
    collector = OperaExtensionCollector(bridge)
    _, finished = capture(collector)
    collector.start(source_target())
    job = bridge.jobs[-1]

    bridge.job_finished.emit(OperaJobResult(job.id, reason, diagnostic))
    bridge.job_finished.emit(OperaJobResult(job.id, reason, "duplicate"))

    assert len(finished) == 1
    assert finished[0].reason is reason
    assert finished[0].diagnostic == diagnostic


def test_cancel_delegates_once_finishes_and_suppresses_late_progress(qtbot):
    bridge = BridgeDouble()
    collector = OperaExtensionCollector(bridge)
    progress, finished = capture(collector)
    collector.start(source_target())
    job = bridge.jobs[-1]

    collector.cancel()
    collector.cancel()
    bridge.candidate_received.emit(job.id, observation("1"))
    bridge.job_finished.emit(OperaJobResult(job.id, DiscoveryReason.CANCELLED))

    assert bridge.cancelled == [job.id]
    assert progress == []
    assert len(finished) == 1
    assert finished[0].reason is DiscoveryReason.CANCELLED


def test_restart_cancels_prior_job_and_ignores_its_late_signals(qtbot):
    bridge = BridgeDouble()
    collector = OperaExtensionCollector(bridge)
    progress, finished = capture(collector)
    collector.start(source_target("first"))
    first = bridge.jobs[-1]

    collector.start(source_target())
    second = bridge.jobs[-1]
    bridge.candidate_received.emit(first.id, observation("1"))
    bridge.job_finished.emit(OperaJobResult(first.id, DiscoveryReason.CANCELLED))
    bridge.candidate_received.emit(second.id, observation("2"))
    bridge.job_finished.emit(OperaJobResult(second.id, DiscoveryReason.EXHAUSTED))
    bridge.candidate_received.emit(second.id, observation("3"))

    assert bridge.cancelled == [first.id]
    assert [value.post_id for value in progress] == ["2"]
    assert len(finished) == 1
    assert finished[0].candidates == (observation("2"),)


def test_for_you_start_enqueues_fifty_and_preserves_mixed_authors_and_kinds(qtbot):
    bridge = BridgeDouble()
    collector = OperaExtensionCollector(bridge)
    progress, finished = capture(collector)
    target = CollectionTarget.for_you()

    collector.start(target, 50)
    job = bridge.jobs[-1]
    bridge.candidate_received.emit(job.id, observation("1", author="alpha", is_repost=True))
    bridge.candidate_received.emit(job.id, observation("2", author="beta", is_reply=True))
    bridge.job_finished.emit(OperaJobResult(job.id, DiscoveryReason.EXHAUSTED))

    assert job == OperaJob(
        "job-1",
        CollectionTargetKind.FOR_YOU,
        None,
        "https://x.com/home",
        50,
        45.0,
    )
    assert progress == [
        observation("1", author="alpha", is_repost=True),
        observation("2", author="beta", is_reply=True),
    ]
    assert finished[0].candidates == tuple(progress)


def test_following_start_preserves_mixed_authors_and_home_observation_kinds(qtbot):
    bridge = BridgeDouble()
    collector = OperaExtensionCollector(bridge)
    progress, finished = capture(collector)

    collector.start(CollectionTarget.following(), 50)
    job = bridge.jobs[-1]
    bridge.candidate_received.emit(job.id, observation("1", author="alpha", is_repost=True))
    bridge.candidate_received.emit(job.id, observation("2", author="beta", is_reply=True))
    bridge.candidate_received.emit(job.id, observation("3", is_promoted=True))
    bridge.job_finished.emit(OperaJobResult(job.id, DiscoveryReason.EXHAUSTED))

    assert job.target_kind is CollectionTargetKind.FOLLOWING
    assert [item.post_id for item in progress] == ["1", "2"]
    assert finished[0].candidates == tuple(progress)


@pytest.mark.parametrize(
    "value",
    [0, float("inf")],
)
def test_collector_validates_timeout(qtbot, value):
    with pytest.raises(ValueError):
        OperaExtensionCollector(BridgeDouble(), timeout_s=value)


@pytest.mark.parametrize(
    ("target", "maximum", "message"),
    [
        (source_target(), 0, "between 1 and 30"),
        (source_target(), 31, "between 1 and 30"),
        (CollectionTarget.for_you(), 0, "between 1 and 50"),
        (CollectionTarget.for_you(), 51, "between 1 and 50"),
    ],
)
def test_collector_validates_target_specific_maximum(qtbot, target, maximum, message):
    collector = OperaExtensionCollector(BridgeDouble())

    with pytest.raises(ValueError, match=message):
        collector.start(target, maximum)
