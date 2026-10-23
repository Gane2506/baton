"""The Connections page's provider cards: Claude Code + Codex subscription login.

No real CLIs, browsers, or credentials: the Codex device flow is faked at the
class level, the secrets file is isolated, and tokens are obviously-fake."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from starlette.testclient import TestClient

from jev_agent.core import provider_auth as pa
from jev_agent.core import secrets
from jev_agent.core.provider_auth import CodexDeviceFlow, DeviceFlowStatus
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings

FAKE_TOKEN = "sk-ant-oat01-fake-token-for-test"


@pytest.fixture
def sfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated secrets file; the module's process-level `_loaded` set is reset."""
    file = tmp_path / ".jev" / "secrets.env"
    monkeypatch.setenv(secrets.FILE_ENV, str(file))
    secrets._loaded.clear()
    yield file
    secrets._loaded.discard(pa.CLAUDE_OAUTH_TOKEN_ENV)
    os.environ.pop(pa.CLAUDE_OAUTH_TOKEN_ENV, None)


@pytest.fixture
def client(fixture_repo: Path, sfile: Path) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/chats").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str]) -> httpx.Response:
    response = client.post(url, data={**data, "csrf_token": _csrf(client)}, follow_redirects=False)
    return cast(httpx.Response, response)


# --- the provider cards ---------------------------------------------------------


def test_connections_page_lists_both_providers(client: TestClient) -> None:
    html = client.get("/connections").text
    assert "Claude Code" in html
    assert "Codex" in html
    assert "claude setup-token" in html
    assert "Sign in with ChatGPT" in html
    assert 'action="/connections/providers/claude/oauth-token"' in html
    assert 'data-provider-auth="claude_code"' in html
    assert 'data-provider-auth="codex"' in html


def test_key_datalist_suggests_provider_vars(client: TestClient) -> None:
    html = client.get("/connections").text
    assert f'<option value="{pa.CLAUDE_OAUTH_TOKEN_ENV}">' in html
    assert f'<option value="{pa.ANTHROPIC_API_KEY_ENV}">' in html
    assert f'<option value="{pa.OPENAI_API_KEY_ENV}">' in html


# --- Claude subscription token --------------------------------------------------


def test_save_claude_oauth_token(client: TestClient, sfile: Path) -> None:
    response = _post(client, "/connections/providers/claude/oauth-token", {"value": FAKE_TOKEN})
    assert response.status_code == 303
    page = client.get("/connections").text
    assert "Subscription login (OAuth)" in page
    assert FAKE_TOKEN not in page  # the value is never rendered back
    assert FAKE_TOKEN not in client.get("/connections/live").text
    assert sfile.read_text(encoding="utf-8") == f"{pa.CLAUDE_OAUTH_TOKEN_ENV}={FAKE_TOKEN}\n"
    assert (sfile.stat().st_mode & 0o777) == 0o600
    # In effect immediately: the worker's environment sees it.
    assert os.environ[pa.CLAUDE_OAUTH_TOKEN_ENV] == FAKE_TOKEN


