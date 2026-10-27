"""`jev dashboard --install-service` / `--uninstall-service` (deploy Phase 3, issue #341).

Reboot survival, one mechanism per platform — all of them a *user* mechanism, not a
system one, because the CSRF secret and `secrets.env` live in `~/.jev` and must be
readable by the service user (audit §3, constraint 5):

- macOS: a launchd agent at `~/Library/LaunchAgents/com.baton.jev-dashboard.plist`
  (`RunAtLoad`, `KeepAlive`), loaded with `launchctl load`.
- Linux: a systemd user unit at `~/.config/systemd/user/jev-dashboard.service`
  (`Restart=always`, `WantedBy=default.target`), enabled with
  `systemctl --user enable --now`.
- Windows: a Scheduled Task at logon (`schtasks /create /xml`), not a Windows
  service — no service wrapper exists, and a logon task is simpler and more honest.

Design constraints (audit §3), all enforced here:

1. **Absolute paths only.** The service manager's PATH is minimal (launchd's is
   `/usr/bin:/bin:/usr/sbin:/sbin`; a systemd unit's is barely more), so the
   recorded command is the absolute path to `jev` found at install time
   (`jev_executable_path`, via the Phase 1 `resolve_executable` seam), and the
   directories of the worker CLIs found at install time are embedded as the
   service's PATH (launchd `EnvironmentVariables`, systemd `Environment=PATH=`).
   The dashboard *also* repairs its own PATH at startup (Phase 1), so the
   embedded PATH is belt-and-braces, not the only defence. Windows tasks cannot
   carry environment variables in their XML, so there the Phase 1 startup
   repair is the whole story.
2. **The repo is recorded, and `~` is refused** — the same guard as
   `dashboard_cmd._refuse_home_root`.
3. **Idempotent install, matching uninstall.** Installing twice rewrites the
   same definition and re-enables it; uninstall removes the definition and
   disables/unloads it, and reports "not installed" instead of failing when
   there is nothing to remove.
4. **Stop timeout exceeds the dashboard's shutdown.** The dashboard cancels its
   jobs on shutdown and waits up to 30 s *per job* (`JOB_SHUTDOWN_TIMEOUT_S`),
   so the service definitions give the process 120 s to exit: launchd
   `ExitTimeOut` 120, systemd `TimeoutStopSec=120s`. A scheduled task has no
   stop-timeout setting — `schtasks /end` terminates the process — so on
   Windows the graceful-shutdown guarantee does not hold; run state is durable
   and resumable, which is what bounds the damage, and the XML says so in a
   comment rather than pretending otherwise.
5. **User scope, not system scope** (see the module docstring's first paragraph).

The generators (`launchd_plist`, `systemd_unit`, `scheduled_task_xml`) are pure
functions of a `ServiceSpec` so unit tests can assert their contents without
installing anything; only `install_service` / `uninstall_service` touch the
service manager, and they take a `run` callable so tests can record the
commands instead of running them.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape as xml_escape

from jev_agent.app.errors import JevError
from jev_agent.core.executables import KNOWN_CLIS, resolve_executable

__all__ = [
    "SERVICE_LABEL",
    "SERVICE_NAME",
    "STOP_TIMEOUT_S",
    "ServiceSpec",
    "install_service",
    "jev_executable_path",
    "launchd_plist",
    "scheduled_task_xml",
    "service_file_path",
    "systemd_unit",
    "uninstall_service",
]

# The dashboard waits up to 30 s per job on shutdown (JOB_SHUTDOWN_TIMEOUT_S in
# dashboard/app.py); a service manager that kills the process sooner would kill
# jobs mid-run. 120 s covers several jobs stacking up, with room to spare.
STOP_TIMEOUT_S = 120

SERVICE_NAME = "jev-dashboard"
SERVICE_LABEL = "com.baton.jev-dashboard"

Platform = Literal["darwin", "linux", "win32"]

RunFn = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class ServiceSpec:
    """Everything a service definition needs: the absolute `jev`, the repo, the listen address."""

    jev_path: str
    repo: Path
    host: str = "127.0.0.1"
    port: int = 8765
    # PATH value to embed in the definition (discovered CLI dirs + the install-time
    # PATH), or None when nothing was discovered outside PATH. None means the
    # definition carries no PATH and the dashboard's startup repair does the work.
    cli_path: str | None = None


def dashboard_argv(spec: ServiceSpec) -> list[str]:
    """The exact command the service runs: absolute `jev`, no browser, this repo."""
    return [
        spec.jev_path,
        "dashboard",
        "--no-open",
        "--repo",
        str(spec.repo),
        "--host",
        spec.host,
        "--port",
        str(spec.port),
    ]


def jev_executable_path() -> str:
    """Absolute path to the `jev` binary — the one actually running this process.

    `sys.argv[0]` wins when it names this binary: a service must record the
    `jev` that is running the install, not whichever `jev` happens to resolve
    first on PATH (on the Mac, PATH finds a Homebrew shim to the main venv
    while the install may be running from a different venv). The basename
    check matters: under a test runner `argv[0]` is the runner's binary, not
    `jev`, and under `python -m` it is the module file — both fall through to
    the PATH lookup below, which checks PATH first and then the well-known
    per-platform install locations (the Phase 1 seam).
    """
    argv0 = Path(sys.argv[0])
    if argv0.name in ("jev", "jev.exe"):
        candidate = argv0 if argv0.is_absolute() else Path.cwd() / argv0
        if candidate.is_file():
            return str(candidate.resolve())
    found = resolve_executable("jev")
    if found:
        return found
    raise JevError(
        "could not find the `jev` binary to record in the service definition: "
        "run `jev dashboard --install-service` from a shell where `jev` is on PATH."
    )


def discover_cli_path() -> str | None:
    """A PATH value embedding the worker CLIs found outside PATH, or None.

    Resolves each known CLI the Phase 1 way and prepends the directories that
    are not already on PATH — the same set dashboard startup prepends — so the
    service definition records where `npx`/`claude`/`codex` actually live.
    """
    current = os.environ.get("PATH", "")
    present = set(current.split(os.pathsep)) if current else set()
    added: list[str] = []
    for name in KNOWN_CLIS:
        found = resolve_executable(name)
        if found is None:
            continue
        directory = str(Path(found).parent)
        if directory in present:
            continue
        present.add(directory)
        added.append(directory)
    if not added:
        return None
    prefix = os.pathsep.join(added)
    return f"{prefix}{os.pathsep}{current}" if current else prefix


def current_platform() -> Platform:
    """This machine's service platform, or a clear error on anything else."""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "win32"
    if sys.platform.startswith("linux"):
        return "linux"
    raise JevError(
        f"`jev dashboard --install-service` is not supported on this platform "
        f"({sys.platform}): macOS (launchd), Linux (systemd) and Windows "
        "(Scheduled Task) only."
    )


