"""Database layer for Git-Blast Live.

Two interchangeable backends behind one protocol (SPEC.md "db.py"):

* ``SQLiteDB``     — local SQLite, tables created from ``schema.sql``.
* ``DatabricksDB`` — same interface, queries
  ``dev_catalog.git_blast.module_test_map`` via ``databricks-sql-connector``.

``get_db()`` returns ``DatabricksDB`` when the ``DATABRICKS_SERVER_HOSTNAME``
environment variable is set, otherwise ``SQLiteDB``.

Every row is scoped by ``repo_id`` so a single store can serve many repos, and
every SQL statement uses parameter placeholders — never f-string interpolation.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

DEFAULT_SQLITE_PATH = "git_blast.db"

# Databricks target used by the Delta-backed deployment.
DATABRICKS_CATALOG = "dev_catalog"
DATABRICKS_SCHEMA = "git_blast"


@dataclass
class Checkpoint:
    """One row of ``checkpoint_logs`` (SPEC.md "Checkpoint Recovery")."""

    checkpoint_sha: str
    prompt_summary: str
    files_modified: list[str] = field(default_factory=list)
    created_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "checkpoint_sha": self.checkpoint_sha,
            "prompt_summary": self.prompt_summary,
            "files_modified": list(self.files_modified),
            "created_at": self.created_at,
        }


@runtime_checkable
class TestMapDB(Protocol):
    """The interface both backends implement."""

    def fetch_target_tests(self, repo_id: str, import_surface: Iterable[str]) -> list[str]:
        """Return the test files that cover any file in ``import_surface``."""

    def upsert_mapping(
        self,
        repo_id: str,
        source_file: str,
        test_file: str,
        imported_symbol: str | None = None,
        last_execution_status: str | None = None,
    ) -> None:
        """Insert or update one source→test mapping row."""

    def all_mappings(self, repo_id: str) -> list[dict]:
        """Return every mapping row for ``repo_id``."""

    def clear_mappings(self, repo_id: str) -> int:
        """Delete every mapping row for ``repo_id``; return the row count removed."""

    def update_execution_status(
        self, repo_id: str, test_file: str, status: str
    ) -> None:
        """Record the outcome of the most recent run of ``test_file``."""

    def log_checkpoint(
        self,
        repo_id: str,
        checkpoint_sha: str,
        prompt_summary: str,
        files_modified: Iterable[str],
    ) -> None:
        """Append one checkpoint row."""

    def get_recent_checkpoints(self, repo_id: str, limit: int = 5) -> list[dict]:
        """Return up to ``limit`` most-recent checkpoints, newest first."""

    def close(self) -> None:
        """Release any held resources."""


# Assigned after class creation so it is not treated as a protocol member;
# stops pytest from trying to collect the imported name as a test class.
TestMapDB.__test__ = False


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


class SQLiteDB:
    """Local SQLite-backed :class:`TestMapDB`.

    A ``:memory:`` path keeps a single shared connection (so the data survives
    between calls within a process); any other path opens a short-lived
    connection per operation, which keeps the store safe to share across the
    FastAPI server's worker threads.
    """

    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_SQLITE_PATH):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._shared: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self._shared = self._new_connection()
        self._ensure_schema()

    # -- connection plumbing ------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    class _Borrowed:
        def __init__(self, db: "SQLiteDB"):
            self._db = db
            self._conn: sqlite3.Connection | None = None

        def __enter__(self) -> sqlite3.Connection:
            self._db._lock.acquire()
            if self._db._shared is not None:
                self._conn = self._db._shared
            else:
                self._conn = self._db._new_connection()
            return self._conn

        def __exit__(self, exc_type, exc, tb):
            try:
                if self._conn is not None:
                    if exc_type is None:
                        self._conn.commit()
                    else:
                        self._conn.rollback()
                    if self._db._shared is None:
                        self._conn.close()
            finally:
                self._db._lock.release()

    def _connect(self) -> "SQLiteDB._Borrowed":
        return SQLiteDB._Borrowed(self)

    def _ensure_schema(self) -> None:
        ddl = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._connect() as conn:
            conn.executescript(ddl)

    # -- mappings ---------------------------------------------------------

    def fetch_target_tests(self, repo_id: str, import_surface: Iterable[str]) -> list[str]:
        surface = [p for p in dict.fromkeys(import_surface) if p]
        if not surface:
            return []
        query = (
            "SELECT DISTINCT test_file FROM module_test_map "
            "WHERE repo_id = ? AND source_file IN (" + _placeholders(len(surface)) + ") "
            "ORDER BY test_file"
        )
        with self._connect() as conn:
            rows = conn.execute(query, [repo_id, *surface]).fetchall()
        return [r["test_file"] for r in rows]

    def upsert_mapping(
        self,
        repo_id: str,
        source_file: str,
        test_file: str,
        imported_symbol: str | None = None,
        last_execution_status: str | None = None,
    ) -> None:
        # The spec's DDL declares no unique constraint, and SQLite treats NULLs
        # as distinct in unique indexes, so `ON CONFLICT` cannot dedupe rows
        # with a NULL imported_symbol. Update-then-insert keeps re-seeding
        # idempotent. `IS` is null-safe equality in SQLite.
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE module_test_map SET "
                "last_execution_status = COALESCE(?, last_execution_status), "
                "last_updated = CURRENT_TIMESTAMP "
                "WHERE repo_id = ? AND source_file = ? AND test_file = ? "
                "AND imported_symbol IS ?",
                (last_execution_status, repo_id, source_file, test_file, imported_symbol),
            )
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO module_test_map "
                    "(repo_id, source_file, imported_symbol, test_file, last_execution_status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (repo_id, source_file, imported_symbol, test_file, last_execution_status),
                )

    def all_mappings(self, repo_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT repo_id, source_file, imported_symbol, test_file, "
                "last_execution_status, last_updated FROM module_test_map "
                "WHERE repo_id = ? ORDER BY source_file, test_file",
                (repo_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_mappings(self, repo_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM module_test_map WHERE repo_id = ?", (repo_id,)
            )
            return cur.rowcount

    def update_execution_status(self, repo_id: str, test_file: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE module_test_map SET last_execution_status = ?, "
                "last_updated = CURRENT_TIMESTAMP "
                "WHERE repo_id = ? AND test_file = ?",
                (status, repo_id, test_file),
            )

    # -- checkpoints ----------------------------------------------------

    def log_checkpoint(
        self,
        repo_id: str,
        checkpoint_sha: str,
        prompt_summary: str,
        files_modified: Iterable[str],
    ) -> None:
        payload = json.dumps(list(files_modified))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoint_logs "
                "(repo_id, checkpoint_sha, prompt_summary, files_modified) "
                "VALUES (?, ?, ?, ?)",
                (repo_id, checkpoint_sha, prompt_summary, payload),
            )

    def get_recent_checkpoints(self, repo_id: str, limit: int = 5) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT checkpoint_sha, prompt_summary, files_modified, created_at "
                "FROM checkpoint_logs WHERE repo_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (repo_id, int(limit)),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            out.append(
                Checkpoint(
                    checkpoint_sha=r["checkpoint_sha"],
                    prompt_summary=r["prompt_summary"] or "",
                    files_modified=_loads_list(r["files_modified"]),
                    created_at=r["created_at"],
                ).as_dict()
            )
        return out

    def close(self) -> None:
        with self._lock:
            if self._shared is not None:
                self._shared.close()
                self._shared = None


def _loads_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return []


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DatabricksDB:
    """Delta Lake-backed :class:`TestMapDB` (SPEC.md "Next Stage").

    Same interface as :class:`SQLiteDB`, backed by
    ``<catalog>.<schema>.module_test_map`` / ``checkpoint_logs`` over
    ``databricks-sql-connector``. Every value is passed as a ``%(name)s``
    parameter; only the trusted catalog/schema config is interpolated into
    table identifiers, and it is validated as a bare identifier first.

    Not exercised against a live SQL Warehouse in this repo — a fake
    ``connection_factory`` is injected in tests.
    """

    def __init__(
        self,
        server_hostname: str | None = None,
        http_path: str | None = None,
        access_token: str | None = None,
        catalog: str = DATABRICKS_CATALOG,
        schema: str = DATABRICKS_SCHEMA,
        *,
        connection_factory=None,
    ):
        self.server_hostname = server_hostname or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
        self.http_path = http_path or os.environ.get("DATABRICKS_HTTP_PATH")
        self.access_token = access_token or os.environ.get("DATABRICKS_TOKEN")
        self.catalog = catalog
        self.schema = schema
        self._lock = threading.Lock()
        self._conn = None
        self._connection_factory = connection_factory

        missing = [
            name
            for name, value in (
                ("DATABRICKS_SERVER_HOSTNAME", self.server_hostname),
                ("DATABRICKS_HTTP_PATH", self.http_path),
                ("DATABRICKS_TOKEN", self.access_token),
            )
            if not value
        ]
        if missing:
            raise RuntimeError("DatabricksDB requires " + ", ".join(missing))
        if not (_IDENT_RE.match(self.catalog) and _IDENT_RE.match(self.schema)):
            raise ValueError("Databricks catalog/schema must be bare identifiers")

    # -- identifiers --------------------------------------------------

    @property
    def _map_table(self) -> str:
        return f"{self.catalog}.{self.schema}.module_test_map"

    @property
    def _checkpoint_table(self) -> str:
        return f"{self.catalog}.{self.schema}.checkpoint_logs"

    # -- connection plumbing ---------------------------------------

    def _connect(self):
        if self._conn is not None:
            return self._conn
        if self._connection_factory is not None:
            self._conn = self._connection_factory()
            return self._conn
        try:
            from databricks import sql as databricks_sql  # type: ignore
        except ImportError as exc:  # pragma: no cover - env dependent
            raise RuntimeError(
                "databricks-sql-connector is not installed; "
                "`pip install git-blast[databricks]`"
            ) from exc
        self._conn = databricks_sql.connect(
            server_hostname=self.server_hostname,
            http_path=self.http_path,
            access_token=self.access_token,
        )
        return self._conn

    def _execute(self, sql: str, params: dict | None = None, *, fetch: bool = False):
        with self._lock:
            conn = self._connect()
            cur = conn.cursor()
            try:
                cur.execute(sql, params or {})
                rows = cur.fetchall() if fetch else None
                rowcount = getattr(cur, "rowcount", -1)
            finally:
                cur.close()
        return rows, rowcount

    # -- mappings ---------------------------------------------------

    def fetch_target_tests(self, repo_id: str, import_surface: Iterable[str]) -> list[str]:
        surface = [p for p in dict.fromkeys(import_surface) if p]
        if not surface:
            return []
        keys = {f"s{i}": path for i, path in enumerate(surface)}
        placeholders = ",".join(f"%({k})s" for k in keys)
        sql = (
            f"SELECT DISTINCT test_file FROM {self._map_table} "
            f"WHERE repo_id = %(repo_id)s AND source_file IN ({placeholders}) "
            "ORDER BY test_file"
        )
        rows, _ = self._execute(sql, {"repo_id": repo_id, **keys}, fetch=True)
        return [_row_value(r, 0, "test_file") for r in rows or []]

    def upsert_mapping(
        self,
        repo_id: str,
        source_file: str,
        test_file: str,
        imported_symbol: str | None = None,
        last_execution_status: str | None = None,
    ) -> None:
        sql = (
            f"MERGE INTO {self._map_table} AS t "
            "USING (SELECT %(repo_id)s AS repo_id, %(source_file)s AS source_file, "
            "%(imported_symbol)s AS imported_symbol, %(test_file)s AS test_file) AS s "
            "ON t.repo_id = s.repo_id AND t.source_file = s.source_file "
            "AND t.test_file = s.test_file "
            "AND (t.imported_symbol = s.imported_symbol "
            "OR (t.imported_symbol IS NULL AND s.imported_symbol IS NULL)) "
            "WHEN MATCHED THEN UPDATE SET "
            "last_execution_status = COALESCE(%(status)s, t.last_execution_status), "
            "last_updated = current_timestamp() "
            "WHEN NOT MATCHED THEN INSERT "
            "(repo_id, source_file, imported_symbol, test_file, last_execution_status, last_updated) "
            "VALUES (s.repo_id, s.source_file, s.imported_symbol, s.test_file, %(status)s, current_timestamp())"
        )
        self._execute(
            sql,
            {
                "repo_id": repo_id,
                "source_file": source_file,
                "imported_symbol": imported_symbol,
                "test_file": test_file,
                "status": last_execution_status,
            },
        )

    def all_mappings(self, repo_id: str) -> list[dict]:
        sql = (
            "SELECT repo_id, source_file, imported_symbol, test_file, "
            f"last_execution_status, last_updated FROM {self._map_table} "
            "WHERE repo_id = %(repo_id)s ORDER BY source_file, test_file"
        )
        rows, _ = self._execute(sql, {"repo_id": repo_id}, fetch=True)
        cols = ["repo_id", "source_file", "imported_symbol", "test_file",
                "last_execution_status", "last_updated"]
        return [{c: _row_value(r, i, c) for i, c in enumerate(cols)} for r in rows or []]

    def clear_mappings(self, repo_id: str) -> int:
        count_rows, _ = self._execute(
            f"SELECT COUNT(*) FROM {self._map_table} WHERE repo_id = %(repo_id)s",
            {"repo_id": repo_id},
            fetch=True,
        )
        removed = int(_row_value(count_rows[0], 0, "count")) if count_rows else 0
        self._execute(
            f"DELETE FROM {self._map_table} WHERE repo_id = %(repo_id)s",
            {"repo_id": repo_id},
        )
        return removed

    def update_execution_status(self, repo_id: str, test_file: str, status: str) -> None:
        self._execute(
            f"UPDATE {self._map_table} SET last_execution_status = %(status)s, "
            "last_updated = current_timestamp() "
            "WHERE repo_id = %(repo_id)s AND test_file = %(test_file)s",
            {"status": status, "repo_id": repo_id, "test_file": test_file},
        )

    # -- checkpoints ---------------------------------------------

    def log_checkpoint(
        self,
        repo_id: str,
        checkpoint_sha: str,
        prompt_summary: str,
        files_modified: Iterable[str],
    ) -> None:
        files = list(files_modified)
        keys = {f"f{i}": v for i, v in enumerate(files)}
        array_expr = (
            "array(" + ",".join(f"%({k})s" for k in keys) + ")" if keys else "array()"
        )
        self._execute(
            f"INSERT INTO {self._checkpoint_table} "
            "(repo_id, checkpoint_sha, prompt_summary, files_modified, created_at) "
            f"VALUES (%(repo_id)s, %(sha)s, %(summary)s, {array_expr}, current_timestamp())",
            {
                "repo_id": repo_id,
                "sha": checkpoint_sha,
                "summary": prompt_summary,
                **keys,
            },
        )

    def get_recent_checkpoints(self, repo_id: str, limit: int = 5) -> list[dict]:
        rows, _ = self._execute(
            "SELECT checkpoint_sha, prompt_summary, files_modified, created_at "
            f"FROM {self._checkpoint_table} WHERE repo_id = %(repo_id)s "
            "ORDER BY created_at DESC LIMIT %(limit)s",
            {"repo_id": repo_id, "limit": int(limit)},
            fetch=True,
        )
        out: list[dict] = []
        for r in rows or []:
            files = _row_value(r, 2, "files_modified")
            if isinstance(files, str):
                files = _loads_list(files)
            out.append(
                Checkpoint(
                    checkpoint_sha=_row_value(r, 0, "checkpoint_sha"),
                    prompt_summary=_row_value(r, 1, "prompt_summary") or "",
                    files_modified=list(files or []),
                    created_at=str(_row_value(r, 3, "created_at")),
                ).as_dict()
            )
        return out

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None


def _row_value(row, index: int, name: str):
    """Read a column from a DB-API row that may be a tuple or a mapping."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(name)
    try:
        return row[name]  # sqlite3.Row / named-tuple style
    except (TypeError, KeyError, IndexError):
        pass
    try:
        return row[index]
    except (TypeError, KeyError, IndexError):
        return None


