const BASE_URL = "http://127.0.0.1:47831/v1";
const PROTOCOL_VERSION = 4;
const TOKEN_KEY = "token";
const ERROR_LIMIT = 512;
const RESPONSE_LIMIT = 65_536;

async function token() {
  const value = await chrome.storage.local.get(TOKEN_KEY);
  return typeof value[TOKEN_KEY] === "string" && value[TOKEN_KEY] ? value[TOKEN_KEY] : null;
}

async function readBoundedResponse(response) {
  const declared = response.headers?.get("Content-Length");
  if (declared !== null && declared !== undefined && /^[0-9]+$/.test(declared)) {
    const length = Number(declared);
    if (!Number.isSafeInteger(length) || length > RESPONSE_LIMIT) {
      throw new Error("Bridge response is too large");
    }
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Bridge response body is unavailable");
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!(value instanceof Uint8Array)) throw new Error("Bridge response stream is invalid");
      if (total + value.byteLength > RESPONSE_LIMIT) {
        try { await reader.cancel(); } catch {}
        throw new Error("Bridge response is too large");
      }
      chunks.push(value);
      total += value.byteLength;
    }
  } finally {
    try { reader.releaseLock?.(); } catch {}
  }
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(joined);
}

async function request(path, body, authenticated = true) {
  const headers = { "Content-Type": "application/json" };
  if (authenticated) {
    const bearer = await token();
    if (!bearer) throw new Error("Extension is not paired");
    headers.Authorization = `Bearer ${bearer}`;
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  let response;
  let raw;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (response.status === 401) await chrome.storage.local.remove(TOKEN_KEY);
    raw = await readBoundedResponse(response);
  } finally {
    clearTimeout(timeout);
  }
  if (!response.ok) {
    let detail = raw.slice(0, ERROR_LIMIT);
    try { detail = JSON.parse(raw).error || detail; } catch {}
    throw new Error(`Bridge request failed (${response.status}): ${String(detail).slice(0, ERROR_LIMIT)}`);
  }
  let payload;
  try { payload = JSON.parse(raw); } catch { throw new Error("Bridge returned invalid JSON"); }
  if (!payload || payload.protocol_version !== PROTOCOL_VERSION) {
    throw new Error("Bridge protocol version mismatch");
  }
  return payload;
}

export async function pair(code) {
  if (typeof code !== "string" || !code.trim()) throw new Error("Pairing code is required");
  const payload = await request("/pair", { code: code.trim() }, false);
  if (typeof payload.token !== "string" || !payload.token) throw new Error("Bridge returned no token");
  await chrome.storage.local.set({ [TOKEN_KEY]: payload.token });
  return payload;
}

export const heartbeat = (xState) => request("/heartbeat", {
  protocol_version: PROTOCOL_VERSION,
  x_state: xState,
});

