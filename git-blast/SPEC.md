# Git-Blast Live — Technical Specification (v1)

## What Git-Blast Does

Git-Blast is a test impact analysis tool built for AI coding agents. When an agent edits a file, Git-Blast figures out which tests are affected by that edit and runs only those tests. It returns a compact JSON result instead of dumping hundreds of lines of pytest output into the agent's context window.

One line: **detect what changed, trace what depends on it, test only that, report in JSON.**

## The Problem It Solves

When AI agents (Claude Code, Cursor, Codex) run `pytest` after editing code, the full test suite output floods the LLM context window. This causes:

- Token waste (thousands of lines of irrelevant test output)
- Hallucination cascades (agent tries to "fix" unrelated test failures)
- Slow feedback loops (full suite can take minutes)

Git-Blast eliminates this by running only the tests that could possibly be affected by the edit.

## How It Works — Data Flow

```
1. Agent edits a file (e.g. src/requests/auth.py)
                    |
2. git diff --name-only → ["src/requests/auth.py"]
                    |
3. entire graph snapshot → NDJSON of all files + IMPORTS relations
                    |
4. parser.py traverses graph: "which files import auth.py?"
   → import surface: [auth.py, sessions.py, models.py, adapters.py, ...]
                    |
5. db.py queries module_test_map: "which tests cover these source files?"
   → [tests/test_requests.py, tests/test_adapters.py]
                    |
6. runner.py executes: pytest test_requests.py test_adapters.py
                    |
7. Returns lean JSON:
   {"import_surface": [...], "target_tests_executed": [...],
    "execution_time_seconds": 39.3, "status": "PASSED"}
```

## External Tools Used

### Entire CLI (v0.10.3)

Git observability platform. We use it only as a host for the graph plugin. Install: `brew tap entireio/tap && brew install --cask entire`.

**We do NOT use:** checkpoints, sessions, shadow branches, hooks, or any session-tracking features. Only the graph plugin.

### entire-graph plugin (v0.4.0)

Tree-sitter-based AST analysis plugin for Entire CLI. Parses 36 languages locally, no API calls.

Install: `entire plugin install graph`

**Command we use: `entire graph snapshot`**

Outputs NDJSON (one JSON object per line) with three record types:

```
# Line 1: header with metadata
{"schema_version":"1.1", "provider":"entire-graph", ...}

# File records
{"record_type":"file", "id":"repo:file:src/auth.py", "path":"src/auth.py", "language":"Python", ...}

# Symbol records (functions, classes, sections)
{"record_type":"symbol", "id":"repo:Python:src/auth.py:function:verify", "kind":"function", "name":"verify", "file_path":"src/auth.py", "start_line":10, ...}

# Relation records (imports, calls, defines)
{"record_type":"relation", "from_id":"repo:file:tests/test_auth.py", "to_id":"repo:file:src/auth.py", "type":"IMPORTS"}
```

We extract `file` records (to build node list) and `relation` records with `type: "IMPORTS"` (to build dependency edges). Everything else is ignored.

**No other entire-graph commands are used.** `search`, `def`, `neighbors`, `impact`, `diff` exist but Git-Blast doesn't call them.

## Project Structure

```
git-blast/
├── src/
│   ├── parser.py       # Calls entire graph snapshot, parses NDJSON, builds import surface
│   ├── matcher.py      # Fallback: convention-based src→test matching (test_X.py)
│   ├── db.py           # SQLite + Databricks DB layer, auto-seeder
│   ├── runner.py       # Runs pytest on target files, returns JSON
│   ├── mcp_server.py   # MCP tool for agent integration
│   ├── server.py       # FastAPI: REST + WebSocket + static files
│   └── cli.py          # CLI entry point
├── frontend/
│   ├── src/
│   │   ├── App.tsx          # Main app with header, status, blast button
│   │   ├── GraphCanvas.tsx  # React Flow graph with glow effects
│   │   ├── useWebSocket.ts  # WebSocket hook for real-time events
│   │   └── layout.ts        # Dagre auto-layout for node positioning
│   ├── index.html
│   ├── package.json         # @xyflow/react, @dagrejs/dagre, react
│   └── vite.config.ts       # Builds to ../static/
├── static/              # Built frontend (gitignored)
├── schema.sql           # SQLite DDL
├── pyproject.toml       # Python deps: fastapi, uvicorn, mcp, pytest
└── SPEC.md              # This file
```

