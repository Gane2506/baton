import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli import wiring
from jev_agent.cli.app import app
from jev_agent.cli.status_cmds import format_answers, format_time
from jev_agent.cli.wiring import (
    FAKE_WORKERS_ENV,
    build_cli_workers,
    build_dispatcher,
    build_registry,
    pipeline_settings,
)
from jev_agent.config import JevConfig
from jev_agent.core.models import RunState
from jev_agent.core.store import Store
from jev_agent.dispatch.jev import JevDispatcher
from jev_agent.dispatch.rules import RulesDispatcher
from jev_agent.workers.api import ApiWorker
from jev_agent.workers.claude_code import ClaudeCodeWorker
from jev_agent.workers.codex import CodexWorker
from jev_agent.workers.fake import FakeWorker

runner = CliRunner()

NO_WORKERS_TOML = "".join(
    f"[workers.{name}]\nenabled = false\n" for name in ("claude_code", "codex", "api")
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(FAKE_WORKERS_ENV, raising=False)
    # No network in tests: without a key the Jev dispatcher falls back to rules locally.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def _jev(repo: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--repo", str(repo)])
    # Rich wraps long lines to the terminal width (narrow in CI); compare unwrapped text.
    return result.exit_code, " ".join(result.output.split())


def _run_id(repo: Path) -> str:
    with Store.open(repo / ".jev" / "state.db") as store:
        return store.list_runs()[0].id


def test_run_without_workers_explains(fixture_repo: Path) -> None:
    (fixture_repo / "jev.toml").write_text(NO_WORKERS_TOML)
    code, output = _jev(fixture_repo, "run", "add search")
    assert code == 1
    assert "No workers are available" in output and FAKE_WORKERS_ENV in output
    code, output = _jev(fixture_repo, "status")
    assert code == 0 and "No runs yet" in output


def test_fake_workers_full_flow(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    code, output = _jev(fixture_repo, "run", "add search")
    assert code == 0, output
    assert "AWAITING_APPROVAL" in output
    assert output.count("OPENROUTER_API_KEY is not set") == 1
    run_id = _run_id(fixture_repo)

    code, output = _jev(fixture_repo, "status")
    assert code == 0 and run_id in output
    code, output = _jev(fixture_repo, "status", run_id)
    assert code == 0 and "Fake task" in output and "PENDING" in output

    code, output = _jev(fixture_repo, "resume", run_id)
    assert code == 1 and "approve" in output

    code, output = _jev(fixture_repo, "approve", run_id)
    assert code == 0, output
    assert "DONE" in output and f"jev/{run_id}/result" in output
    assert output.count(f"jev/{run_id}/result") == 1  # the outcome reason is printed once

    code, output = _jev(fixture_repo, "log", run_id)
    assert code == 0, output
    for expected in ("decision D1 [run] by rules", "decision D6", "attempt coder [T1] #1"):
        assert expected in output
    assert "FALLBACK: jev: raised DispatchError: jev: API key env var" in output
    code, output = _jev(fixture_repo, "cancel", run_id)
    assert code == 1 and "already DONE" in output


def test_approve_without_workers_marks_approved(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    assert _jev(fixture_repo, "run", "--plan-only", "add search")[0] == 0
    run_id = _run_id(fixture_repo)
    monkeypatch.delenv(FAKE_WORKERS_ENV)
    (fixture_repo / "jev.toml").write_text(NO_WORKERS_TOML)
    code, output = _jev(fixture_repo, "approve", run_id)
    assert code == 0 and "resume" in output
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        assert store.get_run(run_id).state is RunState.EXECUTING


def test_cancel_and_unknown_run(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    assert _jev(fixture_repo, "run", "add search")[0] == 0
    run_id = _run_id(fixture_repo)
    code, output = _jev(fixture_repo, "cancel", run_id)
    assert code == 0 and "CANCELLED" in output
    for command in ("status", "log", "approve", "resume", "cancel"):
        code, output = _jev(fixture_repo, command, "nope")
        assert code == 1 and "not found" in output, command


def test_not_a_git_repo(tmp_path: Path) -> None:
    code, output = _jev(tmp_path, "status")
    assert code == 1 and "not inside a git repository" in output


def test_build_dispatcher_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    rules, warnings = build_dispatcher(
        JevConfig.model_validate({"dispatcher": {"primary": "rules"}})
    )
    assert isinstance(rules, RulesDispatcher) and warnings == []

    config = JevConfig.model_validate({"dispatcher": {"jev": {"min_confidence": 0.8}}})
    jev, warnings = build_dispatcher(config)
    assert isinstance(jev, JevDispatcher)
    assert jev.settings.min_confidence == 0.8
    assert len(warnings) == 1 and "OPENROUTER_API_KEY is not set" in warnings[0]

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secret-value")
    jev, warnings = build_dispatcher(config)
    assert isinstance(jev, JevDispatcher) and warnings == []


def test_build_registry_registers_api_worker_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wiring, "which", lambda cmd: f"/fake/bin/{cmd}")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-fake")
    config = JevConfig.model_validate(
        {"workers": {"api": {"base_url": "https://example.test/v1/", "default_model": "m/x"}}}
    )
    registry, warnings = build_registry(config)
    assert warnings == []
    assert sorted(registry.names()) == ["api", "claude_code", "codex"]
    api = registry.get("api")
    assert isinstance(api, ApiWorker)
    assert (api.base_url, api.api_key_env, api.default_model) == (
        "https://example.test/v1",
        "OPENROUTER_API_KEY",
        "m/x",
    )
    disabled = JevConfig.model_validate({"workers": {"api": {"enabled": False}}})
    assert "api" not in build_registry(disabled)[0].names()


def test_build_cli_workers_from_config() -> None:
    config = JevConfig.model_validate(
        {
            "budget": {"per_task_usd": 1.25},
            "workers": {
                "claude_code": {
                    "command": "/opt/claude",
                    "permission_mode": "default",
                    "allowed_tools": ["Read", "Bash(pytest *)"],
                    "bare": True,
                    "extra_args": ["--max-turns", "40"],
                },
                "codex": {"sandbox": "read-only", "extra_args": ["--foo"]},
            },
        }
    )
    workers = build_cli_workers(config)
    claude, codex = workers["claude_code"], workers["codex"]
    assert isinstance(claude, ClaudeCodeWorker) and isinstance(codex, CodexWorker)
    assert claude.command == "/opt/claude"
    assert claude.permission_mode == "default"
    assert claude.read_only_permission_mode == "dontAsk"
    assert claude.allowed_tools == ("Read", "Bash(pytest *)")
    assert claude.max_budget_usd == 1.25  # follows budget.per_task_usd when unset
    assert claude.bare is True and claude.skip_permissions is False
    assert claude.extra_args == ("--max-turns", "40")
    assert (codex.command, codex.sandbox, codex.extra_args) == ("codex", "read-only", ("--foo",))

    config = JevConfig.model_validate(
        {"workers": {"claude_code": {"max_budget_usd": 0.5}, "codex": {"enabled": False}}}
    )
    workers = build_cli_workers(config)
    assert list(workers) == ["claude_code"]
    claude = workers["claude_code"]
    assert isinstance(claude, ClaudeCodeWorker) and claude.max_budget_usd == 0.5


def test_fake_workers_replace_real_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    registry, _ = build_registry(JevConfig())
    assert sorted(registry.names()) == ["api", "claude_code", "codex"]
    assert all(isinstance(registry.get(n), FakeWorker) for n in registry.names())


def test_log_shows_jev_confidence() -> None:
    details = {"answers": {"effort": {"choice": "high", "confidence": 0.912}}, "direct": ["w"]}
    assert format_answers({"w": "codex", "effort": "high"}, details) == "w=codex effort=high (0.91)"
    assert format_answers({"w": "codex"}, {}) == "w=codex"


def test_pipeline_settings_from_config() -> None:
    config = JevConfig.model_validate(
        {
            "budget": {"test_timeout_s": 90},
            "integration": {"test_command": "uv run pytest -q", "max_fix_rounds": 0},
        }
    )
    settings = pipeline_settings(config)
    assert (
        settings.test_timeout_s,
        settings.integration_test_command,
        settings.max_fix_rounds,
    ) == (
        90,
        "uv run pytest -q",
        0,
    )
    default = pipeline_settings(JevConfig())
    assert (default.test_timeout_s, default.integration_test_command, default.max_fix_rounds) == (
        1200,
        None,
        2,
    )


def test_format_time_is_utc_with_marker() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    assert format_time(datetime(2026, 10, 12, 21, 0, 12, tzinfo=ist)) == "2026-10-12 15:30:12Z"
    assert format_time(datetime(2026, 10, 12, 15, 30, 12)) == "2026-10-12 15:30:12Z"  # naive: UTC


def test_status_and_log_show_utc_whatever_the_local_zone(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    monkeypatch.setenv("TZ", "Asia/Kolkata")  # UTC+5:30, so local and UTC never agree
    time.tzset()
    try:
        assert _jev(fixture_repo, "run", "--plan-only", "add search")[0] == 0
        with Store.open(fixture_repo / ".jev" / "state.db") as store:
            run = store.list_runs()[0]
            first = store.list_decisions(run.id)[0].created_at
        utc_updated = run.updated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
        code, output = _jev(fixture_repo, "status")
        assert code == 0 and utc_updated in output
        code, output = _jev(fixture_repo, "log", run.id)
        assert code == 0 and first.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ") in output
    finally:
        monkeypatch.undo()
        time.tzset()
