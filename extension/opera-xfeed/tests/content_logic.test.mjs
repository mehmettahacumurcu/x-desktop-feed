import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../content_logic.js", import.meta.url), "utf8");
const context = { URL, Node: { DOCUMENT_POSITION_FOLLOWING: 4 } };
vm.createContext(context);
vm.runInContext(source, context);
const api = context.XFeedContent;

function homeDocument({
  labels,
  selectedIndex = null,
  state = { selectedIndex },
  onClick = () => {},
  timelineState = { cards: [{}] },
}) {
  const tabs = labels.map((textContent, index) => ({
    textContent,
    getAttribute(name) {
      return name === "aria-selected" && index === state.selectedIndex ? "true" : null;
    },
    click() { onClick(index); },
  }));
  const tabList = {
    querySelectorAll(selector) {
      assert.equal(selector, ':scope [role="tab"]');
      return tabs;
    },
  };
  const primaryColumn = {
    querySelectorAll(selector) {
      assert.equal(selector, 'article[data-testid="tweet"]');
      return timelineState.cards;
    },
  };
  return {
    querySelector(selector) {
      if (selector === '[role="tablist"]') return tabList;
      if (selector === '[data-testid="primaryColumn"]') return primaryColumn;
      return null;
    },
    querySelectorAll(selector) {
      assert.equal(selector, '[role="tablist"]');
      return [tabList];
    },
  };
}

for (const [label, kind, expectedIndex, labels] of [
  ["For you", "for_you", 0, ["For you", "Following"]],
  ["Sana özel", "for_you", 0, ["Sana özel", "Takip edilenler"]],
  ["Following", "following", 1, ["For you", "Following"]],
  ["Takip edilenler", "following", 1, ["Sana özel", "Takip edilenler"]],
]) {
  test(`finds ${label} only in its verified core-tab position`, () => {
    const match = api.findHomeFeedTab(homeDocument({ labels }), kind);

    assert.equal(match.index, expectedIndex);
  });
}

test("rejects home labels outside their verified core-tab positions", () => {
  assert.equal(api.findHomeFeedTab(homeDocument({
    labels: ["Following", "For you"],
  }), "following"), null);
  assert.equal(api.findHomeFeedTab(homeDocument({
    labels: ["Explore", "Popular"],
  }), "for_you"), null);
  assert.equal(api.findHomeFeedTab({ querySelectorAll() { return []; } }, "following"), null);
});

test("rejects multiple matching core home tablists as ambiguous", () => {
  const first = homeDocument({
    labels: ["For you", "Following"], selectedIndex: 1,
  }).querySelector('[role="tablist"]');
  const duplicate = homeDocument({
    labels: ["For you", "Following"], selectedIndex: 1,
  }).querySelector('[role="tablist"]');
  const document = {
    querySelectorAll(selector) {
      assert.equal(selector, '[role="tablist"]');
      return [first, duplicate];
    },
  };

  assert.equal(api.findHomeFeedTab(document, "following"), null);
});

test("activates, settles, and reverifies the requested home feed before extraction", async () => {
  const state = { selectedIndex: 0 };
  const events = [];
  const document = homeDocument({
    labels: ["For you", "Following"],
    state,
    onClick(index) {
      events.push(`click:${index}`);
      state.selectedIndex = index;
    },
  });

  await api.selectAndVerifyHomeFeed(document, "following", 12_345, {
    displayName: () => "Following",
    captureTimeline() {
      events.push("timeline:before");
      return { root: {}, cards: [] };
    },
    async waitUntil(predicate, deadlineMs) {
      events.push(`selected:${deadlineMs}`);
      assert.equal(predicate(), true);
    },
    async waitForTimelineSettle(deadlineMs, _timeline, transitionRequired, displayName) {
      events.push(`settled:${deadlineMs}:${transitionRequired}:${displayName}`);
    },
  });

  assert.deepEqual(events, [
    "timeline:before",
    "click:1",
    "selected:12345",
    "settled:12345:true:Following",
  ]);
});

test("fails when the requested home feed loses selection after settling", async () => {
  const state = { selectedIndex: 1 };
  const document = homeDocument({ labels: ["For you", "Following"], state });

  await assert.rejects(api.selectAndVerifyHomeFeed(document, "following", 12_345, {
    displayName: () => "Following",
    captureTimeline() { return { root: {}, cards: [] }; },
    async waitUntil(predicate) { assert.equal(predicate(), true); },
    async waitForTimelineSettle() { state.selectedIndex = 0; },
  }), /Following tab selection was lost/);
});

function articleFixture(handle, postId, {
  social = false,
  socialText = social ? "Pinned" : "",
  nestedPostId = null,
  nestedHandle = handle,
  promoted = false,
  promotedOnArticle = false,
  promotedAncestor = false,
  promotedText = "",
  bodyText = "",
  bodyContainerText = "",
  reply = false,
  parentUrl = null,
  parentUrls = parentUrl === null ? [] : [parentUrl],
  permalinkUrl = `https://x.com/${handle}/status/${postId}`,
  photos = [],
} = {}) {
  let replyProfile = null;
  const permalink = {
    href: permalinkUrl,
    compareDocumentPosition(other) { return other === replyProfile ? 4 : 0; },
    closest() { return null; },
    matches() { return false; },
  };
  const time = { closest() { return permalink; } };
  const nested = nestedPostId === null ? [] : [{
    href: `https://x.com/${nestedHandle}/status/${nestedPostId}`,
    closest() { return null; },
    matches() { return false; },
  }];
  const parent = parentUrls.map((url) => ({
    href: url,
    closest(selector) { return selector.includes("replyParent") ? {} : null; },
    matches(selector) { return selector.includes("reply-parent"); },
  }));
  if (reply) {
    replyProfile = {
      href: "https://x.com/parent",
      closest() { return null; },
      matches() { return false; },
    };
  }
  const socialNode = socialText ? { innerText: socialText, textContent: socialText } : null;
  return {
    matches(selector) {
      return promotedOnArticle && selector.includes('[data-testid="placementTracking"]');
    },
    closest(selector) {
      return promotedAncestor && selector.includes('[data-testid="placementTracking"]') ? {} : null;
    },
    querySelector(selector) {
      if (selector === "time") return time;
      if (selector === '[data-testid="socialContext"]') return socialNode;
      if (selector.includes('[data-testid="placementTracking"]')) return promoted ? {} : null;
      if (selector.includes('[data-testid="replyingTo"]')) return reply ? {} : null;
      if (selector.includes('[data-testid="reply"]')) return {};
      return null;
    },
    querySelectorAll(selector) {
      if (selector === 'a[href*="/status/"]') return [permalink, ...nested, ...parent];
      if (selector === "a[href]") return [permalink, ...nested, ...parent, ...(replyProfile ? [replyProfile] : [])];
      if (selector === '[data-testid="tweetPhoto"] img') return photos;
      if (selector === "span, div") {
        const nodes = [];
        if (promotedText) {
          nodes.push({
            innerText: promotedText,
            textContent: promotedText,
            closest() { return null; },
          });
        }
        if (bodyText) {
          nodes.push({
            innerText: bodyText,
            textContent: bodyText,
            closest(selector) {
              return selector.includes('[data-testid="tweetText"]') ? {} : null;
            },
          });
        }
        if (bodyContainerText) {
          nodes.push({
            innerText: bodyContainerText,
            textContent: bodyContainerText,
            closest() { return null; },
            querySelector(selector) {
              return selector.includes('[data-testid="tweetText"]') ? {} : null;
            },
          });
        }
        return nodes;
      }
      return [];
    },
  };
}

function photoNode({ currentSrc = "", src = "", alt = "", statusId = null, statusUrl = null } = {}) {
  const statusAnchor = statusUrl === null && statusId === null ? null : {
    href: statusUrl || `https://x.com/author/status/${statusId}`,
  };
  return {
    currentSrc,
    src,
    alt,
    closest(selector) {
      return selector === 'a[href*="/status/"]' ? statusAnchor : null;
    },
  };
}

