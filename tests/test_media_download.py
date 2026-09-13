from __future__ import annotations

import errno
import socket
import ssl
import struct
import subprocess
import sys
import zlib
from dataclasses import replace
from email.message import Message
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage, QImageReader

import xfeed.media_download as media_download
from xfeed.media import MediaAsset, MediaAssetState
from xfeed.media_download import (
    BoundedPhotoFetcher,
    DownloadFailure,
    FetchResponse,
    PhotoDownloader,
)


VALID_URL = "https://pbs.twimg.com/media/abc?format=jpg&name=large"


class StaticFetcher:
    def __init__(self, response: FetchResponse) -> None:
        self.response = response
        self.urls: list[str] = []

    def fetch(self, url: str) -> FetchResponse:
        self.urls.append(url)
        return self.response


class FakeSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)


class FakeNetworkResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str = VALID_URL,
        mime_type: str = "image/jpeg",
        read_error: BaseException | None = None,
        status: int = 200,
        location: str | None = None,
    ) -> None:
        self._body = body
        self._offset = 0
        self._read_error = read_error
        self.read_sizes: list[int] = []
        self.closed = False
        self.code = status
        self.msg = "Found" if 300 <= status < 400 else "OK"
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = mime_type
        if location is not None:
            self.headers["Location"] = location
        self.socket = FakeSocket()
        raw = type("Raw", (), {"_sock": self.socket})()
        self.fp = type("Fp", (), {"raw": raw})()

    def __enter__(self) -> FakeNetworkResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
        return None

    def close(self) -> None:
        self.closed = True

    def info(self) -> Message:
        return self.headers

    def geturl(self) -> str:
        return self.url

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if self._read_error is not None:
            raise self._read_error
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeOpener:
    def __init__(
        self,
        response: FakeNetworkResponse | None = None,
        *,
        error: BaseException | None = None,
        redirects: tuple[str, ...] = (),
    ) -> None:
        self.response = response
        self.error = error
        self.redirects = redirects
        self.handlers: tuple[object, ...] = ()
        self.requests: list[Request] = []
        self.timeouts: list[float] = []

    def open(self, request: Request, *, timeout: float) -> FakeNetworkResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        redirect_handler = next(
            handler for handler in self.handlers if isinstance(handler, HTTPRedirectHandler)
        )
        current = request
        for target in self.redirects:
            redirected = redirect_handler.redirect_request(
                current, None, 302, "Found", Message(), target
            )
            assert redirected is not None
            current = redirected
        assert self.response is not None
        return self.response


class ScriptedHttpsHandler(HTTPSHandler):
    def __init__(self, responses: list[FakeNetworkResponse]) -> None:
        self.responses = responses
        self.requests: list[Request] = []

    def https_open(self, request: Request) -> FakeNetworkResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        response.url = request.full_url
        return response


def bounded_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    opener: FakeOpener,
    **kwargs: object,
) -> BoundedPhotoFetcher:
    def fake_build_opener(*handlers: object) -> FakeOpener:
        opener.handlers = handlers
        return opener

    monkeypatch.setattr(media_download, "build_opener", fake_build_opener)
    return BoundedPhotoFetcher(**kwargs)  # type: ignore[arg-type]


def asset(*, source_url: str = VALID_URL, position: int = 0) -> MediaAsset:
    return MediaAsset(
        id=7,
        post_id=42,
        position=position,
        source_url=source_url,
        alt_text=None,
        state=MediaAssetState.DOWNLOADING,
        local_path=None,
        mime_type=None,
        width=None,
        height=None,
        attempts=1,
        diagnostic=None,
        created_at="2026-07-24T00:00:00+00:00",
        updated_at="2026-07-24T00:00:00+00:00",
    )


def encoded_image(
    width: int,
    height: int,
    image_format: str,
    *,
    alpha: bool = False,
    quality: int = -1,
    mono: bool = False,
) -> bytes:
    if mono:
        image = QImage(width, height, QImage.Format.Format_Mono)
        image.fill(0)
    else:
        image_format_value = QImage.Format.Format_ARGB32 if alpha else QImage.Format.Format_RGB32
        image = QImage(width, height, image_format_value)
        image.fill(QColor(20, 80, 140, 100 if alpha else 255))
    assert not image.isNull()
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, image_format, quality)
    buffer.close()
    return bytes(data)


