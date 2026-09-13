import assert from "node:assert/strict";
import test from "node:test";

const TEST_OWNER_NONCE = "test-owner-nonce-0123456789abcdef";

function markedXUrl(value, nonce = TEST_OWNER_NONCE) {
  const parsed = new URL(value);
  parsed.hash = `xfeed-owner=${nonce}`;
  return parsed.href;
}

function ownerRecord(tabId, nonce = TEST_OWNER_NONCE) {
  return { version: 1, nonce, tab_id: tabId };
}

function event() {
  const listeners = [];
  return {
    listeners,
    addListener(listener) { listeners.push(listener); },
    removeListener(listener) {
      const index = listeners.indexOf(listener);
      if (index >= 0) listeners.splice(index, 1);
    },
    emit(...args) { return Promise.all([...listeners].map((listener) => listener(...args))); },
  };
}

function minimalChrome() {
  const local = {};
  const session = {};
  return {
    runtime: { onMessage: event(), onStartup: event() },
    storage: {
      local: {
        async get(key) { return { [key]: local[key] }; },
        async set(value) { Object.assign(local, value); },
        async remove(key) { delete local[key]; },
      },
      session: {
        async get(key) { return { [key]: session[key] }; },
        async set(value) { Object.assign(session, value); },
        async remove(key) { delete session[key]; },
      },
      onChanged: event(),
    },
    tabs: {
      onUpdated: event(), onRemoved: event(),
      async get() { throw new Error("missing tab"); },
      async create() { return { id: 1, url: "about:blank", status: "complete" }; },
      async update(id, changes) { return { id, url: changes.url || "https://x.com/home", status: "complete" }; },
      async remove() {},
      async sendMessage() { return { x_state: "signed_out" }; },
    },
  };
}

globalThis.chrome = minimalChrome();
const background = await import("../background.js");

test("exports an injected background controller factory", () => {
  assert.equal(typeof background.createBackground, "function");
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function manualIntervals() {
  const callbacks = new Map();
  let nextId = 1;
  const calls = { start: [], clear: [] };
  return {
    calls,
    setIntervalFn(callback, delay) {
      const id = nextId++;
      calls.start.push({ id, delay });
      callbacks.set(id, callback);
      return id;
    },
    clearIntervalFn(id) {
      calls.clear.push(id);
      callbacks.delete(id);
    },
    async tick() {
      await Promise.all([...callbacks.values()].map((callback) => callback()));
    },
  };
}

function manualAlarms() {
  const listeners = [];
  const calls = { create: [], clear: [] };
  return {
    calls,
    create(name, info) { calls.create.push({ name, info }); },
    clear(name) { calls.clear.push(name); },
    onAlarm: {
      addListener(listener) { listeners.push(listener); },
    },
    async fire(name) {
      await Promise.all([...listeners].map((listener) => listener({ name })));
    },
  };
}

function harness() {
  const local = { token: "secret-token" };
  const session = {};
  const values = new Map();
  const calls = { create: [], update: [], remove: [], messages: [], query: [] };
  let nextId = 100;
  const chromeApi = {
    storage: {
      local: {
        async get(key) { return { [key]: local[key] }; },
        async set(value) { Object.assign(local, value); },
        async remove(key) { delete local[key]; },
      },
      onChanged: event(),
      session: {
        async get(key) { return { [key]: session[key] }; },
        async set(value) { Object.assign(session, value); },
        async remove(key) { delete session[key]; },
      },
    },
    runtime: { onMessage: event(), onStartup: event() },
    tabs: {
      onUpdated: event(), onRemoved: event(),
      async get(id) {
        if (!values.has(id)) throw new Error("missing tab");
        return { ...values.get(id) };
      },
      async create(options) {
        calls.create.push(options);
        const tab = { id: nextId++, url: options.url, status: "complete" };
        values.set(tab.id, tab);
        return { ...tab };
      },
      async update(id, changes) {
        calls.update.push({ id, changes });
        const tab = { ...(values.get(id) || { id }), ...changes, status: "complete" };
        values.set(id, tab);
        return { ...tab };
      },
      async remove(id) { calls.remove.push(id); values.delete(id); },
      async query(options) {
        calls.query.push(options);
        return [...values.values()].filter((tab) => {
          try {
            const parsed = new URL(tab.url || tab.pendingUrl);
            return parsed.protocol === "https:" && parsed.hostname === "x.com";
          } catch {
            return false;
          }
        }).map((tab) => ({ ...tab }));
      },
      async sendMessage(id, message) {
        calls.messages.push({ id, message });
        return chromeApi.tabMessage(id, message);
      },
    },
    async tabMessage(_id, message) {
      if (message.type === "probe") return { x_state: "signed_in" };
      if (message.type === "collect") return { reason: "exhausted", diagnostic: null };
      return {};
    },
  };
  async function runtimeMessage(message, sender = {}) {
    const listener = chromeApi.runtime.onMessage.listeners[0];
    return new Promise((resolve, reject) => {
      let answered = false;
      const keepOpen = listener(message, sender, (value) => { answered = true; resolve(value); });
      if (keepOpen !== true && !answered) resolve(undefined);
    });
  }
  return { chromeApi, local, session, values, calls, runtimeMessage };
}

function seedOwnedTab(
  h, id = 7, url = "https://x.com/home", status = "complete", nonce = TEST_OWNER_NONCE,
) {
  h.local.collectionOwner = ownerRecord(id, nonce);
  h.session.collectionTabId = id;
  h.values.set(id, { id, url: markedXUrl(url, nonce), status });
}

function protocol(overrides = {}) {
  const calls = { heartbeat: [], poll: 0, progress: [], complete: [] };
  return {
    calls,
    async pair() {},
    async heartbeat(state) { calls.heartbeat.push(state); return { x_state: state }; },
    async pollJob() { calls.poll += 1; return { job: null }; },
    async reportProgress(id, observations) { calls.progress.push({ id, observations }); return { cancelled: false }; },
    async completeJob(id, reason, diagnostic) {
      if (id && typeof id === "object") calls.complete.push({ job: id, result: reason });
      else calls.complete.push({ id, reason, diagnostic });
      return { accepted: true };
    },
    ...overrides,
  };
}

test("forwards a collection photo manifest unchanged from the owned tab", async () => {
  const h = harness();
  seedOwnedTab(h);
  const collectionStarted = deferred();
  const collectionDone = deferred();
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") return { x_state: "signed_in" };
    if (message.type === "collect") {
      collectionStarted.resolve();
      return collectionDone.promise;
    }
    return {};
  };
  const p = protocol();
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: p });
  const observation = {
    url: "https://x.com/openai/status/1", post_id: "1", author_handle: "openai",
    is_pinned: false, is_reply: false, is_repost: false, is_quote: false,
    is_promoted: false, parent_url: null, discovery_order: 0,
    photos: [{
      position: 0, url: "https://pbs.twimg.com/media/one?format=jpg&name=large",
      alt_text: "first <photo>",
    }],
  };
  const running = controller.runJob({
    id: "job-1", handle: "openai", profile_url: "https://x.com/openai", maximum: 30,
    timeout_ms: 45_000,
  });

  await collectionStarted.promise;
  assert.deepEqual(await h.runtimeMessage({
    type: "collection-progress", jobId: "job-1", observations: [observation],
  }, { tab: { id: 7 } }), { cancelled: false });
  assert.strictEqual(p.calls.progress[0].observations[0].photos, observation.photos);

  collectionDone.resolve({ reason: "exhausted", diagnostic: null });
  await running;
});

