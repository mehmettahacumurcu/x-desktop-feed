import assert from "node:assert/strict";
import test from "node:test";

const stored = {};
globalThis.chrome = { storage: { local: {
  async get(key) { return { [key]: stored[key] }; },
  async set(value) { Object.assign(stored, value); },
  async remove(key) { delete stored[key]; },
} } };

let calls = [];
function response(payload, { status = 200, headers = {}, chunks, readError } = {}) {
  const encoded = new TextEncoder().encode(payload);
  const bodyChunks = chunks || [encoded];
  let index = 0;
  return {
    ok: status >= 200 && status < 300,
    status,
    textCalled: false,
    headers: { get(name) {
      const found = Object.keys(headers).find((key) => key.toLowerCase() === name.toLowerCase());
      return found ? headers[found] : null;
    } },
    body: { getReader() { return {
      async read() {
        if (readError) throw readError;
        if (index >= bodyChunks.length) return { done: true, value: undefined };
        return { done: false, value: bodyChunks[index++] };
      },
      async cancel() {},
    }; } },
    async text() {
      this.textCalled = true;
      if (readError) throw readError;
      return payload;
    },
  };
}

const okFetch = async (url, options) => {
  calls.push({ url, options });
  return response(JSON.stringify({ protocol_version: 4, token: "paired-token", job: null }));
};
globalThis.fetch = okFetch;

const api = await import("../protocol.js");

test.beforeEach(() => { calls = []; delete stored.token; globalThis.fetch = okFetch; });

test("pair uses the fixed loopback endpoint and persists returned token", async () => {
  const response = await api.pair("ABC123");
  assert.equal(calls[0].url, "http://127.0.0.1:47831/v1/pair");
  assert.equal(calls[0].options.headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(calls[0].options.body), { code: "ABC123" });
  assert.equal(stored.token, "paired-token");
  assert.equal(response.protocol_version, 4);
});

test("authenticated requests send bearer auth and protocol version", async () => {
  stored.token = "secret-token";
  await api.heartbeat("signed_in");
  assert.equal(calls[0].url, "http://127.0.0.1:47831/v1/heartbeat");
  assert.equal(calls[0].options.headers.Authorization, "Bearer secret-token");
  assert.deepEqual(JSON.parse(calls[0].options.body), { protocol_version: 4, x_state: "signed_in" });
});

test("missing bearer leaves no request timeout behind", async () => {
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const active = new Set();
  globalThis.setTimeout = (_callback, delay) => {
    const id = { delay };
    active.add(id);
    return id;
  };
  globalThis.clearTimeout = (id) => { active.delete(id); };
  try {
    await assert.rejects(api.heartbeat("signed_in"), /not paired/i);
    assert.equal(active.size, 0);
    assert.equal(calls.length, 0);
  } finally {
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }
});

test("poll sends the exact empty request contract", async () => {
  stored.token = "secret-token";
  await api.pollJob();
  assert.deepEqual(JSON.parse(calls[0].options.body), {});
});

test("poll rejects a job that could navigate outside the exact X profile", async () => {
  stored.token = "secret-token";
  globalThis.fetch = async () => response(JSON.stringify({
    protocol_version: 4, cancelled: false,
    job: { id: "job-1", target_kind: "source", handle: "openai", profile_url: "https://evil.example/openai", maximum: 30, timeout_ms: 45_000 },
  }));
  await assert.rejects(api.pollJob(), /invalid job/i);
});

test("poll accepts only the exact bounded relative timeout job schema", async () => {
  stored.token = "secret-token";
  const validJob = {
    id: "job-1", target_kind: "source", handle: "openai", profile_url: "https://x.com/openai",
    maximum: 30, timeout_ms: 12_345,
  };
  globalThis.fetch = async () => response(JSON.stringify({
    protocol_version: 4, cancelled: false, job: validJob,
  }));

  const payload = await api.pollJob();

  assert.deepEqual(payload.job, validJob);
});

test("poll accepts the exact For You job schema", async () => {
  stored.token = "secret-token";
  const homeJob = {
    id: "job-home", target_kind: "for_you", handle: null,
    profile_url: "https://x.com/home", maximum: 50, timeout_ms: 45_000,
  };
  globalThis.fetch = async () => response(JSON.stringify({
    protocol_version: 4, cancelled: false, job: homeJob,
  }));

  assert.deepEqual((await api.pollJob()).job, homeJob);
});

