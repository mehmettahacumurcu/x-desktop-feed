import os
from pathlib import Path

import pytest
import shiboken6
from PySide6.QtCore import QObject, QUrl
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
    QWebEngineUrlRequestJob,
    QWebEngineUrlScheme,
)

from xfeed.db import Database
from xfeed.domain import Availability, PostDraft
from xfeed.media import MediaAssetState, MediaOrigin, PhotoCandidate
from xfeed.media_repository import MediaRepository
from xfeed.repositories import PostRepository
from xfeed.ui.media_scheme import (
    MediaSchemeHandler,
    register_media_scheme,
    resolve_asset_request,
)


SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807


def available_asset(tmp_path: Path) -> tuple[MediaRepository, Path, int]:
    database = Database(tmp_path / "feed.sqlite3")
    database.migrate()
    post = PostRepository(database).insert(
        PostDraft(
            canonical_url="https://x.com/example/status/42",
            x_post_id="42",
            author_handle="example",
            author_name="Example",
            text="A photo post",
            published_at=None,
            embed_html="<blockquote></blockquote>",
            provider_json="{}",
            source_method="oembed",
            has_media=True,
            availability=Availability.AVAILABLE,
        )
    )
    repository = MediaRepository(database)
    asset = repository.record_manifest(
        post.id,
        post.canonical_url,
        MediaOrigin.COLLECTION,
        (PhotoCandidate(0, "https://pbs.twimg.com/media/photo?format=jpg", "Alt"),),
    )[0]
    assert repository.claim_asset(asset.id) is not None
    media_root = tmp_path / "media"
    local_path = Path("42") / "photo-0.jpg"
    target = media_root / local_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"jpeg")
    available = repository.mark_asset_available(
        asset.id,
        local_path=local_path.as_posix(),
        mime_type="image/jpeg",
        width=640,
        height=480,
    )
    return repository, media_root, available.id


def tamper_asset(repository: MediaRepository, asset_id: int, **fields: str) -> None:
    assignments = ",".join(f"{name}=?" for name in fields)
    with repository.database.transaction() as connection:
        connection.execute(
            f"UPDATE post_media SET {assignments} WHERE id=?",
            (*fields.values(), asset_id),
        )


def test_media_scheme_is_registered_with_secure_local_flags() -> None:
    register_media_scheme()
    scheme = QWebEngineUrlScheme.schemeByName(b"xfeed-media")

    assert scheme.syntax() is QWebEngineUrlScheme.Syntax.Path
    assert scheme.defaultPort() == -1
    assert scheme.flags() == (
        QWebEngineUrlScheme.Flag.SecureScheme
        | QWebEngineUrlScheme.Flag.LocalScheme
        | QWebEngineUrlScheme.Flag.LocalAccessAllowed
    )


class RecordingMediaSchemeHandler(MediaSchemeHandler):
    def __init__(
        self,
        repository: MediaRepository,
        media_root: Path,
        parent: QObject,
    ) -> None:
        super().__init__(repository, media_root, parent)
        self.authorized_urls: list[str] = []

    def requestStarted(self, request: QWebEngineUrlRequestJob) -> None:
        if resolve_asset_request(request.requestUrl(), self._repository, self._media_root):
            self.authorized_urls.append(request.requestUrl().toString())
        super().requestStarted(request)


class RecordingPage(QWebEnginePage):
    def __init__(self, profile: QWebEngineProfile) -> None:
        super().__init__(profile)
        self.navigation_urls: list[str] = []

    def acceptNavigationRequest(
        self,
        url: QUrl,
        navigation_type: QWebEnginePage.NavigationType,
        is_main_frame: bool,
    ) -> bool:
        self.navigation_urls.append(url.toString())
        return super().acceptNavigationRequest(url, navigation_type, is_main_frame)


class RecordingInterceptor(QWebEngineUrlRequestInterceptor):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.request_urls: list[str] = []

    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:
        self.request_urls.append(info.requestUrl().toString())


