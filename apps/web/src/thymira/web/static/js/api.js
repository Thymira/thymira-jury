// HTTP client for the Thymira API, reached through this console's same-origin /api pass-through.
// The operator's bearer token lives in sessionStorage for this tab only and is sent as the
// Authorization header; the console server forwards it unchanged and never stores one.

const TOKEN_KEY = "thymira.console.token";
const EVENT_PAGE_LIMIT = 1000;
const RUN_PAGE_LIMIT = 100;
const MAX_RUN_PAGES = 20;
const RECONNECT_MS = 3000;

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export function getToken() {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token) {
  try {
    sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Storage is unavailable; the token lasts only as long as this call.
  }
}

export function clearToken() {
  try {
    sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // Nothing was stored.
  }
}

function endpoint(path, query) {
  const url = new URL(`/api${path}`, window.location.origin);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
  }
  return url;
}

function headers(extra = {}) {
  const result = { Accept: "application/json", ...extra };
  const token = getToken();
  if (token) result.Authorization = `Bearer ${token}`;
  return result;
}

function announceUnauthorized() {
  window.dispatchEvent(new CustomEvent("thymira:unauthorized"));
}

function problemFromBody(status, body) {
  const code = typeof body?.code === "string" ? body.code : `http_${status}`;
  const message = typeof body?.message === "string" ? body.message : `The API answered HTTP ${status}.`;
  return new ApiError(status, code, message);
}

async function problemFrom(response) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    // Not a problem document.
  }
  return problemFromBody(response.status, body);
}

export async function request(method, path, { query, body, signal } = {}) {
  const init = {
    method,
    headers: headers(body === undefined ? {} : { "Content-Type": "application/json" }),
    cache: "no-store",
    credentials: "omit",
    signal,
  };
  if (body !== undefined) init.body = JSON.stringify(body);
  let response;
  try {
    response = await fetch(endpoint(path, query), init);
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError(0, "network_error", "The console server could not be reached.");
  }
  if (response.status === 401) announceUnauthorized();
  if (!response.ok) throw await problemFrom(response);
  return response.status === 204 ? null : response.json();
}

const segment = encodeURIComponent;

export const api = {
  health: () =>
    fetch("/api/healthz", { cache: "no-store", credentials: "omit" })
      .then((response) => response.ok)
      .catch(() => false),
  listRuns: ({ status, cursor, limit = 50 } = {}) =>
    request("GET", "/runs", { query: { status, cursor, limit } }),
  getRun: (runId) => request("GET", `/runs/${segment(runId)}`),
  createRun: (prompt) => request("POST", "/runs", { body: { prompt, client: "web" } }),
  riskInterview: (runId) => request("GET", `/runs/${segment(runId)}/risk-interview`),
  answerRiskInterview: (runId, answer) =>
    request("POST", `/runs/${segment(runId)}/risk-interview`, { body: { answer } }),
  resume: (runId) => request("POST", `/runs/${segment(runId)}/resume`, { body: {} }),
  cancel: (runId, reason) =>
    request("POST", `/runs/${segment(runId)}/cancel`, { body: reason ? { reason } : {} }),
  approve: (runId, note) =>
    request("POST", `/runs/${segment(runId)}/approve`, { body: note ? { note } : {} }),
  reject: (runId, note) =>
    request("POST", `/runs/${segment(runId)}/reject`, { body: note ? { note } : {} }),
  plan: (runId) => request("GET", `/runs/${segment(runId)}/plan`),
  audit: (runId) => request("GET", `/runs/${segment(runId)}/audit`),
  assurance: (runId) => request("GET", `/runs/${segment(runId)}/assurance`),
  experiments: (runId) => request("GET", `/runs/${segment(runId)}/experiments`),
  mlflow: (runId) => request("GET", `/runs/${segment(runId)}/mlflow`),
  artifacts: (runId) => request("GET", `/runs/${segment(runId)}/artifacts`),
  artifact: (runId, artifactId) =>
    request("GET", `/runs/${segment(runId)}/artifacts/${segment(artifactId)}`),
  projectContext: () => request("GET", "/project/context"),
  saveProjectContext: (text, expectedSha256) =>
    request("PUT", "/project/context", { body: { text, expected_sha256: expectedSha256 } }),
  getSettings: (namespace) => request("GET", `/settings/${segment(namespace)}`),
  putSettings: (namespace, values, expectedRevision = 0) =>
    request("PUT", `/settings/${segment(namespace)}`, {
      body: { values, expected_revision: expectedRevision },
    }),
  projectDatasets: () => request("GET", "/project/datasets"),
  setDatasetTarget: (name, target) =>
    request("PATCH", `/project/datasets/${segment(name)}`, { body: { target: target || null } }),
  removeDataset: (name) => request("DELETE", `/project/datasets/${segment(name)}`),
};

