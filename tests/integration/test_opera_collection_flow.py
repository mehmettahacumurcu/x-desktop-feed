import json
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtWidgets import QWidget

from xfeed.app import build_services
from xfeed.domain import (
    CollectionStatus,
    CollectionTarget,
    CollectionTargetKind,
    ObservationKind,
    PostDraft,
)
from xfeed.media import MediaAssetState, MediaOrigin, MediaResolutionState
from xfeed.opera_bridge import OperaBridge
from xfeed.opera_collector import OperaExtensionCollector
from xfeed.opera_media_resolver import OperaMediaResolver
from xfeed.opera_session import OperaXSession
from xfeed.session_repository import AppSessionRepository
from xfeed.sources import canonicalize_profile
from xfeed.ui.collection_controller import CollectionController
from xfeed.ui.collection_coordinator import CollectionCoordinator
from xfeed.ui.feed_view import FeedView
from xfeed.ui.settings import UiSettingsStore
from xfeed.urls import CanonicalPostUrl
from xfeed.x_session import SessionState


CONTRACT = Path(__file__).parents[1] / "fixtures" / "opera_protocol" / "contract.json"


class FixtureProvider:
    def fetch(self, post_url: CanonicalPostUrl) -> PostDraft:
        return PostDraft(
            canonical_url=post_url.url,
            x_post_id=post_url.post_id,
            author_handle=post_url.handle,
            author_name="OpenAI",
            text="fixture post",
            published_at="2026-07-20T00:00:00Z",
            embed_html="<blockquote>fixture post pic.x.com/media</blockquote>",
            provider_json="{}",
            source_method="oembed-fixture",
            has_media=True,
        )