test("an overlapping tick and refresh share one atomic cycle", async () => {
  const h = harness();
  seedOwnedTab(h);
  const gate = deferred();
  const started = deferred();
  const p = protocol({ async heartbeat(state) { p.calls.heartbeat.push(state); started.resolve(); await gate.promise; return { x_state: state }; } });
  background.createBackground({ chromeApi: h.chromeApi, protocol: p });
  await h.runtimeMessage({ type: "tick" }, { tab: { id: 7 } });
  const refresh = h.runtimeMessage({ type: "refresh" });
  await started.promise;
  assert.equal(p.calls.heartbeat.length, 1);
  gate.resolve();
  await refresh;
  assert.equal(p.calls.poll, 1);
});

test("navigation completion emitted during tabs.get is not missed", async () => {
  const h = harness();
  h.values.set(7, { id: 7, url: "https://x.com/openai", status: "loading" });
  h.chromeApi.tabs.get = async (id) => {
    h.values.get(id).status = "complete";
    await h.chromeApi.tabs.onUpdated.emit(id, { status: "complete" }, { ...h.values.get(id) });
    return { ...h.values.get(id) };
  };
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: protocol() });
  await controller.waitForComplete(7, 50);
});

test("concurrent owned-tab requests create exactly one tab", async () => {
  const h = harness();
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: protocol() });
  const tabs = await Promise.all([
    controller.getOwnedTab(true), controller.getOwnedTab(true), controller.getOwnedTab(true),
  ]);
  assert.equal(h.calls.create.length, 1);
  assert.deepEqual(tabs.map((tab) => tab.id), [tabs[0].id, tabs[0].id, tabs[0].id]);
});

test("paired worker reload bootstraps one fresh owned tab across concurrent triggers", async () => {
  const h = harness();
  h.values.set(77, { id: 77, url: "https://x.com/openai", status: "complete" });
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: protocol() });

  assert.equal(typeof controller.bootstrapOwnedTab, "function");
  const [bootstrapped, _startup, tick] = await Promise.all([
    controller.bootstrapOwnedTab(),
    h.chromeApi.runtime.onStartup.emit(),
    h.runtimeMessage({ type: "tick" }, { tab: { id: 77 } }),
  ]);

  assert.equal(h.calls.create.length, 1);
  assert.notEqual(h.session.collectionTabId, 77);
  assert.equal(bootstrapped.id, h.session.collectionTabId);
  assert.equal(tick.accepted, false);
  assert.equal(h.values.get(77).url, "https://x.com/openai");
});

test("empty-session reload adopts the exact durable marker across concurrent triggers", async () => {
  const h = harness();
  h.local.collectionOwner = ownerRecord(7);
  h.values.set(55, {
    id: 55, url: markedXUrl("https://x.com/openai"), status: "complete",
  });
  h.values.set(77, { id: 77, url: "https://x.com/nasa", status: "complete" });
  h.values.set(88, {
    id: 88, url: markedXUrl("https://x.com/home", "different-owner-nonce-0123456789"),
    status: "complete",
  });
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: protocol() });

  const [adopted, _startup, tick] = await Promise.all([
    controller.bootstrapOwnedTab(),
    h.chromeApi.runtime.onStartup.emit(),
    h.runtimeMessage({ type: "tick" }, { tab: { id: 77 } }),
  ]);

  assert.equal(adopted.id, 55);
  assert.equal(h.calls.create.length, 0);
  assert.equal(h.session.collectionTabId, 55);
  assert.deepEqual(h.local.collectionOwner, ownerRecord(55));
  assert.deepEqual(h.calls.remove, []);
  assert.equal(tick.accepted, false);
  assert.equal(h.values.get(77).url, "https://x.com/nasa");
  assert.equal(h.values.get(88).url, markedXUrl(
    "https://x.com/home", "different-owner-nonce-0123456789",
  ));
});

test("stale reused tab ID is ignored and duplicate exact markers are deduplicated", async () => {
  const h = harness();
  h.local.collectionOwner = ownerRecord(77);
  h.session.collectionTabId = 77;
  h.values.set(77, { id: 77, url: "https://x.com/user-tab", status: "complete" });
  h.values.set(55, { id: 55, url: markedXUrl("https://x.com/home"), status: "complete" });
  h.values.set(56, { id: 56, url: markedXUrl("https://x.com/openai"), status: "complete" });
  h.values.set(66, {
    id: 66, url: markedXUrl("https://x.com/home", "another-owner-nonce-0123456789"),
    status: "complete",
  });
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: protocol() });

  const adopted = await controller.bootstrapOwnedTab();

  assert.equal(adopted.id, 55);
  assert.equal(h.session.collectionTabId, 55);
  assert.deepEqual(h.local.collectionOwner, ownerRecord(55));
  assert.deepEqual(h.calls.remove, [56]);
  assert.equal(h.calls.create.length, 0);
  assert.equal(h.values.get(77).url, "https://x.com/user-tab");
  assert.equal(h.values.has(66), true);
});

test("new ownership persists one nonce and creates one directly marked tab", async () => {
  const h = harness();
  const controller = background.createBackground({
    chromeApi: h.chromeApi,
    protocol: protocol(),
    ownerNonceFn: () => TEST_OWNER_NONCE,
  });

  const [created, _startup, concurrent] = await Promise.all([
    controller.bootstrapOwnedTab(),
    h.chromeApi.runtime.onStartup.emit(),
    controller.getOwnedTab(true),
  ]);

  assert.equal(h.calls.create.length, 1);
  assert.deepEqual(h.calls.create[0], {
    url: markedXUrl("https://x.com/home"), active: false,
  });
  assert.equal(created.id, concurrent.id);
  assert.deepEqual(h.local.collectionOwner, ownerRecord(created.id));
  assert.equal(h.session.collectionTabId, created.id);
});