function photoArticleFixture(photoNodes) {
  const avatar = photoNode({
    src: "https://pbs.twimg.com/profile_images/avatar.jpg",
    alt: "avatar",
  });
  return {
    ownerDocument: { location: { origin: "https://x.com" } },
    querySelectorAll(selector) {
      if (selector === '[data-testid="tweetPhoto"] img') return photoNodes;
      if (selector === "img") return [avatar, ...photoNodes];
      return [];
    },
  };
}

function documentFixture({
  tweetText = "hello", platformText = "", challengeForm = false,
  loginControls = false,
  articles = [articleFixture("openai", "123")],
  url = "https://x.com/openai",
} = {}) {
  const platformNodes = platformText ? [{ innerText: platformText, textContent: platformText }] : [];
  return {
    location: { href: url, origin: "https://x.com" },
    documentElement: { scrollHeight: 2000 },
    body: { innerText: `${tweetText} ${platformText}` },
    querySelectorAll(selector) {
      if (selector === 'article[data-testid="tweet"]') return articles;
      if (selector.includes('[role="dialog"]')) return platformNodes;
      return [];
    },
    querySelector(selector) {
      if (selector.includes('form[action*="challenge"]')) return challengeForm ? {} : null;
      if (selector.includes('[data-testid="loginButton"]')) return loginControls ? {} : null;
      return null;
    },
  };
}

function observationFixture(postId, handle = "author") {
  return {
    url: `https://x.com/${handle}/status/${postId}`,
    post_id: String(postId),
    author_handle: handle,
    is_pinned: false,
    is_reply: false,
    is_repost: false,
    is_quote: false,
    is_promoted: false,
    parent_url: null,
    discovery_order: 0,
    photos: [],
  };
}

test("extracts only ordered outer-post tweet photos with normalized URLs", () => {
  const article = photoArticleFixture([
    photoNode({
      currentSrc: "https://pbs.twimg.com/media/one?name=small&format=jpg&width=1200",
      alt: "  first <photo>  ", statusId: "123",
    }),
    photoNode({
      currentSrc: "https://pbs.twimg.com/media/one?format=jpg&name=medium",
      alt: "responsive duplicate", statusId: "123",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/two?name=small&format=png&height=600",
      alt: "   ", statusId: "123",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/quote?format=jpg", alt: "quoted", statusId: "456",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/parent?format=jpg", alt: "reply parent", statusId: "789",
    }),
    photoNode({
      src: "https://pbs.twimg.com/profile_images/not-a-photo.jpg", alt: "avatar", statusId: "123",
    }),
    photoNode({
      src: "https://images.example/media/not-x.jpg", alt: "unrelated", statusId: "123",
    }),
  ]);

  assert.deepEqual(JSON.parse(JSON.stringify(api.extractPostPhotos(article, "123"))), [
    {
      position: 0,
      url: "https://pbs.twimg.com/media/one?format=jpg&name=large",
      alt_text: "first <photo>",
    },
    {
      position: 1,
      url: "https://pbs.twimg.com/media/two?format=png&name=large",
      alt_text: null,
    },
  ]);
});

test("canonicalizes safe photo escapes and rejects path separator smuggling", () => {
  const safeArticle = photoArticleFixture([
    photoNode({
      src: "https://pbs.twimg.com/media/%61bc?format=jpg",
      alt: "safe", statusId: "123",
    }),
  ]);

  assert.deepEqual(JSON.parse(JSON.stringify(api.extractPostPhotos(safeArticle, "123"))), [{
    position: 0,
    url: "https://pbs.twimg.com/media/abc?format=jpg&name=large",
    alt_text: "safe",
  }]);

  for (const src of [
    "https://pbs.twimg.com/media/abc/extra?format=jpg",
    "https://pbs.twimg.com/media/abc%2f..%2fsecret?format=jpg",
    "https://pbs.twimg.com/media/abc%5c..%5csecret?format=jpg",
    "https://pbs.twimg.com/media/%252e%252e%252fsecret?format=jpg",
    "https://pbs.twimg.com/media/abc%?format=jpg",
    " https://pbs.twimg.com/media/abc?format=jpg",
    "https://pbs.twimg.com/media/abc?format=jpg ",
    "https://pbs.twimg.com/\tmedia/abc?format=jpg",
    "https://pbs.twimg.com/media/abc\n?format=jpg",
    "https://pbs.twimg.com/media/abc?\rformat=jpg",
    "https://pbs.twimg.com/media/abc\0?format=jpg",
    "https://pbs.twimg.com/media/abc\x1f?format=jpg",
    "https://pbs.twimg.com/media/abc\x7f?format=jpg",
    "\ufeffhttps://pbs.twimg.com/media/abc?format=jpg",
    "https://pbs.twimg.com/media/abc?format=jpg\ufeff",
    "\u0085https://pbs.twimg.com/media/abc?format=jpg",
    "https://pbs.twimg.com/media/abc?format=jpg\u0085",
  ]) {
    const unsafeArticle = photoArticleFixture([
      photoNode({ src, alt: "unsafe", statusId: "123" }),
    ]);
    assert.deepEqual(
      JSON.parse(JSON.stringify(api.extractPostPhotos(unsafeArticle, "123"))),
      [],
    );
  }
});

test("extracts outer photos from exact status photo permalinks only", () => {
  const article = photoArticleFixture([
    photoNode({
      src: "https://pbs.twimg.com/media/one?format=jpg",
      alt: "one", statusUrl: "https://x.com/author/status/123/photo/1",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/quote?format=jpg",
      alt: "quote", statusUrl: "https://x.com/quoted/status/456/photo/1",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/two?format=jpg",
      alt: "two", statusUrl: "https://x.com/author/status/123/photo/2",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/extra?format=jpg",
      alt: "extra suffix", statusUrl: "https://x.com/author/status/123/photo/3/extra",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/zero?format=jpg",
      alt: "zero", statusUrl: "https://x.com/author/status/123/photo/0",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/three?format=jpg",
      alt: "three", statusUrl: "https://x.com/author/status/123/photo/3",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/malformed?format=%ZZ",
      alt: "malformed", statusUrl: "https://x.com/author/status/123/photo/4",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/four?format=jpg",
      alt: "four", statusUrl: "https://x.com/author/status/123/photo/4",
    }),
  ]);

  assert.deepEqual(JSON.parse(JSON.stringify(api.extractPostPhotos(article, "123"))), [
    { position: 0, url: "https://pbs.twimg.com/media/one?format=jpg&name=large", alt_text: "one" },
    { position: 1, url: "https://pbs.twimg.com/media/two?format=jpg&name=large", alt_text: "two" },
    { position: 2, url: "https://pbs.twimg.com/media/three?format=jpg&name=large", alt_text: "three" },
    { position: 3, url: "https://pbs.twimg.com/media/four?format=jpg&name=large", alt_text: "four" },
  ]);
});

test("caps outer-post photo manifests at four and truncates alt text", () => {
  const article = photoArticleFixture(Array.from({ length: 5 }, (_value, index) => photoNode({
    src: `https://pbs.twimg.com/media/${index}?format=jpg`,
    alt: index === 0 ? `  ${"x".repeat(1_005)}  ` : `photo ${index}`,
    statusId: "123",
  })));

  const photos = api.extractPostPhotos(article, "123");

  assert.equal(photos.length, 4);
  const summary = photos.map(({ position, url }) => ({ position, url }));
  assert.deepEqual(JSON.parse(JSON.stringify(summary)), [
    { position: 0, url: "https://pbs.twimg.com/media/0?format=jpg&name=large" },
    { position: 1, url: "https://pbs.twimg.com/media/1?format=jpg&name=large" },
    { position: 2, url: "https://pbs.twimg.com/media/2?format=jpg&name=large" },
    { position: 3, url: "https://pbs.twimg.com/media/3?format=jpg&name=large" },
  ]);
  assert.equal(photos[0].alt_text, "x".repeat(1_000));
});

