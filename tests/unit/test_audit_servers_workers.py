"""Regression tests for the 2026-10-16 code audit (Worker F: servers, workers, quality,
dashboard, runlog, demo).

Every test asserts behavior (allowed/denied, bounded output, redacted prompts, stable
order), not implementation. Fixtures here are self-contained mirrors of the ones in
test_servers.py / test_quality.py (not imports: those files belong to other suites).
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jev_agent.config.schema import JevConfig
from jev_agent.config.servers_schema import AccessLevel
from jev_agent.core.commands import FakeCommandRunner
from jev_agent.core.models import WorkerResult
from jev_agent.dashboard.app import Dashboard
from jev_agent.dashboard.preview import FrameTooLarge, read_capped_body
from jev_agent.dashboard.runner import ChatRunner, RunLauncher
from jev_agent.dashboard.security import DashboardSettings
from jev_agent.demo import marker_path, read_demo_marker
from jev_agent.quality.base import BaseCheck
from jev_agent.quality.checks_production import TrivyCheck
from jev_agent.quality.gate import run_gate
from jev_agent.quality.levels import LEVEL_SUMMARY
from jev_agent.quality.models import CheckResult
from jev_agent.quality.review import ai_review
from jev_agent.quality.tools import QualitySettings, ToolRunner
from jev_agent.roles.schemas import QualityReview
from jev_agent.runlog.writer import log_path
from jev_agent.servers.audit import AuditEntry, AuditLog, audit_path
from jev_agent.servers.policy import PolicyContext, check_command, check_read_path
from jev_agent.servers.service import DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S, ServerService
from jev_agent.servers.ssh import SshResult, SshTransport
from jev_agent.workers.base import WorkerJob, WorkerOutput
from jev_agent.workers.fake import FakeStep, FakeWorker
from jev_agent.workers.openrouter_models import ModelCatalog
from jev_agent.workers.subprocess_runner import MAX_OUTPUT_BYTES, run_command

# ---------------------------------------------------------------------------
# servers: policy
# ---------------------------------------------------------------------------


def _medium() -> PolicyContext:
    return PolicyContext(access="medium", workdir="/srv/app", write_roots=("/srv/app",))


def _basic() -> PolicyContext:
    return PolicyContext(access="basic", workdir="/srv/app", write_roots=("/srv/app",))


def test_compose_project_name_is_denied() -> None:
    for command in ("docker compose -p prod down", "docker compose --project-name prod down"):
        decision = check_command(command, _medium())
        assert not decision.allowed
        assert "project-name" in decision.reason


def test_compose_file_must_stay_inside_write_roots() -> None:
    denied = check_command("docker compose -f /etc/other/docker-compose.yml up -d", _medium())
    assert not denied.allowed
    assert "outside allowed_paths" in denied.reason
    allowed = check_command("docker compose -f docker-compose.yml up -d", _medium())
    assert allowed.allowed
    assert check_command("docker compose up -d", _medium()).allowed


def test_git_config_read_is_denied() -> None:
    decision = check_command("cat /srv/app/.git/config", _basic())
    assert not decision.allowed
    assert "git config may hold credentials" in decision.reason
    decision = check_read_path("/srv/app/.git/config", _basic())
    assert not decision.allowed
    # A plain file named `config` outside `.git/` is still readable.
    assert check_command("cat /srv/app/config", _basic()).allowed


def test_pip_install_r_location_is_checked_like_editable() -> None:
    denied = check_command("pip install -r /etc/requirements.txt", _medium())
    assert not denied.allowed
    assert "outside allowed_paths" in denied.reason
    assert check_command("pip install -r requirements.txt", _medium()).allowed
    assert check_command("pip install -e ./pkg", _medium()).allowed


def test_double_dash_separator_is_not_an_option() -> None:
    assert check_command("rm -- /srv/app/sub/file", _medium()).allowed
    assert check_command("mkdir -- /srv/app/newdir", _medium()).allowed
    assert check_command("touch -- /srv/app/f", _medium()).allowed


def test_proc_cmdline_read_is_denied() -> None:
    decision = check_command("cat /proc/1/cmdline", _basic())
    assert not decision.allowed
    assert "command lines may hold secrets" in decision.reason
    assert check_command("cat /proc/self/cmdline", _basic()).allowed is False


def test_database_yml_is_a_credential_file() -> None:
    decision = check_command("cat /srv/app/database.yml", _basic())
    assert not decision.allowed
    assert "keys or passwords" in decision.reason


def test_pipe_into_sudo_grep_gets_the_filter_reason() -> None:
    decision = check_command("df | sudo grep x", _basic())
    assert not decision.allowed
    assert "a pipe may only feed grep, head, tail, wc or sort" in decision.reason
    # A real shell pipe keeps the shell-pipe reason.
    shell = check_command("df | sudo sh", _basic())
    assert not shell.allowed
    assert "piping into a shell" in shell.reason
    assert check_command("df | grep x", _basic()).allowed


# ---------------------------------------------------------------------------
# servers: service + ssh + audit
# ---------------------------------------------------------------------------


_UNRESTRICTED_TOML = """
[servers.web]
host = "web1"
user = "deploy"
access = "unrestricted"
unrestricted_acknowledged = true
workdir = "/srv/app"
allowed_paths = ["/srv/app"]

