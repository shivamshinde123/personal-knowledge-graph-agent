/**
 * Centralized API client: every network call the frontend makes lives here.
 *
 * Per docs/Coding_Conventions.docx section 3: "API calls are centralized in
 * frontend/src/api/client.js; components never call fetch directly." Base
 * URL and every request/response shape match docs/API_Specification.docx.
 */

// 127.0.0.1, not "localhost": verified directly that "localhost" can
// resolve to ::1 first on a machine where something else (e.g. a WSL/Docker
// port-forward) is already listening on ::1:8080 — silently talking to the
// wrong service instead of this API. 127.0.0.1 is unambiguous. See
// DECISIONS.md.
const API_BASE_URL = "http://127.0.0.1:8080/api";

class ApiError extends Error {
  constructor(status, body) {
    super(
      body?.detail || body?.error || `Request failed with status ${status}`,
    );
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, body);
  }
  return body;
}

/**
 * Submit a question to the agent. Per API_Specification.docx section 3.1.
 * @param {string} question
 * @param {string | null} sessionId - omit/null to start a new session
 */
export function postQuery(question, sessionId) {
  return request("/query", {
    method: "POST",
    body: JSON.stringify({ question, session_id: sessionId ?? undefined }),
  });
}

/** Backing-service health. Per API_Specification.docx section 3.2. */
export function getHealth() {
  return request("/health");
}

/** Most recent daily batch run, per source. Per section 3.3. */
export function getSourcesStatus() {
  return request("/sources/status");
}

/**
 * Each source's live connection status — cached server-side; a fresh
 * check only runs if the cache is stale. Extension beyond
 * API_Specification.docx section 3.3 — see DECISIONS.md.
 */
export function getSourceConnections() {
  return request("/sources/connections");
}

/** Force a fresh connection check for every source, bypassing the cache. */
export function verifySourceConnections() {
  return request("/sources/connections/verify", { method: "POST" });
}

/** Current LLM provider configuration. Per section 3.6. */
export function getSettings() {
  return request("/settings");
}

/**
 * Update the LLM provider configuration (partial update). Extends
 * API_Specification.docx section 3.7 with cloud_embedding_model — see
 * DECISIONS.md.
 * @param {{provider_mode?: string, local_generation_model?: string, cloud_generation_model?: string, cloud_embedding_model?: string}} payload
 */
export function putSettings(payload) {
  return request("/settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

/**
 * Current source-scope configuration (local watch folders, Notion page
 * scope, GitHub repo scope, Gmail/GitHub date ranges). Extension beyond
 * API_Specification.docx — see DECISIONS.md.
 */
export function getSourceConfig() {
  return request("/settings/sources");
}

/**
 * Update the source-scope configuration (partial update). The four
 * date-range fields are plain "YYYY-MM-DD" strings (an empty string
 * clears the field) — matching an <input type="date">'s value directly.
 * @param {{local_files_watch_dirs?: string[], notion_page_ids?: string[], github_repos?: string[], gmail_date_range_start?: string, gmail_date_range_end?: string, github_date_range_start?: string, github_date_range_end?: string}} payload
 */
export function putSourceConfig(payload) {
  return request("/settings/sources", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

/**
 * Open a native folder-picker dialog on the machine running the backend
 * (meaningful only because this is a local, single-user system — a
 * browser can't reveal a picked folder's real filesystem path on its
 * own). Blocks until the dialog closes; `path` is null if cancelled.
 * Extension beyond API_Specification.docx — see DECISIONS.md.
 */
export function postBrowseFolder() {
  return request("/settings/browse-folder", { method: "POST" });
}

/**
 * The whole relationship graph (every item node, every confirmed edge).
 * Extension beyond API_Specification.docx — see DECISIONS.md.
 */
export function getGraph() {
  return request("/graph");
}

/**
 * Wipe SQLite, Chroma, and Neo4j back to empty. Destructive and
 * irreversible — callers must confirm with the user before calling this.
 * Extension beyond API_Specification.docx — see DECISIONS.md.
 */
export function postAdminReset() {
  return request("/admin/reset", {
    method: "POST",
    body: JSON.stringify({ confirm: true }),
  });
}

/**
 * Manually start a daily-batch ingestion run right now, outside the
 * schedule. Per API_Specification.docx section 3.8 — returns immediately
 * (202 Accepted); the run itself happens in the background, checked
 * afterward via getSourcesStatus().
 */
export function postIngestTrigger() {
  return request("/ingest/trigger", { method: "POST" });
}

/**
 * Ask a running ingestion to stop at its next check point. Acknowledges the
 * request only — items already processed are kept, not rolled back — the
 * actual outcome (status: "cancelled") is observed afterward via
 * getSourcesStatus(). Extension beyond API_Specification.docx — see
 * DECISIONS.md.
 * @param {string} runId - ingestion_runs.id, from getSourcesStatus()'s
 *   last_run.run_id (not postIngestTrigger()'s display-label run_id).
 */
export function postIngestCancel(runId) {
  return request("/ingest/cancel", {
    method: "POST",
    body: JSON.stringify({ run_id: runId }),
  });
}

/** List past conversation sessions, most recently active first. Per section 3.4. */
export function getSessions() {
  return request("/sessions");
}

/**
 * Full message history for one session, oldest first. Per section 3.5.
 * @param {string} sessionId
 */
export function getSessionHistory(sessionId) {
  return request(`/sessions/${encodeURIComponent(sessionId)}`);
}

export { ApiError };
