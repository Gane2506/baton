"""`jev learning ...`, the learning flags of `jev run` / `jev resume`, and the learning rows of
`jev status`, `jev log` and `jev doctor`."""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli import doctor_cmd
from jev_agent.cli.app import app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.learning import Learning
from jev_agent.core.models import RunState
from jev_agent.core.store import Store

runner = CliRunner()

CONFIG = '[dispatcher]\nprimary = "rules"\n[approval]\nrequire_plan_approval = false\n'


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(FAKE_WORKERS_ENV, raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@pytest.fixture
def repo(fixture_repo: Path) -> Path:
    (fixture_repo / "jev.toml").write_text(CONFIG)
    return fixture_repo


def _jev(repo: Path, *args: str, input: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--repo", str(repo)], input=input)
    # Unwrap lines and drop table borders, so rows read as plain words.
    return result.exit_code, " ".join(re.sub(r"[\u2500-\u257f]", " ", result.output).split())


def _store(repo: Path) -> Store:
    return Store.open(repo / ".jev" / "state.db")


def _add_lesson(repo: Path, profile: str, text: str) -> str:
    with _store(repo) as store:
        learning = Learning.for_store(store)
        try:
            lesson = learning.db_for(profile).add_lesson(profile, text, ["planner", "coder"], [])
        finally:
            learning.close()
    return lesson.id


def test_profile_commands(repo: Path, tmp_path: Path) -> None:
    code, out = _jev(repo, "learning", "list")
    assert code == 0, out
    assert "learning is off for this repo (default)" in out and "No profiles yet" in out

    code, out = _jev(repo, "learning", "enable")
    assert code == 0 and "Learning is on for this repo, with profile default" in out
    code, out = _jev(repo, "learning", "create", "team", "--shared")
    assert code == 0 and "(shared:" in out
    assert (tmp_path / "xdg-data" / "jev" / "learning.db").exists()
    code, out = _jev(repo, "learning", "create", "work")
    assert code == 0 and "(repo:" in out
    code, out = _jev(repo, "learning", "create", "work")
    assert code == 1 and "already exists" in out

    code, out = _jev(repo, "learning", "use", "work")
    assert code == 0 and "Active profile: work." in out
    code, out = _jev(repo, "learning", "use", "nope")
    assert code == 1 and "no learning profile named 'nope'" in out
    code, out = _jev(repo, "learning", "list")
    assert code == 0
    assert "learning is on for this repo (cli setting); active profile: work (cli setting)" in out
    for row in ("default repo yes 0", "team shared yes 0", "work repo yes 0 *"):
        assert row in out, row

    lesson_id = _add_lesson(repo, "work", "Keep tasks small.")
    code, out = _jev(repo, "learning", "show")
    assert code == 0 and lesson_id in out and "Keep tasks small." in out
    assert "planner,coder" in out
    code, out = _jev(repo, "learning", "forget", lesson_id)
    assert code == 0 and f"Retired {lesson_id} from work" in out
    code, out = _jev(repo, "learning", "show", "work")
    assert code == 0 and "has no lessons" in out
    code, out = _jev(repo, "learning", "show", "work", "--all")
    assert code == 0 and "Keep tasks small." in out and "retired" in out

    code, out = _jev(repo, "learning", "disable", "team")
    assert code == 0 and "disabled" in out
    code, out = _jev(repo, "learning", "list")
    assert "team shared no 0" in out
    code, out = _jev(repo, "learning", "enable", "team")
    assert code == 0 and "Profile team is enabled" in out

    code, out = _jev(repo, "learning", "flush", "work", input="n\n")
    assert code == 1
    code, out = _jev(repo, "learning", "flush", "work", "--yes")
    assert code == 0 and "Deleted 1 lesson(s) from work" in out
    code, out = _jev(repo, "learning", "delete", "work", input="y\n")
    assert code == 0 and "Deleted profile work" in out
    code, out = _jev(repo, "learning", "list")
    assert "active profile: default (default)" in out  # the deleted active profile is unset
    code, out = _jev(repo, "learning", "delete", "nope", "--yes")
    assert code == 1 and "no learning profile" in out
    code, out = _jev(repo, "learning", "forget", "L99")
    assert code == 1 and "no lesson" in out

    code, out = _jev(repo, "learning", "disable")
    assert code == 0 and "Learning is off for this repo" in out
    code, out = _jev(repo, "learning", "list")
    assert "learning is off for this repo (cli setting)" in out


def test_run_with_learning_flags(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    code, out = _jev(repo, "run", "add search", "--learning", "x", "--no-learning")
    assert code == 1 and "cannot be used together" in out
    code, out = _jev(repo, "run", "add search", "--learning", "nope")
    assert code == 1 and "no learning profile named 'nope'" in out

    assert _jev(repo, "learning", "create", "work")[0] == 0
    lesson_id = _add_lesson(repo, "work", "Prefer small diffs.")
    code, out = _jev(repo, "run", "add search", "--learning", "work")
    assert code == 0, out
    assert "DONE" in out and "learning: learned into work: 0 added" in out
    with _store(repo) as store:
        run = store.list_runs()[0]
        planner = next(a for a in store.list_attempts(run.id) if a.role == "planner")
    assert run.learning_profile == "work" and planner.lesson_ids == (lesson_id,)

    code, out = _jev(repo, "status", run.id)
    assert code == 0 and "learning: profile work (learned into work" in out
    code, out = _jev(repo, "log", run.id)
    assert code == 0 and f"lessons {lesson_id}" in out and "decision D7" in out
    assert "attempt learner [run]" in out

    code, out = _jev(repo, "run", "add search", "--no-learning")
    assert code == 0 and "learning:" not in out
    with _store(repo) as store:
        run = store.list_runs()[0]
    assert run.learning_profile is None
    code, out = _jev(repo, "status", run.id)
    assert "learning: off" in out


def test_resume_learning_flags(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    (repo / "jev.toml").write_text('[dispatcher]\nprimary = "rules"\n')
    assert _jev(repo, "learning", "create", "work")[0] == 0
    code, out = _jev(repo, "run", "--plan-only", "add search", "--learning", "work")
    assert code == 0 and "AWAITING_APPROVAL" in out
    with _store(repo) as store:
        run_id = store.list_runs()[0].id
        store.set_run_state(run_id, RunState.EXECUTING)  # as `jev approve` without workers
    code, out = _jev(repo, "resume", run_id, "--learning", "nope")
    assert code == 1 and "no learning profile" in out
    code, out = _jev(repo, "resume", run_id, "--no-learning")
    assert code == 0 and "DONE" in out
    with _store(repo) as store:
        run = store.get_run(run_id)
    assert (run.learning_profile, run.learning_outcome) == (None, None)
    code, out = _jev(repo, "resume", "nope", "--learning", "work")
    assert code == 1 and "not found" in out


def test_doctor_learning_rows(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_cmd, "which", lambda name: f"/usr/bin/{name}")
    checks = {c.name: c for c in doctor_cmd.run_checks(repo)}
    assert checks["learning"].status == "ok"
    assert checks["learning"].detail == "off (default); profile default (not created yet)"
    assert checks["role learner"].status == "ok"
    assert not (repo / ".jev" / "state.db").exists()  # doctor never creates the database

    assert _jev(repo, "learning", "enable")[0] == 0
    _add_lesson(repo, "default", "A lesson.")
    checks = {c.name: c for c in doctor_cmd.run_checks(repo)}
    assert checks["learning"].status == "ok"
    assert checks["learning"].detail.startswith(
        "on (cli setting); profile default (repo, 1 lesson(s))"
    )

    (repo / "jev.toml").write_text(CONFIG + "[roles.learner]\nworkers = []\n")
    checks = {c.name: c for c in doctor_cmd.run_checks(repo)}
    assert checks["role learner"].status == "warn"
    assert checks["learning"].status == "warn" and "no usable worker" in checks["learning"].detail
