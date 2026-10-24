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
    code, out, err = _jev(fixture_repo, "cache", "stats")
    assert code == 0, out
    assert "Decision/role cache" in out
    assert "Entries" in out
    code, out, err = _jev(fixture_repo, "cache", "clear")
    assert code == 0, out
    assert "Cleared 0 cache entries" in out
    code, out, err = _jev(fixture_repo, "cache", "clear", "--type", "decisions")
    assert code == 0, out
    assert "Cleared 0 cache entries" in out
    code, out, err = _jev(fixture_repo, "cache", "clear", "--type", "roles")
    assert code == 0, out
    assert "Cleared 0 cache entries" in out