test("normalizes a selected author's standalone post", () => {
  const value = api.normalizeObservation({
    url: "https://x.com/openai/status/123",
    postId: "123",
    authorHandle: "openai",
    pinned: false,
    reply: false,
    repost: false,
    quote: false,
    promoted: false,
    parentUrl: null,
  }, 0);
  assert.deepEqual(JSON.parse(JSON.stringify(value)), {
    url: "https://x.com/openai/status/123",
    post_id: "123",
    author_handle: "openai",
    is_pinned: false,
    is_reply: false,
    is_repost: false,
    is_quote: false,
    is_promoted: false,
    parent_url: null,
    discovery_order: 0,
    photos: [],
  });
  assert.equal(Object.isFrozen(value.photos), true);
});

test("rejects malformed or noncanonical cards", () => {
  assert.equal(api.normalizeObservation({ postId: "1" }, 0), null);
  assert.equal(api.normalizeObservation({
    url: "https://evil.example/openai/status/1", postId: "1", authorHandle: "openai",
    pinned: false, reply: false, repost: false, quote: false, promoted: false, parentUrl: null,
  }, 0), null);
  assert.equal(api.normalizeObservation({
    url: "https://x.com/openai/status/2", postId: "1", authorHandle: "openai",
    pinned: false, reply: false, repost: false, quote: false, promoted: false, parentUrl: null,
  }, 0), null);
  assert.equal(api.normalizeObservation({
    url: "https://x.com/openai/status/1", postId: "1", authorHandle: "openai",
    pinned: false, reply: true, repost: false, quote: false, promoted: false,
    parentUrl: "https://evil.example/parent/status/9",
  }, 0), null);
});

test("normalizes canonical X hosts and handle casing for posts and reply parents", () => {
  const value = api.normalizeObservation({
    url: "https://www.x.com/OpenAI/status/1", postId: "1", authorHandle: "OpenAI",
    pinned: false, reply: true, repost: false, quote: false, promoted: false,
    parentUrl: "https://www.x.com/Parent/status/9",
  }, 0);

  assert.equal(value.url, "https://x.com/openai/status/1");
  assert.equal(value.parent_url, "https://x.com/parent/status/9");
});

test("classification applies challenge, rate-limit, and signed-out precedence", () => {
  assert.equal(api.classifyPage({ url: "https://x.com/openai", challenge: true, rateLimited: true, loginControls: true, observations: [] }).x_state, "challenge");
  assert.equal(api.classifyPage({ url: "https://x.com/openai", rateLimited: true, loginControls: true, observations: [] }).x_state, "rate_limited");
  assert.equal(api.classifyPage({ url: "https://x.com/login", observations: [{ post_id: "1" }] }).x_state, "signed_out");
  assert.equal(api.classifyPage({ url: "https://x.com/openai", loginControls: true, observations: [] }).x_state, "signed_out");
  assert.equal(api.classifyPage({ url: "https://x.com/openai", loginControls: true, signedInMarker: true, observations: [] }).x_state, "signed_in");
});

test("a signed-in marker keeps a stray login link from flipping the page to signed out", () => {
  assert.equal(api.classifyPage({ url: "https://x.com/home", loginControls: true, observations: [] }).x_state, "signed_out");
  assert.equal(api.classifyPage({ url: "https://x.com/home", loginControls: true, signedInMarker: true, observations: [] }).x_state, "signed_in");
  assert.equal(api.documentXState({ querySelector: () => null, querySelectorAll: () => []}, 0), "signed_in");
});

test("classification validates the exact selected profile", () => {
  const sourceTarget = { kind: "source", handle: "openai" };
  const exact = api.classifyPage({ url: "https://x.com/OpenAI", observations: [], endReached: true }, sourceTarget);
  assert.equal(exact.page_supported, true);
  assert.equal(exact.end_reached, true);
  assert.equal(exact.x_state, "signed_in");
  assert.equal(api.classifyPage({
    url: "https://x.com/OpenAI#xfeed-owner=test-owner-nonce-0123456789abcdef",
    observations: [],
  }, sourceTarget).page_supported, true);
  assert.equal(api.classifyPage({ url: "https://x.com/openai/status/1", observations: [] }, sourceTarget).page_supported, false);
  assert.equal(api.classifyPage({ url: "https://www.x.com/other", observations: [] }, sourceTarget).page_supported, false);
  assert.equal(api.classifyPage({ url: "https://example.com/openai", observations: [] }, sourceTarget).page_supported, false);
});

test("classification accepts the exact home route only for verified home-feed kinds", () => {
  const home = { kind: "for_you", handle: null };
  const following = { kind: "following", handle: null };
  const sourceTarget = { kind: "source", handle: "openai" };
  assert.equal(api.classifyPage({ url: "https://x.com/home", observations: [] }, home).page_supported, true);
  assert.equal(api.classifyPage({ url: "https://x.com/home", observations: [] }, following).page_supported, true);
  assert.equal(api.classifyPage({
    url: "https://x.com/home#xfeed-owner=test-owner-nonce-0123456789abcdef", observations: [],
  }, home).page_supported, true);
  assert.equal(api.classifyPage({ url: "https://x.com/home/", observations: [] }, home).page_supported, false);
  assert.equal(api.classifyPage({ url: "https://x.com/home", observations: [] }, sourceTarget).page_supported, false);
  assert.equal(api.classifyPage({ url: "https://x.com/explore", observations: [] }, home).page_supported, false);
  assert.equal(api.classifyPage({ url: "https://example.com/home", observations: [] }, home).page_supported, false);
});

test("classification exposes the exact observation contract without leaking page data", () => {
  const result = api.classifyPage({ url: "https://x.com/openai", observations: [{
    url: "https://x.com/openai/status/7", post_id: "7", author_handle: "openai",
    is_pinned: true, is_reply: true, is_repost: true, is_quote: true,
    is_promoted: false, parent_url: "https://x.com/parent/status/6",
    discovery_order: 0, photos: [], secret: "do not expose",
  }], cookies: "secret" }, { kind: "source", handle: "openai" });
  assert.deepEqual(JSON.parse(JSON.stringify(result.observations[0])), {
    url: "https://x.com/openai/status/7", post_id: "7", author_handle: "openai",
    is_pinned: true, is_reply: true, is_repost: true, is_quote: true,
    is_promoted: false, parent_url: "https://x.com/parent/status/6", discovery_order: 0, photos: [],
  });
  assert.equal("cookies" in result, false);
});

for (const phrase of ["try again later", "rate limit", "security check"]) {
  test(`tweet text containing ${phrase} does not classify the page unsafe`, () => {
    context.window = { scrollY: 0, innerHeight: 800 };
    const result = api.collectPage(documentFixture({ tweetText: phrase }), { kind: "source", handle: "openai" });
    assert.equal(result.x_state, "signed_in");
    assert.equal(result.observations.length, 1);
    assert.equal(result.observations[0].post_id, "123");
  });
}

test("scoped X platform UI classifies rate limits and challenges", () => {
  context.window = { scrollY: 0, innerHeight: 800 };
  const target = { kind: "source", handle: "openai" };
  assert.equal(api.collectPage(documentFixture({ platformText: "Try again later" }), target).x_state, "rate_limited");
  assert.equal(api.collectPage(documentFixture({ platformText: "Security check" }), target).x_state, "challenge");
  assert.equal(api.collectPage(documentFixture({ challengeForm: true }), target).x_state, "challenge");
});

