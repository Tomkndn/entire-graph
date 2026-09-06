"""Track 2 end-to-end: the ``demo_repo_partial`` fixture (dynamic dispatch).

``src/core.py`` reaches ``src/handlers/*`` only through ``importlib`` +
``getattr`` in ``src/registry.py``. The code graph therefore has no edge to the
handler modules. These tests pin the two behaviours that matter:

* editing a dynamically-reached handler must NOT be reported as a clean pass —
  it is ``PASSED_UNVERIFIED`` with the file named in ``verification_required``,
  and a fallback still runs its test;
* editing a fully-resolved file (``src/core.py``) must behave exactly as before:
  ``high`` confidence, no escalation, a plain ``PASSED``.
"""

import json
from pathlib import Path

import pytest

from src import parser
from src.db import SQLiteDB
from src.runner import run_impact_analysis

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "demo_repo_partial"
SNAPSHOT_NDJSON = (
    Path(__file__).resolve().parent / "fixtures" / "partial_repo.ndjson"
).read_text()


@pytest.fixture
def snapshot(monkeypatch):
    monkeypatch.setattr(parser, "_run_snapshot", lambda *a, **k: SNAPSHOT_NDJSON)


def _blast(modified, *, mappings=None, **kw):
    db = SQLiteDB(":memory:")
    try:
        for src, test in mappings or []:
            db.upsert_mapping("partial", src, test)
        return run_impact_analysis(
            str(FIXTURE_DIR), "partial", db=db, modified_files=modified, **kw
        )
    finally:
        db.close()


# --- the fixture actually represents incomplete analysis ---------------

def test_fixture_snapshot_has_no_static_edge_to_handlers():
    graph = parser.parse_snapshot_ndjson(SNAPSHOT_NDJSON)
    ib = graph.imported_by_paths()
    assert ib.get("src/handlers/pdf.py", set()) == set()
    assert ib.get("src/handlers/csv.py", set()) == set()
    # the resolvable part is still resolved
    assert "src/core.py" in ib["src/registry.py"]


# --- editing a dynamically-reached handler ---------------------------

def test_edit_dynamic_handler_is_unverified(snapshot):
    result = _blast(["src/handlers/pdf.py"])

    assert result["confidence"] == "low"
    assert "src/handlers/pdf.py" in result["evidence"]["unverified"]
    assert any(
        item["path"] == "src/handlers/pdf.py"
        for item in result["verification_required"]
    )
    # a fallback still gets the covering test executed
    assert "tests/test_pdf_handler.py" in result["target_tests_executed"]
    # and the pass is explicitly not presented as a confirmed pass
    assert result["status"] == "PASSED_UNVERIFIED"


def test_report_only_mode_flags_but_does_not_widen(snapshot):
    result = _blast(["src/handlers/pdf.py"], fallback="report-only")
    assert result["confidence"] == "low"
    assert result["fallback_level"] in (0, 1)
    assert "<whole suite>" not in result["target_tests_executed"]


# --- editing fully-resolved code: unchanged behaviour (R4) ----------

def test_edit_resolved_core_stays_high_confidence(snapshot):
    # the normal workflow: `git-blast seed` has populated the map first.
    result = _blast(
        ["src/core.py"],
        mappings=[
            ("src/core.py", "tests/test_core.py"),
            ("src/core.py", "tests/test_pdf_handler.py"),
        ],
    )

    assert result["confidence"] == "high"
    assert result["fallback_level"] == 0
    assert result["status"] == "PASSED"
    assert result["verification_required"] == []
    assert result["analysis_completeness"]["level"] == "ok"
