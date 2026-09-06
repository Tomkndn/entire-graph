"""Stage 5 coverage: the `seed` and `test` CLI subcommands."""

import json
import os
import subprocess

import pytest

from src import cli, parser

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


def _git(cwd, *args):
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com",
    }
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True, env=env)


@pytest.fixture
def cli_project(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "a.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    monkeypatch.setattr(parser, "_run_snapshot", lambda *a, **k: _snapshot())
    return tmp_path


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "cli.db")


# --- seed ------------------------------------------------------------

def test_seed_demo(db_path, capsys):
    code = cli.main(["seed", "--demo", "--repo-id", "demo", "--db", db_path])
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["mappings"] == 3
    assert summary["repo_id"] == "demo"


def test_seed_auto(cli_project, db_path, capsys):
    code = cli.main([
        "seed", "--auto", "--repo-root", str(cli_project),
        "--repo-id", "proj", "--db", db_path,
    ])
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["repo_id"] == "proj"
    assert summary["mappings"] >= 1


def test_seed_snapshot_error_exits_2(tmp_path, db_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise parser.SnapshotError("no entire binary")

    monkeypatch.setattr(parser, "_run_snapshot", boom)
    code = cli.main([
        "seed", "--auto", "--repo-root", str(tmp_path),
        "--repo-id", "proj", "--db", db_path,
    ])
    assert code == 2
    assert "error:" in capsys.readouterr().err


# --- test ----------------------------------------------------------

def test_test_dry_run_lean(cli_project, db_path, capsys):
    (cli_project / "src" / "a.py").write_text("VALUE = 2\n")
    cli.main(["seed", "--auto", "--repo-root", str(cli_project),
              "--repo-id", "proj", "--db", db_path])
    capsys.readouterr()

    code = cli.main([
        "test", "--repo-root", str(cli_project), "--repo-id", "proj",
        "--db", db_path, "--dry-run",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "\n" in out  # pretty JSON
    result = json.loads(out)
    assert result["status"] == "DRY_RUN"
    assert result["target_tests_executed"] == ["tests/test_a.py"]


def test_test_passed_agent_format(cli_project, db_path, capsys):
    (cli_project / "src" / "a.py").write_text("VALUE = 3\n")
    cli.main(["seed", "--auto", "--repo-root", str(cli_project),
              "--repo-id", "proj", "--db", db_path])
    capsys.readouterr()

    code = cli.main([
        "test", "--repo-root", str(cli_project), "--repo-id", "proj",
        "--db", db_path, "--format", "agent",
    ])
    assert code == 0
    out = capsys.readouterr().out.strip()
    assert out.count("\n") == 0
    assert json.loads(out)["status"] == "PASSED"


def test_test_failed_exits_1(cli_project, db_path, capsys):
    cli.main(["seed", "--auto", "--repo-root", str(cli_project),
              "--repo-id", "proj", "--db", db_path])
    capsys.readouterr()
    (cli_project / "src" / "a.py").write_text("VALUE = 4\n")
    (cli_project / "tests" / "test_a.py").write_text(
        "def test_bad():\n    assert False\n"
    )

    code = cli.main([
        "test", "--repo-root", str(cli_project), "--repo-id", "proj",
        "--db", db_path,
    ])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "FAILED"


def test_test_no_changes_exits_0(cli_project, db_path, capsys):
    cli.main(["seed", "--auto", "--repo-root", str(cli_project),
              "--repo-id", "proj", "--db", db_path])
    capsys.readouterr()

    code = cli.main([
        "test", "--repo-root", str(cli_project), "--repo-id", "proj",
        "--db", db_path,
    ])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "NO_CHANGES"


def test_test_negative_max_depth_exits_2(cli_project, db_path, capsys):
    code = cli.main([
        "test", "--repo-root", str(cli_project), "--repo-id", "proj",
        "--db", db_path, "--max-depth", "-1",
    ])
    assert code == 2
    assert "--max-depth" in capsys.readouterr().err


def test_unknown_command_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["frobnicate"])
    assert exc.value.code == 2


def test_no_command_is_usage_error():
    with pytest.raises(SystemExit):
        cli.main([])


def test_module_entrypoint_runs():
    proc = subprocess.run(
        ["python", "-m", "src.cli", "--help"],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert "seed" in proc.stdout and "test" in proc.stdout
