from __future__ import annotations

import errno
import os
import socket
import ssl
import stat
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage, QImageReader

from xfeed.media import MediaAsset, normalize_photo_url

_CHUNK_SIZE = 64 * 1024
_MAXIMUM_DIMENSION = 20_000
_MAXIMUM_PIXELS = 50_000_000
_ALLOWED_MIME_FORMATS = {
    "image/jpeg": {b"jpeg", b"jpg"},
    "image/png": {b"png"},
    "image/webp": {b"webp"},
}
_TRANSIENT_NETWORK_ERRNOS = frozenset(
    value
    for name in (
        "ECONNABORTED",
        "ECONNREFUSED",
        "ECONNRESET",
        "EHOSTUNREACH",
        "ENETUNREACH",
        "ETIMEDOUT",
        "WSAECONNABORTED",
        "WSAECONNREFUSED",
        "WSAECONNRESET",
        "WSAEHOSTUNREACH",
        "WSAENETUNREACH",
        "WSAETIMEDOUT",
    )
    if (value := getattr(errno, name, None)) is not None
)


class DownloadFailure(Exception):
    def __init__(self, retryable: bool, diagnostic: str) -> None:
        self.retryable = retryable
        self.diagnostic = _bounded_diagnostic(diagnostic)
        super().__init__(self.diagnostic)


class DownloadSuperseded(DownloadFailure):
    def __init__(self) -> None:
        super().__init__(False, "photo download was superseded by a newer manifest")


@dataclass(frozen=True)
class FetchResponse:
    final_url: str
    mime_type: str
    body: bytes


class PhotoFetcher(Protocol):
    def fetch(self, url: str) -> FetchResponse: ...


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, maximum_redirects: int, connect_timeout_s: float = 5.0) -> None:
        super().__init__()
        self.maximum_redirects = maximum_redirects
        self.connect_timeout_s = connect_timeout_s

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        try:
            normalized_url = normalize_photo_url(newurl)
        except ValueError as error:
            raise DownloadFailure(False, "redirect target is not an allowed photo URL") from error
        redirect_count = int(getattr(req, "_xfeed_redirect_count", 0)) + 1
        if redirect_count > self.maximum_redirects:
            raise DownloadFailure(False, "photo response exceeded the redirect limit")
        redirected = Request(
            normalized_url,
            headers={
                "Accept": "image/jpeg,image/png,image/webp",
                "User-Agent": "xfeed-desktop/0.1",
            },
            method="GET",
        )
        setattr(redirected, "_xfeed_redirect_count", redirect_count)
        return redirected

    def http_error_302(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
    ) -> Any:
        try:
            location = headers.get("Location") or headers.get("URI")
            if not isinstance(location, str) or not location:
                raise DownloadFailure(False, "photo redirect has no valid target")
            redirected = self.redirect_request(req, fp, code, msg, headers, location)
            if redirected is None:
                raise DownloadFailure(False, "photo redirect method is not allowed")
        finally:
            try:
                fp.close()
            except OSError:
                pass
        timeout = float(getattr(req, "timeout", self.connect_timeout_s))
        return self.parent.open(redirected, timeout=timeout)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