test("poll accepts the exact Following job schema at protocol version 4", async () => {
  stored.token = "secret-token";
  const homeJob = {
    id: "job-home", target_kind: "following", handle: null,
    profile_url: "https://x.com/home", maximum: 50, timeout_ms: 45_000,
  };
  globalThis.fetch = async () => response(JSON.stringify({
    protocol_version: 4, cancelled: false, job: homeJob,
  }));

  assert.deepEqual((await api.pollJob()).job, homeJob);
});

for (const [name, job] of [
  ["unknown target kind", {
    id: "job-1", target_kind: "likes", handle: "openai",
    profile_url: "https://x.com/openai", maximum: 30, timeout_ms: 45_000,
  }],
  ["non-null For You handle", {
    id: "job-home", target_kind: "for_you", handle: "openai",
    profile_url: "https://x.com/home", maximum: 50, timeout_ms: 45_000,
  }],
  ["non-home For You URL", {
    id: "job-home", target_kind: "for_you", handle: null,
    profile_url: "https://x.com/openai", maximum: 50, timeout_ms: 45_000,
  }],
  ["non-null Following handle", {
    id: "job-home", target_kind: "following", handle: "openai",
    profile_url: "https://x.com/home", maximum: 50, timeout_ms: 45_000,
  }],
  ["non-home Following URL", {
    id: "job-home", target_kind: "following", handle: null,
    profile_url: "https://x.com/openai", maximum: 50, timeout_ms: 45_000,
  }],
  ["source URL and handle mismatch", {
    id: "job-1", target_kind: "source", handle: "openai",
    profile_url: "https://x.com/nasa", maximum: 30, timeout_ms: 45_000,
  }],
  ["source maximum 31", {
    id: "job-1", target_kind: "source", handle: "openai",
    profile_url: "https://x.com/openai", maximum: 31, timeout_ms: 45_000,
  }],
  ["For You maximum 51", {
    id: "job-home", target_kind: "for_you", handle: null,
    profile_url: "https://x.com/home", maximum: 51, timeout_ms: 45_000,
  }],
  ["Following maximum 51", {
    id: "job-home", target_kind: "following", handle: null,
    profile_url: "https://x.com/home", maximum: 51, timeout_ms: 45_000,
  }],
]) {
  test(`poll rejects ${name}`, async () => {
    stored.token = "secret-token";
    globalThis.fetch = async () => response(JSON.stringify({
      protocol_version: 4, cancelled: false, job,
    }));
    await assert.rejects(api.pollJob(), /invalid job/i);
  });
}

for (const [name, timeoutValue, extra = {}] of [
  ["missing", undefined],
  ["zero", 0],
  ["negative", -1],
  ["fractional", 1.5],
  ["over cap", 45_001],
  ["legacy monotonic deadline", 1_000, { deadline_at: 123.5 }],
]) {
  test(`poll rejects ${name} timeout_ms`, async () => {
    stored.token = "secret-token";
    const job = {
      id: "job-1", target_kind: "source", handle: "openai", profile_url: "https://x.com/openai",
      maximum: 30, ...extra,
    };
    if (timeoutValue !== undefined) job.timeout_ms = timeoutValue;
    globalThis.fetch = async () => response(JSON.stringify({
      protocol_version: 4, cancelled: false, job,
    }));
    await assert.rejects(api.pollJob(), /invalid job/i);
  });
}

test("progress and completion encode job identifiers and exact nested photo bodies", async () => {
  stored.token = "secret-token";
  const observation = {
    url: "https://x.com/openai/status/1", post_id: "1", author_handle: "openai",
    is_pinned: false, is_reply: false, is_repost: false, is_quote: false, is_promoted: false,
    parent_url: null, discovery_order: 0,
    photos: [{
      position: 0, url: "https://pbs.twimg.com/media/abc?format=jpg&name=large",
      alt_text: "A test photo",
    }],
  };
  await api.reportProgress("job /1", [observation]);
  await api.completeJob("job /1", "exhausted", null);
  assert.match(calls[0].url, /jobs\/job%20%2F1\/progress$/);
  assert.deepEqual(JSON.parse(calls[0].options.body), { protocol_version: 4, observations: [observation] });
  assert.match(calls[1].url, /jobs\/job%20%2F1\/complete$/);
  assert.deepEqual(JSON.parse(calls[1].options.body), { protocol_version: 4, reason: "exhausted", diagnostic: null });
});

