"""Command-line entry point: the `jev` Typer app and its commands."""

import typer
from rich.console import Console

from jev_agent import __version__
from jev_agent.cli.board_cmds import board
from jev_agent.cli.chat_cmds import chat
from jev_agent.cli.doctor_cmd import doctor
from jev_agent.cli.init_cmd import init
from jev_agent.cli.learning_cmds import learning_app
from jev_agent.cli.models_cmds import models_app
from jev_agent.cli.quality_cmds import quality_app
from jev_agent.cli.queue_cmds import queue_app
from jev_agent.cli.run_cmds import approve, cancel, resume, run
from jev_agent.cli.scheduler_cmds import scheduler_app
from jev_agent.cli.status_cmds import log, status

app = typer.Typer(
    name="jev",
    help="Baton Agent Orchestrator: turn an idea into reviewed, tested code.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


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


app.command("init")(init)
app.command("doctor")(doctor)
app.command("run")(run)
app.command("approve")(approve)
app.command("resume")(resume)
app.command("status")(status)
app.command("log")(log)
app.command("cancel")(cancel)
app.add_typer(learning_app, name="learning")
app.add_typer(models_app, name="models")
app.add_typer(queue_app, name="queue")
app.add_typer(quality_app, name="quality")
app.add_typer(scheduler_app, name="scheduler")
app.command("board")(board)
app.command("chat")(chat)


def main() -> None:
    app()
