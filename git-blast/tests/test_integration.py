"""Stage 9: end-to-end over a real git repo and a real `entire graph snapshot`.

Skipped when the Entire CLI is not installed.
"""

import json
import os
import shutil
import subprocess

import pytest

from src.db import SQLiteDB, auto_seed
from src.runner import format_agent_payload, run_impact_analysis

pytestmark = pytest.mark.skipif(
    shutil.which("entire") is None, reason="entire CLI not installed"
)


def _git(cwd, *args):
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com",
    }
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True, env=env)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "base.py").write_text(
        "def base_value():\n    return 10\n"
    )
    (tmp_path / "pkg" / "mid.py").write_text(
        "from pkg.base import base_value\n\n\n"
        "def mid_value():\n    return base_value() + 1\n"
    )
    (tmp_path / "pkg" / "leaf.py").write_text(
        "from pkg.mid import mid_value\n\n\n"
        "def leaf_value():\n    return mid_value() + 1\n"
    )
    (tmp_path / "tests" / "test_leaf.py").write_text(
        "from pkg.leaf import leaf_value\n\n\n"
        "def test_leaf():\n    assert leaf_value() == 12\n"
    )
    (tmp_path / "tests" / "test_base.py").write_text(
        "from pkg.base import base_value\n\n\n"
        "def test_base():\n    assert base_value() == 10\n"
    )
    (tmp_path / "conftest.py").write_text(
        "import os, sys\n"
        "sys.path.insert(0, os.path.dirname(__file__))\n"
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_transitive_change_runs_only_the_affected_tests(repo):
    db = SQLiteDB(repo / "git_blast.db")
    try:
        summary = auto_seed("proj", str(repo), db=db)
        assert summary["mappings"] >= 1

        # Edit the deepest module; test_leaf depends on it transitively,
        # test_base does not.
        (repo / "pkg" / "base.py").write_text(
            "def base_value():\n    return 10  # touched\n"
        )

        result = run_impact_analysis(str(repo), "proj", db=db)
    finally:
        db.close()

    assert result["status"] == "PASSED"
    assert "pkg/base.py" in result["import_surface"]
    assert "pkg/leaf.py" in result["import_surface"]
    assert "tests/test_leaf.py" in result["target_tests_executed"]
    assert "tests/test_base.py" in result["target_tests_executed"]

    payload = format_agent_payload(result)
    assert "\n" not in payload
    assert json.loads(payload)["status"] == "PASSED"


def test_no_changes_reports_no_changes(repo):
    db = SQLiteDB(repo / "git_blast.db")
    try:
        auto_seed("proj", str(repo), db=db)
        result = run_impact_analysis(str(repo), "proj", db=db)
    finally:
        db.close()
    assert result["status"] == "NO_CHANGES"


def test_unrelated_change_still_scopes_down(repo):
    db = SQLiteDB(repo / "git_blast.db")
    try:
        auto_seed("proj", str(repo), db=db)
        (repo / "pkg" / "leaf.py").write_text(
            "from pkg.mid import mid_value\n\n\n"
            "def leaf_value():\n    return mid_value() + 1  # touched\n"
        )
        result = run_impact_analysis(str(repo), "proj", db=db)
    finally:
        db.close()
    # leaf changed: test_leaf must run; test_base must not.
    assert "tests/test_leaf.py" in result["target_tests_executed"]
    assert "tests/test_base.py" not in result["target_tests_executed"]
    assert result["status"] == "PASSED"
