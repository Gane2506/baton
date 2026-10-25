"""Tools page markup tests (workstream 2: design-system pass over /tools).

The page renders its own template with Title Case labels via field_label(),
real <details class="disclosure"> add expanders, .btn button classes, and
section headers with plain-language blurbs. No network, no real models.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient

from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.dashboard import routes_settings
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings
from jev_agent.workers.openrouter_models import ModelCatalog, ModelInfo


@pytest.fixture(autouse=True)
def _fake_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


def _tools_html(client: TestClient) -> str:
    page = client.get("/tools")
    assert page.status_code == 200
    text: str = page.text
    return text


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/tools").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str]) -> httpx.Response:
    response = cast(
        httpx.Response,
        client.post(url, data={**data, "csrf_token": _csrf(client)}, follow_redirects=False),
    )
    return response


def test_tools_sections_have_headers_and_blurbs(client: TestClient) -> None:
    html = _tools_html(client)
    assert 'id="s-tools-mcp"' in html
    assert 'id="s-tools-skills"' in html
    assert 'id="s-tools-plugins"' in html
    assert ">MCP servers</h2>" in html
    assert ">Skills</h2>" in html
    assert ">Plugins (Claude Code only)</h2>" in html
    assert 'aria-label="Tools sections"' in html


def test_tools_add_expandable_is_disclosure_button(client: TestClient) -> None:
    html = _tools_html(client)
    assert html.count('<details class="disclosure">') == 3
    assert "Add to Tools: MCP servers" in html
    assert "Add to Tools: skills" in html
    assert "Add to Tools: plugins (Claude Code only)" in html
    # The legacy text-link expander is gone from this page.
    assert 'class="entity add-entity"' not in html


def test_tools_labels_are_title_case(client: TestClient) -> None:
    html = _tools_html(client)
    for label in (
        "Name",
        "Save to",
        "Command",
        "Args",
        "Env from",
        "URL",
        "Transport",
        "Bearer token env",
        "Path",
        "Allowed tools",
    ):
        assert f">{label}</label>" in html, label
    # No raw config keys as visible labels in the add forms.
    for raw in (
        ">name</label>",
        ">save to</label>",
        ">env_from</label>",
        ">bearer_token_env</label>",
    ):
        assert raw not in html, raw


def test_tools_add_buttons_use_primary_style(client: TestClient) -> None:
    html = _tools_html(client)
    assert html.count('<button class="btn btn-primary" type="submit">Add</button>') == 3


def test_tools_entry_rows_use_title_case_labels_and_btn_classes(client: TestClient) -> None:
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
    html = _tools_html(client)
    assert ">Command</label>" in html
    assert ">Env from</label>" in html
    assert '<button class="btn btn-sm btn-secondary" type="submit">Save</button>' in html
    assert "Remove github" in html


# ---- cycle 4 (issue #251): the model catalog refresh used to be CLI-only ------


def test_models_refresh_button_present(client: TestClient) -> None:
    html = _tools_html(client)
    assert "/tools/models/refresh" in html
    assert "Fetch the model list" in html  # the empty state: no catalog yet


def test_models_refresh_success(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = ModelCatalog(
        models=[ModelInfo(id="a/b"), ModelInfo(id="c/d")],
        fetched_at=time.time(),
    )
    monkeypatch.setattr(
        routes_settings, "fetch_catalog", lambda root, config, refresh: (catalog, None)
    )
    response = _post(client, "/tools/models/refresh", {})
    assert response.status_code == 303
    assert "refreshed the model catalog (2 models)" in client.get("/tools").text


def test_models_refresh_failure_is_a_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_settings,
        "fetch_catalog",
        lambda root, config, refresh: (
            None,
            "cannot fetch https://example.test/models: boom; no cached model list",
        ),
    )
    response = _post(client, "/tools/models/refresh", {})
    assert response.status_code == 303
    assert "cannot fetch https://example.test/models" in client.get("/tools").text


# ---- issue #254 workstream A: the Cache section ----------------------------------


def _seed_cache(repo: Path, decisions: int = 2, roles: int = 1, hits: int = 2) -> None:
    from jev_agent.dashboard import views

    with views.open_repo(repo) as r:
        for i in range(decisions):
            key = f"dec{i}"
            r.store.cache_put(key, "decision", '{"run_id": "r"}', "{}", 0.5, 168)
            for _ in range(hits):
                r.store.cache_get(key)
        for i in range(roles):
            key = f"role{i}"
            r.store.cache_put(key, "role", '{"run_id": "r"}', "{}", 0.25, 168)
            for _ in range(hits):
                r.store.cache_get(key)


def _seed_decisions(repo: Path, answers: list[str]) -> None:
    from jev_agent.core.models import Decision, DecisionRequest, Idea
    from jev_agent.dashboard import views

    with views.open_repo(repo) as r:
        run_id = r.store.create_run(Idea(text="add search", repo_path=repo)).id
        for answer in answers:
            r.store.record_decision(
                run_id,
                None,
                DecisionRequest(
                    point="D1",
                    state={},
                    questions={"action": ["plan", "ask"]},
                ),
                Decision(
                    point="D1",
                    answers={"action": answer},
                    dispatcher="consensus",
                ),
            )


def test_cache_section_renders_stats(client: TestClient, fixture_repo: Path) -> None:
    _seed_cache(fixture_repo)
    html = _tools_html(client)
    assert 'id="cache"' in html
    assert "3 (2 decisions, 1 roles)" in html
    assert "Hits served" in html
    assert "$2.50" in html
    assert "Semantic mode" in html
    assert "/tools/cache/clear" in html
    assert "/tools/cache/calibrate" in html


def test_cache_section_empty_state(client: TestClient) -> None:
    html = _tools_html(client)
    assert "No entries yet" in html
    assert "Misses aren" in html  # the honest note: no hit rate without miss counts


def test_cache_clear_two_step(client: TestClient, fixture_repo: Path) -> None:
    _seed_cache(fixture_repo)
    confirm = _post(client, "/tools/cache/clear", {"kind": "decisions"})
    assert confirm.status_code == 200
    assert "Clear the cache?" in confirm.text
    assert "2 entries" in confirm.text
    assert "decisions only" in confirm.text
    cleared = _post(client, "/tools/cache/clear", {"kind": "decisions", "confirm": "yes"})
    assert cleared.status_code == 303
    html = client.get("/tools").text
    assert "cleared 2 cache entries" in html
    assert "1 (0 decisions, 1 roles)" in html  # the role entry survives


def test_cache_clear_older_than_filter(client: TestClient, fixture_repo: Path) -> None:
    _seed_cache(fixture_repo)
    nothing = _post(client, "/tools/cache/clear", {"older_than": "10000"})
    assert nothing.status_code == 303
    assert "already empty for those filters" in client.get("/tools").text
    confirm = _post(client, "/tools/cache/clear", {"older_than": "0"})
    assert confirm.status_code == 200
    assert "3 entries" in confirm.text
    cleared = _post(client, "/tools/cache/clear", {"older_than": "0", "confirm": "yes"})
    assert cleared.status_code == 303
    assert "cleared 3 cache entries" in client.get("/tools").text


def test_cache_clear_bogus_kind_is_an_error(client: TestClient, fixture_repo: Path) -> None:
    _seed_cache(fixture_repo)
    response = _post(client, "/tools/cache/clear", {"kind": "bogus"})
    assert response.status_code == 303
    # Quotes are HTML-escaped in the flash.
    assert "must be &#34;decisions&#34; or &#34;roles&#34;" in client.get("/tools").text
    assert "3 (2 decisions, 1 roles)" in client.get("/tools").text


def test_cache_clear_bad_older_than_is_an_error(client: TestClient) -> None:
    for bad in ("abc", "-1"):
        response = _post(client, "/tools/cache/clear", {"older_than": bad})
        assert response.status_code == 303
        assert "older-than hours" in client.get("/tools").text


def test_cache_calibrate_empty_history(client: TestClient) -> None:
    response = _post(client, "/tools/cache/calibrate", {})
    assert response.status_code == 200
    assert "Cache calibration" in response.text
    assert "Not enough decision history" in response.text


def test_cache_calibrate_candidate_and_apply(client: TestClient, fixture_repo: Path) -> None:
    _seed_decisions(fixture_repo, ["plan", "plan"])
    response = _post(client, "/tools/cache/calibrate", {})
    assert response.status_code == 200
    assert "Candidate:" in response.text
    assert "0.99" in response.text
    assert "/tools/cache/apply-threshold" in response.text
    applied = _post(
        client,
        "/tools/cache/apply-threshold",
        {"threshold": "0.99", "target": "repo"},
    )
    assert applied.status_code == 303
    html = client.get("/tools").text
    assert "set cache.semantic.decision_threshold to 0.99 (repo)" in html
    assert "decision_threshold = 0.99" in (fixture_repo / "jev.toml").read_text()


def test_cache_calibrate_no_candidate(client: TestClient, fixture_repo: Path) -> None:
    _seed_decisions(fixture_repo, ["plan", "plan", "ask"])
    response = _post(client, "/tools/cache/calibrate", {})
    assert response.status_code == 200
    assert "No threshold reaches" in response.text
    assert "/tools/cache/apply-threshold" not in response.text


def test_cache_apply_threshold_invalid(client: TestClient, fixture_repo: Path) -> None:
    response = _post(client, "/tools/cache/apply-threshold", {"threshold": "abc", "target": "repo"})
    assert response.status_code == 303
    assert "threshold must be a number between 0 and 1" in client.get("/tools").text
    for bad in ("1.5", "-0.1"):
        response = _post(
            client, "/tools/cache/apply-threshold", {"threshold": bad, "target": "repo"}
        )
        assert response.status_code == 303
        assert "threshold must be between 0 and 1" in client.get("/tools").text
    assert not (fixture_repo / "jev.toml").exists()
