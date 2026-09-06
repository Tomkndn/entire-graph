"""Stage 2 coverage: snapshot parsing, git diff, reverse-IMPORTS blast radius."""

import json
import os
import shutil
import subprocess

import pytest

from src import parser
from src.parser import (
    SnapshotError,
    build_graph,
    get_graph_for_dashboard,
    get_modified_files,
    get_modified_import_surface,
    parse_entire_graph_snapshot,
    parse_snapshot_ndjson,
    resolve_path_from_id,
    reverse_import_surface,
)

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
    }
    base.update(extra)
    return base


def file_rec(path: str, language: str = "Python") -> dict:
    return {"record_type": "file", "id": fid(path), "path": path, "language": language}


def imports_rec(src: str, dst: str, *, external: bool = False) -> dict:
    to_id = f"external:import:{dst}" if external else fid(dst)
    return {
        "record_type": "relation",
        "from_id": fid(src),
        "to_id": to_id,
        "type": "IMPORTS",
    }


# --- snapshot parsing ------------------------------------------------------

def test_parse_header_and_files():
    graph = parse_snapshot_ndjson(
        ndjson(header(), file_rec("src/a.py"), file_rec("src/b.py", "Python"))
    )
    assert graph.repo_key == REPO_KEY
    assert graph.repo_root == "/abs/proj"
    assert graph.commit == "deadbeef"
    assert set(graph.path_by_id().values()) == {"src/a.py", "src/b.py"}


def test_external_imports_excluded():
    graph = parse_snapshot_ndjson(
        ndjson(
            header(),
            file_rec("src/a.py"),
            imports_rec("src/a.py", "os", external=True),
            imports_rec("src/a.py", "requests", external=True),
        )
    )
    assert graph.relations == []
    assert graph.files[fid("src/a.py")].imports == set()


def test_non_file_records_ignored_and_no_crash():
    graph = parse_snapshot_ndjson(
        ndjson(
            header(),
            {"record_type": "symbol", "id": f"{REPO_KEY}:Python:src/a.py:function:f",
             "file_path": "src/a.py", "start_line": 1, "end_line": 2},
            file_rec("src/a.py"),
            {"record_type": "external", "id": "external:import:os"},
            {"record_type": "summary", "stats": {"files": 1}},
        )
        + "\nnot json at all\n{bad json"
    )
    assert list(graph.path_by_id().values()) == ["src/a.py"]


def test_imports_and_imported_by_linked():
    graph = parse_snapshot_ndjson(
        ndjson(
            header(),
            file_rec("src/a.py"),
            file_rec("src/b.py"),
            imports_rec("src/a.py", "src/b.py"),
        )
    )
    a, b = graph.files[fid("src/a.py")], graph.files[fid("src/b.py")]
    assert a.imports == {b.id}
    assert b.imported_by == {a.id}
    assert graph.imported_by_paths()["src/b.py"] == {"src/a.py"}


def test_parse_entire_graph_snapshot_dict_shape():
    result = parse_entire_graph_snapshot(
        snapshot_text=ndjson(
            header(),
            file_rec("src/a.py"),
            file_rec("src/b.py"),
            imports_rec("src/a.py", "src/b.py"),
        )
    )
    assert set(result) == {"files", "relations", "repo_root", "repo_key", "commit"}
    entry = result["files"][fid("src/a.py")]
    assert entry["path"] == "src/a.py"
    assert entry["imports"] == ["src/b.py"]
    assert result["files"][fid("src/b.py")]["imported_by"] == ["src/a.py"]


# --- id helpers ----------------------------------------------------------

def test_resolve_path_from_id():
    assert resolve_path_from_id(fid("src/a.py")) == "src/a.py"
    assert (
        resolve_path_from_id(f"{REPO_KEY}:Python:src/a.py:function:verify")
        == "src/a.py"
    )
    assert resolve_path_from_id("external:import:os") is None
    assert resolve_path_from_id("") is None
    assert resolve_path_from_id("weird", {"weird": "mapped/path.py"}) == "mapped/path.py"


# --- reverse-IMPORTS BFS ----------------------------------------------

def chain_graph():
    # c <- b <- a   (a imports b, b imports c)
    return parse_snapshot_ndjson(
        ndjson(
            header(),
            file_rec("a.py"),
            file_rec("b.py"),
            file_rec("c.py"),
            imports_rec("a.py", "b.py"),
            imports_rec("b.py", "c.py"),
        )
    )


def test_reverse_surface_direct_importer():
    g = chain_graph()
    surface = reverse_import_surface(["b.py"], g.imported_by_paths())
    assert set(surface) == {"a.py", "b.py"}


def test_reverse_surface_transitive_unlimited():
    g = chain_graph()
    surface = reverse_import_surface(["c.py"], g.imported_by_paths(), max_depth=0)
    assert set(surface) == {"a.py", "b.py", "c.py"}