def _databricks_configured() -> bool:
    return bool(os.environ.get("DATABRICKS_SERVER_HOSTNAME"))


def get_db(db_path: str | os.PathLike[str] | None = None) -> TestMapDB:
    """Return the configured backend.

    Databricks when ``DATABRICKS_SERVER_HOSTNAME`` is set, else SQLite at
    ``db_path`` / ``$GIT_BLAST_DB`` / ``git_blast.db``.
    """
    if _databricks_configured():
        return DatabricksDB()
    resolved = db_path or os.environ.get("GIT_BLAST_DB") or DEFAULT_SQLITE_PATH
    return SQLiteDB(resolved)


# ---------------------------------------------------------------------------
# Seeding (SPEC.md "db.py" — auto_seed / demo data)
# ---------------------------------------------------------------------------

DEMO_REPO_PATH = Path(__file__).resolve().parent.parent / "demo_repo"

# Used only when `entire graph snapshot` cannot run for the bundled demo repo.
_DEMO_FALLBACK_MAPPINGS = (
    ("src/config.py", "tests/test_config.py", None),
    ("src/jwt.py", "tests/test_jwt.py", None),
    ("src/config.py", "tests/test_jwt.py", None),
)


def _source_like(path: str) -> bool:
    from . import matcher

    return path.endswith(".py") and not matcher.looks_like_test_file(path)