test("home extraction retains mixed authors and classifies post, repost, reply, and quote in DOM order", () => {
  context.window = { scrollY: 0, innerHeight: 800 };
  const target = { kind: "for_you", handle: null };
  const result = api.collectPage(documentFixture({
    url: "https://x.com/home#xfeed-owner=test-owner-nonce-0123456789abcdef",
    articles: [
      articleFixture("alpha", "1"),
      articleFixture("beta", "2", { socialText: "Someone reposted" }),
      articleFixture("gamma", "3", {
        reply: true, parentUrl: "https://x.com/parent/status/30",
      }),
      articleFixture("delta", "4", { nestedHandle: "quoted", nestedPostId: "40" }),
    ],
  }), target);

  assert.deepEqual(
    JSON.parse(JSON.stringify(result.observations)),
    [
      {
        url: "https://x.com/alpha/status/1", post_id: "1", author_handle: "alpha",
        is_pinned: false, is_reply: false, is_repost: false, is_quote: false,
        is_promoted: false, parent_url: null, discovery_order: 0, photos: [],
      },
      {
        url: "https://x.com/beta/status/2", post_id: "2", author_handle: "beta",
        is_pinned: false, is_reply: false, is_repost: true, is_quote: false,
        is_promoted: false, parent_url: null, discovery_order: 1, photos: [],
      },
      {
        url: "https://x.com/gamma/status/3", post_id: "3", author_handle: "gamma",
        is_pinned: false, is_reply: true, is_repost: false, is_quote: false,
        is_promoted: false, parent_url: "https://x.com/parent/status/30", discovery_order: 2, photos: [],
      },
      {
        url: "https://x.com/delta/status/4", post_id: "4", author_handle: "delta",
        is_pinned: false, is_reply: false, is_repost: false, is_quote: true,
        is_promoted: false, parent_url: null, discovery_order: 3, photos: [],
      },
    ],
  );
});

test("home extraction excludes structural and exact-text promotions, pinned cards, and malformed cards", () => {
  context.window = { scrollY: 0, innerHeight: 800 };
  const result = api.collectPage(documentFixture({
    url: "https://x.com/home",
    articles: [
      articleFixture("structural_ad", "1", { promoted: true }),
      articleFixture("text_ad", "2", { promotedText: "  Promoted  " }),
      articleFixture("short_ad", "3", { promotedText: "Ad" }),
      articleFixture("not_ad", "4", { promotedText: "Ad choices" }),
      articleFixture("pinned", "5", { socialText: "Pinned" }),
      articleFixture("bad", "6", { permalinkUrl: "https://evil.example/bad/status/6" }),
    ],
  }), { kind: "for_you", handle: null });

  assert.deepEqual(JSON.parse(JSON.stringify(result.observations.map((value) => value.post_id))), ["4"]);
});

test("home extraction detects structural promotion markers on the article and its ancestors", () => {
  context.window = { scrollY: 0, innerHeight: 800 };
  const result = api.collectPage(documentFixture({
    url: "https://x.com/home",
    articles: [
      articleFixture("root_ad", "1", { promotedOnArticle: true }),
      articleFixture("ancestor_ad", "2", { promotedAncestor: true }),
      articleFixture("organic", "3"),
    ],
  }), { kind: "for_you", handle: null });

  assert.deepEqual(
    JSON.parse(JSON.stringify(result.observations.map((value) => value.post_id))),
    ["3"],
  );
});

test("exact-text promotion fallback ignores tweet and card content", () => {
  context.window = { scrollY: 0, innerHeight: 800 };
  const result = api.collectPage(documentFixture({
    url: "https://x.com/home",
    articles: [
      articleFixture("body_ad", "1", { bodyText: "Ad" }),
      articleFixture("body_promoted", "2", { bodyText: "Promoted" }),
      articleFixture("body_container", "3", { bodyContainerText: "Ad" }),
      articleFixture("label_ad", "4", { promotedText: "Ad" }),
      articleFixture("label_promoted", "5", { promotedText: "Promoted" }),
    ],
  }), { kind: "for_you", handle: null });

  assert.deepEqual(
    JSON.parse(JSON.stringify(result.observations.map((value) => value.post_id))),
    ["1", "2", "3"],
  );
});

test("reply parent URL is emitted only for one unique explicitly marked candidate", () => {
  context.window = { scrollY: 0, innerHeight: 800 };
  const oneParent = "https://x.com/parent/status/10";
  const result = api.collectPage(documentFixture({
    url: "https://x.com/home",
    articles: [
      articleFixture("unique", "1", {
        reply: true,
        parentUrls: [oneParent, oneParent],
      }),
      articleFixture("ambiguous", "2", {
        reply: true,
        parentUrls: [
          "https://x.com/first/status/20",
          "https://x.com/second/status/21",
        ],
      }),
    ],
  }), { kind: "for_you", handle: null });

  assert.deepEqual(
    JSON.parse(JSON.stringify(result.observations.map((value) => ({
      post_id: value.post_id,
      is_reply: value.is_reply,
      is_quote: value.is_quote,
      parent_url: value.parent_url,
    })))),
    [
      { post_id: "1", is_reply: true, is_quote: false, parent_url: oneParent },
      { post_id: "2", is_reply: true, is_quote: false, parent_url: null },
    ],
  );
});

test("source extraction keeps only original selected-author cards", () => {
  context.window = { scrollY: 0, innerHeight: 800 };
  const result = api.collectPage(documentFixture({
    articles: [
      articleFixture("other", "1"),
      articleFixture("openai", "2", { socialText: "Pinned" }),
      articleFixture("openai", "3", { socialText: "Someone reposted" }),
      articleFixture("openai", "4", { reply: true }),
      articleFixture("openai", "5", { nestedPostId: "50" }),
      articleFixture("openai", "6", { promoted: true }),
      articleFixture("openai", "7"),
    ],
  }), { kind: "source", handle: "openai" });

  assert.deepEqual(JSON.parse(JSON.stringify(result.observations.map((value) => value.post_id))), ["7"]);
});

test("source posts are not excluded by the reply action button", () => {
  context.window = { scrollY: 0, innerHeight: 800 };
  const result = api.collectPage(documentFixture({
    articles: [
      articleFixture("openai", "1"),
      articleFixture("openai", "2", { reply: true }),
    ],
  }), { kind: "source", handle: "openai" });

  assert.deepEqual(JSON.parse(JSON.stringify(result.observations.map((value) => value.post_id))), ["1"]);
});

test("home extraction keeps the first duplicate ID and accepts fifty observations", () => {
  context.window = { scrollY: 0, innerHeight: 800 };
  const articles = [
    articleFixture("first", "1"),
    articleFixture("duplicate", "1"),
    ...Array.from({ length: 49 }, (_value, index) => articleFixture("author", String(index + 2))),
  ];
  const result = api.collectPage(documentFixture({
    url: "https://x.com/home", articles,
  }), { kind: "for_you", handle: null });

  assert.equal(result.observations.length, 50);
  assert.equal(result.observations[0].author_handle, "first");
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.observations.map((value) => value.discovery_order))),
    Array.from({ length: 50 }, (_value, index) => index),
  );
});

test("scans past more than thirty excluded cards before counting qualifying posts", () => {
  context.window = { scrollY: 0, innerHeight: 800 };
  const excluded = Array.from(
    { length: 35 },
    (_value, index) => articleFixture("other", String(index + 1)),
  );
  const articles = [
    ...excluded,
    articleFixture("openai", "100", { social: true }),
    articleFixture("openai", "101", { nestedPostId: "999" }),
    articleFixture("openai", "200"),
    articleFixture("openai", "200"),
    articleFixture("openai", "201"),
  ];

  const result = api.collectPage(documentFixture({ articles }), { kind: "source", handle: "openai" });

  assert.equal(api.RAW_SCAN_CEILING, 300);
  assert.equal(result.raw_scanned, 40);
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.observations.map((value) => ({
      id: value.post_id, order: value.discovery_order,
    })))),
    [{ id: "200", order: 0 }, { id: "201", order: 1 }],
  );
});

test("raw scan ceiling stops before a qualifying card beyond three hundred observations", () => {
  context.window = { scrollY: 0, innerHeight: 800 };
  const articles = [
    ...Array.from(
      { length: 300 },
      (_value, index) => articleFixture("other", String(index + 1)),
    ),
    articleFixture("openai", "999"),
  ];

  const result = api.collectPage(documentFixture({ articles }), { kind: "source", handle: "openai" });

  assert.equal(result.raw_scanned, 300);
  assert.equal(result.observations.length, 0);
});

