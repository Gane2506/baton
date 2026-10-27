"""`jev dashboard`: serve the web dashboard for one repo (docs/dashboard.md).

The web stack is the optional `dashboard` extra; without it the command prints how to install
it. Binds 127.0.0.1 by default. Any other address needs a token, generated here unless
`--token` / `JEV_DASHBOARD_TOKEN` gives one, and is printed with a warning.

Port handling (issue #333): the running dashboard records its pid in
`<repo>/.jev/dashboard.pid`, so a second start on the same port names the owner and points
at `jev dashboard stop` instead of sending the user to `lsof` + kill. A pid file left by a
crashed dashboard is stale (its pid is dead) and is overwritten, never trusted.

Port handling (issue #355): `stop` means stopped — after the process is gone, `stop` waits
(bounded) until the port is bindable again before reporting success, and the start probe
binds the way uvicorn binds (SO_REUSEADDR), so a port left in TIME_WAIT by the previous
server's closed connections does not read as busy.

Port handling (issue #362): `stop` defaults to the current directory, so run from
anywhere but the repo it finds no pid file while a dashboard is running — the cwd trap
the #358 verifier hit from home. With no pid file `stop` says which directory it
checked and what to do (run from the repo root, or pass `--repo`), and it probes the
port (`--port`, default 8765) to report a running dashboard: the root page's
`<title>Baton · ` marker plus the sidebar's `<div>{{ root }}</div>` name the repo that
dashboard serves. This only finds and reports — stopping still goes through the pid
file, so a dashboard whose pid file is gone is reported, not stopped.
"""

from __future__ import annotations

import contextlib
import html
import http.client
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

from jev_agent.app.errors import JevError
from jev_agent.cli import service as service_mod
from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import cli_errors, fail, repo_config, repo_root
from jev_agent.config.defaults import STATE_DIRNAME
from jev_agent.core.lease import pid_alive
from jev_agent.dashboard import INSTALL_HINT, missing_dependencies

console = Console()

TOKEN_ENV = "JEV_DASHBOARD_TOKEN"
PID_FILENAME = "dashboard.pid"
_STOP_WAIT_S = 10.0  # how long `stop` waits for a graceful shutdown before forcing it
_PORT_FREE_WAIT_S = 5.0  # how long `stop` then waits for the port to be bindable again

dashboard_app = typer.Typer(
    name="dashboard",
    help="Serve the web dashboard for one repo.",
    no_args_is_help=False,
)


def dashboard_pid_path(root: Path) -> Path:
    """`<repo>/.jev/dashboard.pid`: the running dashboard's pid, port and start time."""
    return root / STATE_DIRNAME / PID_FILENAME


def read_dashboard_pid(root: Path) -> dict[str, Any] | None:
    """The pid file's JSON, or None when there is none or it cannot be parsed."""
    try:
        raw = json.loads(dashboard_pid_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_dashboard_pid(root: Path, host: str, port: int) -> None:
    """Record this dashboard so a later start (or `stop`) can find it. Never fatal:
    a read-only `.jev/` must not stop the dashboard from serving."""
    path = dashboard_pid_path(root)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "host": host,
                    "port": port,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "argv": sys.argv,
                }
            )
            + "\n",
            encoding="utf-8",
        )


def _remove_dashboard_pid(root: Path) -> None:
    with contextlib.suppress(OSError):
        dashboard_pid_path(root).unlink()


def _busy_port_owner(root: Path, port: int) -> str | None:
    """One line naming the dashboard already on `port`, or None when unknown.

    Only the pid file can name the owner: it records the port the dashboard bound,
    and the pid is checked live, so a stale file (crashed dashboard) never names
    a dead process as the owner.
    """
    info = read_dashboard_pid(root)
    if info is None:
        return None
    pid = info.get("pid")
    if not isinstance(pid, int) or pid <= 0 or info.get("port") != port:
        return None
    if not pid_alive(pid):
        return None
    started = info.get("started_at") or "unknown start time"
    argv = " ".join(str(a) for a in info.get("argv") or [])
    detail = f"it is already running as pid {pid} (started {started})"
    if argv:
        detail += f": `{argv}`"
    return (
        f"{detail}. Stop it with `jev dashboard stop --repo {root}`, "
        "or pass --port to start a second dashboard."
    )


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


