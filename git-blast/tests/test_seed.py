"""Stage 3 coverage: auto_seed() and seed_demo_data()."""

import json
import shutil

import pytest

from src import db as db_mod
from src import parser
from src.db import SQLiteDB, auto_seed, seed_demo_data

REPO_KEY = "gh/acme/proj"


def _fid(path):
    return f"{REPO_KEY}:file:{path}"


def _sym(path, name):
    return f"{REPO_KEY}:Python:{path}:function:{name}"


def _synthetic_snapshot():
    records = [
        {"schema_version": "1.1", "repo_key": REPO_KEY, "repo_root": "/x",
         "commit": "c0"},
        {"record_type": "file", "id": _fid("src/__init__.py"), "path": "src/__init__.py",
         "language": "Python"},
        {"record_type": "file", "id": _fid("src/config.py"), "path": "src/config.py",
         "language": "Python"},
        {"record_type": "file", "id": _fid("src/jwt.py"), "path": "src/jwt.py",
         "language": "Python"},
        {"record_type": "file", "id": _fid("tests/test_config.py"),
         "path": "tests/test_config.py", "language": "Python"},
        {"record_type": "file", "id": _fid("tests/test_jwt.py"),
         "path": "tests/test_jwt.py", "language": "Python"},
        {"record_type": "relation", "type": "IMPORTS",
         "from_id": _fid("src/jwt.py"), "to_id": _fid("src/config.py")},
        {"record_type": "relation", "type": "IMPORTS",
         "from_id": _fid("tests/test_jwt.py"), "to_id": _fid("src/jwt.py")},
        {"record_type": "relation", "type": "IMPORTS",
         "from_id": _fid("tests/test_config.py"), "to_id": _fid("src/config.py")},
        {"record_type": "relation", "type": "TESTS",
         "from_id": _sym("tests/test_jwt.py", "test_roundtrip"),
         "to_id": _sym("src/jwt.py", "encode"),
         "evidence": [{"file_path": "tests/test_jwt.py"}]},
    ]
    return "\n".join(json.dumps(r) for r in records)


@pytest.fixture
def synthetic_graph(monkeypatch):
    monkeypatch.setattr(
        parser, "_run_snapshot", lambda *a, **k: _synthetic_snapshot()
    )


@pytest.fixture
def store(tmp_path):
    database = SQLiteDB(tmp_path / "seed.db")
    yield database
    database.close()


def test_auto_seed_builds_expected_mappings(synthetic_graph, store):
    summary = auto_seed("proj", "/whatever", db=store)

    assert summary["mappings"] == 3
    assert summary["source_files"] == 2
    assert summary["test_files"] == 2
    assert summary["from_imports"] >= 3
    assert summary["from_tests_relations"] == 1

    pairs = {
        (m["source_file"], m["test_file"]) for m in store.all_mappings("proj")
    }
    assert pairs == {
        ("src/config.py", "tests/test_config.py"),
        ("src/jwt.py", "tests/test_jwt.py"),
        ("src/config.py", "tests/test_jwt.py"),
    }


def test_auto_seed_records_symbol_from_tests_relation(synthetic_graph, store):
    auto_seed("proj", "/whatever", db=store)
    jwt_map = next(
        m for m in store.all_mappings("proj")
        if m["source_file"] == "src/jwt.py"
    )
    assert jwt_map["imported_symbol"] == "encode"


def test_auto_seed_ignores_init_and_test_files_as_sources(synthetic_graph, store):
    auto_seed("proj", "/whatever", db=store)
    sources = {m["source_file"] for m in store.all_mappings("proj")}
    assert "src/__init__.py" not in sources
    assert not any(s.startswith("tests/") for s in sources)


def test_auto_seed_replace_clears_stale_rows(synthetic_graph, store):
    store.upsert_mapping("proj", "src/deleted.py", "tests/test_deleted.py")
    auto_seed("proj", "/whatever", db=store)
    sources = {m["source_file"] for m in store.all_mappings("proj")}
    assert "src/deleted.py" not in sources


def test_auto_seed_repo_isolation(synthetic_graph, store):
    store.upsert_mapping("other", "keep.py", "tests/test_keep.py")
    auto_seed("proj", "/whatever", db=store)
    assert store.all_mappings("other") == [
        m for m in store.all_mappings("other")
        if m["source_file"] == "keep.py"
    ]
    assert len(store.all_mappings("other")) == 1


def test_seed_demo_data_falls_back_when_snapshot_unavailable(monkeypatch, store):
    def boom(*a, **k):
        raise parser.SnapshotError("no entire binary")

    monkeypatch.setattr(db_mod, "auto_seed", boom)
    summary = seed_demo_data(db=store, repo_id="demo_repo")

    assert summary["mode"] == "fallback"
    assert summary["mappings"] == 3
    pairs = {
        (m["source_file"], m["test_file"]) for m in store.all_mappings("demo_repo")
    }
    assert pairs == {
        ("src/config.py", "tests/test_config.py"),
        ("src/jwt.py", "tests/test_jwt.py"),
        ("src/config.py", "tests/test_jwt.py"),
    }


@pytest.mark.skipif(shutil.which("entire") is None, reason="entire CLI not installed")
def test_seed_demo_data_live(store):
    summary = seed_demo_data(db=store, repo_id="demo_live")
    assert summary["mode"] == "auto_seed"
    assert summary["mappings"] == 3
    assert summary["source_files"] == 2
    assert summary["test_files"] == 2
