# Frontend

The React chat interface for the Personal Knowledge Graph Agent, per
`docs/UIUX_Wireframes.docx`. A Vite + React app; see `../CLAUDE.md` and
`../FLOW.md` for how this fits into the rest of the system.

## Setup

```
npm install
```

## Development

Run the backend first (`uv run uvicorn api.main:app`, from the repo root,
port 8080), then:

```
npm run dev
```

Opens on `http://localhost:5173` by default. `src/api/client.js` talks to
the backend at `http://127.0.0.1:8080/api`.

## Scripts

- `npm run dev` — start the dev server with hot reload
- `npm run build` — production build to `dist/`
- `npm run preview` — preview the production build locally
- `npm run lint` — ESLint (`eslint-plugin-react-hooks` ruleset, per
  `docs/Coding_Conventions.docx`)
- `npm run format` / `npm run format:check` — Prettier