for (const [name, photos] of [
  ["missing", undefined],
  ["five items", Array.from({ length: 5 }, (_, position) => ({ position, url: `https://pbs.twimg.com/media/${position}`, alt_text: null }))],
  ["non-contiguous positions", [{ position: 1, url: "https://pbs.twimg.com/media/abc", alt_text: null }]],
  ["duplicate URLs", [{ position: 0, url: "https://pbs.twimg.com/media/abc", alt_text: null }, { position: 1, url: "https://pbs.twimg.com/media/abc", alt_text: null }]],
  ["non-HTTPS URL", [{ position: 0, url: "http://pbs.twimg.com/media/abc", alt_text: null }]],
  ["wrong host", [{ position: 0, url: "https://evil.example/media/abc", alt_text: null }]],
  ["long alt text", [{ position: 0, url: "https://pbs.twimg.com/media/abc", alt_text: "x".repeat(1001) }]],
  ["unknown photo field", [{ position: 0, url: "https://pbs.twimg.com/media/abc", alt_text: null, extra: true }]],
]) {
  test(`progress rejects ${name} photo arrays before calling fetch`, async () => {
    stored.token = "secret-token";
    const observation = {
      url: "https://x.com/openai/status/1", post_id: "1", author_handle: "openai",
      is_pinned: false, is_reply: false, is_repost: false, is_quote: false, is_promoted: false,
      parent_url: null, discovery_order: 0, photos,
    };
    await assert.rejects(api.reportProgress("job-1", [observation]), /invalid observations/i);
    assert.equal(calls.length, 0);
  });
}

function progressObservation(photos) {
  return {
    url: "https://x.com/openai/status/1", post_id: "1", author_handle: "openai",
    is_pinned: false, is_reply: false, is_repost: false, is_quote: false, is_promoted: false,
    parent_url: null, discovery_order: 0, photos,
  };
}

for (const [name, photos] of [
  ["empty photo URL", [{ position: 0, url: "", alt_text: null }]],
  ["whitespace photo URL", [{ position: 0, url: "  ", alt_text: null }]],
  ["photo URL over 2048 characters", [{
    position: 0, url: `https://pbs.twimg.com/media/${"a".repeat(2_100)}`, alt_text: null,
  }]],
  ["photo path without an identifier", [{
    position: 0, url: "https://pbs.twimg.com/media/", alt_text: null,
  }]],
  ["empty alt text", [{
    position: 0, url: "https://pbs.twimg.com/media/abc", alt_text: "",
  }]],
  ["whitespace alt text", [{
    position: 0, url: "https://pbs.twimg.com/media/abc", alt_text: "  ",
  }]],
  ["duplicate photo query keys", [{
    position: 0, url: "https://pbs.twimg.com/media/abc?format=jpg&format=png", alt_text: null,
  }]],
  ["photo URLs that normalize to the same value", [
    { position: 0, url: "https://pbs.twimg.com/media/abc?format=jpg&name=small", alt_text: null },
    { position: 1, url: "https://pbs.twimg.com/media/abc?name=large&format=jpg", alt_text: null },
  ]],
]) {
  test(`progress rejects ${name} before calling fetch`, async () => {
    stored.token = "secret-token";

    await assert.rejects(api.reportProgress("job-1", [progressObservation(photos)]), /invalid observations/i);

    assert.equal(calls.length, 0);
  });
}

