"""Command-line entry point for Git-Blast Live (SPEC.md "CLI Usage").

    python3 -m src.cli seed --auto --repo-root /path/to/repo --repo-id myrepo
    python3 -m src.cli test --repo-root /path/to/repo --repo-id myrepo

Thin: argument parsing plus a call into ``db`` / ``runner``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from . import db as db_mod
from . import runner
from .parser import SnapshotError

DEFAULT_REPO_ID = "main-repo"

# Process exit codes: 0 = fine, 1 = tests failed, 2 = could not run.
_EXIT_OK = 0
_EXIT_TESTS_FAILED = 1
_EXIT_ERROR = 2

_OK_STATUSES = {
    runner.STATUS_PASSED,
    runner.STATUS_PASSED_UNVERIFIED,
    runner.STATUS_NO_CHANGES,
    runner.STATUS_NO_TESTS,
    runner.STATUS_DRY_RUN,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-blast",
        description="Detect what changed, trace what depends on it, test only that.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="populate the module -> test map for a repo")
    seed.add_argument("--auto", action="store_true",
                      help="auto-seed from the repo's code graph (default)")
    seed.add_argument("--demo", action="store_true",
                      help="seed the bundled demo_repo instead of --repo-root")
    seed.add_argument("--repo-root", default=".")
    seed.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    seed.add_argument("--db", default=None, help="SQLite path (overrides $GIT_BLAST_DB)")
    seed.add_argument("--no-replace", action="store_true",
                      help="keep existing rows instead of clearing first")
    seed.set_defaults(func=_cmd_seed)

    test = sub.add_parser("test", help="run the tests affected by the current changes")
    test.add_argument("--repo-root", default=".")
    test.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    test.add_argument("--db", default=None, help="SQLite path (overrides $GIT_BLAST_DB)")
    test.add_argument("--max-depth", type=int, default=0,
                      help="reverse-import BFS depth (0 = unlimited)")
    test.add_argument(
        "--fallback", choices=runner.FALLBACK_MODES, default=runner.DEFAULT_FALLBACK,
        help=(
            "what to do when the graph cannot confirm the selection: "
            "off (never widen) | report-only (flag only) | "
            "dir (widen to the package's tests/ dir, default) | full (whole suite)"
        ),
    )
    test.add_argument("--dry-run", action="store_true",
                      help="resolve affected tests but do not run pytest")
    test.add_argument("--no-worktree", action="store_true",
                      help="snapshot HEAD instead of the working tree")
    test.add_argument("--format", choices=("lean", "agent"), default="lean")
    test.set_defaults(func=_cmd_test)

    return parser


def _cmd_seed(args: argparse.Namespace) -> int:
    database = db_mod.get_db(args.db)
    try:
        if args.demo:
            summary = db_mod.seed_demo_data(db=database, repo_id=args.repo_id)
        else:
            summary = db_mod.auto_seed(
                args.repo_id,
                args.repo_root,
                db=database,
                replace=not args.no_replace,
            )
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_ERROR
    finally:
        database.close()

    print(json.dumps(summary, indent=2, sort_keys=True))
    return _EXIT_OK


def _cmd_test(args: argparse.Namespace) -> int:
    if args.max_depth < 0:
        print("error: --max-depth must be >= 0", file=sys.stderr)
        return _EXIT_ERROR

    database = db_mod.get_db(args.db)
    try:
        result = runner.run_impact_analysis(
            args.repo_root,
            args.repo_id,
            db=database,
            max_depth=args.max_depth,
            worktree=not args.no_worktree,
            dry_run=args.dry_run,
            fallback=args.fallback,
        )
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_ERROR
    finally:
        database.close()

    if args.format == "agent":
        print(runner.format_agent_payload(result))
    else:
        print(runner.format_lean_output(result))

    if result.get("confidence") == "low" or result.get("verification_required"):
        n = len(result.get("verification_required") or [])
        print(
            f"warning: graph confidence {result.get('confidence')!r}; "
            f"{n} item(s) need source/test verification "
            f"(fallback_level={result.get('fallback_level')})",
            file=sys.stderr,
        )

    if result["status"] == runner.STATUS_FAILED:
        return _EXIT_TESTS_FAILED
    if result["status"] in _OK_STATUSES:
        return _EXIT_OK
    return _EXIT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
