"""Stage 6 coverage: the MCP `git_blast_test` tool (no live client needed)."""

import asyncio
import json

import pytest

from src import mcp_server, parser
from src.db import SQLiteDB

REPO_KEY = "gh/acme/proj"


def _fid(path):
    return f"{REPO_KEY}:file:{path}"


def _snapshot():
    records = [
        {"schema_version": "1.1", "repo_key": REPO_KEY},
        {"record_type": "file", "id": _fid("src/a.py"), "path": "src/a.py",
         "language": "Python"},
        {"record_type": "file", "id": _fid("tests/test_a.py"),
         "path": "tests/test_a.py", "language": "Python"},
        {"record_type": "relation", "type": "IMPORTS",
         "from_id": _fid("tests/test_a.py"), "to_id": _fid("src/a.py")},
    ]
    return "\n".join(json.dumps(r) for r in records)


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "a.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n")
    monkeypatch.setattr(parser, "_run_snapshot", lambda *a, **k: _snapshot())
    return tmp_path


def test_exposes_exactly_one_tool():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert [t.name for t in tools] == ["git_blast_test"]


def test_tool_schema_has_repo_id_string():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    schema = tools[0].input_schema
    assert schema["properties"]["repo_id"]["type"] == "string"
    assert schema["properties"]["repo_id"]["default"] == mcp_server.DEFAULT_REPO_ID


def test_run_git_blast_test_returns_single_line_payload(project):
    db = SQLiteDB(":memory:")
    db.upsert_mapping("repo", "src/a.py", "tests/test_a.py")
    try:
        payload = mcp_server.run_git_blast_test(
            "repo", repo_root=str(project), db=db
        )
    finally:
        db.close()
    assert "\n" not in payload
    data = json.loads(payload)
    assert data["status"] in {"PASSED", "NO_CHANGES", "NO_TESTS"}


def test_run_git_blast_test_end_to_end_passed(project, monkeypatch):
    db_file = project / "mcp.db"
    seed = SQLiteDB(db_file)
    seed.upsert_mapping("repo", "src/a.py", "tests/test_a.py")
    seed.close()

    monkeypatch.setenv("GIT_BLAST_DB", str(db_file))
    monkeypatch.setenv("GIT_BLAST_REPO_ROOT", str(project))
    # a real modification so the pipeline has something to analyse
    monkeypatch.setattr(
        parser, "get_modified_files", lambda root: ["src/a.py"]
    )

    payload = mcp_server.run_git_blast_test("repo", repo_root=str(project))
    data = json.loads(payload)
    assert data["status"] == "PASSED"
    assert data["target_tests_executed"] == ["tests/test_a.py"]


def test_call_tool_dispatches(project, monkeypatch):
    db_file = project / "mcp.db"
    seed = SQLiteDB(db_file)
    seed.upsert_mapping("repo", "src/a.py", "tests/test_a.py")
    seed.close()
    monkeypatch.setenv("GIT_BLAST_DB", str(db_file))
    monkeypatch.setenv("GIT_BLAST_REPO_ROOT", str(project))
    monkeypatch.setattr(parser, "get_modified_files", lambda root: ["src/a.py"])

    result = asyncio.run(
        mcp_server.mcp.call_tool("git_blast_test", {"repo_id": "repo"})
    )
    assert result.is_error is False
    text = result.content[0].text
    assert json.loads(text)["status"] == "PASSED"