test("collection sends only prequalified observations and reaches the accepted limit", async () => {
  context.window = { scrollY: 0, innerHeight: 800 };
  const page = api.collectPage(documentFixture({
    articles: [
      ...Array.from(
        { length: 35 },
        (_value, index) => articleFixture("other", String(index + 1)),
      ),
      articleFixture("openai", "300", { social: true }),
      articleFixture("openai", "400"),
      articleFixture("openai", "401"),
    ],
  }), { kind: "source", handle: "openai" });
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  const progress = [];
  const sandbox = {
    document: {},
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback) { callback(); return 1; },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          if (message.type === "is-owned-tab") return { owned: false };
          if (message.type === "collection-progress") {
            progress.push(message.observations);
            return { cancelled: false };
          }
          return {};
        },
      },
    },
    XFeedContent: {
      RAW_SCAN_CEILING: 300,
      documentXState() { return "signed_in"; },
      collectPage() { return page; },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);

  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-1", targetKind: "source", handle: "openai", maximum: 2,
      timeout_ms: 45_000,
    }, {}, resolve), true);
  });

  assert.equal(result.reason, "limit");
  assert.deepEqual(
    JSON.parse(JSON.stringify(progress.flat().map((value) => value.post_id))),
    ["400", "401"],
  );
});

async function runBoundedCollection({
  targetKind,
  handle,
  maximum,
  pages,
  progressResponse = { cancelled: false },
}) {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  let pageIndex = 0;
  const progress = [];
  const targets = [];
  const sandbox = {
    document: {},
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback) { callback(); return 1; },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          if (message.type === "is-owned-tab") return { owned: false };
          if (message.type === "collection-progress") {
            progress.push(message.observations);
            return progressResponse;
          }
          return {};
        },
      },
    },
    XFeedContent: {
      RAW_SCAN_CEILING: 300,
      documentXState() { return "signed_in"; },
      async selectAndVerifyHomeFeed() {},
      findHomeFeedTab() { return { selected: true }; },
      collectPage(_document, target) {
        targets.push(target);
        return pages[Math.min(pageIndex++, pages.length - 1)];
      },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);
  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-bounded", targetKind, handle, maximum,
      timeout_ms: 45_000,
    }, {}, resolve), true);
  });
  return { result, progress, targets };
}

test("home collection emits fifty unique observations and terminates at the limit", async () => {
  const page = {
    observations: Array.from({ length: 50 }, (_value, index) => observationFixture(index + 1)),
    raw_scanned: 50,
    x_state: "signed_in",
    page_supported: true,
    end_reached: false,
  };
  const { result, progress, targets } = await runBoundedCollection({
    targetKind: "for_you", handle: null, maximum: 50, pages: [page],
  });

  assert.equal(result.reason, "limit");
  assert.deepEqual(progress.flat().map((value) => value.post_id),
    Array.from({ length: 50 }, (_value, index) => String(index + 1)));
  assert.deepEqual(JSON.parse(JSON.stringify(targets)), [{ kind: "for_you", handle: null }]);
});

test("Following collection emits fifty unique observations under its own target kind", async () => {
  const page = {
    observations: Array.from({ length: 50 }, (_value, index) => observationFixture(index + 1)),
    raw_scanned: 50,
    x_state: "signed_in",
    page_supported: true,
    end_reached: false,
  };
  const { result, progress, targets } = await runBoundedCollection({
    targetKind: "following", handle: null, maximum: 50, pages: [page],
  });

  assert.equal(result.reason, "limit");
  assert.equal(progress.flat().length, 50);
  assert.deepEqual(JSON.parse(JSON.stringify(targets)), [{ kind: "following", handle: null }]);
});

async function runTimelineTransitionCollection({
  counterMutationAt = null,
  replacementAt,
  timeoutMs,
}) {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  let now = 0;
  let counterMutated = false;
  let replaced = false;
  const timelinePermalink = { href: "https://x.com/oldfeed/status/1001" };
  const timelineTime = {
    closest(selector) {
      assert.equal(selector, 'a[href*="/status/"]');
      return timelinePermalink;
    },
  };
  const timelineCard = {
    textContent: "Old feed post\n10 likes",
    querySelector(selector) {
      assert.equal(selector, "time");
      return timelineTime;
    },
  };
  const state = { selectedIndex: 0, cards: [timelineCard] };
  const document = homeDocument({
    labels: ["For you", "Following"],
    state,
    onClick(index) { state.selectedIndex = index; },
  });
  const originalQuerySelector = document.querySelector.bind(document);
  const primaryColumn = {
    querySelectorAll(selector) {
      assert.equal(selector, 'article[data-testid="tweet"]');
      return state.cards;
    },
  };
  document.querySelector = (selector) => selector === '[data-testid="primaryColumn"]'
    ? primaryColumn : originalQuerySelector(selector);
  const progress = [];
  const extracted = [];
  const sandbox = {
    Date: { now() { return now; } },
    document,
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback, delay) {
      now += delay;
      if (!counterMutated && counterMutationAt !== null && now >= counterMutationAt) {
        counterMutated = true;
        timelineCard.textContent = "Old feed post\n11 likes";
      }
      if (!replaced && replacementAt !== null && now >= replacementAt) {
        replaced = true;
        timelinePermalink.href = "https://x.com/newfeed/status/2002";
        timelineCard.textContent = "New feed post\n3 likes";
      }
      callback();
      return now;
    },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          if (message.type === "is-owned-tab") return { owned: false };
          if (message.type === "collection-progress") {
            progress.push(message.observations);
            return { cancelled: false };
          }
          return {};
        },
      },
    },
    XFeedContent: {
      RAW_SCAN_CEILING: 300,
      documentXState() { return "signed_in"; },
      findHomeFeedTab: api.findHomeFeedTab,
      selectAndVerifyHomeFeed: api.selectAndVerifyHomeFeed,
      collectPage() {
        const postId = timelinePermalink.href.endsWith("/2002") ? "2002" : "1001";
        extracted.push(postId);
        return {
          observations: [observationFixture(postId)], raw_scanned: 1,
          x_state: "signed_in", page_supported: true, end_reached: false,
        };
      },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);

  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-delayed", targetKind: "following", handle: null,
      maximum: 1, timeout_ms: timeoutMs,
    }, {}, resolve), true);
  });

  return { result, extracted, progress, now };
}

test("ignores an old-card counter mutation until post identity transitions", async () => {
  const { result, extracted, progress, now } = await runTimelineTransitionCollection({
    counterMutationAt: 200,
    replacementAt: 1_600,
    timeoutMs: 5_000,
  });

  assert.equal(result.reason, "limit");
  assert.deepEqual(extracted, ["2002"]);
  assert.ok(now > 1_600);
  assert.deepEqual(progress.flat().map((item) => item.post_id), ["2002"]);
});

test("times out clearly when a selected home timeline never transitions", async () => {
  const { result, extracted, progress } = await runTimelineTransitionCollection({
    replacementAt: null,
    timeoutMs: 400,
  });

  assert.equal(result.reason, "error");
  assert.match(result.diagnostic, /Following timeline did not transition and stabilize/);
  assert.deepEqual(extracted, []);
  assert.deepEqual(progress, []);
});

test("source collection remains capped at thirty when a larger maximum is requested", async () => {
  const page = {
    observations: Array.from({ length: 50 }, (_value, index) => observationFixture(index + 1, "openai")),
    raw_scanned: 50,
    x_state: "signed_in",
    page_supported: true,
    end_reached: false,
  };
  const { result, progress, targets } = await runBoundedCollection({
    targetKind: "source", handle: "openai", maximum: 50, pages: [page],
  });

  assert.equal(result.reason, "limit");
  assert.equal(progress.flat().length, 30);
  assert.deepEqual(JSON.parse(JSON.stringify(targets)), [{ kind: "source", handle: "openai" }]);
});