def _install_service(root: Path, host: str, port: int) -> None:
    """Install the dashboard as a login/reboot service for `root` (issue #341).

    Records the absolute `jev` path and the worker CLIs found at install time,
    so the service never depends on the service manager's minimal PATH.
    """
    with cli_errors():
        platform = service_mod.current_platform()
        spec = service_mod.ServiceSpec(
            jev_path=service_mod.jev_executable_path(),
            repo=root,
            host=host,
            port=port,
            cli_path=service_mod.discover_cli_path(),
        )
        path = service_mod.install_service(spec, platform)
    console.print(f"installed the dashboard as a {service_mod.describe_service(platform)}")
    console.print(f"  definition: {escape(str(path))}")
    console.print(f"  serves: {escape(str(root))} on {host}:{port}")
    console.print(
        "  it starts at login and restarts after a reboot. Remove it with "
        "`jev dashboard --uninstall-service`."
    )


def _uninstall_service() -> None:
    """Remove the service installed by `--install-service`."""
    with cli_errors():
        platform = service_mod.current_platform()
        removed = service_mod.uninstall_service(platform)
    if removed is None:
        console.print(f"no dashboard service installed ({service_mod.describe_service(platform)})")
    else:
        console.print(f"removed the dashboard service ({escape(str(removed))})")


def _probe_bind_error(host: str, port: int) -> OSError | None:
    """The error from binding `host:port`, or None when it binds.

    Probed the way uvicorn binds (`uvicorn.config.Config.bind_socket` sets
    SO_REUSEADDR), so a port left in TIME_WAIT by the previous server's closed
    connections counts as free: a plain bind would wrongly call it busy
    (issue #355), and uvicorn itself would have bound it fine.
    """
    try:
        family, _, _, _, sockaddr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
        probe = socket.socket(family)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(sockaddr)
        finally:
            probe.close()
    except OSError as exc:
        return exc
    return None


def _port_bindable(host: str, port: int) -> bool:
    """True when a listener could bind `host:port` right now (uvicorn's terms)."""
    return _probe_bind_error(host, port) is None


_DASHBOARD_MARKER = "<title>Baton · "  # the root page's title, e.g. `<title>Baton · Board`
_IDENTIFY_TIMEOUT_S = 2.0  # how long `stop` waits on the HTTP probe below


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that never follows: the probe must identify the
    listener itself, not content served from wherever it redirects to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _dashboard_served_root(
    host: str, port: int, timeout_s: float = _IDENTIFY_TIMEOUT_S
) -> str | None:
    """The repo root the listener on `host:port` serves, or None when it is
    not a jev dashboard.

    The root page always carries `<title>Baton · …`, and the sidebar renders the
    served repo root as `<div>{{ root }}</div>` (both since 2026-10-13). A
    dashboard bound off loopback with a token answers 401 plain text to an
    unauthenticated probe, so this only identifies loopback (tokenless)
    dashboards. Returns "" for a dashboard whose root could not be read.
    A listener that speaks no HTTP at all (an SSH banner, a database) raises
    `http.client.HTTPException` out of the probe, not `OSError` — it is caught
    here and reads as "not a dashboard".
    """
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(f"http://{host}:{port}/", timeout=timeout_s) as response:
            if response.status != 200:
                return None
            body = response.read(65536)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    text = body.decode("utf-8", errors="ignore")
    if _DASHBOARD_MARKER not in text:
        return None
    match = re.search(r"<div>([^<]+)</div>\s*</div>\s*</aside>", text)
    if match is None:
        return ""
    return html.unescape(match.group(1).strip())


