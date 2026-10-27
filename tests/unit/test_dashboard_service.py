"""`jev dashboard --install-service` / `--uninstall-service` (issue #341).

The generators are pure functions of a spec, so their contents (absolute paths,
repo, stop timeouts) are asserted without installing anything; installing only
ever runs against a recording runner and a fake home. Nothing here touches a
real service manager.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.app.errors import JevError
from jev_agent.cli import dashboard_cmd, service
from jev_agent.cli.app import app

runner = CliRunner()

JEV = "/usr/local/bin/jev"
REPO = Path("/tmp/myrepo")
WIN_JEV = r"C:\Users\test\.local\bin\jev.exe"
WIN_REPO = Path(r"C:\Users\test\code\myrepo")


class Recorder:
    """A fake `subprocess.run`: records argv, answers 0 unless told otherwise."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail: set[tuple[str, ...]] = set()

    def __call__(self, argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert isinstance(argv, list)
        self.calls.append(list(argv))
        code = 1 if tuple(argv) in self.fail else 0
        return subprocess.CompletedProcess(argv, code, stdout="", stderr="mock failure")


def _spec(**overrides: object) -> service.ServiceSpec:
    base: dict[str, object] = {
        "jev_path": JEV,
        "repo": REPO,
        "host": "127.0.0.1",
        "port": 8765,
        "cli_path": "/opt/homebrew/bin:/usr/bin:/bin",
    }
    base.update(overrides)
    return service.ServiceSpec(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def test_launchd_plist_is_a_valid_plist_with_absolute_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    parsed = plistlib.loads(service.launchd_plist(_spec(), home).encode("utf-8"))
    assert parsed["Label"] == "com.baton.jev-dashboard"
    assert parsed["ProgramArguments"] == [
        JEV,
        "dashboard",
        "--no-open",
        "--repo",
        str(REPO),
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
    ]
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    # The stop timeout must exceed the dashboard's 30 s-per-job shutdown.
    assert parsed["ExitTimeOut"] == service.STOP_TIMEOUT_S
    assert parsed["ExitTimeOut"] > 30
    assert parsed["WorkingDirectory"] == str(REPO)
    assert parsed["StandardOutPath"].startswith(str(home / ".jev"))
    assert parsed["StandardErrorPath"].startswith(str(home / ".jev"))
    # The discovered CLI dirs are embedded: launchd's PATH is minimal.
    assert parsed["EnvironmentVariables"]["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin"


def test_launchd_plist_without_discovered_clis_has_no_path_env() -> None:
    parsed = plistlib.loads(service.launchd_plist(_spec(cli_path=None)).encode("utf-8"))
    assert "EnvironmentVariables" not in parsed


def test_systemd_unit() -> None:
    unit = service.systemd_unit(_spec())
    assert f"ExecStart={JEV} dashboard --no-open --repo {REPO} --host 127.0.0.1 --port 8765" in unit
    assert "Restart=always" in unit
    assert "RestartSec=5" in unit
    assert f"TimeoutStopSec={service.STOP_TIMEOUT_S}s" in unit
    assert service.STOP_TIMEOUT_S > 30
    assert "WantedBy=default.target" in unit
    assert "Environment=PATH=/opt/homebrew/bin:/usr/bin:/bin" in unit
    assert "WorkingDirectory=" + str(REPO) in unit


def test_systemd_unit_without_discovered_clis_has_no_path_env() -> None:
    assert "Environment=PATH=" not in service.systemd_unit(_spec(cli_path=None))


def test_scheduled_task_xml() -> None:
    # The XML declares UTF-16 (what schtasks wants), so parse bytes, not str.
    root = ET.fromstring(
        service.scheduled_task_xml(_spec(jev_path=WIN_JEV, repo=WIN_REPO)).encode("utf-16")
    )
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find("t:Triggers/t:LogonTrigger", ns) is not None
    assert root.findtext("t:Actions/t:Exec/t:Command", namespaces=ns) == WIN_JEV
    args = root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
    assert args is not None
    assert "dashboard --no-open" in args
    assert str(WIN_REPO) in args
    assert root.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=ns) == str(WIN_REPO)
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=ns) == "PT0S"
    assert root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=ns) == "IgnoreNew"
    # InteractiveToken: the task runs as the logged-in user, who owns ~/.jev.
    assert (
        root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns) == "InteractiveToken"
    )


def test_dashboard_argv_is_absolute_and_names_the_repo() -> None:
    assert service.dashboard_argv(_spec()) == [
        JEV,
        "dashboard",
        "--no-open",
        "--repo",
        str(REPO),
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
    ]


