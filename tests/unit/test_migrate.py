"""Legacy (jao v0.1) detection and `jev init --migrate`."""

import sqlite3
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli.app import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, " ".join(result.output.split())  # undo Rich's line wrapping


@pytest.fixture
def legacy_repo(tmp_path: Path) -> Path:
    """A repo as jao v0.1 left it: jao.toml, .jao/ with a state.db, branches, a ref, a worktree."""
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    (repo / ".gitignore").write_text("*.pyc\n.jao/\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "jao.toml").write_text("[budget]\nper_run_usd = 1.0\n")
    state = repo / ".jao"
    (state / "runs" / "r1").mkdir(parents=True)
    _git(repo, "update-ref", "refs/jao/r1/start", "HEAD")
    _git(repo, "branch", "jao/r1/T1")
    _git(repo, "branch", "jao/r1/result")
    _git(repo, "worktree", "add", "-q", "-b", "jao/r1/T2", str(state / "worktrees" / "r1-T2"))
    conn = sqlite3.connect(state / "state.db")
    with conn:
        conn.execute(
            "CREATE TABLE attempts (id INTEGER PRIMARY KEY, log_path TEXT, result_json TEXT)"
        )
        log = f"{state}/runs/r1/T1/1.log"
        conn.execute(
            "INSERT INTO attempts (log_path, result_json) VALUES (?, ?)",
            (log, f'{{"raw_log_path": "{log}"}}'),
        )
        conn.execute("INSERT INTO attempts (log_path) VALUES (?)", ("/elsewhere/x.log",))
    conn.close()
    return repo


@pytest.mark.parametrize("command", [["status"], ["doctor"], ["init"], ["queue", "list"]])
def test_legacy_repo_stops_every_command(legacy_repo: Path, command: list[str]) -> None:
    code, output = _invoke(*command, "--repo", str(legacy_repo))
    assert code == 1
    assert "This repo was set up with jao (v0.1). Run `jev init --migrate`" in output
    assert (legacy_repo / "jao.toml").exists()  # nothing was touched


def test_only_state_dir_is_also_legacy(legacy_repo: Path) -> None:
    (legacy_repo / "jao.toml").unlink()
    code, output = _invoke("status", "--repo", str(legacy_repo))
    assert code == 1 and "jev init --migrate" in output


def test_new_layout_is_not_legacy(legacy_repo: Path) -> None:
    (legacy_repo / "jev.toml").write_text("")
    _, output = _invoke("status", "--repo", str(legacy_repo))
    assert "jev init --migrate" not in output, output


def test_migrate_moves_everything(legacy_repo: Path) -> None:
    repo = legacy_repo
    code, output = _invoke("init", "--migrate", "--repo", str(repo))
    assert code == 0, output

    assert not (repo / "jao.toml").exists() and not (repo / ".jao").exists()
    assert (repo / "jev.toml").read_text() == "[budget]\nper_run_usd = 1.0\n"
    assert (repo / ".jev" / "runs" / "r1").is_dir()
    assert (repo / ".gitignore").read_text() == "*.pyc\n.jev/\n"

    refs = set(_git(repo, "for-each-ref", "--format=%(refname)").split())
    assert {"refs/heads/jev/r1/T1", "refs/heads/jev/r1/result", "refs/jev/r1/start"} <= refs
    assert "refs/heads/jao/r1/T1" not in refs and "refs/jao/r1/start" not in refs
    # The branch checked out in the worktree is left alone and reported.
    assert "refs/heads/jao/r1/T2" in refs
    assert "skipped refs/heads/jao/r1/T2: checked out in a worktree" in output

    # The worktree followed the move.
    new_wt = repo / ".jev" / "worktrees" / "r1-T2"
    assert str(new_wt) in _git(repo, "worktree", "list")
    assert _git(new_wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "jao/r1/T2"

    conn = sqlite3.connect(repo / ".jev" / "state.db")
    rows = conn.execute("SELECT log_path, result_json FROM attempts ORDER BY id").fetchall()
    conn.close()
    new_log = f"{repo}/.jev/runs/r1/T1/1.log"
    assert rows == [(new_log, f'{{"raw_log_path": "{new_log}"}}'), ("/elsewhere/x.log", None)]

    # Commands work again, and a second migrate changes nothing.
    assert "jev init --migrate" not in _invoke("status", "--repo", str(repo))[1]
    code, output = _invoke("init", "--migrate", "--repo", str(repo))
    assert code == 0, output
    assert (repo / ".gitignore").read_text() == "*.pyc\n.jev/\n"


def test_migrate_adds_gitignore_entry_when_missing(legacy_repo: Path) -> None:
    (legacy_repo / ".gitignore").write_text("*.pyc\n")
    assert _invoke("init", "--migrate", "--repo", str(legacy_repo))[0] == 0
    assert (legacy_repo / ".gitignore").read_text() == "*.pyc\n.jev/\n"


def test_migrate_refuses_when_both_names_exist(legacy_repo: Path) -> None:
    (legacy_repo / ".jev").mkdir()
    code, output = _invoke("init", "--migrate", "--repo", str(legacy_repo))
    assert code == 1
    assert "both .jao and .jev exist" in output
    assert (legacy_repo / "jao.toml").exists()


def test_migrate_with_nothing_to_do(tmp_path: Path) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(repo, "init", "-q")
    code, output = _invoke("init", "--migrate", "--repo", str(repo))
    assert code == 0 and "Nothing to migrate" in output
    assert not (repo / ".gitignore").exists()
