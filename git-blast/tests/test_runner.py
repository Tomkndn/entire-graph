"""Stage 4 coverage: targeted pytest runner + full blast pipeline."""

import json

import pytest

from src import parser, runner
from src.db import SQLiteDB
from src.runner import (
    _filter_test_files,
    _find_venv_python,
    format_agent_payload,
    format_lean_output,
    resolve_affected_tests,
    run_impact_analysis,
    run_targeted_tests,
    truncate_output,
)

REPO_KEY = "gh/acme/proj"


def _fid(path):
    return f"{REPO_KEY}:file:{path}"


def _snapshot(*extra):
    base = [
        {"schema_version": "1.1", "repo_key": REPO_KEY},
        {"record_type": "file", "id": _fid("src/a.py"), "path": "src/a.py",
         "language": "Python"},
        {"record_type": "file", "id": _fid("tests/test_a.py"),
         "path": "tests/test_a.py", "language": "Python"},
        {"record_type": "relation", "type": "IMPORTS",
         "from_id": _fid("tests/test_a.py"), "to_id": _fid("src/a.py")},
    ]
    return "\n".join(json.dumps(r) for r in [*base, *extra])


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "a.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "test_a.py").write_text(
        "from pathlib import Path\n\n\ndef test_ok():\n    assert 1 + 1 == 2\n"
    )
    return tmp_path


# --- helpers ----------------------------------------------------------

def test_filter_test_files_keeps_only_existing_tests(project):
    (project / "tests" / "helper.py").write_text("x = 1\n")
    out = _filter_test_files(
        ["tests/test_a.py", "tests/test_a.py", "src/a.py", "tests/missing_test.py",
         "tests/helper.py"],
        project,
    )
    assert out == ["tests/helper.py", "tests/test_a.py"]


def test_find_venv_python(tmp_path):
    assert _find_venv_python(tmp_path) is None
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n")
    assert _find_venv_python(tmp_path) == str(venv_bin / "python")


def test_truncate_output_adds_notice():
    text = "\n".join(str(i) for i in range(100))
    out = truncate_output(text, 10)
    assert out.startswith("... (output truncated to last 10 lines) ...")
    assert out.strip().endswith("99")
    assert truncate_output("a\nb", 10) == "a\nb"


# --- run_targeted_tests --------------------------------------------

def test_no_tests_when_nothing_matches(project):
    result = run_targeted_tests(["src/a.py"], ["src/a.py"], cwd=project)
    assert result["status"] == "NO_TESTS"
    assert result["target_tests_executed"] == []


def test_dry_run_does_not_execute(project):
    result = run_targeted_tests(["tests/test_a.py"], ["src/a.py"], cwd=project,
                                dry_run=True)
    assert result["status"] == "DRY_RUN"
    assert result["target_tests_executed"] == ["tests/test_a.py"]
    assert result["returncode"] is None
    assert result["command"].startswith("pytest -q --tb=short --no-header")


def test_passed_run(project):
    result = run_targeted_tests(["tests/test_a.py"], ["src/a.py"], cwd=project)
    assert result["status"] == "PASSED"
    assert result["returncode"] == 0
    assert result["target_tests_executed"] == ["tests/test_a.py"]
    assert "passed" in result["summary"]
    assert "failure_summary" not in result
    assert result["execution_time_seconds"] >= 0.0


def test_failed_run_carries_failure_summary(project):
    (project / "tests" / "test_a.py").write_text("def test_bad():\n    assert False\n")
    result = run_targeted_tests(["tests/test_a.py"], ["src/a.py"], cwd=project)
    assert result["status"] == "FAILED"
    assert result["returncode"] == 1
    assert "failure_summary" in result
    assert "test_bad" in result["failure_summary"]


# --- formatting ---------------------------------------------------

def test_format_lean_output_is_pretty_json():
    text = format_lean_output({"status": "PASSED", "b": 1, "a": 2})
    assert "\n" in text
    parsed = json.loads(text)
    assert parsed["status"] == "PASSED"


def test_format_agent_payload_is_single_line_and_clips():
    result = {"status": "FAILED", "failure_summary": "x" * 5000}
    line = format_agent_payload(result)
    assert "\n" not in line
    parsed = json.loads(line)
    assert len(parsed["failure_summary"]) == 600
    assert parsed["failure_summary_truncated"] is True


# --- resolve_affected_tests ------------------------------------

def test_resolve_prefers_db(monkeypatch):
    graph = parser.parse_snapshot_ndjson(_snapshot())

    class FakeDB:
        def fetch_target_tests(self, repo_id, surface):
            return ["tests/test_a.py"]

    out = resolve_affected_tests(
        ".", "repo", db=FakeDB(), modified_files=["src/a.py"], graph=graph
    )
    assert out["source"] == "db"
    assert out["affected_tests"] == ["tests/test_a.py"]
    assert "src/a.py" in out["import_surface"]


def test_resolve_falls_back_to_convention(monkeypatch):
    graph = parser.parse_snapshot_ndjson(_snapshot())

    class EmptyDB:
        def fetch_target_tests(self, repo_id, surface):
            return []

    out = resolve_affected_tests(
        ".", "repo", db=EmptyDB(), modified_files=["src/a.py"], graph=graph
    )
    assert out["source"] == "convention"
    assert out["affected_tests"] == ["tests/test_a.py"]


# --- run_impact_analysis -------------------------------------

def test_pipeline_no_changes(project, monkeypatch):
    monkeypatch.setattr(parser, "_run_snapshot", lambda *a, **k: _snapshot())
    db = SQLiteDB(":memory:")
    try:
        result = run_impact_analysis(
            str(project), "repo", db=db, modified_files=[]
        )
    finally:
        db.close()
    assert result["status"] == "NO_CHANGES"


def test_pipeline_end_to_end_passed(project, monkeypatch):
    monkeypatch.setattr(parser, "_run_snapshot", lambda *a, **k: _snapshot())
    db = SQLiteDB(":memory:")
    db.upsert_mapping("repo", "src/a.py", "tests/test_a.py")
    try:
        result = run_impact_analysis(
            str(project), "repo", db=db, modified_files=["src/a.py"]
        )
    finally:
        db.close()
    assert result["status"] == "PASSED"
    assert result["target_tests_executed"] == ["tests/test_a.py"]
    assert result["test_source"] == "db"
    assert "total_time_seconds" in result


def test_pipeline_records_execution_status(project, monkeypatch):
    monkeypatch.setattr(parser, "_run_snapshot", lambda *a, **k: _snapshot())
    db = SQLiteDB(":memory:")
    db.upsert_mapping("repo", "src/a.py", "tests/test_a.py")
    try:
        run_impact_analysis(str(project), "repo", db=db, modified_files=["src/a.py"])
        assert db.all_mappings("repo")[0]["last_execution_status"] == "PASSED"
    finally:
        db.close()


def test_pipeline_no_tests_when_map_and_convention_empty(project, monkeypatch):
    monkeypatch.setattr(
        parser, "_run_snapshot",
        lambda *a, **k: "\n".join([
            json.dumps({"schema_version": "1.1", "repo_key": REPO_KEY}),
            json.dumps({"record_type": "file", "id": _fid("src/lonely.py"),
                        "path": "src/lonely.py", "language": "Python"}),
        ]),
    )
    db = SQLiteDB(":memory:")
    try:
        result = run_impact_analysis(
            str(project), "repo", db=db, modified_files=["src/lonely.py"]
        )
    finally:
        db.close()
    assert result["status"] == "NO_TESTS"
