from dataclasses import dataclass
import logging
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from collections.abc import Callable
from typing import cast

from platformdirs import user_data_path
from PySide6.QtCore import QThreadPool
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QWidget

from xfeed.analytics import AnalyticsRepository
from xfeed.collection import CollectionService
from xfeed.db import Database
from xfeed.domain import CollectionTargetKind
from xfeed.i18n import set_language, tr
from xfeed.media_download import BoundedPhotoFetcher, PhotoDownloader
from xfeed.media_download_manager import MediaDownloadManager
from xfeed.media_manifest import MediaManifestService
from xfeed.media_repository import MediaRepository
from xfeed.oembed import OEmbedClient, PostMetadataProvider
from xfeed.opera_bridge import OperaBridge
from xfeed.opera_collector import OperaExtensionCollector
from xfeed.opera_media_resolver import OperaMediaResolver
from xfeed.opera_session import OperaXSession
from xfeed.posts import PostService
from xfeed.repositories import CollectionRepository, PostRepository, SourceRepository
from xfeed.session_repository import AppSessionRepository
from xfeed.single_instance import SingleInstanceGuard
from xfeed.topic_analysis import TopicAnalyzer
from xfeed.topic_manager import TopicAnalysisManager
from xfeed.topic_repository import TopicAnalysisRepository
from xfeed.ui.auto_scheduler import AutoCollectionScheduler
from xfeed.ui.collection_coordinator import CollectionCoordinator
from xfeed.ui.collection_controller import CollectionController
from xfeed.ui.export_view import ExportView
from xfeed.ui.feed_view import FeedView
from xfeed.ui.insights_view import InsightsView
from xfeed.ui.main_window import MainWindow
from xfeed.ui.opera_connect_dialog import OperaConnectDialog
from xfeed.ui.settings import UiSettingsStore
from xfeed.ui.sources_view import SourcesView
from xfeed.ui.media_scheme import MediaSchemeHandler, register_media_scheme
from xfeed.ui.theme import apply_application_theme


logger = logging.getLogger(__name__)

APP_VERSION = "0.1.0"


class ApplicationAlreadyRunning(RuntimeError):
    pass


class DataDirectoryMigrationError(RuntimeError):
    pass


def acquire_single_instance() -> SingleInstanceGuard | None:
    return SingleInstanceGuard.acquire()


# Qt requires custom schemes to be registered before QApplication (and any
# WebEngine object) exists.  Importing the composition root is the earliest
# dependable application boundary.
register_media_scheme()


@dataclass(frozen=True)
class ApplicationServices:
    database: Database
    sources: SourceRepository
    posts: PostRepository
    collections: CollectionRepository
    sessions: AppSessionRepository
    topic_repository: TopicAnalysisRepository
    analytics: AnalyticsRepository
    media_repository: MediaRepository
    post_service: PostService
    collection_service: CollectionService
    media_manifest_service: MediaManifestService


def default_database_path() -> Path:
    return user_data_path("XDesktopFeed", appauthor=False) / "feed.sqlite3"


def default_session_data_path() -> Path:
    return user_data_path("XDesktopFeed", appauthor=False) / "web-profile"


def prepare_data_directory(data_dir: Path) -> None:
    """Upgrade the default location before opening files, under the instance guard.

    Copy to a staging directory before publishing so SQLite sidecars, relative
    media paths and pairing state stay together, including redirected Windows
    AppData files. Keep the original as a backup; never merge two libraries.
    Explicit/custom database locations must not migrate the user's default data.
    """
    current = user_data_path("XDesktopFeed", appauthor=False)
    if data_dir != current:
        return
    legacy = user_data_path("XDesktopFeed", "Internship")
    if not current.exists() and legacy != current and legacy.exists():
        current.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".xfeed-migration-", dir=current.parent) as temporary:
            staging = Path(temporary) / "data"
            shutil.copytree(legacy, staging)
            staging.rename(current)
    else:
        current.mkdir(parents=True, exist_ok=True)


def default_extension_path() -> Path:
    return (Path(__file__).resolve().parents[2] / "extension" / "opera-xfeed").resolve()


def default_bridge_data_path() -> Path:
    return default_session_data_path() / "opera-bridge"


def default_ui_settings_path() -> Path:
    return default_database_path().with_name("ui.ini")