class WebViewDouble(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.html = ""

    def setHtml(self, html: str, _base_url: QUrl) -> None:
        self.html = html


class FeedCoordinatorDouble(QObject):
    state_changed = Signal(object)
    request_completed = Signal(object)
    queue_finished = Signal()

    def __init__(self, run_ids: tuple[int, ...]) -> None:
        super().__init__()
        self.run_ids = run_ids
        self.busy = False

    def current_run_ids(self, kind: CollectionTargetKind) -> tuple[int, ...]:
        return self.run_ids if kind is CollectionTargetKind.FOR_YOU else ()

    def collect_feed(self, *_args, **_kwargs) -> bool:
        return True


class FeedSchedulerDouble(QObject):
    countdown_changed = Signal(object)
    status_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.remaining_seconds = None

    def set_interval(self, _value: int) -> None:
        pass

    def set_maximum(self, _value: int) -> None:
        pass


@contextmanager
def running_bridge(data_path: Path) -> Iterator[OperaBridge]:
    bridge = OperaBridge(data_path, port=0)
    bridge.start()
    try:
        yield bridge
    finally:
        bridge.close()


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


def test_running_bridge_closes_after_later_setup_failure(tmp_path):
    address: tuple[str, int] | None = None
    with pytest.raises(RuntimeError, match="later setup failed"):
        with running_bridge(tmp_path / "bridge") as bridge:
            address = bridge.server_address
            raise RuntimeError("later setup failed")

    assert address is not None
    with pytest.raises(OSError):
        socket.create_connection(address, timeout=0.2)


def test_opera_bridge_collection_saves_only_qualifying_posts(tmp_path, qtbot):
    services = build_services(tmp_path / "feed.sqlite3", provider=FixtureProvider())
    address: tuple[str, int] | None = None
    with running_bridge(tmp_path / "bridge") as bridge:
        address = bridge.server_address
        session = OperaXSession(bridge, tmp_path / "extension")
        collector = OperaExtensionCollector(bridge)
        controller = CollectionController(
            collector,
            services.collection_service,
            services.collections,
            session_id=AppSessionRepository(services.database).begin("test").id,
        )
        source = services.sources.add(canonicalize_profile("openai"))
        offer = bridge.issue_pairing_code()
        token = request_json(bridge, "POST", "/v1/pair", {"code": offer.code})["token"]
        assert isinstance(token, str)
        request_json(
            bridge,
            "POST",
            "/v1/heartbeat",
            {"protocol_version": 4, "x_state": "signed_in"},
            token=token,
        )
        qtbot.waitUntil(lambda: session.state is SessionState.SIGNED_IN, timeout=2_000)
        assert session.state is SessionState.SIGNED_IN
        with qtbot.waitSignal(controller.completed, timeout=5_000) as completed:
            controller.start(
                CollectionTarget.for_source(source.id, source.handle, source.profile_url),
                30,
            )
            job = request_json(bridge, "POST", "/v1/jobs/poll", {}, token=token)["job"]
            assert isinstance(job, dict) and isinstance(job["id"], str)
            progress = json.loads(CONTRACT.read_text(encoding="utf-8"))["progress"]
            progress["observations"].extend(
                [
                    {
                        **progress["observations"][0],
                        "discovery_order": 1,
                    },
                    {
                        **progress["observations"][0],
                        "url": "https://x.com/openai/status/124",
                        "post_id": "124",
                        "is_reply": True,
                        "discovery_order": 2,
                    },
                ]
            )
            request_json(
                bridge,
                "POST",
                f"/v1/jobs/{job['id']}/progress",
                progress,
                token=token,
            )
            request_json(
                bridge,
                "POST",
                f"/v1/jobs/{job['id']}/complete",
                {"protocol_version": 4, "reason": "exhausted", "diagnostic": None},
                token=token,
            )

        result = completed.args[0]
        assert result.run.saved_count == 1
        assert result.run.candidate_count == 1
        assert result.run.duplicate_count == 0
        assert result.run.failed_count == 0
        assert result.run.status is CollectionStatus.PARTIAL
        assert result.run.reason == "exhausted"
        assert services.posts.count_by_author("openai") == 1
        saved = services.posts.by_url("https://x.com/openai/status/123")
        assert saved is not None
        assert saved.author_name == "OpenAI"
        assert saved.text == "fixture post"
        assert saved.published_at == "2026-07-20T00:00:00Z"
        assert saved.embed_html == "<blockquote>fixture post pic.x.com/media</blockquote>"
        assert saved.provider_json == "{}"
        assert saved.source_method == "oembed-fixture"
        assert saved.has_media is True
        assert services.collections.latest_for_source(source.id) == result.run

    assert address is not None
    with pytest.raises(OSError):
        socket.create_connection(address, timeout=0.2)


def test_manual_save_uses_bridge_exact_post_photo_resolution(tmp_path, qtbot):
    services = build_services(tmp_path / "feed.sqlite3", provider=FixtureProvider())
    saved_result = services.post_service.quick_save("https://x.com/openai/status/777")
    assert saved_result.post is not None
    saved = saved_result.post
    with running_bridge(tmp_path / "bridge") as bridge:
        session = OperaXSession(bridge, tmp_path / "extension")
        collector = OperaExtensionCollector(bridge)
        controller = CollectionController(
            collector,
            services.collection_service,
            services.collections,
            session_id=AppSessionRepository(services.database).begin("test").id,
        )
        resolver = OperaMediaResolver(bridge)
        coordinator = CollectionCoordinator(
            controller,
            services.sources,
            session,
            lambda _parent: (_ for _ in ()).throw(AssertionError("login is not expected")),
            media_repository=services.media_repository,
            media_manifest_service=services.media_manifest_service,
            media_resolver=resolver,
        )
        offer = bridge.issue_pairing_code()
        token = request_json(bridge, "POST", "/v1/pair", {"code": offer.code})["token"]
        assert isinstance(token, str)
        request_json(
            bridge,
            "POST",
            "/v1/heartbeat",
            {"protocol_version": 4, "x_state": "signed_in"},
            token=token,
        )
        qtbot.waitUntil(lambda: session.state is SessionState.SIGNED_IN, timeout=2_000)

        assert coordinator.resolve_post_photos(saved, None)
        job = request_json(bridge, "POST", "/v1/jobs/poll", {}, token=token)["job"]
        assert isinstance(job, dict)
        assert job["kind"] == "resolve_post_photos"
        assert job["post_id"] == saved.x_post_id
        assert job["post_url"] == saved.canonical_url
        assert 1 <= job["timeout_ms"] <= 45_000
        response = request_json(
            bridge,
            "POST",
            f"/v1/jobs/{job['id']}/complete",
            {
                "protocol_version": 4,
                "reason": "exhausted",
                "diagnostic": None,
                "photos": [
                    {
                        "position": 0,
                        "url": "https://pbs.twimg.com/media/777?format=jpg&name=large",
                        "alt_text": "manual",
                    }
                ],
            },
            token=token,
        )

        assert response == {"protocol_version": 4, "accepted": True}
        qtbot.waitUntil(
            lambda: bool(services.media_repository.assets_for_posts((saved.id,)).get(saved.id)),
            timeout=2_000,
        )
        assets = services.media_repository.assets_for_posts((saved.id,))
        assert assets[saved.id][0].state is MediaAssetState.PENDING
        with services.database.connection() as connection:
            resolution = connection.execute(
                "SELECT origin,state,manifest_count FROM media_resolution_jobs WHERE post_id=?",
                (saved.id,),
            ).fetchone()
        assert resolution is not None
        assert tuple(resolution) == (
            MediaOrigin.MANUAL.value,
            MediaResolutionState.RESOLVED.value,
            1,
        )


def test_for_you_opera_flow_keeps_global_duplicate_and_latest_useful_order(tmp_path, qtbot):
    services = build_services(tmp_path / "feed.sqlite3", provider=FixtureProvider())
    existing = services.posts.insert(
        PostDraft(
            canonical_url="https://x.com/alpha/status/901",
            x_post_id="901",
            author_handle="alpha",
            author_name="Alpha",
            text="globally saved before For You collection",
            published_at="2026-07-19T00:00:00Z",
            embed_html="<blockquote>existing post</blockquote>",
            provider_json="{}",
            source_method="oembed-fixture",
        )
    )
    with running_bridge(tmp_path / "bridge") as bridge:
        session = OperaXSession(bridge, tmp_path / "extension")
        collector = OperaExtensionCollector(bridge)
        controller = CollectionController(
            collector,
            services.collection_service,
            services.collections,
            session_id=AppSessionRepository(services.database).begin("test").id,
        )
        offer = bridge.issue_pairing_code()
        token = request_json(bridge, "POST", "/v1/pair", {"code": offer.code})["token"]
        assert isinstance(token, str)
        request_json(
            bridge,
            "POST",
            "/v1/heartbeat",
            {"protocol_version": 4, "x_state": "signed_in"},
            token=token,
        )
        qtbot.waitUntil(lambda: session.state is SessionState.SIGNED_IN, timeout=2_000)

        observation = json.loads(CONTRACT.read_text(encoding="utf-8"))["progress"]["observations"][
            0
        ]
        observations = [
            {
                **observation,
                "url": existing.canonical_url,
                "post_id": existing.x_post_id,
                "author_handle": "alpha",
                "discovery_order": 2,
            },
            {
                **observation,
                "url": "https://x.com/gamma/status/903",
                "post_id": "903",
                "author_handle": "gamma",
                "is_reply": True,
                "discovery_order": 1,
            },
            {
                **observation,
                "url": "https://x.com/beta/status/902",
                "post_id": "902",
                "author_handle": "beta",
                "is_repost": True,
                "discovery_order": 0,
            },
        ]
        with qtbot.waitSignal(controller.completed, timeout=5_000) as completed:
            controller.start(CollectionTarget.for_you(), 10)
            job = request_json(bridge, "POST", "/v1/jobs/poll", {}, token=token)["job"]
            assert isinstance(job, dict) and job["target_kind"] == "for_you"
            request_json(
                bridge,
                "POST",
                f"/v1/jobs/{job['id']}/progress",
                {"protocol_version": 4, "observations": observations},
                token=token,
            )
            request_json(
                bridge,
                "POST",
                f"/v1/jobs/{job['id']}/complete",
                {"protocol_version": 4, "reason": "exhausted", "diagnostic": None},
                token=token,
            )

        useful_result = completed.args[0]
        assert useful_result.run.saved_count == 2
        assert useful_result.run.duplicate_count == 1
        latest = services.collections.latest_for_you_posts()
        assert [row.post.x_post_id for row in latest] == ["902", "903", "901"]
        assert [row.observation_kind for row in latest] == [
            ObservationKind.REPOST,
            ObservationKind.REPLY,
            ObservationKind.POST,
        ]
        assert latest[-1].post.id == existing.id

        with qtbot.waitSignal(controller.completed, timeout=5_000) as failed:
            controller.start(CollectionTarget.for_you(), 10)
            empty_job = request_json(bridge, "POST", "/v1/jobs/poll", {}, token=token)["job"]
            assert isinstance(empty_job, dict)
            request_json(
                bridge,
                "POST",
                f"/v1/jobs/{empty_job['id']}/complete",
                {
                    "protocol_version": 4,
                    "reason": "error",
                    "diagnostic": "no usable cards",
                },
                token=token,
            )

        assert failed.args[0].run.status is CollectionStatus.FAILED
        assert [row.post.x_post_id for row in services.collections.latest_for_you_posts()] == [
            "902",
            "903",
            "901",
        ]

        web = WebViewDouble()
        web_widgets = iter((web, WebViewDouble()))
        feed = FeedView(
            services.posts,
            services.post_service,
            collections=services.collections,
            coordinator=FeedCoordinatorDouble((useful_result.run.id,)),
            schedulers={
                CollectionTargetKind.FOR_YOU: FeedSchedulerDouble(),
                CollectionTargetKind.FOLLOWING: FeedSchedulerDouble(),
            },
            settings=UiSettingsStore(tmp_path / "ui.ini"),
            web_factory=lambda: next(web_widgets),
            saved_web_factory=WebViewDouble,
        )
        qtbot.addWidget(feed)

        assert web.html.index('id="post-') < len(web.html)
        assert web.html.index("status/902") < web.html.index("status/903")
        assert web.html.index("status/903") < web.html.index("status/901")