def _report_no_pidfile(root: Path, port: int) -> None:
    """Report a `stop` with no pid file for `root` (issue #362).

    `stop` defaults to the current directory, so run from anywhere but the repo
    it finds no pid file while a dashboard is running — the cwd trap. The
    message says which directory was checked and what to do, and when a
    dashboard is listening on the port it names the repo that dashboard serves
    and the exact command to stop it. This only finds and reports: stopping
    still goes through the pid file, so a dashboard whose pid file is gone is
    reported, not stopped.
    """
    remedy = "run `jev dashboard stop` from the repo root, or pass `--repo <path>`"
    if _port_bindable("127.0.0.1", port):
        console.print(f"no dashboard recorded for {escape(str(root))} — {remedy}")
        return
    served = _dashboard_served_root("127.0.0.1", port)
    if served is None:
        console.print(
            f"no dashboard recorded for {escape(str(root))} — {remedy} "
            f"(port {port} is in use by another process)"
        )
        return
    if not served:
        console.print(
            f"no dashboard recorded for {escape(str(root))} — a jev dashboard "
            f"is running on port {port} but its repo could not be determined; "
            "stop it from its own repo"
        )
        return
    if Path(served).resolve() == Path(root).resolve():
        console.print(
            f"no dashboard recorded for {escape(str(root))} — but a dashboard "
            f"for this repo is running on port {port}; there is no pid file, "
            "so stop the process on that port by hand"
        )
        return
    console.print(
        f"no dashboard recorded for {escape(str(root))} — a dashboard for "
        f"{escape(served)} is running on port {port}; stop it with "
        f"`jev dashboard stop --repo {escape(served)}`"
    )


