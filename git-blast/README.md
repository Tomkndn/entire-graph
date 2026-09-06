# Git-Blast Live

Test impact analysis for AI coding agents. When an agent edits a file,
Git-Blast figures out which tests are affected and runs only those, returning a
compact JSON result instead of dumping a full pytest log into the context
window.

**detect what changed → trace what depends on it → test only that → report in JSON.**

See [`SPEC.md`](./SPEC.md) for the full specification.

## How it works

```
git diff --name-only            ->  modified files
entire graph snapshot (IMPORTS) ->  reverse import graph
  BFS out from the modified files ->  import surface
module_test_map (SQLite/Databricks) lookup over the surface
  -> convention matching (matcher.py) as a fallback
pytest -q --tb=short --no-header  over just those test files
  -> lean JSON: {status, import_surface, affected_tests,
                 target_tests_executed, execution_time_seconds, ...}
```

## Layout

| Path | Purpose |
|---|---|
| `src/parser.py` | Runs `entire graph snapshot`, parses NDJSON, builds the import surface |
| `src/matcher.py` | Convention-based `src`→`test` fallback matching |
| `src/db.py` | SQLite + Databricks test-mapping store, auto-seeder |
| `src/runner.py` | Runs pytest on the targeted files, returns JSON; the shared blast pipeline |
| `src/mcp_server.py` | MCP tool (`git_blast_test`) for agent integration |
| `src/server.py` | FastAPI: REST + WebSocket + static dashboard |
| `src/cli.py` | CLI entry point (`seed`, `test`) |
| `src/evidence.py` | Evidence classification: `confirmed` / `heuristic` / `unverified` + dynamic-dispatch scan |
| `frontend/` | React Flow dashboard (builds to `static/`) |
| `demo_repo/` | Built-in two-module target repo (fully resolved) |
| `demo_repo_partial/` | Fixture with dynamic dispatch the graph cannot fully resolve |
| `schema.sql` | SQLite DDL |

## CLI

```bash
# Seed the module -> test map from a target repo's code graph
python3 -m src.cli seed --auto --repo-root /path/to/repo --repo-id myrepo

# Seed the bundled demo repo
python3 -m src.cli seed --demo --repo-id demo_repo

# Run the tests affected by the current working-tree changes
python3 -m src.cli test --repo-root /path/to/repo --repo-id myrepo
python3 -m src.cli test --repo-root . --repo-id myrepo --format agent --dry-run

# Control what happens when the graph cannot confirm the selection
python3 -m src.cli test --repo-root . --repo-id myrepo --fallback full
```

`test` exits 0 for `PASSED` / `PASSED_UNVERIFIED` / `NO_CHANGES` / `NO_TESTS` /
`DRY_RUN`, 1 for `FAILED`, 2 for an error (snapshot failure, `--max-depth < 0`).

### Evidence & confidence (Track 2)

The graph is evidence, not an oracle. Every result carries `confidence`
(`high` / `medium` / `low`), an `evidence` split of the surface into
`confirmed` / `heuristic` / `unverified`, `verification_required` (coverage the
graph could not confirm), and `fallback_level`. A `low`-confidence pass is
reported as `PASSED_UNVERIFIED`, never a plain `PASSED`.

`--fallback` (`GIT_BLAST_FALLBACK`): `off` | `report-only` | `dir` *(default,
widen to the package's `tests/` dir)* | `full` *(whole suite)*. Fully-resolved
code is unaffected — `confidence: high`, `fallback_level: 0`, same selection as
before. See [`SPEC.md`](./SPEC.md#evidence-classes--partial-analysis-track-2)
and [`docs/evidence-consumers.md`](./docs/evidence-consumers.md).

## Dashboard

```bash
cd frontend && npm install && npm run build   # -> ../static/
cd ..
GIT_BLAST_REPO_ROOT=/path/to/repo GIT_BLAST_REPO_ID=myrepo \
  python3 -m uvicorn src.server:app --port 8000
```

Endpoints: `GET /api/status`, `GET /api/graph`, `POST /api/blast`, `WS /ws`.
The blast streams `blast_started → surface_detected → db_query (querying,
complete) → test_result` over the WebSocket.

## MCP

```bash
GIT_BLAST_REPO_ROOT=/path/to/repo python3 -m src.mcp_server
```

Exposes one tool, `git_blast_test(repo_id="main-repo")`, returning the
single-line agent payload.

## Development

```bash
cd git-blast
uv run --extra dev python -m pytest tests/ -v
```

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `GIT_BLAST_DB` | SQLite database path | `git_blast.db` |
| `GIT_BLAST_REPO_ROOT` | Target repo path (server / MCP) | `.` |
| `GIT_BLAST_REPO_ID` | Repo identifier for DB queries | `main-repo` |
| `GIT_BLAST_ENTIRE_BIN` | Entire CLI binary name | `entire` |
| `GIT_BLAST_FALLBACK` | Fallback policy: `off` / `report-only` / `dir` / `full` | `dir` |
| `DATABRICKS_SERVER_HOSTNAME` | Databricks SQL Warehouse host | unset (uses SQLite) |
| `DATABRICKS_HTTP_PATH` | Databricks SQL Warehouse HTTP path | unset |
| `DATABRICKS_TOKEN` | Databricks personal access token | unset |
