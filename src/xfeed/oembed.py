import http.client
import json
from datetime import datetime
from html import unescape
from typing import Callable, Protocol, cast
from urllib.parse import urlencode
from urllib.request import urlopen

from bs4 import BeautifulSoup

from xfeed.domain import PostDraft
from xfeed.urls import CanonicalPostUrl


class ProviderUnavailable(RuntimeError):
    pass


class PostMetadataProvider(Protocol):
    def fetch(self, post_url: CanonicalPostUrl) -> PostDraft: ...


Transport = Callable[[str, float], bytes]


def urllib_transport(url: str, timeout: float) -> bytes:
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 -- fixed provider host
        return cast(bytes, response.read())


class OEmbedClient:
    def __init__(
        self,
        transport: Transport = urllib_transport,
        timeout: float = 8.0,
    ) -> None:
        self.transport = transport
        self.timeout = timeout

    def fetch(self, post_url: CanonicalPostUrl) -> PostDraft:
        endpoint = "https://publish.x.com/oembed?" + urlencode(
            {
                "url": post_url.url,
                "omit_script": "1",
                "dnt": "true",
                "lang": "en",
            }
        )
        try:
            payload = json.loads(self.transport(endpoint, self.timeout))
            if not isinstance(payload, dict):
                raise TypeError("The oEmbed payload is not an object")
            html = payload.get("html")
            if not isinstance(html, str) or not html.strip():
                raise TypeError("The oEmbed html field is not a non-empty string")
            author_name = payload.get("author_name")
            if author_name is not None and not isinstance(author_name, str):
                raise TypeError("The oEmbed author_name field is not a string or null")
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            http.client.HTTPException,
            UnicodeError,
        ) as error:
            raise ProviderUnavailable(str(error)) from error

        soup = BeautifulSoup(html, "html.parser")
        paragraph = soup.find("p")
        text = unescape(paragraph.get_text(" ", strip=True)) if paragraph else ""
        links = soup.find_all("a")
        published_at = None
        if links:
            try:
                published_at = (
                    datetime.strptime(
                        links[-1].get_text(strip=True),
                        "%B %d, %Y",
                    )
                    .date()
                    .isoformat()
                )
            except ValueError:
                published_at = None
        has_media = "pic.x.com/" in html or "pic.twitter.com/" in html
        return PostDraft(
            canonical_url=post_url.url,
            x_post_id=post_url.post_id,
            author_handle=post_url.handle,
            author_name=author_name,
            text=text,
            published_at=published_at,
            embed_html=html,
            provider_json=json.dumps(payload, ensure_ascii=False),
            source_method="oembed",
            has_media=has_media,
        )
