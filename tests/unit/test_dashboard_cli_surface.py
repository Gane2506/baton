"""Dashboard CLI surface (issue #88): scheduler strip, queue clear and friends (no network).

The chat backends, the run launcher and the workers are all fakes here: `JEV_FAKE_WORKERS=1`
swaps in a fake registry, and the launcher's executor is a `Calls` recorder, so nothing
touches a model, a CLI or GitHub.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient

from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.config import load_config
from jev_agent.core.scheduler import scheduler_lock
from jev_agent.core.store import ShippedPrRecord, Store
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.browser_install import BrowserInstallRunner
from jev_agent.dashboard.runner import RunAction
from jev_agent.dashboard.security import DashboardSettings


@pytest.fixture(autouse=True)
def _fake_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


class Calls:
    """A fake run executor: records calls and answers at once (no workers run)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, RunAction]] = []

    def __call__(self, root: Path, run_id: str, action: RunAction) -> str:
        self.calls.append((root, run_id, action))
        return "run DONE"


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def client(fixture_repo: Path, calls: Calls) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
        executor=calls,
    )
    with TestClient(app) as client:
        yield client


def _dashboard(client: TestClient) -> Any:
    return cast(Any, client.app).state.dashboard


def _store(repo: Path) -> Store:
    return Store.open(repo / ".jev" / "state.db")


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/chats").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str] | None = None) -> httpx.Response:
    response = client.post(
        url,
        data={**(data or {}), "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    return cast(httpx.Response, response)


def _follow(client: TestClient, response: httpx.Response) -> httpx.Response:
    assert response.status_code == 303
    return cast(httpx.Response, client.get(response.headers["location"]))


# ---- scheduler strip ---------------------------------------------------------------------


def test_queue_strip_offers_start_when_idle(client: TestClient) -> None:
    text = client.get("/queue").text
    assert "not running" in text
    assert "Start scheduler" in text
    assert "Stop scheduler" not in text


def test_scheduler_start_first_post_names_the_spend(client: TestClient) -> None:
    response = _post(client, "/queue/scheduler/start")
    assert response.status_code == 200
    assert "spends real money" in response.text
    assert "Spent today" in response.text
    assert "stops when the dashboard stops" in response.text or "dies when the dashboard" in (
        response.text
    )


def test_scheduler_start_confirmed_runs_and_stops(client: TestClient, fixture_repo: Path) -> None:
    response = _post(client, "/queue/scheduler/start", {"confirm": "yes"})
    assert response.status_code == 303
    dash = _dashboard(client)
    assert dash.scheduler_runner.owned()
    try:
        text = client.get("/queue").text
        assert "Started from this dashboard" in text
        assert "Stop scheduler" in text
        response = _post(client, "/queue/scheduler/stop")
        assert response.status_code == 303
        for _ in range(100):
            if not dash.scheduler_runner.owned():
                break
            time.sleep(0.05)
        assert not dash.scheduler_runner.owned()
    finally:
        dash.scheduler_runner.shutdown(5)


def test_scheduler_runs_a_queued_item_through_the_launcher(
    client: TestClient, calls: Calls, fixture_repo: Path
) -> None:
    with _store(fixture_repo) as store:
        store.add_queue_item("dashboard scheduler item")
    response = _post(client, "/queue/scheduler/start", {"confirm": "yes"})
    assert response.status_code == 303
    dash = _dashboard(client)
    try:
        for _ in range(200):
            with _store(fixture_repo) as store:
                items = store.list_queue()
            note = items[0].note or "" if items else ""
            if note.startswith("run "):
                break
            time.sleep(0.05)
        else:
            pytest.fail("the scheduler did not settle the queued item")
        run_id = items[0].run_id
        assert items[0].run_id is not None
        assert calls.calls and calls.calls[0][1] == run_id and calls.calls[0][2] == "start"
        assert calls.calls[0][1] == run_id and calls.calls[0][2] == "start"
    finally:
        dash.scheduler_runner.stop()
        dash.scheduler_runner.shutdown(10)


def test_scheduler_start_refused_when_an_external_scheduler_holds_the_lock(
    client: TestClient, fixture_repo: Path
) -> None:
    lock = scheduler_lock(fixture_repo)
    lock.acquire()
    try:
        text = client.get("/queue").text
        assert "Started outside this dashboard" in text
        assert "Stop scheduler" not in text
        response = _post(client, "/queue/scheduler/start", {"confirm": "yes"})
        assert response.status_code == 303
        assert "another scheduler is running" in _follow(client, response).text
        assert not _dashboard(client).scheduler_runner.owned()
    finally:
        lock.release()


def test_scheduler_stop_without_a_dashboard_scheduler_errors(client: TestClient) -> None:
    response = _post(client, "/queue/scheduler/stop")
    assert response.status_code == 303
    assert "is not running" in _follow(client, response).text


# ---- ship sync on the KPIs page --------------------------------------------------------------


def _add_shipped_pr(repo: Path) -> str:
    from jev_agent.core.models import Idea

    with _store(repo) as store:
        run = store.create_run(Idea(text="ship me", repo_path=repo))
        store.add_shipped_pr(
            run.id,
            remote="origin",
            repo="owner/repo",
            branch="jev/branch",
            base="main",
            number=12,
            url="https://example.test/12",
            title="A PR",
            draft=False,
        )
        return run.id


def test_kpis_sync_button_and_never_synced(client: TestClient) -> None:
    text = client.get("/kpis").text
    assert "Sync from GitHub" in text
    assert "Never synced" in text


def test_kpis_sync_with_no_prs_flashes(client: TestClient) -> None:
    response = _post(client, "/kpis/sync")
    assert response.status_code == 303
    assert "nothing shipped yet" in _follow(client, response).text


def test_kpis_sync_runs_and_stamps_last_synced(
    client: TestClient, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jev_agent.dashboard.app as app_module

    _add_shipped_pr(fixture_repo)
    synced: list[str] = []

    def _sync_pr(store: Store, gh: object, pr: ShippedPrRecord) -> ShippedPrRecord:
        synced.append(pr.branch)
        return pr

    monkeypatch.setattr(app_module, "check_gh", lambda gh: None)
    monkeypatch.setattr(app_module, "sync_pr", _sync_pr)
    response = _post(client, "/kpis/sync")
    assert response.status_code == 303
    assert "synced 1 PR(s)" in _follow(client, response).text
    assert synced == ["jev/branch"]
    text = client.get("/kpis").text
    assert "Never synced" not in text
    assert "Synced " in text


def test_kpis_sync_surfaces_a_gh_problem(
    client: TestClient, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jev_agent.dashboard.app as app_module

    _add_shipped_pr(fixture_repo)
    monkeypatch.setattr(app_module, "check_gh", lambda gh: "gh is not logged in")
    response = _post(client, "/kpis/sync")
    assert response.status_code == 303
    assert "gh is not logged in" in _follow(client, response).text


# ---- learning delete ---------------------------------------------------------------------


def test_learning_delete_is_two_step(client: TestClient) -> None:
    response = _post(client, "/learning/create", {"name": "temp"})
    assert response.status_code == 303
    response = _post(client, "/learning/delete", {"profile": "temp"})
    assert response.status_code == 200
    assert "Delete profile temp?" in response.text
    assert "0 of its lessons" in response.text
    # Not confirmed: the profile is still there.
    assert "temp" in client.get("/learning").text
    response = _post(client, "/learning/delete", {"profile": "temp", "confirm": "yes"})
    assert response.status_code == 303
    assert "deleted profile temp" in _follow(client, response).text


def test_learning_delete_unknown_profile_errors(client: TestClient) -> None:
    response = _post(client, "/learning/delete", {"profile": "nope"})
    assert response.status_code == 303
    assert "nope" in _follow(client, response).text  # the flash names the problem


# ---- cycle 3: confirm before removing API keys and deleting chats (issue #185) ----


def _isolated_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the secrets file at a throwaway path (issue #185 tests only)."""
    from jev_agent.core import secrets

    file = tmp_path / ".jev" / "secrets.env"
    monkeypatch.setenv(secrets.FILE_ENV, str(file))
    secrets._loaded.clear()
    return file


def _new_chat(client: TestClient) -> str:
    response = _post(client, "/chats/new", {"worker": "api", "learning": "", "budget_usd": "1.00"})
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def test_key_remove_is_two_step(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    from jev_agent.core import secrets

    file = _isolated_secrets(monkeypatch, tmp_path)
    try:
        response = _post(
            client, "/connections/keys/add", {"name": "CYCLE3_TEST_KEY", "value": "fake-key"}
        )
        assert response.status_code == 303
        assert "CYCLE3_TEST_KEY" in file.read_text()
        response = _post(client, "/connections/keys/remove", {"name": "CYCLE3_TEST_KEY"})
        assert response.status_code == 200
        assert "Remove API key CYCLE3_TEST_KEY?" in response.text
        assert "will break until a new key is added" in response.text
        # Not confirmed: the key is still there.
        assert "CYCLE3_TEST_KEY" in file.read_text()
        response = _post(
            client,
            "/connections/keys/remove",
            {"name": "CYCLE3_TEST_KEY", "confirm": "yes"},
        )
        assert response.status_code == 303
        assert "removed from the secrets file" in _follow(client, response).text
        assert "CYCLE3_TEST_KEY" not in file.read_text()
    finally:
        secrets._loaded.discard("CYCLE3_TEST_KEY")
        os.environ.pop("CYCLE3_TEST_KEY", None)


def test_key_remove_unknown_key_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolated_secrets(monkeypatch, tmp_path)
    response = _post(client, "/connections/keys/remove", {"name": "CYCLE3_NOPE_KEY"})
    assert response.status_code == 303
    assert "CYCLE3_NOPE_KEY" in _follow(client, response).text


def test_chat_delete_is_two_step(client: TestClient) -> None:
    chat_id = _new_chat(client)
    assert chat_id in client.get("/chats").text
    response = _post(client, f"/chats/{chat_id}/delete")
    assert response.status_code == 200
    assert f"Delete chat {chat_id}?" in response.text
    assert "Runs it started are kept" in response.text
    # Not confirmed: the chat is still there.
    assert chat_id in client.get("/chats").text
    response = _post(client, f"/chats/{chat_id}/delete", {"confirm": "yes"})
    assert response.status_code == 303
    assert f"deleted chat {chat_id}" in _follow(client, response).text
    assert chat_id not in client.get("/chats").text


def test_chat_delete_unknown_chat_errors(client: TestClient) -> None:
    response = _post(client, "/chats/no-such-chat/delete")
    assert response.status_code == 303
    assert "no-such-chat" in _follow(client, response).text


# ---- backend switch (issue #277) ---------------------------------------------------------


def _new_claude_chat(client: TestClient) -> str:
    response = _post(
        client, "/chats/new", {"worker": "claude_code", "learning": "", "budget_usd": "1.00"}
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def _chat_meta(repo: Path, chat_id: str) -> dict[str, Any]:
    return cast(
        dict[str, Any], json.loads((repo / ".jev" / "chats" / chat_id / "meta.json").read_text())
    )


def _set_chat_session(repo: Path, chat_id: str, session_id: str | None) -> None:
    path = repo / ".jev" / "chats" / chat_id / "meta.json"
    meta = json.loads(path.read_text())
    meta["session_id"] = session_id
    path.write_text(json.dumps(meta, indent=2))


def test_chat_backend_switch_moves_claude_chat_to_api(
    client: TestClient, fixture_repo: Path
) -> None:
    chat_id = _new_claude_chat(client)
    assert _chat_meta(fixture_repo, chat_id)["backend"] == "claude_code"
    _set_chat_session(fixture_repo, chat_id, "sess-old")
    response = _post(
        client,
        f"/chats/{chat_id}/backend",
        {"worker": "api", "model": "openai/gpt-4o-mini"},
    )
    assert response.status_code == 303
    assert "now uses the API backend" in _follow(client, response).text
    meta = _chat_meta(fixture_repo, chat_id)
    assert meta["backend"] == "api"
    assert meta["model"] == "openai/gpt-4o-mini"
    # A different backend starts a new model session: the old session id is gone.
    assert meta["session_id"] is None
    assert "OpenRouter API" in client.get(f"/chats/{chat_id}").text


def test_chat_backend_switch_needs_csrf(client: TestClient) -> None:
    chat_id = _new_claude_chat(client)
    response = client.post(
        f"/chats/{chat_id}/backend",
        data={"worker": "api", "model": "openai/gpt-4o-mini"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_chat_backend_switch_unknown_chat_errors(client: TestClient) -> None:
    response = _post(
        client, "/chats/no-such-chat/backend", {"worker": "api", "model": "openai/gpt-4o-mini"}
    )
    assert response.status_code == 303
    assert "no-such-chat" in _follow(client, response).text


def test_chat_backend_switch_same_backend_errors(client: TestClient) -> None:
    chat_id = _new_chat(client)
    response = _post(client, f"/chats/{chat_id}/backend", {"worker": "api"})
    assert response.status_code == 303
    assert "already uses" in _follow(client, response).text


def test_chat_backend_switch_invalid_worker_errors(client: TestClient) -> None:
    chat_id = _new_claude_chat(client)
    response = _post(client, f"/chats/{chat_id}/backend", {"worker": "bogus"})
    assert response.status_code == 303
    assert "pick a chat backend" in _follow(client, response).text


def test_chat_page_offers_switch_when_backend_not_ready(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claude_code chat's CLI is not on PATH, so the page offers the way out."""
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    chat_id = _new_claude_chat(client)
    text = client.get(f"/chats/{chat_id}").text
    assert "Switch to API backend" in text
    assert f'action="/chats/{chat_id}/backend"' in text
    # The API backend is not ready either (no key), so no model picker is shown.
    assert 'id="switch-model"' not in text


def test_chat_page_switch_form_needs_model_when_no_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API key set, no default model: the switch form carries a required model input."""
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    chat_id = _new_claude_chat(client)
    text = client.get(f"/chats/{chat_id}").text
    match = re.search(r'<input[^>]*id="switch-model"[^>]*>', text)
    assert match and "required" in match.group(0)
    assert "anthropic/claude-sonnet-4" in text


def test_new_chat_model_required_when_no_default_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #277: with no default model the New chat model input is required with a
    visible label, so the browser blocks an empty submit inline."""
    text = client.get("/chats").text
    match = re.search(r'<input[^>]*id="nc-model"[^>]*>', text)
    assert match and "required" not in match.group(0)
    assert '<label class="sr-only" for="nc-model">Model (optional)</label>' in text
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    text = client.get("/chats").text
    match = re.search(r'<input[^>]*id="nc-model"[^>]*>', text)
    assert match and "required" in match.group(0)
    assert '<label for="nc-model">Model</label>' in text


def test_scheduler_routes_need_csrf(client: TestClient) -> None:
    for url in ("/queue/scheduler/start", "/queue/scheduler/stop", "/queue/clear"):
        response = client.post(url, data={"confirm": "yes"}, follow_redirects=False)
        assert response.status_code == 403, url


def test_diagnostics_page_renders_doctor_checks(client: TestClient) -> None:
    response = client.get("/diagnostics")
    assert response.status_code == 200
    assert "jev doctor" in response.text or "Diagnostics" in response.text
    assert "git repo" in response.text  # one of the doctor checks
    # In the System nav group, next to Settings.
    assert 'href="/diagnostics"' in response.text


def test_diagnostics_never_shows_secret_values(client: TestClient) -> None:
    text = client.get("/diagnostics").text
    assert "OPENROUTER_API_KEY" not in text or "not set" in text


def test_phase1_post_routes_need_csrf(client: TestClient) -> None:
    for url in ("/kpis/sync", "/learning/delete"):
        response = client.post(url, data={"confirm": "yes"}, follow_redirects=False)
        assert response.status_code == 403, url


# ---- queue clear -------------------------------------------------------------------------


def test_queue_clear_is_two_step(client: TestClient, fixture_repo: Path) -> None:
    with _store(fixture_repo) as store:
        store.add_queue_item("first")
        store.add_queue_item("second")
    response = _post(client, "/queue/clear")
    assert response.status_code == 200
    assert "Clear the queue?" in response.text
    assert "2 items" in response.text
    # Not confirmed: nothing deleted.
    with _store(fixture_repo) as store:
        assert len(store.list_queue(["queued"])) == 2
    response = _post(client, "/queue/clear", {"confirm": "yes"})
    assert response.status_code == 303
    assert "removed 2 queued item(s)" in _follow(client, response).text
    with _store(fixture_repo) as store:
        assert store.list_queue(["queued"]) == []


def test_queue_clear_on_an_empty_queue_flashes(client: TestClient) -> None:
    response = _post(client, "/queue/clear")
    assert response.status_code == 303
    assert "already empty" in _follow(client, response).text


def test_get_refresh_of_post_pages_redirects(client: TestClient) -> None:
    """Refreshing a POST-rendered page (GET, issue #316) goes back to its
    form/status page, never a bare 405 — for every confirm page and the
    code-grade validation re-render."""
    chat_id = _new_chat(client)
    cases = [
        ("/queue/scheduler/start", "/queue", True),
        ("/queue/clear", "/queue", True),
        ("/learning/flush", "/learning", True),
        ("/learning/delete", "/learning", True),
        (f"/chats/{chat_id}/delete", "/chats", True),
        ("/connections/keys/remove", "/connections", True),
        ("/connections/browser/install", "/connections", True),
        ("/code-grade/grade", "/code-grade", False),
        ("/code-grade/route", "/code-grade", False),
    ]
    for url, target, flashes in cases:
        response = cast(httpx.Response, client.get(url, follow_redirects=False))
        assert response.status_code == 303, url
        assert response.headers["location"] == target, url
        if flashes:
            assert "only follows its form" in _follow(client, response).text, url


# ---- tools: models, quality checks, MCP note -----------------------------------------------------


def test_tools_page_without_a_model_cache(client: TestClient) -> None:
    text = client.get("/tools").text
    assert 'id="models"' in text
    assert "No model list yet" in text
    assert "jev models list --refresh" not in text  # browser-first: no CLI instruction
    assert 'id="quality-checks"' in text
    assert "ai-review" in text  # the synthetic gate row, like the CLI
    assert "jev mcp servers" in text
    assert "jev mcp runs" in text


def test_tools_models_table_renders(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import jev_agent.dashboard.views as views_module
    from jev_agent.app.models import ModelListing, ModelRow

    listing = ModelListing(
        models=[
            ModelRow(
                id="test/model",
                context_length=128000,
                prompt_price=0.0,
                completion_price=0.0,
                supports={"json_schema": True, "reasoning": None},
            )
        ],
        fetched_hours_ago=1.5,
        title="models",
    )
    monkeypatch.setattr(views_module, "model_listing", lambda repo: (listing, None))
    text = client.get("/tools").text
    assert "test/model" in text
    assert "128,000" in text
    assert "free" in text  # a zero price renders as free
    assert 'id="models-filter"' in text
    assert 'data-copy="test/model"' in text  # the copy affordance (live.js handles it)


# ---- profiles ----------------------------------------------------------------------------------


def _write_profiles_toml(repo: Path, tmp_path: Path) -> None:
    # [profiles.*] is trust-guarded: trust the repo like the user's own repository.
    trust = tmp_path / "xdg" / "jev" / "config.toml"
    trust.parent.mkdir(parents=True, exist_ok=True)
    with trust.open("a") as fh:
        fh.write('[trust]\nrepos = ["*"]\n')
    (repo / "jev.toml").write_text(
        '[profiles.dev]\ndescription = "dev box"\n[profiles.prod]\ndescription = "prod"\n'
    )


def test_profiles_page_empty_state(client: TestClient) -> None:
    text = client.get("/profiles").text
    assert "No workspace profiles" in text
    assert 'href="/profiles"' in text  # in the Agents nav group
    # Browser-first (issue #171): no config-file instruction; the page offers a form.
    assert "Define them in" not in text
    assert "New profile" in text
    assert 'name="return_to" value="/profiles"' in text


def test_profiles_add_from_profiles_page(client: TestClient, fixture_repo: Path) -> None:
    added = _post(
        client,
        "/settings/entity",
        {
            "section": "profiles",
            "action": "add",
            "name": "web",
            "target": "user",
            "keys": "description,browser",
            "kinds": "str,bool",
            "f_description": "added from the dashboard",
            "f_browser": "true",
            "return_to": "/profiles",
        },
    )
    assert added.status_code == 303
    assert added.headers["location"] == "/profiles"
    config = load_config(fixture_repo)
    assert config.profiles["web"].description == "added from the dashboard"
    assert config.profiles["web"].browser is True
    assert "saved profiles.web" in _follow(client, added).text
    assert "web" in client.get("/profiles").text


def test_entity_return_to_ignores_absolute_urls(client: TestClient) -> None:
    for i, evil in enumerate(("https://evil.example.com/x", "//evil.example.com/x")):
        response = _post(
            client,
            "/settings/entity",
            {
                "section": "profiles",
                "action": "add",
                "name": f"bad{i}",
                "target": "user",
                "keys": "",
                "kinds": "",
                "return_to": evil,
            },
        )
        assert response.status_code == 303
        # Falls back to the section's own page, never the forged URL.
        assert response.headers["location"] == "/connections#s-profiles"


def test_profiles_list_show_and_use(client: TestClient, fixture_repo: Path, tmp_path: Path) -> None:
    _write_profiles_toml(fixture_repo, tmp_path)
    text = client.get("/profiles").text
    assert "dev" in text and "prod" in text
    assert "dev box" in text
    text = client.get("/profiles?name=dev").text
    assert "What each role gets" in text
    response = _post(client, "/profiles/use", {"profile": "dev"})
    assert response.status_code == 303
    assert "default workspace profile: dev" in _follow(client, response).text
    text = client.get("/profiles").text
    assert "default" in text
    response = _post(client, "/profiles/use", {"profile": "none"})
    assert "no default workspace profile" in _follow(client, response).text


def test_profiles_use_unknown_profile_errors(
    client: TestClient, fixture_repo: Path, tmp_path: Path
) -> None:
    _write_profiles_toml(fixture_repo, tmp_path)
    response = _post(client, "/profiles/use", {"profile": "nope"})
    assert response.status_code == 303
    assert "no workspace profile named" in _follow(client, response).text


def test_profiles_show_unknown_profile_is_404(
    client: TestClient, fixture_repo: Path, tmp_path: Path
) -> None:
    _write_profiles_toml(fixture_repo, tmp_path)
    assert client.get("/profiles?name=nope").status_code == 404


def _write_profile_with_overrides(repo: Path, tmp_path: Path) -> None:
    trust = tmp_path / "xdg" / "jev" / "config.toml"
    trust.parent.mkdir(parents=True, exist_ok=True)
    with trust.open("a") as fh:
        fh.write('[trust]\nrepos = ["*"]\n')
    (repo / "jev.toml").write_text(
        '[servers.db]\nhost = "db1"\naccess = "medium"\n'
        "[profiles.dev]\n"
        'description = "dev box"\n'
        'servers = ["db"]\n'
        "browser = true\n"
        "[profiles.dev.access_overrides]\n"
        'db = "basic"\n'
    )


def test_profiles_detail_shows_access_overrides_and_source(
    client: TestClient, fixture_repo: Path, tmp_path: Path
) -> None:
    _write_profile_with_overrides(fixture_repo, tmp_path)
    text = client.get("/profiles").text
    assert "Repo jev.toml" in text
    text = client.get("/profiles?name=dev").text
    assert "Access overrides" in text
    assert "db" in text and "basic" in text
    assert "Repo jev.toml" in text


def test_profiles_source_labels_user_config(
    client: TestClient, fixture_repo: Path, tmp_path: Path
) -> None:
    user_cfg = tmp_path / "xdg" / "jev" / "config.toml"
    user_cfg.parent.mkdir(parents=True, exist_ok=True)
    user_cfg.write_text('[profiles.mine]\ndescription = "from user config"\n')
    text = client.get("/profiles").text
    assert "mine" in text
    assert "User config" in text
    text = client.get("/profiles?name=mine").text
    assert "User config" in text
    assert "None \u2014 every server uses its configured access level." in text


# ---- browser install ---------------------------------------------------------------------------


class _HangingProc:
    """An installer that runs until terminated."""

    def __init__(self) -> None:
        self.terminated = False

    def wait(self) -> int:
        import time as _time

        while not self.terminated:
            _time.sleep(0.01)
        return -15

    def terminate(self) -> None:
        self.terminated = True


class _DoneProc:
    def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        pass


def _installer_with(client: TestClient, fixture_repo: Path, proc: object) -> BrowserInstallRunner:
    def fake_spawn(argv: list[str], log: Path) -> object:
        log.write_bytes(b"fake install output\n")
        # the fixed installer command (resolved to an absolute path), never from the request
        assert Path(argv[0]).name in ("npx", "npx.exe")
        return proc

    dash = _dashboard(client)
    installer = BrowserInstallRunner(fixture_repo, spawn=fake_spawn)  # type: ignore[arg-type]
    dash.browser_installer = installer
    return installer


def test_connections_has_the_browser_panel(client: TestClient) -> None:
    text = client.get("/connections").text
    assert 'id="browser"' in text
    assert "Install browser" in text


def test_browser_install_is_two_step_and_single_flight(
    client: TestClient, fixture_repo: Path
) -> None:
    installer = _installer_with(client, fixture_repo, _HangingProc())
    try:
        response = _post(client, "/connections/browser/install")
        assert response.status_code == 200
        assert "npx" in response.text and "playwright" in response.text
        response = _post(client, "/connections/browser/install", {"confirm": "yes"})
        assert response.status_code == 303
        assert "browser install started" in _follow(client, response).text
        assert installer.busy()
        assert "installing" in client.get("/connections").text
        # Single-flight: a second start is refused.
        response = _post(client, "/connections/browser/install", {"confirm": "yes"})
        assert "already running" in _follow(client, response).text
        response = _post(client, "/connections/browser/install/stop")
        assert response.status_code == 303
        assert "stopping the browser install" in _follow(client, response).text
        assert not installer.busy()
    finally:
        installer.shutdown(5)


def test_browser_install_done_shows_output(client: TestClient, fixture_repo: Path) -> None:
    installer = _installer_with(client, fixture_repo, _DoneProc())
    response = _post(client, "/connections/browser/install", {"confirm": "yes"})
    assert response.status_code == 303
    for _ in range(100):
        if installer.status().state == "done":
            break
        import time as _time

        _time.sleep(0.05)
    status = installer.status()
    assert status.state == "done" and status.message == "browser installed"
    text = client.get("/connections").text
    assert "fake install output" in text


def test_browser_install_stop_without_running_errors(client: TestClient) -> None:
    response = _post(client, "/connections/browser/install/stop")
    assert response.status_code == 303
    assert "no browser install is running" in _follow(client, response).text


def test_browser_install_routes_need_csrf(client: TestClient) -> None:
    for url in ("/connections/browser/install", "/connections/browser/install/stop"):
        response = client.post(url, data={"confirm": "yes"}, follow_redirects=False)
        assert response.status_code == 403, url


# ---- command palette ---------------------------------------------------------------------------


def _palette_registry(client: TestClient) -> dict[str, Any]:
    import json

    match = re.search(
        r'<script type="application/json" id="palette-registry">(.*?)</script>',
        client.get("/queue").text,
        re.DOTALL,
    )
    assert match, "the palette registry is rendered into the page"
    data: dict[str, Any] = json.loads(match.group(1))
    return data


def _routes(client: TestClient) -> dict[str, set[str]]:
    routes: dict[str, set[str]] = {}
    for route in cast(Any, client.app).routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods and path:
            routes.setdefault(path, set()).update(methods)
    return routes


def test_palette_registry_is_valid(client: TestClient) -> None:
    registry = _palette_registry(client)
    routes = _routes(client)
    assert registry["csrf_field"] == "csrf_token" and registry["csrf_token"]
    for entry in registry["entries"]:
        assert entry["kind"] in ("nav", "action", "cli"), entry
        assert entry["label"], entry
        if entry["kind"] == "nav":
            path = entry["target"].split("#")[0] or "/"
            assert path in routes and "GET" in routes[path], entry
        elif entry["kind"] == "action":
            assert entry["target"] in routes and "POST" in routes[entry["target"]], entry
        else:
            assert entry["command"].startswith("jev "), entry


def test_palette_covers_every_cli_command_verb(client: TestClient) -> None:
    """#88's done bar: every command verb is a page, a section, an action or a
    greyed CLI entry."""
    registry = _palette_registry(client)
    text = " ".join(
        e["label"] + " " + e.get("target", "") + " " + e.get("command", "")
        for e in registry["entries"]
    ).lower()
    for verb in (
        "board",
        "run",
        "approve",
        "queue",
        "scheduler",
        "chat",
        "ship",
        "kpi",
        "learning",
        "models",
        "quality",
        "doctor",
        "profile",
        "browser",
        "init",
        "dashboard",
        "mcp",
    ):
        assert verb in text, verb


def test_palette_cli_only_entries_are_greyed(client: TestClient) -> None:
    registry = _palette_registry(client)
    greyed = {e["command"] for e in registry["entries"] if e["kind"] == "cli"}
    assert "jev mcp servers" in greyed
    assert "jev mcp runs" in greyed
    assert "jev init" in greyed
    assert "jev dashboard" in greyed


def test_palette_chrome_renders(client: TestClient) -> None:
    text = client.get("/").text
    assert 'id="palette"' in text
    assert 'id="palette-input"' in text
    assert "/static/palette.js" in text
    assert "data-palette-open" in text


# ---- about this dashboard ------------------------------------------------------------------------


def test_settings_about_panel(client: TestClient) -> None:
    text = client.get("/settings").text
    assert "This dashboard" in text
    assert "127.0.0.1:8765" in text
    assert "loopback only, no token" in text
    assert "Started" in text
    from jev_agent import __version__

    assert __version__ in text


def test_browser_install_reaps_real_child_process(tmp_path: Path) -> None:
    """P2 regression: a real Popen child is reaped via communicate() — the runner
    owns the child's lifetime, leaving no zombie behind."""
    import subprocess
    import time as _time

    from jev_agent.dashboard.browser_install import BrowserInstallRunner

    procs: list[subprocess.Popen[bytes]] = []

    def spawn(argv: list[str], log: Path) -> subprocess.Popen[bytes]:
        handle = log.open("ab")
        proc = subprocess.Popen(["true"], stdout=handle, stderr=subprocess.STDOUT)
        procs.append(proc)
        return proc

    runner = BrowserInstallRunner(tmp_path, spawn=spawn)
    runner.start()
    for _ in range(100):
        if runner.status().state == "done":
            break
        _time.sleep(0.05)
    assert runner.status().state == "done"
    assert procs and procs[0].poll() is not None  # reaped, not a zombie
    runner.shutdown(5)
