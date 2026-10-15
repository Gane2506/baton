"""`jev mcp servers|runs`, `jev browser install`, `jev profile list|show|use`."""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jev_agent import __version__
from jev_agent.app.profiles import describe_profile, list_profiles, set_default_profile
from jev_agent.cli.repo import RepoOption
from jev_agent.cli.wiring import cli_errors, fail, repo_config, repo_context, repo_root
from jev_agent.config import STATE_DIRNAME, JevConfig
from jev_agent.config.servers_schema import NO_PROFILE
from jev_agent.mcp_runs import runs_mcp
from jev_agent.servers.access import (
    allowed_grants,
    parse_allow,
)
from jev_agent.servers.browser import install_argv
from jev_agent.servers.mcp import servers_mcp
from jev_agent.servers.policy import PolicyContext, check_command
from jev_agent.servers.profiles import (
    ProfileError,
    choose_interactively,
    repo_default,
)
from jev_agent.servers.service import ServerService
from jev_agent.servers.ssh import SshRunner, run_process

console = Console()

mcp_app = typer.Typer(name="mcp", help="MCP servers jev provides to agents.", no_args_is_help=True)
browser_app = typer.Typer(
    name="browser", help="The virtual browser (Playwright + Chromium).", no_args_is_help=True
)
profile_app = typer.Typer(
    name="profile", help="Workspace profiles: servers, browser and learning per run.",
    no_args_is_help=True,
)  # fmt: skip

# Indirections for tests: no real ssh, npx or terminal.
ssh_runner: SshRunner = run_process
LocalRunner = Callable[[Sequence[str]], int]


def _run_local(argv: Sequence[str]) -> int:
    return subprocess.call(list(argv))


local_runner: LocalRunner = _run_local


def stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _load(repo: Path) -> tuple[Path, JevConfig]:
    with cli_errors():
        root = repo_root(repo)
        return root, repo_config(root)


# ---- jev mcp servers ---------------------------------------------------------------------


@mcp_app.command("servers")
def mcp_servers(
    allow: Annotated[
        str,
        typer.Option(
            "--allow",
            metavar="NAME[:LEVEL],...",
            help="Servers to expose, optionally at a lower level (basic|medium).",
        ),
    ],
    run_id: Annotated[
        str | None, typer.Option("--run-id", metavar="ID", help="Run id for the audit log.")
    ] = None,
    role: Annotated[
        str | None,
        typer.Option("--role", metavar="ROLE", help="Agent role; every role but coder: basic."),
    ] = None,
    repo: RepoOption = Path("."),
) -> None:
    """Serve list_servers, run_command, read_file (and for medium write_file, upload) over
    stdio MCP. Every call is checked against the access policy and audited."""
    root, config = _load(repo)
    try:
        grants = allowed_grants(config, role, parse_allow(allow))
    except ValueError as exc:
        raise fail(str(exc)) from exc
    service = ServerService(
        config, root / STATE_DIRNAME, root, grants, run_id=run_id, role=role, runner=ssh_runner
    )
    servers_mcp(service, __version__).serve(sys.stdin, sys.stdout)


# ---- jev mcp runs -------------------------------------------------------------------------