def test_save_claude_oauth_token_rejects_wrong_shape(client: TestClient, sfile: Path) -> None:
    response = client.post(
        "/connections/providers/claude/oauth-token",
        data={"value": "sk-ant-api03-not-an-oauth-token", "csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "doesn&#39;t look like" in response.text
    assert "sk-ant-oat01-" in response.text  # the hint names the shape, not the value
    assert not sfile.exists()
    assert pa.CLAUDE_OAUTH_TOKEN_ENV not in os.environ


def test_claude_card_offers_replace_when_connected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(pa.CLAUDE_OAUTH_TOKEN_ENV, FAKE_TOKEN)
    html = client.get("/connections").text
    # Established login: just a Set badge, no buttons or forms (disappear-when-done).
    assert "Signed in with your Claude subscription" in html
    assert "Sign in with Claude" not in html
    assert "Replace the saved token" not in html
    assert FAKE_TOKEN not in html


# --- Codex device flow ------------------------------------------------------------


@pytest.fixture
def waiting_flow(monkeypatch: pytest.MonkeyPatch) -> DeviceFlowStatus:
    """The dashboard's device flow, faked at the class level: already waiting."""

    def fake_start(
        self: CodexDeviceFlow, command: str = "codex", home: Path | None = None
    ) -> DeviceFlowStatus:
        return fake_status(self)

    def fake_status(self: CodexDeviceFlow) -> DeviceFlowStatus:
        return DeviceFlowStatus(
            state="waiting",
            url=pa.CODEX_DEVICE_URL,
            code="ABCD-12EFG",
            detail="open the link in your browser and enter the code",
        )

    monkeypatch.setattr(CodexDeviceFlow, "start", fake_start)
    monkeypatch.setattr(CodexDeviceFlow, "status", fake_status)
    return fake_status(None)  # type: ignore[arg-type]


def test_codex_device_start_shows_url_and_code(
    client: TestClient, waiting_flow: DeviceFlowStatus
) -> None:
    response = _post(client, "/connections/providers/codex/device/start", {})
    assert response.status_code == 303
    page = client.get("/connections").text
    assert pa.CODEX_DEVICE_URL in page
    assert "ABCD-12EFG" in page
    assert "waiting for browser approval" in page
    assert 'action="/connections/providers/codex/device/cancel"' in page


def test_codex_device_cancel(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled: list[bool] = []

    def fake_cancel(self: CodexDeviceFlow) -> bool:
        cancelled.append(True)
        return True

    monkeypatch.setattr(CodexDeviceFlow, "cancel", fake_cancel)
    monkeypatch.setattr(
        CodexDeviceFlow,
        "status",
        lambda self: DeviceFlowStatus(state="idle"),
    )
    response = _post(client, "/connections/providers/codex/device/cancel", {})
    assert response.status_code == 303
    assert cancelled == [True]


def test_codex_device_start_without_cli_flashes_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    response = client.post(
        "/connections/providers/codex/device/start",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "not on PATH" in response.text


# --- Claude setup-token flow ------------------------------------------------------


@pytest.fixture
def claude_waiting_flow(monkeypatch: pytest.MonkeyPatch) -> pa.ClaudeSetupTokenStatus:
    """The dashboard's Claude flow, faked at the class level: already waiting."""

    def fake_start(
        self: pa.ClaudeSetupTokenFlow, command: str = "claude", timeout_s: float = 600.0
    ) -> pa.ClaudeSetupTokenStatus:
        return fake_status(self)

    def fake_status(self: pa.ClaudeSetupTokenFlow) -> pa.ClaudeSetupTokenStatus:
        return pa.ClaudeSetupTokenStatus(
            state="waiting",
            url="https://claude.ai/oauth/authorize?code=true",
            code_prompt=True,
            detail="open the link in your browser",
        )

    monkeypatch.setattr(pa.ClaudeSetupTokenFlow, "start", fake_start)
    monkeypatch.setattr(pa.ClaudeSetupTokenFlow, "status", fake_status)
    return fake_status(None)  # type: ignore[arg-type]


def test_claude_setup_token_start_shows_url(
    client: TestClient, claude_waiting_flow: pa.ClaudeSetupTokenStatus
) -> None:
    response = _post(client, "/connections/providers/claude/setup-token/start", {})
    assert response.status_code == 303
    page = client.get("/connections").text
    assert "https://claude.ai/oauth/authorize?code=true" in page
    assert "waiting for browser approval" in page
    assert 'action="/connections/providers/claude/setup-token/cancel"' in page
    assert 'action="/connections/providers/claude/setup-token/code"' in page


def test_claude_setup_token_cancel(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled: list[bool] = []

    def fake_cancel(self: pa.ClaudeSetupTokenFlow) -> bool:
        cancelled.append(True)
        return True

    monkeypatch.setattr(pa.ClaudeSetupTokenFlow, "cancel", fake_cancel)
    monkeypatch.setattr(
        pa.ClaudeSetupTokenFlow,
        "status",
        lambda self: pa.ClaudeSetupTokenStatus(state="idle"),
    )
    response = _post(client, "/connections/providers/claude/setup-token/cancel", {})
    assert response.status_code == 303
    assert cancelled == [True]


def test_claude_setup_token_code_forwards(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[str] = []

    def fake_submit(self: pa.ClaudeSetupTokenFlow, code: str) -> bool:
        submitted.append(code)
        return True

    monkeypatch.setattr(pa.ClaudeSetupTokenFlow, "submit_code", fake_submit)
    response = _post(
        client, "/connections/providers/claude/setup-token/code", {"code": "browser-123"}
    )
    assert response.status_code == 303
    assert submitted == ["browser-123"]


def test_claude_setup_token_start_without_cli_flashes_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    response = client.post(
        "/connections/providers/claude/setup-token/start",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "not on PATH" in response.text


# --- live endpoint -----------------------------------------------------------------


def test_live_endpoint_includes_providers_without_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(pa.CLAUDE_OAUTH_TOKEN_ENV, FAKE_TOKEN)
    body = client.get("/connections/live").json()
    assert body["ok"] is True
    by_name = {p["provider"]: p for p in body["providers"]}
    assert by_name["claude_code"]["auth"] == "oauth_token"
    assert by_name["codex"]["auth"] == "none"
    assert FAKE_TOKEN not in client.get("/connections/live").text
    assert body["codex_flow"]["state"] == "idle"
