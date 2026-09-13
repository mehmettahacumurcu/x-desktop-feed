import json
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

from xfeed.domain import Availability, SavedPost
from xfeed.media import MediaAsset, MediaAssetState
from xfeed.ui.feed_html import FeedHtmlBuilder, FeedSection


def saved_post(
    post_id: int = 7,
    *,
    source_method: str = "oembed",
    embed_html: str = "<blockquote>trusted embed</blockquote>",
    canonical_url: str = "https://twitter.com/Example/status/7?ref=feed",
) -> SavedPost:
    return SavedPost(
        canonical_url=canonical_url,
        x_post_id="7",
        author_handle='<author & "friends">',
        author_name='<Author & "Friends">',
        text='<script>alert("local")</script> & text',
        published_at='<time datetime="unsafe">',
        embed_html=embed_html,
        provider_json='{"untrusted":"local"}',
        source_method=source_method,
        has_media=False,
        availability=Availability.OFFLINE,
        id=post_id,
    )


def media_asset(
    asset_id: int,
    *,
    post_id: int = 7,
    position: int = 0,
    state: MediaAssetState = MediaAssetState.AVAILABLE,
    alt_text: str | None = None,
    local_path: str | None = None,
) -> MediaAsset:
    return MediaAsset(
        id=asset_id,
        post_id=post_id,
        position=position,
        source_url=f"https://pbs.twimg.com/media/photo-{asset_id}?format=jpg&name=large",
        alt_text=alt_text,
        state=state,
        local_path=local_path or f"{post_id}/private-photo-{position}.jpg",
        mime_type="image/jpeg" if state is MediaAssetState.AVAILABLE else None,
        width=640 if state is MediaAssetState.AVAILABLE else None,
        height=480 if state is MediaAssetState.AVAILABLE else None,
        attempts=1,
        diagnostic=None,
        created_at="2026-01-01",
        updated_at="2026-01-01",
    )


def test_renderer_escapes_local_fields_and_shows_author_and_provider_state() -> None:
    rendered = FeedHtmlBuilder().render([saved_post()])

    assert "&lt;Author &amp; &quot;Friends&quot;&gt;" in rendered
    assert "@&lt;author &amp; &quot;friends&quot;&gt;" in rendered
    assert "&lt;script&gt;alert(&quot;local&quot;)&lt;/script&gt; &amp; text" in rendered
    assert "&lt;time datetime=&quot;unsafe&quot;&gt;" in rendered
    assert 'href="https://x.com/example/status/7"' in rendered
    assert "offline" in rendered
    assert "oembed" in rendered
    assert '<script>alert("local")</script>' not in rendered


@pytest.mark.parametrize(
    "stored_url",
    [
        "javascript:alert(1)",
        "https://example.com/author/status/7",
        "https://x.com/not-a-post",
        "https://x.com:not-a-port/author/status/7",
    ],
)
def test_renderer_never_links_malformed_or_non_x_stored_urls(stored_url: str) -> None:
    rendered = FeedHtmlBuilder().render([saved_post(canonical_url=stored_url)])

    assert "<a href=" not in rendered
    assert "Saved source URL unavailable" in rendered


def test_renderer_only_includes_stored_embed_for_oembed_source_methods() -> None:
    trusted = saved_post(
        1,
        source_method="oembed-refresh",
        embed_html='<blockquote data-origin="trusted">embedded</blockquote>',
    )
    local = saved_post(
        2,
        source_method="public-dom",
        embed_html='<blockquote data-origin="untrusted">do not render</blockquote>',
    )

    rendered = FeedHtmlBuilder().render([trusted, local])

    assert '<blockquote data-origin="trusted">embedded</blockquote>' in rendered
    assert 'data-origin="untrusted"' not in rendered
    assert "do not render" not in rendered