def service_file_path(platform: Platform, home: Path | None = None) -> Path:
    """Where this platform's service definition lives."""
    home = home or Path.home()
    if platform == "darwin":
        return home / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    if platform == "linux":
        return home / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
    return home / ".jev" / f"{SERVICE_NAME}-task.xml"


# ---------------------------------------------------------------------------
# Generators: pure functions of a spec (unit tests assert their contents).
# ---------------------------------------------------------------------------


def _plist_entry(key: str, value: str, kind: str = "string") -> str:
    return f"\t<key>{xml_escape(key)}</key>\n\t<{kind}>{xml_escape(value)}</{kind}>\n"


def launchd_plist(spec: ServiceSpec, home: Path | None = None) -> str:
    """The launchd agent plist: runs at login, stays alive, 120 s to stop."""
    home = home or Path.home()
    log_out = home / ".jev" / "dashboard-service.out.log"
    log_err = home / ".jev" / "dashboard-service.err.log"
    args = "".join(f"\t\t<string>{xml_escape(arg)}</string>\n" for arg in dashboard_argv(spec))
    env = ""
    if spec.cli_path is not None:
        env = (
            "\t<key>EnvironmentVariables</key>\n"
            "\t<dict>\n"
            f"{_plist_entry('PATH', spec.cli_path)}"
            "\t</dict>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"{_plist_entry('Label', SERVICE_LABEL)}"
        "\t<key>ProgramArguments</key>\n"
        "\t<array>\n"
        f"{args}"
        "\t</array>\n"
        f"{_plist_entry('WorkingDirectory', str(spec.repo))}"
        f"{_plist_entry('StandardOutPath', str(log_out))}"
        f"{_plist_entry('StandardErrorPath', str(log_err))}"
        "\t<key>RunAtLoad</key>\n\t<true/>\n"
        "\t<key>KeepAlive</key>\n\t<true/>\n"
        # launchd sends SIGTERM, then SIGKILL after ExitTimeOut (default 20 s) —
        # the dashboard needs up to 30 s per job to shut down cleanly.
        f"{_plist_entry('ExitTimeOut', str(STOP_TIMEOUT_S), kind='integer')}"
        f"{env}"
        "</dict>\n"
        "</plist>\n"
    )


