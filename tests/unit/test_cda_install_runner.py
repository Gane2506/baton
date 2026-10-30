"""CDA install runner (issue #366): argv, preflight, failure classification,
stop-per-phase, restart/interrupted, and §6.4 coordination.

No test downloads torch or a checkpoint: process spawn is injected, the
environment decision is stubbed, and network reachability is monkeypatched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jev_agent.config.schema import CodeOrchestratorSettings
from jev_agent.dashboard import cda_install_runner as runner_module
from jev_agent.dashboard.cda_install_runner import (
    CdaInstallRunner,
    EnvironmentDecision,
    build_pip_steps,
    classify_failure,
    probe_argv,
    redact_text,
    uninstall_argv,
    venv_argv,
    warmup_argv,
)
from jev_agent.dashboard.code_grade_runner import CodeGradeRunner
from jev_agent.dashboard.core import DashboardCore

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
    """A child that runs until released or terminated (stop-per-phase tests)."""

    def __init__(self) -> None:
        super().__init__(0)
        self.released = threading.Event()

    def wait(self, timeout: float | None = None) -> int:
        while not self.released.is_set() and not self.terminated:
            time.sleep(0.01)
        self.returncode = -15 if self.terminated else 0
        return self.returncode


class _FakeSpawn:
    """Injected spawn: canned output per step kind, no downloads.

    `block_phase` ("pip" | "probe" | "warmup") parks that step's child until
    released, so stop/shutdown tests can catch the job mid-phase.
    """

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


def _decision(
    *, branch: int = 1, torch_path: str = "cpu", interpreter: str = sys.executable
) -> EnvironmentDecision:
    return EnvironmentDecision(
        branch=branch,
        interpreter=interpreter,
        venv_path=None,
        torch_path=torch_path,
        persistence="Installs into the dashboard's Python environment.",
        guide_command=f"{interpreter} -m pip install 'laya>=0.4.15'",
        docker=False,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def runner(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[CdaInstallRunner]:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    yield CdaInstallRunner(tmp_path / "repo", home=home, decision=_decision())


def _wait_procs(spawn: _FakeSpawn, count: int, timeout_s: float = 10.0) -> bool:
    """Wait until the fake has spawned `count` children (the watcher thread
    may lag behind the state flip to "running")."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if len(spawn.procs) >= count:
            return True
        time.sleep(0.02)
    return False


