"""Dashboard health (issue #200, swarm review): one test per fixed error path.

- CSP blocks its own UI (copy buttons, stat bars, showcase)
- Wizard 500 on a skipped step (POST /stack with no prior app_type)
- Preview sessions never pruned (memory leak)
- Unhandled exceptions -> bare 500s (page() and act())
- Browser-install log unbounded and fully re-read per poll
- Minors: state-changing GET for the wizard start, runner job dicts,
  session-cookie Secure flag, secret temp-file permissions.
"""

from __future__ import annotations

import re
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

import jev_agent.dashboard.runner as runner_mod
from jev_agent.core.design_library import save_library_system
from jev_agent.dashboard import security as security_mod
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.browser_install import BrowserInstallRunner
from jev_agent.dashboard.design_studio import wizard_context
from jev_agent.dashboard.preview import PreviewBridge
from jev_agent.dashboard.runner import ChatRunner, RunLauncher
from jev_agent.dashboard.security import DashboardSettings, load_or_create_secret


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str] | None = None) -> httpx.Response:
    response: httpx.Response = client.post(
        url,
        data={"csrf_token": _csrf(client), **(data or {})},
        follow_redirects=False,
    )
    assert isinstance(response, httpx.Response)
    return response


# ---- 1. wizard 500 on skipped step -------------------------------------------------------


def test_wizard_context_stack_without_app_type_does_not_raise() -> None:
    """The old code indexed session_state['app_type']['app_type'] -> KeyError."""
    context = wizard_context({}, "stack")
    assert context["stack_options"] == []
    assert context["stack_app_type"] == ""


def test_post_stack_without_app_type_is_friendly_error_not_500(client: TestClient) -> None:
    response = _post(client, "/design-studio/new/stack", {"stack": "Web + Tailwind"})
    assert response.status_code == 200, "the validation error must re-render, not 500"
    assert "App type" in response.text
    assert "Traceback" not in response.text


def test_wizard_start_is_post_only(client: TestClient) -> None:
    assert client.get("/design-studio/new", follow_redirects=False).status_code == 405
    response = _post(client, "/design-studio/new")
    assert response.status_code == 303
    assert response.headers["location"] == "/design-studio/new/purpose"


# ---- 2. CSP: no inline scripts/styles in dashboard templates -----------------------------


def test_landing_and_connections_have_no_inline_style_or_script(client: TestClient) -> None:
    for path in ("/design-studio", "/connections"):
        page = client.get(path)
        assert page.status_code == 200
        # A bare style="..." attribute (data-ui-style="..." is not one).
        assert not re.search(r'(?<![\w-])style="', page.text), f"inline style survives on {path}"
        assert "<script>" not in page.text, f"inline script survives on {path}"
    landing = client.get("/design-studio").text
    assert "/static/copy.js" not in landing  # copy.js is only on the code page
    assert "/static/data-attrs.js" in landing
    assert 'action="/design-studio/new"' in landing  # the start is a POST form


def test_showcase_gets_scoped_csp_but_dashboard_pages_do_not(
    client: TestClient, home: Path
) -> None:
    save_library_system(
        "acme-brand",
        "# Acme\n\nBody.\n",
        {"colors": {"primary": "#e8590c"}},
        {"name": "acme-brand", "source": "wizard", "app_type": "marketing", "stack": "Next.js"},
    )
    showcase = client.get("/design-systems/acme-brand/showcase")
    assert showcase.status_code == 200
    assert "unsafe-inline" in showcase.headers["content-security-policy"]
    board = client.get("/")
    assert "unsafe-inline" not in board.headers["content-security-policy"]


# ---- 3. preview sessions pruned on polls -------------------------------------------------


def test_preview_poll_prunes_stale_sessions() -> None:
    bridge = PreviewBridge()
    stale = bridge.register(run_id="old", role="coder")
    stale.last_seen = time.monotonic() - 10_000  # no heartbeat for hours
    fresh = bridge.register(run_id="new", role="coder")
    assert bridge.active() is fresh
    assert stale.session_id not in bridge._sessions
    assert fresh.session_id in bridge._sessions


def test_preview_prune_returns_count() -> None:
    bridge = PreviewBridge()
    session = bridge.register(run_id="r", role="c")
    session.last_seen = time.monotonic() - 10_000
    assert bridge.prune() == 1
    assert bridge.active() is None


