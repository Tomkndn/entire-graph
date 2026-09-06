# Git-Blast Live — frontend

React Flow dashboard for the blast pipeline (SPEC.md "Frontend Dashboard").

- **App.tsx** — header, status bar, Blast button, result panel
- **GraphCanvas.tsx** — React Flow graph, custom file nodes, glow states
- **useWebSocket.ts** — `/ws` hook, auto-reconnect
- **layout.ts** — Dagre hierarchical auto-layout
- **types.ts** — shared API / event types

Node glow states: gray (unchanged), amber pulse (modified), blue pulse
(querying / running), green (passed), red (failed).

## Build

```bash
npm install
npm run build      # tsc --noEmit && vite build  ->  ../static/
```

FastAPI serves `../static/` at `/` when the build is present.

## Dev

```bash
npm run dev        # proxies /api and /ws to http://localhost:8000
```
