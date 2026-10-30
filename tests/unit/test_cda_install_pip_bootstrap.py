"""CDA install pip bootstrap (issue #372): a stock uv-tool environment has
no pip module, so preflight must notice and the job must bootstrap pip
with ensurepip before the pip steps — falling to the guide only when
bootstrapping fails. Repair never reports a half-installed backend as done.

No test downloads torch or a checkpoint: spawn is injected, the
environment decision is stubbed, network reachability is monkeypatched,
and pip presence is monkeypatched except for the pip_available unit tests.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jev_agent.dashboard import cda_install_runner as runner_module
from jev_agent.dashboard.cda_install_runner import (
    CDA_CPU_INDEX,
    CdaInstallRunner,
    EnvironmentDecision,
    bootstrap_guide_command,
    classify_failure,
    ensurepip_argv,
    pip_available,
)

MODEL = "S4MPL3BI4S/Coding_Decision_Agent"
PROBE_LINE = (
    'CDA_PROBE_JSON {"laya": "0.4.15", "torch": "2.5.0", '
    f'"cuda": null, "constructible": true, "model": "{MODEL}"}}'
)


class _FakeProc:
    def __init__(self, code: int) -> None:
        self.returncode: int | None = None
        self.pid = 43211
        self.terminated = False
        self._code = code

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._code
        return self._code

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class _FakeSpawn:
    """Injected spawn: canned output per step kind, no downloads.

    pip_codes is consumed in order by pip steps (uninstall, torch, laya);
    a single int applies to every pip step.
    """

    def __init__(
        self,
        *,
        ensurepip_code: int = 0,
        pip_codes: int | list[int] = 0,
        pip_stderr: str = "",
    ) -> None:
        self.calls: list[list[str]] = []
        self.procs: list[_FakeProc] = []
        self.ensurepip_code = ensurepip_code
        self._pip_codes = pip_codes if isinstance(pip_codes, list) else [pip_codes]
        self._pip_index = 0
        self.pip_stderr = pip_stderr

    def _next_pip_code(self) -> int:
        code = self._pip_codes[min(self._pip_index, len(self._pip_codes) - 1)]
        self._pip_index += 1
        return code

    def __call__(self, argv: list[str], log: Path, env: dict[str, str]) -> _FakeProc:
        self.calls.append(list(argv))
        if len(argv) >= 2 and argv[1] == "-c":
            script = argv[2]
            lines = [f"CDA_WARM_OK {MODEL}"] if "laya.load" in script else [PROBE_LINE]
            code = 0
        elif len(argv) >= 3 and argv[2] == "ensurepip":
            lines = ["ensurepip done"] if self.ensurepip_code == 0 else ["ensurepip failed"]
            code = self.ensurepip_code
        else:
            lines = [self.pip_stderr] if self.pip_stderr else ["Successfully installed"]
            code = self._next_pip_code()
        with log.open("ab") as handle:
            for line in lines:
                handle.write((line + "\n").encode("utf-8"))
        proc = _FakeProc(code)
        self.procs.append(proc)
        return proc


def _decision() -> EnvironmentDecision:
    return EnvironmentDecision(
        branch=1,
        interpreter=sys.executable,
        venv_path=None,
        torch_path="cpu",
        persistence="Installs into the dashboard's Python environment.",
        guide_command=f"{sys.executable} -m pip install 'laya>=0.4.15'",
        docker=False,
    )


def _pip_sequence(results: list[bool]) -> Callable[..., bool]:
    iterator = iter(results)

    def _fake(interpreter: str, *, timeout: float = 10.0) -> bool:
        try:
            return next(iterator)
        except StopIteration:
            return True

    return _fake


def _always(value: bool) -> Callable[..., bool]:
    def _fake(interpreter: str, *, timeout: float = 10.0) -> bool:
        return value

    return _fake


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _make_runner(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawn: _FakeSpawn,
    pip_fn: Any | None = None,
) -> CdaInstallRunner:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    if pip_fn is not None:
        monkeypatch.setattr(runner_module, "pip_available", pip_fn)
    return CdaInstallRunner(tmp_path / "repo", home=home, spawn=spawn, decision=_decision())


def _wait_until(runner: CdaInstallRunner, states: set[str], timeout_s: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = runner.status().state
        if state in states:
            return state
        time.sleep(0.02)
    return runner.status().state


def _preflight_map(runner: CdaInstallRunner) -> dict[str, tuple[bool, str]]:
    return {name: (passed, detail) for name, passed, detail in runner.status().preflight}


# ---- pip_available and classification ------------------------------------------


def _stub_interpreter(tmp_path: Path, name: str, pip_exit: int) -> str:
    """A fake interpreter script: exits `pip_exit` for `-m pip --version`,
    1 otherwise. Deterministic across environments (no reliance on the
    test runner's own pip)."""
    path = tmp_path / name
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ] && [ "$3" = "--version" ]; then\n'
        f"  exit {pip_exit}\n"
        "fi\n"
        "exit 1\n"
    )
    path.chmod(0o755)
    return str(path)


