import pytest

from xfeed.urls import InvalidPostUrl, canonicalize_post_url


MALFORMED_NETLOC_URLS = [
    "https://[x.com/example/status/123",
    "https://x.com:not-a-port/example/status/123",
    "https://x.com:99999/example/status/123",
]


@pytest.mark.parametrize(
    "raw",
    [
        "https://x.com/Test_User/status/12345?s=20",
        "https://twitter.com/Test_User/status/12345/photo/1",
    ],
)
def test_canonicalizes_supported_post_urls(raw):
    result = canonicalize_post_url(raw)

    assert result.url == "https://x.com/test_user/status/12345"
    assert result.handle == "test_user"
    assert result.post_id == "12345"


def test_rejects_non_x_hosts():
    with pytest.raises(InvalidPostUrl):
        canonicalize_post_url("https://example.com/Test_User/status/12345")


@pytest.mark.parametrize("raw", MALFORMED_NETLOC_URLS)
def test_rejects_malformed_netlocs_as_invalid_post_urls(raw):
    with pytest.raises(InvalidPostUrl):
        canonicalize_post_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "http://x.com/example/status/123",
        "https://x.com/example",
        "https://x.com/example/status/not-a-number",
        "https://x.com/handle-that-is-too-long/status/123",
    ],
)
def test_rejects_unsupported_x_urls(raw):
    with pytest.raises(InvalidPostUrl):
        canonicalize_post_url(raw)
