"""`--quality` on run/resume/queue, `jev quality show|checks`, and the status/log/doctor rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli import doctor_cmd
from jev_agent.cli.app import app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.models import Idea, RunState
from jev_agent.core.store import Store
from jev_agent.quality import tools

runner = CliRunner()

CONFIG = '[dispatcher]\nprimary = "rules"\n[approval]\nrequire_plan_approval = false\n'


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Never run a real scanner: no quality tool is "installed".
    monkeypatch.setattr(tools, "which", lambda tool: None)


@pytest.fixture
def repo(fixture_repo: Path) -> Path:
    (fixture_repo / "jev.toml").write_text(CONFIG)
    return fixture_repo


def _jev(repo: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--repo", str(repo)])
    return result.exit_code, " ".join(result.output.split())


def _store(repo: Path) -> Store:
    return Store.open(repo / ".jev" / "state.db")


def _only_run_id(repo: Path) -> str:
    with _store(repo) as store:
        (run,) = store.list_runs()
        return run.id


def test_run_flag_status_log_and_show(repo: Path) -> None:
    code, out = _jev(repo, "run", "--quality", "medium", "add a file")
    assert code == 0, out
    run_id = _only_run_id(repo)
    assert "quality: medium; report: `jev quality show" in out
    with _store(repo) as store:
        run = store.get_run(run_id)
    assert run.state is RunState.DONE and run.quality_level == "medium"

    code, out = _jev(repo, "status", run_id)
    assert code == 0 and "quality: medium; T1 #1 passed, integration #1 passed" in out
    code, out = _jev(repo, "log", run_id)
    assert code == 0 and "quality [T1] #1 medium: passed" in out
    assert "secrets=pass" in out and "attempt code_reviewer [T1]" in out
    code, out = _jev(repo, "quality", "show", run_id)
    assert code == 0 and "# Quality review: run" in out and "## Checks" in out
    (repo / ".jev" / "runs" / run_id / "quality" / "review.md").unlink()
    code, out = _jev(repo, "quality", "show", run_id)
    assert code == 0 and "every gate passed" in out  # rebuilt from the database


def test_bad_level_is_rejected(repo: Path) -> None:
    code, out = _jev(repo, "run", "--quality", "ultra", "x")
    assert code == 1 and "unknown quality level 'ultra'" in out


def test_config_level_and_show_without_gates(repo: Path) -> None:
    (repo / "jev.toml").write_text(CONFIG + '[quality]\nlevel = "basic"\n')
    code, out = _jev(repo, "run", "--plan-only", "x")
    assert code == 0, out
    run_id = _only_run_id(repo)
    with _store(repo) as store:
        assert store.get_run(run_id).quality_level == "basic"
    code, out = _jev(repo, "quality", "show", run_id)
    assert code == 0 and "No quality gate has run for run" in out
    code, out = _jev(repo, "status", run_id)
    assert "quality: basic (no gate has run yet)" in out


def test_queue_item_level_beats_config(repo: Path) -> None:
    (repo / "jev.toml").write_text(CONFIG + '[quality]\nlevel = "basic"\n')
    code, out = _jev(repo, "queue", "add", "--quality", "production", "x")
    assert code == 0, out
    code, out = _jev(repo, "queue", "add", "y")
    code, out = _jev(repo, "queue", "list")
    assert "quality=production" in out
    code, out = _jev(repo, "queue", "add", "--quality", "nope", "z")
    assert code == 1 and "unknown quality level" in out
    code, out = _jev(repo, "scheduler", "start", "--max-runs", "2", "--poll", "0.1")
    assert code == 0, out
    with _store(repo) as store:
        runs = sorted(store.list_runs(), key=lambda r: r.created_at)
    assert [r.quality_level for r in runs] == ["production", "basic"]


def test_resume_flag_changes_the_level(repo: Path) -> None:
    with _store(repo) as store:
        run = store.create_run(Idea(text="x", repo_path=repo), quality_level="production")
    code, out = _jev(repo, "resume", "--quality", "none", run.id)
    assert code == 0, out
    with _store(repo) as store:
        assert store.get_run(run.id).quality_level == "none"
        assert store.list_quality_reports(run.id) == []


def test_quality_checks_lists_checks_and_tools(repo: Path) -> None:
    (repo / "pyproject.toml").write_text("")
    code, out = _jev(repo, "quality", "checks")
    assert code == 0, out
    for name in ("ruff", "tests", "secrets", "bandit", "mypy", "trivy", "ai-review"):
        assert name in out
    assert "no (built-in scan)" in out and "scalable:" in out


def test_doctor_quality_row(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_cmd, "which", lambda tool: None)
    rows = {c.name: c for c in doctor_cmd.run_checks(repo)}
    assert rows["quality"].status == "ok" and "level none" in rows["quality"].detail
    (repo / "pyproject.toml").write_text("")
    (repo / "jev.toml").write_text(CONFIG + '[quality]\nlevel = "medium"\n')
    rows = {c.name: c for c in doctor_cmd.run_checks(repo)}
    assert rows["quality"].status == "warn"
    assert "ruff" in rows["quality"].detail and "bandit" in rows["quality"].detail
    assert "gitleaks" not in rows["quality"].detail  # the built-in scan covers it
    (repo / "jev.toml").write_text(
        CONFIG + '[quality]\nlevel = "medium"\nrequired_tools = ["ruff"]\n'
    )
    rows = {c.name: c for c in doctor_cmd.run_checks(repo)}
    assert (
        rows["quality"].status == "fail"
        and "required tools missing: ruff" in rows["quality"].detail
    )