[servers.app]
host = "web1"
user = "deploy"
access = "medium"
workdir = "/srv/app"
allowed_paths = ["/srv/app"]
"""


class _ScriptedRunner:
    """Answers by substring match on the remote command; records every remote."""

    def __init__(self, script: list[tuple[str, SshResult]]) -> None:
        self.script = script
        self.remotes: list[str] = []

    def __call__(self, argv: Any, *, stdin: bytes | None, timeout_s: float) -> SshResult:
        remote = argv[-1]
        self.remotes.append(remote)
        for needle, result in self.script:
            if needle in remote:
                return result
        raise AssertionError(f"unexpected remote command: {remote!r}")


def _service(
    tmp_path: Path, runner: _ScriptedRunner, grants: dict[str, AccessLevel]
) -> ServerService:
    config = JevConfig.model_validate(tomllib.loads(_UNRESTRICTED_TOML))
    return ServerService(
        config, tmp_path / ".jev", tmp_path, grants, run_id="r1", role="coder", runner=runner
    )


def _realpath_ok(path: str) -> SshResult:
    return SshResult(0, (path + "\n").encode(), b"")


def test_unrestricted_write_skips_the_write_roots_recheck(tmp_path: Path) -> None:
    runner = _ScriptedRunner(
        [("realpath -m -- ", _realpath_ok("/etc/motd")), ("mv -f", SshResult(0, b"ok\n", b""))]
    )
    service = _service(tmp_path, runner, {"web": "unrestricted"})
    result = service.write_file("web", "/etc/motd", "hi")
    assert not result.is_error, result.text
    assert "wrote 2 bytes to /etc/motd" in result.text


def test_medium_write_outside_roots_still_denied(tmp_path: Path) -> None:
    runner = _ScriptedRunner([("realpath -m -- ", _realpath_ok("/etc/motd"))])
    service = _service(tmp_path, runner, {"app": "medium"})
    result = service.write_file("app", "/etc/motd", "hi")
    assert result.is_error
    assert "outside allowed_paths" in result.text


def test_realpath_falls_back_when_gnu_realpath_is_missing(tmp_path: Path) -> None:
    runner = _ScriptedRunner(
        [
            ("realpath -m -- ", SshResult(127, b"", b"bash: realpath: command not found\n")),
            ("python3 -c", SshResult(0, b"/srv/app/data.txt\n", b"")),
        ]
    )
    config = JevConfig.model_validate(tomllib.loads(_UNRESTRICTED_TOML))
    transport = SshTransport(config.servers["app"], sockets=tmp_path, runner=runner)
    result = transport.realpath("/srv/app/data.txt")
    assert result.ok
    assert result.stdout == b"/srv/app/data.txt\n"
    assert len(runner.remotes) == 2
    assert "realpath -m" in runner.remotes[0]
    assert "python3 -c" in runner.remotes[1]


def test_realpath_does_not_fall_back_on_a_genuine_failure(tmp_path: Path) -> None:
    failure = SshResult(1, b"", b"realpath: permission denied\n")
    runner = _ScriptedRunner([("realpath -m -- ", failure)])
    config = JevConfig.model_validate(tomllib.loads(_UNRESTRICTED_TOML))
    transport = SshTransport(config.servers["app"], sockets=tmp_path, runner=runner)
    result = transport.realpath("/srv/app/data.txt")
    assert not result.ok
    assert result.stderr == b"realpath: permission denied\n"
    assert len(runner.remotes) == 1  # no fallback: the failure is real, not a missing tool


def test_audit_read_skips_torn_lines(tmp_path: Path) -> None:
    log = AuditLog(audit_path(tmp_path / ".jev"))
    log.write(
        AuditEntry(
            run_id="r1",
            role=None,
            server="web",
            access="medium",
            action="run_command",
            command="uptime",
            allowed=True,
        )
    )
    with log.path.open("a", encoding="utf-8") as f:
        f.write('{"torn": "line without an end"\n')
    log.write(
        AuditEntry(
            run_id="r1",
            role=None,
            server="web",
            access="medium",
            action="run_command",
            command="df -h",
            allowed=True,
        )
    )
    entries = log.read()
    assert [e["command"] for e in entries] == ["uptime", "df -h"]


def test_audit_chmods_a_preexisting_audit_dir(tmp_path: Path) -> None:
    audit_dir = tmp_path / ".jev" / "audit"
    audit_dir.mkdir(parents=True)
    audit_dir.chmod(0o755)
    AuditLog(audit_path(tmp_path / ".jev")).write(
        AuditEntry(
            run_id="r1",
            role=None,
            server="web",
            access="medium",
            action="run_command",
            command="uptime",
            allowed=True,
        )
    )
    assert audit_dir.stat().st_mode & 0o777 == 0o700


def test_timeout_rejects_nan() -> None:
    assert ServerService._timeout(float("nan")) == DEFAULT_TIMEOUT_S
    assert ServerService._timeout(None) == DEFAULT_TIMEOUT_S
    assert ServerService._timeout(0) == DEFAULT_TIMEOUT_S
    assert ServerService._timeout(float("inf")) == MAX_TIMEOUT_S
    assert ServerService._timeout(30.0) == 30.0


# ---------------------------------------------------------------------------
# workers: subprocess runner, fake worker, model catalog cache
# ---------------------------------------------------------------------------


async def test_chatty_process_output_is_capped_with_a_marker(tmp_path: Path) -> None:
    code = "import sys; sys.stdout.write('x' * 5_000_000); sys.stderr.write('e' * 100)"
    result = await run_command([sys.executable, "-c", code], tmp_path, {}, 60.0)
    assert result.exit_code == 0
    assert len(result.stdout.encode("utf-8")) < MAX_OUTPUT_BYTES + 200
    assert "[output truncated: kept the first 1048576 of 5000000 bytes]" in result.stdout
    assert "e" * 100 in result.stderr  # small streams pass through untouched


async def test_missing_workdir_is_not_reported_as_missing_command(tmp_path: Path) -> None:
    result = await run_command(["true"], tmp_path / "no-such-dir", {}, 10.0)
    assert result.exit_code == 127
    assert "working directory not found" in result.stderr
    assert "no-such-dir" in result.stderr


async def test_missing_command_still_reported(tmp_path: Path) -> None:
    result = await run_command(["jev-no-such-binary-xyz"], tmp_path, {}, 10.0)
    assert result.exit_code == 127
    assert "command not found: jev-no-such-binary-xyz" in result.stderr


def _job(tmp_path: Path) -> WorkerJob:
    return WorkerJob(
        role="coder",
        prompt="write it",
        workdir=tmp_path / "work",
        log_path=tmp_path / "job.log",
        task_id="T1",
    )


async def test_fake_worker_rejects_absolute_paths(tmp_path: Path) -> None:
    worker = FakeWorker(script={"coder": FakeStep(files={"/etc/evil": "x"})})
    with pytest.raises(ValueError, match="escapes the workdir"):
        await worker.run(_job(tmp_path))
    assert not (tmp_path / "etc").exists()


async def test_fake_worker_rejects_dotdot_escapes(tmp_path: Path) -> None:
    worker = FakeWorker(script={"coder": FakeStep(files={"../evil.txt": "x"})})
    with pytest.raises(ValueError, match="escapes the workdir"):
        await worker.run(_job(tmp_path))


async def test_fake_worker_still_writes_relative_paths(tmp_path: Path) -> None:
    worker = FakeWorker(script={"coder": FakeStep(files={"sub/ok.txt": "fine"})})
    await worker.run(_job(tmp_path))
    assert (tmp_path / "work" / "sub" / "ok.txt").read_text() == "fine"


def _write_cache(tmp_path: Path, models: list[Any]) -> Path:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps({"fetched_at": 1_700_000_000, "source": "test", "data": models}),
        encoding="utf-8",
    )
    return path


def test_cache_entry_with_wrong_types_gets_parse_model_validation(tmp_path: Path) -> None:
    path = _write_cache(
        tmp_path,
        [
            {
                "id": "good/model",
                "name": "Good",
                "context_length": "not-an-int",  # corrupt: str instead of int
                "prompt_price": "0",
                "completion_price": "0",
                "supported_parameters": ["structured_outputs", 42],
            }
        ],
    )
    catalog = ModelCatalog.load(path)
    assert catalog is not None
    model = catalog.get("good/model")
    assert model is not None
    assert model.context_length is None  # `parse_model` drops the bad value
    assert model.supported_parameters == ("structured_outputs",)
    assert model.free  # prices still parsed


def test_cache_entry_with_unusable_id_is_rejected(tmp_path: Path) -> None:
    path = _write_cache(tmp_path, [{"id": 12345, "name": "Bad"}])
    assert ModelCatalog.load(path) is None


# ---------------------------------------------------------------------------
# quality: trivy guard, review redaction, levels wording, pip-audit target,
# gate tail guard, parallel checks
# ---------------------------------------------------------------------------


def _tool_runner(
    tmp_path: Path,
    outputs: dict[str, list[str]] | None = None,
    workdir: Path | None = None,
    scope: str = "task",
) -> tuple[ToolRunner, FakeCommandRunner]:
    commands = FakeCommandRunner(None, outputs)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    settings = QualitySettings(level="production")
    runner = ToolRunner(
        commands=commands,
        repo=repo,
        workdir=workdir or repo,
        log_dir=tmp_path / "logs",
        log_prefix="1.quality",
        settings=settings,
        scope=scope,
        diff="",
        passed_commands=(),
        which=lambda tool: f"/bin/{tool}",
    )
    return runner, commands


async def test_trivy_tolerates_a_non_dict_cause_metadata(tmp_path: Path) -> None:
    body = json.dumps(
        {
            "Results": [
                {
                    "Target": "Dockerfile",
                    "Misconfigurations": [
                        {
                            "ID": "DS002",
                            "Title": "bad",
                            "Severity": "HIGH",
                            "CauseMetadata": "not-a-dict",
                        }
                    ],
                }
            ]
        }
    )
    runner, _ = _tool_runner(
        tmp_path, {"trivy fs --scanners misconfig,secret --format json --quiet .": [body]}
    )
    result = await TrivyCheck().run(runner.workdir, runner)
    assert result.status == "fail"
    assert len(result.findings) == 1
    assert result.findings[0].line_start is None  # no line info, but no crash either


class _ReviewHarness:
    """Captures the prompt `ai_review` builds; answers with a valid review."""

    def __init__(self) -> None:
        self.prompt_kwargs: dict[str, Any] = {}

    def options(self, role: str) -> SimpleNamespace:
        return SimpleNamespace(workers=["fake"], models=["m"], efforts=["low", "high"])

    def assignment(self, role: str, spec: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(worker=spec["worker"], model=spec["model"])

    def check_budget(self, run_id: str, task_id: str | None) -> None:
        pass

    @property
    def store(self) -> SimpleNamespace:
        run = SimpleNamespace(idea=SimpleNamespace(text="idea"), plan=None)
        return SimpleNamespace(get_run=lambda rid: run)

    def prompt(self, role: str, *args: Any, **kwargs: Any) -> str:
        self.prompt_kwargs = kwargs
        return "prompt"

    async def run_worker_with_fallback(self, **kwargs: Any) -> WorkerOutput:
        return WorkerOutput(
            result=WorkerResult(status="success", summary="ok", raw_log_path=Path("/tmp/x.log")),
            parsed=QualityReview(summary="looks fine"),
        )


async def test_review_prompt_redacts_the_diff() -> None:
    secret = "ghp_" + "a" * 36
    diff = f'diff --git a/x b/x\n+token = "{secret}"\n'
    harness = _ReviewHarness()
    outcome = await ai_review(
        cast(Any, harness),
        run_id="r1",
        task=None,
        level="basic",
        diff=diff,
        checks=[],
        workdir=Path("/tmp"),
        attempt_no=1,
        log_name="review",
    )
    assert outcome.status == "done"
    seen = harness.prompt_kwargs["diff"]
    assert secret not in seen
    assert "[REDACTED]" in seen


def test_medium_summary_says_coverage_blocks_from_production() -> None:
    assert "blocking from production" in LEVEL_SUMMARY["medium"]


async def test_pip_audit_prefers_requirements_txt(tmp_path: Path) -> None:
    from jev_agent.quality.checks_medium import PipAuditCheck

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (repo / "requirements.txt").write_text("requests==2.0\n")
    runner, commands = _tool_runner(
        tmp_path,
        {"pip-audit -r requirements.txt -f json --progress-spinner off": ['{"dependencies": []}']},
        repo,
        scope="integration",
    )
    result = await PipAuditCheck().run(repo, runner)
    assert result.status == "pass"
    assert "-r requirements.txt" in commands.calls[-1][0]


async def test_pip_audit_documents_the_ambient_environment_target(tmp_path: Path) -> None:
    from jev_agent.quality.checks_medium import PipAuditCheck

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (repo / "uv.lock").write_text("# lock\n")
    runner, commands = _tool_runner(
        tmp_path,
        {"pip-audit . -f json --progress-spinner off": ['{"dependencies": []}']},
        repo,
        scope="integration",
    )
    result = await PipAuditCheck().run(repo, runner)
    assert result.status == "pass"
    assert commands.calls[-1][0].split()[1] == "."  # the ambient environment, not uv.lock
    assert "installed environment" in result.detail
    assert "lockfile" in result.detail


class _FakeStore:
    def __init__(self) -> None:
        self.reports: list[SimpleNamespace] = []

    def add_quality_report(self, run_id: str, result: Any, path: Path) -> None:
        self.reports.append(SimpleNamespace(run_id=run_id, result=result, path=path))

    def list_quality_reports(self, run_id: str) -> list[SimpleNamespace]:
        return [r for r in self.reports if r.run_id == run_id]

    def get_run(self, run_id: str) -> SimpleNamespace:
        return SimpleNamespace(quality_level="basic")


class _GateHarness:
    def __init__(self, tmp_path: Path) -> None:
        self.config = JevConfig()
        self.commands = FakeCommandRunner()
        self.repo = tmp_path
        self.state_dir = tmp_path / ".jev"
        self._store = _FakeStore()

    @property
    def store(self) -> _FakeStore:
        return self._store


async def _run_gate(tmp_path: Path, checks: list[Any]) -> Any:
    harness = _GateHarness(tmp_path)
    return await run_gate(
        cast(Any, harness),
        run_id="r1",
        level="basic",
        scope="task",
        task=None,
        number=1,
        workdir=tmp_path,
        diff="",
        checks=checks,
    )


async def test_gate_tail_failure_records_a_blocked_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jev_agent.quality.gate as gate_mod

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("weights exploded")

    monkeypatch.setattr(gate_mod, "score_gate", boom)
    harness = _GateHarness(tmp_path)
    result = await run_gate(
        cast(Any, harness),
        run_id="r1",
        level="basic",
        scope="task",
        task=None,
        number=1,
        workdir=tmp_path,
        diff="",
        checks=[],
    )
    assert result.blocked
    assert any("gate error" in reason for reason in result.reasons)
    assert result.score is None
    # The blocked record itself is persisted, not just returned in memory (issue #204).
    assert [r.result.blocked for r in harness.store.reports] == [True]


async def test_gate_persist_failure_still_returns_a_blocked_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that cannot persist must not turn the gate into a crash either."""

    class _BrokenStore(_FakeStore):
        def add_quality_report(self, run_id: str, result: Any, path: Path) -> None:
            raise OSError("disk full")

    harness = _GateHarness(tmp_path)
    harness._store = _BrokenStore()
    result = await run_gate(
        cast(Any, harness),
        run_id="r1",
        level="basic",
        scope="task",
        task=None,
        number=1,
        workdir=tmp_path,
        diff="",
        checks=[],
    )
    assert result.blocked
    assert any("gate error" in reason for reason in result.reasons)


