# Where Git-Blast consumes Entire Graph evidence

Track 2 asks that the product identify which parts of its implementation consume
**relationship**, **impact**, or **semantic-diff** evidence from the code graph,
and never present incomplete evidence as certain.

## Commands used

Git-Blast calls exactly one Entire Graph command: `entire graph snapshot`
(`src/parser.py::_run_snapshot`). It does **not** use `impact`, `diff`,
`neighbors`, `search`, or `def`. So:

| Evidence kind | Consumed? |
|---|---|
| Relationship (`IMPORTS`, `TESTS` relations) | **yes** — see below |
| Impact (`entire graph impact`) | no |
| Semantic diff (`entire graph diff` / `analyze` / `commit`) | no |

## Relationship-evidence consumers

| Site | Relation | What it does with it | Track 2 handling |
|---|---|---|---|
| `parser.parse_snapshot_ndjson` | `IMPORTS` (internal `file→file`) | builds the file dependency graph | keeps `confidence`, `resolution`, `relation_scope`, `warning_codes`; `_finalize_evidence` grades each edge with `evidence.classify_relation` into `Graph.edge_class` |
| `parser.parse_snapshot_ndjson` | `TESTS` (`symbol→symbol`) | seeds `Graph.test_edges` (test file ↔ unit under test) | resolved to file paths only; feeds `db.auto_seed` |
| `parser.parse_snapshot_ndjson` | `summary` record | — (previously ignored) | now captured: `partial_failures`, `language_tiers`, `stats.completeness_level` → `Graph.snapshot_partial()`, `Graph.analysis_completeness()` |
| `parser.reverse_import_surface` / `imported_by_paths` | `IMPORTS` | reverse-BFS blast radius (unweighted) | unchanged; the classified sibling `reverse_import_surface_classified` / `imported_by_edges` carries the **weakest edge class** along each path |
| `parser.get_modified_import_surface_classified` | `IMPORTS` | `{path: evidence class}` for the whole blast radius | modified files never indexed by the graph are returned `unverified` |
| `db.auto_seed` | `TESTS` + transitive `IMPORTS` + convention | populates `module_test_map` | convention-sourced rows are heuristic by construction |
| `runner.resolve_affected_tests` | derived surface + `db` / convention | selects the tests to run | computes `selection_class` (weakest link), `confidence`, `analysis_completeness`, `verification_required`; a dynamic-dispatch source scan (`evidence.scan_dynamic_dispatch`) downgrades files with no static importers in a package that imports dynamically |
| `runner.run_impact_analysis` | — | runs pytest, formats the verdict | escalates the fallback ladder when confidence is not `high`; a `low`-confidence pass becomes `PASSED_UNVERIFIED` |
| `server.get_graph_for_dashboard` | `IMPORTS` | React-Flow node/edge projection | not yet class-aware (dashboard glow is future work) |

## The three evidence classes

Defined in `src/evidence.py`:

| Class | Meaning | Trigger |
|---|---|---|
| `confirmed` | structural fact | `resolution` in {`exact`, `import_resolved`}, `confidence ≥ 0.85`, no `warning_codes`, semantic-tier language, snapshot not partial |
| `heuristic` | graph produced it, but weak | `resolution` in {`name_only`, `import_external`}, `0.5 ≤ confidence < 0.85`, a `warning_code`, a missing `resolution`, or a convention-matched test |
| `unverified` | could not resolve / partial | snapshot `partial_failures` or degraded `completeness_level`; inventory-only language on a relevant file; `confidence < 0.5`; a modified file with no static importers in a dynamically-importing package; a surface file reachable only through a weaker edge |

Healthy internal Python imports resolve at `import_resolved` / `0.88–0.95` — that
is the normal fully-resolved case and is deliberately `confirmed`.
