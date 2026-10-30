"""Grade-runner interpreter override (design §5.4, branch 2): grades run
under the dedicated-venv interpreter via `code_grade_subprocess` instead
of in-process, so a venv-only backend install is actually usable.

Through the runner directly (no HTTP): the child protocol is exercised
with stub spawns, plus the real child module for its JSON contract
(the model is never loaded — no test grades for real here).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from jev_agent.config.schema import CodeOrchestratorSettings
from jev_agent.dashboard.code_grade_runner import (
    CodeGradeRunner,
    discover_grade_interpreter,
)

SETTINGS = CodeOrchestratorSettings()
GRADE_INPUTS = {"task": "fix the bug", "diff": "diff --git a/a.py", "tests": ""}
ROUTE_INPUTS = {"route_request": "refactor the parser"}


class _StubProc:
    """A Popen-like child returning canned stdout (and recording stdin)."""

    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.terminated = False
        self.inputs: list[bytes] = []

    def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if input is not None:
            self.inputs.append(input)
        return self._stdout, self._stderr

    def terminate(self) -> None:
        self.terminated = True

    def poll(self) -> int | None:
        return -15 if self.terminated else None


def _json_proc(payload: dict[str, Any]) -> _StubProc:
    return _StubProc(json.dumps(payload).encode())


def _spawn_with(proc: _StubProc, seen: list[list[str]]) -> Any:
    def _spawn(argv: list[str]) -> _StubProc:
        seen.append(argv)
        return proc

    return _spawn


def _wait_done(runner: CodeGradeRunner, timeout_s: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = runner.status().state
        if state in ("done", "failed", "cancelled"):
            return state
        time.sleep(0.05)
    return runner.status().state


def _run(
    runner: CodeGradeRunner,
    kind: str,
    inputs: dict[str, str],
) -> Any:
    runner.start(kind=kind, settings=SETTINGS, inputs=inputs, task="t")
    assert _wait_done(runner) in ("done", "failed", "cancelled")
    return runner.status()


def test_subprocess_grade_success() -> None:
    seen: list[list[str]] = []
    proc = _json_proc({"ok": True, "result": {"merge_action": "accept"}})
    runner = CodeGradeRunner(interpreter="/fake/python", spawn=_spawn_with(proc, seen))
    status = _run(runner, "grade", GRADE_INPUTS)
    assert status.state == "done"
    assert status.result == {"merge_action": "accept"}
    assert seen == [["/fake/python", "-m", "jev_agent.dashboard.code_grade_subprocess"]]
    request = json.loads(proc.inputs[0])
    assert request["kind"] == "grade"
    assert request["task"] == "fix the bug"
    assert request["diff"] == "diff --git a/a.py"
    assert request["tests"] == ""
    assert request["model_id"] == SETTINGS.model_id
    assert request["confidence_threshold"] == SETTINGS.confidence_threshold


def test_subprocess_route_success() -> None:
    proc = _json_proc({"ok": True, "result": {"hint": "use the fast model"}})
    runner = CodeGradeRunner(interpreter="/fake/python", spawn=_spawn_with(proc, []))
    status = _run(runner, "route", ROUTE_INPUTS)
    assert status.state == "done"
    assert status.result == {"hint": "use the fast model"}
    assert json.loads(proc.inputs[0])["route_request"] == "refactor the parser"


def test_subprocess_unavailable_maps_to_failed_with_guidance() -> None:
    proc = _json_proc({"ok": False, "unavailable": True, "message": "the backend is not installed"})
    runner = CodeGradeRunner(interpreter="/fake/python", spawn=_spawn_with(proc, []))
    status = _run(runner, "grade", GRADE_INPUTS)
    assert status.state == "failed"
    assert status.error == "the backend is not installed"
    assert status.result is None


def test_subprocess_error_maps_to_failed() -> None:
    proc = _json_proc({"ok": False, "error": "the model file is corrupt"})
    runner = CodeGradeRunner(interpreter="/fake/python", spawn=_spawn_with(proc, []))
    status = _run(runner, "grade", GRADE_INPUTS)
    assert status.state == "failed"
    assert status.error == "the model file is corrupt"


def test_subprocess_malformed_output_is_a_plain_failure() -> None:
    proc = _StubProc(b"not json at all", b"Traceback (most recent call last): ...")
    runner = CodeGradeRunner(interpreter="/fake/python", spawn=_spawn_with(proc, []))
    status = _run(runner, "grade", GRADE_INPUTS)
    assert status.state == "failed"
    assert status.error is not None
    assert "Traceback" not in status.error
    assert "configured interpreter" in status.error


def test_subprocess_spawn_failure_is_a_plain_failure() -> None:
    def _boom(argv: list[str]) -> Any:
        raise FileNotFoundError(2, "No such file", argv[0])

    runner = CodeGradeRunner(interpreter="/nonexistent/python", spawn=_boom)
    status = _run(runner, "grade", GRADE_INPUTS)
    assert status.state == "failed"
    assert status.error is not None
    assert "Traceback" not in status.error


def test_no_interpreter_uses_the_executor() -> None:
    seen: dict[str, Any] = {}

    def _executor(kind: str, settings: Any, inputs: dict[str, str]) -> dict[str, Any]:
        seen["kind"] = kind
        return {"merge_action": "accept"}

    def _never_spawn(argv: list[str]) -> Any:
        raise AssertionError("no subprocess expected without an interpreter")

    runner = CodeGradeRunner(executor=_executor, spawn=_never_spawn)
    status = _run(runner, "grade", GRADE_INPUTS)
    assert status.state == "done"
    assert seen["kind"] == "grade"
    assert status.result == {"merge_action": "accept"}


def test_subprocess_module_json_contract() -> None:
    """The real child module: one JSON line on stdout, exit 0, no model.

    An unknown kind never touches the model — it proves the import
    chain and the stdout protocol end to end in a real interpreter.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "jev_agent.dashboard.code_grade_subprocess"],
        input=json.dumps(
            {
                "kind": "bogus",
                "model_id": SETTINGS.model_id,
                "max_diff_chars": SETTINGS.max_diff_chars,
                "confidence_threshold": SETTINGS.confidence_threshold,
            }
        ).encode(),
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {
        "ok": False,
        "error": "unknown grade kind: 'bogus'",
    }