test("source jobs route canonical target metadata through the exact owned tab", async () => {
  const h = harness();
  h.local.collectionOwner = ownerRecord(7);
  h.session.collectionTabId = 7;
  h.values.set(7, { id: 7, url: markedXUrl("https://x.com/home"), status: "complete" });
  const controller = background.createBackground({
    chromeApi: h.chromeApi, protocol: protocol(), nowFn: () => 10_000,
  });

  await controller.runJob({
    id: "job-1", target_kind: "source", handle: "openai", profile_url: "https://x.com/openai",
    maximum: 30, timeout_ms: 45_000,
  });

  const navigation = h.calls.update.find(({ changes }) =>
    changes.url?.startsWith("https://x.com/openai"));
  assert.equal(navigation.changes.url, markedXUrl("https://x.com/openai"));
  const parsed = new URL(navigation.changes.url);
  assert.equal(parsed.origin, "https://x.com");
  assert.equal(parsed.pathname, "/openai");
  assert.equal(parsed.search, "");
  assert.equal(parsed.hash, `#xfeed-owner=${TEST_OWNER_NONCE}`);
  assert.deepEqual(h.calls.messages, [
    { id: 7, message: {
      type: "probe", targetKind: "source", handle: "openai", timeout_ms: 45_000,
    } },
    { id: 7, message: {
      type: "collect", jobId: "job-1", targetKind: "source", handle: "openai",
      maximum: 30, timeout_ms: 45_000,
    } },
  ]);
});

test("For You jobs route home target metadata through the exact owned tab", async () => {
  const h = harness();
  seedOwnedTab(h, 7, "https://x.com/openai");
  h.values.set(8, { id: 8, url: "https://x.com/home", status: "complete" });
  const controller = background.createBackground({
    chromeApi: h.chromeApi, protocol: protocol(), nowFn: () => 10_000,
  });

  await controller.runJob({
    id: "job-home", target_kind: "for_you", handle: null,
    profile_url: "https://x.com/home", maximum: 50, timeout_ms: 45_000,
  });

  assert.deepEqual(h.calls.update, [{
    id: 7,
    changes: {
      url: "https://x.com/home#xfeed-owner=test-owner-nonce-0123456789abcdef",
      active: true,
    },
  }]);
  assert.deepEqual(h.calls.messages, [
    { id: 7, message: {
      type: "probe", targetKind: "for_you", handle: null, timeout_ms: 45_000,
    } },
    { id: 7, message: {
      type: "collect", jobId: "job-home", targetKind: "for_you", handle: null,
      maximum: 50, timeout_ms: 45_000,
    } },
  ]);
  assert.equal(h.values.get(8).url, "https://x.com/home");
});

test("Following tab verification failure terminalizes before collection", async () => {
  const h = harness();
  seedOwnedTab(h);
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") {
      return {
        x_state: "signed_in",
        reason: "error",
        diagnostic: "Following tab could not be verified",
      };
    }
    throw new Error("collection must not start");
  };
  const p = protocol();
  const controller = background.createBackground({
    chromeApi: h.chromeApi, protocol: p, nowFn: () => 10_000,
  });

  await controller.runJob({
    id: "job-home", target_kind: "following", handle: null,
    profile_url: "https://x.com/home", maximum: 50, timeout_ms: 45_000,
  });

  assert.deepEqual(p.calls.complete, [{
    id: "job-home",
    reason: "error",
    diagnostic: "Following tab could not be verified",
  }]);
});

test("Following probe receives only the job deadline remaining after navigation", async () => {
  const h = harness();
  seedOwnedTab(h);
  let now = 10_000;
  const originalUpdate = h.chromeApi.tabs.update;
  h.chromeApi.tabs.update = async (id, changes) => {
    const tab = await originalUpdate(id, changes);
    if (changes.url) now += 600;
    return tab;
  };
  let probeMessage;
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") {
      probeMessage = message;
      return { x_state: "signed_in" };
    }
    return { reason: "exhausted", diagnostic: null };
  };
  const controller = background.createBackground({
    chromeApi: h.chromeApi, protocol: protocol(), nowFn: () => now,
  });

  await controller.runJob({
    id: "job-home", target_kind: "following", handle: null,
    profile_url: "https://x.com/home", maximum: 50, timeout_ms: 2_000,
  });

  assert.deepEqual(probeMessage, {
    type: "probe", targetKind: "following", handle: null, timeout_ms: 1_400,
  });
});

test("token removal clears and closes only the exact durable owned marker", async () => {
  const h = harness();
  h.local.collectionOwner = ownerRecord(7);
  h.session.collectionTabId = 7;
  h.values.set(7, { id: 7, url: markedXUrl("https://x.com/home"), status: "complete" });
  h.values.set(8, { id: 8, url: "https://x.com/user-tab", status: "complete" });
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: protocol() });
  await controller.bootstrapOwnedTab();
  delete h.local.token;

  await h.chromeApi.storage.onChanged.emit(
    { token: { oldValue: "secret-token", newValue: undefined } },
    "local",
  );

  assert.equal(h.local.collectionOwner, undefined);
  assert.equal(h.session.collectionTabId, undefined);
  assert.deepEqual(h.calls.remove, [7]);
  assert.equal(h.values.has(8), true);
});

test("pairing creates the owned tab when an unpaired reload bootstrap is still settling", async () => {
  const h = harness();
  delete h.local.token;
  const bootstrapRead = deferred();
  const releaseBootstrap = deferred();
  let firstRead = true;
  h.chromeApi.storage.local.get = async (key) => {
    const value = h.local[key];
    if (firstRead) {
      firstRead = false;
      bootstrapRead.resolve();
      await releaseBootstrap.promise;
    }
    return { [key]: value };
  };
  const controller = background.createBackground({
    chromeApi: h.chromeApi,
    protocol: protocol({ async pair() { h.local.token = "new-token"; } }),
  });
  await bootstrapRead.promise;

  const pairing = h.runtimeMessage({ type: "pair", code: "123456" });
  releaseBootstrap.resolve();
  const response = await pairing;

  assert.equal(response.ok, true);
  assert.equal(h.calls.create.length, 1);
  assert.equal(Number.isInteger(h.session.collectionTabId), true);
  assert.equal((await controller.getOwnedTab(false)).id, h.session.collectionTabId);
});

test("a failed owned-tab setup terminalizes the job and allows the next poll", async () => {
  const h = harness();
  delete h.local.token;
  let createCalls = 0;
  const originalCreate = h.chromeApi.tabs.create;
  h.chromeApi.tabs.create = async (options) => {
    createCalls += 1;
    if (createCalls === 1) throw new Error("tab creation failed");
    return originalCreate(options);
  };
  const jobs = [
    { id: "job-1", handle: "openai", profile_url: "https://x.com/openai", maximum: 1, timeout_ms: 1_000 },
    { id: "job-2", handle: "openai", profile_url: "https://x.com/openai", maximum: 1, timeout_ms: 1_000 },
  ];
  const p = protocol({
    async pollJob() {
      p.calls.poll += 1;
      return { job: jobs.shift() || null };
    },
  });
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: p });
  await controller.bootstrapOwnedTab();
  h.local.token = "secret-token";

  await controller.cycle();
  await new Promise((resolve) => setImmediate(resolve));
  await controller.cycle();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(p.calls.poll, 2);
  assert.deepEqual(p.calls.complete, [
    { id: "job-1", reason: "error", diagnostic: "tab creation failed" },
    { id: "job-2", reason: "exhausted", diagnostic: null },
  ]);
});