// Upload one dataset file as the raw request body. XMLHttpRequest rather than fetch, because only
// it reports upload progress; the body is the File itself, so nothing is read into memory here.
export function uploadDataset(name, file, { target, onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", endpoint(`/project/datasets/${segment(name)}`, { filename: file.name, target }));
    for (const [key, value] of Object.entries(headers({ "Content-Type": "application/octet-stream" }))) {
      xhr.setRequestHeader(key, value);
    }
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress?.(event.loaded / event.total);
    });
    xhr.addEventListener("load", () => {
      let body = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // Not JSON.
      }
      if (xhr.status === 401) announceUnauthorized();
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(problemFromBody(xhr.status, body));
    });
    xhr.addEventListener("error", () => reject(new ApiError(0, "network_error", "The console server could not be reached.")));
    xhr.send(file);
  });
}

export async function listAllRuns({ status } = {}) {
  const runs = [];
  let cursor;
  for (let page = 0; page < MAX_RUN_PAGES; page += 1) {
    const body = await request("GET", "/runs", { query: { status, cursor, limit: RUN_PAGE_LIMIT } });
    runs.push(...body.items);
    if (!body.next_cursor) break;
    cursor = body.next_cursor;
  }
  return runs;
}

export async function readAllEvents(runId, { afterSeq = -1, signal } = {}) {
  const events = [];
  let cursor = afterSeq;
  for (;;) {
    const page = await request("GET", `/runs/${segment(runId)}/events`, {
      query: { after_seq: cursor, limit: EVENT_PAGE_LIMIT },
      signal,
    });
    events.push(...page.items);
    if (!page.has_more || page.next_after_seq === null) return events;
    cursor = page.next_after_seq;
  }
}

function parseFrame(frame) {
  const data = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  if (!data) return null;
  try {
    return JSON.parse(data);
  } catch {
    return null;
  }
}

function pause(milliseconds, signal) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

// Follow the API's Server-Sent Event stream with fetch, because EventSource cannot send an
// Authorization header. Events arrive in sequence order; the caller repairs any gap from pages.
export async function followEvents(runId, { since, onEvent, signal, keepGoing = () => true }) {
  let cursor = since;
  while (!signal.aborted && keepGoing()) {
    try {
      const response = await fetch(endpoint(`/runs/${segment(runId)}/events`, { follow: "true", since: cursor }), {
        headers: headers({ Accept: "text/event-stream" }),
        cache: "no-store",
        credentials: "omit",
        signal,
      });
      if (response.status === 401) {
        announceUnauthorized();
        return;
      }
      if (response.ok && response.body) {
        const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += value.replaceAll("\r\n", "\n");
          let boundary = buffer.indexOf("\n\n");
          while (boundary >= 0) {
            const event = parseFrame(buffer.slice(0, boundary));
            buffer = buffer.slice(boundary + 2);
            if (event && event.seq > cursor) {
              cursor = event.seq;
              onEvent(event);
            }
            boundary = buffer.indexOf("\n\n");
          }
        }
      }
    } catch {
      if (signal.aborted) return;
    }
    await pause(RECONNECT_MS, signal);
  }
}
