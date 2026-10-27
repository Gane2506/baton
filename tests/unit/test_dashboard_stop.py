"""`jev dashboard stop` and the pid file (issue #333).

The running dashboard records `<repo>/.jev/dashboard.pid`, so a second start on the
same port names the owner and points at `jev dashboard stop` instead of `lsof`.

`stop` means stopped (issue #355): after the process is gone, `stop` waits until the
port is bindable again before reporting success, and the start probe binds the way
uvicorn binds (SO_REUSEADDR), so TIME_WAIT left by the previous server does not read
as a busy port.
"""

import contextlib
import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli import dashboard_cmd
from jev_agent.cli.app import app

runner = CliRunner()


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


def _dead_pid() -> int:
    """The pid of a process that has already exited (a stale pid file's case)."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    pid = proc.pid
    assert isinstance(pid, int)
    return pid


def _double_forked_sleeper() -> int:
    """A `sleep 30` reparented to init, so that once killed it is reaped by init
    instead of lingering as this process's zombie — `pid_alive` (kill(pid, 0))
    still sees a zombie, which is not the dashboard's real stop case (its
    parent is a shell, not the stopper). POSIX only."""
    rpipe, wpipe = os.pipe()
    first = os.fork()
    if first == 0:  # first child: fork again, then exit at once
        try:
            second = os.fork()
            if second == 0:  # grandchild: the sleeper, reparented to init
                os.setsid()
                os.execvp("sleep", ["sleep", "30"])
            os.write(wpipe, str(second).encode())
        finally:
            os._exit(0)
    os.close(wpipe)
    os.waitpid(first, 0)
    grandchild = int(os.read(rpipe, 32).decode())
    os.close(rpipe)
    return grandchild


def _leave_port_in_time_wait(port: int) -> None:
    """Serve one connection on `port` and close it first, leaving the server
    side in TIME_WAIT: a plain bind then fails EADDRINUSE, while a SO_REUSEADDR
    bind (uvicorn's terms) succeeds. The state the previous dashboard leaves
    behind on every shutdown that served a request."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)

    def _accept_and_close() -> None:
        conn, _ = srv.accept()
        time.sleep(0.1)
        conn.close()  # server closes first -> server side TIME_WAIT

    thread = threading.Thread(target=_accept_and_close)
    thread.start()
    cli = socket.socket()
    cli.connect(("127.0.0.1", port))
    thread.join()
    cli.close()
    srv.close()
    time.sleep(0.2)  # let the FIN exchange finish


def _write_pidfile(repo: Path, pid: int, port: int) -> Path:
    path = repo / ".jev" / "dashboard.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "host": "127.0.0.1",
                "port": port,
                "started_at": "2026-10-27T05:00:00+0000",
                "argv": ["jev", "dashboard"],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_stop_without_pidfile(fixture_repo: Path) -> None:
    result = runner.invoke(app, ["dashboard", "stop", "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    assert "no dashboard recorded" in result.output


def test_stop_with_stale_pidfile_removes_it(fixture_repo: Path) -> None:
    path = _write_pidfile(fixture_repo, _dead_pid(), _free_port())
    result = runner.invoke(app, ["dashboard", "stop", "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    assert "no dashboard running" in result.output
    assert "stale pid file" in result.output
    assert not path.exists()


def test_stop_with_unreadable_pidfile_removes_it(fixture_repo: Path) -> None:
    path = fixture_repo / ".jev" / "dashboard.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    result = runner.invoke(app, ["dashboard", "stop", "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    assert "no dashboard recorded" in result.output


def test_stop_live_process(fixture_repo: Path) -> None:
    """Stop a real process. POSIX only: the Windows stop path is proven by the
    Windows CI job's live checks."""
    if os.name != "posix":
        pytest.skip("double-fork sleeper needs POSIX")
    grandchild = _double_forked_sleeper()
    try:
        _write_pidfile(fixture_repo, grandchild, _free_port())
        result = runner.invoke(app, ["dashboard", "stop", "--repo", str(fixture_repo)])
        assert result.exit_code == 0, result.output
        assert f"stopped the dashboard (pid {grandchild})" in result.output
        assert not (fixture_repo / ".jev" / "dashboard.pid").exists()
    finally:
        # The stop should have killed it; make sure no sleeper survives the test.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(grandchild, signal.SIGKILL)


def test_stop_fails_when_port_stays_busy(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process being gone is not the same as the port being free (issue #355):
    when something else still holds the port after the dashboard's pid dies,
    `stop` must fail with a port-busy error, not claim success. POSIX only."""
    if os.name != "posix":
        pytest.skip("double-fork sleeper needs POSIX")
    monkeypatch.setattr(dashboard_cmd, "_PORT_FREE_WAIT_S", 1.0)
    grandchild = _double_forked_sleeper()
    port = _free_port()
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    try:
        _write_pidfile(fixture_repo, grandchild, port)
        result = runner.invoke(app, ["dashboard", "stop", "--repo", str(fixture_repo)])
    finally:
        holder.close()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(grandchild, signal.SIGKILL)
    assert result.exit_code == 1, result.output
    flat = " ".join(result.output.split())
    assert f"stopped the dashboard (pid {grandchild}), but its port is still busy" in flat
    assert f"127.0.0.1:{port}" in flat
    # The pid is dead, so the pid file is stale and goes — the failure is about
    # the port, not the process.
    assert not (fixture_repo / ".jev" / "dashboard.pid").exists()


def test_stop_waits_for_port_to_free(fixture_repo: Path) -> None:
    """`stop` waits (bounded) for the port, not just the pid (issue #355): the
    port frees 0.6 s after the pid dies, and `stop` succeeds — and a bind
    right after it returns works with no sleep or retry in the caller.
    POSIX only."""
    if os.name != "posix":
        pytest.skip("double-fork sleeper needs POSIX")
    grandchild = _double_forked_sleeper()
    port = _free_port()
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    threading.Timer(0.6, holder.close).start()
    try:
        _write_pidfile(fixture_repo, grandchild, port)
        result = runner.invoke(app, ["dashboard", "stop", "--repo", str(fixture_repo)])
    finally:
        holder.close()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(grandchild, signal.SIGKILL)
    assert result.exit_code == 0, result.output
    assert f"stopped the dashboard (pid {grandchild})" in result.output
    assert not (fixture_repo / ".jev" / "dashboard.pid").exists()
    # No sleep or retry: the port is bindable the moment `stop` returns.
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


def test_start_succeeds_when_port_in_time_wait(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start probe binds the way uvicorn binds (issue #355): a port left in
    TIME_WAIT by the previous server's closed connections does not read as
    busy, so a start right after a stop works first time."""
    import uvicorn

    seen: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        seen["pidfile"] = dashboard_cmd.read_dashboard_pid(fixture_repo)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    port = _free_port()
    _leave_port_in_time_wait(port)
    result = runner.invoke(
        app,
        ["dashboard", "--no-open", "--port", str(port), "--repo", str(fixture_repo)],
    )
    assert result.exit_code == 0, result.output
    info = seen["pidfile"]
    assert isinstance(info, dict)
    assert info["port"] == port


def test_start_writes_pidfile_and_removes_it_on_exit(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    seen: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        # While "serving", the pid file must exist and name this process.
        seen["pidfile"] = dashboard_cmd.read_dashboard_pid(fixture_repo)
        seen["host"] = kwargs.get("host")
        seen["port"] = kwargs.get("port")

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = runner.invoke(app, ["dashboard", "--no-open", "--repo", str(fixture_repo)])
    assert result.exit_code == 0, result.output
    info = seen["pidfile"]
    assert isinstance(info, dict)
    assert info["pid"] == os.getpid()
    assert info["port"] == 8765
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 8765
    # A clean exit removes the pid file, so the next start does not see a stale one.
    assert not (fixture_repo / ".jev" / "dashboard.pid").exists()


def test_busy_port_names_the_owner(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    port = _free_port()
    _write_pidfile(fixture_repo, os.getpid(), port)
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))
    try:
        result = runner.invoke(
            app,
            ["dashboard", "--no-open", "--port", str(port), "--repo", str(fixture_repo)],
        )
    finally:
        holder.close()
    assert result.exit_code == 1, result.output
    # Rich wraps the long error line at the terminal width, so the words may be
    # split across lines: compare with whitespace flattened.
    flat = " ".join(result.output.split())
    assert f"already running as pid {os.getpid()}" in flat
    assert "jev dashboard stop" in flat


def test_busy_port_without_pidfile_is_generic(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    port = _free_port()
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))
    try:
        result = runner.invoke(
            app,
            ["dashboard", "--no-open", "--port", str(port), "--repo", str(fixture_repo)],
        )
    finally:
        holder.close()
    assert result.exit_code == 1, result.output
    assert "Stop the process already using that port" in result.output
    assert "already running as pid" not in result.output


def test_dashboard_help_lists_stop() -> None:
    result = runner.invoke(app, ["dashboard", "--help"])
    assert result.exit_code == 0, result.output
    assert "stop" in result.output


def test_options_before_stop_are_rejected_not_dropped(fixture_repo: Path) -> None:
    """`jev dashboard --repo X stop` must fail loudly: the callback's options do
    not reach the subcommand, so silently ignoring them would stop the wrong repo."""
    result = runner.invoke(app, ["dashboard", "--repo", str(fixture_repo), "stop"])
    assert result.exit_code == 1, result.output
    assert "options belong after the subcommand" in " ".join(result.output.split())
