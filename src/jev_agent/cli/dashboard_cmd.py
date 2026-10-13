"""`jev dashboard`: serve the web dashboard for one repo (docs/dashboard.md).

The web stack is the optional `dashboard` extra; without it the command prints how to install
it. Binds 127.0.0.1 by default. Any other address needs a token, generated here unless
`--token` / `JEV_DASHBOARD_TOKEN` gives one, and is printed with a warning.
"""

from __future__ import annotations

import secrets
import threading
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import cli_errors, fail, repo_config, repo_root
from jev_agent.dashboard import INSTALL_HINT, missing_dependencies

console = Console()

TOKEN_ENV = "JEV_DASHBOARD_TOKEN"


def dashboard_url(host: str, port: int, token: str | None) -> str:
    shown = f"[{host}]" if ":" in host else host
    if shown in ("0.0.0.0", "[::]"):
        shown = "127.0.0.1" if shown == "0.0.0.0" else "[::1]"
    return f"http://{shown}:{port}/" + (f"?token={token}" if token else "")


def dashboard(
    host: Annotated[
        str, typer.Option("--host", help="Address to listen on (loopback by default).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535, help="Port.")] = 8765,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the dashboard in a browser.")
    ] = True,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            envvar=TOKEN_ENV,
            help="Access token (required off loopback; generated when not given).",
            show_default=False,
        ),
    ] = None,
    repo: RepoOption = Path("."),
) -> None:
    """Serve the web dashboard: board, runs, queue, learning, chats, spend and settings."""
    missing = missing_dependencies()
    if missing:
        raise fail(f"missing {', '.join(missing)}. {INSTALL_HINT}")
    import uvicorn

    from jev_agent.dashboard.app import create_app
    from jev_agent.dashboard.security import DashboardSettings, is_loopback

    with cli_errors():
        root = repo_root(repo)
        repo_config(root)
    if token is not None and len(token) < 16:
        raise fail("--token must be at least 16 characters")
    loopback = is_loopback(host)
    if not loopback and token is None:
        token = secrets.token_urlsafe(24)
    app = create_app(DashboardSettings(root=root, host=host, port=port, token=token))
    url = dashboard_url(host, port, token)
    if not loopback:
        console.print(
            f"[yellow]warning:[/yellow] listening on {escape(host)}, which is reachable from "
            "other machines. Anyone with the token can approve runs and change the queue. "
            "Traffic is plain HTTP: prefer the default 127.0.0.1 with an SSH tunnel "
            f"(`ssh -L {port}:127.0.0.1:{port} host`); see docs/dashboard.md."
        )
    console.print(f"jev dashboard for {escape(str(root))}")
    console.print(f"  {escape(url)}", highlight=False)
    console.print(
        "  Ctrl-C stops it. Runs started from the page keep their state and can be resumed."
    )
    if open_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
