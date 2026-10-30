"""CDA install routes (issue #366): the §6.7 route set, CSRF refusal,
single-flight double-submit, Verify/Repair/Uninstall, the D3 enable path,
and the three retired pip-instruction surfaces.

Through Starlette's in-process test client; no test downloads torch or a
checkpoint — the dashboard's install runner is replaced with one over an
injected spawn, and the orchestrator seam is patched like the other
code-grade tests.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.applications import Starlette
from starlette.testclient import TestClient

from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.code_orchestrator import CodeOrchestrator, CodeOrchestratorUnavailable
from jev_agent.dashboard import cda_install_runner as install_module
from jev_agent.dashboard import code_grade_runner, routes_code
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.cda_install_runner import CdaInstallRunner, EnvironmentDecision
from jev_agent.dashboard.security import DashboardSettings

MODEL = "S4MPL3BI4S/Coding_Decision_Agent"
PROBE_LINE = (
    'CDA_PROBE_JSON {"laya": "0.4.15", "torch": "2.5.0", '
    f'"cuda": null, "constructible": true, "model": "{MODEL}"}}'
)


class _FakeProc:
    """A spawned child: `wait()` returns the exit code (browser-runner precedent)."""

    def __init__(self, code: int) -> None:
        self.returncode: int | None = None
        self.pid = 43210
        self.terminated = False
        self._code = code

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._code
        return self._code

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class _BlockingProc(_FakeProc):
    """A child that runs until released or terminated."""

    def __init__(self) -> None:
        super().__init__(0)
        self.released = threading.Event()

    def wait(self, timeout: float | None = None) -> int:
        while not self.released.is_set() and not self.terminated:
            time.sleep(0.01)
        self.returncode = -15 if self.terminated else 0
        return self.returncode


class _FakeSpawn:
    """Injected spawn: canned output per step kind, no downloads."""

    def __init__(
        self,
        *,
        pip_code: int = 0,
        pip_stderr: str = "",
        block_phase: str | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.procs: list[_FakeProc] = []
        self.pip_code = pip_code
        self.pip_stderr = pip_stderr
        self.block_phase = block_phase
        self._block_used = False

    def __call__(self, argv: list[str], log: Path, env: dict[str, str]) -> _FakeProc:
        self.calls.append(list(argv))
        if len(argv) >= 2 and argv[1] == "-c":
            script = argv[2]
            kind = "warmup" if "laya.load" in script else "probe"
            lines = [f"CDA_WARM_OK {MODEL}"] if kind == "warmup" else [PROBE_LINE]
            code = 0
        else:
            kind = "pip"
            lines = [self.pip_stderr] if self.pip_stderr else ["Successfully installed"]
            code = self.pip_code
        with log.open("ab") as handle:
            for line in lines:
                handle.write((line + "\n").encode("utf-8"))
        if self.block_phase == kind and not self._block_used:
            # Only the first matching step parks: a CPU install has two pip
            # steps, and releasing the first must let the job finish.
            self._block_used = True
            proc: _FakeProc = _BlockingProc()
        else:
            proc = _FakeProc(code)
        self.procs.append(proc)
        return proc


DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


class _NoBackend:
    """An orchestrator stand-in reporting no backend at all (page status)."""

    available = False


class _YesBackend:
    """An orchestrator stand-in reporting a backend present."""

    available = True


class _UnavailableBackend:
    def grade_code(self, *, task: str, diff: str, tests: str) -> object:
        raise CodeOrchestratorUnavailable("the Coding Decision Agent is not installed")

    def route_model(self, request: str) -> object:
        raise CodeOrchestratorUnavailable("the Coding Decision Agent is not installed")


@pytest.fixture(autouse=True)
def _fake_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


@pytest.fixture
def client(
    fixture_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(install_module, "_url_reachable", lambda url: True)
    # Same premise as the runner tests: a working environment, pip present.
    # The pip-missing path is covered by test_cda_install_pip_bootstrap.py.
    monkeypatch.setattr(install_module, "pip_available", lambda interpreter, *, timeout=10.0: True)
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as test_client:
        yield test_client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/code-grade").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str]) -> httpx.Response:
    return cast(
        httpx.Response,
        client.post(url, data={**data, "csrf_token": _csrf(client)}, follow_redirects=False),
    )


def _inject_runner(
    client: TestClient, repo: Path, home: Path, spawn: _FakeSpawn
) -> CdaInstallRunner:
    dash = cast(Starlette, client.app).state.dashboard
    decision = EnvironmentDecision(
        1,
        sys.executable,
        None,
        "cpu",
        "Installs into the dashboard's Python environment.",
        "guide",
        False,
    )
    runner = CdaInstallRunner(repo, home=home, spawn=spawn, decision=decision)
    dash.cda_install_runner = runner
    return runner


def _wait_procs(spawn: _FakeSpawn, count: int, timeout_s: float = 10.0) -> bool:
    """Wait until the fake has spawned `count` children (the watcher thread
    may lag behind the state flip to "running")."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if len(spawn.procs) >= count:
            return True
        time.sleep(0.02)
    return False