def test_renderer_loads_widgets_script_once_for_multiple_embeds() -> None:
    widget_script = '<script async src="https://platform.x.com/widgets.js"></script>'
    posts = [
        saved_post(1, embed_html=f"<blockquote>one</blockquote>{widget_script}"),
        saved_post(2, embed_html=f"<blockquote>two</blockquote>{widget_script}"),
    ]

    rendered = FeedHtmlBuilder().render(posts)

    assert rendered.count("https://platform.x.com/widgets.js") == 1
    assert "<blockquote>one</blockquote>" in rendered
    assert "<blockquote>two</blockquote>" in rendered


def test_renderer_removes_all_provider_scripts_before_adding_controlled_widgets_script() -> None:
    provider_scripts = """
      <script>alert('inline')</script>
      <script src=https://platform.x.com/widgets.js?cache=1></script>
      <SCRIPT defer SRC='https://platform.twitter.com/widgets.js'></SCRIPT>
      <script type="application/javascript" src="https://evil.example/script.js"></script>
    """

    rendered = FeedHtmlBuilder().render(
        [saved_post(embed_html=f"<blockquote>safe embed</blockquote>{provider_scripts}")]
    )

    assert rendered.lower().count("<script") == 1
    assert rendered.count("https://platform.x.com/widgets.js") == 1
    assert "platform.twitter.com" not in rendered
    assert "evil.example" not in rendered
    assert "alert('inline')" not in rendered
    assert "<blockquote>safe embed</blockquote>" in rendered


def test_renderer_marks_only_the_focused_card() -> None:
    rendered = FeedHtmlBuilder().render([saved_post(1), saved_post(2)], focus_post_id=2)

    assert 'id="post-1" class="post-card"' in rendered
    assert 'id="post-2" class="post-card focused"' in rendered
    assert "if (focused)" in rendered


def test_renderer_guards_focus_lookup_when_matching_card_is_absent() -> None:
    rendered = FeedHtmlBuilder().render([saved_post(1)], focus_post_id=999)

    assert "document.getElementById('post-999')" in rendered
    assert "if (focused)" in rendered
    assert "post-card focused" not in rendered


def test_renderer_has_an_explicit_empty_state() -> None:
    rendered = FeedHtmlBuilder().render([])

    assert "No saved posts yet" in rendered
    assert "<article" not in rendered


def test_renderer_uses_the_focused_reader_visual_system() -> None:
    rendered = FeedHtmlBuilder().render([saved_post()])

    assert "#f8fafc" in rendered.casefold()
    assert "#6d5dfc" in rendered.casefold()
    assert "Segoe UI" in rendered
    assert "max-width: 760px" in rendered
    assert "border-radius: 16px" in rendered


def test_renderer_supports_named_sections_and_older_session_anchor() -> None:
    rendered = FeedHtmlBuilder().render_sections(
        (
            FeedSection("This session", (saved_post(1),)),
            FeedSection(
                "Older app sessions",
                (saved_post(2),),
                anchor="older-sessions",
                subdued=True,
            ),
        )
    )

    assert "This session" in rendered
    assert "Older app sessions" in rendered
    assert 'id="older-sessions"' in rendered
    assert 'class="feed-section subdued"' in rendered
    assert rendered.index('id="post-1"') < rendered.index('id="post-2"')


def test_renderer_accepts_specific_empty_state_copy() -> None:
    rendered = FeedHtmlBuilder().render_sections(
        (),
        empty_title="Your fresh feed starts here",
        empty_body="Collect from For You when you are ready.",
    )

    assert "Your fresh feed starts here" in rendered
    assert "Collect from For You when you are ready." in rendered
    assert "empty-title" in rendered