@mcp_app.command("runs")
def mcp_runs(
    allow_mutations: Annotated[
        bool,
        typer.Option(
            "--allow-mutations",
            help="Also expose run_start and chat_send; without it the server is read-only.",
        ),
    ] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Serve the pipeline tools (run_list, run_status, run_start, quality_show, chat_send)
    over stdio MCP. Every call is audit-logged (redacted)."""
    with repo_context(repo, create=allow_mutations) as ctx:
        runs_mcp(ctx, __version__, allow_mutations=allow_mutations).serve(sys.stdin, sys.stdout)


# ---- jev browser install -----------------------------------------------------------------


@browser_app.command("install")
def browser_install(
    server: Annotated[
        str | None,
        typer.Option("--server", metavar="NAME", help="Install on this server (medium access)."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the command and exit without running it.")
    ] = False,
    repo: RepoOption = Path("."),
) -> None:
    """Install Playwright's Chromium (and its system libraries) locally or on a server."""
    root, config = _load(repo)
    target = server or config.browser.install_target
    argv = install_argv()
    if target == "local":
        console.print(f"Will run locally: [bold]{escape(shlex.join(argv))}[/bold]")
        console.print("  (downloads Chromium; --with-deps installs system libraries, needing root)")
        if dry_run:
            return
        if not _confirm(yes):
            raise typer.Exit(1)
        code = local_runner(argv)
        if code != 0:
            raise fail(f"the install exited with {code}")
        console.print("Browser installed.")
        return
    settings = config.servers.get(target)
    if settings is None:
        known = ", ".join(config.servers) or "none defined"
        raise fail(f"unknown server {target!r} (known: {known})")
    if settings.access != "medium":
        raise fail(f'installing on {target} needs `access = "medium"` (it is {settings.access})')
    command = shlex.join(argv)
    ctx = PolicyContext(
        "medium",
        settings.workdir,
        tuple(settings.write_roots()),
        tuple(argv),
        project_boundary=settings.project_folder,
    )
    decision = check_command(command, ctx)
    if not decision.allowed:  # e.g. no workdir: npx needs nothing else, so this is a bug guard
        raise fail(f"refused by the access policy: {decision.reason}")
    console.print(f"Will run on {escape(target)} over ssh: [bold]{escape(command)}[/bold]")
    console.print("  (--with-deps needs root there: passwordless sudo or a root login)")
    if dry_run:
        return
    if not _confirm(yes):
        raise typer.Exit(1)
    service = ServerService(
        config, root / STATE_DIRNAME, root, {target: "medium"}, role="user", runner=ssh_runner
    )
    result = service.run_command(target, command, timeout_s=600)
    console.print(escape(result.text))
    if result.is_error:
        raise typer.Exit(1)


def _confirm(yes: bool) -> bool:
    if yes:
        return True
    if not stdin_is_tty():
        console.print("Not a terminal: pass --yes to run it, or --dry-run to only show it.")
        return False
    return typer.confirm("Run it?", default=False)


# ---- jev profile -------------------------------------------------------------------------


@profile_app.command("list")
def profile_list(repo: RepoOption = Path(".")) -> None:
    """List workspace profiles; the repo default is marked with *."""
    root, config = _load(repo)
    profiles = list_profiles(config, root / STATE_DIRNAME)
    if not profiles:
        console.print(
            escape("No workspace profiles. Define them in jev.toml under [profiles.NAME].")
        )
        return
    table = Table(title="workspace profiles")
    for column in ("profile", "servers", "browser", "learning", "quality", "description"):
        table.add_column(column, overflow="fold")
    for profile in profiles:
        table.add_row(
            escape(profile.name + (" *" if profile.is_default else "")),
            escape(profile.servers),
            "yes" if profile.browser else "no",
            escape(profile.learning),
            profile.quality,
            escape(profile.description),
        )
    console.print(table)


@profile_app.command("show")
def profile_show(
    name: Annotated[str, typer.Argument(help="Profile name.")], repo: RepoOption = Path(".")
) -> None:
    """Show a profile and what each role gets from it."""
    root, config = _load(repo)
    try:
        profile = describe_profile(config, root / STATE_DIRNAME, name)
    except ProfileError as exc:
        raise fail(str(exc)) from exc
    console.print(
        f"profile [bold]{escape(name)}[/bold]" + (" (repo default)" if profile.is_default else "")
    )
    if profile.description:
        console.print(f"  {escape(profile.description)}")
    console.print(f"  servers: {escape(profile.servers)}")
    console.print(
        f"  browser: {'yes' if profile.browser else 'no'}; learning: {escape(profile.learning)}"
    )
    console.print(escape(f"  quality: {profile.quality}"))
    for grant in profile.roles:
        console.print(f"  {grant.role}: {escape(grant.text)}")


@profile_app.command("use")
def profile_use(
    name: Annotated[str, typer.Argument(help="Profile name, or `none` to clear the default.")],
    repo: RepoOption = Path("."),
) -> None:
    """Set the repo's default workspace profile (stored in .jev/profile)."""
    root, config = _load(repo)
    state_dir = root / STATE_DIRNAME
    if name == NO_PROFILE:
        set_default_profile(config, state_dir, None)
        console.print("No default workspace profile: runs use none unless --profile is given.")
        return
    try:
        set_default_profile(config, state_dir, name)
    except ProfileError as exc:
        raise fail(str(exc)) from exc
    console.print(f"Default workspace profile: {escape(name)}")


def pick_profile_for_run(config: JevConfig, root: Path, flag: str | None) -> str | None:
    """`--profile` for `jev run`: the flag, else (more than one profile, on a terminal) a
    numbered menu defaulting to the repo default; else None (start_run applies the default)."""
    if flag is not None or len(config.profiles) < 2 or not stdin_is_tty():
        return flag
    default = repo_default(root / STATE_DIRNAME)
    default = default if default in config.profiles else None
    chosen = choose_interactively(
        config, default, lambda text: typer.prompt(text, default="", show_default=False),
        lambda text: console.print(escape(text)),
    )  # fmt: skip
    return NO_PROFILE if chosen is None else chosen
