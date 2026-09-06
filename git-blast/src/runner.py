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

from . import matcher, parser

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

# Status vocabulary shared across the CLI / MCP / dashboard.
STATUS_NO_CHANGES = "NO_CHANGES"
STATUS_NO_TESTS = "NO_TESTS"
STATUS_DRY_RUN = "DRY_RUN"
STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"
STATUS_ERROR = "ERROR"

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
) -> dict:
    """Run pytest over ``test_files`` and return a lean result dict."""
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

    if not filtered:
        result["summary"] = "no affected test files to run"
        return result

    python = _find_venv_python(cwd) or sys.executable
    cmd = [python, "-m", "pytest", *PYTEST_ARGS, *(extra_args or []), *filtered]
    result["command"] = " ".join(["pytest", *PYTEST_ARGS, *(extra_args or []), *filtered])
    result["target_tests_executed"] = filtered

    if dry_run:
        result["status"] = STATUS_DRY_RUN
        result["summary"] = f"would run {len(filtered)} test file(s)"
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
    """Compute the import surface and the tests that cover it.

    Returns ``{modified_files, import_surface, affected_tests, sources}`` where
    ``sources`` records which mechanism (``db`` / ``convention``) produced the
    test list.
    """
    if graph is None:
        graph = parser.build_graph(repo_root, worktree=worktree)
    if modified_files is None:
        modified_files = parser.get_modified_files(repo_root)
    modified_files = sorted(set(modified_files))

    import_surface = parser.get_modified_import_surface(
        modified_files=modified_files, graph=graph, max_depth=max_depth
    )
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
    _emit(progress, "db_query", {
        "status": "complete", "affected_tests": affected, "source": used,
    })

    return {
        "modified_files": modified_files,
        "import_surface": import_surface,
        "affected_tests": affected,
        "source": used,
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
    progress: ProgressFn | None = None,
) -> dict:
    """git diff → import surface → DB / convention → pytest → lean result.

    ``progress`` receives, in order: ``blast_started``, ``surface_detected``,
    ``db_query`` (querying then complete), and ``test_result``.
    """
    from .db import get_db

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

        base = {
            "repo_id": repo_id,
            "repo_root": str(repo_root),
            "modified_files": analysis["modified_files"],
            "import_surface": analysis["import_surface"],
            "affected_tests": analysis["affected_tests"],
            "test_source": analysis["source"],
        }

        if not analysis["modified_files"]:
            outcome = {**base, "status": STATUS_NO_CHANGES,
                       "execution_time_seconds": 0.0,
                       "target_tests_executed": [], "summary": "no modified files"}
            _emit(progress, "test_result", {"status": outcome["status"],
                                            "result": outcome})
            return outcome

        if not analysis["affected_tests"]:
            outcome = {**base, "status": STATUS_NO_TESTS,
                       "execution_time_seconds": 0.0,
                       "target_tests_executed": [], "summary": "no affected tests"}
            _emit(progress, "test_result", {"status": outcome["status"],
                                            "result": outcome})
            return outcome

        run = run_targeted_tests(
            analysis["affected_tests"],
            import_surface=analysis["import_surface"],
            cwd=repo_root,
            timeout=timeout,
            dry_run=dry_run,
        )
        merged = {**base, **run}
        merged["execution_time_seconds"] = run["execution_time_seconds"]
        merged["total_time_seconds"] = round(time.monotonic() - started, 3)

        if update_status and merged["status"] in (STATUS_PASSED, STATUS_FAILED):
            for test_file in run["target_tests_executed"]:
                try:
                    db.update_execution_status(repo_id, test_file, merged["status"])
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
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
