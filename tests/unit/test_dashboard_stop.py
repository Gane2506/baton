"""`jev dashboard stop` and the pid file (issue #333).

The running dashboard records `<repo>/.jev/dashboard.pid`, so a second start on the
same port names the owner and points at `jev dashboard stop` instead of `lsof`.

`stop` means stopped (issue #355): after the process is gone, `stop` waits until the
port is bindable again before reporting success, and the start probe binds the way
uvicorn binds (SO_REUSEADDR), so TIME_WAIT left by the previous server does not read
as a busy port.

A dashboard with no pid file is not invisible to `stop` (issue #362): it falls back
to the port itself — identifying the listener over HTTP and reading the repo it
serves — so a pre-existing instance is stoppable without PID hunting, while
another repo's dashboard on the same port is reported, never killed.
"""

import contextlib
import http.server
import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
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


def test_stop_without_pidfile_and_free_port(fixture_repo: Path) -> None:
    """No pid file and nothing listening: the old clean message stands. The
    port is explicit: the default 8765 must not be assumed free — another
    test (or another repo's dashboard) may hold it, and the fallback must
    not be judged on a port it does not own."""
    result = runner.invoke(
        app,
        ["dashboard", "stop", "--repo", str(fixture_repo), "--port", str(_free_port())],
    )
    assert result.exit_code == 0, result.output
    assert "no dashboard recorded" in result.output


def _serve(
    directory: Path, port: int, marker: bool, root: str | None = None
) -> subprocess.Popen[str]:
    """A `python -m http.server` on `port` whose index page does (or does not)
    carry the dashboard's `<title>Baton` marker, and — when `root` is given —
    the sidebar's `<div>{{ root }}</div>` naming the repo it serves. Its
    process is a stand-in for a dashboard that never wrote a pid file."""
    directory.mkdir(parents=True, exist_ok=True)
    title = "Baton · Board" if marker else "Some other page"
    aside = f"<aside><div>{root}</div></div></aside>" if root is not None else ""
    (directory / "index.html").write_text(
        f"<!doctype html><html><head><title>{title}</title></head><body>{aside}hi</body></html>",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(directory),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    # Reap it like a shell parent would: a child killed by `stop` lingers as
    # a zombie until reaped, and `pid_alive` (kill(pid, 0)) still sees a
    # zombie — the same reason `test_stop_live_process` double-forks.
    threading.Thread(target=proc.wait, daemon=True).start()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.0) as resp:
                if resp.status == 200:
                    return proc
        except OSError:
            time.sleep(0.1)
    proc.terminate()
    raise AssertionError(f"test server on port {port} never came up")


def _server_gone(proc: subprocess.Popen[str]) -> bool:
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        return False
    return True


def test_stop_by_port_stops_live_dashboard_without_pidfile(
    fixture_repo: Path, tmp_path: Path
) -> None:
    """Issue #362: no pid file, but a dashboard for this repo is listening —
    `stop` finds it on the port and stops it, with no manual PID hunting."""
    port = _free_port()
    proc = _serve(tmp_path / "site", port, marker=True, root=str(fixture_repo))
    try:
        result = runner.invoke(
            app,
            ["dashboard", "stop", "--repo", str(fixture_repo), "--port", str(port)],
        )
        assert result.exit_code == 0, result.output
        assert f"stopped the dashboard (pid {proc.pid})" in result.output
        assert "no pid file" in result.output
        assert _server_gone(proc), "stop did not kill the server"
        assert dashboard_cmd._port_bindable("127.0.0.1", port)
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.terminate()


def test_stop_by_port_leaves_other_repos_dashboard_alone(
    fixture_repo: Path, tmp_path: Path
) -> None:
    """Issue #362: the listener is a jev dashboard, but for another repo —
    `stop` reports it and leaves it alone. Stopping it would stop a
    dashboard this repo does not own."""
    port = _free_port()
    proc = _serve(tmp_path / "site", port, marker=True, root=str(tmp_path / "other-repo"))
    try:
        result = runner.invoke(
            app,
            ["dashboard", "stop", "--repo", str(fixture_repo), "--port", str(port)],
        )
        assert result.exit_code == 0, result.output
        assert "not this repo's dashboard" in result.output
        assert str(tmp_path / "other-repo") in result.output
        assert proc.poll() is None, "stop killed another repo's dashboard"
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.terminate()
        _server_gone(proc)


def test_stop_by_port_leaves_non_dashboard_alone(fixture_repo: Path, tmp_path: Path) -> None:
    """Issue #362: a listener that is not a jev dashboard is reported, never
    killed — even though there is no pid file to tell `stop` apart."""
    port = _free_port()
    proc = _serve(tmp_path / "site", port, marker=False)
    try:
        result = runner.invoke(
            app,
            ["dashboard", "stop", "--repo", str(fixture_repo), "--port", str(port)],
        )
        assert result.exit_code == 0, result.output
        assert "not a jev dashboard" in result.output
        assert proc.poll() is None, "stop killed a process that is not a dashboard"
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.terminate()
        _server_gone(proc)


class _GarbageHandler(socketserver.BaseRequestHandler):
    """Speaks an SSH banner, not HTTP — the listener `stop` must report,
    never crash on."""

    def handle(self) -> None:
        self.request.sendall(b"SSH-2.0-OpenSSH_8.9\r\n")
        self.request.close()


