import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from xfeed.collection import CandidateObservation, DiscoveryReason
from xfeed.domain import CollectionTargetKind
from xfeed.media import PhotoCandidate
from xfeed.opera_protocol import (
    BRIDGE_HOST,
    BRIDGE_PORT,
    PROTOCOL_VERSION,
    ExtensionXState,
    OperaCollectionJob,
    OperaJobResult,
    OperaPhotoJob,
    OperaPhotoResolutionResult,
    ProtocolError,
    parse_heartbeat,
    parse_job_completion,
    parse_observations,
)


FIXTURE = Path(__file__).parent / "fixtures" / "opera_protocol" / "contract.json"


def test_contract_fixture_parses_valid_heartbeat_observations_and_completion():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert (BRIDGE_HOST, BRIDGE_PORT, PROTOCOL_VERSION) == ("127.0.0.1", 47831, 4)
    assert parse_heartbeat(fixture["heartbeat"]) is ExtensionXState.SIGNED_IN
    assert parse_observations(fixture["progress"])[0] == CandidateObservation(
        url="https://x.com/openai/status/123",
        post_id="123",
        author_handle="openai",
        is_promoted=False,
        parent_url=None,
        discovery_order=0,
        photos=(
            PhotoCandidate(
                position=0,
                url="https://pbs.twimg.com/media/abc?format=jpg&name=large",
                alt_text="A test photo",
            ),
        ),
    )
    assert parse_job_completion("job-1", fixture["completion"]) == OperaJobResult(
        "job-1", DiscoveryReason.LIMIT, None
    )


@pytest.mark.parametrize("value", [None, {}, {"x_state": "wat"}, {"x_state": 1}])
def test_heartbeat_rejects_invalid_shapes(value: object):
    with pytest.raises(ProtocolError):
        parse_heartbeat(value)


def test_parsers_reject_boolean_flags_represented_as_integers():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["progress"]["observations"][0]["is_pinned"] = 1

    with pytest.raises(ProtocolError):
        parse_observations(fixture["progress"])


def test_observations_accept_canonical_parent_url():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["progress"]["observations"][0]["parent_url"] = "https://x.com/openai/status/122"

    assert parse_observations(fixture["progress"])[0].parent_url == (
        "https://x.com/openai/status/122"
    )


@pytest.mark.parametrize("field", ["is_promoted", "parent_url", "photos"])
def test_observations_reject_missing_expanded_fields(field: str):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del fixture["progress"]["observations"][0][field]

    with pytest.raises(ProtocolError):
        parse_observations(fixture["progress"])


def test_observations_reject_extra_fields():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["progress"]["observations"][0]["extra"] = True

    with pytest.raises(ProtocolError):
        parse_observations(fixture["progress"])


@pytest.mark.parametrize(
    "photos",
    [
        [
            {
                "position": position,
                "url": f"https://pbs.twimg.com/media/{position}?format=jpg&name=large",
                "alt_text": None,
            }
            for position in range(5)
        ],
        [
            {
                "position": 1,
                "url": "https://pbs.twimg.com/media/abc?format=jpg&name=large",
                "alt_text": None,
            }
        ],
        [
            {
                "position": 0,
                "url": "https://pbs.twimg.com/media/abc?format=jpg&name=large",
                "alt_text": None,
            },
            {
                "position": 1,
                "url": "https://pbs.twimg.com/media/abc?format=jpg&name=large",
                "alt_text": None,
            },
        ],
        [
            {
                "position": 0,
                "url": "http://pbs.twimg.com/media/abc?format=jpg&name=large",
                "alt_text": None,
            }
        ],
        [
            {
                "position": 0,
                "url": "https://evil.example/media/abc?format=jpg&name=large",
                "alt_text": None,
            }
        ],
        [
            {
                "position": 0,
                "url": "https://pbs.twimg.com/media/abc?format=jpg&name=large",
                "alt_text": "x" * 1001,
            }
        ],
        [
            {
                "position": 0,
                "url": "https://pbs.twimg.com/media/abc?format=jpg&name=large",
                "alt_text": None,
                "extra": True,
            }
        ],
    ],
    ids=[
        "five photos",
        "non-contiguous positions",
        "duplicate URLs",
        "non-HTTPS URL",
        "wrong host",
        "alt text over 1000 characters",
        "unknown photo field",
    ],
)
def test_observations_reject_invalid_photo_contract(photos: object):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["progress"]["observations"][0]["photos"] = photos

    with pytest.raises(ProtocolError):
        parse_observations(fixture["progress"])


def test_observations_reject_legacy_protocol_version_two():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["progress"]["protocol_version"] = 2

    with pytest.raises(ProtocolError):
        parse_observations(fixture["progress"])


@pytest.mark.parametrize("value", [0, 1, "false", None])
def test_observations_reject_non_boolean_promotion_flags(value: object):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["progress"]["observations"][0]["is_promoted"] = value

    with pytest.raises(ProtocolError):
        parse_observations(fixture["progress"])


