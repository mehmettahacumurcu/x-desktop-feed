import html
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

from bs4 import BeautifulSoup

from xfeed.domain import SavedPost
from xfeed.i18n import tr
from xfeed.media import MediaAsset, MediaAssetState
from xfeed.media_repository import MediaRepository
from xfeed.urls import InvalidPostUrl, canonicalize_post_url


MediaMap: TypeAlias = Mapping[int, Sequence[MediaAsset]]


def media_for_posts(
    repository: MediaRepository,
    posts: Sequence[SavedPost],
) -> dict[int, tuple[MediaAsset, ...]]:
    return repository.assets_for_posts(tuple(post.id for post in posts))


@dataclass(frozen=True)
class FeedSection:
    title: str
    posts: Sequence[SavedPost]
    anchor: str | None = None
    subdued: bool = False


class FeedHtmlBuilder:
    def render(
        self,
        posts: Iterable[SavedPost],
        focus_post_id: int | None = None,
        *,
        media_by_post: MediaMap | None = None,
    ) -> str:
        materialized = tuple(posts)
        sections = (FeedSection("", materialized),) if materialized else ()
        return self.render_sections(
            sections,
            focus_post_id=focus_post_id,
            media_by_post=media_by_post,
            empty_title=tr("No saved posts yet"),
            empty_body=tr("Posts you collect will appear here."),
        )

    def render_sections(
        self,
        sections: Sequence[FeedSection],
        *,
        focus_post_id: int | None = None,
        media_by_post: MediaMap | None = None,
        empty_title: str = "Nothing here yet",
        empty_body: str = "Collected posts will appear here.",
    ) -> str:
        media = media_by_post or {}
        content = "".join(self._section(section, focus_post_id, media) for section in sections)
        if not content:
            content = self._empty_state(tr(empty_title), tr(empty_body))
        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="color-scheme" content="light">
  <style>
    :root {{ color-scheme: light; font-family: "Segoe UI", system-ui, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 22px 18px 48px; background: #f8fafc; color: #202534; }}
    main {{ display: grid; gap: 18px; max-width: 760px; margin: 0 auto; }}
    .feed-section {{ display: grid; gap: 13px; scroll-margin-top: 20px; }}
    .feed-section.subdued {{ padding-top: 8px; }}
    .section-heading {{ display: flex; align-items: center; gap: 12px; margin: 5px 0 1px; }}
    .section-heading h2 {{ margin: 0; font-size: 13px; letter-spacing: .055em; text-transform: uppercase; color: #697386; }}
    .section-heading::after {{ content: ""; height: 1px; background: #e0e5ed; flex: 1; }}
    .post-card {{ background: #fff; border: 1px solid #e0e5ed; border-radius: 16px; padding: 18px; box-shadow: 0 8px 24px rgba(21,26,39,.045); }}
    .post-card.focused {{ outline: 3px solid #6d5dfc; outline-offset: 2px; scroll-margin-top: 18px; }}
    .author {{ font-weight: 700; color: #151a27; }}
    .handle, .provider-state, time {{ color: #697386; }}
    time {{ display: block; margin-top: 3px; font-size: 12px; }}
    .local-text {{ white-space: pre-wrap; line-height: 1.52; font-size: 15px; }}
    a {{ color: #6d5dfc; font-weight: 600; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .provider-state {{ margin-top: 12px; padding-top: 10px; border-top: 1px solid #eef1f5; font-size: 11px; }}
    .media-grid {{ display: grid; gap: 2px; margin: 14px 0 4px; overflow: hidden; border-radius: 12px; background: #fff; }}
    .media-grid.media-count-1 {{ aspect-ratio: 16 / 9; }}
    .media-grid.media-count-2 {{ grid-template-columns: repeat(2, minmax(0, 1fr)); aspect-ratio: 16 / 9; }}
    .media-grid.media-count-3 {{ grid-template-columns: 1.35fr 1fr; grid-template-rows: repeat(2, minmax(0, 1fr)); aspect-ratio: 4 / 3; }}
    .media-grid.media-count-3 .media-tile:first-child {{ grid-row: 1 / 3; }}
    .media-grid.media-count-4 {{ grid-template-columns: repeat(2, minmax(0, 1fr)); grid-template-rows: repeat(2, minmax(0, 1fr)); aspect-ratio: 4 / 3; }}
    .media-tile {{ min-width: 0; min-height: 0; padding: 0; border: 0; background: #eef1f5; cursor: zoom-in; }}
    .media-tile:focus-visible {{ outline: 3px solid #6d5dfc; outline-offset: -3px; }}
    .media-photo {{ display: block; width: 100%; height: 100%; object-fit: cover; }}
    .media-state {{ margin: 8px 0 0; color: #697386; font-size: 12px; }}
    .media-preview {{ position: fixed; inset: 0; z-index: 10; display: grid; place-items: center; padding: 4vh 4vw; background: rgba(21,26,39,.88); }}
    .media-preview[hidden] {{ display: none; }}
    .media-preview img {{ display: block; max-width: min(92vw, 1180px); max-height: 86vh; border-radius: 12px; box-shadow: 0 18px 60px rgba(0,0,0,.35); }}
    .preview-close {{ position: fixed; top: 18px; right: 18px; width: 40px; height: 40px; border: 1px solid rgba(255,255,255,.5); border-radius: 50%; background: rgba(21,26,39,.72); color: #fff; font: 24px/1 "Segoe UI", system-ui, sans-serif; cursor: pointer; }}
    .preview-close:focus-visible {{ outline: 3px solid #fff; outline-offset: 3px; }}
    .empty-state {{ padding: 64px 28px; text-align: center; background: #fff; border: 1px dashed #cdd3dd; border-radius: 16px; }}
    .empty-title {{ margin: 0 0 8px; font-size: 19px; color: #151a27; }}
    .empty-body {{ margin: 0; color: #697386; line-height: 1.5; }}
    blockquote.twitter-tweet {{ margin-left: 0; margin-right: 0; }}
  </style>
</head>
<body>
  <main>{content}</main>
  {self._preview_markup() if "xfeed-media://asset/" in content else ""}
  <script async src="https://platform.x.com/widgets.js"></script>
  {self._preview_script() if "xfeed-media://asset/" in content else ""}
  {self._focus_script(focus_post_id)}
</body>
</html>"""

    @staticmethod
    def _section(
        section: FeedSection,
        focus_post_id: int | None,
        media_by_post: MediaMap,
    ) -> str:
        cards = "".join(
            FeedHtmlBuilder._card(post, focus_post_id, media_by_post.get(post.id, ()))
            for post in section.posts
        )
        if not cards:
            return ""
        classes = "feed-section subdued" if section.subdued else "feed-section"
        anchor = ""
        if section.anchor is not None and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", section.anchor):
            anchor = f' id="{section.anchor}"'
        heading = (
            f'<header class="section-heading"><h2>{html.escape(section.title)}</h2></header>'
            if section.title
            else ""
        )
        return f'<section{anchor} class="{classes}">{heading}{cards}</section>'

    @staticmethod
    def _empty_state(title: str, body: str) -> str:
        return (
            '<section class="empty-state">'
            f'<h2 class="empty-title">{html.escape(title)}</h2>'
            f'<p class="empty-body">{html.escape(body)}</p>'
            "</section>"
        )

    @staticmethod
    def _card(
        post: SavedPost,
        focus_post_id: int | None,
        assets: Sequence[MediaAsset],
    ) -> str:
        focused = " focused" if post.id == focus_post_id else ""
        author_name = html.escape(tr(post.author_name or "Unknown author"), quote=True)
        author_handle = html.escape(post.author_handle or "unknown", quote=True)
        text = html.escape(post.text, quote=True)
        published_at = html.escape(tr(post.published_at or "Unknown date"), quote=True)
        availability = html.escape(post.availability.value, quote=True)
        source_method = html.escape(post.source_method, quote=True)
        available = tuple(
            sorted(
                (asset for asset in assets if asset.state is MediaAssetState.AVAILABLE),
                key=lambda asset: asset.position,
            )
        )[:4]
        pending = tuple(
            asset
            for asset in assets
            if asset.state in (MediaAssetState.PENDING, MediaAssetState.DOWNLOADING)
        )
        failed = tuple(asset for asset in assets if asset.state is MediaAssetState.FAILED)
        gallery = FeedHtmlBuilder._gallery(post, available)
        media_state = FeedHtmlBuilder._media_state(pending, failed)
        embed = ""
        if not available and post.source_method.startswith("oembed"):
            embed = FeedHtmlBuilder._sanitize_embed(post.embed_html)
        local_fields = f'<p class="local-text">{text}</p>{FeedHtmlBuilder._source_link(post)}'
        body = f"{local_fields}{gallery}{media_state}{embed}"
        return f"""<article id="post-{post.id}" class="post-card{focused}">
  <header><span class="author">{author_name}</span>
    <span class="handle">@{author_handle}</span></header>
  <time>{published_at}</time>
  {body}
  <footer class="provider-state">{availability} | {source_method}</footer>
</article>"""

    @staticmethod
    def _gallery(post: SavedPost, assets: Sequence[MediaAsset]) -> str:
        if not assets:
            return ""
        handle = post.author_handle or "unknown"
        photos = []
        for asset in assets:
            alt = asset.alt_text or tr("Saved photo {position} from @{handle}").format(
                position=asset.position + 1, handle=handle
            )
            photos.append(
                '<button class="media-tile" type="button" aria-label="{}">'
                '<img class="media-photo" src="xfeed-media://asset/{}" '
                'alt="{}"></button>'.format(
                    html.escape(tr("Preview photo"), quote=True),
                    asset.id,
                    html.escape(alt, quote=True),
                )
            )
        return f'<div class="media-grid media-count-{len(assets)}">{"".join(photos)}</div>'

    @staticmethod
    def _media_state(
        pending: Sequence[MediaAsset],
        failed: Sequence[MediaAsset],
    ) -> str:
        messages = []
        if pending:
            messages.append(tr("Saving photos&hellip;"))
        if failed:
            if len(failed) == 1:
                messages.append(
                    tr("{count} photo couldn&#x27;t be saved.").format(count=len(failed))
                )
            else:
                messages.append(
                    tr("{count} photos couldn&#x27;t be saved.").format(count=len(failed))
                )
        return "".join(f'<p class="media-state">{message}</p>' for message in messages)

    @staticmethod
    def _preview_markup() -> str:
        return (
            '<div id="media-preview" class="media-preview" role="dialog" '
            'aria-modal="true" aria-label="{}" hidden>'
            '<button class="preview-close" type="button" aria-label="{}">&times;</button>'
            '<img id="media-preview-image" alt="">'
            "</div>"
        ).format(
            html.escape(tr("Photo preview"), quote=True),
            html.escape(tr("Close preview"), quote=True),
        )

    @staticmethod
    def _preview_script() -> str:
        return r"""<script>
  (() => {
    const preview = document.getElementById('media-preview');
    const previewImage = document.getElementById('media-preview-image');
    const closeButton = preview.querySelector('.preview-close');
    let triggerTile = null;
    const closePreview = () => {
      preview.hidden = true;
      previewImage.removeAttribute('src');
      const restoreTarget = triggerTile;
      triggerTile = null;
      if (restoreTarget?.isConnected) restoreTarget.focus();
    };
    document.addEventListener('click', (event) => {
      if (!(event.target instanceof Element)) return;
      const tile = event.target.closest('.media-tile');
      if (!tile) return;
      const photo = tile.querySelector('.media-photo');
      if (!photo) return;
      const url = new URL(photo.src);
      if (url.protocol !== 'xfeed-media:'
          || !/^xfeed-media:\/\/asset\/[0-9]+$/.test(url.href)) return;
      const assetUrl = new URL(url.href.replace(/^xfeed-media:/, 'https:'));
      if (assetUrl.hostname !== 'asset'
          || assetUrl.username || assetUrl.password || assetUrl.port
          || !/^\/[0-9]+$/.test(assetUrl.pathname)
          || assetUrl.search || assetUrl.hash) return;
      triggerTile = tile;
      previewImage.src = url.href;
      previewImage.alt = photo.alt;
      preview.hidden = false;
      closeButton.focus();
    });
    closeButton.addEventListener('click', closePreview);
    preview.addEventListener('click', (event) => {
      if (event.target === preview) closePreview();
    });
    document.addEventListener('keydown', (event) => {
      if (preview.hidden) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        closePreview();
      } else if (event.key === 'Tab') {
        event.preventDefault();
        closeButton.focus();
      }
    });
  })();
  </script>"""

    @staticmethod
    def _source_link(post: SavedPost) -> str:
        try:
            canonical = canonicalize_post_url(post.canonical_url)
        except InvalidPostUrl:
            return f'<p class="source-unavailable">{html.escape(tr("Saved source URL unavailable"))}</p>'
        safe_url = html.escape(canonical.url, quote=True)
        return f'<p><a href="{safe_url}">{html.escape(tr("Open the saved post on X"))}</a></p>'

    @staticmethod
    def _sanitize_embed(embed_html: str) -> str:
        parsed = BeautifulSoup(embed_html, "html.parser")
        for script in parsed.find_all("script"):
            script.decompose()
        return str(parsed)

    @staticmethod
    def _focus_script(focus_post_id: int | None) -> str:
        if focus_post_id is None:
            return ""
        return (
            "<script>const focused = document.getElementById('post-"
            f"{focus_post_id}'); if (focused) "
            "{ focused.scrollIntoView({block: 'start'}); }</script>"
        )