test("only the marked tab is restored after off-X navigation", async () => {
  const h = harness();
  seedOwnedTab(h);
  h.values.set(8, { id: 8, url: "https://example.com", status: "complete" });
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: protocol() });
  await controller.bootstrapOwnedTab();
  h.values.get(7).url = "https://example.com";
  await h.chromeApi.tabs.onUpdated.emit(7, { url: "https://example.com" }, { ...h.values.get(7) });
  await h.chromeApi.tabs.onUpdated.emit(8, { url: "https://example.com" }, { ...h.values.get(8) });
  assert.deepEqual(h.calls.update, [{
    id: 7, changes: { url: markedXUrl("https://x.com/home"), active: false },
  }]);
  assert.equal(h.calls.create.length, 0);
});

test("tab removal and concurrent recovery keep one replacement marked", async () => {
  const h = harness();
  seedOwnedTab(h);
  const removalPaused = deferred();
  const releaseRemoval = deferred();
  let removeCalls = 0;
  h.chromeApi.storage.session.remove = async (key) => {
    removeCalls += 1;
    if (removeCalls === 1) {
      removalPaused.resolve();
      await releaseRemoval.promise;
    }
    delete h.session[key];
  };
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: protocol() });
  h.values.delete(7);
  const removed = h.chromeApi.tabs.onRemoved.emit(7, { isWindowClosing: false });
  await removalPaused.promise;
  const concurrent = controller.getOwnedTab(true);
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  releaseRemoval.resolve();
  const [, replacement] = await Promise.all([removed, concurrent]);
  assert.equal(h.calls.create.length, 1);
  assert.equal(h.session.collectionTabId, replacement.id);
});

test("a cancellation closes only the owned tab and creates one replacement", async () => {
  const h = harness();
  seedOwnedTab(h);
  const intervals = manualIntervals();
  const removalStarted = deferred();
  const releaseRemoval = deferred();
  h.chromeApi.tabs.remove = async (id) => {
    h.calls.remove.push(id);
    removalStarted.resolve();
    await releaseRemoval.promise;
    h.values.delete(id);
  };
  h.chromeApi.tabMessage = async (_id, message) => message.type === "probe"
    ? { x_state: "signed_in" } : { cancelled: true };
  const p = protocol();
  const controller = background.createBackground({
    chromeApi: h.chromeApi,
    protocol: p,
    setIntervalFn: intervals.setIntervalFn,
    clearIntervalFn: intervals.clearIntervalFn,
  });
  const running = controller.runJob({
    id: "job-1", handle: "openai", profile_url: "https://x.com/openai", maximum: 30,
  });
  await removalStarted.promise;
  assert.equal(intervals.calls.clear.length, 1);
  releaseRemoval.resolve();
  await running;
  assert.deepEqual(h.calls.remove, [7]);
  assert.equal(h.calls.create.length, 1);
  assert.equal(p.calls.complete.length, 0);
});

test("a dedicated-tab tick wakes heartbeat and polling", async () => {
  const h = harness();
  seedOwnedTab(h);
  const gate = deferred();
  const p = protocol({ async heartbeat(state) { p.calls.heartbeat.push(state); await gate.promise; return { x_state: state }; } });
  background.createBackground({ chromeApi: h.chromeApi, protocol: p });
  await h.runtimeMessage({ type: "tick" }, { tab: { id: 7 } });
  const refresh = h.runtimeMessage({ type: "refresh" });
  gate.resolve();
  await refresh;
  assert.deepEqual(p.calls.heartbeat, ["signed_in"]);
  assert.equal(p.calls.poll, 1);
});

test("a paired keep-alive alarm publishes heartbeats without an owned-tab tick", async () => {
  const h = harness();
  const alarms = manualAlarms();
  let pollCalls = 0;
  const p = protocol({
    async pollJob() { pollCalls += 1; return { job: null }; },
  });
  h.chromeApi.alarms = {
    create: alarms.create,
    clear: alarms.clear,
    onAlarm: alarms.onAlarm,
  };
  background.createBackground({ chromeApi: h.chromeApi, protocol: p });
  await h.runtimeMessage({ type: "refresh" });
  assert.equal(p.calls.heartbeat.length, 1);
  assert.deepEqual(alarms.calls.create, [{
    name: "xfeed-keepalive", info: { periodInMinutes: 0.5 },
  }]);
  await alarms.fire("xfeed-keepalive");
  await alarms.fire("xfeed-keepalive");
  assert.equal(p.calls.heartbeat.length, 3);
  assert.equal(pollCalls, 3);
});

test("idle keep-alive keeps the last confirmed X state after an owned-tab probe", async () => {
  const h = harness();
  seedOwnedTab(h);
  const alarms = manualAlarms();
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") return { x_state: "signed_in" };
    return {};
  };
  const p = protocol({ async pollJob() { return { job: null }; } });
  h.chromeApi.alarms = {
    create: alarms.create,
    clear: alarms.clear,
    onAlarm: alarms.onAlarm,
  };
  background.createBackground({ chromeApi: h.chromeApi, protocol: p });

  await alarms.fire("xfeed-keepalive");
  await alarms.fire("xfeed-keepalive");
  assert.ok(p.calls.heartbeat.every((state) => state === "signed_in"));
});

test("accepted ticks keep heartbeating while one collection job is blocked", async () => {
  const h = harness();
  seedOwnedTab(h);
  const collectionStarted = deferred();
  const collectionGate = deferred();
  const completed = deferred();
  let collectCalls = 0;
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") return { x_state: "signed_in" };
    if (message.type === "collect") {
      collectCalls += 1;
      collectionStarted.resolve();
      return collectionGate.promise;
    }
    return {};
  };
  const job = {
    id: "job-1", target_kind: "source", handle: "openai", profile_url: "https://x.com/openai",
    maximum: 30, timeout_ms: 45_000,
  };
  const p = protocol({
    async pollJob() {
      p.calls.poll += 1;
      return { job: p.calls.poll === 1 ? job : null };
    },
    async completeJob(id, reason, diagnostic) {
      p.calls.complete.push({ id, reason, diagnostic });
      completed.resolve();
      return { accepted: true };
    },
  });
  const controller = background.createBackground({
    chromeApi: h.chromeApi, protocol: p, nowFn: () => 10_000,
  });

  const firstCycle = controller.cycle();
  await collectionStarted.promise;
  await h.runtimeMessage({ type: "tick" }, { tab: { id: 7 } });
  await new Promise((resolve) => setImmediate(resolve));
  await h.runtimeMessage({ type: "tick" }, { tab: { id: 7 } });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(p.calls.heartbeat.length, 4);
  assert.equal(p.calls.poll, 1);
  assert.equal(collectCalls, 1);
  assert.equal(
    h.calls.messages.find(({ message }) => message.type === "collect").message.timeout_ms,
    45_000,
  );

  collectionGate.resolve({ reason: "exhausted", diagnostic: null });
  await completed.promise;
  await firstCycle;
  assert.deepEqual(p.calls.complete, [
    { id: "job-1", reason: "exhausted", diagnostic: null },
  ]);
});