def test_pip_available_probes_exit_code(tmp_path: Path) -> None:
    assert pip_available(_stub_interpreter(tmp_path, "with-pip", 0)) is True
    assert pip_available(_stub_interpreter(tmp_path, "without-pip", 1)) is False
    assert pip_available("/nonexistent/python-xyz-372") is False


def test_classify_no_module_named_pip() -> None:
    failure_class, _message, retryable, guide = classify_failure("No module named pip", 1)
    assert failure_class == "pip-missing"
    assert retryable is True
    assert guide is False


def test_ensurepip_argv_contract() -> None:
    argv = ensurepip_argv(sys.executable)
    assert argv == [sys.executable, "-m", "ensurepip"]
    assert "jev-agent" not in " ".join(argv)
    assert "--break-system-packages" not in argv
    guide = bootstrap_guide_command(_decision())
    assert guide.startswith(f"{sys.executable} -m ensurepip\n")
    assert CDA_CPU_INDEX in guide
    assert "jev-agent" not in guide


# ---- bootstrap success ---------------------------------------------------------


def test_install_bootstraps_pip_then_succeeds(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _FakeSpawn()
    runner = _make_runner(tmp_path, home, monkeypatch, spawn, _pip_sequence([False, True]))
    runner.start("install", MODEL)
    assert _wait_until(runner, {"done", "failed"}) == "done"
    assert spawn.calls[0] == [sys.executable, "-m", "ensurepip"]
    assert any(call[3:] == ["install", "laya>=0.4.15"] for call in spawn.calls)
    status = runner.status()
    assert status.probe_by_model[MODEL].warmed is True
    pip_row = _preflight_map(runner)["pip available"]
    assert pip_row[0] is True  # corrected to pass once bootstrapping worked


def test_pip_present_runs_no_ensurepip(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _FakeSpawn()
    runner = _make_runner(tmp_path, home, monkeypatch, spawn, _always(True))
    runner.start("install", MODEL)
    assert _wait_until(runner, {"done", "failed"}) == "done"
    assert not any("ensurepip" in call for call in spawn.calls)
    assert _preflight_map(runner)["pip available"][0] is True


# ---- bootstrap failure ---------------------------------------------------------


def test_install_ensurepip_failure_is_guide(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _FakeSpawn(ensurepip_code=1)
    runner = _make_runner(tmp_path, home, monkeypatch, spawn, _always(False))
    runner.start("install", MODEL)
    assert _wait_until(runner, {"done", "failed"}) == "failed"
    status = runner.status()
    assert status.failure_class == "pip-bootstrap"
    assert status.guide is True
    assert status.guide_command.startswith(f"{sys.executable} -m ensurepip\n")
    assert "pip install" in status.guide_command
    assert "Run the command below" in status.message
    assert spawn.calls == [[sys.executable, "-m", "ensurepip"]]
    assert status.probe_by_model == {}


def test_install_ensurepip_ok_but_pip_still_missing(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _FakeSpawn(ensurepip_code=0)
    runner = _make_runner(tmp_path, home, monkeypatch, spawn, _always(False))
    runner.start("install", MODEL)
    assert _wait_until(runner, {"done", "failed"}) == "failed"
    status = runner.status()
    assert status.failure_class == "pip-bootstrap"
    assert status.guide is True
    assert not any("install" in call for call in spawn.calls)


def test_repair_bootstrap_failure_runs_no_steps(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _FakeSpawn(ensurepip_code=1)
    runner = _make_runner(tmp_path, home, monkeypatch, spawn, _always(False))
    runner.start("repair", MODEL)
    assert _wait_until(runner, {"done", "failed"}) == "failed"
    status = runner.status()
    assert status.failure_class == "pip-bootstrap"
    assert spawn.calls == [[sys.executable, "-m", "ensurepip"]]
    assert status.probe_by_model == {}


def test_uninstall_bootstrap_failure_runs_no_uninstall(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _FakeSpawn(ensurepip_code=1)
    runner = _make_runner(tmp_path, home, monkeypatch, spawn, _always(False))
    runner.start("uninstall", MODEL)
    assert _wait_until(runner, {"done", "failed"}) == "failed"
    status = runner.status()
    assert status.failure_class == "pip-bootstrap"
    assert not any("uninstall" in call for call in spawn.calls)


# ---- half-installed backends are never reported done ---------------------------


def test_repair_mid_install_failure_never_reports_done(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Repair order (cpu): uninstall ok, torch ok, laya install fails.
    spawn = _FakeSpawn(pip_codes=[0, 0, 1], pip_stderr="ERROR: install failed")
    runner = _make_runner(tmp_path, home, monkeypatch, spawn, _always(True))
    runner.start("repair", MODEL)
    assert _wait_until(runner, {"done", "failed"}) == "failed"
    status = runner.status()
    assert status.failure_class == "pip-failed"
    assert status.probe_by_model == {}
    assert status.state == "failed"
