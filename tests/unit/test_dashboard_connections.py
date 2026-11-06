"""The connection status panel: the sidebar slot, `/connections/live`, the full panel page
and the explicit refresh. No network: the ssh runner is faked, and the audit log is the
only state the GET endpoints read."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient

from jev_agent.core import provider_auth as pa
from jev_agent.core.usage import UsageWindow
from jev_agent.core.usage.cache import UsageCacheRecord
from jev_agent.core.usage.fetcher import UsageFetcher
from jev_agent.dashboard import app as dashboard_app
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings
from jev_agent.servers.audit import AuditEntry, AuditLog, audit_path
from jev_agent.servers.ssh import SshResult

SERVERS_TOML = """
[servers.alpha]
host = "alpha.example"
access = "medium"

[servers.beta]
host = "beta.example"
"""

SECRET = "sk-test-secret-value-123"


@pytest.fixture
def _trust_all_repos(tmp_path: Path) -> None:
    """`servers` is a guarded key: a repo's jev.toml may only set it when trusted."""
    user_config = tmp_path / "xdg" / "jev" / "config.toml"
    user_config.write_text(user_config.read_text() + '\n[trust]\nrepos = ["*"]\n')


@pytest.fixture
def repo_with_servers(fixture_repo: Path, _trust_all_repos: None) -> Path:
    (fixture_repo / "jev.toml").write_text(SERVERS_TOML)
    return fixture_repo


@pytest.fixture
def client(repo_with_servers: Path) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=repo_with_servers, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/chats").text)
    assert match
    return match.group(1)


def _audit(repo: Path) -> AuditLog:
    return AuditLog(audit_path(repo / ".jev"))


def _connect_entry(server: str, exit_code: int) -> AuditEntry:
    return AuditEntry(
        run_id=None,
        role="doctor",
        server=server,
        access="medium",
        action="connect",
        command="true",
        allowed=True,
        exit_code=exit_code,
        duration_s=0.5,
    )


def _fake_ssh_runner(argv: Sequence[str], *, stdin: bytes | None, timeout_s: float) -> SshResult:
    """alpha answers, beta refuses: reachability without a network."""
    del stdin, timeout_s
    code = 0 if argv[-2] == "alpha.example" else 255  # argv ends with [host, command]
    return SshResult(exit_code=code, stdout=b"", stderr=b"", duration_s=0.05)


def test_sidebar_slot_is_live_wired(client: TestClient) -> None:
    html = client.get("/").text
    assert 'id="conn-status"' in html
    assert 'data-conn-live="/connections/live"' in html
    assert 'src="/static/conn.js"' in html


def test_live_endpoint_serves_cached_state(client: TestClient, repo_with_servers: Path) -> None:
    response = client.get("/connections/live")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["total"] == 2
    assert body["unknown"] == 2
    assert body["state"] == "degraded"
    assert body["label"] == "2 not checked"
    assert [s["name"] for s in body["servers"]] == ["alpha", "beta"]
    # A GET never checks: no audit directory appears as a side effect.
    assert not (repo_with_servers / ".jev" / "audit").exists()


def test_live_endpoint_reflects_audit_log(client: TestClient, repo_with_servers: Path) -> None:
    log = _audit(repo_with_servers)
    log.write(_connect_entry("alpha", 0))
    log.write(_connect_entry("beta", 255))
    log.write(_connect_entry("alpha", 0))  # the latest entry per server wins
    body = client.get("/connections/live").json()
    states = {s["name"]: s["state"] for s in body["servers"]}
    assert states == {"alpha": "reachable", "beta": "unreachable"}
    assert body["reachable"] == 1 and body["unreachable"] == 1
    assert body["state"] == "degraded"
    assert body["label"] == "1 unreachable"
    alpha = next(s for s in body["servers"] if s["name"] == "alpha")
    assert alpha["last_check"]  # an ISO timestamp, not a secret


def test_live_endpoint_never_exposes_key_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    text = client.get("/connections/live").text
    assert SECRET not in text
    assert "TYPESAFE_API_KEY" in text or "servers" in text  # names are fine, values are not


def test_connections_page_is_the_full_panel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    html = client.get("/connections").text
    assert "Server reachability" in html
    assert "alpha" in html and "beta" in html
    assert "Decision route" in html
    assert "Keys" in html
    # Access tiers, the rules route and key names are shown; key values never are.
    assert "medium" in html and "basic" in html
    assert "Rules dispatcher" in html
    assert "OPENROUTER_API_KEY" in html
    assert SECRET not in html
    assert 'action="/connections/refresh"' in html


def test_connections_page_consensus_route(
    fixture_repo: Path, _trust_all_repos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fixture_repo / "jev.toml").write_text(SERVERS_TOML + '\n[dispatcher]\nprimary = "consensus"\n')
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        html = client.get("/connections").text
    # The consensus dispatcher is gone (issue #193): an old `primary = "consensus"`
    # config migrates to jev, so the page shows the Jev route with rules fallback.
    assert "Laya" not in html
    assert "Rules dispatcher" in html
    assert "TYPESAFE_API_KEY: set" in html
    assert "OPENROUTER_API_KEY: not set" in html
    assert SECRET not in html