def read_image(path: Path) -> QImage:
    image = QImage(str(path))
    assert not image.isNull()
    return image


def oriented_jpeg() -> bytes:
    original = encoded_image(40, 20, "JPEG", quality=95)
    tiff = (
        b"II\x2a\x00\x08\x00\x00\x00"
        + struct.pack("<H", 1)
        + struct.pack("<HHI", 0x0112, 3, 1)
        + struct.pack("<H", 6)
        + b"\x00\x00"
        + b"\x00\x00\x00\x00"
    )
    payload = b"Exif\x00\x00" + tiff
    return original[:2] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + original[2:]


def animated_webp() -> bytes:
    static = encoded_image(2, 2, "WEBP")
    assert static[:4] == b"RIFF" and static[8:12] == b"WEBP"
    frame_chunk = static[12:]

    def uint24(value: int) -> bytes:
        return value.to_bytes(3, "little")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            kind
            + struct.pack("<I", len(payload))
            + payload
            + (b"\x00" if len(payload) % 2 else b"")
        )

    vp8x = chunk(b"VP8X", b"\x02\x00\x00\x00" + uint24(1) + uint24(1))
    animation = chunk(b"ANIM", b"\x00\x00\x00\x00\x00\x00")
    frame_header = uint24(0) + uint24(0) + uint24(1) + uint24(1) + uint24(100) + b"\x00"
    frame = chunk(b"ANMF", frame_header + frame_chunk)
    payload = b"WEBP" + vp8x + animation + frame + frame
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def animated_png() -> bytes:
    static = encoded_image(2, 2, "PNG")
    chunks: dict[bytes, list[bytes]] = {}
    offset = 8
    while offset < len(static):
        size = struct.unpack(">I", static[offset : offset + 4])[0]
        kind = static[offset + 4 : offset + 8]
        payload = static[offset + 8 : offset + 8 + size]
        chunks.setdefault(kind, []).append(payload)
        offset += 12 + size

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFF_FFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    frame_control = struct.pack(">IIIIIHHBB", 0, 2, 2, 0, 0, 1, 10, 0, 0)
    second_control = struct.pack(">IIIIIHHBB", 1, 2, 2, 0, 0, 1, 10, 0, 0)
    compressed = b"".join(chunks[b"IDAT"])
    return (
        static[:8]
        + chunk(b"IHDR", chunks[b"IHDR"][0])
        + chunk(b"acTL", struct.pack(">II", 2, 0))
        + chunk(b"fcTL", frame_control)
        + chunk(b"IDAT", compressed)
        + chunk(b"fcTL", second_control)
        + chunk(b"fdAT", struct.pack(">I", 2) + compressed)
        + chunk(b"IEND", b"")
    )


def downloader(tmp_path: Path, body: bytes, mime_type: str) -> PhotoDownloader:
    response = FetchResponse(VALID_URL, mime_type, body)
    return PhotoDownloader(tmp_path / "media", StaticFetcher(response))


def assert_failure(error: pytest.ExceptionInfo[DownloadFailure], *, retryable: bool) -> None:
    assert error.value.retryable is retryable
    assert 0 < len(error.value.diagnostic) <= 500


