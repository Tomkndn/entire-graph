# Git-Blast Live

## One-sentence summary

Git-Blast Live is a test-impact analysis tool for AI coding agents: when an agent edits a file it uses the Entire Graph to trace which tests could be affected, runs only those with pytest, and returns a compact JSON verdict instead of flooding the model's context with a full test log.

## Problem, intended user and why it matters

**User:** AI coding agents (Claude Code, Cursor, Codex) and the developers supervising them.

**Problem:** after an edit, an agent's reflex is to run `pytest`. On a real repo that dumps hundreds to thousands of lines into the context window, which causes:

- **Token waste** — most of the output is about tests unrelated to the change.
- **Hallucination cascades** — the agent sees unrelated failures and tries to "fix" them, expanding the blast radius of its own edit.
- **Slow loops** — the full suite can take minutes per iteration.

**Why it matters:** the feedback loop after an edit is the tightest loop in agentic coding. Making it *scoped* and *lean* directly reduces cost, wall-clock time, and the chance the agent goes off the rails. Git-Blast turns "run the suite and read 800 lines" into "run 7 files, read 12 lines of JSON."

Measured on `psf/requests`: a one-line change to `src/requests/utils.py` selected **7 of 12** test files (599 of ~1150 tests) and returned a result object whose failure section is capped at the last 50 lines.

## Selected Entire track and why Entire is essential

