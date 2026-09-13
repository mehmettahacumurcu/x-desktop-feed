import * as defaultProtocol from "./protocol.js";

const OWNED_TAB_KEY = "collectionTabId";
const OWNERSHIP_KEY = "collectionOwner";
const OWNERSHIP_VERSION = 1;
const OWNER_FRAGMENT_PREFIX = "#xfeed-owner=";
const OWNER_NONCE = /^[A-Za-z0-9_-]{16,128}$/;
const HOME_URL = "https://x.com/home";
const LAST_X_STATE_KEY = "lastXState";
const VALID_X_STATES = new Set(["signed_in", "signed_out", "challenge", "rate_limited"]);
const HEARTBEAT_PUMP_INTERVAL_MS = 1_000;
const KEEPALIVE_ALARM = "xfeed-keepalive";
const KEEPALIVE_PERIOD_MINUTES = 0.5;
const UNSAFE_STATES = Object.freeze({
  signed_out: {
    reason: "login_wall",
    diagnostic: "X is signed out in Opera GX; sign in and reconnect",
  },
  challenge: {
    reason: "error",
    diagnostic: "X presented an account challenge in Opera GX",
  },
  rate_limited: {
    reason: "error",
    diagnostic: "X rate-limited the Opera GX session",
  },
});

function isXUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" && parsed.hostname.toLowerCase() === "x.com";
  } catch {
    return false;
  }
}

function randomOwnerNonce() {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function validOwnership(value) {
  return value?.version === OWNERSHIP_VERSION && OWNER_NONCE.test(value.nonce) &&
    (value.tab_id === null || (Number.isInteger(value.tab_id) && value.tab_id >= 0));
}

function markedOwnedUrl(value, nonce) {
  if (!OWNER_NONCE.test(nonce)) throw new Error("Invalid owned-tab nonce");
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.hostname.toLowerCase() !== "x.com") {
    throw new Error("Owned tab navigation must stay on X");
  }
  parsed.hash = `xfeed-owner=${nonce}`;
  return parsed.href;
}

function tabHasOwnerMarker(tab, nonce) {
  return [tab?.url, tab?.pendingUrl].some((value) => {
    try {
      const parsed = new URL(value);
      return parsed.protocol === "https:" && parsed.hostname.toLowerCase() === "x.com" &&
        parsed.hash === `${OWNER_FRAGMENT_PREFIX}${nonce}`;
    } catch {
      return false;
    }
  });
}