## Module Details

### parser.py

- `parse_entire_graph_snapshot()` — runs `entire graph snapshot`, parses NDJSON, returns `{files: {id: {path, language, imports, imported_by}}, relations: [...]}`
- `get_modified_files()` — runs `git diff --name-only` (staged + unstaged). Falls back to `git ls-files --others` for untracked .py files
- `get_modified_import_surface()` — combines graph + git diff. For each file in the graph, if it imports a modified file, it's added to the surface. Returns flat list of affected file paths
- `get_graph_for_dashboard()` — returns `{nodes: [{id, label, path, language}], edges: [{source, target}]}` for React Flow

### db.py

- `TestMapDB` protocol: `fetch_target_tests(repo_id, import_surface) -> List[str]`
- `SQLiteDB` — local SQLite, tables created from `schema.sql`
- `DatabricksDB` — same interface, queries `dev_catalog.git_blast.module_test_map` via `databricks-sql-connector`
- `get_db()` — returns DatabricksDB if `DATABRICKS_SERVER_HOSTNAME` env var set, else SQLiteDB
- `auto_seed(repo_id, repo_root)` — parses `entire graph snapshot` IMPORTS/TESTS relations + convention matching to populate module_test_map automatically. No manual mapping needed.
- All SQL queries use parameterized placeholders (no f-string interpolation)

### runner.py

- `run_targeted_tests(test_files, import_surface, cwd)` — filters to test files only, finds `.venv/bin/python` if available, runs `pytest -q --tb=short --no-header`, returns JSON result
- `format_lean_output()` — pretty JSON for CLI
- `format_agent_payload()` — single-line string for MCP tool response

### server.py

- `GET /api/graph` — graph nodes + edges from entire graph snapshot, with modified state
- `GET /api/status` — server status, repo info, connection count
- `POST /api/blast` — triggers full pipeline, broadcasts WebSocket events
- `WebSocket /ws` — real-time events: `blast_started`, `surface_detected`, `db_query`, `test_result`
- Serves built React frontend from `static/`
- Configurable via env vars: `GIT_BLAST_REPO_ROOT`, `GIT_BLAST_REPO_ID`

### mcp_server.py

- Exposes `git_blast_test(repo_id)` tool via MCP (MCPServer v2)
- Returns lean single-line payload for agent context

## Database Schema

```sql
-- Source file to test file mappings
CREATE TABLE module_test_map (
    repo_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    imported_symbol TEXT,
    test_file TEXT NOT NULL,
    last_execution_status TEXT,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Checkpoint logs for session recovery
CREATE TABLE checkpoint_logs (
    repo_id TEXT NOT NULL,
    checkpoint_sha TEXT NOT NULL,
    prompt_summary TEXT,
    files_modified TEXT,  -- JSON array in SQLite, ARRAY<STRING> in Databricks
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## CLI Usage

```bash
# Auto-seed test mappings from a target repo's AST graph
python3 -m src.cli seed --auto --repo-root /path/to/repo --repo-id myrepo

# Run targeted tests after editing a file
python3 -m src.cli test --repo-root /path/to/repo --repo-id myrepo

# Start dashboard
GIT_BLAST_REPO_ROOT=/path/to/repo GIT_BLAST_REPO_ID=myrepo \
  python3 -m uvicorn src.server:app --port 8000