def test_real_webengine_routes_exact_asset_but_never_authorizes_explicit_port(
    qtbot,
    tmp_path: Path,
) -> None:
    repository, media_root, asset_id = available_asset(tmp_path)
    profile = QWebEngineProfile()
    interceptor = RecordingInterceptor(profile)
    profile.setUrlRequestInterceptor(interceptor)
    handler = RecordingMediaSchemeHandler(repository, media_root, profile)
    profile.installUrlSchemeHandler(b"xfeed-media", handler)
    exact_page = QWebEnginePage(profile)
    alias_page = RecordingPage(profile)
    exact_finished: list[bool] = []
    alias_finished: list[bool] = []
    exact_page.loadFinished.connect(exact_finished.append)
    alias_page.loadFinished.connect(alias_finished.append)

    exact_page.setUrl(QUrl(f"xfeed-media://asset/{asset_id}"))
    qtbot.waitUntil(lambda: bool(exact_finished), timeout=5_000)
    assert handler.authorized_urls == [f"xfeed-media://asset/{asset_id}"]

    handler.authorized_urls.clear()
    alias_page.setUrl(QUrl(f"xfeed-media://asset:0/{asset_id}"))
    qtbot.waitUntil(lambda: bool(alias_finished), timeout=5_000)
    assert interceptor.request_urls[-1] == f"xfeed-media://asset:0/{asset_id}"
    assert alias_page.navigation_urls == [f"xfeed-media://asset:0/{asset_id}"]
    assert handler.authorized_urls == []

    exact_page.deleteLater()
    alias_page.deleteLater()
    profile.deleteLater()


def test_resolver_returns_available_asset_with_stored_mime(tmp_path: Path) -> None:
    repository, media_root, asset_id = available_asset(tmp_path)

    resolved = resolve_asset_request(
        QUrl(f"xfeed-media://asset/{asset_id}"),
        repository,
        media_root,
    )

    assert resolved.mime_type == "image/jpeg"
    assert resolved.path == media_root / "42" / "photo-0.jpg"


@pytest.mark.parametrize(
    "asset_id",
    (
        SQLITE_MAX_INTEGER,
        SQLITE_MAX_INTEGER + 1,
        9_999_999_999_999_999_999,
    ),
)
def test_resolver_never_raises_for_signed_sqlite_id_boundaries(
    tmp_path: Path,
    asset_id: int,
) -> None:
    repository, media_root, _existing_id = available_asset(tmp_path)

    assert (
        resolve_asset_request(
            QUrl(f"xfeed-media://asset/{asset_id}"),
            repository,
            media_root,
        )
        is None
    )


def test_resolver_rejects_above_sqlite_max_before_asset_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from xfeed.ui import media_scheme

    repository, media_root, _asset_id = available_asset(tmp_path)
    looked_up = False

    def unexpected_lookup(*_args) -> None:
        nonlocal looked_up
        looked_up = True
        raise AssertionError("out-of-range ID reached repository lookup")

    monkeypatch.setattr(media_scheme, "_asset_by_id", unexpected_lookup)

    assert (
        resolve_asset_request(
            QUrl(f"xfeed-media://asset/{SQLITE_MAX_INTEGER + 1}"),
            repository,
            media_root,
        )
        is None
    )
    assert looked_up is False


@pytest.mark.parametrize(
    "url",
    (
        "xfeed-media://asset/not-a-number",
        "xfeed-media://asset/-1",
        "xfeed-media://asset/+1",
        "xfeed-media://asset/١",
        "xfeed-media://asset/1?size=large",
        "xfeed-media://asset/1#photo",
        "xfeed-media://other/1",
        "xfeed-media://asset.example/1",
        "xfeed-media://asset:0/1",
        "xfeed-media://asset:7/1",
        "xfeed-media://user@asset/1",
        "https://asset/1",
        "xfeed-media://asset/1/extra",
        "xfeed-media://asset//1",
        "xfeed-media://asset/%2F1",
        "xfeed-media://asset/%2e%2e/1",
        "xfeed-media://asset/1/",
    ),
)
def test_resolver_rejects_every_noncanonical_request(tmp_path: Path, url: str) -> None:
    repository, media_root, _asset_id = available_asset(tmp_path)

    assert resolve_asset_request(QUrl(url), repository, media_root) is None


@pytest.mark.parametrize("state", (MediaAssetState.PENDING, MediaAssetState.FAILED))
def test_resolver_rejects_assets_that_are_not_available(
    tmp_path: Path,
    state: MediaAssetState,
) -> None:
    repository, media_root, asset_id = available_asset(tmp_path)
    tamper_asset(repository, asset_id, state=state.value)

    assert (
        resolve_asset_request(QUrl(f"xfeed-media://asset/{asset_id}"), repository, media_root)
        is None
    )


@pytest.mark.parametrize(
    "local_path",
    (
        "../outside.jpg",
        "42/../../outside.jpg",
        "/outside.jpg",
        "C:/outside.jpg",
        r"42\photo.jpg",
    ),
)
def test_resolver_rejects_unsafe_database_paths(tmp_path: Path, local_path: str) -> None:
    repository, media_root, asset_id = available_asset(tmp_path)
    tamper_asset(repository, asset_id, local_path=local_path)

    assert (
        resolve_asset_request(QUrl(f"xfeed-media://asset/{asset_id}"), repository, media_root)
        is None
    )


