import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, unquote_to_bytes, urlencode, urlsplit, urlunsplit


_MALFORMED_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SAFE_MEDIA_IDENTIFIER = re.compile(r"[A-Za-z0-9._~-]+")


class MediaOrigin(StrEnum):
    COLLECTION = "collection"
    MANUAL = "manual"


class MediaResolutionState(StrEnum):
    PENDING = "pending"
    RESOLVING = "resolving"
    RESOLVED = "resolved"
    FAILED = "failed"


class MediaAssetState(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    AVAILABLE = "available"
    FAILED = "failed"


@dataclass(frozen=True)
class PhotoCandidate:
    position: int
    url: str
    alt_text: str | None = None

    def __post_init__(self) -> None:
        if type(self.position) is not int or not 0 <= self.position <= 3:
            raise ValueError("position must be an integer between 0 and 3")


@dataclass(frozen=True)
class MediaResolutionJob:
    id: int
    post_id: int
    canonical_url: str
    origin: MediaOrigin
    state: MediaResolutionState
    manifest_count: int | None
    attempts: int
    diagnostic: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MediaAsset:
    id: int
    post_id: int
    position: int
    source_url: str
    alt_text: str | None
    state: MediaAssetState
    local_path: str | None
    mime_type: str | None
    width: int | None
    height: int | None
    attempts: int
    diagnostic: str | None
    created_at: str
    updated_at: str


def normalize_photo_url(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 2048
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise ValueError("photo URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ValueError("photo URL is invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != "pbs.twimg.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise ValueError("photo URL is invalid")
    normalized_path = _normalize_media_path(parsed.path)
    if _MALFORMED_PERCENT.search(parsed.query):
        raise ValueError("photo URL is invalid")
    try:
        query = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("photo URL is invalid") from error
    names = [name for name, _value in query]
    if len(names) != len(set(names)):
        raise ValueError("photo URL is invalid")
    normalized = [(name, item) for name, item in query if name != "name"]
    normalized.append(("name", "large"))
    return urlunsplit(
        ("https", "pbs.twimg.com", normalized_path, urlencode(sorted(normalized)), "")
    )


def _normalize_media_path(path: str) -> str:
    match = re.fullmatch(r"/media/([^/\\]+)", path)
    if match is None:
        raise ValueError("photo URL is invalid")
    identifier = match.group(1)
    for _attempt in range(8):
        if _MALFORMED_PERCENT.search(identifier):
            raise ValueError("photo URL is invalid")
        try:
            decoded = unquote_to_bytes(identifier).decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError("photo URL is invalid") from error
        if decoded == identifier:
            break
        identifier = decoded
    else:
        raise ValueError("photo URL is invalid")
    if identifier in {".", ".."} or _SAFE_MEDIA_IDENTIFIER.fullmatch(identifier) is None:
        raise ValueError("photo URL is invalid")
    return f"/media/{identifier}"
