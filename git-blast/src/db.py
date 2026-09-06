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


class DatabricksDB:
    """Delta Lake-backed :class:`TestMapDB` (SPEC.md "Next Stage").

    Wraps ``databricks-sql-connector``. Fully populated in a later stage; the
    constructor already validates that the connector and credentials are
    present so a misconfigured environment fails loudly rather than silently
    falling back to SQLite.
    """

    def __init__(
        self,
        server_hostname: str | None = None,
        http_path: str | None = None,
        access_token: str | None = None,
        catalog: str = DATABRICKS_CATALOG,
        schema: str = DATABRICKS_SCHEMA,
    ):
        self.server_hostname = server_hostname or os.environ.get("DATABRICKS_SERVER_HOSTNAME")
        self.http_path = http_path or os.environ.get("DATABRICKS_HTTP_PATH")
        self.access_token = access_token or os.environ.get("DATABRICKS_TOKEN")
        self.catalog = catalog
        self.schema = schema
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
            raise RuntimeError(
                "DatabricksDB requires " + ", ".join(missing)
            )
        raise NotImplementedError(
            "DatabricksDB query implementation is added in a later stage; "
            "unset DATABRICKS_SERVER_HOSTNAME to use the local SQLite backend."
        )


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
