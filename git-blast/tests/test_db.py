"""Stage 1 coverage: SQLite backend, repo isolation, checkpoints, get_db()."""

import importlib

import pytest

from src import db as db_mod
from src.db import SQLiteDB, TestMapDB, get_db


@pytest.fixture
def store(tmp_path):
    database = SQLiteDB(tmp_path / "git_blast.db")
    yield database
    database.close()


def test_sqlitedb_satisfies_protocol(store):
    assert isinstance(store, TestMapDB)


def test_schema_is_created(store):
    with store._connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"module_test_map", "checkpoint_logs"} <= tables


def test_upsert_is_idempotent(store):
    for _ in range(3):
        store.upsert_mapping("repo", "src/a.py", "tests/test_a.py")
    rows = store.all_mappings("repo")
    assert len(rows) == 1
    assert rows[0]["source_file"] == "src/a.py"
    assert rows[0]["test_file"] == "tests/test_a.py"


def test_upsert_distinguishes_imported_symbol(store):
    store.upsert_mapping("repo", "src/a.py", "tests/test_a.py", imported_symbol=None)
    store.upsert_mapping("repo", "src/a.py", "tests/test_a.py", imported_symbol="foo")
    assert len(store.all_mappings("repo")) == 2


def test_fetch_target_tests_dedupes_and_sorts(store):
    store.upsert_mapping("repo", "src/a.py", "tests/test_b.py")
    store.upsert_mapping("repo", "src/b.py", "tests/test_b.py")
    store.upsert_mapping("repo", "src/b.py", "tests/test_a.py")
    tests = store.fetch_target_tests("repo", ["src/a.py", "src/b.py", "src/a.py"])
    assert tests == ["tests/test_a.py", "tests/test_b.py"]


def test_fetch_target_tests_empty_surface(store):
    store.upsert_mapping("repo", "src/a.py", "tests/test_a.py")
    assert store.fetch_target_tests("repo", []) == []


def test_repo_isolation(store):
    store.upsert_mapping("repo-1", "src/a.py", "tests/test_a.py")
    store.upsert_mapping("repo-2", "src/a.py", "tests/test_z.py")
    assert store.fetch_target_tests("repo-1", ["src/a.py"]) == ["tests/test_a.py"]
    assert store.fetch_target_tests("repo-2", ["src/a.py"]) == ["tests/test_z.py"]
    assert store.all_mappings("repo-1") != store.all_mappings("repo-2")


def test_clear_mappings_scoped_to_repo(store):
    store.upsert_mapping("repo-1", "src/a.py", "tests/test_a.py")
    store.upsert_mapping("repo-2", "src/a.py", "tests/test_a.py")
    removed = store.clear_mappings("repo-1")
    assert removed == 1
    assert store.all_mappings("repo-1") == []
    assert len(store.all_mappings("repo-2")) == 1


def test_update_execution_status(store):
    store.upsert_mapping("repo", "src/a.py", "tests/test_a.py")
    store.update_execution_status("repo", "tests/test_a.py", "PASSED")
    assert store.all_mappings("repo")[0]["last_execution_status"] == "PASSED"


def test_checkpoints_recent_first_with_limit(store):
    for i in range(7):
        store.log_checkpoint("repo", f"sha{i}", f"summary {i}", [f"file{i}.py"])
    recent = store.get_recent_checkpoints("repo", limit=5)
    assert len(recent) == 5
    assert [c["checkpoint_sha"] for c in recent] == ["sha6", "sha5", "sha4", "sha3", "sha2"]
    assert recent[0]["files_modified"] == ["file6.py"]


def test_checkpoints_scoped_to_repo(store):
    store.log_checkpoint("repo-1", "a", "s", [])
    store.log_checkpoint("repo-2", "b", "s", [])
    assert [c["checkpoint_sha"] for c in store.get_recent_checkpoints("repo-1")] == ["a"]


def test_memory_backend_persists_within_process():
    database = SQLiteDB(":memory:")
    try:
        database.upsert_mapping("repo", "src/a.py", "tests/test_a.py")
        assert database.fetch_target_tests("repo", ["src/a.py"]) == ["tests/test_a.py"]
    finally:
        database.close()


def test_get_db_defaults_to_sqlite(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABRICKS_SERVER_HOSTNAME", raising=False)
    monkeypatch.setenv("GIT_BLAST_DB", str(tmp_path / "from_env.db"))
    database = get_db()
    try:
        assert isinstance(database, SQLiteDB)
        assert database.db_path == str(tmp_path / "from_env.db")
    finally:
        database.close()


def test_get_db_explicit_path_wins_over_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABRICKS_SERVER_HOSTNAME", raising=False)
    monkeypatch.setenv("GIT_BLAST_DB", str(tmp_path / "env.db"))
    database = get_db(tmp_path / "explicit.db")
    try:
        assert database.db_path == str(tmp_path / "explicit.db")
    finally:
        database.close()


def test_get_db_selects_databricks_when_configured(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "example.cloud.databricks.com")
    monkeypatch.delenv("DATABRICKS_HTTP_PATH", raising=False)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    # Selected, but not yet usable — must fail loudly, never fall back to SQLite.
    with pytest.raises(RuntimeError, match="DATABRICKS_HTTP_PATH"):
        get_db()


def test_databricks_not_implemented_with_full_credentials(monkeypatch):
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "example.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/abc")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-xxx")
    with pytest.raises(NotImplementedError):
        get_db()


def test_module_reimport_is_clean():
    importlib.reload(db_mod)