class _OrderCheck(BaseCheck):
    def __init__(self, name: str, delay: float) -> None:
        self.name = name
        self.levels = ("basic",)
        self._delay = delay

    def applies(self, repo: Path) -> bool:
        return True

    async def run(self, workdir: Path, runner: ToolRunner) -> CheckResult:
        await asyncio.sleep(self._delay)
        return CheckResult(status="pass", detail=self.name)


async def test_gate_runs_checks_concurrently_in_registry_order(tmp_path: Path) -> None:
    checks = [_OrderCheck("c1", 0.3), _OrderCheck("c2", 0.2), _OrderCheck("c3", 0.1)]
    started = time.monotonic()
    result = await _run_gate(tmp_path, checks)
    elapsed = time.monotonic() - started
    assert [c.name for c in result.checks] == ["c1", "c2", "c3"]
    assert elapsed < 0.55  # sequential would take 0.6s


# ---------------------------------------------------------------------------
# dashboard: stale jobs, frame size cap, flash store eviction
# ---------------------------------------------------------------------------


def _boom_start(self: threading.Thread, *args: Any, **kwargs: Any) -> None:
    raise RuntimeError("cannot start threads here")


def test_run_start_failure_leaves_no_stale_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(threading.Thread, "start", _boom_start)
    launcher = RunLauncher(tmp_path)
    with pytest.raises(RuntimeError, match="cannot start threads"):
        launcher.start("r1", "start")
    assert launcher.job("r1") is None
    assert launcher._threads == {} and launcher._cancels == {}
    assert not launcher.busy("r1")


