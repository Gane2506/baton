"""`jev mcp runs`: tool listing, read tools, the mutation gate, bad input, audit, no secrets."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from jev_agent.app.context import FAKE_WORKERS_ENV, RepoContext
from jev_agent.cli.app import app
from jev_agent.config import load_config
from jev_agent.config.schema import DEFAULT_LAYA_WEIGHTS, LAYA_CRITERIA
from jev_agent.core.chat import ChatLearning, ChatSession, EchoChatBackend
from jev_agent.core.learning import Learning
from jev_agent.core.models import Idea, RunState
from jev_agent.core.store import Store
from jev_agent.mcp_runs import runs_mcp
from jev_agent.quality.models import GateResult, ScoreBreakdown
from jev_agent.servers.audit import AuditLog
from jev_agent.servers.mcp import McpServer

runner = CliRunner()

CONFIG = '[dispatcher]\nprimary = "rules"\n[approval]\nrequire_plan_approval = false\n'


@pytest.fixture(autouse=True)
def _fake_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


@pytest.fixture
def repo(fixture_repo: Path) -> Path:
    (fixture_repo / "jev.toml").write_text(CONFIG)
    return fixture_repo


@contextmanager
def _repo_ctx(repo: Path, toml: str = CONFIG) -> Iterator[RepoContext]:
    (repo / "jev.toml").write_text(toml)
    config = load_config(repo)
    with Store.open(repo / ".jev" / "state.db") as store:
        learning = Learning.for_store(store)
        try:
            yield RepoContext(root=repo, config=config, store=store, learning=learning)
        finally:
            learning.close()


def _trust(repo: Path) -> None:
    """Trust a fixture repo in the isolated test user config.

    `autonomy.mode` is a guarded key: an untrusted repo's jev.toml may not set it
    (trust gate, audit 2026-10-16), so the fixture repo is opted in, mirroring a user
    who trusts their own checkout. Idempotent within a test.
    """
    user_config = Path(os.environ["XDG_CONFIG_HOME"]) / "jev" / "config.toml"
    if "[trust]" not in user_config.read_text():
        with user_config.open("a") as fh:
            fh.write(f'\n[trust]\nrepos = ["{repo}"]\n')


@pytest.fixture
def ctx(repo: Path) -> Iterator[RepoContext]:
    with _repo_ctx(repo) as context:
        yield context


def _rpc(server: McpServer, *messages: dict[str, Any]) -> list[dict[str, Any]]:
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    stdout = io.StringIO()
    server.serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def _call(server: McpServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    (reply,) = _rpc(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    return cast("dict[str, Any]", reply["result"])


def _text(result: dict[str, Any]) -> str:
    assert result["isError"] is False, result["content"][0]["text"]
    return cast(str, result["content"][0]["text"])


def _make_run(ctx: RepoContext, idea: str = "test idea") -> str:
    return ctx.store.create_run(Idea(text=idea, repo_path=ctx.root), None, "none").id


def _score(total: int) -> ScoreBreakdown:
    return ScoreBreakdown(
        criteria={name: 4 for name in LAYA_CRITERIA},
        weights=dict(DEFAULT_LAYA_WEIGHTS),
        total=total,
        threshold=85,
        passed=total >= 85,
        details={name: "test" for name in LAYA_CRITERIA},
    )


def _audit(repo: Path) -> list[dict[str, Any]]:
    return AuditLog(repo / ".jev" / "audit" / "mcp_runs.log").read()


def ctx_server(ctx: RepoContext, **kwargs: Any) -> McpServer:
    return runs_mcp(ctx, "1", **kwargs)


# ---- tool listing -----------------------------------------------------------------------


def test_handshake_names_server(ctx: RepoContext) -> None:
    (reply,) = _rpc(
        ctx_server(ctx),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
        },
    )
    assert reply["result"]["serverInfo"]["name"] == "jev-runs"


def test_tools_list_read_only_by_default(ctx: RepoContext) -> None:
    (reply,) = _rpc(ctx_server(ctx), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = reply["result"]["tools"]
    assert [t["name"] for t in tools] == ["run_list", "run_status", "quality_show"]
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools)


def test_tools_list_with_mutations(ctx: RepoContext) -> None:
    (reply,) = _rpc(
        ctx_server(ctx, allow_mutations=True),
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    tools = reply["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "run_list",
        "run_status",
        "quality_show",
        "run_start",
        "chat_send",
    ]
    by_name = {t["name"]: t for t in tools}
    assert by_name["run_start"]["annotations"]["readOnlyHint"] is False
    assert by_name["chat_send"]["annotations"]["readOnlyHint"] is False


# ---- read tools -------------------------------------------------------------------------


def test_run_list_newest_first_and_limit(ctx: RepoContext) -> None:
    _make_run(ctx, "first idea")
    _make_run(ctx, "second idea")
    server = ctx_server(ctx)
    listed = json.loads(_text(_call(server, "run_list", {})))
    assert [r["idea"] for r in listed] == ["second idea", "first idea"]
    assert listed[0]["state"] == "INTAKE"
    assert set(listed[0]) == {"id", "state", "idea", "created", "spend_usd"}
    limited = json.loads(_text(_call(server, "run_list", {"limit": 1})))
    assert len(limited) == 1 and limited[0]["idea"] == "second idea"


def test_run_list_empty(ctx: RepoContext) -> None:
    assert json.loads(_text(_call(ctx_server(ctx), "run_list", {}))) == []


def test_run_status_reports_state_phase_and_score(ctx: RepoContext) -> None:
    run_id = _make_run(ctx, "add dark mode")
    ctx.store.set_run_state(run_id, RunState.AWAITING_APPROVAL)
    ctx.store.add_quality_report(
        run_id, GateResult(level="basic", scope="integration", score=_score(88))
    )
    text = _text(_call(ctx_server(ctx), "run_status", {"run_id": run_id}))
    assert "state: AWAITING_APPROVAL" in text
    assert "waiting for a person to approve the plan" in text
    assert f"jev approve {run_id}" in text
    assert "quality: ship score 88/100 (threshold 85): passed the ship gate" in text


def test_run_status_unknown_run_is_error(ctx: RepoContext) -> None:
    result = _call(ctx_server(ctx), "run_status", {"run_id": "no-such-run"})
    assert result["isError"] is True


def test_quality_show_without_gates(ctx: RepoContext) -> None:
    run_id = _make_run(ctx)
    result = _call(ctx_server(ctx), "quality_show", {"run_id": run_id})
    assert result["isError"] is False
    assert "No quality gate has run" in result["content"][0]["text"]


def test_quality_show_with_scored_gate(ctx: RepoContext) -> None:
    run_id = _make_run(ctx)
    ctx.store.add_quality_report(
        run_id, GateResult(level="basic", scope="integration", score=_score(72))
    )
    text = _text(_call(ctx_server(ctx), "quality_show", {"run_id": run_id}))
    assert "## Score" in text
    assert "**72/100** (threshold 85)" in text


# ---- mutating tools ---------------------------------------------------------------------


def test_run_start_creates_without_advancing(ctx: RepoContext) -> None:
    server = ctx_server(ctx, allow_mutations=True)
    text = _text(_call(server, "run_start", {"idea": "build a widget"}))
    run_id = text.splitlines()[0].split()[1]
    run = ctx.store.get_run(run_id)
    assert run.state is RunState.INTAKE  # created, never advanced, nothing approved
    assert run.autonomy_mode == "standard"
    assert f"jev resume {run_id}" in text


def test_run_start_records_autonomy_mode(repo: Path) -> None:
    _trust(repo)  # `autonomy.mode` is a guarded key (trust gate, audit 2026-10-16)
    with _repo_ctx(repo, CONFIG + '[autonomy]\nmode = "ask_every_step"\n') as ctx:
        server = ctx_server(ctx, allow_mutations=True)
        text = _text(_call(server, "run_start", {"idea": "x"}))
        run_id = text.splitlines()[0].split()[1]
        assert ctx.store.get_run(run_id).autonomy_mode == "ask_every_step"
        assert "autonomy: ask_every_step" in text
        assert "each step's gate" in text


def test_run_start_hidden_without_mutations(ctx: RepoContext) -> None:
    (reply,) = _rpc(
        ctx_server(ctx),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "run_start", "arguments": {"idea": "x"}},
        },
    )
    assert reply["error"]["code"] == -32602  # unknown tool


def test_chat_send_hidden_without_mutations(ctx: RepoContext) -> None:
    (reply,) = _rpc(
        ctx_server(ctx),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "chat_send", "arguments": {"chat_id": "c-x", "message": "hi"}},
        },
    )
    assert reply["error"]["code"] == -32602  # unknown tool


def test_chat_send_unknown_chat_is_error(ctx: RepoContext) -> None:
    server = ctx_server(ctx, allow_mutations=True)
    result = _call(server, "chat_send", {"chat_id": "c-20260101-000000-0000", "message": "hi"})
    assert result["isError"] is True
    assert "no chat" in result["content"][0]["text"]


def test_chat_send_returns_reply(ctx: RepoContext) -> None:
    backend = EchoChatBackend("api", "m")
    session = ChatSession.create(ctx.root, backend, "S", ChatLearning(), None, 1.0)
    server = ctx_server(ctx, allow_mutations=True)
    text = _text(_call(server, "chat_send", {"chat_id": session.meta.chat_id, "message": "hello"}))
    assert "You said: hello" in text


# ---- bad input --------------------------------------------------------------------------


def test_bad_input_keeps_serving(ctx: RepoContext) -> None:
    server = ctx_server(ctx)
    replies = _rpc(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "nope"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "run_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "run_list", "arguments": {"limit": "many"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "run_list", "arguments": {"limit": 0}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "nope", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "ping"},
    )  # fmt: skip
    assert replies[0]["error"]["code"] == -32601
    assert replies[1]["result"]["isError"] is True
    assert "missing argument(s): run_id" in replies[1]["result"]["content"][0]["text"]
    assert "bad arguments: limit must be an integer" in replies[2]["result"]["content"][0]["text"]
    assert "bad arguments: limit must be at least 1" in replies[3]["result"]["content"][0]["text"]
    assert replies[4]["error"]["code"] == -32602
    assert replies[5]["result"] == {}


def test_bad_json(ctx: RepoContext) -> None:
    assert ctx_server(ctx).handle_line("{nope}")["error"]["code"] == -32700  # type: ignore[index]


# ---- audit and secrets ------------------------------------------------------------------


def test_every_call_is_audit_logged(ctx: RepoContext, repo: Path) -> None:
    server = ctx_server(ctx)
    run_id = _make_run(ctx)
    _call(server, "run_list", {"limit": 5})
    _call(server, "run_status", {"run_id": run_id})
    entries = _audit(repo)
    assert [e["action"] for e in entries] == ["run_list", "run_status"]
    assert entries[0]["access"] == "read" and entries[0]["allowed"] is True
    assert entries[0]["command"] == json.dumps({"limit": 5})
    assert entries[1]["run_id"] == run_id
    assert all(e["time"] and e["duration_s"] is not None for e in entries)


def test_mutation_calls_are_audit_logged(ctx: RepoContext, repo: Path) -> None:
    server = ctx_server(ctx, allow_mutations=True)
    text = _text(_call(server, "run_start", {"idea": "audit me"}))
    run_id = text.splitlines()[0].split()[1]
    (entry,) = _audit(repo)
    assert (entry["action"], entry["access"], entry["run_id"]) == ("run_start", "write", run_id)


def test_audit_redacts_secrets(ctx: RepoContext, repo: Path) -> None:
    server = ctx_server(ctx, allow_mutations=True)
    key = "sk-or-abcdefgh12345678"
    result = _call(server, "run_start", {"idea": f"try the model with {key}"})
    assert result["isError"] is False
    (entry,) = _audit(repo)
    assert key not in entry["command"]
    assert "[REDACTED]" in entry["command"]
    # The result never echoes the idea, so a pasted secret does not come back.
    assert key not in result["content"][0]["text"]


def test_results_carry_no_config_secrets(ctx: RepoContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """Key names only: tool results never carry secret values."""
    monkeypatch.setenv("JEV_TEST_API_KEY", "supersecretvalue")
    server = ctx_server(ctx)
    run_id = _make_run(ctx)
    listed = _text(_call(server, "run_list", {}))
    status = _text(_call(server, "run_status", {"run_id": run_id}))
    assert "supersecretvalue" not in listed + status


# ---- CLI wiring -------------------------------------------------------------------------


def test_mcp_runs_command(repo: Path) -> None:
    request = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "run_list", "arguments": {}}},
    ]  # fmt: skip
    text = "".join(json.dumps(r) + "\n" for r in request)
    result = runner.invoke(app, ["mcp", "runs", "--repo", str(repo)], input=text)
    assert result.exit_code == 0, result.output
    replies = [json.loads(line) for line in result.output.splitlines()]
    assert [t["name"] for t in replies[0]["result"]["tools"]] == [
        "run_list",
        "run_status",
        "quality_show",
    ]
    assert json.loads(replies[1]["result"]["content"][0]["text"]) == []
    assert replies[1]["result"]["isError"] is False
    (entry,) = _audit(repo)
    assert entry["action"] == "run_list"


def test_mcp_runs_command_allow_mutations(repo: Path) -> None:
    request = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
    text = "".join(json.dumps(r) + "\n" for r in request)
    result = runner.invoke(
        app, ["mcp", "runs", "--allow-mutations", "--repo", str(repo)], input=text
    )
    assert result.exit_code == 0, result.output
    (reply,) = [json.loads(line) for line in result.output.splitlines()]
    assert [t["name"] for t in reply["result"]["tools"]] == [
        "run_list",
        "run_status",
        "quality_show",
        "run_start",
        "chat_send",
    ]