test("collection preserves cancellation returned by progress", async () => {
  const page = {
    observations: [observationFixture("1")], raw_scanned: 1,
    x_state: "signed_in", page_supported: true, end_reached: false,
  };
  const { result } = await runBoundedCollection({
    targetKind: "for_you", handle: null, maximum: 50, pages: [page],
    progressResponse: { cancelled: true },
  });

  assert.deepEqual(JSON.parse(JSON.stringify(result)), { cancelled: true });
});

test("collection preserves the raw scan ceiling", async () => {
  const page = {
    observations: [], raw_scanned: 300,
    x_state: "signed_in", page_supported: true, end_reached: false,
  };
  const { result, progress } = await runBoundedCollection({
    targetKind: "for_you", handle: null, maximum: 50, pages: [page],
  });

  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    reason: "exhausted", diagnostic: "Raw scan ceiling of 300 observations reached",
  });
  assert.equal(progress.length, 1);
});

test("collection preserves the three-pass no-progress bound", async () => {
  const page = {
    observations: [], raw_scanned: 1,
    x_state: "signed_in", page_supported: true, end_reached: false,
  };
  const { result, progress } = await runBoundedCollection({
    targetKind: "for_you", handle: null, maximum: 50, pages: [page],
  });

  assert.equal(result.reason, "no_progress");
  assert.match(result.diagnostic, /seen=0 scanned=3 stalls=3 advances=0/);
  assert.equal(progress.length, 3);
});

test("collection scrolls an inner timeline container to reveal more posts", async () => {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  let pageIndex = 0;
  const progress = [];
  const scrollerState = { scrollTop: 0, clientHeight: 800 };
  const scrollerElement = {
    get clientHeight() { return scrollerState.clientHeight; },
    get scrollHeight() { return scrollerState.scrollTop + 1_800; },
    get scrollTop() { return scrollerState.scrollTop; },
    set scrollTop(value) { scrollerState.scrollTop = value; },
  };
  const article = {
    parentElement: scrollerElement,
  };
  const sandbox = {
    document: {
      querySelector(selector) {
        if (selector === 'article[data-testid="tweet"]') return article;
        return null;
      },
      documentElement: { scrollHeight: 5000 },
      scrollingElement: null,
    },
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback) { callback(); return 1; },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          if (message.type === "is-owned-tab") return { owned: false };
          if (message.type === "collection-progress") {
            progress.push(message.observations);
            return { cancelled: false };
          }
          return {};
        },
      },
    },
    XFeedContent: {
      RAW_SCAN_CEILING: 300,
      documentXState() { return "signed_in"; },
      findHomeFeedTab() { return { selected: true }; },
      selectAndVerifyHomeFeed() {},
      collectPage() {
        pageIndex += 1;
        if (pageIndex === 1) {
          return {
            observations: [observationFixture("1")],
            raw_scanned: 1, x_state: "signed_in", page_supported: true,
            end_reached: false,
          };
        }
        return {
          observations: pageIndex <= 11 ? [observationFixture(String(pageIndex))] : [],
          raw_scanned: 1, x_state: "signed_in", page_supported: true,
          end_reached: false,
        };
      },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);

  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-scroller", targetKind: "for_you", handle: null,
      maximum: 5, timeout_ms: 45_000,
    }, {}, resolve), true);
  });

  assert.equal(result.reason, "limit");
  assert.deepEqual(
    progress.flat().map((value) => value.post_id),
    ["1", "2", "3", "4", "5"],
  );
  assert.ok(scrollerState.scrollTop > 0);
});

test("collection keeps scrolling when a tall non-scrollable document element is present", async () => {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  let pageIndex = 0;
  const progress = [];
  const scrollerState = { scrollTop: 0, clientHeight: 800 };
  const scrollerElement = {
    get clientHeight() { return scrollerState.clientHeight; },
    get scrollHeight() { return scrollerState.scrollTop + 1_800; },
    get scrollTop() { return scrollerState.scrollTop; },
    set scrollTop(value) { scrollerState.scrollTop = value; },
  };
  const article = {
    parentElement: scrollerElement,
  };
  const sandbox = {
    document: {
      querySelector(selector) {
        if (selector === 'article[data-testid="tweet"]') return article;
        if (selector === '[data-testid="primaryColumn"]') return { parentElement: scrollerElement };
        return null;
      },
      documentElement: { scrollHeight: 50_000, clientHeight: 800, scrollTop: 0 },
      body: {},
      scrollingElement: null,
    },
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback) { callback(); return 1; },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          if (message.type === "is-owned-tab") return { owned: false };
          if (message.type === "collection-progress") {
            progress.push(message.observations);
            return { cancelled: false };
          }
          return {};
        },
      },
    },
    XFeedContent: {
      RAW_SCAN_CEILING: 300,
      documentXState() { return "signed_in"; },
      findHomeFeedTab() { return { selected: true }; },
      selectAndVerifyHomeFeed() {},
      collectPage() {
        pageIndex += 1;
        return {
          observations: pageIndex <= 11 ? [observationFixture(String(pageIndex))] : [],
          raw_scanned: 1, x_state: "signed_in", page_supported: true,
          end_reached: false,
        };
      },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);

  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-tall-doc", targetKind: "source", handle: "google",
      maximum: 10, timeout_ms: 45_000,
    }, {}, resolve), true);
  });

  assert.equal(result.reason, "limit");
  assert.deepEqual(
    progress.flat().map((value) => value.post_id),
    ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"],
  );
  assert.ok(scrollerState.scrollTop > 0);
});

test("probe passes target metadata into page collection", async () => {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  const targets = [];
  const events = [];
  const sandbox = {
    document: {}, window: {}, setInterval() {},
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) { return message.type === "is-owned-tab" ? { owned: false } : {}; },
      },
    },
    XFeedContent: {
      documentXState() { return "signed_in"; },
      async selectAndVerifyHomeFeed() { events.push("verified"); },
      collectPage(_document, target) {
        events.push("extracted");
        targets.push(target);
        return { page_supported: true };
      },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);
  const response = await new Promise((resolve) => {
    assert.equal(listener({
      type: "probe", targetKind: "for_you", handle: null, timeout_ms: 45_000,
    }, {}, resolve), true);
  });
  assert.deepEqual(JSON.parse(JSON.stringify(response)), { page_supported: true });
  assert.deepEqual(JSON.parse(JSON.stringify(targets)), [{ kind: "for_you", handle: null }]);
  assert.deepEqual(events, ["verified", "extracted"]);
});

test("collection reports an unsupported target as an unexpected collection page", async () => {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  const sandbox = {
    document: {},
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback) { callback(); return 1; },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          if (message.type === "is-owned-tab") return { owned: false };
          return {};
        },
      },
    },
    XFeedContent: {
      RAW_SCAN_CEILING: 300,
      documentXState() { return "signed_in"; },
      async selectAndVerifyHomeFeed() {},
      findHomeFeedTab() { return { selected: true }; },
      collectPage() { return {
      observations: [], raw_scanned: 0, x_state: "signed_in",
      page_supported: false, end_reached: false,
    }; } },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);

  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-home", targetKind: "for_you", handle: null,
      maximum: 50, timeout_ms: 45_000,
    }, {}, resolve), true);
  });

  assert.deepEqual(
    JSON.parse(JSON.stringify(result)),
    { reason: "error", diagnostic: "unexpected collection page" },
  );
});