**Track:** Entire Graph, including Track 2 ("the graph is evidence, not an
oracle") — Git-Blast grades every relation it consumes and never presents an
incomplete graph as certainty.

**Why Entire is essential, not incidental:**

- The entire product *is* a consumer of `entire graph snapshot`. The core question — "given these changed files, which files import them, transitively?" — is answered by walking the `IMPORTS` relations in the graph. Without a precomputed, language-aware import graph we would be back to grep heuristics or a bespoke per-language import parser.
- **No-egress matters for the use case.** Git-Blast runs inside the agent's loop on the user's machine and on CI. Entire Graph's analysis is local, deterministic, no network, no keys — so Git-Blast inherits those properties for free. A hosted graph service would make it unusable in exactly the environments it targets.
- **Tree-sitter breadth.** `IMPORTS` resolution works across 36 languages. Git-Blast's Python packaging is just the first target; the impact algorithm is language-agnostic because the graph is.
- **`TESTS` relations** give a second, symbol-accurate signal (test symbol → unit under test) that seeds the mapping database beyond pure import reachability.

## Architecture and main workflow

```
agent edits a file
      │
      ▼
git diff --name-only  ─────────────►  modified files  (staged + unstaged + untracked .py)
      │
      ▼
entire graph snapshot --format ndjson --worktree
      │   parse: file records + IMPORTS relations (internal file→file only;
      │          external:import:* dropped) + TESTS relations
      ▼
reverse-IMPORTS BFS from the modified files  ─────►  import surface
      │   (max-depth 0 = unlimited, 1 = direct importers, N = N hops)
      │   each surface file carries the weakest edge class on its path:
      │   confirmed / heuristic / unverified   (src/evidence.py)
      ▼
module_test_map lookup over the surface  (SQLite, or Databricks)
      │   miss → convention matcher (test_<stem>.py / <stem>_test.py / tests/ mirror)
      ▼
confidence + fallback ladder (Track 2)
      │   confidence != high or no narrow set  → widen:
      │   0 db → 1 convention → 2 tests/ dir → 3 whole suite   (--fallback)
      ▼
pytest -q --tb=short --no-header  <the affected test files, or whole suite>   (repo's .venv if present)
      │
      ▼
lean JSON:  { status, import_surface, affected_tests, target_tests_executed,
              execution_time_seconds, total_time_seconds, summary,
              failure_summary (last 50 lines, on failure),
              confidence, evidence {confirmed|heuristic|unverified},
              analysis_completeness, verification_required, fallback_level }
```

`status` is `PASSED` / `PASSED_UNVERIFIED` / `FAILED` / `NO_TESTS` /
`NO_CHANGES` / `DRY_RUN` / `ERROR`. A pass the graph could not confirm is
`PASSED_UNVERIFIED`, never a plain `PASSED`.

**Components** (all under `git-blast/`):

| Module | Role |
|---|---|
| `src/parser.py` | Runs `entire graph snapshot`, parses NDJSON (keeping `confidence` / `resolution` / `warning_codes` and the `summary` record), builds the file/IMPORTS graph, grades every edge, computes `get_modified_files()` and the classified reverse-import surface, and a React-Flow projection |
| `src/evidence.py` | The Track 2 core: `classify_relation()` → `confirmed` / `heuristic` / `unverified`; `weakest()` for path propagation; `scan_dynamic_dispatch()` source scan (`getattr`, `importlib`, `entry_points`, `.register(`, …) |
| `src/matcher.py` | Convention fallback: five ordered `src`→`test` strategies + dedupe; strict `test_*.py` / `*_test.py` detection |
| `src/db.py` | `TestMapDB` protocol; `SQLiteDB` and `DatabricksDB` backends; `auto_seed()` (TESTS relations + transitive IMPORTS + convention); `get_recent_checkpoints()` for session recovery |
| `src/runner.py` | `run_targeted_tests()` (+ whole-suite mode) + the end-to-end `run_impact_analysis()` pipeline; confidence + fallback ladder; lean / single-line agent output formatting |
| `src/cli.py` | `git-blast seed` / `git-blast test` (`--fallback`, `--dry-run`, `--format agent`, `--max-depth`) |
| `src/mcp_server.py` | One MCP tool, `git_blast_test(repo_id, fallback)`, returning the single-line agent payload with the confidence / evidence / verification fields (MCP v2 / `MCPServer`) |
| `src/server.py` | FastAPI: `/api/graph`, `/api/status`, `/api/blast`, `WS /ws`; serves the built dashboard |
| `frontend/` | React 19 + `@xyflow/react` + Dagre dashboard; nodes glow gray → amber (modified) → blue (querying/running) → green/red (passed/failed) as WebSocket events arrive |

**Three ways to invoke it:** CLI (`git-blast test`), MCP tool (for the agent itself), and the dashboard (`POST /api/blast`, streamed over `/ws`). All three call the same `run_impact_analysis()`.

## Entire Graph findings and verification

**Entire CLI / plugin:** `entire` v0.10.x, `entire graph` plugin **v0.4.0**.

**Snapshot shape verified against this repo and against `psf/requests`:**

- Line 1 is the header — **no `record_type`** — carrying `schema_version: "1.1"`, `repo_root`, `repo_key`, `commit`, `tree`.
- Then `file` records (`id` = `<repo_key>:file:<path>`, `path`, `language`, `bytes`), `symbol` records (`file_path`, `start_line`, `end_line`, `kind`, `name`), `relation` records (`from_id`, `to_id`, `type`), and a trailing `summary` record. `external` records also appear. Everything except `file` and `IMPORTS`/`TESTS` relations is ignored.
- **`IMPORTS` includes internal file→file edges.** In this Go repo: 2286 `IMPORTS` total, ~144 internal (`target_kind: "file"`, both ids `<repo_key>:file:<path>`), the rest `external:import:*` (dropped). Python relative and resolved module imports resolve to file→file at confidence 0.88–0.95.
- **`TESTS` relations are symbol→symbol** (`..._test.go:function:TestX` → `...go:function:X`), matched by test-name convention; the test-side path is also in `evidence[].file_path`. `auto_seed()` resolves both sides to file paths.
- Use `--worktree` so a freshly-added untracked file appears in the graph.

- **Grading fields, verified against `pallets/flask` (`entire` v0.4.0):** internal Python imports resolve as `resolution: import_resolved` at `confidence` 0.88–0.95 with `relation_scope: module` and no `warning_codes` — this is the fully-resolved case and is classed `confirmed`. `resolution: name_only` and `import_external` are `heuristic`. The `summary` record carries `partial_failures`, `language_tiers` (`semantic` vs `inventory-only`), and `stats.completeness_level`.

**Verification performed:**

- **Unit + integration suite:** 135 Python tests, including a live test that runs a real `entire graph snapshot` over a throwaway git repo and asserts the transitive surface — editing `pkg/base.py` selects `test_base` + `test_leaf`; editing `pkg/leaf.py` selects only `test_leaf`.
- **Live on `psf/requests`:** `seed --auto` produced 129 mappings across 19 source files / 12 test files. A comment-only change to `src/requests/utils.py` produced a 19-entry import surface, selected 7 test files, ran 599 tests in ~69 s, and returned `status: FAILED` with an 8-failure tail. Those 8 failures (`test_use_proxy_from_environment[*]`) were confirmed **pre-existing and environmental** (`PySocks` missing from the repo's venv) by running the same node directly under plain pytest — Git-Blast reports the true pytest exit code and does not mask it.
- **Blast-radius sanity check:** `src/requests/help.py` → only `tests/test_help.py`; `src/requests/structures.py` → 8 files; `src/requests/utils.py` → 7 files. Wider fan-in ⇒ wider surface, as expected.
- **Track 2, live on `pallets/flask`:** all 179 internal edges class `confirmed` (Flask ships one broken example `schema.sql` — its parse failure downgrades only edges that touch it, not the repo). After `seed --auto`, editing `src/flask/blueprints.py` → `confidence: high`, `fallback_level: 0`, `status: PASSED`. Editing `src/flask/cli.py` (uses `getattr` / `__import__` / `entry_points`) → `confidence: low`, `verification_required` names it, the fallback ladder widens the run, `status: PASSED_UNVERIFIED`.
- **Track 2, on the `demo_repo_partial/` fixture:** editing `src/handlers/pdf.py` (reached only through `importlib` + `getattr` in `src/registry.py`) → `status: PASSED_UNVERIFIED`, `confidence: low`, `evidence.unverified: [src/handlers/pdf.py]`, `dynamic_dispatch_hits: [{path: src/registry.py, markers: [getattr, importlib]}]`, the fallback widened to the package `tests/` dir and ran `test_pdf_handler.py`. Editing the fully-resolved `src/core.py` → `confidence: high`, `fallback_level: 0`, `status: PASSED`, `verification_required: []`.
- **Dashboard:** `GET /` serves the built React app; `/api/graph` returned 33 nodes / 107 edges for `requests` with the two modified files flagged; `POST /api/blast` streamed `blast_started → surface_detected → db_query(querying) → db_query(complete) → test_result` over `/ws`.

## Track 2 — Graph is evidence, not an oracle

**Status: complete.** The graph only sees static structure. A repo that wires
code together with `importlib` / `getattr` / plugin registries produces an
**incomplete** graph, and `entire graph snapshot` can itself report partial
failures. Git-Blast now grades every relation it consumes and never presents an
incomplete graph as certainty.

### Three evidence classes (`src/evidence.py`)

| Class | Meaning | Decided by |
|---|---|---|
| `confirmed` | structural fact | `resolution` in {`exact`, `import_resolved`}, `confidence ≥ 0.85`, no `warning_codes`, semantic-tier language |
| `heuristic` | graph produced it, weak | `resolution` in {`name_only`, `import_external`}, `0.5 ≤ confidence < 0.85`, a `warning_code`, a missing `resolution`, or a test chosen by filename convention |
| `unverified` | unresolved / partial | an edge touching an unparsed file; an inventory-only language or unparsed file **on the change's surface**; `confidence < 0.5`; a modified file with no static importers in a package that imports dynamically; a surface file reached only through a weaker edge |

The classifier feature-detects `relation_resolution` in the snapshot header and
degrades to `heuristic` on older snapshots.

### What each requirement maps to

| Track 2 requirement | Implementation |
|---|---|
| Never present incomplete relationships as certain | every edge graded; blast-radius walk keeps the weakest class on each path; a `low`-confidence pass is `PASSED_UNVERIFIED` |
| Identify when analysis may be partial | `parser` reads the `summary` record → `Graph.snapshot_partial()`, `analysis_completeness()` (`partial_failures`, `inventory_only_languages`, `unparsed_files`, `repo_parse_failures`); `evidence.scan_dynamic_dispatch()` flags `getattr` / `importlib` / `entry_points` / `.register(` |
| Provide a safe fallback or verification path | `--fallback off \| report-only \| dir \| full` (default `dir`) widens the run when confidence is not `high`; `verification_required: [{path, reason}]` names every file whose coverage the graph could not confirm |
| Existing behaviour for fully-resolved code | an all-`confirmed` surface still yields `confidence: high`, `fallback_level: 0`, and the same selection as before; of the 107 pre-Track-2 tests, 104 are unchanged and 3 (which asserted the old `NO_TESTS` dead-end that the fallback ladder now fills) were updated to cover both `--fallback off` and the default |
| A test / fixture for incomplete analysis | `demo_repo_partial/` (runtime handler dispatch), captured snapshot `tests/fixtures/partial_repo.ndjson`, and `tests/test_evidence.py` / `tests/test_partial_repo.py` |
| Identify which code consumes relationship / impact / semantic-diff evidence | `docs/evidence-consumers.md` — Git-Blast calls only `entire graph snapshot`; it consumes **relationship** evidence (`IMPORTS`, `TESTS`) and no `impact` or semantic-`diff` evidence |
| Users and agents can tell the three apart | result fields `confidence`, `evidence {confirmed \| heuristic \| unverified}`, `verification_required`, `analysis_completeness`, `fallback_level`; surfaced in the CLI lean JSON, the CLI stderr warning, and the MCP agent payload |

### Partial-ness is scoped, never repo-wide

A single broken file elsewhere in the repo (Flask ships
`examples/tutorial/flaskr/schema.sql` with a tree-sitter syntax error) sets
`snapshot_partial()` to `True` and is counted in `repo_parse_failures`, but it
does **not** downgrade the hundreds of clean `import_resolved` Python edges.
Only edges that touch the unparsed file — or a change whose own surface
includes it — are affected. A bare `completeness_level: degraded` (normal for
any polyglot repo, because of YAML / Make / HTML inventory-only languages) does
not by itself force `unverified` either.

## Noon Curveball — Track 2

The curveball was Track 2 itself: "your Graph-powered experience has encountered
a repository using dynamic dispatch, generated code, or reflection that static
analysis cannot fully resolve." The adaptation is the whole section above —
`src/evidence.py`, the grading pass in `parser.py`, the confidence + fallback
ladder in `runner.py`, the `demo_repo_partial/` fixture, and the scoped
partial-ness fix found while verifying against `pallets/flask`. Behaviour for
fully-resolved repos is unchanged.

### Session-wipe recovery (independent defense, already in the codebase)

- `checkpoint_logs` table (`repo_id`, `checkpoint_sha`, `prompt_summary`, `files_modified`, `created_at`) in `schema.sql`, present in both the SQLite and Databricks backends.
- `db.get_recent_checkpoints(repo_id, limit=5)` and `db.log_checkpoint(...)` on both backends.
- Recovery plan (SPEC "Checkpoint Recovery"): a fresh session queries the last N checkpoints from the shared store and re-hydrates its context from `prompt_summary` + `files_modified`.
- Independently, every stage commit carries an `Entire-Checkpoint:` trailer, so the Entire checkpoint history *is* the recovery record even without the table being wired.

**Not done:** the git-hook wiring that would write `checkpoint_logs` during a normal session — only the storage + retrieval half is implemented (see Known Limitations).

## Checkpoint links and what each checkpoint proves

Branch `git-blast`, one Entire checkpoint per reviewable stage (`entire checkpoint list`):

| Checkpoint | Commit | Proves |
|---|---|---|
| `ced633264dc8` | `6c4a167` | Package scaffold + `schema.sql` + `SQLiteDB` (protocol, idempotent upsert, `repo_id` isolation, checkpoint log/retrieve); `demo_repo` fixture |
| `1e37972e3044` | `31d0e74` | `parser.py` — snapshot parse, `git diff` union, reverse-IMPORTS BFS with `max_depth`, dashboard projection; live snapshot test green |
| `18e3332631a0` | `6fa7139` | `matcher.py` (5 strategies) + `auto_seed()` (TESTS + transitive IMPORTS + convention) + full `DatabricksDB` (MERGE, `%()s` params, fake-connection tested) |
| `167c33882d36` | `210c44b` | `runner.py` — targeted `pytest -q --tb=short --no-header`, exit-code → status mapping, `run_impact_analysis` pipeline, lean / agent formatting |
| `35a2b5fbe295` | `619c964` | `cli.py` — `seed` / `test`, exit codes, `--dry-run` / `--format agent` / `--max-depth` |
| `039a28089c80` | `478f958` | `mcp_server.py` — exactly one tool `git_blast_test(repo_id)` on MCP v2, end-to-end via `call_tool` without a live client |
| `df3b31ed735a` | `614cd55` | `server.py` — REST + `/ws`; the exact event order asserted through `TestClient` |
| `06f1579dba32` | `aec7701` | `frontend/` — React 19 + xyflow + Dagre; `npm run build` (tsc + vite) → `static/`, served by FastAPI |
| `3883ecddc59d` | `c4923f1` | End-to-end integration tests over real git + real snapshot + real pytest; `mcp>=2.0` pin; README |
| `641c5bce403a` | `bc5064a` | Fix found during the live `requests` run: only `test_*.py` / `*_test.py` are executed, not every `.py` under `tests/` |
| _(this session)_ | _(pending commit)_ | Track 2 — `src/evidence.py`, edge grading + `summary` capture in `parser.py`, confidence + fallback ladder in `runner.py`, `--fallback` CLI flag, MCP payload fields, `demo_repo_partial/` fixture + `tests/fixtures/partial_repo.ndjson`, `tests/test_evidence.py` / `tests/test_partial_repo.py`, `docs/evidence-consumers.md`; 107 → 135 tests |

Run `entire checkpoint show <id>` for the full detail of any stage.

## Setup, run and test instructions

Prerequisites: the Entire CLI with the `graph` plugin (`entire graph version` → `v0.4.0`), Python 3.10+, `uv`, and Node 20+ for the dashboard.

```bash
cd git-blast
```

### Test the project

```bash
uv run --extra dev python -m pytest tests/ -q          # 135 passing
```

### Use it on a target repo

```bash
export GIT_BLAST_DB=/path/to/target/git_blast.db       # keep this consistent

# 1. build the module -> test map from the target repo's code graph
uv run python -m src.cli seed --auto --repo-root /path/to/target --repo-id myrepo

# 2. after editing files in the target repo, run only the affected tests
uv run python -m src.cli test --repo-root /path/to/target --repo-id myrepo
#   --dry-run              show the selection without running pytest
#   --format agent         single-line JSON for an agent's context
#   --max-depth 1          limit the surface to direct importers
#   --fallback off|report-only|dir|full   what to do when the graph cannot
#                          confirm the selection (default: dir)
```

`test` exits 0 for `PASSED` / `PASSED_UNVERIFIED` / `NO_CHANGES` / `NO_TESTS` / `DRY_RUN`, 1 for `FAILED`, 2 for an error. On a `low`-confidence result it also prints a one-line `warning:` to stderr.
The runner uses `<target>/.venv/bin/python` if it exists, otherwise the calling interpreter — so pass `--extra dev` when the target repo has no venv of its own.

### Dashboard

```bash
cd frontend && npm install && npm run build && cd ..   # -> ../static/
GIT_BLAST_REPO_ROOT=/path/to/target GIT_BLAST_REPO_ID=myrepo GIT_BLAST_DB=/path/to/target/git_blast.db \
  uv run python -m uvicorn src.server:app --port 8000
# open http://localhost:8000, edit a file in the target repo, click "Blast"
```

### MCP server

```bash
GIT_BLAST_REPO_ROOT=/path/to/target GIT_BLAST_REPO_ID=myrepo \
  uv run python -m src.mcp_server
```

Exposes one stdio tool, `git_blast_test(repo_id="main-repo", fallback="dir")`.
Its payload includes `confidence`, the `evidence` split, `verification_required`,
`analysis_completeness`, and `fallback_level` so the agent can tell confirmed
structural evidence from heuristic or unverified evidence. `GIT_BLAST_FALLBACK`
sets the default policy.

### Verified example (`psf/requests`)

```bash
python3 -m venv /path/to/requests/.venv
/path/to/requests/.venv/bin/pip install -e . pytest pytest-httpbin pytest-mock  # add PySocks for the proxy tests
export GIT_BLAST_DB=/path/to/requests/git_blast.db
uv run python -m src.cli seed --auto --repo-root /path/to/requests --repo-id requests
# edit e.g. src/requests/help.py, then:
uv run python -m src.cli test --repo-root /path/to/requests --repo-id requests
```

## Databricks use, data sources and limitations

**Data sources:**

- `entire graph snapshot` NDJSON — `file` records and `IMPORTS` / `TESTS` relations. No other command is used.
- `git diff` / `git ls-files` for the changed-file set.
- The `module_test_map` table (source_file → test_file, optionally with the imported symbol and last execution status).

**Databricks:** `DatabricksDB` implements the same `TestMapDB` interface against `dev_catalog.git_blast.module_test_map` / `checkpoint_logs` over `databricks-sql-connector`. `get_db()` selects it automatically when `DATABRICKS_SERVER_HOSTNAME` (+ `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN`) is set, else SQLite. Upserts use `MERGE`; every value is a `%()s` parameter; only the trusted catalog/schema config is interpolated into identifiers, and it is validated as a bare identifier first. The intent (SPEC "Next Stage") is a shared, ACID mapping store that CI refreshes on every merge to `main` so all agents and developers query one source of truth, plus checkpoint recovery for wiped sessions.

**Limitations of the Databricks path:**

- **Not verified against a live SQL Warehouse** in this build. It is unit-tested with an injected fake connection that records SQL and parameters; the `MERGE` / array / paramstyle choices are believed correct for `databricks-sql-connector` 3.x but unproven end-to-end.
- Delta has no `ON CONFLICT`; correctness of the `MERGE` predicate for a `NULL` `imported_symbol` relies on the explicit `IS NULL` branch.
- `rowcount` is unreliable on Databricks, so `clear_mappings()` does a `COUNT(*)` then `DELETE`.
- No connection pooling / retry beyond a single lazily-opened connection.

## Known limitations and next steps

**Limitations:**

- **File-level granularity.** Editing one function pulls in every test that imports the file, even if it exercises a different function. Symbol-level diagnostics exist as a design idea but are not used to filter tests.
- **Static analysis only.** `getattr`, plugin registries, monkey-patching, fixtures that wire things at runtime, and dynamic dispatch are invisible to the AST graph. Git-Blast now *detects* the pattern and marks the selection `unverified` (→ `PASSED_UNVERIFIED` + a fallback run + a `verification_required` entry), but it still cannot say exactly which dynamically-reached tests matter.
- **No historical signal.** Selection is purely structural; it does not learn from which tests have failed for which changes before.
- **Widely-imported modules produce wide surfaces.** A change to something like `utils.py` legitimately selects most of the suite — correct, but not a big saving for that class of edit.
- **`checkpoint_logs` is storage-only.** No git-hook writes to it during a normal session; recovery would today read the Entire checkpoint history instead.
- **Python-only test execution.** The impact algorithm is language-agnostic (it is just the graph), but `runner.py` shells out to `pytest`.
- **Databricks path unverified against a real warehouse** (see above).
- **Evidence is recomputed live, not persisted.** `module_test_map` has no `evidence_class` column; the confidence grade is derived from the snapshot on every blast rather than stored per mapping.
- **Dashboard is not evidence-aware.** Node / edge glow does not yet reflect `confirmed` / `heuristic` / `unverified`.
- **No baseline diff.** A `FAILED` result does not distinguish a regression the edit caused from a pre-existing / environmental failure.

**Next steps:**

1. Verify and harden `DatabricksDB` against a real SQL Warehouse; add the CI job that runs `seed --auto` on every merge to `main`.
2. Wire an Entire checkpoint hook to populate `checkpoint_logs`, and add a `recover` command that re-hydrates a fresh session from the last N checkpoints.
3. Add a `module_test_map.evidence_class` column so the grade is stored per mapping and the dashboard can render it.
4. Baseline diff: run the selection against `git stash` (or the stored per-nodeid status) so a failure is reported as `REGRESSED` vs `PASSED_WITH_KNOWN_FAILURES`.
5. Optional symbol-level narrowing: use `diff` hunk ranges against `SymbolRecord` line spans to *rank* affected tests (still run the file-level set, but run the most-likely-affected first).
6. Pluggable runners (Go `go test`, JS `vitest`/`jest`) behind the same result schema.
7. Cache the parsed graph between invocations keyed on `tree` from the snapshot header.
