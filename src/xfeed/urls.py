import re
from dataclasses import dataclass
from urllib.parse import urlparse

from xfeed.i18n import tr


POST_PATH = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)(?:/.*)?$")


class InvalidPostUrl(ValueError):
    pass


@dataclass(frozen=True)
class CanonicalPostUrl:
    url: str
    handle: str
    post_id: str


def canonicalize_post_url(raw_url: str) -> CanonicalPostUrl:
    try:
        parsed = urlparse(raw_url.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise InvalidPostUrl(tr("Use a public https://x.com/.../status/... URL")) from error
    if (
        parsed.scheme != "https"
        or hostname
        not in {
            "x.com",
            "www.x.com",
            "twitter.com",
            "www.twitter.com",
        }
        or port is not None
    ):
        raise InvalidPostUrl(tr("Use a public https://x.com/.../status/... URL"))
    match = POST_PATH.match(parsed.path)
    if match is None:
        raise InvalidPostUrl(tr("The URL is not an X Post URL"))
    handle, post_id = match.groups()
    handle = handle.casefold()
    return CanonicalPostUrl(
        f"https://x.com/{handle}/status/{post_id}",
        handle,
        post_id,
    )