test("one active-job pump heartbeats through blocked navigation and stops at terminal", async () => {
  const h = harness();
  seedOwnedTab(h);
  const intervals = manualIntervals();
  const navigationBlocked = deferred();
  const collectionStarted = deferred();
  const collectionGate = deferred();
  const completed = deferred();
  let now = 10_000;
  let collectCalls = 0;
  const originalUpdate = h.chromeApi.tabs.update;
  h.chromeApi.tabs.update = async (id, changes) => {
    const tab = await originalUpdate(id, changes);
    if (changes.url && new URL(changes.url).pathname === "/openai") {
      h.values.get(id).status = "loading";
      tab.status = "loading";
      navigationBlocked.resolve();
    }
    return tab;
  };
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") return { x_state: "signed_in" };
    if (message.type === "collect") {
      collectCalls += 1;
      collectionStarted.resolve();
      await collectionGate.promise;
      return { reason: "exhausted", diagnostic: null };
    }
    return {};
  };
  const job = {
    id: "job-1", handle: "openai", profile_url: "https://x.com/openai",
    maximum: 30, timeout_ms: 45_000,
  };
  const p = protocol({
    async pollJob() {
      p.calls.poll += 1;
      return { job: p.calls.poll === 1 ? job : null };
    },
    async completeJob(id, reason, diagnostic) {
      p.calls.complete.push({ id, reason, diagnostic });
      completed.resolve();
      return { accepted: true };
    },
  });
  const controller = background.createBackground({
    chromeApi: h.chromeApi,
    protocol: p,
    nowFn: () => now,
    setIntervalFn: intervals.setIntervalFn,
    clearIntervalFn: intervals.clearIntervalFn,
  });

  await controller.cycle();
  await navigationBlocked.promise;
  assert.deepEqual(intervals.calls.start.map(({ delay }) => delay), [1_000]);
  for (let elapsed = 0; elapsed < 4_000; elapsed += 1_000) {
    now += 1_000;
    await intervals.tick();
  }
  assert.equal(p.calls.heartbeat.length, 5);
  assert.equal(p.calls.poll, 1);
  assert.equal(collectCalls, 0);

  h.values.get(7).status = "complete";
  await h.chromeApi.tabs.onUpdated.emit(7, { status: "complete" }, { ...h.values.get(7) });
  await collectionStarted.promise;
  for (let elapsed = 0; elapsed < 4_000; elapsed += 1_000) {
    now += 1_000;
    await intervals.tick();
  }
  assert.equal(p.calls.poll, 1);
  assert.equal(collectCalls, 1);
  assert.equal(p.calls.heartbeat.length, 10);
  collectionGate.resolve();
  await completed.promise;
  await new Promise((resolve) => setImmediate(resolve));
  const heartbeatCountAtTerminal = p.calls.heartbeat.length;
  await intervals.tick();

  assert.equal(p.calls.poll, 1);
  assert.equal(collectCalls, 1);
  assert.equal(intervals.calls.start.length, 1);
  assert.equal(intervals.calls.clear.length, 1);
  assert.equal(p.calls.heartbeat.length, heartbeatCountAtTerminal);
  assert.deepEqual(p.calls.complete, [
    { id: "job-1", reason: "exhausted", diagnostic: null },
  ]);
});

test("navigation time is deducted from the relative content timeout", async () => {
  const h = harness();
  seedOwnedTab(h);
  let now = 10_000;
  let measureNavigation = false;
  const navigationTimeouts = [];
  const originalUpdate = h.chromeApi.tabs.update;
  h.chromeApi.tabs.update = async (id, changes) => {
    const tab = await originalUpdate(id, changes);
    if (measureNavigation && changes.url && new URL(changes.url).pathname === "/openai") {
      now += 600;
    }
    return tab;
  };
  const controller = background.createBackground({
    chromeApi: h.chromeApi,
    protocol: protocol(),
    nowFn: () => now,
    setTimeoutFn(_callback, delay) {
      navigationTimeouts.push(delay);
      return navigationTimeouts.length;
    },
    clearTimeoutFn() {},
  });
  await controller.bootstrapOwnedTab();
  measureNavigation = true;

  await controller.runJob({
    id: "job-1", handle: "openai", profile_url: "https://x.com/openai",
    maximum: 30, timeout_ms: 2_000,
  });

  const collect = h.calls.messages.find(({ message }) => message.type === "collect");
  assert.equal(navigationTimeouts[0], 1_400);
  assert.equal(collect.message.timeout_ms, 1_400);
});

const unsafeCases = [
  ["signed_out", "login_wall", "X is signed out in Opera GX; sign in and reconnect"],
  ["challenge", "error", "X presented an account challenge in Opera GX"],
  ["rate_limited", "error", "X rate-limited the Opera GX session"],
];

for (const unsafeState of ["challenge", "rate_limited"]) {
  test(`serialized heartbeat observations keep ${unsafeState} sticky after a delayed safe response`, async () => {
    const h = harness();
    seedOwnedTab(h);
    const safeStarted = deferred();
    const releaseSafe = deferred();
    const unsafeStarted = deferred();
    const releaseUnsafe = deferred();
    let probeCalls = 0;
    let collectCalls = 0;
    let heartbeatInFlight = 0;
    let maximumHeartbeatConcurrency = 0;
    h.chromeApi.tabMessage = async (_id, message) => {
      if (message.type === "probe") {
        probeCalls += 1;
        return { x_state: probeCalls === 1 ? "signed_in" : unsafeState };
      }
      if (message.type === "collect") {
        collectCalls += 1;
        return { reason: "exhausted", diagnostic: null };
      }
      return {};
    };
    const p = protocol({
      async heartbeat(state) {
        p.calls.heartbeat.push(state);
        heartbeatInFlight += 1;
        maximumHeartbeatConcurrency = Math.max(
          maximumHeartbeatConcurrency, heartbeatInFlight,
        );
        try {
          if (state === "signed_in") {
            safeStarted.resolve();
            await releaseSafe.promise;
          } else {
            unsafeStarted.resolve();
            await releaseUnsafe.promise;
          }
          return { x_state: state };
        } finally {
          heartbeatInFlight -= 1;
        }
      },
    });
    const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: p });
    const running = controller.runJob({
      id: "job-1", handle: "openai", profile_url: "https://x.com/openai",
      maximum: 30, timeout_ms: 45_000,
    });
    await safeStarted.promise;

    const laterObservation = controller.cycle();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(p.calls.heartbeat, ["signed_in"]);
    releaseSafe.resolve();
    await unsafeStarted.promise;
    const statusWhileUnsafePending = await h.runtimeMessage({ type: "status" });
    assert.notEqual(statusWhileUnsafePending.x_state, "signed_in");
    releaseUnsafe.resolve();
    await Promise.all([running, laterObservation]);

    assert.deepEqual(p.calls.heartbeat, ["signed_in", unsafeState]);
    assert.equal(maximumHeartbeatConcurrency, 1);
    assert.equal(p.calls.heartbeat.filter((state) => state === unsafeState).length, 1);
    assert.equal(collectCalls, 0);
    assert.equal(p.calls.complete.length, 0);
    const status = await h.runtimeMessage({ type: "status" });
    assert.equal(status.connected, true);
    assert.equal(status.x_state, unsafeState);
  });
}