def _wait_state(runner: CdaInstallRunner, states: set[str], timeout_s: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = runner.status().state
        if state in states:
            return state
        time.sleep(0.02)
    return runner.status().state


def _live_until(client: TestClient, needle: str, timeout_s: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_s
    text = ""
    while time.monotonic() < deadline:
        text = client.get("/code-grade/live").text
        if needle in text:
            return text
        time.sleep(0.05)
    return text


# ---- route set (§6.7) ---------------------------------------------------------------


def test_install_pages_render(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_code, "_orchestrator", lambda settings: _NoBackend())
    for url in ("/code-grade/install", "/code-grade/install/live", "/code-grade/install/confirm"):
        page = client.get(url)
        assert page.status_code == 200, url
    assert "Backend install status" in client.get("/code-grade/install").text
    assert (
        "Install the Coding Decision Agent backend?"
        in client.get("/code-grade/install/confirm").text
    )


def test_install_start_runs_to_done(client: TestClient, fixture_repo: Path, tmp_path: Path) -> None:
    spawn = _FakeSpawn()
    runner = _inject_runner(client, fixture_repo, tmp_path / "home", spawn)
    response = _post(client, "/code-grade/install/start", {})
    assert response.status_code == 303
    assert response.headers["location"] == "/code-grade/install"
    assert _wait_state(runner, {"done"}) == "done"
    assert "Backend installed and warmed." in client.get("/code-grade/install").text


def test_install_post_routes_refuse_without_csrf(client: TestClient) -> None:
    for url in (
        "/code-grade/install/start",
        "/code-grade/install/stop",
        "/code-grade/install/verify",
        "/code-grade/install/repair",
        "/code-grade/install/uninstall",
    ):
        response = client.post(url, data={}, follow_redirects=False)
        assert response.status_code == 403, url


def test_single_flight_double_submit_sees_running_status_not_error(
    client: TestClient, fixture_repo: Path, tmp_path: Path
) -> None:
    spawn = _FakeSpawn(block_phase="pip")
    runner = _inject_runner(client, fixture_repo, tmp_path / "home", spawn)
    first = _post(client, "/code-grade/install/start", {})
    assert first.status_code == 303
    assert _wait_state(runner, {"running"}) == "running"
    assert _wait_procs(spawn, 1)
    second = _post(client, "/code-grade/install/start", {})
    assert second.status_code == 303
    assert second.headers["location"] == "/code-grade/install"
    assert runner.status().state == "running"
    # Still one job: only the first submit spawned a child.
    assert len(spawn.procs) == 1
    proc = spawn.procs[0]
    assert isinstance(proc, _BlockingProc)
    proc.released.set()
    assert _wait_state(runner, {"done"}) == "done"


def test_stop_route_terminates_install(
    client: TestClient, fixture_repo: Path, tmp_path: Path
) -> None:
    spawn = _FakeSpawn(block_phase="pip")
    runner = _inject_runner(client, fixture_repo, tmp_path / "home", spawn)
    _post(client, "/code-grade/install/start", {})
    assert _wait_state(runner, {"running"}) == "running"
    response = _post(client, "/code-grade/install/stop", {})
    assert response.status_code == 303
    assert _wait_state(runner, {"stopped"}) == "stopped"


def test_verify_route_refreshes_probe(
    client: TestClient, fixture_repo: Path, tmp_path: Path
) -> None:
    spawn = _FakeSpawn()
    runner = _inject_runner(client, fixture_repo, tmp_path / "home", spawn)
    response = _post(client, "/code-grade/install/verify", {})
    assert response.status_code == 303
    assert _wait_state(runner, {"done"}) == "done"
    assert runner.status().probe_by_model[MODEL].constructible is True


def test_verify_refused_with_link_while_install_runs(
    client: TestClient, fixture_repo: Path, tmp_path: Path
) -> None:
    spawn = _FakeSpawn(block_phase="pip")
    runner = _inject_runner(client, fixture_repo, tmp_path / "home", spawn)
    _post(client, "/code-grade/install/start", {})
    assert _wait_state(runner, {"running"}) == "running"
    response = _post(client, "/code-grade/install/verify", {})
    assert response.status_code == 303
    assert response.headers["location"] == "/code-grade/install"
    # Still the install job, not a verify job — and verify ledgers nothing
    # (verify never touches the ledger; the refused job never started).
    assert runner.status().kind == "install"
    assert _wait_procs(spawn, 1)
    proc = spawn.procs[0]
    assert isinstance(proc, _BlockingProc)
    proc.released.set()
    assert _wait_state(runner, {"done"}) == "done"


def test_repair_route_runs_uninstall_then_install(
    client: TestClient, fixture_repo: Path, tmp_path: Path
) -> None:
    spawn = _FakeSpawn()
    runner = _inject_runner(client, fixture_repo, tmp_path / "home", spawn)
    response = _post(client, "/code-grade/install/repair", {})
    assert response.status_code == 303
    assert _wait_state(runner, {"done"}) == "done"
    assert runner.status().message == "Backend repaired and warmed."
    first = spawn.calls[0]
    assert first[3:] == ["uninstall", "-y", "laya"]


def test_uninstall_route_removes_laya_only(
    client: TestClient, fixture_repo: Path, tmp_path: Path
) -> None:
    spawn = _FakeSpawn()
    runner = _inject_runner(client, fixture_repo, tmp_path / "home", spawn)
    response = _post(client, "/code-grade/install/uninstall", {})
    assert response.status_code == 303
    assert _wait_state(runner, {"done"}) == "done"
    assert runner.status().message == "Backend uninstalled."
    assert len(spawn.calls) == 1
    assert spawn.calls[0][3:] == ["uninstall", "-y", "laya"]


def test_grade_refused_while_install_runs_ledgers_nothing(
    client: TestClient, fixture_repo: Path, tmp_path: Path
) -> None:
    spawn = _FakeSpawn(block_phase="pip")
    runner = _inject_runner(client, fixture_repo, tmp_path / "home", spawn)
    _post(client, "/code-grade/install/start", {})
    assert _wait_state(runner, {"running"}) == "running"
    response = _post(client, "/code-grade/grade", {"task": "Fix x", "diff": DIFF, "tests": ""})
    assert response.status_code == 303
    assert response.headers["location"] == "/code-grade"
    page = client.get("/code-grade").text
    assert "an install is running" in page
    ledger = fixture_repo / ".jev" / "cda-ratings.jsonl"
    assert not ledger.exists()
    assert _wait_procs(spawn, 1)
    proc = spawn.procs[0]
    assert isinstance(proc, _BlockingProc)
    proc.released.set()
    assert _wait_state(runner, {"done"}) == "done"


def test_install_blocked_while_grade_runs(
    client: TestClient, fixture_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()

    def _blocking(kind: str, settings: Any, inputs: dict[str, str]) -> dict[str, Any]:
        gate.wait(timeout=10)
        return {
            "code_quality": 3,
            "instruction_followed": "yes",
            "likely_correct": "yes",
            "merge_action": "accept",
            "answer_confidence": {"merge_action": 0.95},
            "gate": {
                "decision": "accept",
                "reason": "",
                "merge_action": "accept",
                "confidence": 0.95,
            },
        }

    monkeypatch.setattr(
        code_grade_runner,
        "_orchestrator",
        lambda settings: CodeOrchestrator(
            model_id=settings.model_id,
            max_diff_chars=settings.max_diff_chars,
            backend=_UnavailableBackend(),
        ),
    )
    dash = cast(Starlette, client.app).state.dashboard
    dash.code_grade_runner.executor = _blocking
    response = _post(client, "/code-grade/grade", {"task": "Fix x", "diff": DIFF, "tests": ""})
    assert response.status_code == 303
    assert response.headers["location"] == "/code-grade/status"
    assert dash.code_grade_runner.busy() is True
    spawn = _FakeSpawn()
    runner = _inject_runner(client, fixture_repo, tmp_path / "home", spawn)
    refused = _post(client, "/code-grade/install/start", {})
    assert refused.status_code == 303
    assert refused.headers["location"] == "/code-grade"
    assert "a grade is finishing" in client.get("/code-grade").text
    assert runner.status().state == "idle"
    gate.set()


# ---- D3 enable path -----------------------------------------------------------------


def test_d3_enable_button_flips_setting_and_returns_to_code_grade(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_code, "_orchestrator", lambda settings: _YesBackend())
    page = client.get("/code-grade").text
    assert "Turn on gate grading" in page
    assert 'name="next" value="/code-grade"' in page
    response = _post(
        client,
        "/settings/set",
        {
            "path": "code_orchestrator.enabled",
            "kind": "bool",
            "value": "true",
            "target": "repo",
            "next": "/code-grade",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/code-grade"
    after = client.get("/code-grade").text
    assert "Turn on gate grading" not in after


def test_settings_set_without_next_keeps_default_redirect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_code, "_orchestrator", lambda settings: _YesBackend())
    response = _post(
        client,
        "/settings/set",
        {
            "path": "code_orchestrator.enabled",
            "kind": "bool",
            "value": "false",
            "target": "repo",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings#s-code_orchestrator"


def test_d3_button_hidden_when_gate_already_enabled(
    client: TestClient, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fixture_repo / "jev.toml").write_text("[code_orchestrator]\nenabled = true\n")
    monkeypatch.setattr(routes_code, "_orchestrator", lambda settings: _YesBackend())
    assert "Turn on gate grading" not in client.get("/code-grade").text


# ---- retired pip-instruction surfaces (§10) ------------------------------------------


def test_retired_status_panel_has_no_pip_command(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_code, "_orchestrator", lambda settings: _NoBackend())
    page = client.get("/code-grade").text
    assert "not installed" in page
    assert "jev-agent[code-orchestrator]" not in page
    assert "Install backend" in page


def test_retired_failed_grade_message_links_to_install(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        code_grade_runner,
        "_orchestrator",
        lambda settings: CodeOrchestrator(
            model_id=settings.model_id,
            max_diff_chars=settings.max_diff_chars,
            backend=_UnavailableBackend(),
        ),
    )
    response = _post(client, "/code-grade/grade", {"task": "Fix x", "diff": DIFF, "tests": ""})
    assert response.status_code == 303
    live = _live_until(client, "not installed")
    assert "not installed" in live
    assert "jev-agent[code-orchestrator]" not in live
    assert "Code Grade page" in live
    assert "Traceback" not in live


def test_retired_ratings_empty_state_links_to_code_grade(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = client.get("/learning?tab=ratings")
    assert response.status_code == 200
    assert "jev-agent[code-orchestrator]" not in response.text
    assert "No grades recorded yet" in response.text