@pytest.mark.parametrize(
    "unsafe_url",
    (
        "http://pbs.twimg.com/media/abc?format=jpg",
        "https://example.com/media/abc?format=jpg",
        "https://user:secret@pbs.twimg.com/media/abc?format=jpg",
        "https://pbs.twimg.com:444/media/abc?format=jpg",
        "https://pbs.twimg.com/media/abc?format=jpg#fragment",
        "https://pbs.twimg.com/not-media/abc?format=jpg",
        "https://pbs.twimg.com/media/../secret?format=jpg",
        "https://pbs.twimg.com/media/%2e%2e/secret?format=jpg",
        "https://pbs.twimg.com/media/abc/extra?format=jpg",
        "https://pbs.twimg.com/media/abc%2f..%2fsecret?format=jpg",
        "https://pbs.twimg.com/media/abc%5c..%5csecret?format=jpg",
        "https://pbs.twimg.com/media/%252e%252e%252fsecret?format=jpg",
        "https://pbs.twimg.com/media/abc%?format=jpg",
    ),
)
def test_downloader_rejects_untrusted_source_urls_before_fetch(
    tmp_path: Path, unsafe_url: str
) -> None:
    fetcher = StaticFetcher(FetchResponse(VALID_URL, "image/jpeg", b"unused"))

    with pytest.raises(DownloadFailure) as error:
        PhotoDownloader(tmp_path, fetcher).download(asset(source_url=unsafe_url))

    assert_failure(error, retryable=False)
    assert fetcher.urls == []


def test_downloader_accepts_and_normalizes_exact_pbs_https_url(tmp_path: Path) -> None:
    fetcher = StaticFetcher(FetchResponse(VALID_URL, "image/jpeg", encoded_image(20, 10, "JPEG")))
    source = "https://pbs.twimg.com/media/abc?name=small&format=jpg"

    PhotoDownloader(tmp_path, fetcher).download(asset(source_url=source))

    assert fetcher.urls == [VALID_URL]