export async function pollJob() {
  const payload = await request("/jobs/poll", {});
  if (payload.job === null) return payload;
  const job = payload.job;
  const keys = job && typeof job === "object" && !Array.isArray(job)
    ? Object.keys(job).sort() : [];
  const idOk = typeof job?.id === "string" && Boolean(job.id) && job.id.length <= 128;
  const timeoutOk = Number.isInteger(job?.timeout_ms) &&
    job.timeout_ms >= 1 && job.timeout_ms <= 45_000;
  if (job?.kind === "resolve_post_photos") {
    const expectedMediaKeys = ["id", "kind", "post_id", "post_url", "timeout_ms"];
    let canonicalPostUrl = null;
    if (typeof job.post_url === "string" && typeof job.post_id === "string" &&
        /^[1-9]\d{0,127}$/.test(job.post_id)) {
      try {
        const parsed = new URL(job.post_url);
        const match = /^\/([a-z0-9_]{1,15})\/status\/(\d+)$/.exec(parsed.pathname);
        if (parsed.protocol === "https:" && parsed.hostname === "x.com" &&
            !parsed.username && !parsed.password && !parsed.port &&
            parsed.search === "" && parsed.hash === "" && match &&
            match[2] === job.post_id) {
          canonicalPostUrl = `https://x.com/${match[1]}/status/${match[2]}`;
        }
      } catch {}
    }
    if (!idOk || !timeoutOk ||
        keys.length !== expectedMediaKeys.length ||
        keys.some((key, index) => key !== expectedMediaKeys[index]) ||
        canonicalPostUrl !== job.post_url) {
      throw new Error("Bridge returned an invalid job");
    }
    return payload;
  }
  const expectedKeys = ["handle", "id", "maximum", "profile_url", "target_kind", "timeout_ms"];
  const handleOk = typeof job?.handle === "string" && /^[A-Za-z0-9_]{1,15}$/.test(job.handle);
  const sourceOk = job?.target_kind === "source" && handleOk &&
    job.profile_url === `https://x.com/${job.handle}` && job.maximum <= 30;
  const homeOk = ["for_you", "following"].includes(job?.target_kind) && job.handle === null &&
    job.profile_url === "https://x.com/home" && job.maximum <= 50;
  if (!job || typeof job !== "object" || !idOk ||
      keys.length !== expectedKeys.length || keys.some((key, index) => key !== expectedKeys[index]) ||
      (!sourceOk && !homeOk) || !Number.isInteger(job.maximum) || job.maximum < 1 ||
      !timeoutOk) {
    throw new Error("Bridge returned an invalid job");
  }
  return payload;
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

function normalizePhotoUrl(value) {
  if (typeof value !== "string" || value.length > 2048 ||
      !/^[\x21-\x7E]+$/.test(value)) return null;
  try {
    const url = new URL(value);
    const rawPath = value.match(/^[A-Za-z][A-Za-z0-9+.-]*:\/\/[^/?#]*([^?#]*)/)?.[1];
    const normalizedPath = normalizeMediaPath(rawPath);
    if (url.protocol !== "https:" || url.hostname !== "pbs.twimg.com" ||
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
        if (name !== "name") query.push([name, queryValue]);
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

function validPhoto(photo, index) {
  if (!photo || typeof photo !== "object" || Array.isArray(photo)) return false;
  if (Object.keys(photo).sort().join(",") !== "alt_text,position,url") return false;
  if (photo.position !== index) return false;
  if (photo.alt_text !== null &&
      (typeof photo.alt_text !== "string" || !photo.alt_text.trim() || photo.alt_text.length > 1000)) return false;
  return normalizePhotoUrl(photo.url) !== null;
}

function validObservation(observation) {
  if (!observation || typeof observation !== "object" || Array.isArray(observation)) return false;
  const keys = Object.keys(observation).sort().join(",");
  if (keys !== "author_handle,discovery_order,is_pinned,is_promoted,is_quote,is_reply,is_repost,parent_url,photos,post_id,url") return false;
  if (typeof observation.url !== "string" || !observation.url.trim() || observation.url.length > 2048 ||
      typeof observation.post_id !== "string" || !observation.post_id.trim() || observation.post_id.length > 128 ||
      typeof observation.author_handle !== "string" || !observation.author_handle.trim() || observation.author_handle.length > 64 ||
      typeof observation.is_pinned !== "boolean" || typeof observation.is_reply !== "boolean" ||
      typeof observation.is_repost !== "boolean" || typeof observation.is_quote !== "boolean" ||
      typeof observation.is_promoted !== "boolean" ||
      (observation.parent_url !== null && typeof observation.parent_url !== "string") ||
      !Number.isInteger(observation.discovery_order) || observation.discovery_order < 0 ||
      !Array.isArray(observation.photos) || observation.photos.length > 4 ||
      !observation.photos.every(validPhoto)) return false;
  const urls = observation.photos.map((photo) => normalizePhotoUrl(photo.url));
  return new Set(urls).size === urls.length;
}

export function reportProgress(jobId, observations) {
  if (!Array.isArray(observations) || !observations.every(validObservation)) {
    return Promise.reject(new Error("Invalid observations"));
  }
  return request(
    `/jobs/${encodeURIComponent(jobId)}/progress`,
    { protocol_version: PROTOCOL_VERSION, observations },
  );
}

const TERMINAL_REASONS = new Set([
  "limit", "exhausted", "no_progress", "login_wall", "timeout", "error", "cancelled",
]);

function validCompletionBase(result) {
  return result && typeof result === "object" && !Array.isArray(result) &&
    TERMINAL_REASONS.has(result.reason) &&
    (result.diagnostic === null ||
      (typeof result.diagnostic === "string" && Boolean(result.diagnostic.trim()) &&
       result.diagnostic.length <= 500));
}

export function completeJob(jobOrId, resultOrReason, legacyDiagnostic) {
  if (typeof jobOrId === "string") {
    const result = { reason: resultOrReason, diagnostic: legacyDiagnostic };
    if (!jobOrId || jobOrId.length > 128 || !validCompletionBase(result)) {
      return Promise.reject(new Error("Invalid completion"));
    }
    return request(
      `/jobs/${encodeURIComponent(jobOrId)}/complete`,
      { protocol_version: PROTOCOL_VERSION, ...result },
    );
  }
  if (!jobOrId || typeof jobOrId !== "object" || Array.isArray(jobOrId) ||
      typeof jobOrId.id !== "string" || !jobOrId.id || jobOrId.id.length > 128 ||
      !validCompletionBase(resultOrReason)) {
    return Promise.reject(new Error("Invalid completion"));
  }
  const media = jobOrId.kind === "resolve_post_photos";
  if (jobOrId.kind !== undefined && !media) {
    return Promise.reject(new Error("Invalid completion"));
  }
  const expectedKeys = media ? "diagnostic,photos,reason" : "diagnostic,reason";
  if (Object.keys(resultOrReason).sort().join(",") !== expectedKeys) {
    return Promise.reject(new Error("Invalid completion"));
  }
  if (media) {
    const photos = resultOrReason.photos;
    if (!Array.isArray(photos) || photos.length > 4 || !photos.every(validPhoto)) {
      return Promise.reject(new Error("Invalid completion"));
    }
    const normalized = photos.map((photo) => normalizePhotoUrl(photo.url));
    if (new Set(normalized).size !== normalized.length) {
      return Promise.reject(new Error("Invalid completion"));
    }
  }
  return request(
    `/jobs/${encodeURIComponent(jobOrId.id)}/complete`,
    { protocol_version: PROTOCOL_VERSION, ...resultOrReason },
  );
}