test("rejects an unverified home-tab structure without collecting observations", async () => {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  let progressCalls = 0;
  let extractionCalls = 0;
  const sandbox = {
    document: homeDocument({ labels: ["Explore", "Popular"] }),
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback) { callback(); return 1; },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          if (message.type === "is-owned-tab") return { owned: false };
          if (message.type === "collection-progress") progressCalls += 1;
          return { cancelled: false };
        },
      },
    },
    XFeedContent: {
      RAW_SCAN_CEILING: 300,
      documentXState() { return "signed_in"; },
      findHomeFeedTab: api.findHomeFeedTab,
      selectAndVerifyHomeFeed: api.selectAndVerifyHomeFeed,
      collectPage() {
        extractionCalls += 1;
        return {
          observations: [], raw_scanned: 0, x_state: "signed_in",
          page_supported: true, end_reached: false,
        };
      },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);

  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-home", targetKind: "following", handle: null,
      maximum: 50, timeout_ms: 45_000,
    }, {}, resolve), true);
  });

  assert.match(result.diagnostic, /Following tab could not be verified/);
  assert.equal(extractionCalls, 0);
  assert.equal(progressCalls, 0);
});

test("unsafe X state takes precedence over an unverified home-tab structure", async () => {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  let selectionCalls = 0;
  let extractionCalls = 0;
  const sandbox = {
    document: homeDocument({ labels: ["Explore", "Popular"] }),
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback) { callback(); return 1; },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          return message.type === "is-owned-tab" ? { owned: false } : { cancelled: false };
        },
      },
    },
    XFeedContent: {
      RAW_SCAN_CEILING: 300,
      documentXState() { return "challenge"; },
      async selectAndVerifyHomeFeed() { selectionCalls += 1; },
      collectPage() { extractionCalls += 1; },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);

  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-home", targetKind: "following", handle: null,
      maximum: 50, timeout_ms: 45_000,
    }, {}, resolve), true);
  });

  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    reason: "error",
    diagnostic: "X presented an account challenge in Opera GX",
    x_state: "challenge",
  });
  assert.equal(selectionCalls, 0);
  assert.equal(extractionCalls, 0);
});

test("home-tab selection stops at the collection deadline without reporting progress", async () => {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  let now = 0;
  let progressCalls = 0;
  let extractionCalls = 0;
  const sandbox = {
    Date: { now() { return now; } },
    document: homeDocument({ labels: ["For you", "Following"], selectedIndex: 0 }),
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback, delay) { now += delay; callback(); return 1; },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          if (message.type === "is-owned-tab") return { owned: false };
          if (message.type === "collection-progress") progressCalls += 1;
          return { cancelled: false };
        },
      },
    },
    XFeedContent: {
      RAW_SCAN_CEILING: 300,
      documentXState() { return "signed_in"; },
      selectAndVerifyHomeFeed: api.selectAndVerifyHomeFeed,
      collectPage() { extractionCalls += 1; },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);

  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-home", targetKind: "following", handle: null,
      maximum: 50, timeout_ms: 100,
    }, {}, resolve), true);
  });

  assert.equal(result.reason, "error");
  assert.match(result.diagnostic, /selection timed out/);
  assert.equal(extractionCalls, 0);
  assert.equal(progressCalls, 0);
});

test("home-tab selection honors cancellation before extraction", async () => {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  let pendingTimer;
  let progressCalls = 0;
  let extractionCalls = 0;
  const sandbox = {
    document: homeDocument({ labels: ["For you", "Following"], selectedIndex: 0 }),
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback) { pendingTimer = callback; return 1; },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          if (message.type === "is-owned-tab") return { owned: false };
          if (message.type === "collection-progress") progressCalls += 1;
          return { cancelled: false };
        },
      },
    },
    XFeedContent: {
      RAW_SCAN_CEILING: 300,
      documentXState() { return "signed_in"; },
      selectAndVerifyHomeFeed: api.selectAndVerifyHomeFeed,
      collectPage() { extractionCalls += 1; },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);

  const resultPromise = new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-home", targetKind: "following", handle: null,
      maximum: 50, timeout_ms: 45_000,
    }, {}, resolve), true);
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(typeof pendingTimer, "function");
  assert.equal(listener({ type: "cancel" }, {}, () => {}), false);
  pendingTimer();

  assert.deepEqual(JSON.parse(JSON.stringify(await resultPromise)), { cancelled: true });
  assert.equal(extractionCalls, 0);
  assert.equal(progressCalls, 0);
});

test("rejects a home-tab selection change after settling before extraction", async () => {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  let timerCalls = 0;
  let extractionCalls = 0;
  const progressReports = [];
  const state = { selectedIndex: 1 };
  const sandbox = {
    document: homeDocument({ labels: ["For you", "Following"], state }),
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback) {
      timerCalls += 1;
      if (timerCalls === 2) state.selectedIndex = 0;
      callback();
      return timerCalls;
    },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          if (message.type === "is-owned-tab") return { owned: false };
          if (message.type === "collection-progress") {
            progressReports.push(message.observations);
          }
          return { cancelled: false };
        },
      },
    },
    XFeedContent: {
      RAW_SCAN_CEILING: 300,
      documentXState() { return "signed_in"; },
      findHomeFeedTab: api.findHomeFeedTab,
      selectAndVerifyHomeFeed: api.selectAndVerifyHomeFeed,
      collectPage() {
        extractionCalls += 1;
        return {
          observations: [observationFixture("1")], raw_scanned: 1,
          x_state: "signed_in", page_supported: true, end_reached: false,
        };
      },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);

  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-home", targetKind: "following", handle: null,
      maximum: 50, timeout_ms: 45_000,
    }, {}, resolve), true);
  });

  assert.match(result.diagnostic, /Following tab selection was lost/);
  assert.equal(extractionCalls, 0);
  assert.equal(progressReports.length, 0);
});

for (const [xState, reason, diagnostic] of [
  ["signed_out", "login_wall", "X is signed out in Opera GX; sign in and reconnect"],
  ["challenge", "error", "X presented an account challenge in Opera GX"],
  ["rate_limited", "error", "X rate-limited the Opera GX session"],
]) {
  test(`collection preserves actionable ${xState} state mid-run`, async () => {
    const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
    let listener;
    let progressCalls = 0;
    const sandbox = {
      document: {},
      window: { innerHeight: 800, scrollBy() {} },
      setInterval() {},
      setTimeout(callback) { callback(); return 1; },
      chrome: {
        storage: { local: { async get() { return {}; } } },
        runtime: {
          onMessage: { addListener(value) { listener = value; } },
          async sendMessage(message) {
            if (message.type === "is-owned-tab") return { owned: false };
            if (message.type === "collection-progress") progressCalls += 1;
            return { cancelled: false };
          },
        },
      },
      XFeedContent: {
        RAW_SCAN_CEILING: 300,
        documentXState() { return "signed_in"; },
        collectPage() { return {
        observations: [], raw_scanned: 1, x_state: xState,
        page_supported: true, end_reached: false,
      }; } },
    };
    vm.createContext(sandbox);
    vm.runInContext(contentSource, sandbox);

    const result = await new Promise((resolve) => {
      assert.equal(listener({
        type: "collect", jobId: "job-1", targetKind: "source", handle: "openai", maximum: 30,
        timeout_ms: 45_000,
      }, {}, resolve), true);
    });

    assert.deepEqual(
      JSON.parse(JSON.stringify(result)),
      { reason, diagnostic, x_state: xState },
    );
    assert.equal(progressCalls, 0);
  });
}

async function runTimedCollection(timeoutMs, clockStep) {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  let now = 0;
  let nextPostId = 1;
  let progressCalls = 0;
  const sandbox = {
    Date: { now() { const value = now; now += clockStep; return value; } },
    document: {},
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback) { callback(); return 1; },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          if (message.type === "is-owned-tab") return { owned: false };
          if (message.type === "collection-progress") progressCalls += 1;
          return { cancelled: false };
        },
      },
    },
    XFeedContent: {
      RAW_SCAN_CEILING: 300,
      documentXState() { return "signed_in"; },
      collectPage() {
      const postId = String(nextPostId++);
      return {
        observations: [{
          url: `https://x.com/openai/status/${postId}`,
          post_id: postId,
          author_handle: "openai",
          is_pinned: false,
          is_reply: false,
          is_repost: false,
          is_quote: false,
          is_promoted: false,
          parent_url: null,
          discovery_order: 0,
        }],
        raw_scanned: 1,
        x_state: "signed_in",
        page_supported: true,
        end_reached: false,
      };
    } },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);
  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-1", targetKind: "source", handle: "openai", maximum: 30,
      timeout_ms: timeoutMs,
    }, {}, resolve), true);
  });
  return { result, progressCalls };
}

