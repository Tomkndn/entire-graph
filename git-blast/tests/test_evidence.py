"""Track 2 coverage: evidence classification + partial-analysis detection.

The graph is evidence, not an oracle. These tests pin down when a relation is a
``confirmed`` structural fact, when it is only ``heuristic``, and when it is
``unverified`` (the snapshot itself is partial, or the trail runs into dynamic
dispatch / generated code / an inventory-only language).
"""

import json

import pytest

from src import evidence, parser
from src.evidence import CONFIRMED, HEURISTIC, UNVERIFIED

REPO_KEY = "gh/acme/proj"


def fid(path: str) -> str:
    return f"{REPO_KEY}:file:{path}"


def ndjson(*objs: dict) -> str:
    return "\n".join(json.dumps(o) for o in objs)


def header(**extra) -> dict:
    base = {
        "schema_version": "1.1",
        "provider": "entire-graph",
        "repo_root": "/abs/proj",
        "repo_key": REPO_KEY,
        "commit": "deadbeef",
        "schema_features": ["relation_resolution", "relation_evidence"],
    }
    base.update(extra)
    return base


def file_rec(path: str, language: str = "Python") -> dict:
    return {"record_type": "file", "id": fid(path), "path": path, "language": language}


def imports_rec(src: str, dst: str, **fields) -> dict:
    rec = {
        "record_type": "relation",
        "type": "IMPORTS",
        "from_id": fid(src),
        "to_id": fid(dst),
        "confidence": 1.0,
        "resolution": "exact",
        "relation_scope": "module",
        "target_kind": "file",
        "warning_codes": [],
    }
    rec.update(fields)
    return rec


def summary_rec(**fields) -> dict:
    rec = {
        "record_type": "summary",
        "partial_failures": [],
        "warnings": [],
        "language_tiers": {"Python": "semantic"},
        "stats": {"completeness_level": "ok"},
    }
    rec.update(fields)
    return rec


# --- classify_relation ---------------------------------------------------

@pytest.mark.parametrize(
    "rel, expected",
    [
        ({"resolution": "exact", "confidence": 1.0, "warning_codes": []}, CONFIRMED),
        ({"resolution": "exact", "confidence": 0.95, "warning_codes": []}, CONFIRMED),
        # healthy internal Python imports: import_resolved @ 0.88-0.95 -> confirmed
        ({"resolution": "import_resolved", "confidence": 0.95, "warning_codes": []}, CONFIRMED),
        ({"resolution": "import_resolved", "confidence": 0.88, "warning_codes": []}, CONFIRMED),
        ({"resolution": "exact", "confidence": 0.7, "warning_codes": []}, HEURISTIC),
        ({"resolution": "name_only", "confidence": 0.88, "warning_codes": []}, HEURISTIC),
        ({"resolution": "import_resolved", "confidence": 0.7, "warning_codes": []}, HEURISTIC),
        ({"resolution": "exact", "confidence": 1.0, "warning_codes": ["W_X"]}, HEURISTIC),
        ({"resolution": "exact", "confidence": 0.3, "warning_codes": []}, UNVERIFIED),
        ({"resolution": None, "confidence": None, "warning_codes": []}, HEURISTIC),
    ],
)
def test_classify_relation_grades(rel, expected):
    assert evidence.classify_relation(rel) == expected


def test_classify_relation_inventory_only_is_unverified():
    rel = {"resolution": "exact", "confidence": 1.0, "warning_codes": []}
    assert (
        evidence.classify_relation(rel, language_tier="inventory-only") == UNVERIFIED
    )


def test_classify_relation_partial_snapshot_forces_unverified():
    rel = {"resolution": "exact", "confidence": 1.0, "warning_codes": []}
    assert evidence.classify_relation(rel, snapshot_partial=True) == UNVERIFIED


def test_classify_relation_without_schema_support_is_heuristic():
    rel = {"resolution": "exact", "confidence": 1.0, "warning_codes": []}
    assert evidence.classify_relation(rel, resolution_supported=False) == HEURISTIC


def test_weakest_and_is_weaker():
    assert evidence.weakest([]) == CONFIRMED
    assert evidence.weakest([CONFIRMED, HEURISTIC]) == HEURISTIC
    assert evidence.weakest([HEURISTIC, UNVERIFIED, CONFIRMED]) == UNVERIFIED
    assert evidence.is_weaker(HEURISTIC, CONFIRMED)
    assert not evidence.is_weaker(CONFIRMED, UNVERIFIED)


def test_confidence_label():
    assert evidence.confidence_label(CONFIRMED) == "high"
    assert evidence.confidence_label(HEURISTIC) == "medium"
    assert evidence.confidence_label(UNVERIFIED) == "low"


# --- dynamic-dispatch scan --------------------------------------------

def test_scan_dynamic_dispatch_flags_markers(tmp_path):
    (tmp_path / "dyn.py").write_text(
        "import importlib\n"
        "def load(name):\n"
        "    mod = importlib.import_module(name)\n"
        "    return getattr(mod, 'run')\n"
    )
    (tmp_path / "plain.py").write_text("def add(a, b):\n    return a + b\n")
    hits = evidence.scan_dynamic_dispatch(
        str(tmp_path), ["dyn.py", "plain.py", "missing.py"]
    )
    assert "plain.py" not in hits
    assert "missing.py" not in hits
    assert set(hits["dyn.py"]) >= {"getattr", "importlib"}


# --- parser wiring ----------------------------------------------------