# ---- 4. bare 500s -> friendly error page / flash ------------------------------------------


def test_page_catch_all_renders_error_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jev_agent.dashboard.views as views_mod

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the board build exploded")

    # Same module object app.py renders through, so this replaces its board.
    monkeypatch.setattr(views_mod, "board", _boom)
    response = client.get("/")
    assert response.status_code == 500
    assert "text/html" in response.headers["content-type"]
    assert "Something went wrong" in response.text
    assert "Traceback" not in response.text
    assert "the board build exploded" in response.text


def test_act_catch_all_flashes_and_redirects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jev_agent.app.settings as settings_mod

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("trust exploded")

    monkeypatch.setattr(settings_mod, "trust_repo", _boom)
    response = client.post(
        "/connections/trust?next=/connections",
        data={"csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert response.status_code == 200, "an act() failure must redirect, not 500"
    assert "trust exploded" in response.text


# ---- 5. browser-install log: tail-read + cap ----------------------------------------------


def test_install_log_tail_reads_last_lines_only(tmp_path: Path) -> None:
    runner = BrowserInstallRunner(tmp_path)
    log = tmp_path / ".jev" / "browser-install.log"
    log.parent.mkdir(parents=True)
    # ~250 KiB of log: far past the 64 KiB tail window, so a whole-file read
    # would be visible here only through the result, not the cost — but the
    # tail must still be exactly the last 30 lines.
    lines = [f"line {i:05d} " + "x" * 40 for i in range(5000)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert runner.status().log_tail == tuple(lines[-30:])


def test_install_log_capped_after_finish(tmp_path: Path) -> None:
    runner = BrowserInstallRunner(tmp_path)
    log = tmp_path / ".jev" / "browser-install.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"old line\n" * 40_000)  # ~360 KiB, past the 256 KiB cap
    runner._cap_log()
    assert log.stat().st_size <= 262_144
    assert log.read_bytes().endswith(b"old line\n")


# ---- minors --------------------------------------------------------------------------------


def test_runner_job_dicts_pruned_past_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runner_mod, "_MAX_JOBS", 2)
    launcher = RunLauncher(tmp_path, executor=lambda root, run_id, action: "done")
    for run_id in ("r1", "r2", "r3"):
        assert launcher.start(run_id, "approve")
        assert launcher.wait(run_id) is not None
    assert launcher.start("r4", "approve")
    assert launcher.wait("r4") is not None
    assert len(launcher._jobs) <= 2
    assert launcher.job("r4") is not None  # the live job is never pruned


def test_chat_runner_job_dicts_pruned_past_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "_MAX_JOBS", 2)
    chat = ChatRunner()
    for chat_id in ("c1", "c2", "c3"):
        assert chat.start(chat_id, lambda: "done")
        assert chat.wait(chat_id) is not None
    assert chat.start("c4", lambda: "done")
    assert chat.wait("c4") is not None
    assert len(chat._jobs) <= 2


def test_secret_file_created_mode_600(home: Path) -> None:
    first = load_or_create_secret()
    assert len(first) == 32
    path = home / ".jev" / "dashboard-secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_secret() == first  # persisted, not regenerated


def test_session_cookie_secure_only_over_https(fixture_repo: Path) -> None:
    """Secure follows the request scheme, not the bind host: a Secure cookie
    on a plain-HTTP dashboard would never be sent back, breaking sessions
    and the ?token= cookie swap (test_token_required_off_loopback pins it)."""
    loopback_app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(loopback_app) as loopback:
        cookie = loopback.get("/").headers.get("set-cookie", "")
        assert "secure" not in cookie.lower()

    off_loopback_app = create_app(
        DashboardSettings(
            root=fixture_repo,
            host="192.0.2.1",
            token="tok123",
            allowed_hosts=frozenset({"testserver"}),
        ),
    )
    with TestClient(off_loopback_app) as remote:
        response = remote.get("/?token=tok123", follow_redirects=False)
        assert response.status_code == 303
        assert "secure" not in response.headers.get("set-cookie", "").lower()
    with TestClient(off_loopback_app, base_url="https://testserver") as remote_https:
        response = remote_https.get("/?token=tok123", follow_redirects=False)
        assert response.status_code == 303
        assert "secure" in response.headers.get("set-cookie", "").lower()
    assert security_mod.SHOWCASE_CONTENT_SECURITY_POLICY  # the constant exists