def test_resolver_rejects_missing_files_and_unknown_assets(tmp_path: Path) -> None:
    repository, media_root, asset_id = available_asset(tmp_path)
    (media_root / "42" / "photo-0.jpg").unlink()

    assert (
        resolve_asset_request(QUrl(f"xfeed-media://asset/{asset_id}"), repository, media_root)
        is None
    )
    assert resolve_asset_request(QUrl("xfeed-media://asset/999999"), repository, media_root) is None


def test_resolver_rejects_symlinks_and_reparse_points(tmp_path: Path) -> None:
    repository, media_root, asset_id = available_asset(tmp_path)
    target = media_root / "42" / "photo-0.jpg"
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("This environment does not permit creating file symlinks")

    assert target.exists()
    assert (
        resolve_asset_request(QUrl(f"xfeed-media://asset/{asset_id}"), repository, media_root)
        is None
    )
    request = FakeRequestJob(QUrl(f"xfeed-media://asset/{asset_id}"))
    MediaSchemeHandler(repository, media_root).requestStarted(request)  # type: ignore[arg-type]
    assert request.failed is True


def test_resolver_rejects_a_reparse_media_root(tmp_path: Path) -> None:
    repository, media_root, asset_id = available_asset(tmp_path)
    real_root = tmp_path / "real-media"
    media_root.rename(real_root)
    try:
        media_root.symlink_to(real_root, target_is_directory=True)
    except OSError:
        pytest.skip("This environment does not permit creating directory symlinks")

    assert (
        resolve_asset_request(QUrl(f"xfeed-media://asset/{asset_id}"), repository, media_root)
        is None
    )


def test_handler_rejects_an_intermediate_directory_reparse_point(tmp_path: Path) -> None:
    repository, media_root, asset_id = available_asset(tmp_path)
    media_directory = media_root / "42"
    real_directory = media_root / "real-42"
    media_directory.rename(real_directory)
    try:
        media_directory.symlink_to(real_directory, target_is_directory=True)
    except OSError:
        pytest.skip("This environment does not permit creating directory symlinks")
    request = FakeRequestJob(QUrl(f"xfeed-media://asset/{asset_id}"))

    MediaSchemeHandler(repository, media_root).requestStarted(request)  # type: ignore[arg-type]

    assert request.failed is True
    assert request.reply_device is None


def test_handler_rejects_hard_linked_files(tmp_path: Path) -> None:
    repository, media_root, asset_id = available_asset(tmp_path)
    target = media_root / "42" / "photo-0.jpg"
    outside = tmp_path / "outside-hard-link.jpg"
    target.rename(outside)
    try:
        target.hardlink_to(outside)
    except OSError:
        pytest.skip("This environment does not permit creating hard links")
    request = FakeRequestJob(QUrl(f"xfeed-media://asset/{asset_id}"))

    MediaSchemeHandler(repository, media_root).requestStarted(request)  # type: ignore[arg-type]

    assert request.failed is True
    assert request.reply_device is None


def test_handler_revalidates_the_open_handle_after_a_symlink_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from xfeed.ui import media_scheme

    repository, media_root, asset_id = available_asset(tmp_path)
    target = media_root / "42" / "photo-0.jpg"
    outside = tmp_path / "outside-swap.jpg"
    outside.write_bytes(b"outside")
    original_resolver = media_scheme.resolve_asset_request

    def resolve_then_swap(*args, **kwargs):
        resolved = original_resolver(*args, **kwargs)
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError:
            pytest.skip("This environment does not permit creating file symlinks")
        return resolved

    monkeypatch.setattr(media_scheme, "resolve_asset_request", resolve_then_swap)
    request = FakeRequestJob(QUrl(f"xfeed-media://asset/{asset_id}"))

    MediaSchemeHandler(repository, media_root).requestStarted(request)  # type: ignore[arg-type]

    assert request.failed is True
    assert request.reply_device is None