def auto_seed(
    repo_id: str,
    repo_root: str | os.PathLike[str],
    *,
    db: TestMapDB | None = None,
    worktree: bool = True,
    entire_bin: str | None = None,
    replace: bool = True,
) -> dict:
    """Populate ``module_test_map`` for ``repo_id`` from the code graph.

    Combines three signals (SPEC.md): ``TESTS`` relations, transitive
    ``IMPORTS`` reachability from each test file, and convention matching.
    Re-runnable — with ``replace`` (default) it clears the repo's rows first so
    CI can keep the map in sync on every merge.
    """
    from . import matcher, parser

    own_db = db is None
    db = db or get_db()
    kwargs = {"worktree": worktree}
    if entire_bin is not None:
        kwargs["entire_bin"] = entire_bin
    graph = parser.build_graph(str(repo_root), **kwargs)

    all_paths = sorted(graph.path_by_id().values())
    test_files = [p for p in all_paths if matcher.looks_like_test_file(p)]
    source_files = [p for p in all_paths if _source_like(p)]

    # (source_file, test_file) -> imported_symbol (first non-null wins)
    mappings: dict[tuple[str, str], str | None] = {}
    from_tests = from_imports = from_convention = 0

    def record(src: str, test: str, symbol: str | None, bucket: str) -> None:
        nonlocal from_tests, from_imports, from_convention
        key = (src, test)
        if key not in mappings:
            mappings[key] = symbol
        elif symbol and not mappings[key]:
            mappings[key] = symbol
        if bucket == "tests":
            from_tests += 1
        elif bucket == "imports":
            from_imports += 1
        else:
            from_convention += 1

    # 1. TESTS relations (symbol-accurate).
    for edge in graph.test_edges:
        src, test = edge["source_file"], edge["test_file"]
        if matcher.looks_like_test_file(src) or not matcher.looks_like_test_file(test):
            continue
        record(src, test, edge.get("source_symbol"), "tests")

    # 2. Transitive IMPORTS reachability from each test file.
    for test in test_files:
        for src in graph.forward_reachable_paths(test):
            if _source_like(src):
                record(src, test, None, "imports")

    # 3. Convention matching for any remaining source file.
    for src in source_files:
        for test in matcher.match_tests(src, test_files):
            record(src, test, None, "convention")

    if replace:
        db.clear_mappings(repo_id)
    for (src, test), symbol in sorted(mappings.items()):
        db.upsert_mapping(repo_id, src, test, imported_symbol=symbol)

    if own_db:
        db.close()

    return {
        "repo_id": repo_id,
        "repo_root": str(repo_root),
        "mappings": len(mappings),
        "source_files": len({s for s, _ in mappings}),
        "test_files": len({t for _, t in mappings}),
        "from_tests_relations": from_tests,
        "from_imports": from_imports,
        "from_convention": from_convention,
    }


