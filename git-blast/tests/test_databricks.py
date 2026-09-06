"""Stage 3 coverage: DatabricksDB SQL/param shape against a fake connection."""

import pytest

from src.db import DatabricksDB, get_db


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._result: list = []

    def execute(self, sql, params=None):
        self._conn.calls.append((" ".join(sql.split()), params or {}))
        self._result = self._conn.next_rows
        self._conn.next_rows = []

    def fetchall(self):
        return self._result

    @property
    def rowcount(self):
        return -1

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.calls: list = []
        self.next_rows: list = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "x.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-xxx")


@pytest.fixture
def fake_db(creds):
    conn = FakeConnection()
    db = DatabricksDB(connection_factory=lambda: conn)
    db._fake_conn = conn
    return db


def test_get_db_returns_databricks_when_configured(creds):
    # No connection is opened at construction, so this must not raise.
    db = get_db()
    assert isinstance(db, DatabricksDB)


def test_missing_http_path_raises(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "x.cloud.databricks.com")
    monkeypatch.delenv("DATABRICKS_HTTP_PATH", raising=False)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="DATABRICKS_HTTP_PATH"):
        DatabricksDB()


def test_rejects_non_identifier_catalog(creds):
    with pytest.raises(ValueError):
        DatabricksDB(catalog="dev; DROP TABLE x")


def test_fetch_target_tests_builds_in_clause(fake_db):
    fake_db._fake_conn.next_rows = [("tests/test_a.py",), ("tests/test_b.py",)]
    out = fake_db.fetch_target_tests("repo", ["src/a.py", "src/b.py", "src/a.py"])
    assert out == ["tests/test_a.py", "tests/test_b.py"]

    sql, params = fake_db._fake_conn.calls[-1]
    assert "dev_catalog.git_blast.module_test_map" in sql
    assert "source_file IN (%(s0)s,%(s1)s)" in sql
    assert params == {"repo_id": "repo", "s0": "src/a.py", "s1": "src/b.py"}


def test_fetch_target_tests_empty_surface_no_query(fake_db):
    assert fake_db.fetch_target_tests("repo", []) == []
    assert fake_db._fake_conn.calls == []


def test_upsert_mapping_emits_merge(fake_db):
    fake_db.upsert_mapping("repo", "src/a.py", "tests/test_a.py", imported_symbol="f")
    sql, params = fake_db._fake_conn.calls[-1]
    assert sql.startswith("MERGE INTO dev_catalog.git_blast.module_test_map")
    assert "WHEN MATCHED THEN UPDATE SET" in sql
    assert "WHEN NOT MATCHED THEN INSERT" in sql
    assert params["source_file"] == "src/a.py"
    assert params["imported_symbol"] == "f"


def test_clear_mappings_counts_then_deletes(fake_db):
    fake_db._fake_conn.next_rows = [(4,)]
    removed = fake_db.clear_mappings("repo")
    assert removed == 4
    kinds = [c[0].split()[0] for c in fake_db._fake_conn.calls]
    assert kinds == ["SELECT", "DELETE"]


def test_log_checkpoint_uses_array_constructor(fake_db):
    fake_db.log_checkpoint("repo", "sha1", "did things", ["a.py", "b.py"])
    sql, params = fake_db._fake_conn.calls[-1]
    assert "array(%(f0)s,%(f1)s)" in sql
    assert params["f0"] == "a.py" and params["f1"] == "b.py"


def test_get_recent_checkpoints_handles_array_and_json(fake_db):
    fake_db._fake_conn.next_rows = [
        ("sha2", "s2", ["x.py", "y.py"], "2026-01-02"),
        ("sha1", "s1", '["z.py"]', "2026-01-01"),
    ]
    out = fake_db.get_recent_checkpoints("repo", limit=5)
    assert out[0]["files_modified"] == ["x.py", "y.py"]
    assert out[1]["files_modified"] == ["z.py"]
    sql, params = fake_db._fake_conn.calls[-1]
    assert "ORDER BY created_at DESC LIMIT %(limit)s" in sql
    assert params["limit"] == 5


def test_close_closes_connection(fake_db):
    fake_db.fetch_target_tests("repo", ["src/a.py"])
    fake_db.close()
    assert fake_db._fake_conn.closed is True