def test_discover_returns_venv_when_laya_invisible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "jev_agent.dashboard.code_grade_runner.importlib.util.find_spec",
        lambda name: None,
    )
    venv_python = tmp_path / ".jev" / "cda-venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")
    venv_python.chmod(0o755)
    assert discover_grade_interpreter(home=tmp_path) == str(venv_python)


def test_discover_returns_none_when_laya_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "jev_agent.dashboard.code_grade_runner.importlib.util.find_spec",
        lambda name: SimpleNamespace(name="laya"),
    )
    venv_python = tmp_path / ".jev" / "cda-venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")
    venv_python.chmod(0o755)
    assert discover_grade_interpreter(home=tmp_path) is None


def test_discover_returns_none_when_no_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "jev_agent.dashboard.code_grade_runner.importlib.util.find_spec",
        lambda name: None,
    )
    assert discover_grade_interpreter(home=tmp_path) is None


def test_shutdown_terminates_a_hanging_child() -> None:
    """shutdown() kills a still-running grade child (no orphaned model)."""

    class _SleepProc:
        def __init__(self) -> None:
            self._proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            self.terminated = False

        def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            return self._proc.communicate(input)

        def terminate(self) -> None:
            self.terminated = True
            self._proc.terminate()

        def poll(self) -> int | None:
            return self._proc.poll()

    sleep_proc = _SleepProc()
    try:
        runner = CodeGradeRunner(interpreter="/fake/python", spawn=lambda argv: sleep_proc)
        runner.start(kind="grade", settings=SETTINGS, inputs=GRADE_INPUTS, task="t")
        assert runner.status().state == "running"
        runner.shutdown(timeout=2)
        assert sleep_proc.terminated
        assert sleep_proc.poll() is not None
        assert runner.status().state == "failed"
    finally:
        if sleep_proc.poll() is None:
            sleep_proc._proc.terminate()
            sleep_proc._proc.wait(timeout=10)


def test_cancel_does_not_terminate_the_child() -> None:
    """Cancel keeps in-process semantics under the override: the child
    runs to completion and its result is dropped, never killed."""
    entered = threading.Event()
    release = threading.Event()

    class _BlockingProc(_StubProc):
        def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            entered.set()
            assert release.wait(timeout=10)
            return super().communicate(input)

    proc = _BlockingProc(json.dumps({"ok": True, "result": {}}).encode())
    runner = CodeGradeRunner(interpreter="/fake/python", spawn=_spawn_with(proc, []))
    runner.start(kind="grade", settings=SETTINGS, inputs=GRADE_INPUTS, task="t")
    assert entered.wait(timeout=10)
    assert runner.cancel() is True
    assert proc.terminated is False
    assert runner.status().state == "cancelled"
    release.set()
    deadline = time.monotonic() + 10
    while runner.busy() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not runner.busy()
    assert runner.status().state == "cancelled"
    assert runner.status().result is None


# ---- DashboardCore wiring --------------------------------------------------------


def test_core_passes_explicit_interpreter(tmp_path: Path) -> None:
    from jev_agent.dashboard.app import Dashboard
    from jev_agent.dashboard.runner import RunLauncher
    from jev_agent.dashboard.security import DashboardSettings

    dash = Dashboard(
        DashboardSettings(root=tmp_path),
        RunLauncher(tmp_path),
        None,
        None,
        "/fake/python",
    )
    assert dash.code_grade_runner.interpreter == "/fake/python"


def test_core_discovers_interpreter_when_no_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jev_agent.dashboard.app import Dashboard
    from jev_agent.dashboard.runner import RunLauncher
    from jev_agent.dashboard.security import DashboardSettings

    monkeypatch.setattr(
        "jev_agent.dashboard.core.discover_grade_interpreter",
        lambda: "/discovered/python",
    )
    dash = Dashboard(DashboardSettings(root=tmp_path), RunLauncher(tmp_path))
    assert dash.code_grade_runner.interpreter == "/discovered/python"


def test_core_with_fake_executor_discovers_nothing(tmp_path: Path) -> None:
    from jev_agent.dashboard.app import Dashboard
    from jev_agent.dashboard.runner import RunLauncher
    from jev_agent.dashboard.security import DashboardSettings

    def _fake_executor(kind: str, settings: Any, inputs: dict[str, str]) -> dict[str, Any]:
        return {}

    dash = Dashboard(
        DashboardSettings(root=tmp_path),
        RunLauncher(tmp_path),
        None,
        _fake_executor,
    )
    assert dash.code_grade_runner.interpreter is None