for (const [name, url] of [
  ["a literal dot segment before media", "https://pbs.twimg.com/foo/../media/abc?format=jpg"],
  ["a literal dot segment within media", "https://pbs.twimg.com/media/./abc?format=jpg"],
  ["an encoded dot-dot segment before media", "https://pbs.twimg.com/foo/%2e%2e/media/abc?format=jpg"],
  ["an encoded dot segment within media", "https://pbs.twimg.com/media/%2E/abc?format=jpg"],
  ["an extra raw path segment", "https://pbs.twimg.com/media/abc/extra?format=jpg"],
  ["an encoded slash traversal", "https://pbs.twimg.com/media/abc%2f..%2fsecret?format=jpg"],
  ["an encoded backslash traversal", "https://pbs.twimg.com/media/abc%5c..%5csecret?format=jpg"],
  ["a double-encoded traversal", "https://pbs.twimg.com/media/%252e%252e%252fsecret?format=jpg"],
  ["a malformed percent escape", "https://pbs.twimg.com/media/abc%?format=jpg"],
  ["leading whitespace", " https://pbs.twimg.com/media/abc?format=jpg"],
  ["trailing whitespace", "https://pbs.twimg.com/media/abc?format=jpg "],
  ["an embedded tab", "https://pbs.twimg.com/\tmedia/abc?format=jpg"],
  ["an embedded newline", "https://pbs.twimg.com/media/abc\n?format=jpg"],
  ["an embedded carriage return", "https://pbs.twimg.com/media/abc?\rformat=jpg"],
  ["an embedded NUL", "https://pbs.twimg.com/media/abc\0?format=jpg"],
  ["an embedded C0 control", "https://pbs.twimg.com/media/abc\x1f?format=jpg"],
  ["an embedded DEL", "https://pbs.twimg.com/media/abc\x7f?format=jpg"],
  ["leading byte-order mark", "\ufeffhttps://pbs.twimg.com/media/abc?format=jpg"],
  ["trailing byte-order mark", "https://pbs.twimg.com/media/abc?format=jpg\ufeff"],
  ["leading next-line control", "\u0085https://pbs.twimg.com/media/abc?format=jpg"],
  ["trailing next-line control", "https://pbs.twimg.com/media/abc?format=jpg\u0085"],
]) {
  test(`progress rejects ${name} before calling fetch`, async () => {
    stored.token = "secret-token";

    await assert.rejects(api.reportProgress("job-1", [progressObservation([
      { position: 0, url, alt_text: null },
    ])]), /invalid observations/i);

    assert.equal(calls.length, 0);
  });
}

test("rejects legacy version 1 responses", async () => {
  stored.token = "secret-token";
  globalThis.fetch = async () => response('{"protocol_version":1}');
  await assert.rejects(api.pollJob(), /protocol version/i);
});

test("bounds error bodies and removes a rejected bearer", async () => {
  stored.token = "secret-token";
  globalThis.fetch = async () => response("x".repeat(10_000), { status: 401 });
  await assert.rejects(api.heartbeat("signed_in"), (error) => error.message.length < 700);
  assert.equal(stored.token, undefined);
});

test("rejects an oversized streamed response without calling response.text", async () => {
  stored.token = "secret-token";
  const oversized = response("", {
    headers: { "Content-Length": "invalid" },
    chunks: [new Uint8Array(65_537)],
  });
  globalThis.fetch = async () => oversized;
  await assert.rejects(api.heartbeat("signed_in"), /too large/i);
  assert.equal(oversized.textCalled, false);
});

test("removes a rejected bearer before a failing 401 body read", async () => {
  stored.token = "secret-token";
  const unauthorized = response("", { status: 401, readError: new Error("stream failed") });
  globalThis.fetch = async () => unauthorized;
  await assert.rejects(api.heartbeat("signed_in"), /stream failed/);
  assert.equal(stored.token, undefined);
  assert.equal(unauthorized.textCalled, false);
});

test("poll accepts the exact resolve-post-photos job schema", async () => {
  stored.token = "secret-token";
  const mediaJob = {
    id: "job-media-1",
    kind: "resolve_post_photos",
    post_id: "123",
    post_url: "https://x.com/openai/status/123",
    timeout_ms: 45_000,
  };
  globalThis.fetch = async () => response(JSON.stringify({
    protocol_version: 4, cancelled: false, job: mediaJob,
  }));

  assert.deepEqual((await api.pollJob()).job, mediaJob);
});