for (const [unsafeState, _reason, diagnostic] of unsafeCases) {
  test(`an unsafe ${unsafeState} probe relies on heartbeat as the sole termination`, async () => {
    const h = harness();
    seedOwnedTab(h);
    h.chromeApi.tabMessage = async (_id, message) => {
      if (message.type === "probe") return { x_state: unsafeState };
      throw new Error("collection must not start");
    };
    const p = protocol();
    const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: p });
    await controller.runJob({ id: "job-1", handle: "openai", profile_url: "https://x.com/openai", maximum: 30 });
    assert.deepEqual(p.calls.heartbeat, [unsafeState]);
    assert.equal(p.calls.complete.length, 0);
    const status = await h.runtimeMessage({ type: "status" });
    assert.equal(status.connected, true);
    assert.equal(status.x_state, unsafeState);
    assert.equal(status.diagnostic, diagnostic);
  });

  test(`a mid-run ${unsafeState} result heartbeats once without stale completion`, async () => {
    const h = harness();
    seedOwnedTab(h);
    h.chromeApi.tabMessage = async (_id, message) => {
      if (message.type === "probe") return { x_state: "signed_in" };
      if (message.type === "collect") {
        return { x_state: unsafeState, reason: _reason, diagnostic };
      }
      return {};
    };
    const p = protocol();
    const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: p });

    await controller.runJob({
      id: "job-1", handle: "openai", profile_url: "https://x.com/openai",
      maximum: 30, timeout_ms: 45_000,
    });

    assert.deepEqual(p.calls.heartbeat, ["signed_in", unsafeState]);
    assert.equal(p.calls.complete.length, 0);
    const status = await h.runtimeMessage({ type: "status" });
    assert.equal(status.connected, true);
    assert.equal(status.x_state, unsafeState);
    assert.equal(status.diagnostic, diagnostic);
  });
}

test("unsafe heartbeat suppresses a late normal collection completion", async () => {
  const h = harness();
  seedOwnedTab(h);
  const collectionStarted = deferred();
  const collectionGate = deferred();
  let probeCalls = 0;
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") {
      probeCalls += 1;
      return { x_state: probeCalls === 1 ? "signed_in" : "challenge" };
    }
    if (message.type === "collect") {
      collectionStarted.resolve();
      return collectionGate.promise;
    }
    return {};
  };
  const p = protocol();
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: p });
  const running = controller.runJob({
    id: "job-1", handle: "openai", profile_url: "https://x.com/openai",
    maximum: 30, timeout_ms: 45_000,
  });
  await collectionStarted.promise;

  await controller.cycle();
  collectionGate.resolve({ reason: "exhausted", diagnostic: null });
  await running;

  assert.deepEqual(p.calls.heartbeat, ["signed_in", "challenge"]);
  assert.equal(p.calls.complete.length, 0);
  const status = await h.runtimeMessage({ type: "status" });
  assert.equal(status.connected, true);
  assert.equal(status.x_state, "challenge");
  assert.equal(status.diagnostic, "X presented an account challenge in Opera GX");
});

test("a rejected unsafe heartbeat errors the job instead of faking terminalization", async () => {
  const h = harness();
  seedOwnedTab(h);
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") return { x_state: "challenge" };
    return {};
  };
  const p = protocol({
    async heartbeat(state) {
      p.calls.heartbeat.push(state);
      throw new Error("heartbeat unavailable");
    },
  });
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: p });

  await assert.rejects(
    controller.runJob({
      id: "job-1", handle: "openai", profile_url: "https://x.com/openai",
      maximum: 30, timeout_ms: 45_000,
    }),
    /heartbeat unavailable/,
  );

  assert.deepEqual(p.calls.complete, [
    { id: "job-1", reason: "error", diagnostic: "heartbeat unavailable" },
  ]);
});

test("a failed mid-run unsafe heartbeat suppresses a late normal completion", async () => {
  const h = harness();
  seedOwnedTab(h);
  const collectionStarted = deferred();
  const collectionGate = deferred();
  let probeCalls = 0;
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") {
      probeCalls += 1;
      return { x_state: probeCalls === 1 ? "signed_in" : "challenge" };
    }
    if (message.type === "collect") {
      collectionStarted.resolve();
      return collectionGate.promise;
    }
    return {};
  };
  const p = protocol({
    async heartbeat(state) {
      p.calls.heartbeat.push(state);
      if (state === "challenge") throw new Error("heartbeat unavailable");
      return { x_state: state };
    },
  });
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: p });
  const running = controller.runJob({
    id: "job-1", handle: "openai", profile_url: "https://x.com/openai",
    maximum: 30, timeout_ms: 45_000,
  });
  await collectionStarted.promise;

  await controller.cycle();
  collectionGate.resolve({ reason: "exhausted", diagnostic: null });
  await assert.rejects(running, /heartbeat unavailable/);

  assert.deepEqual(p.calls.complete, [
    { id: "job-1", reason: "error", diagnostic: "heartbeat unavailable" },
  ]);
});

test("photo jobs navigate the same owned tab and complete with only their manifest", async () => {
  const h = harness();
  seedOwnedTab(h);
  const photo = {
    position: 0,
    url: "https://pbs.twimg.com/media/one?format=jpg&name=large",
    alt_text: "One",
  };
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") return { x_state: "signed_in" };
    if (message.type === "resolve-post-photos") {
      assert.deepEqual(message, {
        type: "resolve-post-photos",
        jobId: "job-media-1",
        postId: "123",
      });
      return { reason: "exhausted", diagnostic: null, photos: [photo] };
    }
    throw new Error(`unexpected tab message ${message.type}`);
  };
  const p = protocol();
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: p });
  const job = {
    id: "job-media-1",
    kind: "resolve_post_photos",
    post_id: "123",
    post_url: "https://x.com/openai/status/123",
    timeout_ms: 45_000,
  };

  await controller.runJob(job);

  assert.deepEqual(h.calls.update, [{
    id: 7,
    changes: {
      url: markedXUrl("https://x.com/openai/status/123"),
      active: false,
    },
  }]);
  assert.deepEqual(p.calls.complete, [{
    job,
    result: { reason: "exhausted", diagnostic: null, photos: [photo] },
  }]);
  assert.equal(p.calls.progress.length, 0);
});

