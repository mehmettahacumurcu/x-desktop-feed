(() => {
  "use strict";
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const HOME_TARGETS = new Set(["for_you", "following"]);
  let cancelled = false;
  let collecting = false;

  const displayName = (targetKind) => targetKind === "following" ? "Following" : "For You";

  function cancelledError() {
    const error = new Error("Opera job was cancelled");
    error.cancelled = true;
    return error;
  }

  async function waitWithinDeadline(
    delayMs,
    deadlineMs,
    timeoutMessage = "Home feed selection timed out",
  ) {
    if (cancelled) throw cancelledError();
    const remaining = deadlineMs - Date.now();
    if (remaining <= 0) throw new Error(timeoutMessage);
    await wait(Math.min(delayMs, remaining));
    if (cancelled) throw cancelledError();
    if (Date.now() >= deadlineMs) throw new Error(timeoutMessage);
  }

  function captureTimeline() {
    const root = document.querySelector('[data-testid="primaryColumn"]');
    if (!root) return null;
    return {
      root,
      cards: Array.from(root.querySelectorAll('article[data-testid="tweet"]')).map((card) => {
        const permalink = card.querySelector?.("time")
          ?.closest?.('a[href*="/status/"]')?.href ?? "";
        return {
          element: card,
          postId: /\/status\/(\d+)(?:[/?#]|$)/.exec(permalink)?.[1] ?? null,
          signature: [permalink, card.textContent ?? ""].join("\n"),
        };
      }),
    };
  }

  function sameTimeline(left, right) {
    return Boolean(left && right && left.root === right.root &&
      left.cards.length === right.cards.length &&
      left.cards.every((card, index) =>
        card.element === right.cards[index].element &&
        card.signature === right.cards[index].signature));
  }

  function timelineIdentityTransitioned(left, right) {
    return Boolean(left && right && left.cards.length > 0 && right.cards.length > 0 &&
      left.cards.every((card) => card.postId) &&
      right.cards.every((card) => card.postId) &&
      (left.cards.length !== right.cards.length ||
        left.cards.some((card, index) => card.postId !== right.cards[index].postId)));
  }

  const homeSelectionHelpers = {
    displayName,
    captureTimeline,
    async waitUntil(predicate, deadlineMs) {
      while (!predicate()) await waitWithinDeadline(50, deadlineMs);
    },
    async waitForTimelineSettle(
      deadlineMs,
      timelineBeforeSelection,
      transitionRequired,
      targetDisplayName,
    ) {
      let previous = timelineBeforeSelection;
      let transitionObserved = !transitionRequired;
      let stableChecks = 0;
      const timeoutMessage =
        `${targetDisplayName} timeline did not transition and stabilize before timeout`;
      while (true) {
        await waitWithinDeadline(50, deadlineMs, timeoutMessage);
        const current = captureTimeline();
        if (!current) {
          stableChecks = 0;
          continue;
        }
        if (transitionRequired) {
          transitionObserved = timelineIdentityTransitioned(timelineBeforeSelection, current);
        }
        if (!sameTimeline(previous, current)) {
          previous = current;
          stableChecks = 0;
          continue;
        }
        if (!transitionObserved) continue;
        stableChecks += 1;
        if (stableChecks >= 3) return;
      }
    },
  };

  function currentXState() {
    return globalThis.XFeedContent.documentXState(document);
  }

  function isRealScroller(element) {
    if (!element || typeof element.scrollTop !== "number") return false;
    if (element.scrollHeight <= element.clientHeight + 4) return false;
    if (typeof window.getComputedStyle === "function") {
      try {
        const overflowY = window.getComputedStyle(element).overflowY;
        if (overflowY === "hidden") return false;
      } catch {}
    }
    return true;
  }

  function findScrollContainers() {
    const nodes = new Set();
    const primary = document.querySelector
      ? document.querySelector('[data-testid="primaryColumn"]')
      : null;
    const article = document.querySelector
      ? document.querySelector('article[data-testid="tweet"]')
      : null;
    let start = primary || article;
    let node = start ? start.parentElement : null;
    while (node && node !== document.documentElement && node !== document.body) {
      if (isRealScroller(node)) nodes.add(node);
      node = node.parentElement;
    }
    if (start && isRealScroller(start)) nodes.add(start);
    for (const element of [document.scrollingElement, document.documentElement]) {
      if (element && isRealScroller(element)) nodes.add(element);
    }
    return [...nodes].sort((left, right) => right.scrollHeight - left.scrollHeight);
  }

  function currentScrollTop(scrollers) {
    if (typeof window.scrollY === "number" && window.scrollY > 0) return window.scrollY;
    for (const scroller of scrollers) {
      if (scroller.scrollTop > 0) return scroller.scrollTop;
    }
    return 0;
  }

  function feedReachedBottom(scrollers) {
    if (typeof window.scrollY === "number") {
      const height = document.documentElement?.scrollHeight || 0;
      if (window.scrollY + window.innerHeight >= height - 4) return true;
    }
    if (scrollers.length) {
      const tallest = scrollers[0];
      if (tallest.scrollTop + tallest.clientHeight >= tallest.scrollHeight - 4) return true;
    }
    return false;
  }

  function scrollFeedBy(scrollers, amount) {
    for (const scroller of scrollers) {
      try {
        scroller.scrollTop += amount;
      } catch {}
    }
    if (typeof window.scrollBy === "function") {
      window.scrollBy({ top: amount, behavior: "auto" });
    }
  }

  function collectDiagnostic(message, scrollers, seen, rawScanned, stall, advance) {
    const parts = [
      `profile=${message.targetKind}${message.handle ? "/" + message.handle : ""}`,
      `seen=${seen}`,
      `scanned=${rawScanned}`,
      `stalls=${stall}`,
      `advances=${advance}`,
      `top=${currentScrollTop(scrollers)}`,
      `numScrollers=${scrollers.length}`,
      `scrollers=${scrollers.map((s) => `${s.scrollTop}/${s.clientHeight}/${s.scrollHeight}`).join("|")}`,
      `winY=${typeof window.scrollY === "number" ? window.scrollY : "na"}`,
      `articles=${document.querySelectorAll?.('article[data-testid="tweet"]')?.length ?? "na"}`,
      `primaryCol=${Boolean(document.querySelector?.('[data-testid="primaryColumn"]'))}`,
    ];
    return parts.join(" ");
  }

  async function verifyHomeTarget(targetKind, deadlineMs) {
    if (!HOME_TARGETS.has(targetKind)) return;
    await globalThis.XFeedContent.selectAndVerifyHomeFeed(
      document,
      targetKind,
      deadlineMs,
      homeSelectionHelpers,
    );
  }

  function verifyHomeTargetBeforeExtraction(targetKind, deadlineMs) {
    if (!HOME_TARGETS.has(targetKind)) return;
    if (cancelled) throw cancelledError();
    if (Date.now() >= deadlineMs) throw new Error("Home feed selection timed out");
    const match = globalThis.XFeedContent.findHomeFeedTab(document, targetKind);
    if (!match?.selected) {
      throw new Error(`${displayName(targetKind)} tab selection was lost`);
    }
  }

  function unsafeResult(xState) {
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

  async function tick() {
    const stored = await chrome.storage.local.get("token");
    if (stored.token) chrome.runtime.sendMessage({ type: "tick" }).catch(() => {});
  }
  chrome.runtime.sendMessage({ type: "is-owned-tab" }).then((response) => {
    if (!response?.owned) return;
    setInterval(tick, 1000);
    tick().catch(() => {});
  }).catch(() => {});

  async function collect(message) {
    if (collecting) return { reason: "error", diagnostic: "collection already active" };
    if (!Number.isInteger(message.timeout_ms) || message.timeout_ms <= 0) {
      return { reason: "error", diagnostic: "invalid collection timeout" };
    }
    collecting = true;
    cancelled = false;
    const seen = new Set();
    let noProgress = 0;
    let rawScanned = 0;
    const rawScanCeiling = globalThis.XFeedContent.RAW_SCAN_CEILING || 300;
    const deadline = Date.now() + Math.min(45_000, message.timeout_ms);
    const hardMaximum = HOME_TARGETS.has(message.targetKind) ? 50 : 30;
    const maximum = Math.min(hardMaximum, message.maximum || 30);
    const target = { kind: message.targetKind, handle: message.handle };
    const scrollers = findScrollContainers();
    let previousScrollTop = currentScrollTop(scrollers);
    let scrollStalls = 0;
    let scrolledAdvances = 0;
    let articleCountStalls = 0;
    let previousArticleCount = 0;
    try {
      const initialUnsafe = unsafeResult(currentXState());
      if (initialUnsafe) return initialUnsafe;
      await verifyHomeTarget(message.targetKind, deadline);
      while (Date.now() < deadline) {
        if (cancelled) return { cancelled: true };
        if (rawScanned >= rawScanCeiling) {
          return {
            reason: "exhausted",
            diagnostic: `Raw scan ceiling of ${rawScanCeiling} observations reached`,
          };
        }
        if (HOME_TARGETS.has(message.targetKind)) {
          await waitWithinDeadline(750, deadline);
        } else {
          await wait(750);
        }
        const preExtractionUnsafe = unsafeResult(currentXState());
        if (preExtractionUnsafe) return preExtractionUnsafe;
        verifyHomeTargetBeforeExtraction(message.targetKind, deadline);
        const page = globalThis.XFeedContent.collectPage(
          document,
          target,
          rawScanCeiling - rawScanned,
        );
        const pageRawScanned = Number.isInteger(page.raw_scanned) && page.raw_scanned >= 0
          ? Math.min(page.raw_scanned, rawScanCeiling - rawScanned) : 0;
        rawScanned += pageRawScanned;
        const unsafe = unsafeResult(page.x_state);
        if (unsafe) return unsafe;
        if (!page.page_supported) return { reason: "error", diagnostic: "unexpected collection page" };
        const batch = [];
        for (const observation of page.observations) {
          if (!seen.has(observation.post_id) && seen.size < maximum) {
            observation.discovery_order = seen.size;
            seen.add(observation.post_id);
            batch.push(observation);
          }
        }
        if (batch.length) {
          noProgress = 0;
          const progress = await chrome.runtime.sendMessage({ type: "collection-progress", jobId: message.jobId, observations: batch });
          if (progress?.error) throw new Error(progress.error);
          if (progress?.cancelled) return { cancelled: true };
        } else {
          noProgress += 1;
          const progress = await chrome.runtime.sendMessage({ type: "collection-progress", jobId: message.jobId, observations: [] });
          if (progress?.error) throw new Error(progress.error);
          if (progress?.cancelled) return { cancelled: true };
        }
        if (seen.size >= maximum) return { reason: "limit", diagnostic: null };
        if (rawScanned >= rawScanCeiling) {
          return {
            reason: "exhausted",
            diagnostic: `Raw scan ceiling of ${rawScanCeiling} observations reached`,
          };
        }
        scrollFeedBy(scrollers, Math.max(window.innerHeight * 0.8, 600));
        const currentTop = currentScrollTop(scrollers);
        if (currentTop > previousScrollTop) {
          scrollStalls = 0;
          scrolledAdvances += 1;
        } else if (noProgress > 0) {
          scrollStalls += 1;
        }
        previousScrollTop = currentTop;
        const currentArticleCount = typeof document.querySelectorAll === "function"
          ? document.querySelectorAll('article[data-testid="tweet"]').length
          : rawScanned;
        if (currentArticleCount === previousArticleCount) {
          articleCountStalls += 1;
        } else {
          articleCountStalls = 0;
          previousArticleCount = currentArticleCount;
        }
        if (feedReachedBottom(scrollers) && scrolledAdvances >= 2 && articleCountStalls >= 2) {
          return {
            reason: "exhausted",
            diagnostic: collectDiagnostic(message, scrollers, seen.size, rawScanned, scrollStalls, scrolledAdvances),
          };
        }
        if (noProgress >= 3 && scrollStalls >= 3) {
          return {
            reason: "no_progress",
            diagnostic: collectDiagnostic(message, scrollers, seen.size, rawScanned, scrollStalls, scrolledAdvances),
          };
        }
      }
      return {
        reason: "timeout",
        diagnostic: collectDiagnostic(message, scrollers, seen.size, rawScanned, scrollStalls, scrolledAdvances),
      };
    } catch (error) {
      if (error?.cancelled) return { cancelled: true };
      return { reason: "error", diagnostic: String(error?.message || error).slice(0, 500) };
    } finally {
      collecting = false;
    }
  }

  async function resolvePostPhotos(message) {
    if (collecting) return { reason: "error", diagnostic: "browser work already active", photos: [] };
    collecting = true;
    cancelled = false;
    try {
      if (cancelled) return { cancelled: true };
      return globalThis.XFeedContent.resolvePostPhotos(document, message.postId);
    } catch (error) {
      return {
        reason: "error",
        diagnostic: String(error?.message || error).slice(0, 500),
        photos: [],
      };
    } finally {
      collecting = false;
    }
  }

  async function probe(message) {
    const target = { kind: message.targetKind, handle: message.handle };
    const deadline = Date.now() + Math.min(
      45_000,
      Number.isInteger(message.timeout_ms) && message.timeout_ms > 0
        ? message.timeout_ms : 45_000,
    );
    const xState = currentXState();
    const unsafe = unsafeResult(xState);
    if (unsafe) return unsafe;
    try {
      await verifyHomeTarget(message.targetKind, deadline);
      return globalThis.XFeedContent.collectPage(document, target);
    } catch (error) {
      if (error?.cancelled) return { cancelled: true, x_state: xState };
      return {
        reason: "error",
        diagnostic: String(error?.message || error).slice(0, 500),
        x_state: xState,
      };
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "probe") {
      probe(message).then(sendResponse);
      return true;
    }
    if (message?.type === "cancel") {
      cancelled = true;
      sendResponse({ cancelled: true });
      return false;
    }
    if (message?.type === "collect") {
      collect(message).then(sendResponse);
      return true;
    }
    if (message?.type === "resolve-post-photos") {
      resolvePostPhotos(message).then(sendResponse);
      return true;
    }
    return false;
  });
})();
