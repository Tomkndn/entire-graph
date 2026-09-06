"""Targeted pytest execution + the end-to-end blast pipeline (SPEC.md "runner.py").

``run_targeted_tests()`` runs ``pytest -q --tb=short --no-header`` over just the
given test files and returns a lean JSON-able dict. ``run_impact_analysis()``
wires the whole flow together — git diff → import surface → DB lookup →
convention fallback → run — and is the single entry point shared by the CLI,
the MCP server, and the FastAPI dashboard.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import evidence, matcher, parser

# Optional hook the dashboard uses to stream pipeline events. It is called with
# (event_name, payload) and must not raise.
ProgressFn = Callable[[str, dict], None]


def _emit(progress: ProgressFn | None, event: str, payload: dict) -> None:
    if progress is None:
        return
    try:
        progress(event, payload)
    except Exception:  # pragma: no cover - progress is best-effort
        pass

PYTEST_ARGS = ["-q", "--tb=short", "--no-header"]
FAILURE_TAIL_LINES = 50
AGENT_FAILURE_CHARS = 600
AGENT_VERIFICATION_ITEMS = 8

# Status vocabulary shared across the CLI / MCP / dashboard.
STATUS_NO_CHANGES = "NO_CHANGES"
STATUS_NO_TESTS = "NO_TESTS"
STATUS_DRY_RUN = "DRY_RUN"
STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"
STATUS_ERROR = "ERROR"
# Track 2: the selected tests passed, but the graph could not confirm the
# selection covers the change (dynamic dispatch, partial snapshot, weak edges).
STATUS_PASSED_UNVERIFIED = "PASSED_UNVERIFIED"

# Fallback ladder (Track 2 "safe fallback or verification path").
FALLBACK_OFF = "off"                # never widen; current lean behaviour
FALLBACK_REPORT_ONLY = "report-only"  # annotate confidence, never widen the run
FALLBACK_DIR = "dir"               # widen to the tests/ dirs of modified packages
FALLBACK_FULL = "full"            # widen to the whole pytest suite
FALLBACK_MODES = (FALLBACK_OFF, FALLBACK_REPORT_ONLY, FALLBACK_DIR, FALLBACK_FULL)
DEFAULT_FALLBACK = FALLBACK_DIR

# Cap on files fed to the dynamic-dispatch source scan per blast.
_DYNAMIC_SCAN_CAP = 400

_LEVEL_DB = 0
_LEVEL_CONVENTION = 1
_LEVEL_DIR = 2
_LEVEL_FULL = 3

VERIFICATION_LIST_CAP = 25

# pytest's own exit codes.
_PYTEST_OK = 0
_PYTEST_TESTS_FAILED = 1
_PYTEST_INTERRUPTED = 2
_PYTEST_INTERNAL_ERROR = 3
_PYTEST_USAGE_ERROR = 4
_PYTEST_NO_TESTS_COLLECTED = 5


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _filter_test_files(paths: Iterable[str], cwd: str | os.PathLike[str]) -> list[str]:
    """Keep existing, deduped, sorted paths that look like test files."""
    root = Path(cwd)
    seen: set[str] = set()
    out: list[str] = []
    for raw in paths:
        if not raw:
            continue
        rel = raw.replace(os.sep, "/")
        if rel in seen:
            continue
        if not matcher.looks_like_test_file(rel):
            continue
        if not (root / rel).is_file():
            continue
        seen.add(rel)
        out.append(rel)
    return sorted(out)


def _find_venv_python(cwd: str | os.PathLike[str]) -> str | None:
    """Return the repo's virtualenv interpreter if one is present."""
    root = Path(cwd)
    candidates = [
        root / ".venv" / "bin" / "python",
        root / "venv" / "bin" / "python",
        root / ".venv" / "Scripts" / "python.exe",
        root / "venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _combined_output(proc: subprocess.CompletedProcess) -> str:
    parts = [p for p in (proc.stdout, proc.stderr) if p]
    return "\n".join(parts).strip()


def _summary_line(output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return ""
    for line in reversed(lines):
        stripped = line.strip("= ")
        if any(
            token in stripped
            for token in (" passed", " failed", " error", " skipped", "no tests ran")
        ):
            return stripped
    return lines[-1]


def _tail(output: str, n: int) -> tuple[str, bool]:
    lines = output.splitlines()
    if len(lines) <= n:
        return output, False
    return "\n".join(lines[-n:]), True


def truncate_output(output: str, n: int = FAILURE_TAIL_LINES) -> str:
    """Last ``n`` lines of ``output`` with a notice when lines were dropped."""
    tail, truncated = _tail(output, n)
    if truncated:
        return f"... (output truncated to last {n} lines) ...\n{tail}"
    return tail


# ---------------------------------------------------------------------------
# run just the tests
# ---------------------------------------------------------------------------

def run_targeted_tests(
    test_files: Sequence[str],
    import_surface: Sequence[str] | None = None,
    cwd: str | os.PathLike[str] = ".",
    *,
    extra_args: Sequence[str] | None = None,
    timeout: float | None = None,
    dry_run: bool = False,
    whole_suite: bool = False,
) -> dict:
    """Run pytest over ``test_files`` and return a lean result dict.

    ``whole_suite=True`` is the top rung of the fallback ladder: pytest is
    invoked with no path arguments so it discovers the entire suite. The
    ``failure_summary`` stays capped at :data:`FAILURE_TAIL_LINES` either way, so
    a full run still cannot flood an agent's context.
    """
    cwd = str(cwd)
    surface = sorted(set(import_surface or []))
    filtered = _filter_test_files(test_files, cwd)

    result: dict = {
        "status": STATUS_NO_TESTS,
        "import_surface": surface,
        "target_tests_executed": [],
        "execution_time_seconds": 0.0,
        "returncode": None,
        "command": None,
        "summary": "",
    }

    if not filtered and not whole_suite:
        result["summary"] = "no affected test files to run"
        return result

    python = _find_venv_python(cwd) or sys.executable
    pytest_targets = [] if whole_suite else filtered
    cmd = [python, "-m", "pytest", *PYTEST_ARGS, *(extra_args or []), *pytest_targets]
    result["command"] = " ".join(
        ["pytest", *PYTEST_ARGS, *(extra_args or []), *pytest_targets]
    )
    result["target_tests_executed"] = ["<whole suite>"] if whole_suite else filtered

    if dry_run:
        result["status"] = STATUS_DRY_RUN
        result["summary"] = (
            "would run the whole suite"
            if whole_suite
            else f"would run {len(filtered)} test file(s)"
        )
        return result

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        result["status"] = STATUS_ERROR
        result["execution_time_seconds"] = round(time.monotonic() - started, 3)
        result["summary"] = f"pytest timed out after {timeout}s"
        return result
    except OSError as exc:
        result["status"] = STATUS_ERROR
        result["summary"] = f"could not launch pytest: {exc}"
        return result

    elapsed = round(time.monotonic() - started, 3)
    output = _combined_output(proc)
    result["execution_time_seconds"] = elapsed
    result["returncode"] = proc.returncode
    result["summary"] = _summary_line(output)

    if proc.returncode == _PYTEST_OK:
        result["status"] = STATUS_PASSED
    elif proc.returncode == _PYTEST_NO_TESTS_COLLECTED:
        result["status"] = STATUS_NO_TESTS
    elif proc.returncode == _PYTEST_TESTS_FAILED:
        result["status"] = STATUS_FAILED
    else:
        result["status"] = STATUS_ERROR

    if result["status"] in (STATUS_FAILED, STATUS_ERROR):
        result["failure_summary"] = truncate_output(output, FAILURE_TAIL_LINES)

    return result


# ---------------------------------------------------------------------------
# Track 2: evidence + fallback helpers
# ---------------------------------------------------------------------------

def _evidence_split(surface_evidence: dict[str, str]) -> dict[str, list[str]]:
    """``{path: class}`` -> ``{confirmed: [...], heuristic: [...], unverified: [...]}``."""
    out: dict[str, list[str]] = {
        evidence.CONFIRMED: [],
        evidence.HEURISTIC: [],
        evidence.UNVERIFIED: [],
    }
    for path, cls in surface_evidence.items():
        out.setdefault(cls, []).append(path)
    return {k: sorted(v) for k, v in out.items()}


def _build_verification_list(
    modified_files: Sequence[str],
    surface_evidence: dict[str, str],
    dynamic_hits: dict[str, list[str]],
    graph: parser.Graph,
    inventory_langs: Sequence[str],
    relevant_unparsed: Sequence[str],
) -> list[dict]:
    """Files whose coverage the graph could NOT confirm — needs source/test proof.

    Everything here is scoped to the change or its surface: an unrelated
    Markdown file or a broken example elsewhere in the repo is not a gap.
    """
    indexed = set(graph.path_by_id().values())
    modified_set = set(modified_files)
    items: list[dict] = []

    for path in sorted(relevant_unparsed):
        items.append({
            "path": path,
            "reason": "on the change surface but failed to parse; relations may be missing",
        })
    for lang in inventory_langs:
        items.append({
            "path": f"<language:{lang}>",
            "reason": "inventory-only language on the surface: no relations parsed",
        })

    for mf in sorted(modified_set):
        if mf in dynamic_hits:
            items.append({
                "path": mf,
                "reason": (
                    "dynamic dispatch ("
                    + ", ".join(dynamic_hits[mf])
                    + "); static graph cannot see all dependents"
                ),
            })
        elif mf.endswith(".py") and mf not in indexed:
            items.append({"path": mf, "reason": "not indexed by the code graph"})
        elif surface_evidence.get(mf) == evidence.UNVERIFIED:
            items.append({
                "path": mf,
                "reason": (
                    "no static importers, but the package uses dynamic import; "
                    "dependents and their tests may be hidden"
                ),
            })

    for path, cls in sorted(surface_evidence.items()):
        if path in modified_set:
            continue
        if cls == evidence.UNVERIFIED:
            items.append({
                "path": path,
                "reason": "reachable only through unverified import edges",
            })
        elif cls == evidence.HEURISTIC:
            items.append({
                "path": path,
                "reason": "reachable only through heuristic import edges",
            })

    return items[:VERIFICATION_LIST_CAP]


def _collect_dir_fallback_tests(
    repo_root: str | os.PathLike[str],
    modified_files: Sequence[str],
) -> list[str]:
    """Every ``test_*.py`` / ``*_test.py`` under a ``tests/`` dir that plausibly
    covers a modified file's package. The middle rung of the fallback ladder:
    wider than convention matching, narrower than the whole suite.
    """
    root = Path(repo_root)
    test_dirs: set[Path] = set()
    for mf in modified_files:
        parts = [p for p in mf.replace(os.sep, "/").split("/") if p]
        for i in range(len(parts)):
            prefix = root.joinpath(*parts[:i]) if i else root
            for name in ("tests", "test"):
                test_dirs.add(prefix / name)
        test_dirs.add(root / "tests")
        test_dirs.add(root / "test")

    out: set[str] = set()
    for d in test_dirs:
        if not d.is_dir():
            continue
        for f in d.rglob("*.py"):
            try:
                rel = f.relative_to(root).as_posix()
            except ValueError:
                continue
            if matcher.looks_like_test_file(rel):
                out.add(rel)
    return sorted(out)


# ---------------------------------------------------------------------------
# full pipeline
# ---------------------------------------------------------------------------

def resolve_affected_tests(
    repo_root: str,
    repo_id: str,
    *,
    db,
    modified_files: Sequence[str] | None = None,
    graph: parser.Graph | None = None,
    max_depth: int = 0,
    worktree: bool = True,
    progress: ProgressFn | None = None,
) -> dict:
    """Compute the import surface, the tests that cover it, and how much of that
    the code graph could actually confirm.

    Returns ``{modified_files, import_surface, affected_tests, source,
    surface_evidence, selection_class, confidence, analysis_completeness,
    verification_required}``. ``source`` records which mechanism (``db`` /
    ``convention``) produced the test list; ``selection_class`` is the weakest
    evidence class on the path from a change to the selected tests.
    """
    if graph is None:
        graph = parser.build_graph(repo_root, worktree=worktree)
    if modified_files is None:
        modified_files = parser.get_modified_files(repo_root)
    modified_files = sorted(set(modified_files))

    import_surface = parser.get_modified_import_surface(
        modified_files=modified_files, graph=graph, max_depth=max_depth
    )
    surface_evidence = parser.get_modified_import_surface_classified(
        modified_files=modified_files, graph=graph, max_depth=max_depth
    )

    # Dynamic-dispatch scan: the modified files plus the rest of their top-level
    # packages, so a registry/plugin loader elsewhere in the package is seen even
    # when the edit itself looks innocuous.
    indexed_paths = set(graph.path_by_id().values())
    mod_pkgs = {m.split("/", 1)[0] for m in modified_files if "/" in m}
    scan_targets = set(modified_files) | {
        p for p in indexed_paths
        if p.endswith(".py") and p.split("/", 1)[0] in mod_pkgs
    }
    dynamic_hits = evidence.scan_dynamic_dispatch(
        repo_root, sorted(scan_targets)[:_DYNAMIC_SCAN_CAP]
    )
    repo_has_dynamic = bool(dynamic_hits)

    imported_by = graph.imported_by_edges()
    for mf in modified_files:
        if not mf.endswith(".py"):
            continue
        if mf in dynamic_hits:
            # The change itself is in dynamic-dispatch code: its blast radius
            # cannot be trusted.
            surface_evidence[mf] = evidence.UNVERIFIED
        elif (
            mf in indexed_paths
            and not imported_by.get(mf)
            and repo_has_dynamic
        ):
            # The graph found no importers, but the package wires things up
            # dynamically — dependents (and their tests) may be hidden.
            surface_evidence[mf] = evidence.UNVERIFIED

    _emit(progress, "surface_detected", {
        "modified_files": modified_files, "import_surface": import_surface,
    })

    _emit(progress, "db_query", {"status": "querying"})
    db_tests = db.fetch_target_tests(repo_id, import_surface) if import_surface else []
    used = "db"
    tests = list(db_tests)

    if not tests:
        all_tests = [
            p for p in graph.path_by_id().values() if matcher.looks_like_test_file(p)
        ]
        tests = matcher.match_surface(import_surface, all_tests)
        used = "convention"

    affected = sorted(set(tests))

    # Scope the snapshot-completeness signal to what this change actually
    # touches: an inventory-only README or an unparsed vendored blob elsewhere
    # in the repo is not a gap in *this* analysis.
    lang_by_path = {n.path: n.language for n in graph.files.values()}
    relevant_paths = set(modified_files) | set(import_surface)
    relevant_langs = {lang_by_path.get(p) for p in relevant_paths}
    inv_langs = [
        lang for lang in graph.inventory_only_languages() if lang in relevant_langs
    ]
    relevant_unparsed = [p for p in graph.unparsed_files if p in relevant_paths]

    # Weakest link from any change to the selected tests.
    classes = [surface_evidence.get(p, evidence.HEURISTIC) for p in import_surface]
    selection_class = evidence.weakest(classes) if classes else evidence.CONFIRMED
    if used == "convention":
        selection_class = evidence.weakest([selection_class, evidence.HEURISTIC])
    # Partial-ness only matters when it touches THIS change's surface.
    if relevant_unparsed or inv_langs:
        selection_class = evidence.weakest([selection_class, evidence.UNVERIFIED])
    confidence = evidence.confidence_label(selection_class)

    completeness = graph.analysis_completeness()
    # Effective, surface-scoped level: an unparsed file or inventory-only
    # language elsewhere in the repo does not degrade THIS analysis.
    completeness["level"] = (
        "partial" if relevant_unparsed
        else "degraded" if inv_langs
        else "ok"
    )
    completeness["inventory_only_languages"] = inv_langs
    completeness["unparsed_files"] = sorted(relevant_unparsed)
    completeness["repo_parse_failures"] = len(graph.partial_failures)
    completeness["dynamic_dispatch_hits"] = [
        {"path": p, "markers": m} for p, m in sorted(dynamic_hits.items())
    ]
    verification_required = _build_verification_list(
        modified_files, surface_evidence, dynamic_hits, graph, inv_langs,
        relevant_unparsed,
    )

    _emit(progress, "db_query", {
        "status": "complete", "affected_tests": affected, "source": used,
        "confidence": confidence,
    })

    return {
        "modified_files": modified_files,
        "import_surface": import_surface,
        "affected_tests": affected,
        "source": used,
        "surface_evidence": surface_evidence,
        "selection_class": selection_class,
        "confidence": confidence,
        "analysis_completeness": completeness,
        "verification_required": verification_required,
    }


def run_impact_analysis(
    repo_root: str = ".",
    repo_id: str = "main-repo",
    *,
    db=None,
    modified_files: Sequence[str] | None = None,
    max_depth: int = 0,
    worktree: bool = True,
    dry_run: bool = False,
    timeout: float | None = None,
    update_status: bool = True,
    fallback: str = DEFAULT_FALLBACK,
    progress: ProgressFn | None = None,
) -> dict:
    """git diff → import surface → DB / convention → pytest → lean result.

    ``fallback`` controls the safety ladder when the graph could not fully
    confirm the selection (Track 2):

    * ``off``          — never widen; the original lean behaviour.
    * ``report-only``  — annotate ``confidence`` / ``verification_required`` but
      run only the graph's selection.
    * ``dir`` (default) — widen to the ``tests/`` dirs of the modified packages.
    * ``full``         — widen to the whole pytest suite.

    A ``PASSED`` run whose selection the graph rated ``low`` confidence is
    reported as ``PASSED_UNVERIFIED``, never a plain ``PASSED``.

    ``progress`` receives, in order: ``blast_started``, ``surface_detected``,
    ``db_query`` (querying then complete), and ``test_result``.
    """
    from .db import get_db

    if fallback not in FALLBACK_MODES:
        raise ValueError(f"unknown fallback mode: {fallback!r}")

    own_db = db is None
    db = db or get_db()
    started = time.monotonic()
    try:
        graph = parser.build_graph(repo_root, worktree=worktree)
        if modified_files is None:
            modified_files = parser.get_modified_files(repo_root)
        modified_files = sorted(set(modified_files))
        _emit(progress, "blast_started", {
            "repo_id": repo_id, "modified_files": modified_files,
        })

        analysis = resolve_affected_tests(
            repo_root,
            repo_id,
            db=db,
            modified_files=modified_files,
            graph=graph,
            max_depth=max_depth,
            worktree=worktree,
            progress=progress,
        )

        confidence = analysis["confidence"]
        base = {
            "repo_id": repo_id,
            "repo_root": str(repo_root),
            "modified_files": analysis["modified_files"],
            "import_surface": analysis["import_surface"],
            "affected_tests": analysis["affected_tests"],
            "test_source": analysis["source"],
            "confidence": confidence,
            "evidence": _evidence_split(analysis["surface_evidence"]),
            "analysis_completeness": analysis["analysis_completeness"],
            "verification_required": analysis["verification_required"],
        }
        fallback_level = (
            _LEVEL_CONVENTION if analysis["source"] == "convention" else _LEVEL_DB
        )

        if not analysis["modified_files"]:
            outcome = {**base, "status": STATUS_NO_CHANGES,
                       "execution_time_seconds": 0.0, "fallback_level": fallback_level,
                       "target_tests_executed": [], "summary": "no modified files"}
            _emit(progress, "test_result", {"status": outcome["status"],
                                            "result": outcome})
            return outcome

        has_py = any(m.endswith(".py") for m in analysis["modified_files"])
        low_conf = confidence != "high"
        widen_allowed = fallback in (FALLBACK_DIR, FALLBACK_FULL)
        want_widen = widen_allowed and (low_conf or not analysis["affected_tests"])

        if not analysis["affected_tests"] and not (want_widen and has_py):
            outcome = {**base, "status": STATUS_NO_TESTS,
                       "execution_time_seconds": 0.0, "fallback_level": fallback_level,
                       "target_tests_executed": [], "summary": "no affected tests"}
            _emit(progress, "test_result", {"status": outcome["status"],
                                            "result": outcome})
            return outcome

        tests_to_run = list(analysis["affected_tests"])
        whole_suite = False
        if want_widen and fallback == FALLBACK_FULL:
            whole_suite = True
            fallback_level = _LEVEL_FULL
        elif want_widen and fallback == FALLBACK_DIR:
            dir_tests = _collect_dir_fallback_tests(
                repo_root, analysis["modified_files"]
            )
            widened = sorted(set(tests_to_run) | set(dir_tests))
            if set(widened) != set(tests_to_run):
                fallback_level = _LEVEL_DIR
            tests_to_run = widened

        if not whole_suite and not tests_to_run:
            outcome = {**base, "status": STATUS_NO_TESTS,
                       "execution_time_seconds": 0.0, "fallback_level": fallback_level,
                       "target_tests_executed": [], "summary": "no affected tests"}
            _emit(progress, "test_result", {"status": outcome["status"],
                                            "result": outcome})
            return outcome

        run = run_targeted_tests(
            tests_to_run,
            import_surface=analysis["import_surface"],
            cwd=repo_root,
            timeout=timeout,
            dry_run=dry_run,
            whole_suite=whole_suite,
        )
        merged = {**base, **run}
        merged["fallback_level"] = fallback_level
        merged["execution_time_seconds"] = run["execution_time_seconds"]
        merged["total_time_seconds"] = round(time.monotonic() - started, 3)

        if merged["status"] == STATUS_PASSED and confidence == "low":
            merged["status"] = STATUS_PASSED_UNVERIFIED
            merged["summary"] = (
                (merged.get("summary") or "")
                + " — selection unverified by the graph; see verification_required"
            ).strip(" —")

        if update_status and run.get("status") in (STATUS_PASSED, STATUS_FAILED):
            for test_file in run["target_tests_executed"]:
                if test_file == "<whole suite>":
                    continue
                try:
                    db.update_execution_status(repo_id, test_file, run["status"])
                except Exception:  # pragma: no cover - status is best-effort
                    pass

        _emit(progress, "test_result", {"status": merged["status"],
                                        "result": merged})
        return merged
    finally:
        if own_db:
            db.close()


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def format_lean_output(result: dict) -> str:
    """Pretty JSON for the CLI."""
    return json.dumps(result, indent=2, sort_keys=True)


def format_agent_payload(result: dict) -> str:
    """Single-line string for the MCP tool response.

    Compact JSON, with any failure blob clipped so it cannot flood the agent's
    context — the whole point of the tool.
    """
    payload = dict(result)
    blob = payload.get("failure_summary")
    if isinstance(blob, str) and len(blob) > AGENT_FAILURE_CHARS:
        payload["failure_summary"] = blob[-AGENT_FAILURE_CHARS:]
        payload["failure_summary_truncated"] = True
    vr = payload.get("verification_required")
    if isinstance(vr, list) and len(vr) > AGENT_VERIFICATION_ITEMS:
        payload["verification_required"] = vr[:AGENT_VERIFICATION_ITEMS]
        payload["verification_required_truncated"] = True
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