test("collection progress is rejected while a photo job owns the active slot", async () => {
  const h = harness();
  seedOwnedTab(h);
  const resolutionStarted = deferred();
  const resolutionDone = deferred();
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") return { x_state: "signed_in" };
    if (message.type === "resolve-post-photos") {
      resolutionStarted.resolve();
      return resolutionDone.promise;
    }
    return {};
  };
  const p = protocol();
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: p });
  const running = controller.runJob({
    id: "job-media-1",
    kind: "resolve_post_photos",
    post_id: "123",
    post_url: "https://x.com/openai/status/123",
    timeout_ms: 45_000,
  });
  await resolutionStarted.promise;

  const response = await h.runtimeMessage({
    type: "collection-progress",
    jobId: "job-media-1",
    observations: [],
  }, { tab: { id: 7 } });

  assert.match(response.error, /not a collection job/i);
  assert.equal(p.calls.progress.length, 0);
  resolutionDone.resolve({ reason: "exhausted", diagnostic: null, photos: [] });
  await running;
});

test("photo resolution cancellation closes only the owned tab and creates one replacement", async () => {
  const h = harness();
  seedOwnedTab(h);
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") return { x_state: "signed_in" };
    if (message.type === "resolve-post-photos") return { cancelled: true };
    return {};
  };
  const p = protocol();
  const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: p });

  await controller.runJob({
    id: "job-media-1",
    kind: "resolve_post_photos",
    post_id: "123",
    post_url: "https://x.com/openai/status/123",
    timeout_ms: 45_000,
  });

  assert.deepEqual(h.calls.remove, [7]);
  assert.equal(h.calls.create.length, 1);
  assert.equal(p.calls.complete.length, 0);
});

test("photo navigation time is deducted before the one exact extraction", async () => {
  const h = harness();
  seedOwnedTab(h);
  let now = 10_000;
  const originalUpdate = h.chromeApi.tabs.update;
  h.chromeApi.tabs.update = async (id, changes) => {
    const tab = await originalUpdate(id, changes);
    if (changes.url && new URL(changes.url).pathname.includes("/status/")) now += 600;
    return tab;
  };
  let extractionNow = null;
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") return { x_state: "signed_in" };
    if (message.type === "resolve-post-photos") {
      extractionNow = now;
      return { reason: "exhausted", diagnostic: null, photos: [] };
    }
    return {};
  };
  const p = protocol();
  const controller = background.createBackground({
    chromeApi: h.chromeApi,
    protocol: p,
    nowFn: () => now,
  });

  await controller.runJob({
    id: "job-media-1",
    kind: "resolve_post_photos",
    post_id: "123",
    post_url: "https://x.com/openai/status/123",
    timeout_ms: 2_000,
  });

  assert.equal(extractionNow, 10_600);
  assert.deepEqual(p.calls.complete[0].result.photos, []);
});

test("a blocked photo extraction is bounded by the remaining relative timeout", async () => {
  const h = harness();
  seedOwnedTab(h);
  const extractionStarted = deferred();
  const extractionGate = deferred();
  let now = 10_000;
  const timers = new Map();
  let nextTimerId = 1;
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") return { x_state: "signed_in" };
    if (message.type === "resolve-post-photos") {
      extractionStarted.resolve();
      return extractionGate.promise;
    }
    return {};
  };
  const p = protocol();
  const controller = background.createBackground({
    chromeApi: h.chromeApi,
    protocol: p,
    nowFn: () => now,
    setTimeoutFn(callback, delay) {
      const id = nextTimerId++;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeoutFn(id) {
      timers.delete(id);
    },
  });
  const running = controller.runJob({
    id: "job-media-1",
    kind: "resolve_post_photos",
    post_id: "123",
    post_url: "https://x.com/openai/status/123",
    timeout_ms: 2_000,
  });
  await extractionStarted.promise;

  let timerAssertion;
  try {
    assert.deepEqual([...timers.values()].map(({ delay }) => delay), [2_000]);
    now += 2_000;
    [...timers.values()][0].callback();
  } catch (error) {
    timerAssertion = error;
  } finally {
    extractionGate.resolve({ reason: "exhausted", diagnostic: null, photos: [] });
  }
  await running;
  if (timerAssertion) throw timerAssertion;

  assert.deepEqual(p.calls.complete, [{
    job: {
      id: "job-media-1",
      kind: "resolve_post_photos",
      post_id: "123",
      post_url: "https://x.com/openai/status/123",
      timeout_ms: 2_000,
    },
    result: { reason: "timeout", diagnostic: null, photos: [] },
  }]);
});

test("a blocked media safety probe is bounded by the remaining relative timeout", async () => {
  const h = harness();
  seedOwnedTab(h);
  const probeStarted = deferred();
  const probeGate = deferred();
  let now = 10_000;
  const timers = new Map();
  let nextTimerId = 1;
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") {
      probeStarted.resolve();
      return probeGate.promise;
    }
    if (message.type === "resolve-post-photos") {
      return { reason: "exhausted", diagnostic: null, photos: [] };
    }
    return {};
  };
  const p = protocol();
  const job = {
    id: "job-media-1",
    kind: "resolve_post_photos",
    post_id: "123",
    post_url: "https://x.com/openai/status/123",
    timeout_ms: 2_000,
  };
  const controller = background.createBackground({
    chromeApi: h.chromeApi,
    protocol: p,
    nowFn: () => now,
    setTimeoutFn(callback, delay) {
      const id = nextTimerId++;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeoutFn(id) {
      timers.delete(id);
    },
  });
  const running = controller.runJob(job);
  await probeStarted.promise;

  let timerAssertion;
  try {
    assert.deepEqual([...timers.values()].map(({ delay }) => delay), [2_000]);
    now += 2_000;
    [...timers.values()][0].callback();
  } catch (error) {
    timerAssertion = error;
  } finally {
    probeGate.resolve({ x_state: "signed_in" });
  }
  await running;
  if (timerAssertion) throw timerAssertion;

  assert.deepEqual(p.calls.complete, [{
    job,
    result: { reason: "timeout", diagnostic: null, photos: [] },
  }]);
});