def test_chat_start_failure_leaves_no_stale_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(threading.Thread, "start", _boom_start)
    chats = ChatRunner()
    with pytest.raises(RuntimeError, match="cannot start threads"):
        chats.start("c1", lambda: "done")
    assert chats.job("c1") is None
    assert chats._threads == {} and chats._cancels == {}


class _StubRequest:
    def __init__(self, headers: dict[str, str], chunks: list[bytes]) -> None:
        self.headers = headers
        self._chunks = chunks
        self.consumed = 0

    async def stream(self) -> Any:
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


async def test_frame_body_refused_by_content_length_first() -> None:
    req = _StubRequest({"content-length": str(3_000_000)}, [b"x" * 10])
    with pytest.raises(FrameTooLarge):
        await read_capped_body(cast(Any, req))
    assert req.consumed == 0  # nothing was buffered


async def test_frame_body_bails_out_while_streaming() -> None:
    req = _StubRequest({}, [b"y" * 1_500_000, b"y" * 1_000_000])
    with pytest.raises(FrameTooLarge):
        await read_capped_body(cast(Any, req))
    assert req.consumed == 2  # bailed on the second chunk instead of buffering it all


async def test_frame_body_returns_small_bodies() -> None:
    req = _StubRequest({"content-length": "5"}, [b"hel", b"lo"])
    assert await read_capped_body(cast(Any, req)) == b"hello"