export function createBackground({
  chromeApi = globalThis.chrome,
  protocol = defaultProtocol,
  setTimeoutFn = globalThis.setTimeout,
  clearTimeoutFn = globalThis.clearTimeout,
  setIntervalFn = globalThis.setInterval,
  clearIntervalFn = globalThis.clearInterval,
  nowFn = Date.now,
  ownerNonceFn = randomOwnerNonce,
} = {}) {
  let activeJob = null;
  let activeRun = null;
  let jobInFlight = null;
  let cycleInFlight = null;
  let bootstrapInFlight = null;
  let ownedTabTail = Promise.resolve();
  let heartbeatTail = Promise.resolve();
  let heartbeatGeneration = 0;
  let validatedOwnedTabId = null;
  let lastStatus = { paired: false, connected: false, x_state: "signed_out", diagnostic: "Not paired" };
  let lastConfirmedXState = "signed_out";

  async function readLastXState() {
    try {
      const stored = await chromeApi.storage.local.get(LAST_X_STATE_KEY);
      if (VALID_X_STATES.has(stored[LAST_X_STATE_KEY])) {
        lastConfirmedXState = stored[LAST_X_STATE_KEY];
      }
    } catch {}
  }

  async function rememberXState(xState) {
    if (!VALID_X_STATES.has(xState)) return;
    lastConfirmedXState = xState;
    try {
      await chromeApi.storage.local.set({ [LAST_X_STATE_KEY]: xState });
    } catch {}
  }

  async function paired() {
    const stored = await chromeApi.storage.local.get("token");
    return typeof stored.token === "string" && Boolean(stored.token);
  }

  function withOwnedTabLock(operation) {
    const current = ownedTabTail.then(operation, operation);
    ownedTabTail = current.then(() => undefined, () => undefined);
    return current;
  }

  async function readOwnership() {
    const stored = await chromeApi.storage.local.get(OWNERSHIP_KEY);
    return validOwnership(stored[OWNERSHIP_KEY]) ? stored[OWNERSHIP_KEY] : null;
  }

  async function writeOwnership(nonce, tabId) {
    const record = { version: OWNERSHIP_VERSION, nonce, tab_id: tabId };
    await chromeApi.storage.local.set({ [OWNERSHIP_KEY]: record });
    return record;
  }

  async function ownershipForCreation(create) {
    const existing = await readOwnership();
    if (existing || !create) return existing;
    const nonce = ownerNonceFn();
    if (!OWNER_NONCE.test(nonce)) throw new Error("Owned-tab nonce generation failed");
    return writeOwnership(nonce, null);
  }

  async function exactOwnedTab(tabId, nonce) {
    if (!Number.isInteger(tabId)) return null;
    try {
      const tab = await chromeApi.tabs.get(tabId);
      return tabHasOwnerMarker(tab, nonce) ? tab : null;
    } catch {
      return null;
    }
  }

  async function rememberOwnedTab(record, tab) {
    await writeOwnership(record.nonce, tab.id);
    await chromeApi.storage.session.set({ [OWNED_TAB_KEY]: tab.id });
    validatedOwnedTabId = tab.id;
    return tab;
  }

  async function exactMarkedTabs(nonce) {
    const tabs = await chromeApi.tabs.query({ url: "https://x.com/*" });
    return tabs.filter((tab) => tabHasOwnerMarker(tab, nonce))
      .sort((left, right) => left.id - right.id);
  }

  async function getOwnedTabUnlocked(create) {
    const record = await ownershipForCreation(create);
    if (!record) {
      validatedOwnedTabId = null;
      await chromeApi.storage.session.remove(OWNED_TAB_KEY);
      return null;
    }
    const saved = await chromeApi.storage.session.get(OWNED_TAB_KEY);
    const sessionTab = await exactOwnedTab(saved[OWNED_TAB_KEY], record.nonce);
    if (sessionTab) return rememberOwnedTab(record, sessionTab);
    const durableTab = saved[OWNED_TAB_KEY] === record.tab_id
      ? null : await exactOwnedTab(record.tab_id, record.nonce);

    const matches = await exactMarkedTabs(record.nonce);
    const keeper = matches[0] || durableTab;
    if (keeper) {
      await rememberOwnedTab(record, keeper);
      for (const duplicate of matches) {
        if (duplicate.id !== keeper.id) {
          try { await chromeApi.tabs.remove(duplicate.id); } catch {}
        }
      }
      return keeper;
    }
    validatedOwnedTabId = null;
    await chromeApi.storage.session.remove(OWNED_TAB_KEY);
    if (!create) return null;
    const tab = await chromeApi.tabs.create({
      url: markedOwnedUrl(HOME_URL, record.nonce),
      active: false,
    });
    return rememberOwnedTab(record, tab);
  }

  function getOwnedTab(create = false) {
    return withOwnedTabLock(() => getOwnedTabUnlocked(create));
  }

  function bootstrapOwnedTab() {
    if (bootstrapInFlight) return bootstrapInFlight;
    const current = (async () => {
      if (!(await paired())) return null;
      return getOwnedTab(true);
    })();
    bootstrapInFlight = current;
    current.then(
      () => { if (bootstrapInFlight === current) bootstrapInFlight = null; },
      (error) => {
        lastStatus = {
          ...lastStatus,
          connected: false,
          diagnostic: String(error?.message || error).slice(0, 500),
        };
        if (bootstrapInFlight === current) bootstrapInFlight = null;
      },
    );
    return current;
  }

  function sendToTab(tabId, message) {
    return chromeApi.tabs.sendMessage(tabId, message);
  }

  function sendToTabWithin(tabId, message, timeoutMs, run = null) {
    return new Promise((resolve, reject) => {
      let settled = false;
      function finish(error, value) {
        if (settled) return;
        settled = true;
        clearTimeoutFn(timer);
        if (run?.abortPending === abort) run.abortPending = null;
        if (error) reject(error); else resolve(value);
      }
      function abort() {
        finish(new Error("Opera job was cancelled"));
      }
      const timer = setTimeoutFn(() => {
        finish(new Error("X photo resolution timed out"));
      }, timeoutMs);
      if (run) run.abortPending = abort;
      sendToTab(tabId, message).then(
        (value) => finish(null, value),
        (error) => finish(error),
      );
    });
  }

  function waitForComplete(tabId, timeoutMs = 15_000, run = null) {
    return new Promise((resolve, reject) => {
      let finished = false;
      let timer;
      function finish(error) {
        if (finished) return;
        finished = true;
        clearTimeoutFn(timer);
        chromeApi.tabs.onUpdated.removeListener(updated);
        if (run?.abortPending === abort) run.abortPending = null;
        if (error) reject(error); else resolve();
      }
      function abort() {
        finish(new Error("Opera job was cancelled"));
      }
      function updated(id, changeInfo) {
        if (id === tabId && changeInfo.status === "complete") finish();
      }
      chromeApi.tabs.onUpdated.addListener(updated);
      if (run) run.abortPending = abort;
      timer = setTimeoutFn(() => finish(new Error("X navigation timed out")), timeoutMs);
      chromeApi.tabs.get(tabId).then(
        (tab) => { if (tab.status === "complete") finish(); },
        (error) => finish(error),
      );
    });
  }

  function closeOwnedTab(tabId) {
    return withOwnedTabLock(async () => {
      const record = await readOwnership();
      if (!record) return false;
      const tab = await exactOwnedTab(tabId, record.nonce);
      if (!tab) return false;
      validatedOwnedTabId = null;
      await chromeApi.storage.session.remove(OWNED_TAB_KEY);
      await writeOwnership(record.nonce, null);
      try { await chromeApi.tabs.remove(tabId); } catch {}
      return true;
    });
  }

  async function clearOwnershipUnlocked() {
    const record = await readOwnership();
    const matches = record ? await exactMarkedTabs(record.nonce) : [];
    validatedOwnedTabId = null;
    await chromeApi.storage.session.remove(OWNED_TAB_KEY);
    await chromeApi.storage.local.remove(OWNERSHIP_KEY);
    for (const tab of matches) {
      try { await chromeApi.tabs.remove(tab.id); } catch {}
    }
  }

  function unsafeDetails(xState) {
    return Object.prototype.hasOwnProperty.call(UNSAFE_STATES, xState)
      ? UNSAFE_STATES[xState] : null;
  }

  function stopHeartbeatPump(run) {
    if (run.pumpTimer === null) return;
    clearIntervalFn(run.pumpTimer);
    run.pumpTimer = null;
  }

  async function abortCancelledRun(run) {
    if (activeRun !== run || run.terminalized) return;
    run.terminalized = true;
    stopHeartbeatPump(run);
    const abort = run.abortPending;
    if (abort) abort();
    if (Number.isInteger(run.ownedTabId)) {
      await closeOwnedTab(run.ownedTabId);
    }
    await getOwnedTab(true);
  }

  async function observeCancellation(run) {
    if (activeRun !== run || run.terminalized || run.unsafeState || run.quiescing) return;
    const response = await protocol.pollJob();
    if (response.cancelled === true) await abortCancelledRun(run);
  }

  async function quiesceMediaPump(run) {
    run.quiescing = true;
    stopHeartbeatPump(run);
    const pending = run.pumpPending;
    if (pending) await pending;
  }

  function startHeartbeatPump(run) {
    if (run.pumpStarted) return;
    run.pumpStarted = true;
    run.pumpTimer = setIntervalFn(() => {
      if (activeRun !== run || run.terminalized || run.unsafeState || run.quiescing) {
        stopHeartbeatPump(run);
        return Promise.resolve();
      }
      if (run.pumpPending) return run.pumpPending;
      const current = publishHeartbeat(run.desiredXState)
        .then(() => run.job.kind === "resolve_post_photos"
          ? observeCancellation(run)
          : undefined)
        .catch((error) => {
          run.heartbeatError = error;
          stopHeartbeatPump(run);
        }).finally(() => {
          if (run.pumpPending === current) run.pumpPending = null;
        });
      run.pumpPending = current;
      return current;
    }, HEARTBEAT_PUMP_INTERVAL_MS);
  }

  function publishHeartbeat(xState) {
    const run = activeRun;
    const requestedUnsafe = unsafeDetails(xState);
    if (run?.unsafeState) {
      if (run.terminalizationPending) return run.terminalizationPending;
      if (run.terminalizationError) return Promise.reject(run.terminalizationError);
      return Promise.resolve({ x_state: run.unsafeState });
    }
    if (run) {
      run.desiredXState = xState;
      if (requestedUnsafe) {
        run.unsafeState = xState;
        run.terminalizationError = null;
        stopHeartbeatPump(run);
      }
    }
    const generation = ++heartbeatGeneration;
    const current = heartbeatTail.then(async () => {
      const hb = await protocol.heartbeat(xState);
      const returnedUnsafe = unsafeDetails(hb.x_state);
      if (run && activeRun === run && returnedUnsafe) {
        run.terminalized = true;
        stopHeartbeatPump(run);
      }
      if (generation === heartbeatGeneration) {
        lastStatus = {
          paired: true,
          connected: true,
          x_state: hb.x_state,
          diagnostic: returnedUnsafe?.diagnostic || "Connected",
        };
      }
      return hb;
    }).catch((error) => {
      if (run && activeRun === run && requestedUnsafe) run.terminalizationError = error;
      throw error;
    });
    heartbeatTail = current.catch(() => undefined);
    if (run && requestedUnsafe) run.terminalizationPending = current;
    current.finally(() => {
      if (run?.terminalizationPending === current) run.terminalizationPending = null;
    }).catch(() => {});
    return current;
  }

  async function runJob(job) {
    const mediaJob = job?.kind === "resolve_post_photos";
    const requestedTimeoutMs = Number.isInteger(job.timeout_ms) && job.timeout_ms > 0
      ? Math.min(45_000, job.timeout_ms) : 45_000;
    const deadlineAt = nowFn() + requestedTimeoutMs;
    const remainingTimeoutMs = () => Math.ceil(deadlineAt - nowFn());
    const run = {
      job,
      terminalized: false,
      terminalizationPending: null,
      terminalizationError: null,
      unsafeState: null,
      desiredXState: "signed_in",
      heartbeatError: null,
      pumpStarted: false,
      pumpTimer: null,
      pumpPending: null,
      abortPending: null,
      ownedTabId: null,
      quiescing: false,
    };
    activeJob = job;
    activeRun = run;
    startHeartbeatPump(run);
    let completionAttempted = false;
    async function complete(reason, diagnostic, photos = []) {
      if (mediaJob) {
        await quiesceMediaPump(run);
        if (run.terminalized) return;
      }
      completionAttempted = true;
      if (mediaJob) {
        await protocol.completeJob(job, { reason, diagnostic, photos });
      } else {
        await protocol.completeJob(job.id, reason, diagnostic);
      }
    }
    async function completeLocalTimeout() {
      await complete("timeout", null);
    }
    try {
      const tab = await getOwnedTab(true);
      run.ownedTabId = tab.id;
      const ownership = await readOwnership();
      if (!ownership || !tabHasOwnerMarker(tab, ownership.nonce)) {
        throw new Error("Owned collection tab could not be verified");
      }
      await chromeApi.tabs.update(tab.id, {
        url: markedOwnedUrl(mediaJob ? job.post_url : job.profile_url, ownership.nonce),
        active: !mediaJob,
      });
      const navigationBudgetMs = remainingTimeoutMs();
      if (navigationBudgetMs <= 0) {
        await completeLocalTimeout();
        return;
      }
      await waitForComplete(tab.id, Math.min(15_000, navigationBudgetMs), run);
      if (run.heartbeatError) throw run.heartbeatError;
      const probeBudgetMs = remainingTimeoutMs();
      if (probeBudgetMs <= 0) {
        await completeLocalTimeout();
        return;
      }
      let probe;
      const probeMessage = mediaJob ? { type: "probe" } : {
        type: "probe", targetKind: job.target_kind, handle: job.handle,
        timeout_ms: probeBudgetMs,
      };
      for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
          probe = await sendToTabWithin(tab.id, probeMessage, probeBudgetMs, run);
          break;
        } catch (error) {
          if (run.terminalized || run.unsafeState) return;
          const notReady = /Receiving end does not exist|Could not establish connection/.test(
            String(error?.message || error),
          );
          if (!notReady || attempt === 2 || remainingTimeoutMs() <= 0) {
            throw error;
          }
          await new Promise((resolve) => setTimeoutFn(resolve, 750));
        }
      }
      await publishHeartbeat(probe.x_state);
      if (run.terminalizationPending) await run.terminalizationPending;
      if (run.terminalizationError) throw run.terminalizationError;
      if (run.heartbeatError) throw run.heartbeatError;
      if (probe.x_state !== "signed_in" || run.terminalized || run.unsafeState) return;
      if (probe?.cancelled) {
        stopHeartbeatPump(run);
        await closeOwnedTab(tab.id);
        await getOwnedTab(true);
        return;
      }
      if (probe?.reason === "error") {
        const diagnostic = typeof probe.diagnostic === "string"
          ? probe.diagnostic.slice(0, 500) : "Home feed could not be verified";
        await complete("error", diagnostic);
        return;
      }
      const workBudgetMs = remainingTimeoutMs();
      if (workBudgetMs <= 0) {
        await completeLocalTimeout();
        return;
      }
      const result = await (mediaJob
        ? sendToTabWithin(
            tab.id,
            { type: "resolve-post-photos", jobId: job.id, postId: job.post_id },
            workBudgetMs,
            run,
          )
        : sendToTab(tab.id, {
            type: "collect", jobId: job.id, targetKind: job.target_kind,
            handle: job.handle, maximum: job.maximum,
            timeout_ms: Math.min(45_000, workBudgetMs),
          }));
      if (run.terminalizationPending) await run.terminalizationPending;
      if (run.terminalizationError) throw run.terminalizationError;
      if (run.terminalized) return;
      if (unsafeDetails(result?.x_state)) {
        await publishHeartbeat(result.x_state);
        return;
      }
      if (result?.cancelled) {
        stopHeartbeatPump(run);
        await closeOwnedTab(tab.id);
        await getOwnedTab(true);
        return;
      }
      const reasons = new Set(["limit", "exhausted", "no_progress", "login_wall", "timeout", "error"]);
      const reason = reasons.has(result?.reason) ? result.reason : "error";
      const diagnostic = typeof result?.diagnostic === "string" ? result.diagnostic.slice(0, 500) : null;
      await complete(reason, diagnostic, mediaJob && Array.isArray(result?.photos) ? result.photos : []);
    } catch (error) {
      if (run.terminalized) return;
      if (!completionAttempted && remainingTimeoutMs() <= 0) {
        try { await completeLocalTimeout(); } catch {}
        return;
      }
      if (!completionAttempted) {
        try {
          await complete("error", String(error?.message || error).slice(0, 500));
        } catch {}
      }
      throw error;
    } finally {
      stopHeartbeatPump(run);
      if (activeRun === run) {
        activeRun = null;
        activeJob = null;
      }
    }
  }

  function startJob(job) {
    if (jobInFlight) return jobInFlight;
    const current = runJob(job);
    jobInFlight = current;
    current.then(
      () => { if (jobInFlight === current) jobInFlight = null; },
      (error) => {
        lastStatus = {
          ...lastStatus,
          connected: false,
          diagnostic: String(error?.message || error).slice(0, 500),
        };
        if (jobInFlight === current) jobInFlight = null;
      },
    );
    return current;
  }

  async function runCycle() {
    if (!(await paired())) {
      stopKeepalive();
      return;
    }
    try {
      const tab = await getOwnedTab(false);
      let xState = "signed_out";
      let probed = false;
      if (tab?.id) {
        try {
          const probeResult = await sendToTab(tab.id, { type: "probe" });
          if (VALID_X_STATES.has(probeResult?.x_state)) {
            xState = probeResult.x_state;
            probed = true;
          }
        } catch {}
      }
      if (!probed) xState = lastConfirmedXState;
      await publishHeartbeat(xState);
      if (probed) await rememberXState(xState);
      if (!jobInFlight && !activeJob) {
        const response = await protocol.pollJob();
        if (response.job) startJob(response.job);
      }
      ensureKeepalive();
    } catch (error) {
      if (!activeRun?.unsafeState) {
        lastStatus = {
          paired: await paired(), connected: false, x_state: "signed_out",
          diagnostic: String(error?.message || error).slice(0, 500),
        };
      }
    }
  }

  function cycle() {
    if (cycleInFlight) return cycleInFlight;
    const current = runCycle();
    cycleInFlight = current;
    current.then(
      () => { if (cycleInFlight === current) cycleInFlight = null; },
      () => { if (cycleInFlight === current) cycleInFlight = null; },
    );
    return current;
  }

  let keepaliveAlarm = null;

  function ensureKeepalive() {
    if (keepaliveAlarm !== null || !chromeApi.alarms?.create) return;
    keepaliveAlarm = KEEPALIVE_ALARM;
    chromeApi.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: KEEPALIVE_PERIOD_MINUTES });
  }

  function stopKeepalive() {
    if (keepaliveAlarm === null) return;
    if (chromeApi.alarms?.clear) chromeApi.alarms.clear(keepaliveAlarm);
    keepaliveAlarm = null;
  }

  chromeApi.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === "is-owned-tab") {
      getOwnedTab(false).then((tab) => sendResponse({ owned: Boolean(tab && sender.tab?.id === tab.id) }));
      return true;
    }
    if (message?.type === "tick") {
      bootstrapOwnedTab().then(() => getOwnedTab(false)).then((tab) => {
        const accepted = Boolean(tab && sender.tab?.id === tab.id);
        if (accepted) cycle();
        sendResponse({ accepted });
      });
      return true;
    }
    if (message?.type === "collection-progress") {
      (async () => {
        const tab = await getOwnedTab(false);
        if (!activeJob || message.jobId !== activeJob.id || sender.tab?.id !== tab?.id) {
          throw new Error("Progress sender is not the active owned tab");
        }
        if (activeJob.kind === "resolve_post_photos") {
          throw new Error("Active job is not a collection job");
        }
        if (activeRun?.terminalized || activeRun?.terminalizationPending ||
            activeRun?.terminalizationError) {
          return { cancelled: true };
        }
        return protocol.reportProgress(activeJob.id, Array.isArray(message.observations) ? message.observations : []);
      })().then(sendResponse, (error) => sendResponse({ error: String(error.message || error).slice(0, 500) }));
      return true;
    }
    if (message?.type === "pair") {
      protocol.pair(message.code).then(() => getOwnedTab(true)).then(() => cycle()).then(
        () => { ensureKeepalive(); sendResponse({ ok: true }); },
        (error) => sendResponse({ ok: false, error: String(error.message || error).slice(0, 500) }),
      );
      return true;
    }
    if (message?.type === "status") {
      paired().then((isPaired) => sendResponse({ ...lastStatus, paired: isPaired }));
      return true;
    }
    if (message?.type === "refresh") {
      cycle().then(() => sendResponse(lastStatus));
      return true;
    }
    if (message?.type === "open-x") {
      getOwnedTab(true).then((tab) => chromeApi.tabs.update(tab.id, { active: true })).then(
        () => sendResponse({ ok: true }),
        (error) => sendResponse({ ok: false, error: String(error.message || error).slice(0, 500) }),
      );
      return true;
    }
    return false;
  });

  chromeApi.tabs.onRemoved.addListener((tabId, removeInfo) => withOwnedTabLock(async () => {
    const record = await readOwnership();
    const saved = await chromeApi.storage.session.get(OWNED_TAB_KEY);
    if (!record || (saved[OWNED_TAB_KEY] !== tabId && record.tab_id !== tabId &&
        validatedOwnedTabId !== tabId)) return;
    validatedOwnedTabId = null;
    await chromeApi.storage.session.remove(OWNED_TAB_KEY);
    if (removeInfo.isWindowClosing) return;
    await writeOwnership(record.nonce, null);
    if (await paired()) await getOwnedTabUnlocked(true);
  }));

  chromeApi.tabs.onUpdated.addListener((tabId, changeInfo) => withOwnedTabLock(async () => {
    if (!changeInfo.url || validatedOwnedTabId !== tabId) return;
    const record = await readOwnership();
    if (!record || record.tab_id !== tabId || tabHasOwnerMarker(
      { url: changeInfo.url }, record.nonce,
    )) return;
    const target = isXUrl(changeInfo.url) ? changeInfo.url : HOME_URL;
    await chromeApi.tabs.update(tabId, {
      url: markedOwnedUrl(target, record.nonce), active: false,
    });
  }));

  chromeApi.storage.onChanged?.addListener((changes, areaName) => {
    if (areaName !== "local" || !changes.token || changes.token.newValue) return;
    return withOwnedTabLock(clearOwnershipUnlocked);
  });

  async function startBackground() {
    await readLastXState();
    await bootstrapOwnedTab();
    if (!jobInFlight && !activeJob) cycle().catch(() => {});
  }

  chromeApi.runtime.onStartup.addListener(() => startBackground());

  chromeApi.runtime.onInstalled?.addListener(() => startBackground());

  chromeApi.alarms?.onAlarm?.addListener((alarm) => {
    if (alarm?.name !== KEEPALIVE_ALARM) return undefined;
    if (jobInFlight || activeJob) return undefined;
    return cycle().catch(() => {});
  });

  startBackground();

  return { cycle, getOwnedTab, bootstrapOwnedTab, waitForComplete, runJob };
}

if (typeof globalThis.chrome !== "undefined") createBackground();
