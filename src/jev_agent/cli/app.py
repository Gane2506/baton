"""Command-line entry point: the `jev` Typer app and its commands."""

import typer
from rich.console import Console

from jev_agent import __version__
from jev_agent.capbench.cli import capbench_app
from jev_agent.cli.board_cmds import board
from jev_agent.cli.cache_cmds import cache_app
from jev_agent.cli.chat_cmds import chat
from jev_agent.cli.code_cmds import code_app
from jev_agent.cli.dashboard_cmd import dashboard
from jev_agent.cli.demo_cmds import demo_app
from jev_agent.cli.design_cmds import design_app
from jev_agent.cli.doctor_cmd import doctor
from jev_agent.cli.events_cmds import events_app
from jev_agent.cli.init_cmd import init
from jev_agent.cli.learning_cmds import learning_app
from jev_agent.cli.models_cmds import models_app
from jev_agent.cli.quality_cmds import quality_app
from jev_agent.cli.queue_cmds import queue_app
from jev_agent.cli.replay_cmds import bisect, replay
from jev_agent.cli.run_cmds import approve, cancel, resume, run
from jev_agent.cli.scheduler_cmds import scheduler_app
from jev_agent.cli.server_cmds import browser_app, mcp_app, profile_app
from jev_agent.cli.ship_cmds import kpi, ship
from jev_agent.cli.status_cmds import log, status

app = typer.Typer(
    name="jev",
    help="Baton Agent Orchestrator: turn an idea into reviewed, tested code.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# Panel names used to group the top-level commands in `jev --help`.
# Panels print in order of first appearance, so the registrations below are
# ordered by panel (Run lifecycle -> Observe -> Configure -> Learn).
PANEL_RUN = "Run lifecycle"
PANEL_OBSERVE = "Observe"
PANEL_CONFIGURE = "Configure"
PANEL_LEARN = "Learn"


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"jev {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Baton Agent Orchestrator."""


# --- Run lifecycle: take an idea through to shipped code ---
app.command("init", rich_help_panel=PANEL_RUN)(init)
app.command("run", rich_help_panel=PANEL_RUN)(run)
app.command("approve", rich_help_panel=PANEL_RUN)(approve)
app.command("resume", rich_help_panel=PANEL_RUN)(resume)
app.command("cancel", rich_help_panel=PANEL_RUN)(cancel)
app.add_typer(queue_app, name="queue", rich_help_panel=PANEL_RUN)
app.add_typer(scheduler_app, name="scheduler", rich_help_panel=PANEL_RUN)
app.command("ship", rich_help_panel=PANEL_RUN)(ship)
app.add_typer(demo_app, name="demo", rich_help_panel=PANEL_RUN)

# --- Observe: watch and inspect what is happening ---
app.command("status", rich_help_panel=PANEL_OBSERVE)(status)
app.command("log", rich_help_panel=PANEL_OBSERVE)(log)
app.command("board", rich_help_panel=PANEL_OBSERVE)(board)
app.command("dashboard", rich_help_panel=PANEL_OBSERVE)(dashboard)
app.command("kpi", rich_help_panel=PANEL_OBSERVE)(kpi)
app.add_typer(capbench_app, name="capbench", rich_help_panel=PANEL_OBSERVE)
app.add_typer(cache_app, name="cache", rich_help_panel=PANEL_OBSERVE)
app.add_typer(quality_app, name="quality", rich_help_panel=PANEL_OBSERVE)
app.add_typer(events_app, name="events", rich_help_panel=PANEL_OBSERVE)
app.command("replay", rich_help_panel=PANEL_OBSERVE)(replay)
app.command("bisect", rich_help_panel=PANEL_OBSERVE)(bisect)

# --- Configure: set up the environment and tooling ---
app.command("doctor", rich_help_panel=PANEL_CONFIGURE)(doctor)
app.add_typer(profile_app, name="profile", rich_help_panel=PANEL_CONFIGURE)
app.add_typer(browser_app, name="browser", rich_help_panel=PANEL_CONFIGURE)
app.add_typer(mcp_app, name="mcp", rich_help_panel=PANEL_CONFIGURE)
app.add_typer(design_app, name="design", rich_help_panel=PANEL_CONFIGURE)

# --- Learn: lessons, the decision model, and the models themselves ---
app.add_typer(learning_app, name="learning", rich_help_panel=PANEL_LEARN)
app.add_typer(code_app, name="code", rich_help_panel=PANEL_LEARN)
app.add_typer(models_app, name="models", rich_help_panel=PANEL_LEARN)
app.command("chat", rich_help_panel=PANEL_LEARN)(chat)


def main() -> None:
    from jev_agent.core.secrets import load_secrets

    load_secrets()  # ~/.jev/secrets.env: real environment always wins
    app()
