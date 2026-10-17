"""`jev --help` groups the top-level commands into Rich panels (issue #107)."""

import re

import typer
from typer.testing import CliRunner

from jev_agent.cli.app import (
    PANEL_CONFIGURE,
    PANEL_LEARN,
    PANEL_OBSERVE,
    PANEL_RUN,
    app,
)

runner = CliRunner()

# Rich emits ANSI color codes when it detects a color-capable terminal
# (e.g. TERM=xterm on CI). Strip them so panel/command matching is
# independent of the terminal's color support.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return _ANSI_RE.sub("", output)


# The one authoritative mapping: every top-level command -> its help panel.
EXPECTED_PANELS: dict[str, str] = {
    # Run lifecycle
    "init": PANEL_RUN,
    "run": PANEL_RUN,
    "approve": PANEL_RUN,
    "resume": PANEL_RUN,
    "cancel": PANEL_RUN,
    "queue": PANEL_RUN,
    "scheduler": PANEL_RUN,
    "ship": PANEL_RUN,
    "demo": PANEL_RUN,
    # Observe
    "status": PANEL_OBSERVE,
    "log": PANEL_OBSERVE,
    "board": PANEL_OBSERVE,
    "dashboard": PANEL_OBSERVE,
    "kpi": PANEL_OBSERVE,
    "capbench": PANEL_OBSERVE,
    "quality": PANEL_OBSERVE,
    "cache": PANEL_OBSERVE,
    "events": PANEL_OBSERVE,
    "replay": PANEL_OBSERVE,
    "bisect": PANEL_OBSERVE,
    # Configure
    "doctor": PANEL_CONFIGURE,
    "profile": PANEL_CONFIGURE,
    "browser": PANEL_CONFIGURE,
    "mcp": PANEL_CONFIGURE,
    "design": PANEL_CONFIGURE,
    # Learn
    "learning": PANEL_LEARN,
    "laya": PANEL_LEARN,
    "chat": PANEL_LEARN,
    "models": PANEL_LEARN,
}

PANEL_ORDER = [PANEL_RUN, PANEL_OBSERVE, PANEL_CONFIGURE, PANEL_LEARN]


def _group() -> typer.core.TyperGroup:
    return typer.main.get_group(app)


def test_every_registered_command_has_exactly_one_expected_panel() -> None:
    """No command may drift into the default 'Commands' panel unnoticed."""
    group = _group()
    assert set(group.commands) == set(EXPECTED_PANELS)
    for name, panel in EXPECTED_PANELS.items():
        assert getattr(group.commands[name], "rich_help_panel", None) == panel


def test_help_renders_panels_in_order_with_no_default_panel() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    output = _plain(result.output)
    positions = [output.index(f"─ {panel} ─") for panel in PANEL_ORDER]
    assert positions == sorted(positions)
    # Every command is grouped, so the catch-all panel must not appear.
    assert "╭─ Commands ─" not in output


def test_each_command_appears_exactly_once_in_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    output = _plain(result.output)
    for name in EXPECTED_PANELS:
        rows = re.findall(rf"│ {re.escape(name)}\s", output)
        assert len(rows) == 1, f"{name!r} appears {len(rows)} times in --help"