def test_service_file_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert service.service_file_path("darwin", home) == (
        home / "Library" / "LaunchAgents" / "com.baton.jev-dashboard.plist"
    )
    assert service.service_file_path("linux", home) == (
        home / ".config" / "systemd" / "user" / "jev-dashboard.service"
    )
    assert service.service_file_path("win32", home) == home / ".jev" / "jev-dashboard-task.xml"


# ---------------------------------------------------------------------------
# Executable resolution
# ---------------------------------------------------------------------------


def test_jev_executable_path_prefers_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "jev"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert service.jev_executable_path() == str(fake)


def test_jev_executable_path_prefers_the_running_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service must record the `jev` running the install, not whichever
    `jev` PATH resolves first (on the Mac, PATH finds a Homebrew shim to the
    main venv while the install may run from another venv — issue #341)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    path_jev = bindir / "jev"
    path_jev.write_text("#!/bin/sh\n")
    path_jev.chmod(0o755)
    otherdir = tmp_path / "other"
    otherdir.mkdir()
    running_jev = otherdir / "jev"
    running_jev.write_text("#!/bin/sh\n")
    running_jev.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(sys, "argv", [str(running_jev), "dashboard"])
    assert service.jev_executable_path() == str(running_jev)


def test_jev_executable_path_falls_back_to_argv0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no `jev` on PATH
    script = tmp_path / "jev"
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(script), "dashboard"])
    assert service.jev_executable_path() == str(script)


def test_jev_executable_path_fails_when_nowhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["jev", "dashboard"])
    with pytest.raises(JevError, match="could not find the `jev` binary"):
        service.jev_executable_path()


def test_discover_cli_path_finds_clis_outside_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    npx = bindir / "npx"
    npx.write_text("#!/bin/sh\n")
    npx.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    # The machine's own search dirs may hold a real `npx` that shadows the
    # fake one (CI's /usr/local/bin does), so pin the fallback to this dir.
    # `_platform_search_dirs` lives in core.executables, where
    # `resolve_executable` looks it up.
    monkeypatch.setattr("jev_agent.core.executables._platform_search_dirs", lambda: [bindir])
    found = service.discover_cli_path()
    assert found is not None
    assert found.startswith(str(bindir) + os.pathsep)
    assert found.endswith("/usr/bin:/bin")


def test_discover_cli_path_is_none_when_everything_is_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "resolve_executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert service.discover_cli_path() is None


# ---------------------------------------------------------------------------
# Install / uninstall with a recording runner (no real service manager)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_install_writes_definition_and_enables(tmp_path: Path, platform: service.Platform) -> None:
    home = tmp_path / "home"
    rec = Recorder()
    path = service.install_service(_spec(), platform, home, run=rec)
    assert path == service.service_file_path(platform, home)
    assert path.exists()
    text = path.read_text(encoding="utf-16" if platform == "win32" else "utf-8")
    assert JEV in text and str(REPO) in text
    if platform == "darwin":
        assert rec.calls[0] == ["launchctl", "unload", str(path)]
        assert rec.calls[1] == ["launchctl", "load", str(path)]
    elif platform == "linux":
        assert rec.calls[0] == ["systemctl", "--user", "daemon-reload"]
        assert rec.calls[1] == [
            "systemctl",
            "--user",
            "enable",
            "--now",
            "jev-dashboard.service",
        ]
    else:
        assert rec.calls[0] == [
            "schtasks",
            "/create",
            "/tn",
            "jev-dashboard",
            "/xml",
            str(path),
            "/f",
        ]


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_install_is_idempotent(tmp_path: Path, platform: service.Platform) -> None:
    home = tmp_path / "home"
    rec = Recorder()
    first = service.install_service(_spec(), platform, home, run=rec)
    before = first.read_bytes()
    second = service.install_service(_spec(), platform, home, run=rec)
    assert second == first
    assert second.read_bytes() == before
    # The second install ran the same enable commands again (reload, not a duplicate).
    assert len(rec.calls) == 2 * (2 if platform != "win32" else 1)


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_uninstall_removes_definition_and_disables(
    tmp_path: Path, platform: service.Platform
) -> None:
    home = tmp_path / "home"
    rec = Recorder()
    path = service.install_service(_spec(), platform, home, run=rec)
    rec.calls.clear()
    assert service.uninstall_service(platform, home, run=rec) == path
    assert not path.exists()
    if platform == "darwin":
        assert rec.calls == [["launchctl", "remove", "com.baton.jev-dashboard"]]
    elif platform == "linux":
        assert rec.calls == [["systemctl", "--user", "disable", "--now", "jev-dashboard.service"]]
    else:
        assert rec.calls == [["schtasks", "/delete", "/tn", "jev-dashboard", "/f"]]


