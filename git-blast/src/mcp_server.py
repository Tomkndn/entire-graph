"""MCP server exposing Git-Blast to coding agents (SPEC.md "mcp_server.py").

One tool, ``git_blast_test(repo_id)``, built on ``MCPServer`` (MCP v2). It runs
the full pipeline — import surface → DB lookup → convention fallback → pytest —
and returns the lean single-line agent payload so the agent's context stays
small.

Run it over stdio::

    python -m src.mcp_server
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

from . import runner

# Resolved once at import so it is the tool's schema default (SPEC env table).
DEFAULT_REPO_ID = os.environ.get("GIT_BLAST_REPO_ID", "main-repo")
DEFAULT_FALLBACK = os.environ.get("GIT_BLAST_FALLBACK", runner.DEFAULT_FALLBACK)


def _repo_root() -> str:
    return os.environ.get("GIT_BLAST_REPO_ROOT", ".")


def run_git_blast_test(
    repo_id: str | None = None,
    *,
    repo_root: str | None = None,
    db=None,
    max_depth: int = 0,
    dry_run: bool = False,
    fallback: str | None = None,
) -> str:
    """Core of the tool, separated so it is testable without an MCP client."""
    result = runner.run_impact_analysis(
        repo_root or _repo_root(),
        repo_id or DEFAULT_REPO_ID,
        db=db,
        max_depth=max_depth,
        dry_run=dry_run,
        fallback=fallback or DEFAULT_FALLBACK,
    )
    return runner.format_agent_payload(result)


mcp = MCPServer("git-blast")


@mcp.tool()
def git_blast_test(
    repo_id: str = DEFAULT_REPO_ID, fallback: str = DEFAULT_FALLBACK
) -> str:
    """Run only the tests affected by the current working-tree changes.

    Detects the modified files in the target repo, traces the reverse import
    graph to every file that could be affected, looks up the tests that cover
    that surface (falling back to naming conventions), runs just those with
    pytest, and returns a compact JSON string.

    The payload also grades how much the code graph could actually confirm, so
    you can tell evidence apart:

    * ``confidence``            — ``high`` (structural), ``medium`` (heuristic),
      ``low`` (unverified: dynamic dispatch, generated code, partial snapshot).
    * ``evidence``              — surface files split into ``confirmed`` /
      ``heuristic`` / ``unverified``.
    * ``verification_required`` — files whose coverage the graph could NOT
      confirm; check these by reading source or adding a test.
    * ``analysis_completeness`` — parse failures, inventory-only languages,
      dynamic-dispatch hits.
    * ``fallback_level``        — 0 db, 1 convention, 2 tests/ dir, 3 whole suite.

    A ``low``-confidence pass is reported as ``PASSED_UNVERIFIED``, never a
    plain ``PASSED``. ``fallback`` picks the widening policy: ``off`` /
    ``report-only`` / ``dir`` (default) / ``full``.
    """
    return run_git_blast_test(repo_id, fallback=fallback)


def main() -> None:  # pragma: no cover - process entry point
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
