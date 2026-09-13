import threading
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import subprocess
import sys

import pytest
from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal
from PySide6.QtWebEngineCore import QWebEngineUrlScheme
from PySide6.QtWidgets import QWidget

from xfeed.analytics import AnalyticsRepository
from xfeed.collection import CollectionProgress, CollectionResult
from xfeed.app import (
    ApplicationServices,
    build_services,
    create_application,
    default_bridge_data_path,
    default_media_path,
    default_ui_settings_path,
)
from xfeed.domain import (
    CollectionRun,
    CollectionStatus,
    CollectionTarget,
    CollectionTargetKind,
    PostDraft,
)
from xfeed.media import MediaOrigin
from xfeed.oembed import OEmbedClient
from xfeed.posts import SaveResult, SaveStatus
from xfeed.session_repository import AppSessionRepository
from xfeed.topic_repository import TopicAnalysisRepository
from xfeed.ui.collection_controller import CollectionController
from xfeed.ui.feed_view import FeedView
from xfeed.ui.insights_view import InsightsView
from xfeed.ui.sources_view import SourcesView
from xfeed.ui.workers import Worker
from xfeed.x_session import SessionState


def test_app_module_registers_media_scheme_before_qapplication_in_fresh_process() -> None:
    workspace_root = Path(__file__).resolve().parents[1]
    environment = os.environ | {
        "PYTHONPATH": str(workspace_root / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from PySide6.QtWidgets import QApplication\n"
            "from PySide6.QtWebEngineCore import QWebEngineUrlScheme\n"
            "assert QApplication.instance() is None\n"
            "import xfeed.app\n"
            "assert QApplication.instance() is None\n"
            "assert not QWebEngineUrlScheme.schemeByName(b'xfeed-media').name().isEmpty()\n",
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert process.returncode == 0, process.stderr


def test_main_reports_an_already_running_launch_and_exits_cleanly(monkeypatch, qapp) -> None:
    import xfeed.__main__ as entrypoint

    already_running = getattr(entrypoint, "ApplicationAlreadyRunning", RuntimeError)
    messages: list[tuple[str, str]] = []

    def refuse(_argv):
        raise already_running("X Desktop Feed is already running")

    class MessageBoxDouble:
        @staticmethod
        def information(_parent, title: str, message: str) -> None:
            messages.append((title, message))

    monkeypatch.setattr(entrypoint, "create_application", refuse)
    monkeypatch.setattr(entrypoint, "QMessageBox", MessageBoxDouble, raising=False)

    assert entrypoint.main() == 0
    assert messages == [("X Desktop Feed", "X Desktop Feed is already running")]


class OperaBridgeDouble(QObject):
    instances: list["OperaBridgeDouble"] = []

    def __init__(self, data_dir, parent=None) -> None:
        super().__init__(parent)
        self.data_dir = data_dir
        self.start_count = 0
        self.close_count = 0
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.start_count += 1

    def close(self) -> None:
        self.close_count += 1


class ApplicationSessionDouble(QObject):
    state_changed = Signal(object)
    verification_finished = Signal(object)
    instances: list["ApplicationSessionDouble"] = []

    def __init__(self, bridge, extension_path, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.extension_path = extension_path
        self.state = SessionState.SIGNED_OUT
        self.verify_count = 0
        self.clear_count = 0
        self.__class__.instances.append(self)

    def verify(self) -> None:
        self.verify_count += 1

    def begin_login(self) -> None:
        self.set_state(SessionState.SIGNING_IN)

    def clear(self) -> None:
        self.clear_count += 1
        self.set_state(SessionState.UNAVAILABLE)

    def set_state(self, state: SessionState) -> None:
        self.state = state
        self.state_changed.emit(state)


class OperaCollectorDouble(QObject):
    progress = Signal(object)
    finished = Signal(object)
    instances: list["OperaCollectorDouble"] = []

    def __init__(self, bridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.widget = QWidget()
        self.__class__.instances.append(self)

    def start(self, _target, _maximum=30) -> None:
        pass

    def cancel(self) -> None:
        pass


class ApplicationLoginDialogDouble(QWidget):
    signed_in = Signal()
    cancelled = Signal()
    instances: list["ApplicationLoginDialogDouble"] = []

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.__class__.instances.append(self)


class MediaDownloadManagerDouble(QObject):
    asset_changed = Signal(int)
    status_changed = Signal(str)
    instances: list["MediaDownloadManagerDouble"] = []

    def __init__(self, repository, downloader, parent=None) -> None:
        super().__init__(parent)
        self.repository = repository
        self.downloader = downloader
        self.start_count = 0
        self.stop_count = 0
        self.schedule_count = 0
        self.asset_connections_at_start = 0
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.asset_connections_at_start = self.receivers("2asset_changed(int)")
        self.start_count += 1

    def stop(self) -> None:
        self.stop_count += 1

    def schedule(self) -> None:
        self.schedule_count += 1


class OperaMediaResolverDouble(QObject):
    completed = Signal(object)
    failed = Signal(str)
    instances: list["OperaMediaResolverDouble"] = []

    def __init__(self, bridge, *, parent=None, **_kwargs) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.__class__.instances.append(self)

    def start(self, _job) -> None:
        pass

    def cancel(self) -> None:
        pass


class TopicAnalysisManagerDouble(QObject):
    status_changed = Signal(object)
    completed = Signal(object)
    instances: list["TopicAnalysisManagerDouble"] = []

    def __init__(self, repository, analyzer, *, parent=None, **_kwargs) -> None:
        super().__init__(parent)
        self.repository = repository
        self.analyzer = analyzer
        self.startup_request_count = 0
        self.stop_count = 0
        self.analysis_requests: list[bool] = []
        self.collection_completions: list[object] = []
        self.manual_completions: list[object] = []
        self.__class__.instances.append(self)

    def status(self):
        return self.repository.status()

    def active_run(self):
        return self.repository.active_run()

    def request_analysis(self, *, force: bool = False) -> None:
        self.analysis_requests.append(force)

    def request_startup_analysis(self) -> None:
        self.startup_request_count += 1

    def on_collection_completed(self, value: object) -> None:
        self.collection_completions.append(value)

    def on_manual_save_completed(self, value: object) -> None:
        self.manual_completions.append(value)

    def stop(self) -> None:
        self.stop_count += 1


class SingleInstanceGuardDouble:
    instances: list["SingleInstanceGuardDouble"] = []

    def __init__(self) -> None:
        self.release_count = 0
        self.__class__.instances.append(self)

    def release(self) -> None:
        self.release_count += 1


def patch_runtime(tmp_path, monkeypatch) -> None:
    OperaBridgeDouble.instances.clear()
    ApplicationSessionDouble.instances.clear()
    OperaCollectorDouble.instances.clear()
    ApplicationLoginDialogDouble.instances.clear()
    MediaDownloadManagerDouble.instances.clear()
    OperaMediaResolverDouble.instances.clear()
    TopicAnalysisManagerDouble.instances.clear()
    SingleInstanceGuardDouble.instances.clear()
    monkeypatch.setattr("xfeed.app.default_database_path", lambda: tmp_path / "feed.sqlite3")
    monkeypatch.setattr("xfeed.app.default_session_data_path", lambda: tmp_path / "web-profile")
    monkeypatch.setattr("xfeed.app.default_extension_path", lambda: tmp_path / "extension")
    monkeypatch.setattr("xfeed.app.default_ui_settings_path", lambda: tmp_path / "ui.ini")
    monkeypatch.setattr("xfeed.app.default_media_path", lambda: tmp_path / "media")
    monkeypatch.setattr("xfeed.app.OperaBridge", OperaBridgeDouble)
    monkeypatch.setattr("xfeed.app.OperaXSession", ApplicationSessionDouble)
    monkeypatch.setattr("xfeed.app.OperaExtensionCollector", OperaCollectorDouble)
    monkeypatch.setattr("xfeed.app.OperaConnectDialog", ApplicationLoginDialogDouble)
    monkeypatch.setattr("xfeed.app.MediaDownloadManager", MediaDownloadManagerDouble)
    monkeypatch.setattr(
        "xfeed.app.OperaMediaResolver",
        OperaMediaResolverDouble,
        raising=False,
    )
    monkeypatch.setattr(
        "xfeed.app.TopicAnalysisManager",
        TopicAnalysisManagerDouble,
        raising=False,
    )
    monkeypatch.setattr(
        "xfeed.app.acquire_single_instance",
        SingleInstanceGuardDouble,
        raising=False,
    )


def test_build_services_uses_one_database_for_every_repository_and_service(tmp_path) -> None:
    services = build_services(tmp_path / "feed.sqlite3")

    assert isinstance(services, ApplicationServices)
    assert services.sources.database is services.database
    assert services.posts.database is services.database
    assert services.collections.database is services.database
    assert services.sessions._database is services.database
    assert services.topic_repository._database is services.database
    assert services.analytics._database is services.database
    assert services.analytics._sessions is services.sessions
    assert isinstance(services.sessions, AppSessionRepository)
    assert isinstance(services.topic_repository, TopicAnalysisRepository)
    assert isinstance(services.analytics, AnalyticsRepository)
    assert services.media_repository.database is services.database
    assert services.post_service.posts is services.posts
    assert services.collection_service.posts is services.post_service
    assert services.collection_service.collections is services.collections
    assert services.collection_service.media is not None
    assert services.collection_service.media.repository is services.media_repository
    assert isinstance(services.post_service.provider, OEmbedClient)
    with pytest.raises(FrozenInstanceError):
        services.database = services.database  # type: ignore[misc]


def test_default_paths_keep_bridge_and_ui_preferences_in_app_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("xfeed.app.default_session_data_path", lambda: tmp_path / "web-profile")
    monkeypatch.setattr("xfeed.app.default_database_path", lambda: tmp_path / "feed.sqlite3")

    assert default_bridge_data_path() == tmp_path / "web-profile" / "opera-bridge"
    assert default_ui_settings_path() == tmp_path / "ui.ini"
    assert default_media_path() == tmp_path / "media"


def test_startup_stops_before_database_open_when_data_migration_fails(tmp_path, monkeypatch, qapp):
    patch_runtime(tmp_path, monkeypatch)
    opened = []

    def fail_migration(_path):
        assert len(SingleInstanceGuardDouble.instances) == 1
        raise PermissionError("old directory is locked")

    monkeypatch.setattr("xfeed.app.prepare_data_directory", fail_migration, raising=False)
    monkeypatch.setattr("xfeed.app.build_services", lambda path: opened.append(path))
    with pytest.raises(RuntimeError, match="old directory is locked"):
        create_application([])
    assert opened == []
    assert SingleInstanceGuardDouble.instances[0].release_count == 1


def test_main_displays_data_migration_error(monkeypatch, qapp):
    import xfeed.__main__ as entrypoint
    import xfeed.app as app_module

    failure = getattr(app_module, "DataDirectoryMigrationError", RuntimeError)
    messages = []

    def refuse(_argv):
        raise failure("Please close the old application")

    class MessageBoxDouble:
        @staticmethod
        def critical(_parent, title, message):
            messages.append((title, message))

    monkeypatch.setattr(entrypoint, "create_application", refuse)
    monkeypatch.setattr(entrypoint, "QMessageBox", MessageBoxDouble)
    assert entrypoint.main() == 1
    assert messages == [("X Desktop Feed", "Please close the old application")]


def test_create_application_composes_one_shared_experience(tmp_path, monkeypatch, qtbot) -> None:
    patch_runtime(tmp_path, monkeypatch)

    _application, window = create_application([])
    qtbot.addWidget(window)
    feed = window.page("My Feed")
    sources = window.page("Sources")
    insights = window.page("Insights")

    assert isinstance(feed, FeedView)
    assert isinstance(sources, SourcesView)
    assert isinstance(insights, InsightsView)
    assert insights.objectName() == "insightsView"
    assert window.current_destination() == "My Feed"
    assert feed.posts is sources.posts
    assert feed.collections is sources.coordinator._controller._collections
    assert feed.coordinator is sources.coordinator
    assert feed.settings is sources.settings
    assert set(feed.schedulers) == {
        CollectionTargetKind.FOR_YOU,
        CollectionTargetKind.FOLLOWING,
    }
    assert all(scheduler._running for scheduler in feed.schedulers.values())
    topic_manager = TopicAnalysisManagerDouble.instances[0]
    assert topic_manager.repository._database is feed.posts.database
    assert insights._analytics._database is feed.posts.database
    assert insights._session_repository._database is feed.posts.database
    assert topic_manager.startup_request_count == 1
    assert ApplicationSessionDouble.instances[0].verify_count == 1
    manager = MediaDownloadManagerDouble.instances[0]
    assert manager.repository.database is feed.collections.database
    assert manager.downloader.media_root == (tmp_path / "media").resolve()
    assert manager.parent() is _application
    assert manager.start_count == 1

    manifest = feed.coordinator._controller._service.media
    assert manifest is not None
    manifest.manifest_recorded.emit(())
    assert manager.schedule_count == 1


def test_create_application_refreshes_insights_for_persistence_and_topic_completions(
    tmp_path, monkeypatch, qtbot
) -> None:
    class TrackingInsightsView(InsightsView):
        def __init__(self, *args, **kwargs) -> None:
            self.refresh_count = 0
            super().__init__(*args, **kwargs)

        def refresh(self) -> None:
            self.refresh_count += 1
            super().refresh()

    patch_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr("xfeed.app.InsightsView", TrackingInsightsView, raising=False)

    _application, window = create_application([])
    qtbot.addWidget(window)
    feed = window.page("My Feed")
    insights = window.page("Insights")
    assert isinstance(feed, FeedView)
    assert isinstance(insights, TrackingInsightsView)
    manager = TopicAnalysisManagerDouble.instances[-1]
    coordinator = feed.coordinator
    assert coordinator is not None

    collection_result = CollectionResult(
        CollectionRun(
            id=99,
            source_id=None,
            started_at="2026-08-02 12:00:00",
            finished_at="2026-08-02 12:01:00",
            requested_max=10,
            candidate_count=1,
            saved_count=0,
            duplicate_count=1,
            failed_count=0,
            status=CollectionStatus.COMPLETED,
            reason="limit",
            diagnostic=None,
            target_kind=CollectionTargetKind.FOLLOWING,
            session_id=1,
        ),
        CollectionProgress(found=1, processed=1, saved=0, duplicates=1, failed=0),
    )
    blank_post = feed.posts.insert(
        PostDraft(
            canonical_url="https://x.com/example/status/999",
            x_post_id="999",
            author_handle="example",
            author_name="Example",
            text=" ",
            published_at=None,
            embed_html="",
            provider_json="{}",
            source_method="test",
        )
    )
    manual_result = SaveResult(SaveStatus.SAVED, post=blank_post, message="Saved")
    refresh_count = insights.refresh_count
    coordinator.request_completed.emit(collection_result)
    assert insights.refresh_count == refresh_count + 1
    feed.manual_saves.post_saved.emit(manual_result)
    assert insights.refresh_count == refresh_count + 2
    manager.completed.emit(object())

    assert manager.collection_completions == [collection_result]
    assert manager.manual_completions == [manual_result]
    assert insights.refresh_count == refresh_count + 3


def test_refused_second_launch_does_not_open_or_mutate_the_active_session(
    tmp_path, monkeypatch
) -> None:
    database_path = tmp_path / "feed.sqlite3"
    persisted = build_services(database_path)
    active = persisted.sessions.begin("already-running")
    patch_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr("xfeed.app.acquire_single_instance", lambda: None, raising=False)
    service_builds = 0
    real_build_services = build_services

    def tracked_build_services(*args, **kwargs):
        nonlocal service_builds
        service_builds += 1
        return real_build_services(*args, **kwargs)

    monkeypatch.setattr("xfeed.app.build_services", tracked_build_services)

    error: RuntimeError | None = None
    try:
        application, _window = create_application([])
    except RuntimeError as caught:
        error = caught
    else:
        application.aboutToQuit.emit()

    assert error is not None
    assert "already running" in str(error).casefold()
    assert service_builds == 0
    assert persisted.sessions.by_id(active.id) == active


def test_owner_acquires_guard_before_database_and_passes_open_session_to_controller(
    tmp_path, monkeypatch, qtbot
) -> None:
    events: list[str] = []
    real_build_services = build_services

    class TrackingController(CollectionController):
        session_ids: list[int] = []

        def __init__(self, *args, session_id: int, **kwargs) -> None:
            events.append(f"controller:{session_id}")
            self.__class__.session_ids.append(session_id)
            super().__init__(*args, session_id=session_id, **kwargs)

    patch_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "xfeed.app.acquire_single_instance",
        lambda: events.append("guard-acquire") or SingleInstanceGuardDouble(),
        raising=False,
    )

    def tracked_build_services(*args, **kwargs):
        events.append("database-open")
        return real_build_services(*args, **kwargs)

    monkeypatch.setattr("xfeed.app.build_services", tracked_build_services)
    monkeypatch.setattr("xfeed.app.CollectionController", TrackingController)

    application, window = create_application([])
    qtbot.addWidget(window)
    session_id = TrackingController.session_ids[-1]
    services = real_build_services(tmp_path / "feed.sqlite3")
    session = services.sessions.by_id(session_id)

    assert events[:2] == ["guard-acquire", "database-open"]
    assert session is not None
    assert session.state.value == "open"
    application.aboutToQuit.emit()
    assert SingleInstanceGuardDouble.instances[-1].release_count == 1


def test_launcher_contract() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    text = (repository_root / "Start X Desktop Feed.cmd").read_text(encoding="utf-8")

    assert 'cd /d "%~dp0"' in text
    assert '"%~dp0.venv\\Scripts\\pythonw.exe" -m xfeed' in text
    assert "pause" in text.lower()


def test_create_application_wires_one_media_graph_profile_and_persisted_backlog(
    tmp_path, monkeypatch, qtbot
) -> None:
    patch_runtime(tmp_path, monkeypatch)
    persisted = build_services(tmp_path / "feed.sqlite3")
    saved = persisted.posts.insert(
        PostDraft(
            canonical_url="https://x.com/alpha/status/700",
            x_post_id="700",
            author_handle="alpha",
            author_name="Alpha",
            text="manual photo",
            published_at="2026-07-24T10:00:00Z",
            embed_html="",
            provider_json="{}",
            source_method="oembed-fixture",
        )
    )
    persisted.media_repository.ensure_resolution_job(
        saved.id,
        saved.canonical_url,
        MediaOrigin.MANUAL,
    )

    application, window = create_application([])
    qtbot.addWidget(window)
    feed = window._pages.widget(0)
    sources = window._pages.widget(1)
    assert isinstance(feed, FeedView)
    assert isinstance(sources, SourcesView)
    coordinator = feed.coordinator
    assert coordinator is not None
    manager = MediaDownloadManagerDouble.instances[-1]
    resolver = OperaMediaResolverDouble.instances[-1]
    repository = feed.feed_pane(CollectionTargetKind.FOR_YOU)._media_repository

    assert not QWebEngineUrlScheme.schemeByName(b"xfeed-media").name().isEmpty()
    assert repository is sources._media_repository
    assert repository is feed.manual_saves._media_repository
    assert repository is manager.repository
    assert repository is coordinator._media_repository
    assert repository is coordinator._media_manifest_service.repository
    assert coordinator._media_resolver is resolver
    assert resolver.bridge is OperaBridgeDouble.instances[-1]
    assert feed.manual_saves._media_resolution_requester == coordinator.resolve_post_photos
    assert feed.saved_pane._media_repository is repository
    assert manager.asset_connections_at_start == 5
    assert coordinator.busy
    assert ApplicationLoginDialogDouble.instances == []

    feed_profile = feed.feed_pane(CollectionTargetKind.FOR_YOU).web_view.page().profile()
    following_profile = feed.feed_pane(CollectionTargetKind.FOLLOWING).web_view.page().profile()
    sources_profile = sources.web_view.page().profile()
    manual_profile = feed.manual_saves.web_view.page().profile()
    assert feed_profile is following_profile is sources_profile is manual_profile
    assert not feed_profile.isOffTheRecord()
    assert feed_profile.urlSchemeHandler(b"xfeed-media") is not None
    assert feed_profile.urlSchemeHandler(b"xfeed-media")._repository is repository
    assert feed_profile.persistentStoragePath().startswith(str(tmp_path / "web-profile"))
    assert feed_profile.parent() is application


def test_session_state_and_sidebar_actions_are_wired(tmp_path, monkeypatch, qtbot) -> None:
    patch_runtime(tmp_path, monkeypatch)
    _application, window = create_application([])
    qtbot.addWidget(window)
    session = ApplicationSessionDouble.instances[0]

    session.set_state(SessionState.SIGNED_IN)
    assert "X signed in" in window.connection_status.status_label.text()
    window.connection_status.disconnect_button.click()
    assert session.clear_count == 1

    window.set_session_state(SessionState.SIGNED_OUT)
    window.connection_status.connect_button.click()
    assert len(ApplicationLoginDialogDouble.instances) == 1
    assert ApplicationLoginDialogDouble.instances[0].parent() is window


def test_one_bridge_is_shared_by_session_and_collector(tmp_path, monkeypatch, qtbot) -> None:
    patch_runtime(tmp_path, monkeypatch)
    application, window = create_application([])
    qtbot.addWidget(window)

    bridge = OperaBridgeDouble.instances[0]
    session = ApplicationSessionDouble.instances[0]
    collector = OperaCollectorDouble.instances[0]
    assert bridge.start_count == 1
    assert bridge.data_dir == tmp_path / "web-profile" / "opera-bridge"
    assert bridge.parent() is application
    assert session.bridge is bridge
    assert collector.bridge is bridge


def test_shutdown_stops_scheduler_cancels_drains_persists_then_closes_bridge(
    tmp_path, monkeypatch, qtbot
) -> None:
    events: list[str] = []
    release = threading.Event()
    worker_started = threading.Event()
    real_pool = QThreadPool.globalInstance()
    terminal_callback = {"processed": False}

    class SchedulerDouble:
        def __init__(self, *_args, **_kwargs) -> None:
            self.countdown_changed = _SignalDouble()
            self.status_changed = _SignalDouble()
            self.remaining_seconds = None

        def start(self) -> None:
            events.append("scheduler-start")

        def stop(self) -> None:
            events.append("scheduler-stop")

        def set_interval(self, _value: int) -> None:
            pass

        def set_maximum(self, _value: int) -> None:
            pass

    class CoordinatorDouble:
        def __init__(self, controller, *_args, **_kwargs) -> None:
            self._controller = controller
            self.state_changed = _SignalDouble()
            self.request_completed = _SignalDouble()
            self.queue_finished = _SignalDouble()
            self.session_blocked = _SignalDouble()
            self.session_state_changed = _SignalDouble()
            self.busy = False

        def current_run_ids(self, _kind: CollectionTargetKind) -> tuple[int, ...]:
            return ()

        def cancel(self) -> None:
            events.append("coordinator-cancel")

            def work() -> object:
                worker_started.set()
                assert release.wait(timeout=3)
                return object()

            real_pool.start(Worker(work))

        def collect_feed(self, *_args, **_kwargs) -> bool:
            return True

        def collect_source(self, *_args, **_kwargs) -> bool:
            return True

        def collect_all(self, *_args, **_kwargs) -> bool:
            return True

        def connect_session(self, _parent) -> None:
            pass

        def disconnect_session(self) -> None:
            pass

        def resolve_post_photos(self, _post, _parent) -> bool:
            return False

        def resume_pending_resolutions(self) -> int:
            return 0

    class OrderedBridge(OperaBridgeDouble):
        def close(self) -> None:
            events.append("bridge-close")
            super().close()

    class OrderedSessionRepository(AppSessionRepository):
        instances: list["OrderedSessionRepository"] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.__class__.instances.append(self)

        def close(self, session_id: int):
            events.append("app-session-close")
            return super().close(session_id)

    class OrderedMediaDownloadManager(MediaDownloadManagerDouble):
        def stop(self) -> None:
            events.append("media-download-manager-stop")
            super().stop()

    class OrderedTopicAnalysisManager(TopicAnalysisManagerDouble):
        def stop(self) -> None:
            events.append("topic-manager-stop")
            super().stop()

    class OrderedThreadPool:
        @staticmethod
        def globalInstance():
            class Pool:
                def waitForDone(self) -> bool:
                    result = real_pool.waitForDone()
                    events.append("worker-drain")
                    QTimer.singleShot(
                        0,
                        lambda: terminal_callback.__setitem__("processed", True),
                    )
                    return result

            return Pool()

    patch_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr("xfeed.app.OperaBridge", OrderedBridge)
    monkeypatch.setattr("xfeed.app.CollectionCoordinator", CoordinatorDouble)
    monkeypatch.setattr("xfeed.app.AutoCollectionScheduler", SchedulerDouble)
    monkeypatch.setattr("xfeed.app.TopicAnalysisManager", OrderedTopicAnalysisManager)
    monkeypatch.setattr("xfeed.app.MediaDownloadManager", OrderedMediaDownloadManager)
    monkeypatch.setattr("xfeed.app.QThreadPool", OrderedThreadPool)
    monkeypatch.setattr("xfeed.app.AppSessionRepository", OrderedSessionRepository)
    application, window = create_application([])
    qtbot.addWidget(window)
    sources = window._pages.widget(1)
    assert isinstance(sources, SourcesView)
    save_window_state = window.save_ui_state

    def ordered_settings_save() -> None:
        assert terminal_callback["processed"]
        events.append("settings-save")
        save_window_state()

    window.save_ui_state = ordered_settings_save  # type: ignore[method-assign]

    def release_after_worker_starts() -> None:
        assert worker_started.wait(timeout=2)
        release.set()

    releaser = threading.Thread(target=release_after_worker_starts)
    releaser.start()
    application.aboutToQuit.emit()
    releaser.join(timeout=3)

    assert not releaser.is_alive()
    assert events[-9:] == [
        "scheduler-stop",
        "scheduler-stop",
        "topic-manager-stop",
        "media-download-manager-stop",
        "coordinator-cancel",
        "worker-drain",
        "settings-save",
        "app-session-close",
        "bridge-close",
    ]
    active_session = OrderedSessionRepository.instances[-1]
    persisted_session = active_session.by_id(1)
    assert persisted_session is not None
    assert persisted_session.state.value == "closed"
    assert (tmp_path / "ui.ini").exists()
    assert SingleInstanceGuardDouble.instances[-1].release_count == 1


class _SignalDouble:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


def test_startup_failure_closes_bridge_without_recovering_live_run(tmp_path, monkeypatch) -> None:
    events: list[str] = []

    class OccupiedBridge(OperaBridgeDouble):
        def start(self) -> None:
            events.append("bridge-start")
            self.start_count += 1
            raise OSError("port occupied by live app")

        def close(self) -> None:
            events.append("bridge-close")
            super().close()

    class TrackingSessionRepository(AppSessionRepository):
        instances: list["TrackingSessionRepository"] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.__class__.instances.append(self)

        def begin(self, app_version: str):
            events.append("app-session-begin")
            return super().begin(app_version)

        def close(self, session_id: int):
            events.append("app-session-close")
            return super().close(session_id)

    database_path = tmp_path / "feed.sqlite3"
    persisted = build_services(database_path)
    active = persisted.collections.start(CollectionTarget.for_you())
    patch_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr("xfeed.app.OperaBridge", OccupiedBridge)
    monkeypatch.setattr("xfeed.app.AppSessionRepository", TrackingSessionRepository)

    with pytest.raises(OSError, match="occupied"):
        create_application([])

    bridge = OccupiedBridge.instances[-1]
    assert bridge.close_count == 1
    assert events == [
        "app-session-begin",
        "bridge-start",
        "app-session-close",
        "bridge-close",
    ]
    created = TrackingSessionRepository.instances[-1].by_id(1)
    assert created is not None
    assert created.state.value == "closed"
    latest = persisted.collections.latest_for_target(CollectionTarget.for_you())
    assert latest is not None
    assert latest.id == active.id
    assert latest.status is CollectionStatus.RUNNING
    assert SingleInstanceGuardDouble.instances[-1].release_count == 1


def test_startup_failure_after_media_start_stops_cancels_drains_flushes_then_closes(
    tmp_path, monkeypatch
) -> None:
    events: list[str] = []
    original_failure = RuntimeError("session verification failed")
    cleanup_failure = OSError("bridge close failed")

    class FailingVerificationSession(ApplicationSessionDouble):
        def verify(self) -> None:
            events.append("session-verify")
            raise original_failure

    class OrderedMediaDownloadManager(MediaDownloadManagerDouble):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.running = False

        def start(self) -> None:
            events.append("media-download-manager-start")
            self.running = True
            super().start()

        def stop(self) -> None:
            events.append("media-download-manager-stop")
            self.running = False
            super().stop()

    class OrderedTopicAnalysisManager(TopicAnalysisManagerDouble):
        def stop(self) -> None:
            events.append("topic-manager-stop")
            super().stop()

    class CoordinatorDouble:
        def __init__(self, controller, *_args, **_kwargs) -> None:
            self._controller = controller
            self.state_changed = _SignalDouble()
            self.request_completed = _SignalDouble()
            self.queue_finished = _SignalDouble()
            self.session_blocked = _SignalDouble()
            self.session_state_changed = _SignalDouble()
            self.busy = False

        def current_run_ids(self, _kind: CollectionTargetKind) -> tuple[int, ...]:
            return ()

        def cancel(self) -> None:
            events.append("coordinator-cancel")

        def collect_feed(self, *_args, **_kwargs) -> bool:
            return True

        def collect_source(self, *_args, **_kwargs) -> bool:
            return True

        def collect_all(self, *_args, **_kwargs) -> bool:
            return True

        def connect_session(self, _parent) -> None:
            pass

        def disconnect_session(self) -> None:
            pass

        def resolve_post_photos(self, _post, _parent) -> bool:
            return False

        def resume_pending_resolutions(self) -> int:
            return 0

    class OrderedThreadPool:
        @staticmethod
        def globalInstance():
            class Pool:
                def waitForDone(self) -> bool:
                    events.append("worker-drain")
                    QTimer.singleShot(0, lambda: events.append("terminal-flush"))
                    return True

            return Pool()

    class OrderedBridge(OperaBridgeDouble):
        def close(self) -> None:
            events.append("bridge-close")
            super().close()
            raise cleanup_failure

    class OrderedSessionRepository(AppSessionRepository):
        def close(self, session_id: int):
            events.append("app-session-close")
            return super().close(session_id)

    patch_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr("xfeed.app.OperaXSession", FailingVerificationSession)
    monkeypatch.setattr("xfeed.app.MediaDownloadManager", OrderedMediaDownloadManager)
    monkeypatch.setattr("xfeed.app.TopicAnalysisManager", OrderedTopicAnalysisManager)
    monkeypatch.setattr("xfeed.app.CollectionCoordinator", CoordinatorDouble)
    monkeypatch.setattr("xfeed.app.QThreadPool", OrderedThreadPool)
    monkeypatch.setattr("xfeed.app.OperaBridge", OrderedBridge)
    monkeypatch.setattr("xfeed.app.AppSessionRepository", OrderedSessionRepository)

    with pytest.raises(RuntimeError, match="session verification failed") as caught:
        create_application([])

    assert caught.value is original_failure
    manager = OrderedMediaDownloadManager.instances[-1]
    assert not manager.running
    assert manager.stop_count == 1
    assert events[-9:] == [
        "media-download-manager-start",
        "session-verify",
        "topic-manager-stop",
        "media-download-manager-stop",
        "coordinator-cancel",
        "worker-drain",
        "terminal-flush",
        "app-session-close",
        "bridge-close",
    ]
    assert SingleInstanceGuardDouble.instances[-1].release_count == 1


def test_startup_recovers_abandoned_run_only_after_bridge_acquisition(
    tmp_path, monkeypatch, qtbot
) -> None:
    database_path = tmp_path / "feed.sqlite3"
    persisted = build_services(database_path)
    abandoned = persisted.collections.start(CollectionTarget.for_you())
    patch_runtime(tmp_path, monkeypatch)

    _application, window = create_application([])
    qtbot.addWidget(window)
    feed = window._pages.widget(0)
    assert isinstance(feed, FeedView)
    recovered = feed.collections.latest_for_target(CollectionTarget.for_you())
    assert recovered is not None
    assert recovered.id == abandoned.id
    assert recovered.status is CollectionStatus.FAILED