@pytest.mark.parametrize(
    "parent_url",
    [
        0,
        "",
        "http://x.com/openai/status/122",
        "https://twitter.com/openai/status/122",
        "https://x.com.evil.example/openai/status/122",
        "https://x.com/OpenAI/status/122",
        "https://x.com/openai/status/not-a-number",
    ],
)
def test_observations_ignore_invalid_parent_metadata_without_rejecting_top_level(
    parent_url: object,
):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["progress"]["observations"][0]["parent_url"] = parent_url

    observation = parse_observations(fixture["progress"])[0]

    assert observation.url == "https://x.com/openai/status/123"
    assert observation.post_id == "123"
    assert observation.parent_url is None


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_heartbeat, {"protocol_version": 4}),
        (parse_observations, {"protocol_version": 4}),
        (parse_job_completion, {"protocol_version": 4, "reason": "limit"}),
    ],
)
def test_parsers_reject_missing_fields(parser: object, payload: object):
    with pytest.raises(ProtocolError):
        if parser is parse_job_completion:
            parser("job-1", payload)  # type: ignore[operator]
        else:
            parser(payload)  # type: ignore[operator]


def test_observations_reject_more_than_maximum_candidates():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    observation = fixture["progress"]["observations"][0]
    fixture["progress"]["observations"] = [observation] * 51

    with pytest.raises(ProtocolError):
        parse_observations(fixture["progress"])


def test_completion_rejects_diagnostic_over_500_characters():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["completion"]["diagnostic"] = "x" * 501

    with pytest.raises(ProtocolError):
        parse_job_completion("job-1", fixture["completion"])


def test_completion_rejects_unknown_reason():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["completion"]["reason"] = "unknown"

    with pytest.raises(ProtocolError):
        parse_job_completion("job-1", fixture["completion"])


@pytest.mark.parametrize("job_id", ["", " ", 1, None])
def test_completion_rejects_invalid_job_id(job_id: object):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    with pytest.raises(ProtocolError):
        parse_job_completion(job_id, fixture["completion"])


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_heartbeat, {"protocol_version": 4, "x_state": "signed_in", "extra": True}),
        (parse_observations, {"protocol_version": 4, "observations": [], "extra": True}),
        (
            parse_job_completion,
            {"protocol_version": 4, "reason": "limit", "diagnostic": None, "extra": True},
        ),
    ],
)
def test_parsers_reject_extra_top_level_keys(parser: object, payload: object):
    with pytest.raises(ProtocolError):
        if parser is parse_job_completion:
            parser("job-1", payload)  # type: ignore[operator]
        else:
            parser(payload)  # type: ignore[operator]


def photo_job() -> OperaPhotoJob:
    return OperaPhotoJob(
        id="job-media-1",
        post_id="123",
        post_url="https://x.com/openai/status/123",
        deadline_at=45.0,
    )


def test_explicit_collection_and_photo_jobs_are_immutable() -> None:
    collection = OperaCollectionJob(
        id="job-1",
        target_kind=CollectionTargetKind.SOURCE,
        handle="openai",
        profile_url="https://x.com/openai",
        maximum=30,
        deadline_at=45.0,
    )
    media = photo_job()

    with pytest.raises(FrozenInstanceError):
        collection.maximum = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        media.post_id = "456"  # type: ignore[misc]


def test_photo_completion_parses_exact_ordered_manifest() -> None:
    result = parse_job_completion(
        photo_job(),
        {
            "protocol_version": 4,
            "reason": "exhausted",
            "diagnostic": None,
            "photos": [
                {
                    "position": 0,
                    "url": "https://pbs.twimg.com/media/abc?format=jpg&name=small",
                    "alt_text": "Photo",
                }
            ],
        },
    )

    assert result == OperaPhotoResolutionResult(
        "job-media-1",
        DiscoveryReason.EXHAUSTED,
        (
            PhotoCandidate(
                0,
                "https://pbs.twimg.com/media/abc?format=jpg&name=large",
                "Photo",
            ),
        ),
    )


def test_collection_completion_rejects_media_photos_field() -> None:
    with pytest.raises(ProtocolError):
        parse_job_completion(
            "job-1",
            {
                "protocol_version": 4,
                "reason": "exhausted",
                "diagnostic": None,
                "photos": [],
            },
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol_version": 4, "reason": "exhausted", "diagnostic": None},
        {
            "protocol_version": 4,
            "reason": "exhausted",
            "diagnostic": None,
            "photos": [],
            "extra": True,
        },
        {
            "protocol_version": 4,
            "reason": "exhausted",
            "diagnostic": "x" * 501,
            "photos": [],
        },
        {
            "protocol_version": 4,
            "reason": "exhausted",
            "diagnostic": None,
            "photos": [
                {
                    "position": 0,
                    "url": "https://evil.example/media/abc",
                    "alt_text": None,
                }
            ],
        },
    ],
)
def test_photo_completion_rejects_invalid_exact_contract(payload: object) -> None:
    with pytest.raises(ProtocolError):
        parse_job_completion(photo_job(), payload)