def seed_demo_data(
    db: TestMapDB | None = None,
    repo_id: str = "demo_repo",
    demo_root: str | os.PathLike[str] | None = None,
) -> dict:
    """Seed the bundled two-module demo repo (SPEC.md "Tested Against").

    Runs :func:`auto_seed` against ``demo_repo/``; if the graph snapshot cannot
    run, falls back to the three known demo mappings so the dashboard still has
    something to show.
    """
    from . import parser

    root = Path(demo_root) if demo_root is not None else DEMO_REPO_PATH
    own_db = db is None
    db = db or get_db()
    try:
        summary = auto_seed(repo_id, root, db=db)
        summary["mode"] = "auto_seed"
        return summary
    except parser.SnapshotError:
        db.clear_mappings(repo_id)
        for src, test, symbol in _DEMO_FALLBACK_MAPPINGS:
            db.upsert_mapping(repo_id, src, test, imported_symbol=symbol)
        return {
            "repo_id": repo_id,
            "repo_root": str(root),
            "mappings": len(_DEMO_FALLBACK_MAPPINGS),
            "source_files": len({m[0] for m in _DEMO_FALLBACK_MAPPINGS}),
            "test_files": len({m[1] for m in _DEMO_FALLBACK_MAPPINGS}),
            "mode": "fallback",
        }
    finally:
        if own_db:
            db.close()
