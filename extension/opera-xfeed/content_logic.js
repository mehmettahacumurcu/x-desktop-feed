(() => {
  "use strict";
  const STATUS = /^\/([A-Za-z0-9_]{1,15})\/status\/(\d+)\/?$/;
  const PHOTO_STATUS = /^\/[A-Za-z0-9_]{1,15}\/status\/(\d+)(?:\/photo\/[1-9]\d*)?\/?$/;
  const PROFILE = /^\/([A-Za-z0-9_]{1,15})\/?$/;
  const RAW_SCAN_CEILING = 300;
  const PROMOTION_SELECTOR =
    '[data-testid="placementTracking"], [data-testid="ad"], [data-promoted="true"]';
  const HOME_TAB_LABELS = Object.freeze({
    for_you: new Set(["for you", "sana özel"]),
    following: new Set(["following", "takip edilenler"]),
  });
  const HOME_TAB_INDEX = Object.freeze({ for_you: 0, following: 1 });

  function homeTabLabel(element, targetKind) {
    const label = element?.textContent?.trim().toLocaleLowerCase("tr-TR") ?? "";
    return Boolean(element && HOME_TAB_LABELS[targetKind]?.has(label));
  }

  function findHomeFeedTab(document, targetKind) {
    if (!HOME_TAB_LABELS[targetKind]) return null;
    const candidates = Array.from(document.querySelectorAll('[role="tablist"]'))
      .map((tabList) => {
        if (tabList.hidden || tabList.getAttribute?.("aria-hidden") === "true") return null;
        const tabs = Array.from(tabList.querySelectorAll(':scope [role="tab"]'));
        if (!homeTabLabel(tabs[0], "for_you") || !homeTabLabel(tabs[1], "following")) {
          return null;
        }
        const index = HOME_TAB_INDEX[targetKind];
        const element = tabs[index];
        return {
          element,
          index,
          selected: element.getAttribute("aria-selected") === "true",
        };
      })
      .filter(Boolean);
    return candidates.length === 1 ? candidates[0] : null;
  }

  async function selectAndVerifyHomeFeed(document, targetKind, deadlineMs, helpers) {
    let match = findHomeFeedTab(document, targetKind);
    if (!match) throw new Error(`${helpers.displayName(targetKind)} tab could not be verified`);
    const displayName = helpers.displayName(targetKind);
    const timelineBeforeSelection = helpers.captureTimeline();
    const transitionRequired = !match.selected;
    if (!match.selected) match.element.click();
    await helpers.waitUntil(() => {
      match = findHomeFeedTab(document, targetKind);
      return Boolean(match?.selected);
    }, deadlineMs);
    await helpers.waitForTimelineSettle(
      deadlineMs,
      timelineBeforeSelection,
      transitionRequired,
      displayName,
    );
    match = findHomeFeedTab(document, targetKind);
    if (!match?.selected) {
      throw new Error(`${helpers.displayName(targetKind)} tab selection was lost`);
    }
  }

  function canonicalStatus(url, base) {
    let parsed;
    try { parsed = new URL(url, base); } catch { return null; }
    const match = parsed.pathname.match(STATUS);
    if (parsed.protocol !== "https:" || !["x.com", "www.x.com"].includes(parsed.hostname.toLowerCase()) || !match) {
      return null;
    }
    const authorHandle = match[1].toLowerCase();
    return {
      url: `https://x.com/${authorHandle}/status/${match[2]}`,
      postId: match[2],
      authorHandle,
    };
  }

  function photoOwnerPostId(url, base) {
    let parsed;
    try { parsed = new URL(url, base); } catch { return null; }
    const match = parsed.pathname.match(PHOTO_STATUS);
    if (parsed.protocol !== "https:" || !["x.com", "www.x.com"].includes(parsed.hostname.toLowerCase()) ||
        parsed.username || parsed.password || parsed.port || !match) {
      return null;
    }
    return match[1];
  }

  function normalizeMediaPath(rawPath) {
    const match = /^\/media\/([^/\\]+)$/.exec(rawPath || "");
    if (!match) return null;
    let identifier = match[1];
    for (let attempt = 0; attempt < 8; attempt += 1) {
      if (/%(?![0-9A-Fa-f]{2})/.test(identifier)) return null;
      const decoded = decodeURIComponent(identifier);
      if (decoded === identifier) {
        if (identifier === "." || identifier === ".." ||
            !/^[A-Za-z0-9._~-]+$/.test(identifier)) return null;
        return `/media/${identifier}`;
      }
      identifier = decoded;
    }
    return null;
  }

  function normalizePhotoUrl(value, _base) {
    if (typeof value !== "string" || value.length > 2048 ||
        !/^[\x21-\x7E]+$/.test(value)) return null;
    try {
      const url = new URL(value);
      const resolved = url.href;
      const rawPath = value.match(/^[A-Za-z][A-Za-z0-9+.-]*:\/\/[^/?#]*([^?#]*)/)?.[1];
      const normalizedPath = normalizeMediaPath(rawPath);
      if (resolved.length > 2048 || url.protocol !== "https:" || url.hostname !== "pbs.twimg.com" ||
          url.username || url.password || url.port || url.hash !== "" ||
          !normalizedPath) {
        return null;
      }
      const query = [];
      const names = new Set();
      if (url.search.length > 1) {
        for (const item of url.search.slice(1).split("&")) {
          const separator = item.indexOf("=");
          if (separator < 0) return null;
          const name = decodeURIComponent(item.slice(0, separator).replaceAll("+", " "));
          const queryValue = decodeURIComponent(item.slice(separator + 1).replaceAll("+", " "));
          if (names.has(name)) return null;
          names.add(name);
          if (name === "format") query.push([name, queryValue]);
        }
      }
      query.push(["name", "large"]);
      query.sort(([leftName, leftValue], [rightName, rightValue]) =>
        leftName.localeCompare(rightName) || leftValue.localeCompare(rightValue));
      const encoded = query.map(([name, queryValue]) =>
        `${encodeURIComponent(name).replace(/%20/g, "+")}` +
        `=${encodeURIComponent(queryValue).replace(/%20/g, "+")}`).join("&");
      return `https://pbs.twimg.com${normalizedPath}?${encoded}`;
    } catch {
      return null;
    }
  }

  function normalizeAltText(value) {
    if (typeof value !== "string") return null;
    const trimmed = value.trim();
    return trimmed ? trimmed.slice(0, 1000) : null;
  }

  function normalizePhotos(photos, base) {
    if (!Array.isArray(photos) || photos.length > 4) return null;
    const result = [];
    const urls = new Set();
    for (let index = 0; index < photos.length; index += 1) {
      const photo = photos[index];
      if (!photo || typeof photo !== "object" || Array.isArray(photo) ||
          Object.keys(photo).sort().join(",") !== "alt_text,position,url" || photo.position !== index) {
        return null;
      }
      const url = normalizePhotoUrl(photo.url, base);
      if (!url || urls.has(url)) return null;
      urls.add(url);
      result.push(Object.freeze({ position: index, url, alt_text: normalizeAltText(photo.alt_text) }));
    }
    return Object.freeze(result);
  }

  function extractPostPhotos(article, outerPostId) {
    const base = article?.ownerDocument?.location?.origin || globalThis.document?.location?.origin;
    const photos = [];
    const urls = new Set();
    for (const image of article?.querySelectorAll?.('[data-testid="tweetPhoto"] img') || []) {
      const statusAnchor = image.closest?.('a[href*="/status/"]');
      const ownerPostId = statusAnchor ? photoOwnerPostId(statusAnchor.href, base) : null;
      if (statusAnchor && ownerPostId !== outerPostId) continue;
      const url = normalizePhotoUrl(image.currentSrc || image.src, base);
      if (!url || urls.has(url)) continue;
      urls.add(url);
      photos.push({ position: photos.length, url, alt_text: normalizeAltText(image.alt) });
      if (photos.length === 4) break;
    }
    return normalizePhotos(photos, base) || Object.freeze([]);
  }

  function signedInMarker(document) {
    return Boolean(document?.querySelector?.(
      '[data-testid="SideNav_AccountSwitcher_Button"], ' +
      '[data-testid="AppTabBar_Home_Link"], [data-testid="SideNav_Home_Link"]'
    ));
  }

  function documentXState(document, observationCount = 0) {
    const platformNodes = Array.from(document.querySelectorAll(
      '[role="dialog"], [role="alert"], [data-testid="emptyState"], ' +
      '[data-testid="error-detail"], [data-testid="toast"]'
    ));
    const platformText = platformNodes
      .map((node) => node.innerText || node.textContent || "")
      .join(" ")
      .toLowerCase();
    const challenge = Boolean(document.querySelector(
      'form[action*="challenge"], iframe[src*="challenge"]'
    )) || /verify (that )?you are human|unusual activity|security check/.test(platformText);
    const rateLimited = /rate limit|try again later|too many requests/.test(platformText);
    const loginButton = Boolean(document.querySelector(
      '[data-testid="loginButton"], form[action*="login"]'
    ));
    let path = "";
    try { path = new URL(document.location.href).pathname.toLowerCase(); } catch {}
    const signedIn = signedInMarker(document);
    const signedOut = (path === "/login" || path.startsWith("/i/flow/login")) ||
      (loginButton && observationCount === 0 && !signedIn);
    if (challenge) return "challenge";
    if (rateLimited) return "rate_limited";
    if (signedOut) return "signed_out";
    return "signed_in";
  }

  function unsafeResolutionResult(xState) {
    if (xState === "signed_out") {
      return {
        reason: "login_wall",
        diagnostic: "X is signed out in Opera GX; sign in and reconnect",
        x_state: xState,
      };
    }
    if (xState === "challenge") {
      return {
        reason: "error",
        diagnostic: "X presented an account challenge in Opera GX",
        x_state: xState,
      };
    }
    if (xState === "rate_limited") {
      return {
        reason: "error",
        diagnostic: "X rate-limited the Opera GX session",
        x_state: xState,
      };
    }
    return null;
  }

  function resolvePostPhotos(document, expectedPostId) {
    if (typeof expectedPostId !== "string" || !/^[1-9]\d{0,127}$/.test(expectedPostId)) {
      return { reason: "error", diagnostic: "unexpected post page", photos: [] };
    }
    const rawPageUrl = document.location.href;
    let page;
    try { page = new URL(rawPageUrl); } catch {
      return { reason: "error", diagnostic: "unexpected post page", photos: [] };
    }
    const match = /^\/([a-z0-9_]{1,15})\/status\/(\d+)$/.exec(page.pathname);
    const canonicalPage = match
      ? `https://x.com/${match[1]}/status/${match[2]}`
      : null;
    const ownerFragment = page.hash === "" ||
      /^#xfeed-owner=[A-Za-z0-9_-]{16,128}$/.test(page.hash);
    if (page.protocol !== "https:" || page.hostname !== "x.com" ||
        page.username || page.password || page.port || page.search ||
        !ownerFragment || rawPageUrl !== `${canonicalPage}${page.hash}` ||
        canonicalPage !== `https://x.com/${match?.[1]}/status/${expectedPostId}`) {
      return { reason: "error", diagnostic: "unexpected post page", photos: [] };
    }

    const unsafe = unsafeResolutionResult(documentXState(document));
    if (unsafe) return unsafe;

    for (const article of document.querySelectorAll('article[data-testid="tweet"]')) {
      const permalink = article.querySelector("time")?.closest('a[href*="/status/"]');
      if (permalink?.href !== canonicalPage) continue;
      return {
        reason: "exhausted",
        diagnostic: null,
        photos: extractPostPhotos(article, expectedPostId),
      };
    }
    return { reason: "error", diagnostic: "expected post was not found", photos: [] };
  }

  function normalizeObservation(card, order) {
    if (!card || typeof card !== "object" || !Number.isInteger(order) || order < 0) return null;
    const postId = card.postId ?? card.post_id;
    const authorHandle = card.authorHandle ?? card.author_handle;
    if (typeof card.url !== "string" || typeof postId !== "string" || typeof authorHandle !== "string") return null;
    const canonical = canonicalStatus(card.url);
    if (!canonical || canonical.authorHandle.toLowerCase() !== authorHandle.toLowerCase() ||
        canonical.postId !== postId) return null;
    const flags = [card.pinned ?? card.is_pinned, card.reply ?? card.is_reply,
      card.repost ?? card.is_repost, card.quote ?? card.is_quote,
      card.promoted ?? card.is_promoted];
    if (flags.some((flag) => typeof flag !== "boolean")) return null;
    const rawParentUrl = card.parentUrl !== undefined ? card.parentUrl
      : (card.parent_url !== undefined ? card.parent_url : null);
    const parent = rawParentUrl === null ? null : canonicalStatus(rawParentUrl);
    if (rawParentUrl !== null && !parent) return null;
    const photos = normalizePhotos(card.photos ?? [], card.baseUrl);
    if (!photos) return null;
    return {
      url: canonical.url,
      post_id: postId,
      author_handle: authorHandle,
      is_pinned: flags[0],
      is_reply: flags[1],
      is_repost: flags[2],
      is_quote: flags[3],
      is_promoted: flags[4],
      parent_url: parent?.url ?? null,
      discovery_order: order,
      photos,
    };
  }

  function classifyPage(view, target) {
    const observations = Array.isArray(view?.observations)
      ? view.observations.filter(validObservation).map((value) => ({
          url: value.url,
          post_id: value.post_id,
          author_handle: value.author_handle,
          is_pinned: value.is_pinned,
          is_reply: value.is_reply,
          is_repost: value.is_repost,
          is_quote: value.is_quote,
          is_promoted: value.is_promoted,
          parent_url: value.parent_url,
          discovery_order: value.discovery_order,
          photos: normalizePhotos(value.photos) || Object.freeze([]),
        }))
      : [];
    let parsed = null;
    try { parsed = new URL(view?.url || ""); } catch {}
    const hostSupported = parsed && ["x.com", "www.x.com"].includes(parsed.hostname.toLowerCase());
    const profileMatch = parsed?.pathname.match(PROFILE);
    const sourceSupported = target?.kind === "source" && profileMatch &&
      profileMatch[1].toLowerCase() === target.handle?.toLowerCase();
    const homeSupported = ["for_you", "following"].includes(target?.kind) &&
      parsed?.pathname === "/home";
    const pageSupported = Boolean(hostSupported && (sourceSupported || homeSupported));
    const path = parsed?.pathname.toLowerCase() || "";
    const signedOut = path === "/login" || path.startsWith("/i/flow/login") ||
      Boolean(view?.loginControls && observations.length === 0 && !view.signedInMarker);
    let xState = "signed_in";
    if (view?.challenge) xState = "challenge";
    else if (view?.rateLimited) xState = "rate_limited";
    else if (signedOut) xState = "signed_out";
    return {
      observations,
      raw_scanned: Number.isInteger(view?.rawScanned) && view.rawScanned >= 0
        ? view.rawScanned : observations.length,
      login_wall: xState === "signed_out",
      end_reached: Boolean(view?.endReached),
      page_supported: pageSupported,
      page_url: parsed?.href || "",
      x_state: xState,
    };
  }

  function validObservation(value) {
    if (!value || typeof value !== "object") return false;
    return typeof value.url === "string" && typeof value.post_id === "string" &&
      typeof value.author_handle === "string" && Number.isInteger(value.discovery_order) && value.discovery_order >= 0 &&
      [value.is_pinned, value.is_reply, value.is_repost, value.is_quote, value.is_promoted]
        .every((flag) => typeof flag === "boolean") &&
      (value.parent_url === null || (typeof value.parent_url === "string" && Boolean(canonicalStatus(value.parent_url)))) &&
      normalizePhotos(value.photos) !== null;
  }

  function collectPage(document, target, rawMaximum = RAW_SCAN_CEILING) {
    const observations = [];
    const seenPostIds = new Set();
    const selected = target?.kind === "source" && typeof target.handle === "string"
      ? target.handle.toLowerCase() : null;
    const boundedMaximum = Number.isInteger(rawMaximum) && rawMaximum >= 0
      ? Math.min(rawMaximum, RAW_SCAN_CEILING) : RAW_SCAN_CEILING;
    const articles = Array.from(
      document.querySelectorAll('article[data-testid="tweet"]'),
    ).slice(0, boundedMaximum);
    for (const article of articles) {
      const time = article.querySelector("time");
      const permalink = time?.closest('a[href*="/status/"]');
      if (!permalink) continue;
      const outer = canonicalStatus(permalink.href, document.location.origin);
      if (!outer) continue;
      const replyParentUrls = new Set();
      let hasQuote = false;
      for (const anchor of article.querySelectorAll('a[href*="/status/"]')) {
        const status = canonicalStatus(anchor.href, document.location.origin);
        if (!status || status.postId === outer.postId) continue;
        const replyParent = Boolean(
          anchor.matches?.('[data-xfeed-reply-parent="true"]') ||
          anchor.closest?.('[data-testid="replyParent"], [data-xfeed-reply-parent="true"]')
        );
        if (replyParent) replyParentUrls.add(status.url);
        else if (!replyParent) hasQuote = true;
      }
      const parentUrl = replyParentUrls.size === 1 ? replyParentUrls.values().next().value : null;
      const social = article.querySelector('[data-testid="socialContext"]');
      const socialText = (social?.innerText || social?.textContent || "").trim();
      const explicitReply = Boolean(article.querySelector('[data-testid="replyingTo"]'));
      const reply = explicitReply || replyParentUrls.size > 0 || Array.from(article.querySelectorAll("a[href]")).some((anchor) => {
        if (anchor.closest('[data-testid="tweetText"], [data-testid="card.wrapper"]')) return false;
        try {
          const match = new URL(anchor.href, document.location.origin).pathname.match(PROFILE);
          const following = globalThis.Node?.DOCUMENT_POSITION_FOLLOWING ?? 4;
          const follows = Boolean(permalink.compareDocumentPosition(anchor) & following);
          return Boolean(match && follows);
        } catch { return false; }
      });
      const structuralPromotion = Boolean(
        article.matches?.(PROMOTION_SELECTOR) ||
        article.closest?.(PROMOTION_SELECTOR) ||
        article.querySelector(PROMOTION_SELECTOR)
      );
      const textPromotion = Array.from(article.querySelectorAll("span, div")).some((node) => {
        const contentSelector = '[data-testid="tweetText"], [data-testid="card.wrapper"]';
        if (node.closest?.(contentSelector) || node.querySelector?.(contentSelector)) return false;
        const text = (node.innerText || node.textContent || "").trim();
        return text === "Promoted" || text === "Ad";
      });
      const value = normalizeObservation({
        url: outer.url,
        postId: outer.postId, authorHandle: outer.authorHandle,
        pinned: /\bpinned\b/i.test(socialText),
        reply,
        repost: /\breposted\b/i.test(socialText),
        quote: hasQuote,
        promoted: structuralPromotion || textPromotion,
        parentUrl,
        photos: extractPostPhotos(article, outer.postId),
      }, observations.length);
      const sourceAccepted = target?.kind === "source" && selected &&
        value?.author_handle.toLowerCase() === selected &&
        !value.is_pinned && !value.is_reply && !value.is_repost && !value.is_quote && !value.is_promoted;
      const homeAccepted = ["for_you", "following"].includes(target?.kind) &&
        value && !value.is_pinned && !value.is_promoted;
      if (value && (sourceAccepted || homeAccepted) && !seenPostIds.has(value.post_id)) {
        seenPostIds.add(value.post_id);
        observations.push(value);
      }
    }
    const loginControls = Boolean(document.querySelector('[data-testid="loginButton"], a[href="/login"], form[action*="login"]'));
    const xState = documentXState(document, observations.length);
    const root = document.documentElement;
    return classifyPage({
      url: document.location.href,
      observations,
      rawScanned: articles.length,
      loginControls,
      signedInMarker: signedInMarker(document),
      challenge: xState === "challenge",
      rateLimited: xState === "rate_limited",
      endReached: window.scrollY + window.innerHeight >= root.scrollHeight - 4,
    }, target);
  }

  globalThis.XFeedContent = Object.freeze({
    RAW_SCAN_CEILING, classifyPage, normalizeObservation, extractPostPhotos, documentXState,
    resolvePostPhotos, collectPage, findHomeFeedTab, selectAndVerifyHomeFeed,
  });
})();
