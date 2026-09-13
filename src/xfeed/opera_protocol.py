from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from xfeed.collection import CandidateObservation, DiscoveryReason
from xfeed.domain import CollectionTargetKind
from xfeed.media import PhotoCandidate, normalize_photo_url
from xfeed.urls import InvalidPostUrl, canonicalize_post_url


BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 47831
PROTOCOL_VERSION = 4
HOME_TARGET_KINDS = frozenset({"for_you", "following"})
MAXIMUM_CANDIDATES = 50

_MAXIMUM_URL_LENGTH = 2_048
_MAXIMUM_POST_ID_LENGTH = 128
_MAXIMUM_HANDLE_LENGTH = 64
_MAXIMUM_JOB_ID_LENGTH = 128
_MAXIMUM_DIAGNOSTIC_LENGTH = 500
_MAXIMUM_ALT_TEXT_LENGTH = 1_000
_TERMINAL_REASONS = frozenset(DiscoveryReason)


class ExtensionXState(StrEnum):
    SIGNED_OUT = "signed_out"
    SIGNED_IN = "signed_in"
    CHALLENGE = "challenge"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class OperaCollectionJob:
    id: str
    target_kind: CollectionTargetKind
    handle: str | None
    profile_url: str
    maximum: int
    deadline_at: float


OperaJob = OperaCollectionJob


@dataclass(frozen=True)
class OperaPhotoJob:
    id: str
    post_id: str
    post_url: str
    deadline_at: float


OperaJobUnion = OperaCollectionJob | OperaPhotoJob


@dataclass(frozen=True)
class OperaJobResult:
    job_id: str
    reason: DiscoveryReason
    diagnostic: str | None = None


@dataclass(frozen=True)
class OperaPhotoResolutionResult:
    job_id: str
    reason: DiscoveryReason
    photos: tuple[PhotoCandidate, ...]
    diagnostic: str | None = None


class ProtocolError(ValueError):
    pass


def parse_heartbeat(payload: object) -> ExtensionXState:
    message = _parse_message(payload, {"protocol_version", "x_state"})
    _require_protocol_version(message["protocol_version"])
    try:
        return ExtensionXState(_require_string(message["x_state"], "x_state", 32))
    except ValueError as error:
        raise ProtocolError("x_state is invalid") from error


def parse_observations(payload: object) -> tuple[CandidateObservation, ...]:
    message = _parse_message(payload, {"protocol_version", "observations"})
    _require_protocol_version(message["protocol_version"])
    observations = message["observations"]
    if type(observations) is not list:
        raise ProtocolError("observations must be a list")
    if len(observations) > MAXIMUM_CANDIDATES:
        raise ProtocolError("too many observations")

    return tuple(_parse_observation(observation) for observation in observations)


def parse_job_completion(
    job: str | OperaJobUnion,
    payload: object,
) -> OperaJobResult | OperaPhotoResolutionResult:
    photo_job = isinstance(job, OperaPhotoJob)
    job_id = job.id if isinstance(job, OperaCollectionJob | OperaPhotoJob) else job
    valid_job_id = _require_string(job_id, "job_id", _MAXIMUM_JOB_ID_LENGTH)
    fields = {"protocol_version", "reason", "diagnostic"}
    if photo_job:
        fields.add("photos")
    message = _parse_message(payload, fields)
    _require_protocol_version(message["protocol_version"])
    try:
        reason = DiscoveryReason(_require_string(message["reason"], "reason", 32))
    except ValueError as error:
        raise ProtocolError("reason is invalid") from error
    if reason not in _TERMINAL_REASONS:
        raise ProtocolError("reason is not terminal")

    diagnostic = message["diagnostic"]
    if diagnostic is not None:
        diagnostic = _require_string(diagnostic, "diagnostic", _MAXIMUM_DIAGNOSTIC_LENGTH)
    if photo_job:
        return OperaPhotoResolutionResult(
            valid_job_id,
            reason,
            _parse_photos(message["photos"]),
            diagnostic,
        )
    return OperaJobResult(valid_job_id, reason, diagnostic)


def _parse_message(payload: object, allowed_keys: set[str]) -> dict[str, object]:
    if type(payload) is not dict:
        raise ProtocolError("message must be an object")
    if set(payload) != allowed_keys:
        raise ProtocolError("message fields are invalid")
    return cast(dict[str, object], payload)


def _require_protocol_version(value: object) -> None:
    if type(value) is not int or value != PROTOCOL_VERSION:
        raise ProtocolError("protocol_version is invalid")


def _parse_observation(payload: object) -> CandidateObservation:
    observation = _parse_message(
        payload,
        {
            "url",
            "post_id",
            "author_handle",
            "is_pinned",
            "is_reply",
            "is_repost",
            "is_quote",
            "is_promoted",
            "parent_url",
            "discovery_order",
            "photos",
        },
    )
    return CandidateObservation(
        url=_require_string(observation["url"], "url", _MAXIMUM_URL_LENGTH),
        post_id=_require_string(observation["post_id"], "post_id", _MAXIMUM_POST_ID_LENGTH),
        author_handle=_require_string(
            observation["author_handle"], "author_handle", _MAXIMUM_HANDLE_LENGTH
        ),
        is_pinned=_require_bool(observation["is_pinned"], "is_pinned"),
        is_reply=_require_bool(observation["is_reply"], "is_reply"),
        is_repost=_require_bool(observation["is_repost"], "is_repost"),
        is_quote=_require_bool(observation["is_quote"], "is_quote"),
        is_promoted=_require_bool(observation["is_promoted"], "is_promoted"),
        parent_url=_parse_parent_url(observation["parent_url"]),
        discovery_order=_require_nonnegative_int(observation["discovery_order"], "discovery_order"),
        photos=_parse_photos(observation["photos"]),
    )


def _parse_photos(value: object) -> tuple[PhotoCandidate, ...]:
    if type(value) is not list:
        raise ProtocolError("photos must be a list")
    if len(value) > 4:
        raise ProtocolError("too many photos")

    photos: list[PhotoCandidate] = []
    seen_urls: set[str] = set()
    for index, value in enumerate(value):
        photo = _parse_message(value, {"position", "url", "alt_text"})
        position = _require_nonnegative_int(photo["position"], "photo position")
        if position != index:
            raise ProtocolError("photo position is invalid")
        try:
            url = normalize_photo_url(
                _require_string(photo["url"], "photo url", _MAXIMUM_URL_LENGTH)
            )
        except ValueError as error:
            raise ProtocolError("photo url is invalid") from error
        if url in seen_urls:
            raise ProtocolError("photo URLs must be unique")
        seen_urls.add(url)

        alt_text = photo["alt_text"]
        if alt_text is not None:
            alt_text = _require_string(alt_text, "photo alt_text", _MAXIMUM_ALT_TEXT_LENGTH)
        photos.append(PhotoCandidate(position=position, url=url, alt_text=alt_text))
    return tuple(photos)


def _parse_parent_url(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value.strip() or len(value) > _MAXIMUM_URL_LENGTH:
        return None
    parent_url = value
    try:
        canonical = canonicalize_post_url(parent_url)
    except InvalidPostUrl:
        return None
    if parent_url != canonical.url:
        return None
    return parent_url


def _require_string(value: object, name: str, maximum_length: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum_length:
        raise ProtocolError(f"{name} is invalid")
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ProtocolError(f"{name} must be a boolean")
    return value


def _require_nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ProtocolError(f"{name} is invalid")
    return value