```

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `GIT_BLAST_DB` | SQLite database path | `git_blast.db` |
| `GIT_BLAST_REPO_ROOT` | Target repo path (server) | `.` |
| `GIT_BLAST_REPO_ID` | Repo identifier for DB queries (server) | `main-repo` |
| `DATABRICKS_SERVER_HOSTNAME` | Databricks SQL Warehouse host | unset (uses SQLite) |
| `DATABRICKS_HTTP_PATH` | Databricks SQL Warehouse HTTP path | unset |
| `DATABRICKS_TOKEN` | Databricks personal access token | unset |

## Frontend Dashboard

React Flow graph visualization with real-time state updates via WebSocket.

**Node glow states:**
- Gray: default/unchanged
- Amber pulse: file modified (detected by git diff)
- Blue pulse: DB query in progress
- Green solid: test passed
- Red solid: test failed

**Layout:** Dagre hierarchical auto-layout (`@dagrejs/dagre`). Nodes positioned automatically based on dependency relationships.

**Build:** `cd frontend && npm install && npx vite build` outputs to `static/`. Served by FastAPI.

## Tested Against

| Repo | Source Files | Test Files | Mappings | Verified |
|---|---|---|---|---|
| psf/requests | 19 | 9 | 59 | auth.py, hooks.py, structures.py — all PASSED |
| httpie/cli | ~50 | 32 | 156 | sessions.py, cookies.py, downloads.py — all PASSED |
| demo_repo (built-in) | 2 | 2 | 3 | jwt.py, config.py — all PASSED |

## Evidence Classes & Partial Analysis (Track 2)

The code graph is **evidence, not an oracle**. Some repositories use dynamic
dispatch, generated code, or reflection that static analysis cannot fully
resolve, and `entire graph snapshot` can itself report partial failures. Every
selection Git-Blast makes is therefore graded so users and agents can tell
apart confirmed structural evidence, heuristic evidence, and claims that need
source or test verification.

### The classes (`src/evidence.py`)

| Class | Meaning | How it is decided |
|---|---|---|
| `confirmed` | structural fact | `resolution` in {`exact`, `import_resolved`}, `confidence ≥ 0.85`, no `warning_codes`, semantic-tier language, snapshot not partial |
| `heuristic` | graph produced it, weak | `resolution` in {`name_only`, `import_external`}, `0.5 ≤ confidence < 0.85`, a `warning_code`, a missing `resolution`, or a test picked by filename convention |
| `unverified` | unresolved / partial | an edge **touching** an unparsed file; an inventory-only language on a **surface** file; an unparsed file **on the surface**; `confidence < 0.5`; a modified file with no static importers in a package that imports dynamically; a surface file reachable only through a weaker edge |

> **Partial-ness is scoped, never repo-wide.** One broken file elsewhere in the
> repo (e.g. Flask ships `examples/tutorial/flaskr/schema.sql` with a
> tree-sitter syntax error) puts `snapshot_partial()` at `True` and is counted
> in `analysis_completeness.repo_parse_failures`, but it does **not** downgrade
> the hundreds of clean `import_resolved` Python edges. Only edges that touch
> the unparsed file, or a change whose own surface includes it, are affected.
> Likewise a bare `completeness_level: degraded` — normal for any polyglot repo
> because of inventory-only languages (YAML, Make, HTML, …) — does not by itself
> force `unverified`.

The classifier is calibrated for provider `v0.4.0`; it feature-detects
`relation_resolution` in the snapshot header and degrades to `heuristic` when a
snapshot predates the grading fields.

### Result fields added

```
confidence            : "high" | "medium" | "low"           (weakest link to the tests)
evidence              : {confirmed: [...], heuristic: [...], unverified: [...]}
analysis_completeness : {level, completeness_level, partial_failures,
                         inventory_only_languages, unparsed_files,
                         dynamic_dispatch_hits: [{path, markers}]}