class _FlashRequest:
    def __init__(self, session: str) -> None:
        self.state = SimpleNamespace(session=session)


def _dashboard(tmp_path: Path) -> Dashboard:
    return Dashboard(DashboardSettings(root=tmp_path), RunLauncher(tmp_path))


def test_flash_store_caps_entries_per_session(tmp_path: Path) -> None:
    dash = _dashboard(tmp_path)
    req = _FlashRequest("s1")
    for i in range(35):
        dash.flash(cast(Any, req), "info", f"m{i}")
    got = dash.take_flashes(cast(Any, req))
    assert len(got) == 30
    assert got[0] == ("info", "m5")  # the newest entries are kept
    assert got[-1] == ("info", "m34")
    assert dash.take_flashes(cast(Any, req)) == []  # shown once


def test_flash_store_prunes_sessions_that_never_render(tmp_path: Path) -> None:
    dash = _dashboard(tmp_path)
    dash.flash(cast(Any, _FlashRequest("old")), "info", "x")
    dash._flash_seen["old"] = time.monotonic() - 7 * 3600  # older than the TTL
    dash.flash(cast(Any, _FlashRequest("new")), "info", "y")
    assert "old" not in dash._flashes
    assert dash.take_flashes(cast(Any, _FlashRequest("new"))) == [("info", "y")]


