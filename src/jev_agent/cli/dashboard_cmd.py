"""`jev dashboard`: serve the web dashboard for one repo (docs/dashboard.md).

The web stack is the optional `dashboard` extra; without it the command prints how to install
it. Binds 127.0.0.1 by default. Any other address needs a token, generated here unless
`--token` / `JEV_DASHBOARD_TOKEN` gives one, and is printed with a warning.
"""

from __future__ import annotations

import secrets
import socket
import threading
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.app.errors import JevError
from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import cli_errors, fail, repo_config, repo_root
from jev_agent.dashboard import INSTALL_HINT, missing_dependencies

console = Console()

TOKEN_ENV = "JEV_DASHBOARD_TOKEN"


def _refuse_home_root(root: Path) -> None:
    """Refuse to serve the user's home directory as a repo (issue #277).

    A home directory is often itself a git repo (dotfiles), so `repo_root`
    happily resolves it — and the dashboard then serves the home directory's
    empty state while the user's real chats, runs and queue sit in the repo
    they meant. That is never what was asked for: fail with a clear `error:`
    line pointing at `--repo`.
    """
    if root.resolve() == Path.home().resolve():
        raise JevError(
            f"refusing to serve your home directory ({root}) as a repo: it is not "
            "a project checkout. Pass --repo with the repo to serve, e.g. "
            "`jev dashboard --repo /path/to/repo`."
        )


def _check_port_free(host: str, port: int) -> None:
    """Fail with an `error:` line when the dashboard cannot bind (issue #198).

    Uvicorn does not raise on a busy port: it logs `ERROR: ... address
    already in use` and exits 3, so without this pre-check the user's
    first-run path ends in a bare server log line, not the CLI's `error:`
    line. The bind here is a probe only (closed at once); the residual race
    with another process grabbing the port in between is caught below,
    where `uvicorn.run`'s startup failure is also turned into `fail()`.
    """
    try:
        family, _, _, _, sockaddr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
        probe = socket.socket(family)
        try:
            probe.bind(sockaddr)
        finally:
            probe.close()
    except OSError as exc:
        raise fail(
            f"could not start the dashboard on {host}:{port}: {exc}. "
            "Stop the process already using that port, or pass --port."
        ) from exc


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
    from jev_agent.dashboard.security import (
        DashboardSettings,
        is_loopback,
        load_or_create_secret,
    )

    with cli_errors():
        root = repo_root(repo)
        _refuse_home_root(root)
        repo_config(root)
    # An empty env var (`JEV_DASHBOARD_TOKEN=`) counts as unset: generate as usual.
    token = token or None
    if token is not None and len(token) < 16:
        raise fail("--token must be at least 16 characters")
    loopback = is_loopback(host)
    if not loopback and token is None:
        token = secrets.token_urlsafe(24)
    app = create_app(
        DashboardSettings(
            root=root, host=host, port=port, token=token, secret=load_or_create_secret()
        )
    )
    _check_port_free(host, port)
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
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except OSError as exc:
        raise fail(f"could not start the dashboard on {host}:{port}: {exc}") from exc
    except SystemExit as exc:
        # Uvicorn exits (rather than raising) on startup failure, e.g. the
        # port was grabbed after the probe above. A clean exit (Ctrl-C is
        # swallowed inside uvicorn) is not an error; anything else is.
        if exc.code not in (None, 0):
            raise fail(
                f"could not start the dashboard on {host}:{port}: the server "
                "failed to start (is the port already in use? pass --port)."
            ) from exc
        raise