def default_media_path() -> Path:
    return default_database_path().with_name("media")


def build_database(path: Path | None = None) -> Database:
    database = Database(path or default_database_path())
    database.migrate()
    return database


def build_services(
    data_dir_or_database_path: Path,
    provider: PostMetadataProvider | None = None,
) -> ApplicationServices:
    database = build_database(data_dir_or_database_path)
    sources = SourceRepository(database)
    posts = PostRepository(database)
    collections = CollectionRepository(database)
    sessions = AppSessionRepository(database)
    topic_repository = TopicAnalysisRepository(database)
    analytics = AnalyticsRepository(database, sessions)
    media_repository = MediaRepository(database)
    post_service = PostService(posts, provider or OEmbedClient())
    media_manifest_service = MediaManifestService(media_repository)
    collection_service = CollectionService(post_service, collections, media_manifest_service)
    return ApplicationServices(
        database=database,
        sources=sources,
        posts=posts,
        collections=collections,
        sessions=sessions,
        topic_repository=topic_repository,
        analytics=analytics,
        media_repository=media_repository,
        post_service=post_service,
        collection_service=collection_service,
        media_manifest_service=media_manifest_service,
    )


def _reader_profile(data_dir: Path, parent: QApplication) -> QWebEngineProfile:
    profile = QWebEngineProfile("xfeed-reader", parent)
    profile.setPersistentStoragePath(str(data_dir / "reader-storage"))
    profile.setCachePath(str(data_dir / "reader-cache"))
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    )
    return profile


def _reader_view_factory(profile: QWebEngineProfile) -> Callable[[], QWidget]:
    def create() -> QWidget:
        view = QWebEngineView()
        view.setPage(QWebEnginePage(profile, view))
        return view

    return create


