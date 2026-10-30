"""`jev run --workflow-profile` (issue #368 partial coverage, phases 0-2).

The flag threads through `start_run` into `create_run`'s single resolver:
an unresolvable reference or a safety-floor violation refuses the run
with a clean CLI error, never a guessed run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli.app import app as cli_app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.store import Store
from jev_agent.core.workflow_profiles import ProfileStore

runner = CliRunner()

# With color forced (FORCE_COLOR=1, as in CI), rich styles the option name
# in segments (`-`, `-workflow`, `-profile`), so the raw output never
# contains the contiguous flag. Compare against the visible text instead.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

PROFILE = {
    "id": "repo:fast",
    "display_name": "Fast",
    "scope": "repo",
    "stage_list": ["INTAKE", "PLANNING", "VALIDATING", "DESIGNING", "EXECUTING", "INTEGRATING"],
    "gate_policies": {
        "PLANNING": "inherit",
        "VALIDATING": "inherit",
        "DESIGNING": "inherit",
        "EXECUTING": "inherit",
        "INTEGRATING": "inherit",
    },
    "designing": "when_needed",
    "ship_enabled": False,
}


@pytest.fixture(autouse=True)
def _fake_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


def _planned_repo(repo: Path) -> Path:
    # Mirror the dashboard tests' planned fixture: the dispatcher with its
    # key unset falls back to rules, so a run reaches approval with no
    # live workers.
    (repo / "jev.toml").write_text('[dispatcher]\nprimary = "jev"\n')
    return repo


def test_run_help_lists_workflow_profile() -> None:
    result = runner.invoke(cli_app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--workflow-profile" in _ANSI_RE.sub("", result.output)


def test_run_with_workflow_profile(fixture_repo: Path) -> None:
    repo = _planned_repo(fixture_repo)
    ProfileStore(repo, repo / "user").save(PROFILE)
    result = runner.invoke(
        cli_app, ["run", "profiled idea", "--workflow-profile", "repo:fast", "--repo", str(repo)]
    )
    assert result.exit_code == 0, result.output
    with Store.open(repo / ".jev" / "state.db") as store:
        runs = store.list_runs()
        assert len(runs) == 1
        assert runs[0].workflow_profile_id == "repo:fast"
        assert runs[0].workflow_profile_version == 1


def test_run_with_unknown_workflow_profile_refuses(fixture_repo: Path) -> None:
    repo = _planned_repo(fixture_repo)
    result = runner.invoke(
        cli_app, ["run", "nope idea", "--workflow-profile", "repo:nope", "--repo", str(repo)]
    )
    assert result.exit_code == 1
    assert "no longer resolves" in result.output
