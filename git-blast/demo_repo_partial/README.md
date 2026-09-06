# demo_repo_partial

A deliberately **partially-analysable** fixture repo for Git-Blast's Track 2
behaviour ("the graph is evidence, not an oracle").

`src/core.py` dispatches work to a format handler, but it does so through
`src/registry.py`, which resolves handlers at runtime with
`importlib.import_module` + `getattr`. The handler modules
(`src/handlers/pdf.py`, `src/handlers/csv.py`) are therefore **never the target
of a static `IMPORTS` edge** — `entire graph snapshot` cannot see that
`core.py` reaches them.

Consequences a consumer must handle:

- Editing `src/handlers/pdf.py` yields an empty reverse-import surface. A naive
  impact tool reports `NO_TESTS` and the change ships untested.
- The graph *looks* complete (every file parses, Python is a semantic tier),
  so completeness metadata alone does not flag the gap — the dynamic-dispatch
  **pattern** in `registry.py` is the signal.
- `tests/test_pdf_handler.py` only reaches the edited code through that dynamic
  edge, so it must be picked up by a fallback, not by graph reachability.

`generated/_pb2.py` is a stand-in for generated code: statically imported by
`core.py` (that edge *is* resolved) but marked `@generated`.