def create_application(argv: list[str]) -> tuple[QApplication, MainWindow]:
    application = cast(QApplication | None, QApplication.instance()) or QApplication(argv)
    apply_application_theme(application)
    instance_guard = acquire_single_instance()
    if instance_guard is None:
        raise ApplicationAlreadyRunning(tr("X Desktop Feed is already running"))
    services: ApplicationServices | None = None
    coordinator: CollectionCoordinator | None = None
    schedulers: dict[CollectionTargetKind, AutoCollectionScheduler] | None = None
    topic_manager: TopicAnalysisManager | None = None
    media_download_manager_started = False
    active_session_id: int | None = None
    bridge: OperaBridge | None = None
    try:
        database_path = default_database_path()
        try:
            prepare_data_directory(database_path.parent)
        except OSError as error:
            raise DataDirectoryMigrationError(
                "Could not prepare the application data directory. Close any older "
                "X Desktop Feed instance and retry. Existing data was not merged or "
                f"replaced. Details: {error}"
            ) from error
        services = build_services(database_path)
        active_session_id = services.sessions.begin(APP_VERSION).id
        settings = UiSettingsStore(default_ui_settings_path())
        set_language(settings.language())
        bridge = OperaBridge(default_bridge_data_path(), parent=application)
        photo_downloader = PhotoDownloader(default_media_path(), BoundedPhotoFetcher())
        media_download_manager = MediaDownloadManager(
            services.media_repository,
            photo_downloader,
            application,
        )
        bridge.start()
        services.collections.recover_abandoned_runs()
        session = OperaXSession(bridge, default_extension_path(), application)
        media_resolver = OperaMediaResolver(bridge, parent=application)
        collector = OperaExtensionCollector(bridge)
        controller = CollectionController(
            collector,
            services.collection_service,
            services.collections,
            session_id=active_session_id,
        )
        coordinator = CollectionCoordinator(
            controller,
            services.sources,
            session,
            lambda parent: OperaConnectDialog(session, parent),
            application,
            media_repository=services.media_repository,
            media_manifest_service=services.media_manifest_service,
            media_resolver=media_resolver,
        )
        schedulers = {
            kind: AutoCollectionScheduler(
                coordinator,
                settings,
                application,
                target_kind=kind,
            )
            for kind in (
                CollectionTargetKind.FOR_YOU,
                CollectionTargetKind.FOLLOWING,
            )
        }
        reader_profile = _reader_profile(default_session_data_path(), application)
        media_handler = MediaSchemeHandler(
            services.media_repository,
            default_media_path(),
            reader_profile,
        )
        reader_profile.installUrlSchemeHandler(b"xfeed-media", media_handler)
        reader_view_factory = _reader_view_factory(reader_profile)
        feed = FeedView(
            services.posts,
            services.post_service,
            services.collections,
            coordinator=coordinator,
            schedulers=schedulers,
            settings=settings,
            web_factory=reader_view_factory,
            manual_web_factory=reader_view_factory,
            media_repository=services.media_repository,
            asset_changed=media_download_manager.asset_changed,
            media_resolution_requester=coordinator.resolve_post_photos,
        )
        sources = SourcesView(
            services.sources,
            services.posts,
            coordinator,
            settings,
            web_factory=reader_view_factory,
            media_repository=services.media_repository,
            asset_changed=media_download_manager.asset_changed,
        )
        topic_manager = TopicAnalysisManager(
            services.topic_repository,
            TopicAnalyzer(),
            parent=application,
        )
        insights = InsightsView(
            services.analytics,
            services.sessions,
            topic_manager,
            active_session_id,
        )
        export = ExportView(services.database, services.sessions)
        window = MainWindow(
            pages={"My Feed": feed, "Sources": sources, "Insights": insights, "Export": export},
            settings=settings,
        )
        session.state_changed.connect(window.set_session_state)
        window.language_changed.connect(feed.apply_language)
        window.language_changed.connect(sources.apply_language)
        window.language_changed.connect(insights.apply_language)
        window.language_changed.connect(export.apply_language)
        window.connect_requested.connect(lambda: coordinator.connect_session(window))
        window.disconnect_requested.connect(coordinator.disconnect_session)
        services.media_manifest_service.manifest_recorded.connect(media_download_manager.schedule)
        coordinator.request_completed.connect(topic_manager.on_collection_completed)
        coordinator.request_completed.connect(insights.refresh)
        coordinator.request_completed.connect(export.refresh)
        feed.manual_saves.post_saved.connect(topic_manager.on_manual_save_completed)
        feed.manual_saves.post_saved.connect(insights.refresh)
        feed.manual_saves.post_saved.connect(export.refresh)
        topic_manager.completed.connect(insights.refresh)
        window.set_session_state(session.state)
        coordinator.resume_pending_resolutions()
        media_download_manager.start()
        media_download_manager_started = True
        session.verify()
        for scheduler in schedulers.values():
            scheduler.start()
        topic_manager.request_startup_analysis()

        shutdown_started = False

        def shutdown() -> None:
            nonlocal shutdown_started
            if shutdown_started:
                return
            shutdown_started = True
            for scheduler in schedulers.values():
                scheduler.stop()
            topic_manager.stop()
            media_download_manager.stop()
            coordinator.cancel()
            QThreadPool.globalInstance().waitForDone()
            application.processEvents()
            window.save_ui_state()
            sources.save_ui_state()
            try:
                services.sessions.close(active_session_id)
            except Exception:
                logger.exception("Failed to close application session during shutdown")
            try:
                bridge.close()
            finally:
                try:
                    instance_guard.release()
                except Exception:
                    logger.exception("Failed to release the application instance guard")

        application.aboutToQuit.connect(shutdown)
    except Exception:
        if schedulers is not None:
            for scheduler in schedulers.values():
                try:
                    scheduler.stop()
                except Exception:
                    pass
        if topic_manager is not None:
            try:
                topic_manager.stop()
            except Exception:
                pass
        if media_download_manager_started:
            try:
                media_download_manager.stop()
            except Exception:
                pass
        if coordinator is not None:
            try:
                coordinator.cancel()
            except Exception:
                pass
        if media_download_manager_started or coordinator is not None:
            try:
                QThreadPool.globalInstance().waitForDone()
            except Exception:
                pass
            try:
                application.processEvents()
            except Exception:
                pass
        if services is not None and active_session_id is not None:
            try:
                services.sessions.close(active_session_id)
            except Exception:
                logger.exception("Failed to close application session during startup cleanup")
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                logger.exception("Failed to close Opera bridge during startup cleanup")
        try:
            instance_guard.release()
        except Exception:
            logger.exception("Failed to release the application instance guard")
        raise
    return application, window
