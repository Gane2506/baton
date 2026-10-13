from typer.testing import CliRunner

from jev_agent import __version__
from jev_agent.cli.app import app
from jev_agent.cli.text import short

runner = CliRunner()


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_short_puts_text_on_one_line_and_cuts_it() -> None:
    assert short("add\n  search ", 20) == "add search"
    assert short("abcdefghij", 6) == "abc..."
    assert short("abcdefghij", 2) == "a..."
