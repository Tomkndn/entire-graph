"""Stage 7 coverage: FastAPI dashboard backend + WebSocket event stream."""

import json

import pytest
from fastapi.testclient import TestClient

from src import parser, server
from src.db import SQLiteDB
from src.parser import SnapshotError

REPO_KEY = "gh/acme/proj"


def _fid(path):
    return f"{REPO_KEY}:file:{path}"


def _snapshot():
    records = [
        {"schema_version": "1.1", "repo_key": REPO_KEY},
        {"record_type": "file", "id": _fid("src/a.py"), "path": "src/a.py",
         "language": "Python"},
        {"record_type": "file", "id": _fid("src/b.py"), "path": "src/b.py",
         "language": "Python"},
        {"record_type": "file", "id": _fid("tests/test_a.py"),
         "path": "tests/test_a.py", "language": "Python"},
        {"record_type": "relation", "type": "IMPORTS",
         "from_id": _fid("src/a.py"), "to_id": _fid("src/b.py")},
        {"record_type": "relation", "type": "IMPORTS",
         "from_id": _fid("tests/test_a.py"), "to_id": _fid("src/a.py")},
    ]
    return "\n".join(json.dumps(r) for r in records)


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "a.py").write_text("VALUE = 1\n")
    (tmp_path / "src" / "b.py").write_text("BASE = 0\n")
    (tmp_path / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n")
    monkeypatch.setattr(parser, "_run_snapshot", lambda *a, **k: _snapshot())
    monkeypatch.setattr(parser, "get_modified_files", lambda root: ["src/b.py"])
    return tmp_path


@pytest.fixture
def client(project, tmp_path):
    db_file = project / "server.db"
    seed = SQLiteDB(db_file)
    seed.upsert_mapping("repo", "src/a.py", "tests/test_a.py")
    seed.upsert_mapping("repo", "src/b.py", "tests/test_a.py")
    seed.close()
    app = server.create_app(
        repo_root=str(project),
        repo_id="repo",
        db_factory=lambda: SQLiteDB(db_file),
        static_dir=tmp_path / "no-static-build",
    )
    with TestClient(app) as c:
        yield c


def test_status(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["repo_id"] == "repo"
    assert body["connections"] == 0
    assert body["db_backend"] == "SQLiteDB"
    assert body["mappings"] == 2


def test_index_without_static_build(client):
    body = client.get("/").json()
    assert body["name"] == "Git-Blast Live"
    assert "/api/blast" in body["endpoints"]


def test_graph_endpoint(client):
    body = client.get("/api/graph").json()
    ids = {n["id"] for n in body["nodes"]}
    assert {"src/a.py", "src/b.py", "tests/test_a.py"} <= ids
    b_node = next(n for n in body["nodes"] if n["id"] == "src/b.py")
    assert b_node["modified"] is True
    assert body["modified_files"] == ["src/b.py"]
    assert "src/a.py" in body["import_surface"]
    assert {"source", "target"} <= set(body["edges"][0])


def test_serves_static_build_when_present(project, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>Git-Blast Live</title>")
    app = server.create_app(
        repo_root=str(project), repo_id="repo",
        db_factory=lambda: SQLiteDB(project / "s.db"),
        static_dir=static,
    )
    with TestClient(app) as c:
        resp = c.get("/")
    assert resp.status_code == 200
    assert "Git-Blast Live" in resp.text
    assert resp.headers["content-type"].startswith("text/html")


def test_graph_endpoint_snapshot_error(project, monkeypatch):
    def boom(*a, **k):
        raise SnapshotError("no entire binary")

    monkeypatch.setattr(parser, "_run_snapshot", boom)
    app = server.create_app(repo_root=str(project), repo_id="repo",
                            db_factory=lambda: SQLiteDB(project / "x.db"))
    with TestClient(app) as c:
        resp = c.get("/api/graph")
    assert resp.status_code == 503
    assert "error" in resp.json()


def test_blast_streams_events_in_order(client):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"

        resp = client.post("/api/blast")
        assert resp.status_code == 200
        assert resp.json()["status"] == "PASSED"

        events = []
        for _ in range(10):
            msg = ws.receive_json()
            events.append(msg)
            if msg["type"] == "test_result":
                break

    seq = [e["type"] for e in events]
    assert seq[0] == "blast_started"
    assert seq[1] == "surface_detected"
    assert ("db_query", "querying") in [(e["type"], e.get("status")) for e in events]
    assert ("db_query", "complete") in [(e["type"], e.get("status")) for e in events]
    assert seq[-1] == "test_result"

    started = events[0]
    assert started["modified_files"] == ["src/b.py"]
    surface = events[1]
    assert "src/a.py" in surface["import_surface"]
    complete = next(e for e in events if e["type"] == "db_query" and e["status"] == "complete")
    assert complete["affected_tests"] == ["tests/test_a.py"]
    result = events[-1]
    assert result["status"] == "PASSED"
    assert result["result"]["target_tests_executed"] == ["tests/test_a.py"]


def test_connection_count_reflects_ws(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert client.get("/api/status").json()["connections"] == 1
    # after the context exits the client disconnects
    assert client.get("/api/status").json()["connections"] == 0
