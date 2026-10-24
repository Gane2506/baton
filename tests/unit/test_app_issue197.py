"""Issue #197 (swarm review: app module trust-gate gaps and destructive defaults).

One test per error path, at the app layer (not just the dashboard HTTP layer):
a duplicate entity add, the trust gate when the pre-save config is invalid,
the last-writer-wins race, an unknown ship choice, queueing from a chat
without holding it, and a profile naming a server that is not defined.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient

from jev_agent.app import chat as app_chat
from jev_agent.app import profiles as app_profiles
from jev_agent.app import settings
from jev_agent.app.errors import JevError
from jev_agent.app.ship import CHOICES, decide_ship
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.config import load_config
from jev_agent.config.schema import JevConfig
from jev_agent.core.chat import ChatInUse, ChatLearning, ChatSession, EchoChatBackend, hold_chat
from jev_agent.core.store import Store
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings


@pytest.fixture(autouse=True)
def _fake_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    app = create_app(DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})))
    with TestClient(app) as test_client:
        yield test_client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/settings").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str]) -> httpx.Response:
    return cast(
        httpx.Response,
        client.post(url, data={**data, "csrf_token": _csrf(client)}, follow_redirects=False),
    )


def _user_config() -> Path:
    import os

    return Path(os.environ["XDG_CONFIG_HOME"]) / "jev" / "config.toml"


def _trust(repo: Path) -> None:
    """Trust `repo` in the isolated test user config (the trust-gate happy path)."""
    path = _user_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    if "[trust]" not in text:
        with path.open("a") as handle:
            handle.write(f'\n[trust]\nrepos = ["{repo}"]\n')


# ---- 1. duplicate entity add ---------------------------------------------------------------


def test_save_returns_duplicate_error_instead_of_overwriting(fixture_repo: Path) -> None:
    """The old `save()` ignored its write closure, so `add_entity`'s duplicate
    check never ran and a second add silently overwrote the first entry."""

    def add(layer: dict[str, Any]) -> None:
        if settings.entity_exists(layer, "tools.mcp", "github"):
            raise ValueError("tools.mcp.github already exists")
        settings.set_path(layer, "tools.mcp.github", {"command": "npx"})

    assert settings.save(fixture_repo, "user", add) is None
    assert load_config(fixture_repo).tools.mcp["github"].command == "npx"

    def add_again(layer: dict[str, Any]) -> None:
        if settings.entity_exists(layer, "tools.mcp", "github"):
            raise ValueError("tools.mcp.github already exists")
        settings.set_path(layer, "tools.mcp.github", {"command": "other"})

    error = settings.save(fixture_repo, "user", add_again)
    assert error is not None and "already exists" in error
    assert load_config(fixture_repo).tools.mcp["github"].command == "npx"


def test_dashboard_duplicate_add_is_refused(client: TestClient, fixture_repo: Path) -> None:
    """End to end through the dashboard: adding the same entity twice errors
    on the second add and keeps the first entry."""
    entity = {
        "section": "tools.mcp",
        "action": "add",
        "name": "github",
        "target": "user",
        "keys": "command",
        "kinds": "str",
        "f_command": "npx",
    }
    assert _post(client, "/settings/entity", entity).status_code == 303
    assert load_config(fixture_repo).tools.mcp["github"].command == "npx"
    again = _post(client, "/settings/entity", {**entity, "f_command": "other"})
    assert again.status_code == 303
    assert "already exists" in client.get("/settings").text
    assert load_config(fixture_repo).tools.mcp["github"].command == "npx"


# ---- 2. trust gate when the pre-save config is invalid -------------------------------------


def test_invalid_before_config_still_needs_trust_for_repo_save(fixture_repo: Path) -> None:
    """A repo jev.toml that fails validation, plus a save that fixes validation
    AND changes a guarded key, used to bypass the trust gate entirely."""
    (fixture_repo / "jev.toml").write_text(
        '[budget]\nper_run_usd = "not-a-number"\n[workers.claude_code]\ncommand = "claude"\n'
    )

    def mutate(layer: dict[str, Any]) -> None:
        # Fixes validation AND changes a guarded key in one save.
        settings.set_path(layer, "budget.per_run_usd", 2.5)
        settings.set_path(layer, "workers.claude_code.command", "claude2")

    error = settings.save(fixture_repo, "repo", mutate)
    assert error is not None and settings.TRUST_GATE_MARKER in error
    assert "claude2" not in (fixture_repo / "jev.toml").read_text()


def test_invalid_before_config_saves_once_trusted(fixture_repo: Path) -> None:
    (fixture_repo / "jev.toml").write_text('[budget]\nper_run_usd = "not-a-number"\n')
    _trust(fixture_repo)
    error = settings.save(
        fixture_repo,
        "repo",
        lambda layer: settings.set_path(layer, "budget.per_run_usd", 2.5),
    )
    assert error is None
    assert load_config(fixture_repo).budget.per_run_usd == 2.5


def test_invalid_before_config_blocks_even_unguarded_save(fixture_repo: Path) -> None:
    """With no valid `before`, the gate cannot prove the save leaves guarded
    keys alone, so any repo save needs trust — not just guarded-key saves."""
    (fixture_repo / "jev.toml").write_text('[budget]\nper_run_usd = "not-a-number"\n')
    error = settings.save(
        fixture_repo,
        "repo",
        lambda layer: settings.set_path(layer, "budget.per_run_usd", 2.5),
    )
    assert error is not None and settings.TRUST_GATE_MARKER in error


# ---- 3. last-writer-wins race ----------------------------------------------------------------


def test_concurrent_writer_aborts_save_instead_of_discarding_it(fixture_repo: Path) -> None:
    """Compare-and-swap: a writer that changes the file mid-save (here, the
    mutate itself, simulating a racing dashboard tab) aborts the save."""

    def mutate(layer: dict[str, Any]) -> None:
        settings.set_path(layer, "budget.per_run_usd", 2.5)
        (fixture_repo / "jev.toml").write_text("[budget]\nper_run_usd = 9.0\n")

    error = settings.save(fixture_repo, "repo", mutate)
    assert error is not None and "changed while this save was in progress" in error
    assert load_config(fixture_repo).budget.per_run_usd == 9.0


def test_concurrent_saves_serialize_and_keep_both_changes(fixture_repo: Path) -> None:
    """Two threads saving different keys: the lock serializes them, so the
    second works from the first's file and neither change is lost."""
    errors: list[str | None] = []

    def save_one(key: str, value: float) -> None:
        def mutate(layer: dict[str, Any]) -> None:
            settings.set_path(layer, f"budget.{key}", value)

        errors.append(settings.save(fixture_repo, "repo", mutate))

    threads = [
        threading.Thread(target=save_one, args=("per_run_usd", 2.5)),
        threading.Thread(target=save_one, args=("per_task_usd", 1.5)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [None, None]
    config = load_config(fixture_repo)
    assert config.budget.per_run_usd == 2.5
    assert config.budget.per_task_usd == 1.5


# ---- 4. unknown ship choice ------------------------------------------------------------------


class _FakePipeline:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def approve(self, run_id: str, execute: bool) -> str:
        self.calls.append("approve")
        return "approved"

    async def request_changes(self, run_id: str, notes: str, execute: bool) -> str:
        self.calls.append("modify")
        return "modified"

    async def stop_ship(self, run_id: str) -> str:
        self.calls.append("stop")
        return "stopped"


def test_decide_ship_rejects_unknown_choice() -> None:
    """An unknown choice used to fall through to `stop_ship`, silently ending
    a run as not shipped. It must error, and touch no pipeline method."""
    pipeline = _FakePipeline()
    ctx = cast(Any, SimpleNamespace(has_workers=False, pipeline=pipeline))
    with pytest.raises(JevError, match="unknown ship choice 'explode'"):
        asyncio.run(decide_ship(ctx, "explode", "run-1", None))
    assert pipeline.calls == []


def test_decide_ship_still_dispatches_known_choices() -> None:
    pipeline = _FakePipeline()
    ctx = cast(Any, SimpleNamespace(has_workers=False, pipeline=pipeline))
    asyncio.run(decide_ship(ctx, "stop", "run-1", None))
    assert pipeline.calls == ["stop"]
    assert CHOICES == ("approve", "modify", "stop")


# ---- 5. queue_from_chat holds the chat -------------------------------------------------------


def _chat_with_brief(root: Path) -> str:
    session = ChatSession.create(root, EchoChatBackend("api"), "prompt", ChatLearning(), None, 1.0)
    session.meta.brief = "# Idea\nDo the thing"
    session.save()
    return session.meta.chat_id


def _queue_ids(root: Path) -> list[int]:
    path = root / ".jev" / "state.db"
    if not path.exists():
        return []
    with Store.open(path) as store:
        return [item.id for item in store.list_queue()]


def test_queue_from_chat_refuses_a_held_chat(fixture_repo: Path) -> None:
    """Before the fix, `queue_from_chat` recorded on the chat without
    `hold_chat`, so a reply in flight could interleave with the recording.
    With the lock held elsewhere it must now fail before queueing anything."""
    chat_id = _chat_with_brief(fixture_repo)
    with hold_chat(fixture_repo, chat_id), pytest.raises(ChatInUse):
        app_chat.queue_from_chat(fixture_repo, chat_id)
    assert _queue_ids(fixture_repo) == []


def test_queue_from_chat_records_under_the_lock(fixture_repo: Path) -> None:
    chat_id = _chat_with_brief(fixture_repo)
    item_id = app_chat.queue_from_chat(fixture_repo, chat_id)
    meta = ChatSession.load_meta(fixture_repo, chat_id)
    assert meta.queue_items and meta.queue_items[0]["id"] == item_id
    assert _queue_ids(fixture_repo) == [item_id]


# ---- 6. dangling server name in a profile -----------------------------------------------------


def _config_with_dangling_profile() -> JevConfig:
    """A config whose profile names a server that is not in `[servers]`.
    `JevConfig` validation rejects this at load time, so the state is built
    by mutating a validated config in place — the shape a stale or
    hand-built config reaches `servers_text` in."""
    config = JevConfig.model_validate(
        {"servers": {"db": {"host": "example.com"}}, "profiles": {"web": {"servers": ["db"]}}}
    )
    config.profiles["web"].servers.append("ghost")
    return config


def test_servers_text_renders_unknown_server_instead_of_crashing() -> None:
    config = _config_with_dangling_profile()
    assert app_profiles.servers_text(config, "web") == "db (basic), ghost (unknown server)"


def test_list_profiles_survives_a_dangling_profile(tmp_path: Path) -> None:
    """One bad profile must not break the entire profiles page."""
    config = _config_with_dangling_profile()
    [summary] = app_profiles.list_profiles(config, tmp_path)
    assert summary.name == "web"
    assert "ghost (unknown server)" in summary.servers