def systemd_unit(spec: ServiceSpec) -> str:
    """The systemd user unit: restarts on failure, 120 s to stop, starts at login."""
    argv = " ".join(shlex.quote(arg) for arg in dashboard_argv(spec))
    env = f"Environment=PATH={spec.cli_path}\n" if spec.cli_path is not None else ""
    return (
        "# Generated by `jev dashboard --install-service` (deploy Phase 3, issue #341).\n"
        "# Serves one repo; reinstall to point it at another. Do not edit by hand —\n"
        "# `jev dashboard --uninstall-service` removes it.\n"
        "[Unit]\n"
        "Description=jev dashboard (Baton)\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={spec.repo}\n"
        f"ExecStart={argv}\n"
        f"{env}"
        "Restart=always\n"
        "RestartSec=5\n"
        # The dashboard waits up to 30 s per job on shutdown; give it 120 s
        # before SIGKILL so jobs are cancelled cleanly instead of killed mid-run.
        f"TimeoutStopSec={STOP_TIMEOUT_S}s\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _windows_quote(arg: str) -> str:
    """Quote one argument for a Scheduled Task's Arguments string (spaces need it)."""
    if not arg or any(ch.isspace() for ch in arg):
        return '"' + arg.replace('"', '""') + '"'
    return arg


def scheduled_task_xml(spec: ServiceSpec) -> str:
    """The Task Scheduler definition: a logon task running as the logged-in user.

    There is no stop-timeout setting for a scheduled task: `schtasks /end`
    terminates the process, so a stopped task does not get the dashboard's
    30 s-per-job graceful shutdown. Run state is durable and resumable, which
    bounds the damage — but the XML does not pretend otherwise (see the comment
    it carries). Environment variables cannot be carried in task XML either;
    the dashboard's Phase 1 startup PATH repair is the whole story on Windows.
    """
    args = " ".join(_windows_quote(arg) for arg in dashboard_argv(spec)[1:])
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        "<!-- Generated by the jev dashboard install-service flag (deploy Phase 3, issue #341).\n"
        "     A Scheduled Task has no stop-timeout setting: stopping the task terminates\n"
        "     the dashboard at once, so running jobs do not get the 30 s-per-job graceful\n"
        "     shutdown a launchd/systemd stop gives them. Run state is durable and\n"
        "     resumable (`jev resume <run>`), which is what bounds the damage. -->\n"
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>jev dashboard for {xml_escape(str(spec.repo))}</Description>\n"
        f"    <URI>\\{SERVICE_NAME}</URI>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>false</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{xml_escape(spec.jev_path)}</Command>\n"
        f"      <Arguments>{xml_escape(args)}</Arguments>\n"
        f"      <WorkingDirectory>{xml_escape(str(spec.repo))}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def generate_definition(spec: ServiceSpec, platform: Platform, home: Path | None = None) -> str:
    """The service definition text for `platform`."""
    if platform == "darwin":
        return launchd_plist(spec, home)
    if platform == "linux":
        return systemd_unit(spec)
    return scheduled_task_xml(spec)


# ---------------------------------------------------------------------------
# Install / uninstall: write the definition, then talk to the service manager.
# ---------------------------------------------------------------------------


def _run_checked(
    run: RunFn, argv: list[str], what: str, tolerate_missing: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a service-manager command; a failure is a clear `JevError`, not a traceback.

    `tolerate_missing` is for the unload/disable half of an idempotent install or
    an uninstall: "not loaded" is not an error there.
    """
    result = run(argv)
    if result.returncode != 0 and not tolerate_missing:
        detail = (result.stderr or result.stdout or "").strip()
        hint = (
            " — is the service manager available on this machine?"
            if resolve_executable(argv[0]) is None
            else ""
        )
        raise JevError(f"`{' '.join(argv)}` failed ({what}): {detail or 'no output'}{hint}")
    return result


def install_service(
    spec: ServiceSpec,
    platform: Platform | None = None,
    home: Path | None = None,
    run: RunFn | None = None,
) -> Path:
    """Write the service definition and enable it. Idempotent: a second install
    rewrites the same definition and re-enables it. Returns the definition's path."""
    platform = platform or current_platform()
    home = home or Path.home()
    run = run or _default_run
    path = service_file_path(platform, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoding = "utf-16" if platform == "win32" else "utf-8"
    path.write_text(generate_definition(spec, platform, home), encoding=encoding)
    if platform == "darwin":
        # `load` fails when the job is already loaded: unload first, tolerating
        # "not loaded" — that is what makes a reinstall idempotent.
        _run_checked(run, ["launchctl", "unload", str(path)], "unloading the old agent", True)
        _run_checked(run, ["launchctl", "load", str(path)], "loading the agent")
    elif platform == "linux":
        _run_checked(run, ["systemctl", "--user", "daemon-reload"], "reloading systemd")
        _run_checked(
            run,
            ["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service"],
            "enabling the service",
        )
    else:
        _run_checked(
            run,
            ["schtasks", "/create", "/tn", SERVICE_NAME, "/xml", str(path), "/f"],
            "registering the scheduled task",
        )
    return path


def uninstall_service(
    platform: Platform | None = None,
    home: Path | None = None,
    run: RunFn | None = None,
) -> Path | None:
    """Disable/unload the service and remove its definition.

    Returns the removed definition's path, or None when nothing was installed.
    The manager call is attempted even when the definition file is gone (a
    hand-deleted file must not leave a loaded job behind); failures there are
    tolerated — "not loaded" is the expected case for an uninstall.
    """
    platform = platform or current_platform()
    home = home or Path.home()
    run = run or _default_run
    path = service_file_path(platform, home)
    existed = path.exists()
    if platform == "darwin":
        _run_checked(run, ["launchctl", "remove", SERVICE_LABEL], "unloading the agent", True)
    elif platform == "linux":
        _run_checked(
            run,
            ["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"],
            "disabling the service",
            True,
        )
    else:
        _run_checked(
            run,
            ["schtasks", "/delete", "/tn", SERVICE_NAME, "/f"],
            "deleting the scheduled task",
            True,
        )
    if existed:
        path.unlink()
        return path
    return None


def describe_service(platform: Platform) -> str:
    """One line naming the mechanism, for the CLI's output."""
    if platform == "darwin":
        return f"launchd agent ({SERVICE_LABEL})"
    if platform == "linux":
        return f"systemd user unit ({SERVICE_NAME}.service)"
    return f"Scheduled Task at logon ({SERVICE_NAME})"
