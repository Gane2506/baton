"""One-click Coding Decision Agent backend install from the dashboard (issue #366).

`CdaInstallRunner` is the deliberate third copy (design §9, B2): it mirrors
`BrowserInstallRunner`'s shape — one job at a time, output streaming to a log
file, stop by terminating the child — and does not share code with it, so no
working runner regresses. It owns what the browser runner does not: the fixed
pip argv sequences (design §5.3a), preflight (§6.1), phased execution (§6.3),
the subprocess verification probe (§5.2, never an in-process import), the
durable job record (§6.5), failure classification (§6.2) and attempt events
(§6.10).

Security invariants (design §7): the argv is built server-side from
`sys.executable` plus the torch-path decision only. No package name, flag,
version or index URL ever comes from a request — form posts carry only the
CSRF token (plus confirmation on the confirm page). `jev-agent` is never
installed or upgraded by any sequence, the subprocess is spawned with an argv
list (never through a shell), and `--break-system-packages` is never passed:
a PEP 668 refusal goes to the branch-3 guide instead.

"Installed" (design §5.1, A2): the panel flips to "warmed" only for the
warmed model id, from the `probe_by_model` map in the job record. Another
repo's panel shows "available" for a shared environment, never "warmed" for
a model id it did not warm.

Branch 2 note (design §5.4): the decision tree reserves the dedicated venv
at `~/.jev/cda-venv` and the argv builder builds its sequences, but the
branch is NOT selected today: later grades run in the dashboard process, so
a venv-only install would be invisible to grading until the grade runner
gains an interpreter override, which §8 does not list and §12 Q6 leaves open.
Per §5.4's own fallback, an unwritable own environment therefore falls to
branch 3 (guide) instead of promising a venv install grading cannot use.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "CDA_CPU_INDEX",
    "CdaInstallBusy",
    "CdaInstallError",
    "CdaInstallGuide",
    "CdaInstallRunner",
    "CdaInstallStatus",
    "EnvironmentDecision",
    "ProbeOutcome",
    "build_pip_steps",
    "classify_failure",
    "decide_environment",
    "probe_argv",
    "redact_text",
    "temp_space_check",
    "uninstall_argv",
    "venv_argv",
    "warmup_argv",
]

_JOB_FILENAME = "cda-install-job.json"
_LOG_FILENAME = "cda-install.log"
_LOG_TAIL_LINES = 30
_TAIL_READ_BYTES = 65_536
_MAX_LOG_BYTES = 262_144
_PROBE_TIMEOUT_S = 300.0
_NETWORK_TIMEOUT_S = 10.0
_SHUTDOWN_TIMEOUT_S = 30.0
_STAGING_BYTES = 1_073_741_824  # 1 GiB of temp staging, on top of the packages
# ~5 GB: 2.5 GB torch + 0.5 GB laya + 0.842 GB ckpt + 1 GB staging
_DEFAULT_DISK_BYTES = 5_368_709_120
# ~3 GB: 0.2 GB CPU torch + 0.5 GB laya + 0.842 GB ckpt + 1 GB staging
_CPU_DISK_BYTES = 3_221_225_472
_MEMORY_WARN_BYTES = 3_221_225_472  # below 3 GB free, grading may be slow: a warning, not a refusal
_LAYA_SPEC = "laya>=0.4.15"
CDA_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
_PYPI_SIMPLE = "https://pypi.org/simple/laya/"
_HF_HOST = "https://huggingface.co"
_VENV_DIRNAME = "cda-venv"
_PROBE_MARKER = "CDA_PROBE_JSON "
_WARM_MARKER = "CDA_WARM_OK "
# The dedicated-venv branch (§5.4) stays unselected until the grade runner
# gains an interpreter override (see the module docstring); see decide_environment.
_VENV_GRADE_OVERRIDE_AVAILABLE = False


class CdaInstallBusy(RuntimeError):
    """An install, repair, uninstall or verify job is already running."""


class CdaInstallError(RuntimeError):
    """The job could not be started, or preflight refused it (retryable)."""


class CdaInstallGuide(RuntimeError):
    """No job can start here: the page must render the branch-3 guide instead.

    Carries the environment-specific command the guide shows. Raised for a
    branch-3 decision and for non-retryable preflight failures (permission,
    PEP 668, no wheel), per design §6.2/R4.
    """

    def __init__(self, message: str, guide_command: str) -> None:
        super().__init__(message)
        self.guide_command = guide_command


@dataclass(frozen=True)
class EnvironmentDecision:
    """The server-side target-environment decision (design §5.4)."""

    branch: int  # 1 = own interpreter, 2 = dedicated venv (reserved), 3 = guide
    interpreter: str
    venv_path: Path | None
    torch_path: str  # "cpu" | "default"
    persistence: str  # the UI statement for this deployment, before any click
    guide_command: str  # the environment-specific command, for branch 3 / guide
    docker: bool


@dataclass(frozen=True)
class ProbeOutcome:
    """One probe's verdict (design §5.2): import plus wiring, plus warmth."""

    laya_version: str
    torch_version: str
    torch_build: str  # "cpu" | "cuda" | ""
    constructible: bool
    warmed: bool
    model_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "laya_version": self.laya_version,
            "torch_version": self.torch_version,
            "torch_build": self.torch_build,
            "constructible": self.constructible,
            "warmed": self.warmed,
            "model_id": self.model_id,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> ProbeOutcome:
        return ProbeOutcome(
            laya_version=str(raw.get("laya_version") or ""),
            torch_version=str(raw.get("torch_version") or ""),
            torch_build=str(raw.get("torch_build") or ""),
            constructible=bool(raw.get("constructible")),
            warmed=bool(raw.get("warmed")),
            model_id=str(raw.get("model_id") or ""),
        )