def test_reverse_surface_max_depth_1_is_direct_only():
    g = chain_graph()
    surface = reverse_import_surface(["c.py"], g.imported_by_paths(), max_depth=1)
    assert set(surface) == {"b.py", "c.py"}


def test_reverse_surface_max_depth_2():
    g = chain_graph()
    surface = reverse_import_surface(["c.py"], g.imported_by_paths(), max_depth=2)
    assert set(surface) == {"a.py", "b.py", "c.py"}


def test_reverse_surface_cycle_terminates():
    g = parse_snapshot_ndjson(
        ndjson(
            header(),
            file_rec("a.py"),
            file_rec("b.py"),
            imports_rec("a.py", "b.py"),
            imports_rec("b.py", "a.py"),
        )
    )
    surface = reverse_import_surface(["a.py"], g.imported_by_paths())
    assert set(surface) == {"a.py", "b.py"}


def test_reverse_surface_multiple_branches_and_dupes():
    g = parse_snapshot_ndjson(
        ndjson(
            header(),
            file_rec("b.py"),
            file_rec("a.py"),
            file_rec("d.py"),
            imports_rec("a.py", "b.py"),
            imports_rec("d.py", "b.py"),
        )
    )
    surface = reverse_import_surface(["b.py", "b.py"], g.imported_by_paths())
    assert surface == ["a.py", "b.py", "d.py"]


def test_get_modified_import_surface_keeps_unindexed_modified_file():
    g = chain_graph()
    surface = get_modified_import_surface(
        modified_files=["c.py", "brand/new.py"], graph=g
    )
    assert "brand/new.py" in surface
    assert {"a.py", "b.py", "c.py"} <= set(surface)


# --- dashboard projection -------------------------------------------

def test_graph_for_dashboard_shape_and_state():
    g = parse_snapshot_ndjson(
        ndjson(
            header(),
            file_rec("src/a.py"),
            file_rec("src/b.py"),
            file_rec("src/isolated.py"),
            file_rec("src/changed_isolated.py"),
            imports_rec("src/a.py", "src/b.py"),
        )
    )
    dash = get_graph_for_dashboard(
        graph=g,
        modified_files=["src/b.py", "src/changed_isolated.py"],
        import_surface=["src/a.py", "src/b.py"],
    )
    node_ids = {n["id"] for n in dash["nodes"]}
    assert "src/isolated.py" not in node_ids  # unconnected + unmodified -> dropped
    assert "src/changed_isolated.py" in node_ids  # modified -> kept even if isolated
    b_node = next(n for n in dash["nodes"] if n["id"] == "src/b.py")
    assert b_node["label"] == "b.py"
    assert b_node["language"] == "Python"
    assert b_node["modified"] is True
    assert b_node["in_surface"] is True
    assert dash["edges"] == [
        {"id": "src/a.py->src/b.py", "source": "src/a.py", "target": "src/b.py"}
    ]


# --- git diff ------------------------------------------------------------

def _git(cwd, *args):
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True, env=env,
    )


@pytest.fixture
def git_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "src" / "b.py").write_text("y = 2\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_get_modified_files_unstaged_staged_and_untracked(git_repo):
    (git_repo / "src" / "a.py").write_text("x = 99\n")            # unstaged edit
    (git_repo / "src" / "b.py").write_text("y = 3\n")
    _git(git_repo, "add", "src/b.py")                              # staged edit
    (git_repo / "src" / "c.py").write_text("z = 4\n")            # untracked .py
    (git_repo / "notes.txt").write_text("hi\n")                  # untracked non-.py

    modified = get_modified_files(str(git_repo))
    assert modified == ["src/a.py", "src/b.py", "src/c.py"]


def test_get_modified_files_clean_tree(git_repo):
    assert get_modified_files(str(git_repo)) == []


# --- subprocess error path -------------------------------------------

def test_snapshot_error_when_binary_missing(monkeypatch):
    monkeypatch.setattr(parser, "DEFAULT_ENTIRE_BIN", "definitely-not-a-real-binary")
    with pytest.raises(SnapshotError):
        build_graph("/tmp", entire_bin="definitely-not-a-real-binary")


@pytest.mark.skipif(shutil.which("entire") is None, reason="entire CLI not installed")
def test_live_snapshot_on_demo_repo():
    demo = os.path.join(os.path.dirname(os.path.dirname(__file__)), "demo_repo")
    graph = build_graph(demo, worktree=True)
    paths = set(graph.path_by_id().values())
    assert any(p.endswith("src/jwt.py") for p in paths)
    assert any(p.endswith("src/config.py") for p in paths)
    # jwt.py imports config.py -> config.py is imported_by jwt.py
    ib = graph.imported_by_paths()
    config_key = next(p for p in ib if p.endswith("src/config.py"))
    assert any(p.endswith("src/jwt.py") for p in ib[config_key])
