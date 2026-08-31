import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The backend (api/main.py, run via `uv run python -m api.main`) tries
// settings.env.fastapi_port first, then a documented fallback list, and
// writes whichever one it actually bound to here — so the frontend can
// find it without either side hardcoding a port the other might not be
// using. Falls back to 8080 (the documented default) if the backend
// hasn't started yet, or was started the old way
// (`uv run uvicorn api.main:app --port ...`), which writes nothing. See
// api/main.py::_select_port(), DECISIONS.md.
const BACKEND_PORT_FILE = fileURLToPath(
  new URL("../data/backend_port.txt", import.meta.url),
);

function readBackendPort() {
  try {
    return readFileSync(BACKEND_PORT_FILE, "utf-8").trim();
  } catch {
    return "8080";
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // strictPort defaults to false, so Vite still falls back to the next
    // free port on its own if 5173 is taken — this just documents the
    // intended default rather than leaving it implicit.
    port: 5173,
  },
  define: {
    "import.meta.env.VITE_API_BASE_URL": JSON.stringify(
      `http://127.0.0.1:${readBackendPort()}/api`,
    ),
  },
});