def test_parser_grades_edges_and_keeps_provider_fields():
    graph = parser.parse_snapshot_ndjson(
        ndjson(
            header(),
            file_rec("src/a.py"),
            file_rec("src/b.py"),
            file_rec("src/c.py"),
            imports_rec("src/a.py", "src/b.py"),  # exact / 1.0 -> confirmed
            imports_rec(
                "src/a.py", "src/c.py", resolution="name_only", confidence=0.8
            ),  # -> heuristic
            summary_rec(),
        )
    )
    assert graph.edge_class[("src/a.py", "src/b.py")] == CONFIRMED
    assert graph.edge_class[("src/a.py", "src/c.py")] == HEURISTIC
    by_target = {r["to_id"]: r for r in graph.relations}
    assert by_target[fid("src/b.py")]["evidence_class"] == CONFIRMED
    assert by_target[fid("src/c.py")]["resolution"] == "name_only"


def test_parser_reads_partial_failures_and_tiers():
    graph = parser.parse_snapshot_ndjson(
        ndjson(
            header(),
            file_rec("src/a.py"),
            file_rec("gen/big.py"),
            file_rec("notes.md", "Markdown"),
            summary_rec(
                partial_failures=[{"path": "gen/big.py", "code": "W_PARSE"}],
                language_tiers={"Python": "semantic", "Markdown": "inventory-only"},
                stats={"completeness_level": "degraded"},
            ),
        )
    )
    assert graph.snapshot_partial() is True
    assert graph.unparsed_files == ["gen/big.py"]
    assert graph.files[fid("gen/big.py")].parsed is False
    assert graph.inventory_only_languages() == ["Markdown"]
    comp = graph.analysis_completeness()
    assert comp["level"] == "partial"
    assert comp["inventory_only_languages"] == ["Markdown"]


def test_edge_into_unparsed_file_is_unverified_but_others_are_not():
    # A parse failure downgrades only the edges that touch the unparsed file;
    # unrelated resolved edges keep their class (no repo-wide poisoning).
    graph = parser.parse_snapshot_ndjson(
        ndjson(
            header(),
            file_rec("src/a.py"),
            file_rec("src/b.py"),
            file_rec("src/broken.py"),
            imports_rec("src/a.py", "src/broken.py", resolution="import_resolved",
                        confidence=0.95),
            imports_rec("src/a.py", "src/b.py", resolution="import_resolved",
                        confidence=0.95),
            summary_rec(
                partial_failures=[{"path": "src/broken.py", "code": "E_PARSE_ERROR"}],
                stats={"completeness_level": "degraded"},
            ),
        )
    )
    assert graph.edge_class[("src/a.py", "src/broken.py")] == UNVERIFIED
    assert graph.edge_class[("src/a.py", "src/b.py")] == CONFIRMED


def test_unrelated_repo_parse_failure_does_not_poison_edges():
    # Flask's real case: one broken example .sql file, hundreds of clean
    # import_resolved Python edges -> those must stay confirmed.
    graph = parser.parse_snapshot_ndjson(
        ndjson(
            header(),
            file_rec("src/a.py"),
            file_rec("src/b.py"),
            file_rec("examples/x/schema.sql", "SQL"),
            imports_rec("src/a.py", "src/b.py", resolution="import_resolved",
                        confidence=0.9),
            summary_rec(
                partial_failures=[
                    {"path": "examples/x/schema.sql", "code": "E_PARSE_ERROR"}
                ],
                language_tiers={"Python": "semantic", "SQL": "semantic"},
                stats={"completeness_level": "degraded"},
            ),
        )
    )
    assert graph.edge_class[("src/a.py", "src/b.py")] == CONFIRMED


def test_degraded_level_alone_does_not_force_unverified():
    # "degraded" is normal for any polyglot repo (inventory-only languages);
    # a resolved Python edge stays confirmed.
    graph = parser.parse_snapshot_ndjson(
        ndjson(
            header(),
            file_rec("src/a.py"),
            file_rec("src/b.py"),
            imports_rec("src/a.py", "src/b.py", resolution="import_resolved", confidence=0.9),
            summary_rec(stats={"completeness_level": "degraded"}),
        )
    )
    assert graph.snapshot_partial() is False
    assert graph.edge_class[("src/a.py", "src/b.py")] == CONFIRMED
    assert graph.analysis_completeness()["level"] == "degraded"


def test_classified_surface_keeps_weakest_link():
    # d <- c <- b <- a ; the c<-b edge is heuristic, so b and a are heuristic.
    graph = parser.parse_snapshot_ndjson(
        ndjson(
            header(),
            file_rec("a.py"),
            file_rec("b.py"),
            file_rec("c.py"),
            file_rec("d.py"),
            imports_rec("c.py", "d.py"),
            imports_rec("b.py", "c.py", resolution="name_only", confidence=0.8),
            imports_rec("a.py", "b.py"),
            summary_rec(),
        )
    )
    classified = parser.reverse_import_surface_classified(
        ["d.py"], graph.imported_by_edges()
    )
    assert classified["d.py"] == CONFIRMED
    assert classified["c.py"] == CONFIRMED
    assert classified["b.py"] == HEURISTIC
    assert classified["a.py"] == HEURISTIC


def test_classified_surface_unindexed_modified_file_is_unverified():
    graph = parser.parse_snapshot_ndjson(
        ndjson(header(), file_rec("a.py"), summary_rec())
    )
    classified = parser.get_modified_import_surface_classified(
        modified_files=["a.py", "brand/new.py"], graph=graph
    )
    assert classified["a.py"] == CONFIRMED
    assert classified["brand/new.py"] == UNVERIFIED