def _garbage_listener(port: int) -> socketserver.ThreadingTCPServer:
    server = socketserver.ThreadingTCPServer(
        ("127.0.0.1", port), _GarbageHandler, bind_and_activate=False
    )
    server.allow_reuse_address = True
    server.server_bind()
    server.server_activate()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_stop_by_port_reports_garbage_speaking_listener(fixture_repo: Path) -> None:
    """Issue #362: a listener that speaks no HTTP raises BadStatusLine out of
    the probe (an HTTPException, not an OSError) — `stop` must report it,
    never crash, and leave it alone."""
    port = _free_port()
    server = _garbage_listener(port)
    try:
        assert dashboard_cmd._dashboard_served_root("127.0.0.1", port) is None
        result = runner.invoke(
            app,
            ["dashboard", "stop", "--repo", str(fixture_repo), "--port", str(port)],
        )
        assert result.exit_code == 0, result.output
        assert "not a jev dashboard" in result.output
        # Still listening: the banner answers a fresh connection.
        with socket.create_connection(("127.0.0.1", port), timeout=2.0) as conn:
            assert conn.recv(64).startswith(b"SSH-2.0-")
    finally:
        server.shutdown()
        server.server_close()


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Redirects `/` to a marker page: the probe must identify the listener
    itself, not content served from wherever it redirects to."""

    def do_GET(self) -> None:
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/target")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<!doctype html><html><head><title>Baton \xc2\xb7 Board</title></head>"
            b"<body><aside><div>/some/repo</div></div></aside></body></html>"
        )

    def log_message(self, *args: object) -> None:
        pass


def test_dashboard_served_root_ignores_redirect() -> None:
    """The probe does not follow redirects: a listener whose `/` redirects
    to a dashboard page is not identified as one."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert dashboard_cmd._dashboard_served_root("127.0.0.1", server.server_port) is None
    finally:
        server.shutdown()
        server.server_close()


def test_stop_stale_pidfile_finds_live_dashboard_on_recorded_port(
    fixture_repo: Path, tmp_path: Path
) -> None:
    """Issue #362: the pid file is stale (its pid is dead) but a dashboard for
    this repo is listening on the recorded port — `stop` finds it there."""
    port = _free_port()
    proc = _serve(tmp_path / "site", port, marker=True, root=str(fixture_repo))
    try:
        path = _write_pidfile(fixture_repo, _dead_pid(), port)
        result = runner.invoke(app, ["dashboard", "stop", "--repo", str(fixture_repo)])
        assert result.exit_code == 0, result.output
        assert "no dashboard running" in result.output
        assert "stale pid file" in result.output
        assert f"stopped the dashboard (pid {proc.pid})" in result.output
        assert _server_gone(proc), "stop did not kill the server"
        assert not path.exists()
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.terminate()


def test_listening_pid_finds_local_listener() -> None:
    """The port-to-pid lookup names this process for a socket it holds."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    probe.listen(1)
    try:
        assert dashboard_cmd._listening_pid(probe.getsockname()[1]) == os.getpid()
    finally:
        probe.close()


def test_dashboard_served_root(tmp_path: Path) -> None:
    """The HTTP probe reads the repo a listener serves: the root string for
    a dashboard page with the sidebar root, "" for a dashboard page without
    one, None for anything else or nothing listening."""
    port = _free_port()
    marker_proc = _serve(tmp_path / "marker", port, marker=True, root="/some/repo")
    no_root_port = _free_port()
    no_root_proc = _serve(tmp_path / "noroot", no_root_port, marker=True)
    other_port = _free_port()
    other_proc = _serve(tmp_path / "other", other_port, marker=False)
    try:
        assert dashboard_cmd._dashboard_served_root("127.0.0.1", port) == "/some/repo"
        assert dashboard_cmd._dashboard_served_root("127.0.0.1", no_root_port) == ""
        assert dashboard_cmd._dashboard_served_root("127.0.0.1", other_port) is None
        assert dashboard_cmd._dashboard_served_root("127.0.0.1", _free_port()) is None
    finally:
        for proc in (marker_proc, no_root_proc, other_proc):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                proc.terminate()
            _server_gone(proc)


def test_parse_lsof_pids() -> None:
    assert dashboard_cmd._parse_lsof_pids("123\n456\n") == [123, 456]
    assert dashboard_cmd._parse_lsof_pids("") == []
    assert dashboard_cmd._parse_lsof_pids("  78  \nnot-a-pid\n90\n") == [78, 90]


def test_parse_netstat_pid() -> None:
    text = (
        "Active Connections\n"
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    127.0.0.1:8765         0.0.0.0:0              LISTENING       1234\n"
        "  TCP    127.0.0.1:9000         0.0.0.0:0              LISTENING       5678\n"
        "  TCP    127.0.0.1:8766         127.0.0.1:50000        ESTABLISHED     9999\n"
    )
    assert dashboard_cmd._parse_netstat_pid(text, 8765) == 1234
    assert dashboard_cmd._parse_netstat_pid(text, 9000) == 5678
    assert dashboard_cmd._parse_netstat_pid(text, 8766) is None
    assert dashboard_cmd._parse_netstat_pid(text, 1234) is None


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
    result = runner.invoke(
        app,
        ["dashboard", "stop", "--repo", str(fixture_repo), "--port", str(_free_port())],
    )
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
    port = _free_port()
    result = runner.invoke(
        app,
        ["dashboard", "--no-open", "--port", str(port), "--repo", str(fixture_repo)],
    )
    assert result.exit_code == 0, result.output
    info = seen["pidfile"]
    assert isinstance(info, dict)
    assert info["pid"] == os.getpid()
    assert info["port"] == port
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == port
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