def _wait_until(runner: CdaInstallRunner, states: set[str], timeout_s: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = runner.status().state
        if state in states:
            return state
        time.sleep(0.02)
    return runner.status().state


def _wait_phase(runner: CdaInstallRunner, phase: str, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = runner.status()
        if status.state == "running" and status.phase == phase:
            return True
        time.sleep(0.02)
    return False


# ---- argv (§5.3a, §7) ------------------------------------------------------------


@pytest.mark.parametrize("torch_path", ["cpu", "default"])
def test_argv_never_installs_jev_agent(torch_path: str) -> None:
    decision = _decision(torch_path=torch_path)
    sequences = [
        *build_pip_steps(decision),
        *build_pip_steps(decision, repair=True),
        uninstall_argv(sys.executable),
        probe_argv(sys.executable, MODEL),
        warmup_argv(sys.executable, MODEL),
    ]
    venv_decision = EnvironmentDecision(
        2, sys.executable, Path("/tmp/venv"), torch_path, "", "", False
    )
    sequences.extend(build_pip_steps(venv_decision))
    sequences.append(venv_argv(sys.executable, Path("/tmp/venv")))
    for argv in sequences:
        joined = " ".join(argv)
        assert "jev-agent" not in joined
        assert "--break-system-packages" not in argv


def test_argv_cpu_path_installs_cpu_torch_first() -> None:
    steps = build_pip_steps(_decision(torch_path="cpu"))
    assert steps[0][3:] == [
        "install",
        "torch",
        "--index-url",
        "https://download.pytorch.org/whl/cpu",
    ]
    assert steps[1][3:] == ["install", "laya>=0.4.15"]


def test_argv_default_path_is_one_laya_step() -> None:
    assert build_pip_steps(_decision(torch_path="default")) == [
        [sys.executable, "-m", "pip", "install", "laya>=0.4.15"]
    ]


def test_argv_repair_uninstalls_laya_first() -> None:
    steps = build_pip_steps(_decision(torch_path="cpu"), repair=True)
    assert steps[0][3:] == ["uninstall", "-y", "laya"]
    assert "torch" not in " ".join(steps[0])


def test_argv_uninstall_removes_laya_only() -> None:
    argv = uninstall_argv(sys.executable)
    assert argv == [sys.executable, "-m", "pip", "uninstall", "-y", "laya"]
    assert "torch" not in argv


def test_argv_venv_branch_uses_venv_interpreter() -> None:
    decision = EnvironmentDecision(2, sys.executable, Path("/tmp/cda-venv"), "cpu", "", "", False)
    steps = build_pip_steps(decision)
    assert steps[0] == [sys.executable, "-m", "venv", "/tmp/cda-venv"]
    assert steps[1][0].endswith("/tmp/cda-venv/bin/python") or steps[1][0].endswith(
        "/tmp/cda-venv/Scripts/python.exe"
    )


# ---- failure classification (§6.2) ------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "code", "expected"),
    [
        ("ERROR: externally-managed-environment", 1, "pep668"),
        ("No space left on device: /home/u/.jev/tmp/x", 1, "temp-full"),
        ("No space left on device", 1, "disk-full"),
        ("Permission denied: site-packages", 1, "permission"),
        ("Could not find a version that satisfies", 1, "network"),
        ("NameResolutionError: pypi.org", 1, "network"),
        ("Connection timed out", 1, "network"),
        ("No matching distribution found for laya", 1, "no-wheel"),
        ("DO NOT MATCH THE HASHES", 1, "hash"),
        ("", 137, "killed"),
        ("", -9, "killed"),
        ("some other pip failure", 2, "pip-failed"),
    ],
)
def test_classify_failure_marker_order(stderr: str, code: int, expected: str) -> None:
    failure_class, _message, _retryable, _guide = classify_failure(
        stderr, code, tmpdir="/home/u/.jev/tmp"
    )
    assert failure_class == expected


def test_classify_failure_marker_order_pep668_first() -> None:
    # First match wins: a PEP 668 line beats a later permission line.
    failure_class, _m, _r, guide = classify_failure(
        "externally-managed-environment\nPermission denied", 1
    )
    assert failure_class == "pep668"
    assert guide is True


def test_classify_failure_temp_full_needs_tmpdir_in_message() -> None:
    failure_class, _m, _r, _g = classify_failure("No space left on device", 1, tmpdir="/elsewhere")
    assert failure_class == "disk-full"


def test_redact_text_hides_url_credentials() -> None:
    redacted = redact_text("https://user:s3cret@host/x?token=abc&other=1")
    assert "s3cret" not in redacted
    assert "abc" not in redacted
    assert "other=1" in redacted


# ---- preflight (§6.1) ---------------------------------------------------------------


def test_preflight_disk_refusal_is_retryable(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    monkeypatch.setattr("shutil.disk_usage", lambda path: SimpleNamespace(free=0, total=0, used=0))
    runner = CdaInstallRunner(tmp_path / "repo", home=home, decision=_decision())
    with pytest.raises(runner_module.CdaInstallError, match="disk"):
        runner.start("install", MODEL)
    status = runner.status()
    assert status.state == "failed"
    assert status.failure_class == "disk-full"
    assert status.guide is False


def test_preflight_network_refusal_is_retryable(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: False)
    runner = CdaInstallRunner(tmp_path / "repo", home=home, decision=_decision())
    with pytest.raises(runner_module.CdaInstallError, match="network"):
        runner.start("install", MODEL)
    assert runner.status().failure_class == "network"


def test_preflight_permission_refusal_swaps_to_guide(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    runner = CdaInstallRunner(tmp_path / "repo", home=home, decision=_decision())
    with pytest.raises(runner_module.CdaInstallGuide):
        runner.start("install", MODEL)
    status = runner.status()
    assert status.state == "failed"
    assert status.guide is True
    assert status.failure_class == "permission"


def test_preflight_branch3_raises_guide_without_job(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    runner = CdaInstallRunner(tmp_path / "repo", home=home, decision=_decision(branch=3))
    with pytest.raises(runner_module.CdaInstallGuide):
        runner.start("install", MODEL)
    assert runner.status().state == "idle"


# ---- happy path, stop-per-phase, restart --------------------------------------------


def test_install_success_warms_the_model(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    spawn = _FakeSpawn()
    runner = CdaInstallRunner(tmp_path / "repo", home=home, spawn=spawn, decision=_decision())
    runner.start("install", MODEL)
    assert _wait_until(runner, {"done"}) == "done"
    status = runner.status()
    assert status.message == "Backend installed and warmed."
    assert status.probe is not None
    assert status.probe.warmed is True
    assert status.probe.model_id == MODEL
    assert status.probe_by_model[MODEL].warmed is True
    assert status.probe.laya_version == "0.4.15"
    assert status.probe.torch_build == "cpu"
    record = json.loads((home / ".jev" / "cda-install-job.json").read_text())
    assert record["probe_by_model"][MODEL]["warmed"] is True
    assert (record["probe"]["model_id"]) == MODEL
    log = home / ".jev" / "cda-install.log"
    assert oct(log.stat().st_mode & 0o777) == "0o600"
    assert oct((home / ".jev" / "cda-install-job.json").stat().st_mode & 0o777) == "0o700"


def test_install_pip_failure_classifies_and_offers_repair_path(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    spawn = _FakeSpawn(pip_code=1, pip_stderr="ERROR: externally-managed-environment")
    runner = CdaInstallRunner(tmp_path / "repo", home=home, spawn=spawn, decision=_decision())
    runner.start("install", MODEL)
    assert _wait_until(runner, {"failed"}) == "failed"
    status = runner.status()
    assert status.failure_class == "pep668"
    assert status.guide is True


def test_verify_refreshes_probe_by_model_entry(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    spawn = _FakeSpawn()
    runner = CdaInstallRunner(tmp_path / "repo", home=home, spawn=spawn, decision=_decision())
    runner.verify(MODEL)
    assert _wait_until(runner, {"done"}) == "done"
    status = runner.status()
    # No warm-up record existed, so verify ran steps 1-2 only: not warmed.
    assert status.probe_by_model[MODEL].warmed is False
    assert status.probe_by_model[MODEL].constructible is True


def test_verify_reruns_warmup_when_warmed_entry_exists(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    spawn = _FakeSpawn()
    runner = CdaInstallRunner(tmp_path / "repo", home=home, spawn=spawn, decision=_decision())
    runner.start("install", MODEL)
    assert _wait_until(runner, {"done"}) == "done"
    warm_calls_before = sum(1 for argv in spawn.calls if "laya.load" in " ".join(argv))
    runner.verify(MODEL)
    assert _wait_until(runner, {"done"}) == "done"
    warm_calls_after = sum(1 for argv in spawn.calls if "laya.load" in " ".join(argv))
    assert warm_calls_after > warm_calls_before
    assert runner.status().probe_by_model[MODEL].warmed is True


def test_single_flight_second_start_sees_busy(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    spawn = _FakeSpawn(block_phase="pip")
    runner = CdaInstallRunner(tmp_path / "repo", home=home, spawn=spawn, decision=_decision())
    runner.start("install", MODEL)
    assert _wait_until(runner, {"running"}) == "running"
    with pytest.raises(runner_module.CdaInstallBusy):
        runner.start("install", MODEL)
    assert runner.status().state == "running"
    assert _wait_procs(spawn, 1)
    proc = spawn.procs[0]
    assert isinstance(proc, _BlockingProc)
    proc.released.set()
    assert _wait_until(runner, {"done"}) == "done"


@pytest.mark.parametrize("phase", ["pip", "probe", "warmup"])
def test_stop_per_phase(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    spawn = _FakeSpawn(block_phase=phase)
    runner = CdaInstallRunner(tmp_path / "repo", home=home, spawn=spawn, decision=_decision())
    runner.start("install", MODEL)
    if phase == "pip":
        assert _wait_until(runner, {"running"}) == "running"
    elif phase == "probe":
        assert _wait_phase(runner, "verifying")
    else:
        assert _wait_phase(runner, "warming")
    assert runner.stop() is True
    assert _wait_until(runner, {"stopped"}) == "stopped"
    assert runner.status().message == "Install stopped. Retry to start again."
    # The stopped state is never rewritten as installed.
    assert runner.status().probe is None or runner.status().probe_by_model.get(MODEL) is None


def test_stop_during_preflight_is_a_noop_then_job_proceeds(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()

    def _blocked(url: str) -> bool:
        gate.wait(timeout=10)
        return True

    monkeypatch.setattr(runner_module, "_url_reachable", _blocked)
    spawn = _FakeSpawn()
    runner = CdaInstallRunner(tmp_path / "repo", home=home, spawn=spawn, decision=_decision())
    thread = threading.Thread(target=runner.start, args=("install", MODEL), daemon=True)
    thread.start()
    time.sleep(0.2)
    # Preflight is synchronous inside start(): no job exists to stop yet.
    assert runner.stop() is False
    gate.set()
    thread.join(timeout=10)
    assert _wait_until(runner, {"done"}) == "done"


def test_restart_mid_install_marks_interrupted_and_retry_works(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    record_path = home / ".jev" / "cda-install-job.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "job_id": "dead-job",
                "kind": "install",
                "state": "running",
                "phase": "installing",
                "message": "",
                "child_pid": 999999,  # no such process
                "probe": None,
                "probe_by_model": {},
            }
        )
    )
    runner = CdaInstallRunner(tmp_path / "repo", home=home, decision=_decision())
    status = runner.status()
    assert status.state == "interrupted"
    assert "interrupted by a restart" in status.message
    spawn = _FakeSpawn()
    runner2 = CdaInstallRunner(tmp_path / "repo", home=home, spawn=spawn, decision=_decision())
    runner2.start("install", MODEL)
    assert _wait_until(runner2, {"done"}) == "done"


def test_restart_terminates_live_child_and_marks_interrupted(tmp_path: Path, home: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record_path = home / ".jev" / "cda-install-job.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(
                {
                    "job_id": "live-job",
                    "kind": "install",
                    "state": "running",
                    "phase": "installing",
                    "message": "",
                    "child_pid": proc.pid,
                    "probe": None,
                    "probe_by_model": {},
                }
            )
        )
        runner = CdaInstallRunner(tmp_path / "repo", home=home, decision=_decision())
        assert runner.status().state == "interrupted"
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_shutdown_marks_interrupted_not_stopped(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    spawn = _FakeSpawn(block_phase="pip")
    runner = CdaInstallRunner(tmp_path / "repo", home=home, spawn=spawn, decision=_decision())
    runner.start("install", MODEL)
    assert _wait_until(runner, {"running"}) == "running"
    runner.shutdown(timeout=5.0)
    assert runner.status().state == "interrupted"


def test_fallback_record_used_when_home_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    home = tmp_path / "home"
    home.mkdir()
    # Make the primary location unusable: a file where the directory must be.
    (tmp_path / "home-file").write_text("x")
    repo = tmp_path / "repo"
    repo.mkdir()
    spawn = _FakeSpawn()
    runner = CdaInstallRunner(repo, home=tmp_path / "home-file", spawn=spawn, decision=_decision())
    runner.start("install", MODEL)
    assert _wait_until(runner, {"done"}) == "done"
    fallback = repo / ".jev" / "cda-install-job.json"
    assert fallback.exists()
    assert oct(fallback.stat().st_mode & 0o777) == "0o700"
    assert (repo / ".jev" / "cda-install.log").exists()


# ---- coordination (§6.4) ---------------------------------------------------------


def _core_with_runners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DashboardCore, CodeGradeRunner, CdaInstallRunner, _FakeSpawn]:
    monkeypatch.setattr(runner_module, "_url_reachable", lambda url: True)
    core = DashboardCore.__new__(DashboardCore)
    core.code_grade_runner = CodeGradeRunner(ledger_path=None)
    spawn = _FakeSpawn(block_phase="pip")
    core.cda_install_runner = CdaInstallRunner(
        tmp_path / "repo", home=tmp_path / "home", spawn=spawn, decision=_decision()
    )
    core._coord_lock = threading.Lock()
    core._coord_install_claim = False
    return core, core.code_grade_runner, core.cda_install_runner, spawn


def test_coordination_install_blocks_grade_and_vice_versa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core, _grade, install, spawn = _core_with_runners(tmp_path, monkeypatch)
    # Install claimed: a grade is refused.
    assert core.try_begin_install() is None
    assert core.try_begin_grade() == "install"
    core.end_install()
    assert core.try_begin_grade() is None
    # A running install blocks a grade; a grade blocks an install.
    install.start("install", MODEL)
    assert _wait_until(install, {"running"}) == "running"
    assert core.try_begin_grade() == "install"
    assert _wait_procs(spawn, 1)
    proc = spawn.procs[0]
    assert isinstance(proc, _BlockingProc)
    proc.released.set()
    assert _wait_until(install, {"done"}) == "done"
    assert core.try_begin_grade() is None


def test_coordination_grade_blocks_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    core, grade, _install, _spawn = _core_with_runners(tmp_path, monkeypatch)
    gate = threading.Event()

    def _blocking(
        kind: str, settings: CodeOrchestratorSettings, inputs: dict[str, str]
    ) -> dict[str, Any]:
        gate.wait(timeout=10)
        return {}

    grade.executor = _blocking
    grade.start(
        kind="grade",
        settings=CodeOrchestratorSettings(),
        inputs={"task": "t", "diff": "d", "tests": ""},
        task="t",
    )
    assert grade.busy() is True
    assert core.try_begin_install() == "grade"
    assert core.try_begin_grade() == "grade"
    gate.set()
    deadline = time.monotonic() + 10
    while grade.busy() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert grade.busy() is False
    assert core.try_begin_install() is None
    core.end_install()


def test_coordination_verify_exempt_from_grade_block_but_not_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Verify is a probe, not a mutating job: mutating_busy stays False while
    # it runs, so try_begin_grade is not blocked by it (the route refuses
    # verify while a mutating job runs — the reverse direction).
    _core, _grade, install, _spawn = _core_with_runners(tmp_path, monkeypatch)
    install.verify(MODEL)
    assert _wait_until(install, {"done"}) == "done"
    assert install.mutating_busy() is False
