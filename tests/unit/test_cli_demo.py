"""`jev init --demo` seeding, `jev demo reset` and the demo marker (issue #105)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli.app import app
from jev_agent.config import DEFAULT_CONFIG_TOML, STATE_DIRNAME
from jev_agent.core.chat import list_chats
from jev_agent.core.learning import Learning
from jev_agent.core.store import Store
from jev_agent.demo import DEMO_MARKER_FILENAME, is_demo_repo

runner = CliRunner()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _init_demo(repo: Path, *extra: str):
    return runner.invoke(app, ["init", "--demo", "--repo", str(repo), *extra])


def _reset(repo: Path, *extra: str, input: str | None = None):
    return runner.invoke(app, ["demo", "reset", "--repo", str(repo), *extra], input=input)


def _marker(repo: Path) -> dict:
    return json.loads((repo / STATE_DIRNAME / DEMO_MARKER_FILENAME).read_text(encoding="utf-8"))


def test_init_demo_seeds_expected_tables(git_repo: Path) -> None:
    result = _init_demo(git_repo)
    assert result.exit_code == 0, result.output
    assert is_demo_repo(git_repo)
    marker = _marker(git_repo)
    assert marker["demo"] is True
    assert marker["seeded_by"] == "jev init --demo"
    assert "remove" in marker["note"].lower()

    with Store.open(git_repo / STATE_DIRNAME / "state.db") as store:
        runs = store.list_runs()
        assert len(runs) == 17
        assert {r.state for r in runs} >= {
            "DONE",
            "EXECUTING",
            "AWAITING_APPROVAL",
            "PLANNING",
        }
        assert sum(r.spend_usd for r in runs) > 0  # spend data is seeded
        assert len(store.list_queue()) == 4
        assert len(store.list_shipped_prs()) == 10
        learning = Learning.for_store(store)
        try:
            assert len(learning.lessons("textstats")) == 6
        finally:
            learning.close()
    assert len(list_chats(git_repo)) == 1


def test_init_demo_marks_data_as_demo(git_repo: Path) -> None:
    result = _init_demo(git_repo)
    assert result.exit_code == 0, result.output
    assert "marked as demo" in result.output
    assert "jev demo reset" in result.output
    # Seeded logs say they are demo logs: no agent ran.
    logs = list((git_repo / STATE_DIRNAME / "runs").rglob("*.log"))
    assert logs, "seeder writes per-attempt logs"
    assert "[demo]" in logs[0].read_text(encoding="utf-8")


def test_plain_init_is_unaffected(git_repo: Path) -> None:
    result = runner.invoke(app, ["init", "--repo", str(git_repo)])
    assert result.exit_code == 0, result.output
    assert not is_demo_repo(git_repo)
    assert "--demo" not in result.output


def test_init_demo_leaves_user_config_alone(git_repo: Path) -> None:
    user_config = Path(os.environ["XDG_CONFIG_HOME"]) / "jev" / "config.toml"
    before = user_config.read_text(encoding="utf-8")
    assert "trust" not in before  # the suite config has no trust section
    assert _init_demo(git_repo).exit_code == 0
    assert user_config.read_text(encoding="utf-8") == before


def test_init_demo_refuses_when_state_exists(git_repo: Path) -> None:
    assert _init_demo(git_repo).exit_code == 0
    with Store.open(git_repo / STATE_DIRNAME / "state.db") as store:
        before = len(store.list_runs())
    result = _init_demo(git_repo, "--force")
    assert result.exit_code == 1
    assert "already holds state" in result.output
    with Store.open(git_repo / STATE_DIRNAME / "state.db") as store:
        assert len(store.list_runs()) == before  # nothing appended


def test_init_demo_rejects_migrate_combination(git_repo: Path) -> None:
    result = _init_demo(git_repo, "--migrate")
    assert result.exit_code == 1
    assert "Cannot combine --demo with --migrate" in result.output


def test_demo_reset_removes_demo_state(git_repo: Path) -> None:
    assert _init_demo(git_repo).exit_code == 0
    result = _reset(git_repo, "--yes")
    assert result.exit_code == 0, result.output
    assert not is_demo_repo(git_repo)
    assert (git_repo / STATE_DIRNAME).is_dir()  # a clean state dir is left behind
    assert (git_repo / "jev.toml").read_text(encoding="utf-8") == DEFAULT_CONFIG_TOML
    with Store.open(git_repo / STATE_DIRNAME / "state.db") as store:
        assert store.list_runs() == []
        assert store.list_queue() == []


def test_demo_reset_refuses_without_marker(git_repo: Path) -> None:
    assert runner.invoke(app, ["init", "--repo", str(git_repo)]).exit_code == 0
    result = _reset(git_repo, "--yes")
    assert result.exit_code == 1
    assert "no demo data" in result.output
    # Nothing was touched.
    assert (git_repo / "jev.toml").read_text(encoding="utf-8") == DEFAULT_CONFIG_TOML
    assert (git_repo / STATE_DIRNAME).is_dir()


def test_demo_reset_asks_before_removing(git_repo: Path) -> None:
    assert _init_demo(git_repo).exit_code == 0
    assert _reset(git_repo, input="n\n").exit_code == 1
    assert is_demo_repo(git_repo)  # declined: demo data is still there
    result = _reset(git_repo, input="y\n")
    assert result.exit_code == 0, result.output
    assert not is_demo_repo(git_repo)


def test_demo_reset_outside_git_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = _reset(plain, "--yes")
    assert result.exit_code == 1
    assert "not inside a git repository" in " ".join(result.output.split())


def _render_base(demo: bool) -> str:
    """Render the dashboard's base template standalone (the app module is not needed)."""
    from jinja2 import Environment, PackageLoader

    env = Environment(loader=PackageLoader("jev_agent.dashboard", "templates"))
    env.globals["csrf_field"] = "csrf"
    return env.get_template("base.html").render(
        request=None,
        path="/",
        csrf="test",
        refresh_ms=2000,
        root="/tmp/x",
        version="0.0",
        nav=[("/", "Board")],
        flashes=[],
        demo=demo,
    )


def test_dashboard_banner_marks_demo_repos() -> None:
    assert "Demo data" in _render_base(True)
    assert "jev demo reset" in _render_base(True)
    assert "Demo data" not in _render_base(False)
