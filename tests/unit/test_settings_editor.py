"""The settings editor (#9): schema-driven fields, validated writes, the trust gate."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from starlette.testclient import TestClient

from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.config import load_config
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings


@pytest.fixture(autouse=True)
def _fake_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    app = create_app(DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})))
    with TestClient(app) as client:
        yield client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/settings").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str]) -> httpx.Response:
    response = cast(
        httpx.Response,
        client.post(url, data={**data, "csrf_token": _csrf(client)}, follow_redirects=False),
    )
    return response


def _repo_text(repo: Path) -> str:
    file = repo / "jev.toml"
    return file.read_text() if file.exists() else ""


def test_pages_split_agents_tools_connections_from_settings(client: TestClient) -> None:
    agents = client.get("/agents")
    assert agents.status_code == 200
    assert "Worker: Claude Code" in agents.text and "roles.coder" in agents.text
    assert "Dispatcher: Jev" in agents.text
    assert 'value="workers.claude_code.command"' in agents.text  # a hidden path field

    tools = client.get("/tools")
    assert "Tools: MCP servers" in tools.text and "Tools: plugins" in tools.text
    assert "Add to Tools: MCP servers" in tools.text

    connections = client.get("/connections")
    assert "Servers (SSH)" in connections.text
    assert "Trust (user config only)" in connections.text
    assert "Workspace profiles" in connections.text

    settings = client.get("/settings")
    assert "Budget and limits" in settings.text and "Effective config" in settings.text
    assert "Worker: Claude Code" not in settings.text
    assert "Tools: MCP servers" not in settings.text
    assert "Environment variables" in settings.text  # the read-only panels stay here


def test_save_and_reset_a_field(client: TestClient, fixture_repo: Path) -> None:
    saved = _post(
        client,
        "/settings/set",
        {"path": "budget.per_run_usd", "kind": "float", "value": "2.5", "target": "repo"},
    )
    assert saved.status_code == 303
    assert "per_run_usd = 2.5" in (fixture_repo / "jev.toml").read_text()
    assert load_config(fixture_repo).budget.per_run_usd == 2.5
    assert "saved budget.per_run_usd" in client.get("/settings").text

    reset = _post(client, "/settings/unset", {"path": "budget.per_run_usd", "target": "repo"})
    assert reset.status_code == 303
    assert "per_run_usd" not in (fixture_repo / "jev.toml").read_text()
    assert load_config(fixture_repo).budget.per_run_usd == 5.0


def test_bad_values_are_refused_with_a_message(client: TestClient, fixture_repo: Path) -> None:
    bad = _post(
        client,
        "/settings/set",
        {"path": "budget.max_plan_rounds", "kind": "int", "value": "many", "target": "repo"},
    )
    assert bad.status_code == 303
    assert "not a whole number" in client.get("/settings").text
    assert "max_plan_rounds" not in _repo_text(fixture_repo)

    cross = _post(
        client,
        "/settings/set",
        {"path": "budget.per_task_usd", "kind": "float", "value": "99", "target": "repo"},
    )
    assert cross.status_code == 303
    assert "must not exceed per_run_usd" in client.get("/settings").text
    assert "per_task_usd" not in _repo_text(fixture_repo)


def test_guarded_key_needs_trust_then_saves(client: TestClient, fixture_repo: Path) -> None:
    refused = _post(
        client,
        "/settings/set",
        {
            "path": "workers.claude_code.command",
            "kind": "str",
            "value": "claude2",
            "target": "repo",
        },
    )
    assert refused.status_code == 303
    assert "only when you trust it" in client.get("/settings").text
    assert "claude2" not in _repo_text(fixture_repo)

    trusted = _post(
        client,
        "/settings/set",
        {"path": "trust.repos", "kind": "list", "value": str(fixture_repo), "target": "user"},
    )
    assert trusted.status_code == 303
    again = _post(
        client,
        "/settings/set",
        {
            "path": "workers.claude_code.command",
            "kind": "str",
            "value": "claude2",
            "target": "repo",
        },
    )
    assert again.status_code == 303
    assert load_config(fixture_repo).workers.claude_code.command == "claude2"


def test_entity_add_edit_remove_in_the_user_config(client: TestClient, fixture_repo: Path) -> None:
    added = _post(
        client,
        "/settings/entity",
        {
            "section": "tools.mcp",
            "action": "add",
            "name": "github",
            "target": "user",
            "keys": "command,args,env_from",
            "kinds": "str,list,list",
            "f_command": "npx",
            "f_args": "-y\n@modelcontextprotocol/server-github",
            "f_env_from": "GITHUB_PERSONAL_ACCESS_TOKEN",
        },
    )
    assert added.status_code == 303
    config = load_config(fixture_repo)
    assert config.tools.mcp["github"].command == "npx"
    assert config.tools.mcp["github"].args == ["-y", "@modelcontextprotocol/server-github"]
    page = client.get("/tools")
    assert "github" in page.text and "env_from" in page.text

    bad_name = _post(
        client,
        "/settings/entity",
        {"section": "tools.mcp", "action": "add", "name": "bad name!", "target": "user"},
    )
    assert bad_name.status_code == 303
    assert "must start with a letter or digit" in client.get("/settings").text

    removed = _post(
        client,
        "/settings/entity",
        {"section": "tools.mcp", "action": "remove", "name": "github", "target": "user"},
    )
    assert removed.status_code == 303
    assert "github" not in load_config(fixture_repo).tools.mcp


def test_planner_policy_picker_saves_policy_and_syncs_pool(
    client: TestClient, fixture_repo: Path
) -> None:
    # The picker renders all four modes with the honest mode-3 warning (issue #327).
    page = client.get("/settings")
    assert page.status_code == 200
    for value in ("free_training_ok", "mixed", "mixed_no_training", "paid_only"):
        assert f'value="{value}"' in page.text
    assert "openrouter.ai/settings/privacy" in page.text
    assert "Baton cannot change your OpenRouter account setting" in page.text

    saved = _post(
        client,
        "/settings/set",
        {
            "path": "models.planner_policy",
            "kind": "enum",
            "value": "mixed_no_training",
            "target": "repo",
        },
    )
    assert saved.status_code == 303
    config = load_config(fixture_repo)
    assert config.models.planner_policy == "mixed_no_training"
    # The pool is synced so every reader of `roles.planner.models` sees the same pool.
    assert config.roles.planner.models == ["free", "openai/gpt-5-mini"]
    assert "saved planner model policy: mixed_no_training" in client.get("/settings").text

    paid = _post(
        client,
        "/settings/set",
        {"path": "models.planner_policy", "kind": "enum", "value": "paid_only", "target": "repo"},
    )
    assert paid.status_code == 303
    assert load_config(fixture_repo).roles.planner.models == ["openai/gpt-5-mini"]

    bad = _post(
        client,
        "/settings/set",
        {"path": "models.planner_policy", "kind": "enum", "value": "everything", "target": "repo"},
    )
    assert bad.status_code == 303
    assert "unknown planner model policy" in client.get("/settings").text
    assert load_config(fixture_repo).models.planner_policy == "paid_only"

    reset = _post(client, "/settings/unset", {"path": "models.planner_policy", "target": "repo"})
    assert reset.status_code == 303
    config = load_config(fixture_repo)
    assert config.models.planner_policy is None
    # Reset leaves the pool alone: the policy is then derived from it (paid_only).
    assert config.roles.planner.models == ["openai/gpt-5-mini"]
