"""PreviewRoutes: Live preview panes and the agent preview bridge."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    Response,
)

from jev_agent.config import STATE_DIRNAME
from jev_agent.dashboard.core import DashboardCore
from jev_agent.dashboard.preview import (
    PreviewBridge,
    PreviewError,
    PreviewInput,
    PreviewSession,
    read_capped_body,
)
from jev_agent.dashboard.security import (
    checked_form,
    same_origin,
)
from jev_agent.servers.audit import AuditEntry, AuditLog, audit_path


def _preview_bridge(request: Request) -> PreviewBridge:
    bridge: PreviewBridge = request.app.state.preview
    return bridge


def _preview_view(session: PreviewSession | None) -> dict[str, Any] | None:
    if session is None:
        return None
    age = (
        int((datetime.now(UTC) - session.frame_at).total_seconds())
        if session.frame_at is not None
        else None
    )
    return {
        "session_id": session.session_id,
        "driver": session.driver.value,
        "label": session.label,
        "run_id": session.run_id,
        "page_url": session.page_url,
        "frame_age_s": age,
        "has_frame": session.frame is not None,
    }


class PreviewRoutes(DashboardCore):
    def preview_live(self, request: Request) -> Response:
        """The Preview pane's poll target for `JevShell.mountPane("preview", "/preview/live", ms)`.

        Renders `_preview_live.html`: the "No live preview" empty state when no
        agent browser session is live, otherwise the latest screenshot with the
        takeover controls (bound by `static/preview.js`).
        """
        bridge = _preview_bridge(request)
        return self.render(request, "_preview_live.html", preview=_preview_view(bridge.active()))

    def preview_frame(self, request: Request) -> Response:
        """The latest screenshot of the live browser session (PNG, never cached)."""
        session = _preview_bridge(request).active()
        if session is None or session.frame is None:
            raise HTTPException(404, "no live preview")
        return Response(session.frame, media_type="image/png")

    def _audit_preview(self, session: PreviewSession, action: str) -> None:
        entry = AuditEntry(
            run_id=session.run_id,
            role="dashboard",
            server="preview",
            access="dashboard",
            action=action,
            command=session.session_id,
            allowed=True,
            reason=f"driver -> {session.driver.value}",
        )
        AuditLog(audit_path(self.settings.root / STATE_DIRNAME)).write(entry)

    async def preview_takeover(self, request: Request) -> Response:
        """Explicit user action: the user drives the agent's browser until hand-back."""
        await checked_form(request)
        bridge = _preview_bridge(request)
        try:
            session = bridge.take_over()
        except PreviewError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        self._audit_preview(session, "preview_takeover")
        return JSONResponse({"driver": session.driver.value, "session_id": session.session_id})

    async def preview_handback(self, request: Request) -> Response:
        """Explicit user action: the wheel goes back to the agent."""
        await checked_form(request)
        bridge = _preview_bridge(request)
        try:
            session = bridge.hand_back()
        except PreviewError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        self._audit_preview(session, "preview_handback")
        return JSONResponse({"driver": session.driver.value, "session_id": session.session_id})

    async def preview_input(self, request: Request) -> Response:
        """One user input event for the agent's browser (typed text, click, key).

        Accepted only while the user drives (409 otherwise); the agent side drains
        the queue through the agent seam's poll endpoint.
        """
        form = await checked_form(request)
        bridge = _preview_bridge(request)
        try:
            event = PreviewInput.from_form(form)
            bridge.queue_input(event)
        except PreviewError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        return JSONResponse({"ok": True, "kind": event.kind})

    def _check_agent(self, request: Request) -> None:
        # The agent side is a local process, not a browser: no CSRF token, but the
        # same-origin check still keeps cross-site pages from driving these.
        if not same_origin(request):
            raise HTTPException(403, "cross-origin request refused")

    async def preview_agent_register(self, request: Request) -> Response:
        """Agent seam: register a browser session when the agent starts browsing."""
        self._check_agent(request)
        form = await request.form()
        run_id = str(form.get("run_id") or "")
        role = str(form.get("role") or "")
        if not run_id or not role:
            raise HTTPException(400, "run_id and role are required")
        session = _preview_bridge(request).register(
            run_id=run_id, role=role, label=str(form.get("label") or "")
        )
        return JSONResponse({"session_id": session.session_id})

    async def preview_agent_frame(self, request: Request) -> Response:
        """Agent seam: push the latest screenshot (PNG body); also a heartbeat."""
        self._check_agent(request)
        session_id = request.query_params.get("session", "")
        page_url = request.query_params.get("url", "")
        # Read in chunks with a bail-out: a malicious local caller must not make the
        # server buffer an unbounded body before the size check runs.
        try:
            body = await read_capped_body(request)
            _preview_bridge(request).post_frame(session_id, body, page_url=page_url)
        except PreviewError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        return JSONResponse({"ok": True})

    async def preview_agent_poll(self, request: Request) -> Response:
        """Agent seam: who drives now, and the queued user input (each delivered once).

        A POST (never GET) so a stray navigation cannot drain the queue.
        """
        self._check_agent(request)
        form = await request.form()
        try:
            driver, events = _preview_bridge(request).poll(str(form.get("session_id") or ""))
        except PreviewError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        return JSONResponse(
            {"driver": driver.value, "inputs": [event.as_dict() for event in events]}
        )