for (const [name, mediaJob] of [
  ["unknown kind", {
    id: "job-media-1", kind: "resolve_video", post_id: "123",
    post_url: "https://x.com/openai/status/123", timeout_ms: 45_000,
  }],
  ["extra field", {
    id: "job-media-1", kind: "resolve_post_photos", post_id: "123",
    post_url: "https://x.com/openai/status/123", timeout_ms: 45_000, extra: true,
  }],
  ["post ID mismatch", {
    id: "job-media-1", kind: "resolve_post_photos", post_id: "456",
    post_url: "https://x.com/openai/status/123", timeout_ms: 45_000,
  }],
  ["noncanonical handle", {
    id: "job-media-1", kind: "resolve_post_photos", post_id: "123",
    post_url: "https://x.com/OpenAI/status/123", timeout_ms: 45_000,
  }],
  ["wrong host", {
    id: "job-media-1", kind: "resolve_post_photos", post_id: "123",
    post_url: "https://evil.example/openai/status/123", timeout_ms: 45_000,
  }],
  ["www host", {
    id: "job-media-1", kind: "resolve_post_photos", post_id: "123",
    post_url: "https://www.x.com/openai/status/123", timeout_ms: 45_000,
  }],
  ["query string", {
    id: "job-media-1", kind: "resolve_post_photos", post_id: "123",
    post_url: "https://x.com/openai/status/123?ref=test", timeout_ms: 45_000,
  }],
  ["missing timeout", {
    id: "job-media-1", kind: "resolve_post_photos", post_id: "123",
    post_url: "https://x.com/openai/status/123",
  }],
  ["timeout over cap", {
    id: "job-media-1", kind: "resolve_post_photos", post_id: "123",
    post_url: "https://x.com/openai/status/123", timeout_ms: 45_001,
  }],
  ["long job ID", {
    id: "x".repeat(129), kind: "resolve_post_photos", post_id: "123",
    post_url: "https://x.com/openai/status/123", timeout_ms: 45_000,
  }],
]) {
  test(`poll rejects media job with ${name}`, async () => {
    stored.token = "secret-token";
    globalThis.fetch = async () => response(JSON.stringify({
      protocol_version: 4, cancelled: false, job: mediaJob,
    }));

    await assert.rejects(api.pollJob(), /invalid job/i);
  });
}

test("media completion emits photos while collection completion keeps its exact body", async () => {
  stored.token = "secret-token";
  const collectionJob = {
    id: "job-1", target_kind: "source", handle: "openai",
    profile_url: "https://x.com/openai", maximum: 30, timeout_ms: 45_000,
  };
  const mediaJob = {
    id: "job-media-1", kind: "resolve_post_photos", post_id: "123",
    post_url: "https://x.com/openai/status/123", timeout_ms: 45_000,
  };
  const photo = {
    position: 0,
    url: "https://pbs.twimg.com/media/abc?format=jpg&name=large",
    alt_text: null,
  };

  await api.completeJob(collectionJob, {
    reason: "exhausted", diagnostic: null,
  });
  await api.completeJob(mediaJob, {
    reason: "exhausted", diagnostic: null, photos: [photo],
  });

  assert.deepEqual(JSON.parse(calls[0].options.body), {
    protocol_version: 4, reason: "exhausted", diagnostic: null,
  });
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    protocol_version: 4, reason: "exhausted", diagnostic: null, photos: [photo],
  });
});

test("completion rejects cross-kind and unbounded terminal fields before fetch", async () => {
  stored.token = "secret-token";
  const collectionJob = {
    id: "job-1", target_kind: "source", handle: "openai",
    profile_url: "https://x.com/openai", maximum: 30, timeout_ms: 45_000,
  };
  const mediaJob = {
    id: "job-media-1", kind: "resolve_post_photos", post_id: "123",
    post_url: "https://x.com/openai/status/123", timeout_ms: 45_000,
  };

  await assert.rejects(api.completeJob(collectionJob, {
    reason: "exhausted", diagnostic: null, photos: [],
  }), /invalid completion/i);
  await assert.rejects(api.completeJob(mediaJob, {
    reason: "exhausted", diagnostic: null,
  }), /invalid completion/i);
  await assert.rejects(api.completeJob(mediaJob, {
    reason: "exhausted", diagnostic: "x".repeat(501), photos: [],
  }), /invalid completion/i);
  assert.equal(calls.length, 0);
});
