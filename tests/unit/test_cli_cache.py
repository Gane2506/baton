"""`jev cache stats|clear` (issue #223): the --type error follows the CLI error contract."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from jev_agent.cli.app import app

runner = CliRunner()


def _jev(repo: Path, *args: str) -> tuple[int, str, str]:
    result = runner.invoke(app, [*args, "--repo", str(repo)])
    return result.exit_code, result.output, result.stderr


def test_cache_clear_bogus_type_errors_on_stderr(fixture_repo: Path) -> None:
    code, out, err = _jev(fixture_repo, "cache", "clear", "--type", "bogus")
    assert code == 1
    assert 'error: --type must be "decisions" or "roles"' in " ".join(err.split())
    assert "bogus" not in out  # nothing about the error on stdout


def test_cache_stats_and_clear(fixture_repo: Path) -> None:
    code, out, _ = _jev(fixture_repo, "cache", "stats")
    assert code == 0, out
    assert "Decision/role cache" in out
    assert "Entries" in out
    code, out, _ = _jev(fixture_repo, "cache", "clear")
    assert code == 0, out
    assert "Cleared 0 cache entries" in out
    code, out, _ = _jev(fixture_repo, "cache", "clear", "--type", "decisions")
    assert code == 0, out
    assert "Cleared 0 cache entries" in out
    code, out, _ = _jev(fixture_repo, "cache", "clear", "--type", "roles")
    assert code == 0, out
    assert "Cleared 0 cache entries" in out


def test_cache_calibrate_candidate(fixture_repo: Path) -> None:
    """The refactored calibrate keeps the CLI's output (issue #254: the dashboard
    shares `calibrate_thresholds`, so the two surfaces cannot disagree)."""
    code, out, _ = _jev(fixture_repo, "cache", "calibrate")
    assert code == 0, out
    assert "Not enough decision history" in out
    from jev_agent.app.context import STATE_DB_FILENAME
    from jev_agent.config import STATE_DIRNAME
    from jev_agent.core.models import Decision, DecisionRequest, Idea
    from jev_agent.core.store import Store

    with Store.open(fixture_repo / STATE_DIRNAME / STATE_DB_FILENAME) as store:
        run_id = store.create_run(Idea(text="add search", repo_path=fixture_repo)).id
        for _ in range(2):
            store.record_decision(
                run_id,
                None,
                DecisionRequest(
                    point="D1",
                    state={},
                    questions={"action": ["plan", "ask"]},
                ),
                Decision(
                    point="D1",
                    answers={"action": "plan"},
                    dispatcher="consensus",
                ),
            )
    code, out, _ = _jev(fixture_repo, "cache", "calibrate")
    assert code == 0, out
    assert "Semantic threshold calibration (decisions)" in out
    assert "1 comparable decision pairs across 1 decision points" in out
    assert "Candidate: 0.99" in out
