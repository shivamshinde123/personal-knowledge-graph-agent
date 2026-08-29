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

/** Current LLM provider configuration. Per section 3.6. */
export function getSettings() {
  return request("/settings");
}

/**
 * Update the LLM provider configuration (partial update). Per section 3.7.
 * @param {{provider_mode?: string, local_model?: string, cloud_model?: string}} payload
 */
export function putSettings(payload) {
  return request("/settings", {
    method: "PUT",
    body: JSON.stringify(payload),
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