@pytest.mark.parametrize("count", (1, 2, 3, 4))
def test_renderer_emits_internal_gallery_geometry_for_one_to_four_photos(count: int) -> None:
    assets = tuple(
        media_asset(
            70 + position,
            position=position,
            alt_text=f'Photo {position + 1} <private & "safe">',
        )
        for position in range(count)
    )

    rendered = FeedHtmlBuilder().render([saved_post()], media_by_post={7: assets})

    assert f'class="media-grid media-count-{count}"' in rendered
    for asset in assets:
        assert f'src="xfeed-media://asset/{asset.id}"' in rendered
    assert rendered.count('class="media-photo"') == count
    assert "Photo 1 &lt;private &amp; &quot;safe&quot;&gt;" in rendered
    assert "private-photo" not in rendered
    assert str(Path("/absolute/private-photo.jpg")) not in rendered


def test_renderer_suppresses_online_embed_only_when_local_photo_is_available() -> None:
    post = saved_post(embed_html='<blockquote data-online="true">online fallback</blockquote>')
    pending = media_asset(70, state=MediaAssetState.PENDING)
    available = media_asset(71)

    with_local = FeedHtmlBuilder().render([post], media_by_post={7: (pending, available)})
    without_local = FeedHtmlBuilder().render([post], media_by_post={7: (pending,)})

    assert 'data-online="true"' not in with_local
    assert "online fallback" not in with_local
    assert '<blockquote data-online="true">online fallback</blockquote>' in without_local
    assert 'href="https://x.com/example/status/7"' in with_local
    assert 'href="https://x.com/example/status/7"' in without_local


def test_renderer_reports_pending_and_failed_assets_separately() -> None:
    assets = (
        media_asset(70, position=0),
        media_asset(71, position=1, state=MediaAssetState.PENDING),
        media_asset(72, position=2, state=MediaAssetState.DOWNLOADING),
        media_asset(73, position=3, state=MediaAssetState.FAILED),
    )

    rendered = FeedHtmlBuilder().render([saved_post()], media_by_post={7: assets})

    assert "Saving photos&hellip;" in rendered
    assert "1 photo couldn&#x27;t be saved." in rendered


def test_renderer_reports_total_failure_and_keeps_online_fallback() -> None:
    assets = (
        media_asset(70, position=0, state=MediaAssetState.FAILED),
        media_asset(71, position=1, state=MediaAssetState.FAILED),
    )

    rendered = FeedHtmlBuilder().render([saved_post()], media_by_post={7: assets})

    assert "2 photos couldn&#x27;t be saved." in rendered
    assert "trusted embed" in rendered
    assert 'href="https://x.com/example/status/7"' in rendered


def test_render_sections_uses_the_media_map_for_each_section() -> None:
    rendered = FeedHtmlBuilder().render_sections(
        (
            FeedSection("This session", (saved_post(1),)),
            FeedSection("Older", (saved_post(2),)),
        ),
        media_by_post={
            1: (media_asset(101, post_id=1),),
            2: (media_asset(202, post_id=2),),
        },
    )

    assert 'src="xfeed-media://asset/101"' in rendered
    assert 'src="xfeed-media://asset/202"' in rendered


def test_preview_is_bounded_and_accepts_only_clicked_internal_asset_urls() -> None:
    rendered = FeedHtmlBuilder().render(
        [saved_post()],
        media_by_post={7: (media_asset(70),)},
    )

    assert 'id="media-preview"' in rendered
    assert "max-width: min(92vw, 1180px)" in rendered
    assert "max-height: 86vh" in rendered
    assert "event.target.closest('.media-tile')" in rendered
    assert "tile.querySelector('.media-photo')" in rendered
    assert "url.protocol !== 'xfeed-media:'" in rendered
    assert r"!/^xfeed-media:\/\/asset\/[0-9]+$/.test(url.href)" in rendered
    assert "assetUrl.hostname !== 'asset'" in rendered
    assert "assetUrl.username || assetUrl.password || assetUrl.port" in rendered
    assert r"!/^\/[0-9]+$/.test(assetUrl.pathname)" in rendered
    assert "assetUrl.search || assetUrl.hash" in rendered
    assert "window.open" not in rendered
    assert "location.href" not in rendered


