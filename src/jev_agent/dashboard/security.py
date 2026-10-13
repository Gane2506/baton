"""The dashboard's security model (docs/dashboard.md "Security").

- **Loopback by default.** On 127.0.0.1 / localhost / ::1 only requests whose `Host` header
  names a loopback address are served, so a web page cannot reach the dashboard through DNS
  rebinding.
- **Token off loopback.** Any other bind address needs a token (`--token`, or one generated at
  start). Every request must carry it: the `jev_token` cookie, the `X-Jev-Token` header or a
  `?token=` query parameter (which is swapped for the cookie by a redirect).
- **CSRF.** Every state-changing request is a POST that must come from the same origin
  (`Sec-Fetch-Site`, `Origin` / `Referer`) and carry the per-session token rendered into every
  form (an HMAC of the session cookie with a per-process secret).
- **Headers.** A strict Content-Security-Policy (no inline scripts or styles, no third-party
  origins), no framing, no sniffing, no caching.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp

TOKEN_COOKIE = "jev_token"
TOKEN_HEADER = "x-jev-token"
TOKEN_QUERY = "token"
SESSION_COOKIE = "jev_session"
CSRF_FIELD = "csrf_token"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "object-src 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


class DashboardConfigError(ValueError):
    """The dashboard settings are unsafe or invalid (e.g. a public bind without a token)."""


def is_loopback(host: str) -> bool:
    """True for `localhost` and loopback IP addresses (127.0.0.0/8, ::1)."""
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class DashboardSettings:
    """What `jev dashboard` serves and how. `token` is required off loopback."""

    root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    token: str | None = None
    # Host header names accepted on loopback; None: the loopback names. Tests add theirs.
    allowed_hosts: frozenset[str] | None = None
    refresh_s: float = 3.0  # how often live parts of pages reload
    # Signs CSRF tokens; a fresh one per process, so restarting invalidates old forms.
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)

    @property
    def loopback(self) -> bool:
        return is_loopback(self.host)

    def check(self) -> None:
        if not self.loopback and not self.token:
            raise DashboardConfigError(
                f"binding to {self.host} (not loopback) needs a token; pass --token or let "
                "`jev dashboard` generate one"
            )

    def host_allowed(self, host_header: str) -> bool:
        """Off loopback every Host is accepted (the token protects); on loopback only
        loopback names, so DNS rebinding cannot reach the dashboard."""
        if not self.loopback:
            return True
        name = _host_name(host_header)
        allowed = self.allowed_hosts if self.allowed_hosts is not None else LOOPBACK_HOSTS
        return name in allowed or is_loopback(name)


def _host_name(host_header: str) -> str:
    """`127.0.0.1:8765` -> `127.0.0.1`; `[::1]:8765` -> `::1`."""
    value = host_header.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def token_matches(given: str | None, expected: str) -> bool:
    return given is not None and hmac.compare_digest(given.encode(), expected.encode())


def csrf_token(settings: DashboardSettings, session: str) -> str:
    return hmac.new(settings.secret, session.encode(), hashlib.sha256).hexdigest()


class SecurityMiddleware(BaseHTTPMiddleware):
    """Host check, token check, session cookie and security headers for every request."""

    def __init__(self, app: ASGIApp, settings: DashboardSettings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await self._handle(request, call_next)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    async def _handle(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        settings = self.settings
        if not settings.host_allowed(request.headers.get("host", "")):
            return PlainTextResponse("invalid Host header", status_code=400)
        if settings.token:
            from_query = request.query_params.get(TOKEN_QUERY)
            if token_matches(from_query, settings.token) and request.method in ("GET", "HEAD"):
                # Swap the query token for a cookie, so it leaves the address bar and history.
                rest = [(k, v) for k, v in request.query_params.multi_items() if k != TOKEN_QUERY]
                target = request.url.path + (f"?{urlencode(rest)}" if rest else "")
                redirect = RedirectResponse(target, status_code=303)
                _set_cookie(redirect, TOKEN_COOKIE, settings.token)
                return redirect
            given = (
                request.cookies.get(TOKEN_COOKIE) or request.headers.get(TOKEN_HEADER) or from_query
            )
            if not token_matches(given, settings.token):
                return PlainTextResponse(
                    "This dashboard needs its access token: open the URL `jev dashboard` "
                    "printed (it ends in ?token=...).",
                    status_code=401,
                )
        session = request.cookies.get(SESSION_COOKIE)
        new_session = session is None or len(session) != 43
        if new_session:
            session = secrets.token_urlsafe(32)
        assert session is not None
        request.state.session = session
        request.state.csrf = csrf_token(settings, session)
        response = await call_next(request)
        if new_session:
            _set_cookie(response, SESSION_COOKIE, session)
        return response


def _set_cookie(response: Response, name: str, value: str) -> None:
    response.set_cookie(name, value, httponly=True, samesite="strict", path="/")


def same_origin(request: Request) -> bool:
    """The request comes from a page of this dashboard (or from a client that sends no
    browser origin headers at all, like a script; the CSRF token still applies)."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    host = request.headers.get("host", "").lower()
    origin = request.headers.get("origin")
    if origin is not None:
        return origin != "null" and urlsplit(origin).netloc.lower() == host
    referer = request.headers.get("referer")
    if referer is not None:
        return urlsplit(referer).netloc.lower() == host
    return True


async def checked_form(request: Request) -> FormData:
    """The POSTed form, after the same-origin and CSRF token checks (403 otherwise)."""
    if not same_origin(request):
        raise HTTPException(403, "cross-origin request refused")
    form = await request.form()
    given = form.get(CSRF_FIELD)
    expected = getattr(request.state, "csrf", None)
    if not isinstance(given, str) or expected is None or not hmac.compare_digest(given, expected):
        raise HTTPException(
            403, "missing or stale form token; reload the page and try again (CSRF check)"
        )
    return form