@dataclass(frozen=True)
class CdaInstallStatus:
    """What the install status page and the Code Grade panel show."""

    state: str  # "idle" | "running" | "done" | "failed" | "stopped" | "interrupted"
    kind: str  # "install" | "repair" | "uninstall" | "verify" | ""
    phase: str  # "preflight" | "installing" | "verifying" | "warming" | ""
    message: str
    failure_class: str  # "" when none; a §6.2 class otherwise
    guide: bool  # True: swap the button for the guide block (§6.2/R4)
    guide_command: str
    target: str  # branch label + interpreter path
    torch_path: str
    interpreter: str
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    probe: ProbeOutcome | None  # the latest install's verdict
    probe_by_model: dict[str, ProbeOutcome]  # keyed by model id (§6.5/R1)
    preflight: tuple[tuple[str, bool, str], ...]  # (check, passed, detail)
    log_tail: tuple[str, ...]
    events: tuple[dict[str, Any], ...]


# ---- fixed argv sequences (design §5.3a) ---------------------------------------
# Every sequence is built from the interpreter plus the torch-path decision
# only. `jev-agent` appears in none of them, on any branch.


def _pip(interpreter: str) -> list[str]:
    return [interpreter, "-m", "pip"]


def venv_argv(interpreter: str, venv_path: Path) -> list[str]:
    """Branch-2 venv creation (reserved: see the module docstring)."""
    return [interpreter, "-m", "venv", str(venv_path)]


def uninstall_argv(interpreter: str) -> list[str]:
    """Uninstall `laya` only — never torch, never `jev-agent` (design §9, G2)."""
    return [*_pip(interpreter), "uninstall", "-y", "laya"]


def build_pip_steps(decision: EnvironmentDecision, *, repair: bool = False) -> list[list[str]]:
    """The pip steps for an install (or a repair, which uninstalls first).

    Default path: one `laya` step. CPU path: the CPU torch step first, then
    `laya` — pip keeps the CPU torch already installed, because it does not
    replace a requirement that is already satisfied; no `--no-deps` flag is
    used, because skipping dependency checks would also skip transformers.
    """
    steps: list[list[str]] = []
    if decision.branch == 2 and decision.venv_path is not None:
        steps.append(venv_argv(decision.interpreter, decision.venv_path))
    interpreter = _venv_interpreter(decision)
    if repair:
        steps.append(uninstall_argv(interpreter))
    if decision.torch_path == "cpu":
        steps.append([*_pip(interpreter), "install", "torch", "--index-url", CDA_CPU_INDEX])
    steps.append([*_pip(interpreter), "install", _LAYA_SPEC])
    return steps


def _venv_interpreter(decision: EnvironmentDecision) -> str:
    if decision.branch == 2 and decision.venv_path is not None:
        if sys.platform == "win32":
            return str(decision.venv_path / "Scripts" / "python.exe")
        return str(decision.venv_path / "bin" / "python")
    return decision.interpreter


def probe_argv(interpreter: str, model_id: str) -> list[str]:
    """Steps 1-2 of the §5.2 probe: import, versions, construct without loading.

    Construction is lazy (`LayaBackend.__init__` only stores the model id),
    so this proves import plus wiring and nothing about the checkpoint — and
    it never imports torch into the dashboard process itself. The probe
    prints one machine-readable line for the runner to parse out of the log.
    """
    script = (
        "import json; "
        "import laya, torch; "
        "from jev_agent.code_orchestrator.laya_backend import LayaBackend; "
        f"backend = LayaBackend({model_id!r}); "
        "print('CDA_PROBE_JSON ' + json.dumps({'laya': getattr(laya, '__version__', ''), "
        "'torch': getattr(torch, '__version__', ''), "
        "'cuda': getattr(torch.version, 'cuda', None), 'constructible': True, "
        "'model': backend.model_id}))"
    )
    return [interpreter, "-c", script]


def warmup_argv(interpreter: str, model_id: str) -> list[str]:
    """Step 3 of the §5.2 probe (A2 only): load the checkpoint, grade nothing."""
    script = (
        "import laya; "
        f"laya.load({model_id!r}); "
        f"print({_WARM_MARKER.strip()!r} + ' ' + {model_id!r})"
    )
    return [interpreter, "-c", script]


# ---- failure classification (design §6.2) --------------------------------------
# Marker order is first-match-wins: pip stderr markers, then exit code, then
# probe output. The message is plain English, never pip's raw output (the log
# tail stays available for diagnosis).

# (failure_class, message, retryable, guide) — first match wins, in order.
_MARKER_TABLE: tuple[tuple[str, str, str, bool, bool], ...] = (
    (
        "externally-managed-environment",
        "pep668",
        "This Python refuses direct installs. Use a virtual environment.",
        False,
        True,
    ),
    (
        "Permission denied",
        "permission",
        "The dashboard cannot write to its Python environment.",
        False,
        True,
    ),
    (
        "Could not find a version",
        "network",
        "Could not reach the package index. Check the network or proxy settings.",
        True,
        False,
    ),
    (
        "NameResolutionError",
        "network",
        "Could not reach the package index. Check the network or proxy settings.",
        True,
        False,
    ),
    (
        "Connection",
        "network",
        "Could not reach the package index. Check the network or proxy settings.",
        True,
        False,
    ),
    (
        "No matching distribution",
        "no-wheel",
        "No installable package for this Python version or machine.",
        False,
        True,
    ),
    (
        "DO NOT MATCH THE HASHES",
        "hash",
        "The download did not match its expected checksum. Retry.",
        True,
        False,
    ),
)


def classify_failure(
    stderr_text: str, exit_code: int | None, *, tmpdir: str = ""
) -> tuple[str, str, bool, bool]:
    """Classify a failed pip step. Returns (class, message, retryable, guide).

    "No space left" is checked before the generic markers because its message
    splits on whether the temp directory is the full one (design §6.2).
    """
    if "No space left" in stderr_text:
        if tmpdir and tmpdir in stderr_text:
            return (
                "temp-full",
                "Ran out of space in the temp directory. Point TMPDIR at a disk "
                "with free space and retry.",
                True,
                False,
            )
        return "disk-full", "Ran out of disk space. Free space and retry.", True, False
    for marker, failure_class, message, retryable, guide in _MARKER_TABLE:
        if marker in stderr_text:
            return failure_class, message, retryable, guide
    lowered = stderr_text.lower()
    if "lock" in lowered and ("another pip" in lowered or "is locked" in lowered):
        return (
            "pip-lock",
            "A previous install left a lock behind. Clear the pip cache and retry.",
            True,
            False,
        )
    if exit_code in (137, -9):
        return "killed", "The install was interrupted. Retry.", True, False
    code = "?" if exit_code is None else str(exit_code)
    return "pip-failed", f"pip failed (exit {code}); see the log", True, False