def execute_preview_javascript(qtbot, script: str) -> dict[str, object]:
    profile = QWebEngineProfile()
    page = QWebEnginePage(profile)
    rendered = (
        FeedHtmlBuilder()
        .render(
            [saved_post()],
            media_by_post={
                7: (
                    media_asset(70, position=0),
                    media_asset(71, position=1),
                )
            },
        )
        .replace('<script async src="https://platform.x.com/widgets.js"></script>', "")
    )
    loaded: list[bool] = []
    page.loadFinished.connect(loaded.append)
    page.setHtml(rendered, QUrl("https://x.com"))
    qtbot.waitUntil(lambda: bool(loaded), timeout=5_000)
    results: list[object] = []
    page.runJavaScript(script, results.append)
    qtbot.waitUntil(lambda: bool(results), timeout=5_000)
    result = json.loads(str(results[0]))
    assert isinstance(result, dict)
    page.deleteLater()
    profile.deleteLater()
    return result


def test_preview_traps_focus_and_restores_the_exact_trigger(qtbot) -> None:
    result = execute_preview_javascript(
        qtbot,
        r"""(() => {
          const [first, second] = document.querySelectorAll('.media-tile');
          const preview = document.getElementById('media-preview');
          const close = preview.querySelector('.preview-close');
          first.focus();
          first.click();
          const focusedIntoModal = document.activeElement === close;
          const tab = new KeyboardEvent(
            'keydown', {key: 'Tab', bubbles: true, cancelable: true}
          );
          document.dispatchEvent(tab);
          const tabTrapped = tab.defaultPrevented && document.activeElement === close;
          const shiftTab = new KeyboardEvent(
            'keydown', {key: 'Tab', shiftKey: true, bubbles: true, cancelable: true}
          );
          document.dispatchEvent(shiftTab);
          const shiftTabTrapped =
            shiftTab.defaultPrevented && document.activeElement === close;
          close.click();
          const closeRestored = preview.hidden && document.activeElement === first;
          const source = document.querySelector('a');
          source.focus();
          close.click();
          const triggerCleared = document.activeElement === source;
          second.focus();
          second.click();
          document.dispatchEvent(
            new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})
          );
          const escapeRestored = preview.hidden && document.activeElement === second;
          first.focus();
          first.click();
          preview.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          const backdropRestored = preview.hidden && document.activeElement === first;
          return JSON.stringify({
            focusedIntoModal,
            tabTrapped,
            shiftTabTrapped,
            closeRestored,
            triggerCleared,
            escapeRestored,
            backdropRestored
          });
        })()""",
    )

    assert result == {
        "focusedIntoModal": True,
        "tabTrapped": True,
        "shiftTabTrapped": True,
        "closeRestored": True,
        "triggerCleared": True,
        "escapeRestored": True,
        "backdropRestored": True,
    }


def test_preview_browser_logic_rejects_every_noncanonical_asset_authority(qtbot) -> None:
    result = execute_preview_javascript(
        qtbot,
        r"""(() => {
          const tile = document.querySelector('.media-tile');
          const photo = tile.querySelector('.media-photo');
          const preview = document.getElementById('media-preview');
          const invalid = [
            'xfeed-media://user@asset/70',
            'xfeed-media://user:secret@asset/70',
            'xfeed-media://asset:0/70',
            'xfeed-media://asset:1/70',
            'xfeed-media://asset:443/70',
            'xfeed-media://asset/70/extra',
            'xfeed-media://asset/70?query=1',
            'xfeed-media://asset/70#fragment'
          ];
          const rejected = invalid.map((url) => {
            photo.src = url;
            tile.click();
            const stayedClosed = preview.hidden;
            if (!stayedClosed) preview.querySelector('.preview-close').click();
            return stayedClosed;
          });
          photo.src = 'xfeed-media://asset/70';
          tile.click();
          return JSON.stringify({
            rejected,
            canonicalOpened: !preview.hidden
          });
        })()""",
    )

    assert result == {
        "rejected": [True, True, True, True, True, True, True, True],
        "canonicalOpened": True,
    }
