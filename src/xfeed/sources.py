import re
from dataclasses import dataclass
from html import escape
from urllib.parse import urlparse

from xfeed.i18n import tr


HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
SUPPORTED_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}


@dataclass(frozen=True)
class SourceProfile:
    handle: str
    profile_url: str


@dataclass(frozen=True)
class SourceRecord:
    id: int
    handle: str
    profile_url: str
    enabled: bool
    last_refresh_at: str | None

    @property
    def profile(self) -> SourceProfile:
        return SourceProfile(self.handle, self.profile_url)


def canonicalize_profile(value: str) -> SourceProfile:
    candidate = value.strip()
    if candidate.casefold().startswith(("http://", "https://")):
        parsed = urlparse(candidate)
        if parsed.hostname not in SUPPORTED_HOSTS:
            raise ValueError(tr("Unsupported profile host"))
        candidate = parsed.path.strip("/").split("/", 1)[0]
    candidate = candidate.removeprefix("@")
    if not HANDLE.fullmatch(candidate):
        raise ValueError(tr("Invalid X handle"))
    candidate = candidate.casefold()
    return SourceProfile(candidate, f"https://x.com/{candidate}")


class TimelineHtmlBuilder:
    def render(self, profile: SourceProfile) -> str:
        url = escape(profile.profile_url, quote=True)
        return (
            "<!doctype html><meta charset='utf-8'><body>"
            f'<a class="twitter-timeline" data-dnt="true" data-tweet-limit="20" href="{url}">'
            f"Posts by @{escape(profile.handle)}</a>"
            '<script async src="https://platform.x.com/widgets.js" charset="utf-8"></script></body>'
        )