test("desktop cancellation aborts a blocked media safety probe", async () => {
  const h = harness();
  seedOwnedTab(h);
  const intervals = manualIntervals();
  const probeStarted = deferred();
  const probeGate = deferred();
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") {
      probeStarted.resolve();
      return probeGate.promise;
    }
    throw new Error("photo extraction must not start");
  };
  const p = protocol({
    async pollJob() {
      p.calls.poll += 1;
      return { job: null, cancelled: true };
    },
  });
  const controller = background.createBackground({
    chromeApi: h.chromeApi,
    protocol: p,
    setIntervalFn: intervals.setIntervalFn,
    clearIntervalFn: intervals.clearIntervalFn,
  });
  const running = controller.runJob({
    id: "job-media-1",
    kind: "resolve_post_photos",
    post_id: "123",
    post_url: "https://x.com/openai/status/123",
    timeout_ms: 45_000,
  });
  let settled = false;
  running.finally(() => { settled = true; });
  await probeStarted.promise;

  await intervals.tick();
  await new Promise((resolve) => setImmediate(resolve));
  let cancellationAssertion;
  try {
    assert.equal(settled, true);
    assert.deepEqual(h.calls.remove, [7]);
    assert.equal(h.calls.create.length, 1);
  } catch (error) {
    cancellationAssertion = error;
  } finally {
    probeGate.resolve({ x_state: "signed_in" });
  }
  await running;
  if (cancellationAssertion) throw cancellationAssertion;

  assert.equal(p.calls.complete.length, 0);
});

test("desktop cancellation aborts blocked media work before polling its successor", async () => {
  const h = harness();
  seedOwnedTab(h);
  const intervals = manualIntervals();
  const extractionStarted = deferred();
  const extractionGate = deferred();
  let resolutionCalls = 0;
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") return { x_state: "signed_in" };
    if (message.type === "resolve-post-photos") {
      resolutionCalls += 1;
      if (resolutionCalls === 1) {
        extractionStarted.resolve();
        return extractionGate.promise;
      }
      return { reason: "exhausted", diagnostic: null, photos: [] };
    }
    return {};
  };
  const successor = {
    id: "job-media-2",
    kind: "resolve_post_photos",
    post_id: "456",
    post_url: "https://x.com/openai/status/456",
    timeout_ms: 45_000,
  };
  const p = protocol({
    async pollJob() {
      p.calls.poll += 1;
      if (p.calls.poll === 1) return { job: null, cancelled: true };
      if (p.calls.poll === 2) return { job: successor, cancelled: false };
      return { job: null, cancelled: false };
    },
  });
  const controller = background.createBackground({
    chromeApi: h.chromeApi,
    protocol: p,
    setIntervalFn: intervals.setIntervalFn,
    clearIntervalFn: intervals.clearIntervalFn,
  });
  const cancelledJob = {
    id: "job-media-1",
    kind: "resolve_post_photos",
    post_id: "123",
    post_url: "https://x.com/openai/status/123",
    timeout_ms: 45_000,
  };
  const running = controller.runJob(cancelledJob);
  await extractionStarted.promise;

  await intervals.tick();
  let cancellationAssertion;
  try {
    assert.deepEqual(h.calls.remove, [7]);
    assert.equal(h.calls.create.length, 1);
  } catch (error) {
    cancellationAssertion = error;
  } finally {
    extractionGate.resolve({ reason: "exhausted", diagnostic: null, photos: [] });
  }
  await running;
  if (cancellationAssertion) throw cancellationAssertion;

  await controller.cycle();
  for (let attempt = 0; attempt < 10 && p.calls.complete.length === 0; attempt += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }

  assert.equal(resolutionCalls, 2);
  assert.deepEqual(p.calls.complete, [{
    job: successor,
    result: { reason: "exhausted", diagnostic: null, photos: [] },
  }]);
});

test("normal media completion cannot let its old cancellation poll consume a successor", async () => {
  const h = harness();
  seedOwnedTab(h);
  const intervals = manualIntervals();
  const extractionStarted = deferred();
  const extractionGate = deferred();
  const pollStarted = deferred();
  const pollRelease = deferred();
  const successor = {
    id: "job-media-2",
    kind: "resolve_post_photos",
    post_id: "456",
    post_url: "https://x.com/openai/status/456",
    timeout_ms: 45_000,
  };
  let activeBridgeJobId = "job-media-1";
  let successorDelivered = false;
  h.chromeApi.tabMessage = async (_id, message) => {
    if (message.type === "probe") return { x_state: "signed_in" };
    if (message.type === "resolve-post-photos" && message.jobId === "job-media-1") {
      extractionStarted.resolve();
      return extractionGate.promise;
    }
    if (message.type === "resolve-post-photos") {
      return { reason: "exhausted", diagnostic: null, photos: [] };
    }
    return {};
  };
  const p = protocol({
    async pollJob() {
      p.calls.poll += 1;
      if (p.calls.poll === 1) {
        pollStarted.resolve();
        await pollRelease.promise;
      }
      if (activeBridgeJobId === "job-media-1") {
        return { job: null, cancelled: false };
      }
      if (!successorDelivered) {
        successorDelivered = true;
        return { job: successor, cancelled: false };
      }
      return { job: null, cancelled: false };
    },
    async completeJob(job, result) {
      p.calls.complete.push({ job, result });
      if (job.id === "job-media-1") activeBridgeJobId = successor.id;
      else activeBridgeJobId = null;
      return { accepted: true };
    },
  });
  const controller = background.createBackground({
    chromeApi: h.chromeApi,
    protocol: p,
    setIntervalFn: intervals.setIntervalFn,
    clearIntervalFn: intervals.clearIntervalFn,
  });
  const original = {
    id: "job-media-1",
    kind: "resolve_post_photos",
    post_id: "123",
    post_url: "https://x.com/openai/status/123",
    timeout_ms: 45_000,
  };
  const running = controller.runJob(original);
  await extractionStarted.promise;
  const pump = intervals.tick();
  await pollStarted.promise;

  extractionGate.resolve({ reason: "exhausted", diagnostic: null, photos: [] });
  await new Promise((resolve) => setImmediate(resolve));
  pollRelease.resolve();
  await Promise.all([running, pump]);

  await controller.cycle();
  for (let attempt = 0; attempt < 10 && p.calls.complete.length < 2; attempt += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }

  assert.deepEqual(p.calls.complete, [
    {
      job: original,
      result: { reason: "exhausted", diagnostic: null, photos: [] },
    },
    {
      job: successor,
      result: { reason: "exhausted", diagnostic: null, photos: [] },
    },
  ]);
});

for (const unsafeState of ["signed_out", "challenge", "rate_limited"]) {
  test(`photo ${unsafeState} safety heartbeat suppresses media completion`, async () => {
    const h = harness();
    seedOwnedTab(h);
    h.chromeApi.tabMessage = async (_id, message) => {
      if (message.type === "probe") return { x_state: unsafeState };
      throw new Error("photo extraction must not start");
    };
    const p = protocol();
    const controller = background.createBackground({ chromeApi: h.chromeApi, protocol: p });

    await controller.runJob({
      id: "job-media-1",
      kind: "resolve_post_photos",
      post_id: "123",
      post_url: "https://x.com/openai/status/123",
      timeout_ms: 45_000,
    });

    assert.deepEqual(p.calls.heartbeat, [unsafeState]);
    assert.equal(p.calls.complete.length, 0);
  });
}
