"""The Connections page's provider cards: Claude Code + Codex subscription login.

No real CLIs, browsers, or credentials: the Codex device flow is faked at the
class level, the secrets file is isolated, and tokens are obviously-fake."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient

from jev_agent.core import provider_auth as pa
from jev_agent.core import secrets
from jev_agent.core.provider_auth import CodexDeviceFlow, DeviceFlowStatus
from jev_agent.core.usage import UsageSnapshot, UsageWindow
from jev_agent.core.usage.cache import UsageCacheRecord
from jev_agent.core.usage.fetcher import UsageFetcher
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
    # Issue #396 (D1): a token saved by Baton names itself, distinct from
    # an env-provided token ("Subscription login (OAuth)").
    assert "Signed in with your Claude subscription (saved by Baton)" in page
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


# --- provider usage (issue #396 Phase A) ---------------------------------------------
#
# The dashboard's own fetcher is replaced with a temp-home fetcher whose
# adapter is fake; the card detection sees a planted Codex `tokens` login
# via a temp HOME. No real CLI, no network, no token values anywhere.

FAKE_ACCESS_TOKEN = "fake-access-token-value"
FAKE_REFRESH_TOKEN = "fake-refresh-token-value"


class _UsageAdapter:
    """A fake usage source: returns a fixed outcome, records its calls."""

    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return self.outcome

    def shutdown(self) -> None:
        pass


def _plant_codex_login(home: Path) -> None:
    auth = home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": FAKE_ACCESS_TOKEN,
                    "refresh_token": FAKE_REFRESH_TOKEN,
                }
            }
        ),
        encoding="utf-8",
    )


def _snapshot_now() -> UsageSnapshot:
    now = datetime.now(UTC)
    return UsageSnapshot(
        provider="codex",
        windows=[
            UsageWindow(kind="monthly", used_percent=48.0, resets_at=now + timedelta(days=20))
        ],
        plan="free",
        fetched_at=now,
        source_version="cli/0.167.0 parser/1",
        account_hash="aaa",
        spend_limit_reached=False,
    )


def _usage_fetcher(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: _UsageAdapter
) -> UsageFetcher:
    """Point the dashboard at a temp home with a planted Codex login and a
    fake adapter; return the replacement fetcher (its cache is the test's)."""
    home = tmp_path / "usage-home"
    _plant_codex_login(home)
    for name in (
        pa.OPENAI_API_KEY_ENV,
        pa.ANTHROPIC_API_KEY_ENV,
        pa.CLAUDE_OAUTH_TOKEN_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    fetcher = UsageFetcher(home=home, env={}, adapters={"codex": adapter})
    client.app.state.dashboard.usage_fetcher = fetcher
    return fetcher


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_dashboard_creates_usage_fetcher(client: TestClient) -> None:
    fetcher = client.app.state.dashboard.usage_fetcher
    assert isinstance(fetcher, UsageFetcher)
    assert "codex" in fetcher.adapters  # Phase A: Codex only
    assert "claude_code" not in fetcher.adapters  # Phase B
    assert fetcher.is_running("codex") is False


def test_usage_block_renders_cached_snapshot_and_no_fetch_on_render(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _UsageAdapter(_snapshot_now())
    fetcher = _usage_fetcher(client, tmp_path, monkeypatch, adapter)
    now = datetime.now(UTC)
    fetcher.cache.write(
        "codex",
        UsageCacheRecord(
            enabled=True,
            fetched_at=now,
            last_attempt_at=now,
            last_status="ok",
            source_version="cli/0.167.0 parser/1",
            account_hash="aaa",
            plan="free",
            windows=[
                UsageWindow(kind="monthly", used_percent=48.0, resets_at=now + timedelta(days=20))
            ],
        ),
    )
    html = client.get("/connections").text
    # The cached snapshot renders (the render never waits for a fetch, and
    # the fresh cache means the render starts none either).
    assert 'data-provider-usage="codex"' in html
    assert "Monthly 52% left" in html  # the line shows what is LEFT, not used
    assert "Plan: free" in html
    assert "Your plan has no separate 5-hour or weekly limit — monthly only" in html
    assert "Refresh usage" in html
    assert adapter.calls == 0
    assert FAKE_ACCESS_TOKEN not in html
    assert FAKE_REFRESH_TOKEN not in html


def test_usage_first_run_not_checked_yet(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _UsageAdapter(_snapshot_now())
    _usage_fetcher(client, tmp_path, monkeypatch, adapter)
    html = client.get("/connections").text
    assert 'data-provider-usage="codex"' in html
    assert "Usage not checked yet" in html
    assert "Check usage" in html
    assert adapter.calls == 0  # not opted in: the render starts no fetch


def test_usage_refresh_route_starts_fetch_then_too_soon(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _UsageAdapter(_snapshot_now())
    fetcher = _usage_fetcher(client, tmp_path, monkeypatch, adapter)
    response = _post(client, "/connections/providers/codex/usage/refresh", {})
    assert response.status_code == 303
    page = client.get("/connections").text
    assert "Checking usage — the numbers will appear when the check finishes." in page
    assert _wait_for(lambda: adapter.calls == 1)
    assert _wait_for(
        lambda: (fetcher.cache.read("codex") or UsageCacheRecord()).last_status == "ok"
    )
    # Inside the 5-minute floor a second click is too soon, with the wait.
    response = _post(client, "/connections/providers/codex/usage/refresh", {})
    assert response.status_code == 303
    page = client.get("/connections").text
    assert "Already checked recently — try again in " in page
    assert "min" in page
    assert adapter.calls == 1


def test_usage_disable_route(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _UsageAdapter(_snapshot_now())
    fetcher = _usage_fetcher(client, tmp_path, monkeypatch, adapter)
    response = _post(client, "/connections/providers/codex/usage/disable", {})
    assert response.status_code == 303
    page = client.get("/connections").text
    assert "Usage checks turned off for Codex." in page
    assert "Usage checks are off for this provider." in page
    assert fetcher.cache.read("codex").enabled is False  # type: ignore[union-attr]


def test_usage_refresh_and_disable_404_for_claude_code(
    client: TestClient,
) -> None:
    # Phase A: `claude_code` has no registered adapter, so both routes 404
    # until Phase B.
    for url in (
        "/connections/providers/claude_code/usage/refresh",
        "/connections/providers/claude_code/usage/disable",
    ):
        response = client.post(
            url, data={"csrf_token": _csrf(client)}, follow_redirects=False
        )
        assert response.status_code == 404


def test_claude_card_has_no_usage_block_and_file_only_sentence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(pa.CLAUDE_OAUTH_TOKEN_ENV, raising=False)
    monkeypatch.delenv(pa.ANTHROPIC_API_KEY_ENV, raising=False)
    html = client.get("/connections").text
    assert 'data-provider-usage="claude_code"' not in html
    # Phase A stopgap (§6.5 `no_login`): a Keychain-only login is not
    # detected yet, and the card says so.
    assert (
        "This phase detects file-stored logins only — a login in the Mac's "
        "Keychain is not detected yet." in html
    )


def test_usage_kill_switch(
    client: TestClient,
    fixture_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _UsageAdapter(_snapshot_now())
    _usage_fetcher(client, tmp_path, monkeypatch, adapter)
    (fixture_repo / "jev.toml").write_text("[usage]\ncodex = false\n")
    html = client.get("/connections").text
    assert "Usage is turned off for this provider" in html
    assert "Enable it in `jev.toml` and restart the dashboard" in html
    assert adapter.calls == 0  # the kill switch stops every fetch
    # The login state itself is unchanged by the kill switch.
    assert "Detected on this device" in html


def test_usage_api_key_hides_section(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _UsageAdapter(_snapshot_now())
    _usage_fetcher(client, tmp_path, monkeypatch, adapter)
    monkeypatch.setenv(pa.OPENAI_API_KEY_ENV, "sk-decoy")
    html = client.get("/connections").text
    # D12: an API key hides the usage section.
    assert 'data-provider-usage="codex"' not in html
    assert "Billed per token — no subscription windows apply to an API key." in html
    assert adapter.calls == 0


def test_usage_no_token_value_in_cache_file(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _UsageAdapter(_snapshot_now())
    fetcher = _usage_fetcher(client, tmp_path, monkeypatch, adapter)
    _post(client, "/connections/providers/codex/usage/refresh", {})
    assert _wait_for(lambda: adapter.calls == 1)
    assert _wait_for(
        lambda: (fetcher.cache.read("codex") or UsageCacheRecord()).last_status == "ok"
    )
    cache_file = tmp_path / "usage-home" / ".jev" / "usage-cache-codex.json"
    content = cache_file.read_text(encoding="utf-8")
    assert FAKE_ACCESS_TOKEN not in content
    assert FAKE_REFRESH_TOKEN not in content
    # Only the account hash (never the token) is stored.
    assert "aaa" in content
