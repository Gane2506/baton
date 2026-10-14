"""Dashboard browser preview (workstream F): feed, empty state, takeover/hand-back,
input forwarding, the one-driver rule and CSRF (no network, no real browser).

The agent side is faked through the preview agent seam (`/preview/agent/*`), the
same HTTP contract the real agent side must call (see
`jev_agent/dashboard/preview.py`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.testclient import TestClient

import jev_agent.dashboard.preview as preview_mod
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.preview import PreviewBridge
from jev_agent.dashboard.runner import RunAction
from jev_agent.dashboard.security import DashboardSettings
from jev_agent.servers.audit import AuditLog, audit_path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


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
def client(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(preview_mod, "SESSION_TTL_S", 3600.0)
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
        executor=Calls(),
    )
    with TestClient(app) as client:
        yield client


def _bridge(client: TestClient) -> PreviewBridge:
    bridge: PreviewBridge = cast(Any, client.app).state.preview
    return bridge


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/chats").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str] | None = None) -> httpx.Response:
    response: httpx.Response = client.post(
        url,
        data={**(data or {}), "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    return response


def _register(client: TestClient, run_id: str = "run1", role: str = "coder") -> str:
    response = client.post("/preview/agent/register", data={"run_id": run_id, "role": role})
    assert response.status_code == 200, response.text
    return str(response.json()["session_id"])


def _frame(client: TestClient, session_id: str, body: bytes = PNG) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/preview/agent/frame?session={session_id}&url=https://example.com",
        content=body,
        headers={"content-type": "image/png"},
    )
    return response


def _audit_entries(repo: Path) -> list[dict[str, Any]]:
    return AuditLog(audit_path(repo / ".jev")).read()


# ---- feed and empty state ----------------------------------------------------


def test_preview_live_empty_state(client: TestClient) -> None:
    response = client.get("/preview/live")
    assert response.status_code == 200
    assert "No live preview" in response.text


def test_preview_frame_empty_is_404(client: TestClient) -> None:
    assert client.get("/preview/frame").status_code == 404


def test_stale_session_shows_empty_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register(client)
    assert _frame(client, session_id).status_code == 200
    assert "Agent driving" in client.get("/preview/live").text
    monkeypatch.setattr(preview_mod, "SESSION_TTL_S", -1.0)  # every session is stale
    assert "No live preview" in client.get("/preview/live").text


def test_feed_serves_latest_frame(client: TestClient) -> None:
    session_id = _register(client)
    assert _frame(client, session_id).status_code == 200
    live = client.get("/preview/live")
    assert live.status_code == 200
    assert "Agent driving" in live.text
    assert 'src="/preview/frame"' in live.text
    frame = client.get("/preview/frame")
    assert frame.status_code == 200
    assert frame.headers["content-type"] == "image/png"
    assert frame.content == PNG


def test_register_needs_run_id_and_role(client: TestClient) -> None:
    assert client.post("/preview/agent/register", data={}).status_code == 400


def test_frame_rejects_non_png(client: TestClient) -> None:
    session_id = _register(client)
    response = client.post(
        f"/preview/agent/frame?session={session_id}",
        content=b"not a png",
        headers={"content-type": "image/png"},
    )
    assert response.status_code == 400


def test_frame_rejects_oversize(client: TestClient) -> None:
    session_id = _register(client)
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (2_000_000 + 1)
    response = client.post(
        f"/preview/agent/frame?session={session_id}",
        content=big,
        headers={"content-type": "image/png"},
    )
    assert response.status_code == 413


def test_frame_unknown_session_is_404(client: TestClient) -> None:
    response = client.post(
        "/preview/agent/frame?session=nope",
        content=PNG,
        headers={"content-type": "image/png"},
    )
    assert response.status_code == 404


# ---- takeover / hand-back: explicit, audited, one driver at a time ------------


def test_takeover_needs_csrf(client: TestClient, fixture_repo: Path) -> None:
    _register(client)
    assert client.post("/preview/takeover", data={}).status_code == 403
    assert _audit_entries(fixture_repo) == []


def test_takeover_and_handback_are_audited(client: TestClient, fixture_repo: Path) -> None:
    session_id = _register(client, run_id="run9")
    assert _frame(client, session_id).status_code == 200

    taken = _post(client, "/preview/takeover")
    assert taken.status_code == 200
    assert taken.json() == {"driver": "user", "session_id": session_id}
    assert "You are driving" in client.get("/preview/live").text

    handed = _post(client, "/preview/handback")
    assert handed.status_code == 200
    assert handed.json() == {"driver": "agent", "session_id": session_id}
    assert "Agent driving" in client.get("/preview/live").text

    actions = [entry["action"] for entry in _audit_entries(fixture_repo)]
    assert actions == ["preview_takeover", "preview_handback"]
    first = _audit_entries(fixture_repo)[0]
    assert first["run_id"] == "run9"
    assert first["command"] == session_id
    # the audit trail never carries input contents
    assert all("text" not in entry for entry in _audit_entries(fixture_repo))


def test_takeover_without_session_is_404(client: TestClient) -> None:
    assert _post(client, "/preview/takeover").status_code == 404


def test_double_takeover_and_early_handback_conflict(client: TestClient) -> None:
    session_id = _register(client)
    assert _frame(client, session_id).status_code == 200
    assert _post(client, "/preview/handback").status_code == 409  # agent drives already
    assert _post(client, "/preview/takeover").status_code == 200
    assert _post(client, "/preview/takeover").status_code == 409  # already driving


def test_get_never_mutates(client: TestClient) -> None:
    assert client.get("/preview/takeover").status_code == 405
    assert client.get("/preview/handback").status_code == 405
    assert client.get("/preview/input").status_code == 405
    assert client.get("/preview/agent/poll").status_code == 405


# ---- input forwarding -------------------------------------------------------


def test_input_needs_takeover(client: TestClient) -> None:
    session_id = _register(client)
    assert _frame(client, session_id).status_code == 200
    payload = {"kind": "type", "text": "s3cret"}
    assert _post(client, "/preview/input", payload).status_code == 409
    assert _post(client, "/preview/takeover").status_code == 200
    assert _post(client, "/preview/input", payload).status_code == 200


def test_agent_poll_delivers_input_once(client: TestClient) -> None:
    session_id = _register(client)
    assert _frame(client, session_id).status_code == 200
    assert _post(client, "/preview/takeover").status_code == 200
    assert _post(client, "/preview/input", {"kind": "type", "text": "s3cret"}).status_code == 200
    click = {"kind": "click", "x": "0.5", "y": "0.25"}
    assert _post(client, "/preview/input", click).status_code == 200
    assert _post(client, "/preview/input", {"kind": "key", "key": "Enter"}).status_code == 200

    first = client.post("/preview/agent/poll", data={"session_id": session_id})
    assert first.status_code == 200
    assert first.json()["driver"] == "user"
    kinds = [event["kind"] for event in first.json()["inputs"]]
    assert kinds == ["type", "click", "key"]
    assert first.json()["inputs"][0]["text"] == "s3cret"

    second = client.post("/preview/agent/poll", data={"session_id": session_id})
    assert second.json()["inputs"] == []  # delivered once

    # the agent is told to pause while the user drives
    assert _post(client, "/preview/handback").status_code == 200
    third = client.post("/preview/agent/poll", data={"session_id": session_id})
    assert third.json()["driver"] == "agent"


def test_handback_drops_queued_input(client: TestClient) -> None:
    session_id = _register(client)
    assert _frame(client, session_id).status_code == 200
    assert _post(client, "/preview/takeover").status_code == 200
    assert _post(client, "/preview/input", {"kind": "type", "text": "late"}).status_code == 200
    assert _post(client, "/preview/handback").status_code == 200
    poll = client.post("/preview/agent/poll", data={"session_id": session_id})
    assert poll.json()["inputs"] == []


def test_input_validation(client: TestClient) -> None:
    session_id = _register(client)
    assert _frame(client, session_id).status_code == 200
    assert _post(client, "/preview/takeover").status_code == 200
    assert _post(client, "/preview/input", {"kind": "nope"}).status_code == 400
    assert _post(client, "/preview/input", {"kind": "type", "text": ""}).status_code == 400
    bad_click = {"kind": "click", "x": "2", "y": "0.5"}
    assert _post(client, "/preview/input", bad_click).status_code == 400
    assert _post(client, "/preview/input", {"kind": "key", "key": "F12"}).status_code == 400


def test_input_after_handback_is_rejected(client: TestClient) -> None:
    session_id = _register(client)
    assert _frame(client, session_id).status_code == 200
    assert _post(client, "/preview/takeover").status_code == 200
    assert _post(client, "/preview/handback").status_code == 200
    assert _post(client, "/preview/input", {"kind": "type", "text": "x"}).status_code == 409


# ---- agent seam trust boundary ----------------------------------------------


def test_agent_endpoints_reject_cross_site(client: TestClient) -> None:
    headers = {"sec-fetch-site": "cross-site"}
    register = client.post(
        "/preview/agent/register", data={"run_id": "r", "role": "c"}, headers=headers
    )
    assert register.status_code == 403
    poll = client.post("/preview/agent/poll", data={"session_id": "x"}, headers=headers)
    assert poll.status_code == 403


def test_bridge_prune_and_unregister() -> None:
    bridge = PreviewBridge()
    session = bridge.register(run_id="r", role="c")
    assert bridge.active() is session
    bridge.unregister(session.session_id)
    assert bridge.active() is None