def test_uninstall_when_nothing_installed_reports_none(tmp_path: Path) -> None:
    home = tmp_path / "home"
    rec = Recorder()
    assert service.uninstall_service("linux", home, run=rec) is None
    # The disable was still attempted (a hand-deleted file must not leave a
    # loaded unit behind); its failure is tolerated.
    assert rec.calls == [["systemctl", "--user", "disable", "--now", "jev-dashboard.service"]]


def test_install_failure_is_a_clear_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    rec = Recorder()
    rec.fail.add(("systemctl", "--user", "daemon-reload"))
    with pytest.raises(JevError, match="systemctl --user daemon-reload"):
        service.install_service(_spec(), "linux", home, run=rec)
    # The definition was still written — the failure is in enabling, not in it.
    assert service.service_file_path("linux", home).exists()


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_install_service_flag(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_install(
        spec: service.ServiceSpec,
        platform: service.Platform | None = None,
        home: Path | None = None,
        run: service.RunFn | None = None,
    ) -> Path:
        seen["spec"] = spec
        seen["platform"] = platform
        return Path("/fake/.config/systemd/user/jev-dashboard.service")

    monkeypatch.setattr(service, "current_platform", lambda: "linux")
    monkeypatch.setattr(service, "jev_executable_path", lambda: JEV)
    monkeypatch.setattr(service, "discover_cli_path", lambda: None)
    monkeypatch.setattr(service, "install_service", fake_install)
    result = runner.invoke(
        app,
        ["dashboard", "--install-service", "--repo", str(fixture_repo)],
    )
    assert result.exit_code == 0, result.output
    assert "installed the dashboard as a systemd user unit" in result.output
    assert "jev-dashboard.service" in result.output
    assert "Remove it with `jev dashboard --uninstall-service`" in result.output
    spec = seen["spec"]
    assert isinstance(spec, service.ServiceSpec)
    assert spec.repo == fixture_repo
    assert spec.jev_path == JEV
    assert Path(spec.jev_path).is_absolute()


def test_install_service_refuses_home(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(fixture_repo))
    monkeypatch.setattr(service, "current_platform", lambda: "linux")
    result = runner.invoke(
        app,
        ["dashboard", "--install-service", "--repo", str(fixture_repo)],
    )
    assert result.exit_code == 1, result.output
    assert "refusing to serve your home directory" in " ".join(result.output.split())


def test_uninstall_service_flag_needs_no_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uninstall removes the service by label: it must work from a directory
    that is not a repo at all (running it from $HOME used to fail on the
    serve path's home-repo refusal — issue #341)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(service, "current_platform", lambda: "linux")
    monkeypatch.setattr(
        service, "uninstall_service", lambda platform=None, home=None, run=None: None
    )
    result = runner.invoke(app, ["dashboard", "--uninstall-service"])
    assert result.exit_code == 0, result.output
    assert "no dashboard service installed" in result.output


def test_uninstall_service_flag_reports_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "current_platform", lambda: "darwin")
    monkeypatch.setattr(
        service,
        "uninstall_service",
        lambda platform=None, home=None, run=None: Path(
            "/fake/Library/LaunchAgents/com.baton.jev-dashboard.plist"
        ),
    )
    result = runner.invoke(app, ["dashboard", "--uninstall-service"])
    assert result.exit_code == 0, result.output
    assert "removed the dashboard service" in result.output


def test_uninstall_service_flag_reports_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "current_platform", lambda: "linux")
    monkeypatch.setattr(
        service, "uninstall_service", lambda platform=None, home=None, run=None: None
    )
    result = runner.invoke(app, ["dashboard", "--uninstall-service"])
    assert result.exit_code == 0, result.output
    assert "no dashboard service installed" in result.output


def test_both_service_flags_rejected(fixture_repo: Path) -> None:
    result = runner.invoke(
        app,
        ["dashboard", "--install-service", "--uninstall-service", "--repo", str(fixture_repo)],
    )
    assert result.exit_code == 1, result.output
    assert "only one of --install-service and --uninstall-service" in " ".join(
        result.output.split()
    )


def test_service_flags_before_a_subcommand_are_rejected(fixture_repo: Path) -> None:
    result = runner.invoke(app, ["dashboard", "--install-service", "stop"])
    assert result.exit_code == 1, result.output
    assert "options belong after the subcommand" in " ".join(result.output.split())


def test_dashboard_help_lists_service_flags() -> None:
    """The flags are listed by `jev dashboard --help`.

    The terminal width is forced wide: Rich truncates long option names when
    it is narrow (CI renders `--open --no-…`), so the literal flag string is
    not always in `--help` output.
    """
    result = runner.invoke(app, ["dashboard", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "--install-service" in result.output
    assert "--uninstall-service" in result.output


def test_refuse_home_root_directly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(JevError, match="refusing to serve your home directory"):
        dashboard_cmd._refuse_home_root(tmp_path)
