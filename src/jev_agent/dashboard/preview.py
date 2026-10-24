"""The agent browser preview: what the agent's browser session sees, and user takeover.

Workstream F. The dashboard's Preview pane mounts ``GET /preview/live`` through the
existing ``JevShell.mountPane("preview", url, ms)`` contract (see
``templates/_panes.html`` and ``static/app.js``); this module is the state behind it.

Feed (polling, honest v1)
-------------------------
The agent side pushes screenshots with ``POST /preview/agent/frame``; the pane polls
``/preview/live`` (3000 ms, the mountPane default; the contract enforces at least
1000 ms), which embeds the latest frame served by ``/preview/frame``. Worst-case
staleness is one pane poll plus one agent push interval. True realtime
(websocket/SSE) was rejected for v1: it needs a new dependency and a new live
contract, while the existing mountPane/live.js polling pattern was built for
exactly this.

One driver at a time
--------------------
Each session's ``driver`` is ``agent`` or ``user`` (a StrEnum: one field, one value).
``POST /preview/takeover`` flips agent -> user, ``POST /preview/handback`` flips
back; anything else is a 409. ``POST /preview/input`` is accepted only while the
user drives. The agent side MUST poll ``POST /preview/agent/poll`` before every
browser action and stop driving while ``driver == "user"`` -- the dashboard cannot
reach into the agent's browser process, so the pause is cooperative; this module is
the single place that decides who drives.

AGENT SEAM (the minimal hook the agent side must call)
------------------------------------------------------
The agent's browser today is the pinned Playwright MCP server (``servers/browser.py``)
launched inside the agent CLI process; it has no hook for external input, so v1
defines this seam instead of rewriting it:

1. ``POST /preview/agent/register`` with form fields ``run_id``, ``role`` (and an
   optional ``label``) -> ``{"session_id": ...}``. Call once when the browser
   session starts.
2. ``POST /preview/agent/frame?session=<id>&url=<page url>`` with the PNG bytes as
   the body, after every screenshot (or at most every ~2 s). This doubles as the
   heartbeat: a session with no frame for 60 s is treated as gone.
3. ``POST /preview/agent/poll`` with ``session_id`` -> ``{"driver": ...,
   "inputs": [{"kind": ...}]}``. Call before each browser action; while the
   driver is ``"user"`` do NOT act -- drain and wait. Each input is delivered once.
4. Input kinds: ``{"kind": "type", "text": ...}`` (typed text, e.g. a password),
   ``{"kind": "click", "x": ..., "y": ...}`` (viewport fractions, 0..1),
   ``{"kind": "key", "key": ...}`` (one of Enter, Escape, Tab).

Security notes
--------------
- The feed serves screenshots only, never DOM: credential values the agent typed
  are pixels, not text the page could scrape. Typed takeover input is never echoed
  back into the pane (the fragment never renders queued input, and the password
  field is ``type="password"``).
- Every takeover and hand-back is audit-logged (``servers/audit.py``, action
  ``preview_takeover`` / ``preview_handback``); input contents are never logged.
- User endpoints are POST + ``security.checked_form`` (same-origin and CSRF); no
  GET ever triggers browser input, and ``GET /preview/agent/poll`` does not exist
  -- the poll is a POST so a stray navigation cannot drain the queue.
- Agent endpoints skip the CSRF token (the agent is a local process, not a
  browser) but still require ``same_origin``, so a cross-site page cannot drive
  them. They trust local callers; that is the documented v1 boundary.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_FRAME_BYTES = 2_000_000  # a 1080p screenshot is far below this; larger is a mistake
SESSION_TTL_S = 60.0  # a session with no heartbeat/frame this old is treated as gone
MAX_TEXT_LEN = 500  # one typed burst: a password, a short command
MAX_QUEUE = 100  # the agent side drains every action; a full queue means it stopped
ALLOWED_KEYS = frozenset({"Enter", "Escape", "Tab"})


class Driver(StrEnum):
    AGENT = "agent"
    USER = "user"


class PreviewError(Exception):
    """Every preview failure maps to an HTTP status (handled in `dashboard/app.py`)."""

    status_code = 500


class NoPreviewSession(PreviewError):
    status_code = 404


class WrongDriver(PreviewError):
    """The transition or input needs the other driver (one driver at a time)."""

    status_code = 409


class BadPreviewInput(PreviewError):
    status_code = 400


class FrameTooLarge(BadPreviewInput):
    status_code = 413


async def read_capped_body(request: Request, limit: int = MAX_FRAME_BYTES) -> bytes:
    """The request body, raising `FrameTooLarge` past `limit` without buffering it all.

    `Content-Length` is checked first so an obviously oversize frame is refused before
    a single byte is read; the chunked read below is the backstop for a lying (or
    missing) header.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise FrameTooLarge(f"frame declares {declared} bytes (max {limit})")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise FrameTooLarge(f"frame is larger than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass(frozen=True)
class PreviewInput:
    """One user input event for the agent's browser (validated at the HTTP boundary)."""

    kind: str  # "type" | "click" | "key"
    text: str = ""
    x: float = 0.0
    y: float = 0.0
    key: str = ""

    @classmethod
    def from_form(cls, form: FormData) -> PreviewInput:
        kind = str(form.get("kind") or "")
        if kind == "type":
            text = str(form.get("text") or "")
            if not text or len(text) > MAX_TEXT_LEN:
                raise BadPreviewInput(f"typed text must be 1..{MAX_TEXT_LEN} characters")
            return cls(kind=kind, text=text)
        if kind == "click":
            try:
                x = float(str(form.get("x") or ""))
                y = float(str(form.get("y") or ""))
            except ValueError:
                raise BadPreviewInput("click needs numeric x and y") from None
            if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
                raise BadPreviewInput("click x and y are viewport fractions (0..1)")
            return cls(kind=kind, x=x, y=y)
        if kind == "key":
            key = str(form.get("key") or "")
            if key not in ALLOWED_KEYS:
                raise BadPreviewInput(f"key must be one of {sorted(ALLOWED_KEYS)}")
            return cls(kind=kind, key=key)
        raise BadPreviewInput("kind must be 'type', 'click' or 'key'")

    def as_dict(self) -> dict[str, Any]:
        # For the agent poll only -- never rendered into a page, so typed secrets
        # (passwords) are never echoed back to the browser.
        data: dict[str, Any] = {"kind": self.kind}
        if self.kind == "type":
            data["text"] = self.text
        elif self.kind == "click":
            data["x"] = self.x
            data["y"] = self.y
        elif self.kind == "key":
            data["key"] = self.key
        return data


@dataclass
class PreviewSession:
    session_id: str
    run_id: str
    role: str
    label: str
    created: datetime
    last_seen: float  # monotonic seconds, refreshed by heartbeat/frame/poll
    page_url: str = ""
    frame: bytes | None = None
    frame_at: datetime | None = None
    driver: Driver = Driver.AGENT
    queue: deque[PreviewInput] = field(default_factory=deque)

    @property
    def fresh(self) -> bool:
        return time.monotonic() - self.last_seen <= SESSION_TTL_S


class PreviewBridge:
    """In-memory registry of agent browser sessions: the one-driver-at-a-time state.

    One per dashboard process (``app.state.preview``); the pane, the takeover
    endpoints and the agent seam all read and write through it under one lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, PreviewSession] = {}

    def register(self, *, run_id: str, role: str, label: str = "") -> PreviewSession:
        session = PreviewSession(
            session_id=uuid.uuid4().hex[:12],
            run_id=run_id,
            role=role,
            label=label or role,
            created=datetime.now(UTC),
            last_seen=time.monotonic(),
        )
        with self._lock:
            self._prune_unlocked()
            self._sessions[session.session_id] = session
        return session

    def _get_unlocked(self, session_id: str) -> PreviewSession:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise NoPreviewSession(f"unknown preview session {session_id!r}") from None

    def _active_unlocked(self) -> PreviewSession:
        fresh = [s for s in self._sessions.values() if s.fresh]
        if not fresh:
            raise NoPreviewSession("no live preview session")
        return max(fresh, key=lambda s: s.last_seen)

    def active(self) -> PreviewSession | None:
        """The newest live session, if any (what the Preview pane shows).

        Issue #200: `prune()` had zero callers, so stale sessions (up to 2 MB
        of frame each) accumulated forever. Every pane/frame poll goes through
        here, so pruning here bounds the registry by live traffic alone — no
        background thread, no new timer.
        """
        with self._lock:
            self._prune_unlocked()
            try:
                return self._active_unlocked()
            except NoPreviewSession:
                return None

    def session_for_run(self, run_id: str) -> PreviewSession | None:
        """The newest live session driving `run_id`'s browser, if any (the chat Work pane's
        preview tab)."""
        with self._lock:
            self._prune_unlocked()
            fresh = [s for s in self._sessions.values() if s.fresh and s.run_id == run_id]
            return max(fresh, key=lambda s: s.last_seen) if fresh else None

    def heartbeat(self, session_id: str) -> None:
        with self._lock:
            self._get_unlocked(session_id).last_seen = time.monotonic()

    def post_frame(self, session_id: str, png: bytes, page_url: str = "") -> PreviewSession:
        if len(png) > MAX_FRAME_BYTES:
            raise FrameTooLarge(f"frame is {len(png)} bytes (max {MAX_FRAME_BYTES})")
        if not png.startswith(PNG_MAGIC):
            raise BadPreviewInput("frame must be PNG bytes")
        with self._lock:
            session = self._get_unlocked(session_id)
            session.frame = png
            session.frame_at = datetime.now(UTC)
            session.last_seen = time.monotonic()
            if page_url:
                session.page_url = page_url[:500]
            return session

    def take_over(self) -> PreviewSession:
        """The user takes the wheel: AGENT -> USER (explicit user action only)."""
        with self._lock:
            session = self._active_unlocked()
            if session.driver is Driver.USER:
                raise WrongDriver("you are already driving this browser")
            session.driver = Driver.USER
            return session

    def hand_back(self) -> PreviewSession:
        """The user hands the wheel back: USER -> AGENT."""
        with self._lock:
            session = self._active_unlocked()
            if session.driver is Driver.AGENT:
                raise WrongDriver("the agent is already driving")
            session.driver = Driver.AGENT
            # Queued user input must never reach the agent after hand-back: it was
            # typed for a page state the agent may already have left (a password
            # landing in the wrong field is the failure this avoids).
            session.queue.clear()
            return session

    def queue_input(self, event: PreviewInput) -> PreviewSession:
        """Buffer one user input event for the agent side to drain."""
        with self._lock:
            session = self._active_unlocked()
            if session.driver is not Driver.USER:
                raise WrongDriver("take over before sending input: the agent is driving")
            if len(session.queue) >= MAX_QUEUE:
                raise BadPreviewInput("input queue is full; the agent side is not draining it")
            session.queue.append(event)
            return session

    def poll(self, session_id: str) -> tuple[Driver, list[PreviewInput]]:
        """The agent side's hook: who drives now, and the queued input (delivered once)."""
        with self._lock:
            session = self._get_unlocked(session_id)
            session.last_seen = time.monotonic()
            events = list(session.queue)
            session.queue.clear()
            return session.driver, events

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def prune(self) -> int:
        """Drop sessions nobody heard from; returns how many went away."""
        with self._lock:
            return self._prune_unlocked()

    def _prune_unlocked(self) -> int:
        """`prune()` with the lock already held (the poll paths call this)."""
        stale = [sid for sid, s in self._sessions.items() if not s.fresh]
        for sid in stale:
            del self._sessions[sid]
        return len(stale)