# ---- log redaction (design §6.10) ----------------------------------------------

_CREDENTIAL_URL = re.compile(r"(://[^:/\s]+:)[^@\s]+@")
_QUERY_SECRET = re.compile(r"((?:token|api_key|apikey|password)=)[^\s&]+")


def redact_text(text: str) -> str:
    """Redact credential-looking tokens in URLs before storage (design §6.10)."""
    text = _CREDENTIAL_URL.sub(r"\1***@", text)
    return _QUERY_SECRET.sub(r"\1***", text)


# ---- environment detection (design §5.3, §5.4) ----------------------------------


def _nvidia_smi_ok() -> bool:
    """Linux/Windows default path only when an NVIDIA driver query succeeds."""
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        proc = subprocess.run(
            ["nvidia-smi"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _is_docker() -> bool:
    return Path("/.dockerenv").exists()


def _site_packages_dir(interpreter: str) -> Path:
    if interpreter == sys.executable:
        return Path(sysconfig.get_path("purelib"))
    # A non-dashboard interpreter is only ever a venv interpreter here; its
    # site-packages sits beside it. This path is used for writability only.
    parent = Path(interpreter).parent
    if parent.name in ("bin", "Scripts"):
        lib = parent.parent / "lib"
        if lib.is_dir():
            for child in lib.iterdir():
                if child.name.startswith("python") and (child / "site-packages").is_dir():
                    return child / "site-packages"
        return parent.parent / "site-packages"
    return parent


def _guide_command(interpreter: str, torch_path: str) -> str:
    """The environment-specific command the branch-3 guide shows (exempt from
    the retired-pip-instruction criterion, design §10)."""
    if torch_path == "cpu":
        return (
            f"{interpreter} -m pip install torch --index-url {CDA_CPU_INDEX}\n"
            f"{interpreter} -m pip install {_LAYA_SPEC!r}"
        )
    return f"{interpreter} -m pip install {_LAYA_SPEC!r}"


def _persistence_text(docker: bool, branch: int) -> str:
    if docker:
        # I2: documented rebuild plus reinstall — no deploy-contract change,
        # no volume. A container recreate wipes a container-side install.
        return (
            "Installs into the dashboard's Python environment inside this container. "
            "A container recreate wipes it, and the button is offered again afterwards."
        )
    if branch == 3:
        return "The dashboard cannot install into its Python environment from here."
    if "uv" in sys.executable and "tools" in sys.executable:
        return "Installs into the dashboard's tool environment. Reinstalling the tool removes it."
    return "Installs into the dashboard's Python environment."


def decide_environment(
    interpreter: str = sys.executable,
    *,
    home: Path | None = None,
    nvidia_ok: bool | None = None,
    docker: bool | None = None,
) -> EnvironmentDecision:
    """Decide the target environment server-side, in §5.4 order (read-only:
    it creates nothing, so page renders can call it freely).

    macOS: CPU path, always. Linux/Windows: default path only when
    `nvidia-smi` exits 0, else CPU. A wrong answer costs size, not breakage:
    the default wheel runs on a CPU-only host too.
    """
    home = home or Path.home()
    if sys.platform == "darwin":
        torch_path = "cpu"
    else:
        nvidia = nvidia_ok if nvidia_ok is not None else _nvidia_smi_ok()
        torch_path = "default" if nvidia else "cpu"
    is_docker = docker if docker is not None else _is_docker()
    site_packages = _site_packages_dir(interpreter)
    try:
        own_writable = os.access(site_packages, os.W_OK)
    except OSError:
        own_writable = False
    if own_writable:
        return EnvironmentDecision(
            branch=1,
            interpreter=interpreter,
            venv_path=None,
            torch_path=torch_path,
            persistence=_persistence_text(is_docker, 1),
            guide_command=_guide_command(interpreter, torch_path),
            docker=is_docker,
        )
    # Branch 2 (dedicated venv) is reserved until the grade runner gains an
    # interpreter override (module docstring); §5.4 falls through to branch 3.
    if _VENV_GRADE_OVERRIDE_AVAILABLE:
        venv_path = home / ".jev" / _VENV_DIRNAME
        venv_python = (
            str(venv_path / "Scripts" / "python.exe")
            if sys.platform == "win32"
            else str(venv_path / "bin" / "python")
        )
        return EnvironmentDecision(
            branch=2,
            interpreter=interpreter,
            venv_path=venv_path,
            torch_path=torch_path,
            persistence=_persistence_text(is_docker, 2),
            guide_command=_guide_command(venv_python, torch_path),
            docker=is_docker,
        )
    return EnvironmentDecision(
        branch=3,
        interpreter=interpreter,
        venv_path=None,
        torch_path=torch_path,
        persistence=_persistence_text(is_docker, 3),
        guide_command=_guide_command(interpreter, torch_path),
        docker=is_docker,
    )


def temp_space_check() -> tuple[int, bool]:
    """Grade-time temp space (design §8): free bytes in the temp directory and
    whether it clears the §6.1 staging need (1 GiB). A dashboard started with
    a small tmpfs still fails first grades until restarted with a real-disk
    TMPDIR — the diagnostics page warns on this, it does not fix it."""
    tmpdir = tempfile.gettempdir()
    try:
        free = shutil.disk_usage(tmpdir).free
    except OSError:
        return 0, False
    return free, free >= _STAGING_BYTES


# ---- process spawn ---------------------------------------------------------------


def _spawn(argv: list[str], log: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    """Start a child with its output appended to `log`. Indirection for tests."""
    handle = log.open("ab")
    try:
        return subprocess.Popen(
            argv, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env
        )
    except BaseException:
        handle.close()
        raise


SpawnFn = Callable[[list[str], Path, dict[str, str]], Any]


@dataclass
class _Job:
    kind: str
    model_id: str
    decision: EnvironmentDecision
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class CdaInstallRunner:
    """One CDA install/repair/uninstall/verify job at a time (design §6)."""

    def __init__(
        self,
        root: Path,
        *,
        home: Path | None = None,
        spawn: SpawnFn = _spawn,
        decision: EnvironmentDecision | None = None,
        on_finish: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
        self._home = home or Path.home()
        self._spawn = spawn
        self._fixed_decision = decision
        self._on_finish = on_finish
        self._guard = threading.Lock()
        self._proc: Any = None
        self._thread: threading.Thread | None = None
        self._state = "idle"
        self._kind = ""
        self._phase = ""
        self._message = ""
        self._failure_class = ""
        self._guide = False
        self._guide_command = ""
        self._target = ""
        self._torch_path = ""
        self._interpreter = ""
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._exit_code: int | None = None
        self._probe: ProbeOutcome | None = None
        self._probe_by_model: dict[str, ProbeOutcome] = {}
        self._preflight_results: list[tuple[str, bool, str]] = []
        self._events: list[dict[str, Any]] = []
        self._record_path: Path | None = None
        self._log_path: Path | None = None
        self._child_pid: int | None = None
        self._job_id = ""
        self._stop_requested = False
        self._reconcile_on_start()

    # ---- durable record (§6.5) ---------------------------------------------------

    def _primary_dir(self) -> Path:
        return self._home / ".jev"

    def _primary_record(self) -> Path:
        return self._primary_dir() / _JOB_FILENAME

    def _fallback_record(self) -> Path:
        return self.root / ".jev" / _JOB_FILENAME

    def _primary_log(self) -> Path:
        return self._primary_dir() / _LOG_FILENAME

    def _fallback_log(self) -> Path:
        return self.root / ".jev" / _LOG_FILENAME

    def _record_locations(self) -> tuple[Path, Path]:
        return self._primary_record(), self._fallback_record()

    def _read_record(self) -> dict[str, Any] | None:
        """Primary first; the fallback only when no primary record exists. A
        record in the non-chosen location never authorizes a second job."""
        for path in self._record_locations():
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if isinstance(data, dict):
                self._record_path = path
                return data
        return None

    def _choose_record_location(self) -> Path | None:
        """A writable record location, or None (then the page renders the guide)."""
        for directory, path in (
            (self._primary_dir(), self._primary_record()),
            (self.root / ".jev", self._fallback_record()),
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                os.chmod(directory, 0o700)
                probe = directory / ".cda-install-write-test"
                probe.write_bytes(b"")
                probe.unlink()
            except OSError:
                continue
            return path
        return None

    def _log_for_record(self, record_path: Path) -> Path:
        if record_path == self._primary_record():
            return self._primary_log()
        return self._fallback_log()

    def _write_record(self) -> None:
        if self._record_path is None:
            return
        with self._guard:
            payload = self._record_payload_locked()
        try:
            self._record_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self._record_path.parent, 0o700)
            tmp = self._record_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.chmod(tmp, 0o700)
            os.replace(tmp, self._record_path)
            os.chmod(self._record_path, 0o700)
        except OSError:
            pass

    def _record_payload_locked(self) -> dict[str, Any]:
        return {
            "job_id": self._job_id,
            "kind": self._kind,
            "state": self._state,
            "phase": self._phase,
            "message": self._message,
            "failure_class": self._failure_class,
            "guide": self._guide,
            "guide_command": self._guide_command,
            "target": self._target,
            "torch_path": self._torch_path,
            "interpreter": self._interpreter,
            "child_pid": self._child_pid,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "finished_at": self._finished_at.isoformat() if self._finished_at else None,
            "exit_code": self._exit_code,
            "probe": self._probe.as_dict() if self._probe else None,
            "probe_by_model": {
                model: outcome.as_dict() for model, outcome in self._probe_by_model.items()
            },
            "preflight": [
                {"check": name, "passed": passed, "detail": detail}
                for name, passed, detail in self._preflight_results
            ],
            "events": self._events[-50:],
        }

    def _load_into_memory(self, data: dict[str, Any]) -> None:
        def _when(raw: Any) -> datetime | None:
            if isinstance(raw, str):
                try:
                    return datetime.fromisoformat(raw)
                except ValueError:
                    return None
            return None

        self._job_id = str(data.get("job_id") or "")
        self._kind = str(data.get("kind") or "")
        self._state = str(data.get("state") or "idle")
        self._phase = str(data.get("phase") or "")
        self._message = str(data.get("message") or "")
        self._failure_class = str(data.get("failure_class") or "")
        self._guide = bool(data.get("guide"))
        self._guide_command = str(data.get("guide_command") or "")
        self._target = str(data.get("target") or "")
        self._torch_path = str(data.get("torch_path") or "")
        self._interpreter = str(data.get("interpreter") or "")
        self._started_at = _when(data.get("started_at"))
        self._finished_at = _when(data.get("finished_at"))
        exit_code = data.get("exit_code")
        self._exit_code = int(exit_code) if isinstance(exit_code, int) else None
        probe = data.get("probe")
        self._probe = ProbeOutcome.from_dict(probe) if isinstance(probe, dict) else None
        by_model = data.get("probe_by_model")
        self._probe_by_model = (
            {
                str(model): ProbeOutcome.from_dict(outcome)
                for model, outcome in by_model.items()
                if isinstance(outcome, dict)
            }
            if isinstance(by_model, dict)
            else {}
        )
        preflight = data.get("preflight")
        self._preflight_results = (
            [
                (
                    str(item.get("check") or ""),
                    bool(item.get("passed")),
                    str(item.get("detail") or ""),
                )
                for item in preflight
                if isinstance(item, dict)
            ]
            if isinstance(preflight, list)
            else []
        )
        events = data.get("events")
        self._events = (
            [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
        )
        self._log_path = (
            self._log_for_record(self._record_path) if self._record_path is not None else None
        )

    def _reconcile_on_start(self) -> None:
        """Read the durable record on start (§6.5): a record that says
        "running" becomes `interrupted` — its child belongs to a dead
        dashboard, so a live pid is terminated first. Best-effort: a broken
        record never breaks dashboard startup."""
        try:
            data = self._read_record()
        except Exception as exc:
            # Best-effort by design (a broken record never breaks startup),
            # but the failure is still recorded — a silently lost record
            # must leave a trace somewhere.
            log.debug("cda install record read failed: %s: %s", type(exc).__name__, exc)
            return
        if data is None:
            return
        try:
            self._load_into_memory(data)
            if self._state != "running":
                return
            pid = data.get("child_pid")
            if isinstance(pid, int):
                try:
                    os.kill(pid, 0)
                except OSError:
                    pass
                else:
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGTERM)
            self._state = "interrupted"
            self._phase = ""
            self._message = "The last install was interrupted by a restart. Retry to start again."
            self._child_pid = None
            self._record_event("interruption", [], "interrupted")
            self._write_record()
        except Exception as exc:
            log.debug("cda install reconcile failed: %s: %s", type(exc).__name__, exc)
            return

    # ---- state -------------------------------------------------------------------

    def status(self) -> CdaInstallStatus:
        with self._guard:
            state = self._state
            kind = self._kind
            phase = self._phase
            message = self._message
            failure_class = self._failure_class
            guide = self._guide
            guide_command = self._guide_command
            target = self._target
            torch_path = self._torch_path
            interpreter = self._interpreter
            started_at = self._started_at
            finished_at = self._finished_at
            exit_code = self._exit_code
            probe = self._probe
            by_model = dict(self._probe_by_model)
            preflight = tuple(self._preflight_results)
            events = tuple(self._events[-20:])
            log_path = self._log_path
        return CdaInstallStatus(
            state=state,
            kind=kind,
            phase=phase,
            message=message,
            failure_class=failure_class,
            guide=guide,
            guide_command=guide_command,
            target=target,
            torch_path=torch_path,
            interpreter=interpreter,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=exit_code,
            probe=probe,
            probe_by_model=by_model,
            preflight=preflight,
            log_tail=self._tail(log_path) if log_path is not None else (),
            events=events,
        )

    def _tail(self, log_path: Path) -> tuple[str, ...]:
        try:
            with log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                start = max(0, size - _TAIL_READ_BYTES)
                handle.seek(start)
                data = handle.read()
        except OSError:
            return ()
        if start and b"\n" in data:
            _, _, data = data.partition(b"\n")
        lines = data.decode("utf-8", errors="replace").splitlines()
        return tuple(lines[-_LOG_TAIL_LINES:])

    def _cap_and_redact_log(self) -> None:
        """Cap the retained log at 256 KiB and redact credential-looking tokens
        (§6.10). Called only once the job has finished, so no writer holds it."""
        if self._log_path is None:
            return
        try:
            data = self._log_path.read_bytes()
        except OSError:
            return
        text = redact_text(data.decode("utf-8", errors="replace"))
        encoded = text.encode("utf-8")
        if len(encoded) > _MAX_LOG_BYTES:
            encoded = encoded[-_MAX_LOG_BYTES:]
            if b"\n" in encoded:
                _, _, encoded = encoded.partition(b"\n")
        try:
            self._log_path.write_bytes(encoded)
            os.chmod(self._log_path, 0o600)
        except OSError:
            pass

    def busy(self) -> bool:
        """Any job running, including a verify probe."""
        with self._guard:
            return self._state == "running"

    def mutating_busy(self) -> bool:
        """An install, repair or uninstall running — the §6.4 coordinator's
        block. A verify probe is exempt (it mutates nothing)."""
        with self._guard:
            return self._state == "running" and self._kind in ("install", "repair", "uninstall")

    def _record_event(self, kind: str, argv: list[str], outcome: str) -> None:
        """One structured attempt event (§6.10): timestamp, argv, target,
        outcome class. Args are logged as a list, never a shell string."""
        self._events.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "event": kind,
                "argv": [redact_text(arg) for arg in argv],
                "target": self._target,
                "outcome": outcome,
            }
        )

    # ---- preflight (§6.1) ----------------------------------------------------------

    def _decision(self) -> EnvironmentDecision:
        if self._fixed_decision is not None:
            return self._fixed_decision
        return decide_environment(home=self._home)

    def _tmpdir(self) -> Path:
        """A real-disk temp directory for install subprocesses (home disk by
        default, never a 512M tmpfs `/tmp`)."""
        for base in (self._primary_dir(), self.root / ".jev"):
            try:
                base.mkdir(parents=True, exist_ok=True)
                os.chmod(base, 0o700)
                candidate = base / "tmp"
                candidate.mkdir(parents=True, exist_ok=True)
                os.chmod(candidate, 0o700)
                return candidate
            except OSError:
                continue
        return Path(tempfile.gettempdir())

    def _subprocess_env(self) -> dict[str, str]:
        tmpdir = str(self._tmpdir())
        env = dict(os.environ)
        env["TMPDIR"] = tmpdir
        if sys.platform == "win32":
            env["TEMP"] = tmpdir
            env["TMP"] = tmpdir
        return env

    def _preflight(
        self, kind: str, decision: EnvironmentDecision
    ) -> tuple[list[tuple[str, bool, str]], str | None, bool, str]:
        """Run the §6.1 checks. Returns (results, refusal_message, guide,
        failure_class).

        `guide` True means a non-retryable failure: the button swaps for the
        guide block (§6.2/R4), including when first learned at preflight.
        """
        results: list[tuple[str, bool, str]] = []
        refusal: str | None = None
        guide = False
        failure_class = ""

        def fail(message: str, *, is_guide: bool, klass: str) -> None:
            nonlocal refusal, guide, failure_class
            if refusal is None:
                refusal, guide, failure_class = message, is_guide, klass

        # Writable target environment (branch decision, §5.4).
        if decision.branch == 1:
            writable = False
            try:
                writable = os.access(_site_packages_dir(decision.interpreter), os.W_OK)
            except OSError:
                writable = False
            results.append(("Target environment writable", writable, decision.interpreter))
            if not writable:
                fail(
                    "The dashboard cannot write to its Python environment.",
                    is_guide=True,
                    klass="permission",
                )
        elif decision.branch == 2 and decision.venv_path is not None:
            try:
                decision.venv_path.parent.mkdir(parents=True, exist_ok=True)
                writable = os.access(decision.venv_path.parent, os.W_OK)
            except OSError:
                writable = False
            results.append(("Venv location writable", writable, str(decision.venv_path)))
            if not writable:
                fail(
                    "The dashboard cannot write to its Python environment.",
                    is_guide=True,
                    klass="permission",
                )
        else:
            results.append(("Target environment writable", False, "no writable environment"))
            fail(
                "The dashboard cannot write to its Python environment.",
                is_guide=True,
                klass="permission",
            )

        # Disk: target disk and temp directory, against the §6.1 thresholds.
        if kind != "uninstall":
            need = _CPU_DISK_BYTES if decision.torch_path == "cpu" else _DEFAULT_DISK_BYTES
            try:
                free_target = shutil.disk_usage(_site_packages_dir(decision.interpreter)).free
            except OSError:
                free_target = 0
            try:
                free_tmp = shutil.disk_usage(str(self._tmpdir())).free
            except OSError:
                free_tmp = 0
            enough = free_target >= need and free_tmp >= _STAGING_BYTES
            results.append(
                (
                    "Disk",
                    enough,
                    f"need about {need // 1_073_741_824} GB free, "
                    f"have {free_target // 1_073_741_824} GB",
                )
            )
            if not enough:
                fail(
                    "Ran out of disk space. Free space and retry.",
                    is_guide=False,
                    klass="disk-full",
                )
            # Memory is a warning, never a refusal: memory at install time
            # does not predict memory at grade time.
            free_mem = _free_memory_bytes()
            if free_mem is None:
                results.append(("Memory", True, "free memory could not be measured"))
            elif free_mem < _MEMORY_WARN_BYTES:
                results.append(
                    (
                        "Memory",
                        True,
                        f"only {free_mem // 1_073_741_824} GB free — grading needs about "
                        "2-3 GB while running, so grades may be slow or fail",
                    )
                )
            else:
                results.append(("Memory", True, "grading needs about 2-3 GB while running"))
            # Network reachability, from the dashboard process, 10 s each.
            # Skipped once a refusal already exists: a disk refusal must not
            # cost three more 10-second network timeouts before it surfaces.
            urls = [_PYPI_SIMPLE]
            if decision.torch_path == "cpu":
                urls.append(CDA_CPU_INDEX)
            if kind in ("install", "repair"):
                urls.append(_HF_HOST)
            if refusal is None:
                for url in urls:
                    reachable = _url_reachable(url)
                    results.append(("Network reachable", reachable, url))
                    if not reachable:
                        fail(
                            "Could not reach the package index. "
                            "Check the network or proxy settings.",
                            is_guide=False,
                            klass="network",
                        )
                        break
        return results, refusal, guide, failure_class

    # ---- control -------------------------------------------------------------------

    def start(self, kind: str, model_id: str) -> None:
        """Start an install/repair/uninstall/verify job.

        Preflight runs synchronously here, before any download; the pip,
        probe and warm-up steps run in a watcher thread. Raises
        `CdaInstallBusy` when a job runs, `CdaInstallGuide` when no job can
        start (the page renders the guide), `CdaInstallError` on a retryable
        preflight refusal (the failed record is already written).
        """
        with self._guard:
            if self._state == "running":
                raise CdaInstallBusy("a backend install job is already running")
        decision = self._decision()
        if decision.branch == 3:
            raise CdaInstallGuide(
                "The dashboard cannot install into its Python environment from here.",
                decision.guide_command,
            )
        record_path = self._choose_record_location()
        if record_path is None:
            raise CdaInstallGuide(
                "No writable location for the install job record.",
                decision.guide_command,
            )
        self._record_path = record_path
        self._log_path = self._log_for_record(record_path)
        interpreter = _venv_interpreter(decision)
        if kind == "verify":
            results: list[tuple[str, bool, str]] = []
            refusal = None
            guide = False
            preflight_class = ""
        else:
            results, refusal, guide, preflight_class = self._preflight(kind, decision)
        with self._guard:
            self._kind = kind
            self._phase = "preflight"
            self._target = f"branch {decision.branch}: {interpreter}"
            self._torch_path = decision.torch_path
            self._interpreter = interpreter
            self._guide_command = decision.guide_command
            self._preflight_results = results
            self._failure_class = ""
            self._guide = False
            self._exit_code = None
            self._job_id = str(uuid.uuid4())
            self._started_at = datetime.now(UTC)
            self._finished_at = None
            self._message = ""
        if refusal is not None:
            with self._guard:
                self._state = "failed"
                self._message = refusal
                self._guide = guide
                self._failure_class = preflight_class
                self._finished_at = datetime.now(UTC)
                self._record_event("failure", [], self._failure_class)
            self._write_record()
            if guide:
                raise CdaInstallGuide(refusal, decision.guide_command)
            raise CdaInstallError(refusal)
        with self._guard:
            self._state = "running"
            self._record_event("start", [], kind)
        assert self._log_path is not None
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self._log_path.parent, 0o700)
            self._log_path.write_bytes(b"")
            os.chmod(self._log_path, 0o600)
        except OSError:
            pass
        self._write_record()
        job = _Job(kind=kind, model_id=model_id, decision=decision, job_id=self._job_id)
        thread = threading.Thread(
            target=self._watch, args=(job,), name="jev-cda-install", daemon=True
        )
        with self._guard:
            self._thread = thread
        try:
            thread.start()
        except BaseException:
            with self._guard:
                self._state = "idle"
                self._thread = None
            raise

    def verify(self, model_id: str) -> None:
        """Re-run the §5.2 probe (step 3 only when a warm-up record exists
        for that model). No preflight, no coordinator claim — the route
        refuses verify while a mutating job runs (§6.4/R9)."""
        self.start("verify", model_id)

    def stop(self) -> bool:
        """Terminate the running child (E1, like the browser install); False
        when no job is running. The watcher keeps "stopped" (or "interrupted"
        from shutdown) — a stop never becomes a success or failure."""
        with self._guard:
            proc = self._proc
            if self._state != "running":
                return False
            # A running job between steps has no child yet; still stoppable.
            # The record write happens below, outside the guard: _write_record
            # takes the same (non-reentrant) lock, so writing here would
            # deadlock exactly when stop lands before the first child spawns.
            self._state = "stopped"
            self._message = "Install stopped. Retry to start again."
            self._finished_at = datetime.now(UTC)
            self._record_event("stop", [], "stopped")
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.terminate()
        self._write_record()
        return True

    def shutdown(self, timeout: float = _SHUTDOWN_TIMEOUT_S) -> None:
        """Terminate the currently-running child (pip, probe or warm-up),
        join the watcher up to 30 s, and mark the record `interrupted`
        (§6.11) — not `stopped`, because the user did not stop it."""
        with self._guard:
            proc = self._proc
            thread = self._thread
            running = self._state == "running"
            if running:
                self._state = "interrupted"
                self._message = (
                    "The last install was interrupted by a restart. Retry to start again."
                )
                self._finished_at = datetime.now(UTC)
                self._record_event("interruption", [], "interrupted")
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.terminate()
        if thread is not None:
            thread.join(timeout)
        if running:
            self._write_record()

    # ---- watcher -------------------------------------------------------------------

    def _run_step(self, argv: list[str], *, timeout: float | None = None) -> tuple[int | None, str]:
        """Run one child step, streaming to the retained log. Returns
        (exit_code, log-tail text for classification). `child_pid` holds the
        currently-running child whatever the phase (§6.5/R6)."""
        assert self._log_path is not None
        env = self._subprocess_env()
        proc = self._spawn(argv, self._log_path, env)
        with self._guard:
            self._proc = proc
            try:
                self._child_pid = int(proc.pid)
            except (AttributeError, TypeError, ValueError):
                self._child_pid = None
        code: int | None = None
        try:
            if timeout is not None and isinstance(proc, subprocess.Popen):
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait()
                code = proc.returncode
            else:
                # Test doubles expose wait() only (browser-runner precedent):
                # its return value is the exit code.
                waited = proc.wait()
                if isinstance(proc, subprocess.Popen):
                    code = proc.returncode
                elif isinstance(waited, int):
                    code = waited
                else:
                    code = 0
        except Exception as exc:
            log.debug("cda install child wait failed: %s: %s", type(exc).__name__, exc)
            code = None
        with self._guard:
            self._proc = None
            self._child_pid = None
        tail = "\n".join(self._tail(self._log_path))
        return code, tail

    def _parse_probe(self, tail: str) -> ProbeOutcome | None:
        for line in reversed(tail.splitlines()):
            if _PROBE_MARKER in line:
                payload = line.split(_PROBE_MARKER, 1)[1].strip()
                try:
                    data = json.loads(payload)
                except ValueError:
                    return None
                if not isinstance(data, dict):
                    return None
                return ProbeOutcome(
                    laya_version=str(data.get("laya") or ""),
                    torch_version=str(data.get("torch") or ""),
                    torch_build=(
                        "cpu" if data.get("cuda") is None else "cuda" if data.get("cuda") else ""
                    ),
                    constructible=bool(data.get("constructible")),
                    warmed=False,
                    model_id=str(data.get("model") or ""),
                )
        return None

    def _watch(self, job: _Job) -> None:
        """Phased execution (§6.3): installing → verifying → warming up.
        Named phases only, never an invented percentage."""
        decision = job.decision
        interpreter = _venv_interpreter(decision)
        failure: tuple[str, str, bool, bool] | None = None  # class, msg, retry, guide
        exit_code: int | None = None
        try:
            if job.kind in ("install", "repair"):
                self._set_phase("installing")
                steps = build_pip_steps(decision, repair=job.kind == "repair")
                for argv in steps:
                    # A stop/shutdown between steps must reach the finish
                    # block below (cap, record, claim release) — never return
                    # early out of the watcher.
                    with self._guard:
                        if self._state != "running":
                            break
                    self._record_event("phase", argv, "installing")
                    code, tail = self._run_step(argv)
                    exit_code = code
                    with self._guard:
                        if self._state != "running":
                            break
                    if code != 0:
                        failure = classify_failure(tail, code, tmpdir=str(self._tmpdir()))
                        break
                with self._guard:
                    still_running = self._state == "running"
                if failure is None and still_running:
                    self._set_phase("verifying")
                    probe = self._run_probe(job.model_id, interpreter)
                    with self._guard:
                        still_running = self._state == "running"
                    if not still_running:
                        pass  # stopped/shut down mid-probe: keep that state
                    elif probe is None:
                        failure = (
                            "not-constructible",
                            "Installed but not loadable.",
                            True,
                            False,
                        )
                    else:
                        self._set_probe(probe)
                        self._set_phase("warming")
                        warmed = self._run_warmup(job.model_id, interpreter)
                        with self._guard:
                            still_running = self._state == "running"
                        if not still_running:
                            pass
                        elif not warmed:
                            failure = (
                                "checkpoint",
                                "The model checkpoint could not be downloaded.",
                                True,
                                False,
                            )
                        else:
                            warmed_probe = ProbeOutcome(
                                laya_version=probe.laya_version,
                                torch_version=probe.torch_version,
                                torch_build=probe.torch_build,
                                constructible=True,
                                warmed=True,
                                model_id=job.model_id,
                            )
                            self._set_probe(warmed_probe)
                            self._probe_by_model_set(job.model_id, warmed_probe)
            elif job.kind == "uninstall":
                self._set_phase("installing")
                code, tail = self._run_step(uninstall_argv(interpreter))
                exit_code = code
                with self._guard:
                    still_running = self._state == "running"
                if not still_running:
                    pass  # stopped/shut down mid-uninstall: keep that state
                elif code != 0:
                    failure = classify_failure(tail, code, tmpdir=str(self._tmpdir()))
                else:
                    with self._guard:
                        self._probe_by_model.pop(job.model_id, None)
                        if self._probe is not None and self._probe.model_id == job.model_id:
                            self._probe = ProbeOutcome(
                                laya_version=self._probe.laya_version,
                                torch_version=self._probe.torch_version,
                                torch_build=self._probe.torch_build,
                                constructible=False,
                                warmed=False,
                                model_id=job.model_id,
                            )
            elif job.kind == "verify":
                self._set_phase("verifying")
                probe = self._run_probe(job.model_id, interpreter)
                with self._guard:
                    still_running = self._state == "running"
                if not still_running:
                    pass  # stopped/shut down mid-probe: keep that state
                elif probe is None:
                    failure = (
                        "not-constructible",
                        "Installed but not loadable.",
                        True,
                        False,
                    )
                else:
                    self._set_probe(probe)
                    warmed_entry = self._probe_by_model_get(job.model_id)
                    if warmed_entry is not None and warmed_entry.warmed:
                        self._set_phase("warming")
                        if not self._run_warmup(job.model_id, interpreter):
                            with self._guard:
                                still_running = self._state == "running"
                            if still_running:
                                failure = (
                                    "checkpoint",
                                    "The model checkpoint could not be loaded.",
                                    True,
                                    False,
                                )
                        else:
                            warmed_probe = ProbeOutcome(
                                laya_version=probe.laya_version,
                                torch_version=probe.torch_version,
                                torch_build=probe.torch_build,
                                constructible=True,
                                warmed=True,
                                model_id=job.model_id,
                            )
                            self._set_probe(warmed_probe)
                            self._probe_by_model_set(job.model_id, warmed_probe)
                    else:
                        self._probe_by_model_set(job.model_id, probe)
        except Exception as exc:  # never leave the runner stuck in "running"
            failure = ("watcher", f"the install job failed: {exc}", True, False)
        with self._guard:
            if self._state == "running":
                if failure is None:
                    self._state = "done"
                    self._message = {
                        "install": "Backend installed and warmed.",
                        "repair": "Backend repaired and warmed.",
                        "uninstall": "Backend uninstalled.",
                        "verify": "Backend verified.",
                    }[job.kind]
                    self._record_event("finish", [], "done")
                else:
                    failure_class, message, _retryable, guide = failure
                    self._state = "failed"
                    self._failure_class = failure_class
                    self._message = message
                    self._guide = guide
                    self._record_event("failure", [], failure_class)
                self._finished_at = datetime.now(UTC)
                self._exit_code = exit_code
                self._phase = ""
            self._thread = None
            self._proc = None
            self._child_pid = None
        self._cap_and_redact_log()
        self._write_record()
        if self._on_finish is not None:
            with contextlib.suppress(Exception):
                self._on_finish()

    def _set_phase(self, phase: str) -> None:
        with self._guard:
            if self._state == "running":
                self._phase = phase
                self._record_event("phase", [], phase)
        self._write_record()

    def _set_probe(self, probe: ProbeOutcome) -> None:
        with self._guard:
            self._probe = probe
        self._write_record()

    def _probe_by_model_set(self, model_id: str, probe: ProbeOutcome) -> None:
        with self._guard:
            self._probe_by_model[model_id] = probe
        self._write_record()

    def _probe_by_model_get(self, model_id: str) -> ProbeOutcome | None:
        with self._guard:
            return self._probe_by_model.get(model_id)

    def _run_probe(self, model_id: str, interpreter: str) -> ProbeOutcome | None:
        code, tail = self._run_step(probe_argv(interpreter, model_id), timeout=_PROBE_TIMEOUT_S)
        if code != 0:
            if code is None:
                return None
            # A probe that times out is killed by _run_step; surface it plainly.
            with self._guard:
                if self._state == "running":
                    self._message = "Verification timed out."
            return None
        probe = self._parse_probe(tail)
        if probe is None or not probe.constructible:
            return None
        return ProbeOutcome(
            laya_version=probe.laya_version,
            torch_version=probe.torch_version,
            torch_build=probe.torch_build,
            constructible=True,
            warmed=False,
            model_id=model_id,
        )

    def _run_warmup(self, model_id: str, interpreter: str) -> bool:
        """Step 3 has no wall-clock timeout (§5.2/R6): its exits are user stop
        (§6.6) and dashboard shutdown (§6.11) only."""
        code, tail = self._run_step(warmup_argv(interpreter, model_id))
        if code != 0:
            return False
        return any(_WARM_MARKER in line for line in tail.splitlines())


def _free_memory_bytes() -> int | None:
    """Free memory, Linux-first (`/proc/meminfo`); None when unmeasurable.
    Only ever a preflight warning, never a refusal."""
    try:
        with Path("/proc/meminfo").open(encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


def _url_reachable(url: str) -> bool:
    """One 10-second reachability check from the dashboard process (§6.1)."""
    try:
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=_NETWORK_TIMEOUT_S):
            return True
    except Exception as exc:
        log.debug("cda install HEAD probe failed for %s: %s: %s", url, type(exc).__name__, exc)
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=_NETWORK_TIMEOUT_S) as response:
                response.read(1)
            return True
        except Exception as exc:
            log.debug("cda install GET probe failed for %s: %s: %s", url, type(exc).__name__, exc)
            return False