def test_handler_fails_cleanly_when_file_disappears_before_secure_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from xfeed.ui import media_scheme

    repository, media_root, asset_id = available_asset(tmp_path)
    target = media_root / "42" / "photo-0.jpg"
    original_resolver = media_scheme.resolve_asset_request

    def resolve_then_remove(*args, **kwargs):
        resolved = original_resolver(*args, **kwargs)
        target.unlink()
        return resolved

    monkeypatch.setattr(media_scheme, "resolve_asset_request", resolve_then_remove)
    request = FakeRequestJob(QUrl(f"xfeed-media://asset/{asset_id}"))

    MediaSchemeHandler(repository, media_root).requestStarted(request)  # type: ignore[arg-type]

    assert request.failed is True
    assert request.failure_error is QWebEngineUrlRequestJob.Error.RequestFailed
    assert request.reply_device is None


def test_handler_rejects_disallowed_stored_mime(tmp_path: Path) -> None:
    repository, media_root, asset_id = available_asset(tmp_path)
    tamper_asset(repository, asset_id, mime_type="text/html")
    request = FakeRequestJob(QUrl(f"xfeed-media://asset/{asset_id}"))

    MediaSchemeHandler(repository, media_root).requestStarted(request)  # type: ignore[arg-type]

    assert request.failed is True
    assert request.reply_device is None


class FakeRequestJob(QObject):
    def __init__(self, url: QUrl) -> None:
        super().__init__()
        self._url = url
        self.reply_mime: bytes | None = None
        self.reply_device: QObject | None = None
        self.failed = False
        self.failure_error: object | None = None

    def requestUrl(self) -> QUrl:
        return self._url

    def reply(self, mime_type: bytes, device: QObject) -> None:
        self.reply_mime = mime_type
        self.reply_device = device

    def fail(self, error: object) -> None:
        self.failed = True
        self.failure_error = error


@pytest.mark.parametrize(
    "asset_id",
    (
        SQLITE_MAX_INTEGER,
        SQLITE_MAX_INTEGER + 1,
        9_999_999_999_999_999_999,
    ),
)
def test_handler_fails_signed_sqlite_id_boundaries_without_raising(
    tmp_path: Path,
    asset_id: int,
) -> None:
    repository, media_root, _existing_id = available_asset(tmp_path)
    request = FakeRequestJob(QUrl(f"xfeed-media://asset/{asset_id}"))

    MediaSchemeHandler(repository, media_root).requestStarted(request)  # type: ignore[arg-type]

    assert request.failed is True
    assert request.reply_device is None


class ThrowingDatabase:
    def connection(self):
        raise RuntimeError("corrupt repository row")


class ThrowingRepository:
    database = ThrowingDatabase()


def test_handler_contains_repository_decoding_failures(tmp_path: Path) -> None:
    request = FakeRequestJob(QUrl("xfeed-media://asset/1"))

    MediaSchemeHandler(  # type: ignore[arg-type]
        ThrowingRepository(),
        tmp_path,
    ).requestStarted(request)  # type: ignore[arg-type]

    assert request.failed is True
    assert request.failure_error is QWebEngineUrlRequestJob.Error.RequestFailed
    assert request.reply_device is None


def test_posix_secure_open_fails_closed_without_no_follow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from xfeed.ui import media_scheme

    opened = False

    def unexpected_open(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("secure open must not run without O_NOFOLLOW")

    monkeypatch.delattr(media_scheme.os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(media_scheme.os, "open", unexpected_open)

    assert (
        media_scheme._open_validated_posix_file(
            tmp_path / "media" / "42" / "photo.jpg",
            tmp_path / "media",
        )
        is None
    )
    assert opened is False


def test_handler_replies_from_owned_descriptor_and_closes_it_with_request(
    qapp,
    qtbot,
    tmp_path: Path,
) -> None:
    repository, media_root, asset_id = available_asset(tmp_path)
    handler = MediaSchemeHandler(repository, media_root)
    request = FakeRequestJob(QUrl(f"xfeed-media://asset/{asset_id}"))

    handler.requestStarted(request)  # type: ignore[arg-type]

    assert request.reply_mime == b"image/jpeg"
    assert request.reply_device is not None
    assert request.reply_device.parent() is request
    assert request.reply_device.fileName() == ""  # type: ignore[attr-defined]
    descriptor = request.reply_device.handle()  # type: ignore[attr-defined]
    assert descriptor >= 0
    assert request.failed is False

    request.deleteLater()
    qtbot.waitUntil(lambda: not shiboken6.isValid(request.reply_device), timeout=1_000)
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_handler_fails_rejected_requests(tmp_path: Path) -> None:
    repository, media_root, _asset_id = available_asset(tmp_path)
    handler = MediaSchemeHandler(repository, media_root)
    request = FakeRequestJob(QUrl("xfeed-media://asset/not-a-number"))

    handler.requestStarted(request)  # type: ignore[arg-type]

    assert request.failed is True
    assert request.reply_device is None