def test_fetcher_rejects_redirect_to_another_host(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = FakeOpener(
        FakeNetworkResponse(b"ok"),
        redirects=("https://example.com/media/abc?format=jpg",),
    )

    with pytest.raises(DownloadFailure) as error:
        bounded_fetcher(monkeypatch, opener).fetch(VALID_URL)

    assert_failure(error, retryable=False)


@pytest.mark.parametrize(
    "target",
    (
        "https://pbs.twimg.com/media/abc%2f..%2fsecret?format=jpg",
        "https://pbs.twimg.com/media/%252e%252e%252fsecret?format=jpg",
        "https://pbs.twimg.com/media/abc%?format=jpg",
    ),
)
def test_fetcher_rejects_smuggled_or_malformed_redirect_targets(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    opener = FakeOpener(FakeNetworkResponse(b"ok"), redirects=(target,))

    with pytest.raises(DownloadFailure) as error:
        bounded_fetcher(monkeypatch, opener).fetch(VALID_URL)

    assert_failure(error, retryable=False)


def test_production_opener_never_reads_and_closes_accepted_redirect_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirect = FakeNetworkResponse(
        b"x" * (16 * 1024 * 1024),
        status=302,
        location="https://pbs.twimg.com/media/final?format=jpg",
        read_error=AssertionError("redirect body must never be read"),
    )
    final = FakeNetworkResponse(b"final", url="https://pbs.twimg.com/media/final?format=jpg")
    transport = ScriptedHttpsHandler([redirect, final])

    monkeypatch.setattr(
        media_download,
        "build_opener",
        lambda *handlers: build_opener(*handlers, transport),
    )

    result = BoundedPhotoFetcher().fetch(VALID_URL)

    assert result.body == b"final"
    assert redirect.read_sizes == []
    assert redirect.closed
    assert final.closed


def test_production_opener_never_reads_and_closes_rejected_redirect_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirect = FakeNetworkResponse(
        b"x" * (16 * 1024 * 1024),
        status=302,
        location="https://example.com/media/secret?format=jpg",
        read_error=AssertionError("rejected redirect body must never be read"),
    )
    transport = ScriptedHttpsHandler([redirect])
    monkeypatch.setattr(
        media_download,
        "build_opener",
        lambda *handlers: build_opener(*handlers, transport),
    )

    with pytest.raises(DownloadFailure) as failure:
        BoundedPhotoFetcher().fetch(VALID_URL)

    assert_failure(failure, retryable=False)
    assert redirect.read_sizes == []
    assert redirect.closed


def test_fetcher_allows_three_redirects_and_rejects_the_fourth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirect_urls = tuple(
        f"https://pbs.twimg.com/media/{index}?format=jpg" for index in range(1, 5)
    )
    three = FakeOpener(FakeNetworkResponse(b"ok"), redirects=redirect_urls[:3])
    four = FakeOpener(FakeNetworkResponse(b"ok"), redirects=redirect_urls)

    assert bounded_fetcher(monkeypatch, three).fetch(VALID_URL).body == b"ok"
    with pytest.raises(DownloadFailure) as error:
        bounded_fetcher(monkeypatch, four).fetch(VALID_URL)

    assert_failure(error, retryable=False)


def test_redirects_do_not_forward_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = FakeOpener(FakeNetworkResponse(b"ok"))
    bounded_fetcher(monkeypatch, opener)
    handler = next(item for item in opener.handlers if isinstance(item, HTTPRedirectHandler))
    request = Request(
        VALID_URL,
        headers={"Authorization": "Bearer secret", "Cookie": "session=secret"},
    )

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        Message(),
        "https://pbs.twimg.com/media/next?format=jpg",
    )

    assert redirected is not None
    assert redirected.get_header("Authorization") is None
    assert redirected.get_header("Cookie") is None


@pytest.mark.parametrize(
    ("error", "diagnostic"),
    (
        (URLError(socket.timeout("connect timed out")), "connect"),
        (TimeoutError("connect timed out"), "connect"),
    ),
)
def test_connect_timeout_is_retryable(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, diagnostic: str
) -> None:
    opener = FakeOpener(error=error)

    with pytest.raises(DownloadFailure) as failure:
        bounded_fetcher(monkeypatch, opener).fetch(VALID_URL)

    assert_failure(failure, retryable=True)
    assert diagnostic in failure.value.diagnostic.lower()
    assert opener.timeouts == [5.0]


def test_read_timeout_is_retryable_and_uses_twenty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeNetworkResponse(b"", read_error=socket.timeout("read timed out"))
    opener = FakeOpener(response)

    with pytest.raises(DownloadFailure) as failure:
        bounded_fetcher(monkeypatch, opener).fetch(VALID_URL)

    assert_failure(failure, retryable=True)
    assert "read" in failure.value.diagnostic.lower()
    assert response.socket.timeouts == [20.0]


@pytest.mark.parametrize(
    "error",
    (
        ConnectionResetError(errno.ECONNRESET, "connection reset"),
        ConnectionRefusedError(errno.ECONNREFUSED, "connection refused"),
        ConnectionAbortedError(errno.ECONNABORTED, "connection aborted"),
        URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure")),
        OSError(errno.ENETUNREACH, "network unreachable"),
        OSError(errno.EHOSTUNREACH, "host unreachable"),
    ),
)
def test_transient_network_failures_are_retryable(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    with pytest.raises(DownloadFailure) as failure:
        bounded_fetcher(monkeypatch, FakeOpener(error=error)).fetch(VALID_URL)

    assert_failure(failure, retryable=True)


@pytest.mark.parametrize(
    "error",
    (
        URLError(socket.gaierror(socket.EAI_NONAME, "name not found")),
        ssl.SSLCertVerificationError(1, "certificate rejected"),
        ssl.SSLError("TLS policy failure"),
        OSError(errno.ENOENT, "not found"),
    ),
)
def test_permanent_network_failures_are_not_retryable(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    with pytest.raises(DownloadFailure) as failure:
        bounded_fetcher(monkeypatch, FakeOpener(error=error)).fetch(VALID_URL)

    assert_failure(failure, retryable=False)


@pytest.mark.parametrize(
    ("status", "retryable"),
    (
        (400, False),
        (404, False),
        (408, True),
        (425, True),
        (429, True),
        (500, True),
        (503, True),
        (599, True),
    ),
)
def test_http_error_status_is_classified_and_response_is_closed(
    monkeypatch: pytest.MonkeyPatch, status: int, retryable: bool
) -> None:
    response = FakeNetworkResponse(b"must not be read", status=status)
    error = HTTPError(VALID_URL, status, "request failed", Message(), response)

    with pytest.raises(DownloadFailure) as failure:
        bounded_fetcher(monkeypatch, FakeOpener(error=error)).fetch(VALID_URL)

    assert_failure(failure, retryable=retryable)
    assert str(status) in failure.value.diagnostic
    assert response.read_sizes == []
    assert response.closed


def test_fetcher_streams_in_64_kib_chunks_and_enforces_compressed_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maximum = 15 * 1024 * 1024
    response = FakeNetworkResponse(b"x" * (maximum + 1))

    with pytest.raises(DownloadFailure) as failure:
        bounded_fetcher(monkeypatch, FakeOpener(response)).fetch(VALID_URL)

    assert_failure(failure, retryable=False)
    assert response.read_sizes
    assert response.read_sizes[:-1]
    assert set(response.read_sizes[:-1]) == {64 * 1024}
    assert response.read_sizes[-1] == 1


@pytest.mark.parametrize("mime_type", ("image/jpeg", "image/png", "image/webp"))
def test_declared_mime_allowlist_accepts_decodable_matching_images(
    tmp_path: Path, mime_type: str
) -> None:
    image_format = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[mime_type]
    body = encoded_image(20, 10, image_format)

    result = downloader(tmp_path, body, mime_type).download(asset())

    assert result.width == 20
    assert result.height == 10


@pytest.mark.parametrize("mime_type", ("text/plain", "image/gif", "application/octet-stream"))
def test_declared_mime_allowlist_rejects_other_types(tmp_path: Path, mime_type: str) -> None:
    with pytest.raises(DownloadFailure) as failure:
        downloader(tmp_path, encoded_image(20, 10, "PNG"), mime_type).download(asset())

    assert_failure(failure, retryable=False)


def test_mime_spoofing_and_random_bytes_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(DownloadFailure) as spoofed:
        downloader(tmp_path, encoded_image(20, 10, "PNG"), "image/jpeg").download(asset())
    with pytest.raises(DownloadFailure) as random:
        downloader(tmp_path, b"not an image", "image/jpeg").download(asset())

    assert_failure(spoofed, retryable=False)
    assert_failure(random, retryable=False)


def test_animated_webp_is_rejected_before_a_frame_is_stored(tmp_path: Path) -> None:
    body = animated_webp()
    data = QByteArray(body)
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    assert reader.supportsAnimation()
    assert reader.imageCount() == 2

    with pytest.raises(DownloadFailure) as failure:
        downloader(tmp_path, body, "image/webp").download(asset())

    assert_failure(failure, retryable=False)
    assert "animated" in failure.value.diagnostic.lower()
    assert not (tmp_path / "media").exists()


def test_animated_png_is_rejected_before_a_frame_is_stored(tmp_path: Path) -> None:
    body = animated_png()

    with pytest.raises(DownloadFailure) as failure:
        downloader(tmp_path, body, "image/png").download(asset())

    assert_failure(failure, retryable=False)
    assert "animated" in failure.value.diagnostic.lower()
    assert not (tmp_path / "media").exists()


def test_dimensions_above_twenty_thousand_are_rejected_before_decode(
    tmp_path: Path,
) -> None:
    body = encoded_image(20_001, 1, "PNG", mono=True)

    with pytest.raises(DownloadFailure) as failure:
        downloader(tmp_path, body, "image/png").download(asset())

    assert_failure(failure, retryable=False)


def test_more_than_fifty_million_decoded_pixels_are_rejected(tmp_path: Path) -> None:
    body = encoded_image(10_000, 5_001, "PNG", mono=True)

    with pytest.raises(DownloadFailure) as failure:
        downloader(tmp_path, body, "image/png").download(asset())

    assert_failure(failure, retryable=False)


def test_image_at_or_below_1600_pixels_is_not_resized(tmp_path: Path) -> None:
    result = downloader(
        tmp_path, encoded_image(1600, 900, "JPEG", quality=95), "image/jpeg"
    ).download(asset())

    assert (result.width, result.height) == (1600, 900)
    assert read_image(tmp_path / "media" / result.relative_path).size().toTuple() == (1600, 900)


def test_image_above_1600_pixels_is_resized_proportionally(tmp_path: Path) -> None:
    result = downloader(
        tmp_path, encoded_image(2000, 1000, "JPEG", quality=95), "image/jpeg"
    ).download(asset())

    assert (result.width, result.height) == (1600, 800)
    assert read_image(tmp_path / "media" / result.relative_path).size().toTuple() == (1600, 800)


def test_opaque_output_is_jpeg_at_quality_88(tmp_path: Path) -> None:
    source = encoded_image(64, 32, "PNG")
    source_image = QImage.fromData(source)
    expected = QByteArray()
    buffer = QBuffer(expected)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert source_image.save(buffer, "JPEG", 88)
    buffer.close()

    result = downloader(tmp_path, source, "image/png").download(asset())
    output = tmp_path / "media" / result.relative_path

    assert result.mime_type == "image/jpeg"
    assert result.relative_path == "42/photo-0.jpg"
    assert output.read_bytes() == bytes(expected)


def test_alpha_output_is_png(tmp_path: Path) -> None:
    result = downloader(tmp_path, encoded_image(32, 16, "PNG", alpha=True), "image/png").download(
        asset(position=2)
    )
    output = tmp_path / "media" / result.relative_path

    assert result.mime_type == "image/png"
    assert result.relative_path == "42/photo-2.png"
    assert QImageReader.imageFormat(str(output)).data().lower() == b"png"
    assert read_image(output).hasAlphaChannel()


def test_exif_orientation_is_applied_before_output(tmp_path: Path) -> None:
    result = downloader(tmp_path, oriented_jpeg(), "image/jpeg").download(asset())

    assert (result.width, result.height) == (20, 40)


def test_output_replaces_existing_file_atomically_and_leaves_no_part_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "media"
    destination = root / "42" / "photo-0.jpg"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old")

    result = downloader(tmp_path, encoded_image(20, 10, "JPEG"), "image/jpeg").download(asset())

    assert (root / result.relative_path).read_bytes() != b"old"
    assert not list(destination.parent.glob("*.part"))


def test_part_file_is_cleaned_when_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(media_download.os, "replace", fail_replace)

    with pytest.raises(DownloadFailure) as failure:
        downloader(tmp_path, encoded_image(20, 10, "JPEG"), "image/jpeg").download(asset())

    assert_failure(failure, retryable=False)
    assert not list((tmp_path / "media").rglob("*.part"))


def test_output_path_cannot_escape_configured_media_root(tmp_path: Path) -> None:
    unsafe_asset = replace(asset(), post_id=cast(int, "../escape"))
    service = downloader(tmp_path, encoded_image(20, 10, "JPEG"), "image/jpeg")

    with pytest.raises(DownloadFailure) as failure:
        service.download(unsafe_asset)

    assert_failure(failure, retryable=False)
    assert not (tmp_path / "escape").exists()


def test_numeric_post_symlink_to_external_directory_is_rejected_without_write(
    tmp_path: Path,
) -> None:
    media_root = tmp_path / "media"
    outside = tmp_path / "outside"
    media_root.mkdir()
    outside.mkdir()
    (media_root / "42").symlink_to(outside, target_is_directory=True)
    service = PhotoDownloader(
        media_root,
        StaticFetcher(FetchResponse(VALID_URL, "image/jpeg", encoded_image(20, 10, "JPEG"))),
    )

    with pytest.raises(DownloadFailure) as failure:
        service.download(asset())

    assert_failure(failure, retryable=False)
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction coverage")
def test_numeric_post_junction_to_external_directory_is_rejected_without_write(
    tmp_path: Path,
) -> None:
    media_root = tmp_path / "media"
    outside = tmp_path / "outside"
    media_root.mkdir()
    outside.mkdir()
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(media_root / "42"), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    service = PhotoDownloader(
        media_root,
        StaticFetcher(FetchResponse(VALID_URL, "image/jpeg", encoded_image(20, 10, "JPEG"))),
    )

    with pytest.raises(DownloadFailure) as failure:
        service.download(asset())

    assert_failure(failure, retryable=False)
    assert list(outside.iterdir()) == []


def test_failure_diagnostics_are_capped_at_500_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = FakeOpener(error=OSError("x" * 1_000))

    with pytest.raises(DownloadFailure) as failure:
        bounded_fetcher(monkeypatch, opener).fetch(VALID_URL)

    assert len(failure.value.diagnostic) == 500