class BoundedPhotoFetcher:
    def __init__(
        self,
        *,
        connect_timeout_s: float = 5.0,
        read_timeout_s: float = 20.0,
        maximum_bytes: int = 15 * 1024 * 1024,
        maximum_redirects: int = 3,
    ) -> None:
        if connect_timeout_s <= 0 or read_timeout_s <= 0:
            raise ValueError("timeouts must be positive")
        if maximum_bytes <= 0:
            raise ValueError("maximum_bytes must be positive")
        if maximum_redirects < 0:
            raise ValueError("maximum_redirects cannot be negative")
        self.connect_timeout_s = connect_timeout_s
        self.read_timeout_s = read_timeout_s
        self.maximum_bytes = maximum_bytes
        self.maximum_redirects = maximum_redirects
        self._opener = build_opener(_SafeRedirectHandler(maximum_redirects, connect_timeout_s))

    def fetch(self, url: str) -> FetchResponse:
        try:
            normalized_url = normalize_photo_url(url)
        except ValueError as error:
            raise DownloadFailure(False, "source is not an allowed photo URL") from error
        request = Request(
            normalized_url,
            headers={
                "Accept": "image/jpeg,image/png,image/webp",
                "User-Agent": "xfeed-desktop/0.1",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self.connect_timeout_s) as response:
                _set_read_timeout(response, self.read_timeout_s)
                final_url = _normalize_final_url(response.geturl())
                mime_type = response.headers.get_content_type().lower()
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as error:
                        raise DownloadFailure(False, "invalid photo content length") from error
                    if declared_length < 0 or declared_length > self.maximum_bytes:
                        raise DownloadFailure(False, "photo response exceeded the size limit")
                body = self._read_bounded(response)
        except DownloadFailure:
            raise
        except HTTPError as error:
            retryable = error.code in {408, 425, 429} or 500 <= error.code <= 599
            try:
                error.close()
            except OSError:
                pass
            raise DownloadFailure(retryable, f"photo request returned HTTP {error.code}") from error
        except (socket.timeout, TimeoutError) as error:
            raise DownloadFailure(True, f"photo request timed out: {error}") from error
        except URLError as error:
            reason = error.reason
            retryable = _is_retryable_network_error(reason)
            if retryable and isinstance(reason, (socket.timeout, TimeoutError)):
                diagnostic = f"photo request timed out: {reason}"
            else:
                diagnostic = f"photo request failed: {reason}"
            raise DownloadFailure(retryable, diagnostic) from error
        except OSError as error:
            raise DownloadFailure(
                _is_retryable_network_error(error),
                f"photo request failed: {error}",
            ) from error
        return FetchResponse(final_url, mime_type, body)

    def _read_bounded(self, response: Any) -> bytes:
        body = bytearray()
        while True:
            remaining = self.maximum_bytes - len(body)
            chunk = response.read(min(_CHUNK_SIZE, remaining + 1))
            if not chunk:
                return bytes(body)
            if len(chunk) > remaining:
                raise DownloadFailure(False, "photo response exceeded the size limit")
            body.extend(chunk)


@dataclass(frozen=True)
class DownloadedPhoto:
    relative_path: str
    mime_type: str
    width: int
    height: int


class PhotoDownloader:
    def __init__(
        self,
        media_root: Path,
        fetcher: PhotoFetcher,
        *,
        maximum_edge: int = 1600,
    ) -> None:
        if type(maximum_edge) is not int or maximum_edge <= 0:
            raise ValueError("maximum_edge must be a positive integer")
        configured_root = media_root.absolute()
        if _is_link_or_reparse_point(configured_root):
            raise DownloadFailure(False, "media root cannot be a filesystem link")
        self.media_root = configured_root.resolve()
        self.fetcher = fetcher
        self.maximum_edge = maximum_edge

    def download(
        self,
        asset: MediaAsset,
        *,
        publication_guard: Callable[[MediaAsset], AbstractContextManager[bool]] | None = None,
    ) -> DownloadedPhoto:
        source_url = _validated_photo_url(asset.source_url, "source is not an allowed photo URL")
        _validate_asset_path_fields(asset)
        try:
            response = self.fetcher.fetch(source_url)
        except DownloadFailure:
            raise
        except (socket.timeout, TimeoutError) as error:
            raise DownloadFailure(True, f"photo request timed out: {error}") from error
        except Exception as error:
            raise DownloadFailure(
                _is_retryable_network_error(error),
                f"photo request failed: {error}",
            ) from error
        _validated_photo_url(response.final_url, "final response URL is not allowed")
        mime_type = response.mime_type.partition(";")[0].strip().lower()
        if mime_type not in _ALLOWED_MIME_FORMATS:
            raise DownloadFailure(False, "photo response has an unsupported MIME type")
        image = _decode_image(response.body, mime_type)
        image = _scale_image(image, self.maximum_edge)
        has_alpha = image.hasAlphaChannel()
        extension = "png" if has_alpha else "jpg"
        output_mime = "image/png" if has_alpha else "image/jpeg"
        encoded = _encode_image(image, extension)
        destination = self._safe_destination(asset.post_id, asset.position, extension)
        if publication_guard is None:
            self._store_atomically(destination, encoded)
        else:
            with publication_guard(asset) as current:
                if not current:
                    raise DownloadSuperseded
                self._store_atomically(destination, encoded)
        relative_path = destination.relative_to(self.media_root).as_posix()
        return DownloadedPhoto(relative_path, output_mime, image.width(), image.height())

    def _safe_destination(self, post_id: int, position: int, extension: str) -> Path:
        destination = self.media_root / str(post_id) / f"photo-{position}.{extension}"
        resolved = destination.resolve()
        if (
            not resolved.is_relative_to(self.media_root)
            or _is_link_or_reparse_point(destination.parent)
            or _is_link_or_reparse_point(destination)
        ):
            raise DownloadFailure(False, "photo output path escapes the media root")
        return destination

    def _store_atomically(self, destination: Path, body: bytes) -> None:
        temporary_path: Path | None = None
        try:
            self.media_root.mkdir(parents=True, exist_ok=True)
            _validate_storage_directory(self.media_root, self.media_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _validate_storage_directory(self.media_root, destination.parent)
            _validate_storage_file(self.media_root, destination, allow_missing=True)
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".part",
                dir=destination.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(file_descriptor, "wb") as temporary:
                _validate_storage_directory(self.media_root, destination.parent)
                _validate_storage_file(self.media_root, temporary_path, allow_missing=False)
                temporary.write(body)
                temporary.flush()
                os.fsync(temporary.fileno())
            _validate_storage_directory(self.media_root, destination.parent)
            _validate_storage_file(self.media_root, destination, allow_missing=True)
            _validate_storage_file(self.media_root, temporary_path, allow_missing=False)
            os.replace(temporary_path, destination)
            temporary_path = None
            _validate_storage_file(self.media_root, destination, allow_missing=False)
        except DownloadFailure:
            raise
        except OSError as error:
            raise DownloadFailure(False, f"could not store photo: {error}") from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _bounded_diagnostic(value: str) -> str:
    diagnostic = str(value).replace("\x00", "").strip()
    return (diagnostic or "photo download failed")[:500]


def _is_retryable_network_error(error: object) -> bool:
    if isinstance(error, (socket.timeout, TimeoutError)):
        return True
    if isinstance(error, ssl.SSLError):
        return False
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    return isinstance(error, OSError) and error.errno in _TRANSIENT_NETWORK_ERRNOS


def _validated_photo_url(value: str, diagnostic: str) -> str:
    try:
        return normalize_photo_url(value)
    except ValueError as error:
        raise DownloadFailure(False, diagnostic) from error


def _normalize_final_url(value: str) -> str:
    return _validated_photo_url(value, "final response URL is not allowed")


def _set_read_timeout(response: Any, timeout: float) -> None:
    try:
        response.fp.raw._sock.settimeout(timeout)
    except (AttributeError, OSError) as error:
        raise DownloadFailure(True, "could not configure the photo read timeout") from error


def _validate_asset_path_fields(asset: MediaAsset) -> None:
    if type(asset.post_id) is not int or asset.post_id < 0:
        raise DownloadFailure(False, "photo asset has an unsafe post identifier")
    if type(asset.position) is not int or not 0 <= asset.position <= 3:
        raise DownloadFailure(False, "photo asset has an unsafe position")


def _decode_image(body: bytes, mime_type: str) -> QImage:
    data = QByteArray(body)
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        raise DownloadFailure(False, "could not open the photo response for decoding")
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    size = reader.size()
    image_format = bytes(reader.format().data()).lower()
    if (
        not size.isValid()
        or size.width() > _MAXIMUM_DIMENSION
        or size.height() > _MAXIMUM_DIMENSION
        or size.width() * size.height() > _MAXIMUM_PIXELS
    ):
        raise DownloadFailure(False, "photo dimensions exceed the decode limit")
    if image_format not in _ALLOWED_MIME_FORMATS[mime_type]:
        raise DownloadFailure(False, "photo bytes do not match the declared MIME type")
    if (
        reader.supportsAnimation()
        or reader.imageCount() > 1
        or (mime_type == "image/png" and _png_declares_animation(body))
    ):
        raise DownloadFailure(False, "animated photos are not supported")
    image = reader.read()
    if image.isNull():
        diagnostic = reader.errorString() or "unknown decoder error"
        raise DownloadFailure(False, f"photo could not be decoded: {diagnostic}")
    if (
        image.width() > _MAXIMUM_DIMENSION
        or image.height() > _MAXIMUM_DIMENSION
        or image.width() * image.height() > _MAXIMUM_PIXELS
    ):
        raise DownloadFailure(False, "decoded photo dimensions exceed the limit")
    return image


def _scale_image(image: QImage, maximum_edge: int) -> QImage:
    if max(image.width(), image.height()) <= maximum_edge:
        return image
    return image.scaled(
        maximum_edge,
        maximum_edge,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _encode_image(image: QImage, extension: str) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise DownloadFailure(False, "could not open a photo output buffer")
    image_format = "PNG" if extension == "png" else "JPEG"
    quality = -1 if extension == "png" else 88
    # PySide6's runtime accepts str here although its generated stub only declares bytes.
    if not image.save(buffer, image_format, quality):  # type: ignore[call-overload]
        raise DownloadFailure(False, f"could not encode photo as {image_format}")
    buffer.close()
    return bytes(data.data())


def _png_declares_animation(body: bytes) -> bool:
    if not body.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    while offset + 12 <= len(body):
        chunk_length = int.from_bytes(body[offset : offset + 4], "big")
        chunk_end = offset + 12 + chunk_length
        if chunk_end > len(body):
            return False
        chunk_type = body[offset + 4 : offset + 8]
        if chunk_type == b"acTL":
            return True
        if chunk_type in {b"IDAT", b"IEND"}:
            return False
        offset = chunk_end
    return False


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(file_attributes & reparse_flag)


def _validate_storage_directory(media_root: Path, directory: Path) -> None:
    if _is_link_or_reparse_point(directory):
        raise DownloadFailure(False, "photo output directory is a filesystem link")
    try:
        resolved = directory.resolve(strict=True)
    except OSError as error:
        raise DownloadFailure(False, f"photo output directory is unsafe: {error}") from error
    if not resolved.is_relative_to(media_root) or not resolved.is_dir():
        raise DownloadFailure(False, "photo output directory escapes the media root")


def _validate_storage_file(
    media_root: Path,
    path: Path,
    *,
    allow_missing: bool,
) -> None:
    if _is_link_or_reparse_point(path):
        raise DownloadFailure(False, "photo output file is a filesystem link")
    if not path.exists():
        if allow_missing and path.parent.resolve(strict=True).is_relative_to(media_root):
            return
        raise DownloadFailure(False, "photo output file is missing or unsafe")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise DownloadFailure(False, f"photo output file is unsafe: {error}") from error
    if not resolved.is_relative_to(media_root) or not resolved.is_file():
        raise DownloadFailure(False, "photo output file escapes the media root")