verification_required : [{path, reason}]   (coverage the graph could NOT confirm)
fallback_level        : 0 db | 1 convention | 2 tests/ dir | 3 whole suite
```

New status: **`PASSED_UNVERIFIED`** — the selected tests passed, but the graph
could not confirm the selection covers the change. Never reported as a plain
`PASSED`. Exit code 0 (with a stderr warning from the CLI).

### The safe fallback / verification path

`--fallback` (CLI) / `fallback=` (MCP, `run_impact_analysis`) / `GIT_BLAST_FALLBACK`:

| Mode | Behaviour when confidence is not `high` (or no narrow set can be derived) |
|---|---|
| `off` | never widen; original lean behaviour |
| `report-only` | annotate `confidence` / `verification_required`, run only the graph's selection |
| `dir` *(default)* | widen to the `tests/` directories of the modified packages |
| `full` | widen to the whole pytest suite (`failure_summary` still capped) |

Fully-resolved code is unaffected: an edit whose surface is all `confirmed`
gets `confidence: high`, `fallback_level: 0`, and a byte-identical selection to
before Track 2.

### Fixture

`demo_repo_partial/` is a deliberately partially-analysable repo: `src/core.py`
reaches `src/handlers/*` only through `importlib` + `getattr` in
`src/registry.py`, so the graph has no edge to the handlers.
`tests/test_partial_repo.py` asserts that editing a handler yields
`PASSED_UNVERIFIED` with the file in `verification_required` and a fallback
still running its test, while editing `src/core.py` stays a plain high-confidence
`PASSED`. A captured snapshot is checked in at
`tests/fixtures/partial_repo.ndjson`.

See [`docs/evidence-consumers.md`](./docs/evidence-consumers.md) for the map of
which code consumes relationship / impact / semantic-diff evidence.

## Known Limitations

- **File-level granularity only.** Editing one function in a file triggers all tests that import that file, even if they use a different function. Runtime coverage tools like pytest-testmon track at method level.
- **Static analysis misses dynamic dispatch.** `getattr`, plugin registries, monkey-patching, and runtime-wired code won't appear in the AST graph. Git-Blast now *detects* the pattern (a source scan for `getattr` / `importlib` / `entry_points` / `.register(` …) and marks affected selections `unverified` so a fallback runs and the verdict is `PASSED_UNVERIFIED` — but it still cannot pinpoint exactly which dynamically-reached tests matter.
- **No historical signal.** Does not learn from past test failures. Pure structural analysis.
- **checkpoint_logs table exists but is not wired.** No Entire checkpoint hooks feed into it during normal flow.
- **`module_test_map` has no `evidence_class` column yet.** Evidence is recomputed live from the snapshot on every blast; it is not persisted per mapping.
- **Dashboard is not evidence-aware.** Node/edge glow does not yet reflect `confirmed` / `heuristic` / `unverified`.

## Next Stage: Databricks Integration

### What Changes

Replace SQLite with Databricks Delta Lake as the centralized test mapping store. Local SQLite fragments across machines. Databricks gives ACID-compliant shared state updated by CI/CD.

### Prerequisites

1. Databricks workspace with SQL Warehouse endpoint
2. Create schema: `CREATE SCHEMA IF NOT EXISTS dev_catalog.git_blast;`
3. Run DDL from `schema.sql` adapted for Delta: add `USING DELTA` to each CREATE TABLE
4. Generate personal access token
5. `pip install databricks-sql-connector`

### Steps

1. Set env vars:
   ```bash
   export DATABRICKS_SERVER_HOSTNAME="your-workspace.cloud.databricks.com"
   export DATABRICKS_HTTP_PATH="/sql/1.0/warehouses/your-warehouse-id"
   export DATABRICKS_TOKEN="dapi..."
   ```
2. `get_db()` auto-switches to `DatabricksDB` — no code changes needed
3. Run auto-seed: `python3 -m src.cli seed --auto --repo-root /path/to/repo --repo-id myrepo`
4. Verify: `python3 -m src.cli test --repo-root /path/to/repo --repo-id myrepo`

### CI/CD Integration

Add to CI pipeline (runs on every merge to main):
```yaml
- name: Update Git-Blast test mappings
  run: python3 -m src.cli seed --auto --repo-root . --repo-id $REPO_NAME
```

This keeps Databricks mappings in sync as the codebase evolves. Every developer and agent queries the same source of truth.

### Checkpoint Recovery (Curveball Defense)

Wire Entire checkpoint hooks to write to `checkpoint_logs` table. When an agent session is wiped (noon curveball), a fresh session queries the last 5 checkpoints from Databricks to re-hydrate context:

```python
db = get_db()
checkpoints = db.get_recent_checkpoints("myrepo", limit=5)
# Inject checkpoint summaries into agent prompt
```

Methods exist in both SQLiteDB and DatabricksDB. Only the hook wiring is missing.
