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
from typing import ClassVar, cast

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
    assert html.count('<details class="disclosure">') == 4
    assert "Add to Tools: MCP servers" in html
    assert "Add to Tools: skills" in html
    assert "Add to Tools: plugins (Claude Code only)" in html
    assert "Connect an API" in html
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


def test_cache_calibrate_get_redirects_to_tools(client: TestClient) -> None:
    """Refreshing the calibration result (GET, issue #316) goes back to Tools, never a 405."""
    response = client.get("/tools/cache/calibrate", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/tools#cache"


def test_cache_clear_get_redirects_to_tools(client: TestClient) -> None:
    """Refreshing the cache-clear confirmation (GET, issue #316) goes back to Tools, never a 405."""
    response = client.get("/tools/cache/clear", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/tools#cache"


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


# ---- issue #290: the APIs section --------------------------------------------


def _add_mcp_server(
    client: TestClient, name: str, target: str = "user", **fields: str
) -> httpx.Response:
    """Add an MCP server through the Connect-an-API form's fields."""
    return _post(
        client,
        "/settings/entity",
        {
            "section": "tools.mcp",
            "action": "add",
            "name": name,
            "target": target,
            "keys": "command,args,env_from,url,transport,bearer_token_env",
            "kinds": "str,list,list,str,enum,env",
            "return_to": "/tools#apis",
            **{f"f_{key}": value for key, value in fields.items()},
        },
    )


def test_apis_section_renders_empty_state(client: TestClient) -> None:
    html = _tools_html(client)
    assert 'id="apis"' in html
    assert ">APIs</h2>" in html
    assert "No APIs connected yet" in html
    assert "Connect an API" in html
    assert "/settings/entity" in html
    # The provider keys are visible with status, and unset ones offer Add.
    assert "Provider APIs" in html
    assert "OPENROUTER_API_KEY: not set" in html
    assert "/connections/keys/add" in html
    assert "manage or remove keys on" in html
    # The section nav links to it.
    assert 'href="#apis"' in html


def test_apis_section_lists_local_server_with_status(client: TestClient) -> None:
    import sys

    added = _add_mcp_server(client, "local-py", command=sys.executable)
    assert added.status_code == 303
    html = _tools_html(client)
    assert "local-py" in html
    assert "Local command" in html
    assert sys.executable in html
    assert ">ready<" in html or "ready" in html
    assert "command found" in html


def test_apis_section_local_server_missing_command_is_error(client: TestClient) -> None:
    added = _add_mcp_server(client, "ghost", command="definitely-not-a-real-command-xyz")
    assert added.status_code == 303
    html = _tools_html(client)
    assert "ghost" in html
    assert "command not found on PATH" in html


def test_apis_connect_remote_server(client: TestClient) -> None:
    added = _add_mcp_server(
        client,
        "remote-docs",
        url="https://example.test/mcp",
        transport="http",
        bearer_token_env="DOCS_TOKEN",
    )
    assert added.status_code == 303
    html = _tools_html(client)
    assert "remote-docs" in html
    assert "Remote URL" in html
    assert "https://example.test/mcp" in html
    assert "bearer: DOCS_TOKEN" in html
    assert "not tested" in html


def test_apis_connect_rejects_both_command_and_url(client: TestClient) -> None:
    added = _add_mcp_server(client, "both", command="npx", url="https://example.test/mcp")
    assert added.status_code == 303
    html = client.get("/tools").text
    assert "set exactly one of" in html
    assert "both" not in html.split('id="apis"')[1].split("</section>")[0]


def test_apis_test_local_command_found(client: TestClient) -> None:
    import sys

    _add_mcp_server(client, "local-py", command=sys.executable)
    response = _post(client, "/tools/apis/test", {"name": "local-py"})
    assert response.status_code == 303
    html = client.get("/tools").text
    assert "local-py: command found" in html


def test_apis_test_local_command_missing(client: TestClient) -> None:
    _add_mcp_server(client, "ghost", command="definitely-not-a-real-command-xyz")
    response = _post(client, "/tools/apis/test", {"name": "ghost"})
    assert response.status_code == 303
    html = client.get("/tools").text
    assert "ghost: command not found on PATH" in html


def test_apis_test_remote_mcp_handshake_passes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote server that answers the MCP initialize handshake passes (issue #315)."""
    _add_mcp_server(client, "remote-docs", url="https://example.test/mcp")

    body = b'{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}'

    class _FakeStream:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {}

        def __enter__(self) -> _FakeStream:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def iter_bytes(self) -> Iterator[bytes]:
            yield body

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: object) -> _FakeStream:
            assert method == "POST"
            assert url == "https://example.test/mcp"
            payload = kwargs["json"]
            assert isinstance(payload, dict)
            assert payload["method"] == "initialize"
            return _FakeStream()

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    response = _post(client, "/tools/apis/test", {"name": "remote-docs"})
    assert response.status_code == 303
    html = client.get("/tools").text
    assert "remote-docs: speaks MCP (initialize answered, protocol 2024-11-05)" in html
    # The row now shows the tested status instead of "not tested".
    assert html.count("not tested") == 0


def test_apis_test_remote_non_mcp_url_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-MCP URL that 404s must NOT pass (issue #315): the audit's example.com/mcp."""
    _add_mcp_server(client, "remote-docs", url="https://example.test/mcp")

    class _FakeStream:
        status_code = 404
        headers: ClassVar[dict[str, str]] = {}

        def __enter__(self) -> _FakeStream:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def iter_bytes(self) -> Iterator[bytes]:
            yield b"<html>not found</html>"

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: object) -> _FakeStream:
            assert method == "POST"
            return _FakeStream()

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    response = _post(client, "/tools/apis/test", {"name": "remote-docs"})
    assert response.status_code == 303
    html = client.get("/tools").text
    assert "remote-docs: answered HTTP 404" in html
    assert "not verified as an MCP server" in html
    assert "speaks MCP" not in html


def test_apis_test_remote_sse_transport(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy SSE endpoint that opens an event stream passes (issue #315)."""
    _add_mcp_server(client, "remote-sse", url="https://example.test/sse", transport="sse")

    class _FakeStream:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {"content-type": "text/event-stream"}

        def __enter__(self) -> _FakeStream:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def iter_bytes(self) -> Iterator[bytes]:
            yield b"event: endpoint\ndata: /message\n\n"

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: object) -> _FakeStream:
            assert method == "GET"
            assert url == "https://example.test/sse"
            return _FakeStream()

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    response = _post(client, "/tools/apis/test", {"name": "remote-sse"})
    assert response.status_code == 303
    html = client.get("/tools").text
    assert "remote-sse: opened an MCP event stream" in html


def test_apis_test_remote_sse_non_mcp_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SSE-configured URL that answers HTML instead of an event stream fails."""
    _add_mcp_server(client, "remote-sse", url="https://example.test/sse", transport="sse")

    class _FakeStream:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {"content-type": "text/html"}

        def __enter__(self) -> _FakeStream:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def iter_bytes(self) -> Iterator[bytes]:
            yield b"<html>hello</html>"

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: object) -> _FakeStream:
            return _FakeStream()

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    response = _post(client, "/tools/apis/test", {"name": "remote-sse"})
    assert response.status_code == 303
    html = client.get("/tools").text
    assert "remote-sse: answered HTTP 200" in html
    assert "not verified as an MCP server" in html


def test_apis_test_remote_unreachable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_mcp_server(client, "remote-docs", url="https://example.test/mcp")

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: object) -> object:
            raise httpx.ConnectError("connection refused", request=None)

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    response = _post(client, "/tools/apis/test", {"name": "remote-docs"})
    assert response.status_code == 303
    html = client.get("/tools").text
    assert "remote-docs:" in html
    assert "connection refused" in html.lower() or "could not" in html.lower()


def test_apis_test_unknown_server_is_an_error(client: TestClient) -> None:
    response = _post(client, "/tools/apis/test", {"name": "nope"})
    assert response.status_code == 303
    assert "no MCP server named" in client.get("/tools").text


def test_apis_remove_returns_to_apis(client: TestClient) -> None:
    import sys

    _add_mcp_server(client, "local-py", command=sys.executable)
    removed = _post(
        client,
        "/settings/entity",
        {
            "section": "tools.mcp",
            "action": "remove",
            "name": "local-py",
            "target": "user",
            "return_to": "/tools#apis",
        },
    )
    assert removed.status_code == 303
    html = _tools_html(client)
    assert "No APIs connected yet" in html


def test_provider_key_add_from_tools_returns_to_tools(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    from jev_agent.core import secrets

    file = tmp_path / ".jev" / "secrets.env"
    monkeypatch.setenv(secrets.FILE_ENV, str(file))
    secrets._loaded.clear()
    try:
        response = _post(
            client,
            "/connections/keys/add",
            {
                "name": "CYCLE3_TOOLS_KEY",
                "value": "fake-key",
                "return_to": "/tools#apis",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/tools#apis"
        assert "CYCLE3_TOOLS_KEY" in file.read_text()
        html = client.get("/tools").text
        assert "CYCLE3_TOOLS_KEY saved to the secrets file" in html
        assert "OPENROUTER_API_KEY: not set" in html  # other keys untouched
    finally:
        secrets._loaded.discard("CYCLE3_TOOLS_KEY")
        os.environ.pop("CYCLE3_TOOLS_KEY", None)


def test_provider_key_add_ignores_foreign_return_to(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    from jev_agent.core import secrets

    file = tmp_path / ".jev" / "secrets.env"
    monkeypatch.setenv(secrets.FILE_ENV, str(file))
    secrets._loaded.clear()
    try:
        response = _post(
            client,
            "/connections/keys/add",
            {
                "name": "CYCLE3_TOOLS_KEY",
                "value": "fake-key",
                "return_to": "https://evil.example/tools",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/connections"
    finally:
        secrets._loaded.discard("CYCLE3_TOOLS_KEY")
        os.environ.pop("CYCLE3_TOOLS_KEY", None)
