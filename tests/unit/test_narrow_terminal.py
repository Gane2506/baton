"""CLI output in a 40-column terminal (issue #53).

The suite pins COLUMNS=200 (tests/conftest.py): rich tables cut cells in a narrow terminal, so
most CLI tests check the wide layout. These tests narrow every CLI console to 40 columns and check
that run outcomes, next-step hints and error messages keep their text: rich only wraps them, so
comparing with whitespace collapsed must still find every word.
"""

import sys
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from jev_agent.cli.app import app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.core.store import Store

WIDTH = 40
runner = CliRunner()


@pytest.fixture(autouse=True)
def _narrow(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, module in list(sys.modules.items()):
        if not name.startswith("jev_agent.cli."):
            continue
        for value in list(vars(module).values()):
            if isinstance(value, Console):
                monkeypatch.setattr(value, "width", WIDTH)


def _jev(repo: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--repo", str(repo)])
    lines = result.output.splitlines()
    assert all(len(line) <= WIDTH for line in lines), result.output  # really narrow
    return result.exit_code, " ".join(result.output.split())


def test_run_outcome_and_hints_survive(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    # The jev dispatcher with its key unset: every decision falls back to rules, with a warning.
    (fixture_repo / "jev.toml").write_text('[dispatcher]\nprimary = "jev"\n')
    code, output = _jev(fixture_repo, "run", "add search")
    assert code == 0, output
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        run_id = store.list_runs()[0].id
    assert f"run {run_id}: AWAITING_APPROVAL" in output
    assert f"`jev approve {run_id}`" in output
    assert "OPENROUTER_API_KEY is not set" in output

    code, output = _jev(fixture_repo, "resume", run_id)
    assert code == 1 and "approve" in output


def test_error_messages_survive(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    code, output = _jev(plain, "init")
    assert code != 0
    assert "not inside a git repository" in output


def test_config_errors_survive(fixture_repo: Path) -> None:
    (fixture_repo / "jev.toml").write_text('[roles.coder]\nefforts = ["hgih"]\n')
    code, output = _jev(fixture_repo, "status")
    assert code != 0
    assert "roles.coder.efforts: 'hgih' is not accepted" in output