def _wait_port_free(host: str, port: int, timeout_s: float) -> bool:
    """Wait until `host:port` is bindable again, polling every 0.1 s.

    True when the port freed inside `timeout_s`. The process being gone is not
    the same as the port being free, so `stop` waits on the port, not just the
    pid (issue #355).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if _port_bindable(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _check_port_free(host: str, port: int, root: Path) -> None:
    """Fail with an `error:` line when the dashboard cannot bind (issue #198).

    Uvicorn does not raise on a busy port: it logs `ERROR: ... address
    already in use` and exits 3, so without this pre-check the user's
    first-run path ends in a bare server log line, not the CLI's `error:`
    line. The probe binds the way uvicorn binds (SO_REUSEADDR — see
    `_probe_bind_error`), so the check answers "can uvicorn bind here?" and
    the residual race with another process grabbing the port in between is
    caught below, where `uvicorn.run`'s startup failure is also turned into
    `fail()`. When the owner is this repo's own dashboard (pid file), the
    error names it and points at `jev dashboard stop` (issue #333) instead
    of `lsof`.
    """
    exc = _probe_bind_error(host, port)
    if exc is None:
        return
    owner = _busy_port_owner(root, port)
    if owner is not None:
        raise fail(f"could not start the dashboard on {host}:{port}: {exc}. {owner}") from exc
    raise fail(
        f"could not start the dashboard on {host}:{port}: {exc}. "
        "Stop the process already using that port, or pass --port."
    ) from exc


def dashboard_url(host: str, port: int, token: str | None) -> str:
    shown = f"[{host}]" if ":" in host else host
    if shown in ("0.0.0.0", "[::]"):
        shown = "127.0.0.1" if shown == "0.0.0.0" else "[::1]"
    return f"http://{shown}:{port}/" + (f"?token={token}" if token else "")


@dashboard_app.callback(invoke_without_command=True)
def dashboard(
    ctx: typer.Context,
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
    install_service: Annotated[
        bool,
        typer.Option(
            "--install-service",
            help="Install the dashboard as a service that starts at login "
            "(launchd on macOS, systemd on Linux, Scheduled Task on Windows), "
            "then exit instead of serving.",
        ),
    ] = False,
    uninstall_service: Annotated[
        bool,
        typer.Option(
            "--uninstall-service",
            help="Remove the service installed by --install-service, then exit.",
        ),
    ] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Serve the web dashboard: board, runs, queue, learning, chats, spend and settings."""
    if ctx.invoked_subcommand is not None:
        # Start options given before a subcommand (`jev dashboard --repo X stop`)
        # would be silently dropped: the subcommand takes its own options.
        if (
            host != "127.0.0.1"
            or port != 8765
            or not open_browser
            or token is not None
            or install_service
            or uninstall_service
            or repo != Path(".")
        ):
            raise fail(
                "options belong after the subcommand, e.g. `jev dashboard stop --repo <repo>`"
            )
        return
    if install_service or uninstall_service:
        if install_service and uninstall_service:
            raise fail("pass only one of --install-service and --uninstall-service")
        if install_service:
            # Installing records the repo, so it resolves the repo and refuses
            # `~` like the serve path does. Uninstalling needs no repo at all:
            # it removes the service by label, from any directory.
            with cli_errors():
                root = repo_root(repo)
                _refuse_home_root(root)
            _install_service(root, host, port)
        else:
            _uninstall_service()
        return
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
    _check_port_free(host, port, root)
    _write_dashboard_pid(root, host, port)
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
    finally:
        _remove_dashboard_pid(root)


def _stop_pid(pid: int, timeout_s: float = _STOP_WAIT_S) -> bool:
    """SIGTERM `pid`, wait up to `timeout_s` for it to exit, then force-kill it.

    Returns True when the process is gone. On Windows SIGTERM already terminates
    (there is no SIGKILL), so the force pass repeats it.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + timeout_s
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if not pid_alive(pid):
        return True
    if hasattr(signal, "SIGKILL"):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    else:  # Windows: SIGTERM is already a terminate; try once more.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    return not pid_alive(pid)


def _wait_port_free_after_stop(root: Path, info: dict[str, Any], pid: int) -> None:
    """After the dashboard's pid is gone, wait (bounded) until its recorded port
    is bindable again (issue #355). A missing or invalid host:port skips the
    wait: there is nothing to wait on. On timeout the pid file is stale (the
    pid is dead), so it goes — but the failure is reported, not a success."""
    host = info.get("host")
    port = info.get("port")
    if not (isinstance(host, str) and isinstance(port, int) and port > 0):
        return
    if _wait_port_free(host, port, _PORT_FREE_WAIT_S):
        return
    _remove_dashboard_pid(root)
    raise fail(
        f"stopped the dashboard (pid {pid}), but its port is still busy "
        f"({host}:{port}) after {_PORT_FREE_WAIT_S:g} s: something else "
        "is holding it. If this dashboard runs as a service, the service "
        "manager restarts it — stop the service instead (see docs/deploy.md)."
    )


@dashboard_app.command("stop")
def dashboard_stop(
    repo: RepoOption = Path("."),
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=1.0, help="Seconds to wait for a graceful stop."),
    ] = _STOP_WAIT_S,
    port: Annotated[
        int,
        typer.Option(
            "--port",
            help="Port to check for a running dashboard when there is no pid file.",
        ),
    ] = 8765,
) -> None:
    """Stop the dashboard serving this repo (the one `jev dashboard` started)."""
    with cli_errors():
        root = repo_root(repo)
    path = dashboard_pid_path(root)
    info = read_dashboard_pid(root)
    if info is None:
        _report_no_pidfile(root, port)
        return
    pid = info.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        with contextlib.suppress(OSError):
            path.unlink()
        console.print(f"removed an unreadable pid file at {escape(str(path))}; nothing to stop")
        return
    if not pid_alive(pid):
        with contextlib.suppress(OSError):
            path.unlink()
        console.print(f"no dashboard running (pid {pid} is gone); removed the stale pid file")
        return
    if _stop_pid(pid, timeout):
        # The process being gone is not the same as the port being free:
        # `stop` must mean a start right after it works first time (issue #355).
        _wait_port_free_after_stop(root, info, pid)
        _remove_dashboard_pid(root)
        console.print(f"stopped the dashboard (pid {pid})")
    else:
        raise fail(
            f"could not stop the dashboard (pid {pid}): it is still running "
            f"after {timeout:g} s; stop it by hand (kill {pid})"
        )
