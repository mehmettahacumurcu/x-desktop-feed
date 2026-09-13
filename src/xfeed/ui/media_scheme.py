import ctypes
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from PySide6.QtCore import QFile, QFileDevice, QIODevice, QObject, QUrl
from PySide6.QtWebEngineCore import (
    QWebEngineUrlRequestJob,
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
)

from xfeed.media import MediaAsset, MediaAssetState
from xfeed.media_repository import MediaRepository


_SCHEME_NAME = b"xfeed-media"
_ASSET_URL = re.compile(rb"xfeed-media://asset/([0-9]+)")
_ALLOWED_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807


@dataclass(frozen=True)
class ResolvedAsset:
    path: Path
    mime_type: str


def register_media_scheme() -> None:
    if not QWebEngineUrlScheme.schemeByName(_SCHEME_NAME).name().isEmpty():
        return
    scheme = QWebEngineUrlScheme(_SCHEME_NAME)
    # Path syntax is the only Qt WebEngine form that preserves an explicitly
    # supplied authority port for exact validation at the handler boundary.
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Path)
    scheme.setFlags(
        QWebEngineUrlScheme.Flag.SecureScheme
        | QWebEngineUrlScheme.Flag.LocalScheme
        | QWebEngineUrlScheme.Flag.LocalAccessAllowed
    )
    QWebEngineUrlScheme.registerScheme(scheme)


def resolve_asset_request(
    url: QUrl,
    repository: MediaRepository,
    media_root: Path,
) -> ResolvedAsset | None:
    asset_id = _request_asset_id(url)
    if asset_id is None:
        return None
    asset = _asset_by_id(repository, asset_id)
    if (
        asset is None
        or asset.state is not MediaAssetState.AVAILABLE
        or asset.local_path is None
        or asset.mime_type not in _ALLOWED_MIME_TYPES
    ):
        return None
    path = _safe_asset_path(media_root, asset.local_path)
    if path is None:
        return None
    return ResolvedAsset(path=path, mime_type=asset.mime_type)


class MediaSchemeHandler(QWebEngineUrlSchemeHandler):
    def __init__(
        self,
        repository: MediaRepository,
        media_root: Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._media_root = media_root

    def requestStarted(self, request: QWebEngineUrlRequestJob) -> None:
        try:
            resolved = resolve_asset_request(
                request.requestUrl(),
                self._repository,
                self._media_root,
            )
        except Exception:
            request.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
            return
        if resolved is None:
            request.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        descriptor = _open_validated_file(resolved.path, self._media_root)
        if descriptor is None:
            request.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
            return
        device = QFile(request)
        if not device.open(
            descriptor,
            QIODevice.OpenModeFlag.ReadOnly,
            QFileDevice.FileHandleFlag.AutoCloseHandle,
        ):
            os.close(descriptor)
            device.deleteLater()
            request.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
            return
        request.reply(resolved.mime_type.encode("ascii"), device)


def _request_asset_id(url: QUrl) -> int | None:
    if not url.isValid():
        return None
    encoded = url.toEncoded().data()
    match = _ASSET_URL.fullmatch(bytes(encoded))
    if match is None or len(match.group(1)) > 19:
        return None
    try:
        asset_id = int(match.group(1))
    except ValueError:
        return None
    return asset_id if asset_id <= _SQLITE_MAX_INTEGER else None


def _asset_by_id(repository: MediaRepository, asset_id: int) -> MediaAsset | None:
    with repository.database.connection() as connection:
        row = connection.execute(
            "SELECT post_id FROM post_media WHERE id=?",
            (asset_id,),
        ).fetchone()
    if row is None:
        return None
    for asset in repository.assets_for_posts((int(row["post_id"]),)).get(int(row["post_id"]), ()):
        if asset.id == asset_id:
            return asset
    return None


def _safe_asset_path(media_root: Path, local_path: str) -> Path | None:
    if not local_path or "\\" in local_path or ":" in local_path or "\x00" in local_path:
        return None
    relative = PurePosixPath(local_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or "." in relative.parts
        or ".." in relative.parts
    ):
        return None
    root = media_root.absolute()
    target = root.joinpath(*relative.parts)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_target = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved_target.is_relative_to(resolved_root):
        return None
    try:
        root_metadata = os.lstat(root)
    except OSError:
        return None
    root_attributes = getattr(root_metadata, "st_file_attributes", 0)
    if stat.S_ISLNK(root_metadata.st_mode) or root_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return None
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = os.lstat(current)
        except OSError:
            return None
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return None
    if not stat.S_ISREG(metadata.st_mode):
        return None
    return target


def _open_validated_file(path: Path, media_root: Path) -> int | None:
    if os.name == "nt":
        return _open_validated_windows_file(path, media_root)
    return _open_validated_posix_file(path, media_root)


def _open_validated_windows_file(path: Path, media_root: Path) -> int | None:
    import msvcrt
    from ctypes import wintypes

    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    native_handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # FILE_SHARE_READ | WRITE | DELETE
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x08000000,  # OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if native_handle == invalid_handle:
        return None
    try:
        descriptor = msvcrt.open_osfhandle(
            int(native_handle),
            os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT,
        )
    except OSError:
        close_handle(native_handle)
        return None
    if not _windows_descriptor_is_safe(descriptor, path, media_root, kernel32):
        os.close(descriptor)
        return None
    return descriptor


def _windows_descriptor_is_safe(
    descriptor: int,
    path: Path,
    media_root: Path,
    kernel32: Any,
) -> bool:
    import msvcrt
    from ctypes import wintypes

    try:
        metadata = os.fstat(descriptor)
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink > 1
        or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        return False
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32_768)
    length = get_final_path(
        wintypes.HANDLE(msvcrt.get_osfhandle(descriptor)),
        buffer,
        len(buffer),
        0,
    )
    if length == 0 or length >= len(buffer):
        return False
    opened_path = _windows_normalized_path(buffer.value)
    expected_path = _windows_normalized_path(str(path.absolute()))
    root_path = _windows_normalized_path(str(media_root.absolute()))
    return opened_path == expected_path and _is_path_beneath(opened_path, root_path)


def _windows_normalized_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def _is_path_beneath(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _open_validated_posix_file(path: Path, media_root: Path) -> int | None:
    try:
        relative = path.absolute().relative_to(media_root.absolute())
    except ValueError:
        return None
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        return None
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | no_follow | close_on_exec
    file_flags = os.O_RDONLY | no_follow | close_on_exec
    directory_descriptor: int | None = None
    try:
        directory_descriptor = os.open(media_root, directory_flags)
        for part in relative.parts[:-1]:
            next_descriptor = os.open(part, directory_flags, dir_fd=directory_descriptor)
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        descriptor = os.open(relative.parts[-1], file_flags, dir_fd=directory_descriptor)
    except (IndexError, OSError):
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        return None
    os.close(directory_descriptor)
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
        os.close(descriptor)
        return None
    return descriptor