# ---------------------------------------------------------------------------
# runlog + demo
# ---------------------------------------------------------------------------


def test_log_path_rejects_unsafe_segments(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe log path segment"):
        log_path(tmp_path, "../x", None, "a")
    with pytest.raises(ValueError, match="unsafe log path segment"):
        log_path(tmp_path, "r1", "a/b", "c")
    with pytest.raises(ValueError, match="unsafe log path segment"):
        log_path(tmp_path, "", None, "a")
    with pytest.raises(ValueError, match="unsafe log path segment"):
        log_path(tmp_path, "r1", "..", "a")
    assert log_path(tmp_path, "r1", None, "x") == tmp_path / "runs" / "r1" / "run" / "x.log"
    assert log_path(tmp_path, "r1", "T1", 2) == tmp_path / "runs" / "r1" / "T1" / "2.log"


def test_read_demo_marker_tolerates_a_corrupt_marker(tmp_path: Path) -> None:
    marker_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    marker_path(tmp_path).write_text('{"demo": true', encoding="utf-8")  # truncated
    assert read_demo_marker(tmp_path) is None
    assert read_demo_marker(tmp_path / "nope") is None
    marker_path(tmp_path).write_text('{"demo": true}', encoding="utf-8")
    assert read_demo_marker(tmp_path) == {"demo": True}


def test_seed_returns_its_summary_message(tmp_path: Path) -> None:
    from jev_agent.config import DEFAULT_CONFIG_TOML
    from jev_agent.demo.seeder import seed

    # `jev init`'s template carries a `[trust]` section that only belongs in the user
    # config; drop it for this repo-local seed.
    (tmp_path / "jev.toml").write_text(DEFAULT_CONFIG_TOML.split("[trust]")[0], encoding="utf-8")
    (tmp_path / ".jev").mkdir()
    message = seed(tmp_path, demo_config=False)
    assert message.startswith("Seeded demo state in ")
    assert str(tmp_path / ".jev") in message