test("collection uses a shorter relative timeout and caps it at forty-five seconds", async () => {
  const short = await runTimedCollection(1_000, 600);
  const capped = await runTimedCollection(60_000, 14_000);

  assert.equal(short.result.reason, "timeout");
  assert.equal(short.progressCalls, 1);
  assert.equal(capped.result.reason, "timeout");
  assert.equal(capped.progressCalls, 3);
});

test("collection reports a bridge progress failure as an error", async () => {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  const observation = {
    url: "https://x.com/openai/status/7", post_id: "7", author_handle: "openai",
    is_pinned: false, is_reply: false, is_repost: false, is_quote: false,
    is_promoted: false, parent_url: null, discovery_order: 0,
  };
  const sandbox = {
    document: {},
    window: { innerHeight: 800, scrollBy() {} },
    setInterval() {},
    setTimeout(callback) { callback(); return 1; },
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          if (message.type === "is-owned-tab") return { owned: false };
          if (message.type === "collection-progress") return { error: "bridge offline" };
          return {};
        },
      },
    },
    XFeedContent: {
      documentXState() { return "signed_in"; },
      collectPage() { return {
      observations: [observation], raw_scanned: 1,
      x_state: "signed_in", page_supported: true, end_reached: false,
    }; } },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);
  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "collect", jobId: "job-1", targetKind: "source", handle: "openai", maximum: 30,
      timeout_ms: 45_000,
    }, {}, resolve), true);
  });
  assert.equal(result.reason, "error");
  assert.match(result.diagnostic, /bridge offline/);
});

test("exact-post resolution returns an empty successful manifest for a text-only post", () => {
  const result = api.resolvePostPhotos(documentFixture({
    url: "https://x.com/openai/status/123#xfeed-owner=test-owner-nonce",
    articles: [articleFixture("openai", "123")],
  }), "123");

  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    reason: "exhausted",
    diagnostic: null,
    photos: [],
  });
});

test("exact-post resolution finds only the expected article and reuses photo filtering order", () => {
  const expectedPhotos = [
    photoNode({
      currentSrc: "https://pbs.twimg.com/media/first?format=jpg&name=small",
      alt: " First ",
      statusId: "123",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/quoted?format=jpg",
      alt: "quote",
      statusId: "999",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/second?format=png&name=medium",
      alt: "",
      statusId: "123",
    }),
    photoNode({
      src: "https://pbs.twimg.com/media/first?name=large&format=jpg",
      alt: "duplicate",
      statusId: "123",
    }),
  ];
  const expected = articleFixture("openai", "123", { photos: expectedPhotos });
  expected.ownerDocument = { location: { origin: "https://x.com" } };

  const result = api.resolvePostPhotos(documentFixture({
    url: "https://x.com/openai/status/123",
    articles: [
      articleFixture("someone", "999", {
        photos: [photoNode({
          src: "https://pbs.twimg.com/media/wrong?format=jpg",
          statusId: "999",
        })],
      }),
      expected,
    ],
  }), "123");

  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    reason: "exhausted",
    diagnostic: null,
    photos: [
      {
        position: 0,
        url: "https://pbs.twimg.com/media/first?format=jpg&name=large",
        alt_text: "First",
      },
      {
        position: 1,
        url: "https://pbs.twimg.com/media/second?format=png&name=large",
        alt_text: null,
      },
    ],
  });
});

for (const [name, url, expectedPostId] of [
  ["different post ID", "https://x.com/openai/status/456", "123"],
  ["www host", "https://www.x.com/openai/status/123", "123"],
  ["uppercase handle", "https://x.com/OpenAI/status/123", "123"],
  ["query string", "https://x.com/openai/status/123?ref=test", "123"],
  ["explicit default port", "https://x.com:443/openai/status/123", "123"],
  ["unexpected fragment", "https://x.com/openai/status/123#photo", "123"],
  ["photo subroute", "https://x.com/openai/status/123/photo/1", "123"],
  ["wrong host", "https://evil.example/openai/status/123", "123"],
  ["nonnumeric expected ID", "https://x.com/openai/status/123", "abc"],
]) {
  test(`exact-post resolution rejects ${name}`, () => {
    const result = api.resolvePostPhotos(documentFixture({
      url,
      articles: [articleFixture("openai", "123")],
    }), expectedPostId);

    assert.deepEqual(JSON.parse(JSON.stringify(result)), {
      reason: "error",
      diagnostic: "unexpected post page",
      photos: [],
    });
  });
}

test("exact-post resolution rejects a page without the expected article permalink", () => {
  const result = api.resolvePostPhotos(documentFixture({
    url: "https://x.com/openai/status/123",
    articles: [articleFixture("openai", "456")],
  }), "123");

  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    reason: "error",
    diagnostic: "expected post was not found",
    photos: [],
  });
});

for (const [name, permalinkUrl] of [
  ["www host", "https://www.x.com/openai/status/123"],
  ["handle case drift", "https://x.com/OpenAI/status/123"],
  ["userinfo", "https://viewer@x.com/openai/status/123"],
  ["explicit port", "https://x.com:444/openai/status/123"],
  ["query", "https://x.com/openai/status/123?ref=timeline"],
  ["fragment", "https://x.com/openai/status/123#context"],
  ["extra subroute", "https://x.com/openai/status/123/photo/1"],
  ["wrong handle with the same post ID", "https://x.com/other/status/123"],
]) {
  test(`exact-post resolution rejects an article permalink with ${name}`, () => {
    const result = api.resolvePostPhotos(documentFixture({
      url: "https://x.com/openai/status/123",
      articles: [articleFixture("openai", "123", { permalinkUrl })],
    }), "123");

    assert.deepEqual(JSON.parse(JSON.stringify(result)), {
      reason: "error",
      diagnostic: "expected post was not found",
      photos: [],
    });
  });
}

test("exact-post safety keeps challenge then rate-limit then signed-out precedence", () => {
  const base = {
    url: "https://x.com/openai/status/123",
    articles: [articleFixture("openai", "123")],
  };

  assert.equal(api.resolvePostPhotos(documentFixture({
    ...base, challengeForm: true, platformText: "Too many requests", loginControls: true,
  }), "123").x_state, "challenge");
  assert.equal(api.resolvePostPhotos(documentFixture({
    ...base, platformText: "Too many requests", loginControls: true,
  }), "123").x_state, "rate_limited");
  assert.equal(api.resolvePostPhotos(documentFixture({
    ...base, loginControls: true,
  }), "123").x_state, "signed_out");
});

test("content resolves an exact-post manifest once and returns one terminal message", async () => {
  const contentSource = fs.readFileSync(new URL("../content.js", import.meta.url), "utf8");
  let listener;
  let calls = 0;
  const terminal = {
    reason: "exhausted",
    diagnostic: null,
    photos: [],
  };
  const sandbox = {
    document: {},
    window: {},
    setInterval() {},
    chrome: {
      storage: { local: { async get() { return {}; } } },
      runtime: {
        onMessage: { addListener(value) { listener = value; } },
        async sendMessage(message) {
          return message.type === "is-owned-tab" ? { owned: false } : {};
        },
      },
    },
    XFeedContent: {
      resolvePostPhotos(_document, expectedPostId) {
        calls += 1;
        assert.equal(expectedPostId, "123");
        return terminal;
      },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(contentSource, sandbox);

  const result = await new Promise((resolve) => {
    assert.equal(listener({
      type: "resolve-post-photos",
      jobId: "job-media-1",
      postId: "123",
    }, {}, resolve), true);
  });

  assert.equal(calls, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), terminal);
});
