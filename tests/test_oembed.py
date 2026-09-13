import http.client
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from xfeed.domain import Availability
from xfeed.oembed import OEmbedClient, ProviderUnavailable
from xfeed.urls import canonicalize_post_url


FIXTURES = Path(__file__).parent / "fixtures" / "oembed"


def json_transport(payload: object):
    def transport(url: str, timeout: float) -> bytes:
        return json.dumps(payload).encode()

    return transport


def test_fetches_post_metadata_from_publish_x_oembed_fixture():
    requested: list[tuple[str, float]] = []

    def fixture_transport(url: str, timeout: float) -> bytes:
        requested.append((url, timeout))
        return (FIXTURES / "success.json").read_bytes()

    post_url = canonicalize_post_url("https://twitter.com/Example/status/123?s=20")

    draft = OEmbedClient(transport=fixture_transport).fetch(post_url)

    assert len(requested) == 1
    endpoint, timeout = requested[0]
    parsed_endpoint = urlparse(endpoint)
    assert parsed_endpoint.scheme == "https"
    assert parsed_endpoint.hostname == "publish.x.com"
    assert parsed_endpoint.path == "/oembed"
    assert parse_qs(parsed_endpoint.query) == {
        "url": ["https://x.com/example/status/123"],
        "omit_script": ["1"],
        "dnt": ["true"],
        "lang": ["en"],
    }
    assert timeout == 8.0
    assert draft.canonical_url == "https://x.com/example/status/123"
    assert draft.x_post_id == "123"
    assert draft.author_handle == "example"
    assert draft.author_name == "Example"
    assert draft.text == "Example text pic.x.com/media"
    assert draft.published_at == "2014-05-05"
    assert draft.has_media is True
    assert draft.source_method == "oembed"
    assert draft.availability is Availability.AVAILABLE
    assert json.loads(draft.provider_json)["provider_name"] == "Twitter"


def test_missing_html_is_provider_unavailable():
    def fixture_transport(url: str, timeout: float) -> bytes:
        return (FIXTURES / "unavailable.json").read_bytes()

    client = OEmbedClient(transport=fixture_transport)

    with pytest.raises(ProviderUnavailable):
        client.fetch(canonicalize_post_url("https://x.com/example/status/123"))


def test_transport_failure_is_provider_unavailable():
    def offline_transport(url: str, timeout: float) -> bytes:
        raise OSError("offline")

    client = OEmbedClient(transport=offline_transport)

    with pytest.raises(ProviderUnavailable, match="offline"):
        client.fetch(canonicalize_post_url("https://x.com/example/status/123"))


def test_truncated_transport_response_is_provider_unavailable():
    def truncated_transport(url: str, timeout: float) -> bytes:
        raise http.client.IncompleteRead(b"partial", partial=42)

    client = OEmbedClient(transport=truncated_transport)

    with pytest.raises(ProviderUnavailable):
        client.fetch(canonicalize_post_url("https://x.com/example/status/123"))


@pytest.mark.parametrize("payload", [None, [], "not an object", 123])
def test_top_level_non_object_payload_is_provider_unavailable(payload):
    client = OEmbedClient(transport=json_transport(payload))

    with pytest.raises(ProviderUnavailable):
        client.fetch(canonicalize_post_url("https://x.com/example/status/123"))


@pytest.mark.parametrize(
    "payload",
    [
        {"html": None},
        {"html": []},
        {"html": ""},
        {"html": "   "},
        {"html": "<p>valid text</p>", "author_name": []},
        {"html": "<p>valid text</p>", "author_name": {}},
    ],
)
def test_invalid_persisted_field_shapes_are_provider_unavailable(payload):
    client = OEmbedClient(transport=json_transport(payload))

    with pytest.raises(ProviderUnavailable):
        client.fetch(canonicalize_post_url("https://x.com/example/status/123"))