def test_connections_page_no_servers(fixture_repo: Path) -> None:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        html = client.get("/connections").text
        body = client.get("/connections/live").json()
    assert "No servers configured" in html
    # Cycle 3 (issue #186): the empty state is a CTA to the Configuration
    # section on this page, where the "Add to Servers" form lives.
    assert "Add a server in Configuration" in html and "#s-servers" in html
    assert body["total"] == 0
    assert body["state"] == "ok"
    assert body["label"] == "Live"


def test_refresh_post_checks_servers_and_audits(
    client: TestClient, repo_with_servers: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dashboard_app, "ssh_runner", _fake_ssh_runner)
    response = client.post(
        "/connections/refresh",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/connections"
    entries = [e for e in _audit(repo_with_servers).read() if e["action"] == "connect"]
    assert {e["server"] for e in entries} == {"alpha", "beta"}
    assert {e["server"]: e["exit_code"] for e in entries} == {"alpha": 0, "beta": 255}
    body = client.get("/connections/live").json()
    states = {s["name"]: s["state"] for s in body["servers"]}
    assert states == {"alpha": "reachable", "beta": "unreachable"}
    assert body["label"] == "1 unreachable"


def test_refresh_needs_post_and_csrf(client: TestClient) -> None:
    assert client.get("/connections/refresh").status_code == 405
    assert client.post("/connections/refresh", data={}).status_code == 403


def test_audit5_polish_css_rules(client: TestClient) -> None:
    """Audit 5 (issue #269): the provider key cards and inline forms keep
    their layout inside narrow containers."""
    css = client.get("/static/style.css").text
    # Label-less field rows (provider key cards, token/code/model forms) are
    # control + actions only: two columns, not the settings 3-column grid
    # whose 200px minimums pushed "Add key" out of the card.
    assert ".field-row:not(:has(> label))" in css
    assert "grid-template-columns: minmax(0, 1fr) auto" in css
    # Sign-in hints read as their own line under the button.
    assert ".provider form > span.hint" in css
    # Inline-form labels keep their natural width (run page bisect finder).
    assert "form.inline label" in css


# --- provider usage in the live poll (issue #396 Phase A) ------------------------
#
# The poll JSON gains the additive `usage` key nested inside each
# `providers[]` entry. The dashboard's fetcher is replaced with a temp-home
# fetcher (fake adapter, planted Codex `tokens` login); no real CLI.


class _UsageAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self) -> Any:
        self.calls += 1
        return None

    def shutdown(self) -> None:
        pass


def test_live_endpoint_carries_provider_usage(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "usage-home"
    auth = home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(
        json.dumps({"tokens": {"access_token": "fake", "refresh_token": "fake"}}),
        encoding="utf-8",
    )
    for name in (
        pa.OPENAI_API_KEY_ENV,
        pa.ANTHROPIC_API_KEY_ENV,
        pa.CLAUDE_OAUTH_TOKEN_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    adapter = _UsageAdapter()
    fetcher = UsageFetcher(home=home, env={}, adapters={"codex": adapter})
    cast(Any, client.app).state.dashboard.usage_fetcher = fetcher
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
    body = client.get("/connections/live").json()
    assert body["ok"] is True
    by_name = {p["provider"]: p for p in body["providers"]}
    codex = by_name["codex"]["usage"]
    # The additive usage key: state, windows (left percent integer,
    # half up), timestamps ISO-8601 UTC with a Z suffix, reason, plan.
    assert codex["state"] == "ready"
    assert len(codex["windows"]) == 1
    window = codex["windows"][0]
    assert window["kind"] == "monthly"
    assert window["left_percent"] == 52  # 100 - 48 used, integer, half up
    assert isinstance(window["left_percent"], int)
    assert window["resets_at"].endswith("Z")
    reset = datetime.fromisoformat(window["resets_at"].replace("Z", "+00:00"))
    assert abs((reset - (now + timedelta(days=20))).total_seconds()) < 5
    assert codex["fetched_at"].endswith("Z")
    fetched = datetime.fromisoformat(codex["fetched_at"].replace("Z", "+00:00"))
    assert abs((fetched - now).total_seconds()) < 5
    assert codex["reason"] is None
    assert codex["plan"] == "free"
    # Claude (no adapter in Phase A, no login here) reads as never checked.
    claude = by_name["claude_code"]["usage"]
    assert claude["state"] == "never"
    assert claude["windows"] == []
    assert claude["fetched_at"] is None
    assert claude["reason"] == "no_login"
    assert claude["plan"] is None
    # The poll never triggers a fetch: the fresh cache means zero calls.
    assert adapter.calls == 0
