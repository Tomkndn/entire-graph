# Git-Blast Live

Test impact analysis for AI coding agents. When an agent edits a file,
Git-Blast figures out which tests are affected and runs only those, returning a
compact JSON result instead of dumping a full pytest log into the context
window.

**detect what changed → trace what depends on it → test only that → report in JSON.**

See [`SPEC.md`](./SPEC.md) for the full specification.

## Layout

| Path | Purpose |
|---|---|
| `src/parser.py` | Runs `entire graph snapshot`, parses NDJSON, builds the import surface |
| `src/matcher.py` | Convention-based `src`→`test` fallback matching |
| `src/db.py` | SQLite + Databricks test-mapping store, auto-seeder |
| `src/runner.py` | Runs pytest on the targeted files, returns JSON |
| `src/mcp_server.py` | MCP tool for agent integration |
| `src/server.py` | FastAPI: REST + WebSocket + static dashboard |
| `src/cli.py` | CLI entry point |
| `frontend/` | React Flow dashboard (builds to `static/`) |
| `demo_repo/` | Built-in two-module target repo |
| `schema.sql` | SQLite DDL |

## Development

```bash
cd git-blast
uv run --extra dev python -m pytest tests/ -v
```

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `GIT_BLAST_DB` | SQLite database path | `git_blast.db` |
| `GIT_BLAST_REPO_ROOT` | Target repo path (server) | `.` |
| `GIT_BLAST_REPO_ID` | Repo identifier for DB queries (server) | `main-repo` |
| `DATABRICKS_SERVER_HOSTNAME` | Databricks SQL Warehouse host | unset (uses SQLite) |
| `DATABRICKS_HTTP_PATH` | Databricks SQL Warehouse HTTP path | unset |
| `DATABRICKS_TOKEN` | Databricks personal access token | unset |
