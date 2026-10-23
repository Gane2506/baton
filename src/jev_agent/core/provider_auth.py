"""Auth detection for the `claude` / `codex` CLIs the workers shell out to.

Both CLIs authenticate with the user's *subscription* (OAuth) or an API key —
whichever the environment offers. This module detects, in each CLI's own
precedence order, which auth is present. It reports names and set/not-set only;
values are never returned, logged, or rendered.

Claude Code precedence (its own: an API key outranks the subscription login):
1. `ANTHROPIC_API_KEY` — API key, billed per token.
2. `CLAUDE_CODE_OAUTH_TOKEN` — long-lived OAuth token minted by
   `claude setup-token` after a browser sign-in with the Claude subscription.
3. `~/.claude/.credentials.json` holding `claudeAiOauth` — the interactive
   subscription login (`claude` first run), refreshed automatically.
4. Nothing.

Codex precedence:
1. `OPENAI_API_KEY` — API key (a supported auth env var).
2. `~/.codex/auth.json` holding `tokens` — the ChatGPT subscription OAuth
   login from `codex login --device-auth` (device code flow).
3. `~/.codex/auth.json` holding `api_key` — API key via
   `codex login --with-api-key`.
4. Nothing.

Workers run the CLIs with the full process environment (`os.environ`), and the
dashboard/CLI inject `~/.jev/secrets.env` into it at startup, so anything saved
through the Connections page is visible to the next worker run with no extra
wiring. The integration point this module provides is detection + the sign-in
flow runners the dashboard drives.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jev_agent.app.errors import JevError

__all__ = [
    "ANTHROPIC_API_KEY_ENV",
    "CLAUDE_OAUTH_TOKEN_ENV",
    "CLAUDE_SETUP_TOKEN_PREFIX",
    "CODEX_DEVICE_URL",
    "OPENAI_API_KEY_ENV",
    "ClaudeSetupTokenFlow",
    "CodexDeviceFlow",
    "DeviceFlowBusy",
    "DeviceFlowError",
    "DeviceFlowStatus",
    "ProviderAuth",
    "check_claude_oauth_token",
    "claude_code_auth",
    "codex_auth",
    "extract_setup_token",
    "parse_device_output",
    "parse_setup_token_output",
]

CLAUDE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

# `claude setup-token` prints a one-year OAuth token with this prefix after the
# browser sign-in. It is not saved anywhere by the CLI; the user pastes it.
CLAUDE_SETUP_TOKEN_PREFIX = "sk-ant-oat01-"

# Where `codex login --device-auth` sends the user; printed by the CLI itself.
CODEX_DEVICE_URL = "https://auth.openai.com/codex/device"

_AUTH_NONE = "none"
_AUTH_OAUTH_TOKEN = "oauth_token"
_AUTH_API_KEY = "api_key"
_AUTH_CLI_LOGIN = "cli_login"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\r")
_URL = re.compile(r"https://\S+")
_DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4,6}-[A-Z0-9]{4,6}\b")


@dataclass(frozen=True)
class ProviderAuth:
    """What a CLI worker will authenticate with. Names only, never values."""

    provider: str  # "claude_code" | "codex"
    title: str  # "Claude Code" | "Codex"
    command: str  # the CLI executable from config
    installed: bool  # the CLI is on PATH
    auth: str  # "oauth_token" | "api_key" | "cli_login" | "none"
    label: str  # short badge text, e.g. "Subscription login (OAuth)"
    detail: str  # one line on where it was found, names only
    warning: str | None = None  # config/auth mismatch worth surfacing


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _credentials_login(home: Path) -> bool:
    """The `claude` CLI's own interactive subscription login is present."""
    try:
        data = json.loads((home / ".claude" / ".credentials.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("claudeAiOauth"))


def _codex_auth_file(home: Path) -> str:
    """`~/.codex/auth.json` contents: "oauth_token" | "api_key" | "none"."""
    try:
        data = json.loads((home / ".codex" / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _AUTH_NONE
    if not isinstance(data, dict):
        return _AUTH_NONE
    if data.get("tokens"):
        return _AUTH_OAUTH_TOKEN
    if data.get("api_key"):
        return _AUTH_API_KEY
    return _AUTH_NONE


def claude_code_auth(
    *,
    command: str = "claude",
    bare: bool = False,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> ProviderAuth:
    """Which auth the `claude` CLI worker will use, in Claude Code's precedence."""
    environ: Mapping[str, str] = os.environ if env is None else env
    home = home or Path.home()
    installed = which(command) is not None
    warning = None
    if environ.get(ANTHROPIC_API_KEY_ENV, "").strip():
        auth, label = _AUTH_API_KEY, "API key"
        detail = f"{ANTHROPIC_API_KEY_ENV} is set (outranks the subscription login)"
    elif environ.get(CLAUDE_OAUTH_TOKEN_ENV, "").strip():
        auth, label = _AUTH_OAUTH_TOKEN, "Subscription login (OAuth)"
        detail = f"{CLAUDE_OAUTH_TOKEN_ENV} is set (long-lived token from `claude setup-token`)"
        if bare:
            warning = (
                "`--bare` mode does not read the OAuth token; "
                f"set {ANTHROPIC_API_KEY_ENV} for bare mode"
            )
    elif _credentials_login(home):
        auth, label = _AUTH_CLI_LOGIN, "Subscription login (OAuth)"
        detail = "the `claude` CLI is signed in with your subscription"
        if bare:
            warning = (
                "`--bare` mode does not read the CLI subscription login; "
                f"set {ANTHROPIC_API_KEY_ENV} for bare mode"
            )
    else:
        auth, label = _AUTH_NONE, "Not connected"
        detail = "run `claude setup-token` and paste the token, or sign in with the `claude` CLI"
    if not installed:
        detail += f"; `{command}` is not on PATH"
    return ProviderAuth(
        provider="claude_code",
        title="Claude Code",
        command=command,
        installed=installed,
        auth=auth,
        label=label,
        detail=detail,
        warning=warning,
    )


def codex_auth(
    *,
    command: str = "codex",
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> ProviderAuth:
    """Which auth the `codex` CLI worker will use."""
    environ: Mapping[str, str] = os.environ if env is None else env
    home = home or Path.home()
    installed = which(command) is not None
    if environ.get(OPENAI_API_KEY_ENV, "").strip():
        auth, label = _AUTH_API_KEY, "API key"
        detail = f"{OPENAI_API_KEY_ENV} is set"
    else:
        file_auth = _codex_auth_file(home)
        if file_auth == _AUTH_OAUTH_TOKEN:
            auth, label = _AUTH_OAUTH_TOKEN, "Subscription login (OAuth)"
            detail = "signed in with ChatGPT (`codex login`)"
        elif file_auth == _AUTH_API_KEY:
            auth, label = _AUTH_API_KEY, "API key"
            detail = "API key stored by `codex login --with-api-key`"
        else:
            auth, label = _AUTH_NONE, "Not connected"
            detail = "sign in with ChatGPT (`codex login --device-auth`)"
    if not installed:
        detail += f"; `{command}` is not on PATH"
    return ProviderAuth(
        provider="codex",
        title="Codex",
        command=command,
        installed=installed,
        auth=auth,
        label=label,
        detail=detail,
    )


def check_claude_oauth_token(value: str) -> None:
    """Validate a pasted `claude setup-token` value. Raises ValueError."""
    value = (value or "").strip()
    if not value:
        raise ValueError("paste the token printed by `claude setup-token`")
    if not value.startswith(CLAUDE_SETUP_TOKEN_PREFIX):
        raise ValueError(
            "that doesn't look like a `claude setup-token` token "
            f"(expected it to start with `{CLAUDE_SETUP_TOKEN_PREFIX}`)"
        )


def parse_device_output(text: str) -> tuple[str | None, str | None]:
    """Pull the (url, one-time code) out of `codex login --device-auth` output.

    The CLI colourises its output, so ANSI escapes are stripped first. Returns
    `(None, None)` until both have been seen.
    """
    clean = _strip_ansi(text)
    url_match = _URL.search(clean)
    code_match = _DEVICE_CODE.search(clean)
    return (
        url_match.group(0) if url_match else None,
        code_match.group(0) if code_match else None,
    )


# `claude setup-token` prints an authorize URL, then — after the browser
# approval — prompts for the code the browser shows. ANSI escapes are stripped
# before matching, like the device output above.
_CLAUDE_AUTHORIZE_URL = re.compile(r"https://claude\.ai/oauth/authorize\?[^\s\"']+")
_CODE_PROMPT = re.compile(r"paste.{0,40}code", re.IGNORECASE)
_SETUP_TOKEN = re.compile(r"sk-ant-oat01-[A-Za-z0-9\-_]+")


def parse_setup_token_output(text: str) -> tuple[str | None, bool]:
    """Pull the (authorize URL, code-prompt-seen) out of `claude setup-token`
    output.

    Returns the URL once printed and whether the CLI is prompting for the
    browser code yet. The token itself is extracted separately on completion
    so its value never lingers in parsed state.
    """
    clean = _strip_ansi(text)
    url_match = _CLAUDE_AUTHORIZE_URL.search(clean)
    if url_match:
        return url_match.group(0), bool(_CODE_PROMPT.search(clean))
    # Fall back to any URL: older/newer CLI wordings still print exactly one.
    generic = _URL.search(clean)
    return (generic.group(0), bool(_CODE_PROMPT.search(clean))) if generic else (None, False)


def extract_setup_token(text: str) -> str | None:
    """Pull the `sk-ant-oat01-…` token out of `claude setup-token` output.

    Returns the token value — callers must treat it as a secret (save it via
    `write_secret`, never log or render it).
    """
    match = _SETUP_TOKEN.search(_strip_ansi(text))
    return match.group(0) if match else None


class DeviceFlowError(JevError):
    """A Codex device sign-in could not be started or tracked."""


class DeviceFlowBusy(DeviceFlowError):
    """A Codex device sign-in is already in progress."""


@dataclass
class DeviceFlowStatus:
    """The dashboard renders this; the one-time code is single-use (15 min)."""

    state: str = "idle"  # idle|starting|waiting|done|failed|cancelled
    url: str | None = None
    code: str | None = None
    detail: str = ""


class CodexDeviceFlow:
    """Runs `codex login --device-auth` in the background for the dashboard.

    The CLI prints a URL and a one-time code, then polls until the user
    approves the sign-in in their browser (or the 15-minute code expires).
    `start()` returns once the process is spawned; `status()` reports the
    parsed URL/code while waiting and the outcome afterwards. Only one flow
    runs at a time; `shutdown()` stops it (the dashboard calls this on exit).
    """

    def __init__(
        self,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self._popen = popen
        self._lock = threading.Lock()
        self._status = DeviceFlowStatus()
        self._process: Any = None
        self._thread: threading.Thread | None = None
        self._command = "codex"
        self._home = Path.home()

    # -- dashboard API ---------------------------------------------------------

    def start(self, command: str = "codex", home: Path | None = None) -> DeviceFlowStatus:
        """Spawn the device flow. Raises DeviceFlowBusy / DeviceFlowError."""
        with self._lock:
            if self._status.state in ("starting", "waiting"):
                raise DeviceFlowBusy("a Codex sign-in is already in progress")
            if shutil.which(command) is None:
                raise DeviceFlowError(f"the `{command}` CLI is not on PATH; install it first")
            self._status = DeviceFlowStatus(state="starting")
            self._command = command
            self._home = home or Path.home()
        try:
            process = self._popen(
                [command, "login", "--device-auth"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            with self._lock:
                self._status = DeviceFlowStatus(state="failed", detail=str(exc))
            raise DeviceFlowError(f"could not start `{command} login`: {exc}") from exc
        with self._lock:
            self._process = process
            thread = threading.Thread(target=self._watch, name="codex-device-flow", daemon=True)
            self._thread = thread
            thread.start()
        return self.status()

    def status(self) -> DeviceFlowStatus:
        with self._lock:
            return DeviceFlowStatus(
                state=self._status.state,
                url=self._status.url,
                code=self._status.code,
                detail=self._status.detail,
            )

    def cancel(self) -> bool:
        """Stop a pending sign-in. True when one was running."""
        with self._lock:
            if self._status.state not in ("starting", "waiting"):
                return False
            process, thread = self._process, self._thread
            self._status.state = "cancelled"
            self._status.detail = "cancelled; no credentials were stored"
        if process is not None:
            with contextlib.suppress(OSError):
                process.terminate()
        if thread is not None:
            thread.join(timeout=5)
        with self._lock:
            self._process = None
            self._thread = None
        return True

    def shutdown(self) -> None:
        """Best-effort stop for dashboard exit; never raises."""
        with contextlib.suppress(Exception):
            self.cancel()

    # -- internals ---------------------------------------------------------------

    def _watch(self) -> None:
        """Read the CLI's output, parse the URL/code, then record the outcome."""
        process = self._process
        output: list[str] = []
        try:
            assert process is not None and process.stdout is not None
            for line in process.stdout:
                output.append(line)
                url, code = parse_device_output("".join(output))
                with self._lock:
                    if self._status.state == "cancelled":
                        return
                    if url and code:
                        self._status.url = url
                        self._status.code = code
                        if self._status.state == "starting":
                            self._status.state = "waiting"
                            self._status.detail = (
                                "open the link in your browser and enter the code; "
                                "the code expires in 15 minutes"
                            )
            process.wait()
        except Exception as exc:  # a watcher must not kill the dashboard
            with self._lock:
                if self._status.state != "cancelled":
                    self._status = DeviceFlowStatus(state="failed", detail=str(exc))
            return
        with self._lock:
            if self._status.state == "cancelled":
                return
            auth = codex_auth(command=self._command, home=self._home)
            if auth.auth == _AUTH_OAUTH_TOKEN:
                self._status = DeviceFlowStatus(state="done", detail="signed in with ChatGPT")
            else:
                self._status = DeviceFlowStatus(
                    state="failed",
                    detail=(
                        "the sign-in did not complete — the code may have expired "
                        "or the browser step was not finished"
                    ),
                )
            self._process = None
            self._thread = None


@dataclass
class ClaudeSetupTokenStatus:
    """The dashboard renders this; the token value is never stored here."""

    state: str = "idle"  # idle|starting|waiting|done|failed|cancelled
    url: str | None = None
    code_prompt: bool = False  # the CLI is asking for the browser code
    detail: str = ""


class ClaudeSetupTokenFlow:
    """Runs `claude setup-token` in the background for the dashboard.

    The CLI prints an authorize URL; the user approves in their browser and
    the browser shows a code which the dashboard forwards to the CLI's stdin
    via `submit_code()`. The CLI then prints the `sk-ant-oat01-…` token,
    which this flow saves to `~/.jev/secrets.env` as `CLAUDE_CODE_OAUTH_TOKEN`
    through `write_secret` — the token value never appears in status, logs,
    or rendered pages.

    `start()` returns once the process is spawned; `status()` reports the
    parsed URL and whether the code prompt has appeared. Only one flow runs
    at a time; `cancel()`/`shutdown()` stop it. A `timeout_s` deadline guards
    against a hung CLI.
    """

    def __init__(
        self,
        popen: Callable[..., Any] = subprocess.Popen,
        write_secret: Callable[[str, str], None] | None = None,
    ) -> None:
        self._popen = popen
        self._write_secret = write_secret
        self._lock = threading.Lock()
        self._status = ClaudeSetupTokenStatus()
        self._process: Any = None
        self._thread: threading.Thread | None = None
        self._timer: threading.Timer | None = None
        self._command = "claude"

    # -- dashboard API ---------------------------------------------------------

    def start(self, command: str = "claude", timeout_s: float = 600.0) -> ClaudeSetupTokenStatus:
        """Spawn the setup-token flow. Raises DeviceFlowBusy / DeviceFlowError."""
        with self._lock:
            if self._status.state in ("starting", "waiting"):
                raise DeviceFlowBusy("a Claude sign-in is already in progress")
            if shutil.which(command) is None:
                raise DeviceFlowError(f"the `{command}` CLI is not on PATH; install it first")
            self._status = ClaudeSetupTokenStatus(state="starting")
            self._command = command
        try:
            process = self._popen(
                [command, "setup-token"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            with self._lock:
                self._status = ClaudeSetupTokenStatus(state="failed", detail=str(exc))
            raise DeviceFlowError(f"could not start `{command} setup-token`: {exc}") from exc
        with self._lock:
            self._process = process
            thread = threading.Thread(
                target=self._watch, name="claude-setup-token-flow", daemon=True
            )
            self._thread = thread
            timer = threading.Timer(timeout_s, self._on_timeout)
            timer.daemon = True
            self._timer = timer
            thread.start()
            timer.start()
        return self.status()

    def status(self) -> ClaudeSetupTokenStatus:
        with self._lock:
            return ClaudeSetupTokenStatus(
                state=self._status.state,
                url=self._status.url,
                code_prompt=self._status.code_prompt,
                detail=self._status.detail,
            )

    def submit_code(self, code: str) -> bool:
        """Forward the browser code to the CLI's stdin. False when no flow is waiting."""
        code = (code or "").strip()
        if not code:
            return False
        with self._lock:
            process = self._process
            waiting = self._status.state == "waiting"
        if not waiting or process is None or process.stdin is None:
            return False
        try:
            process.stdin.write(code + "\n")
            process.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    def cancel(self) -> bool:
        """Stop a pending sign-in. True when one was running."""
        with self._lock:
            if self._status.state not in ("starting", "waiting"):
                return False
            process, thread, timer = self._process, self._thread, self._timer
            self._status.state = "cancelled"
            self._status.detail = "cancelled; no credentials were stored"
        if timer is not None:
            timer.cancel()
        if process is not None:
            with contextlib.suppress(OSError):
                process.terminate()
        if thread is not None:
            thread.join(timeout=5)
        with self._lock:
            self._process = None
            self._thread = None
            self._timer = None
        return True

    def shutdown(self) -> None:
        """Best-effort stop for dashboard exit; never raises."""
        with contextlib.suppress(Exception):
            self.cancel()

    # -- internals ---------------------------------------------------------------

    def _on_timeout(self) -> None:
        with self._lock:
            if self._status.state not in ("starting", "waiting"):
                return
            process = self._process
            self._status.state = "failed"
            self._status.detail = "timed out waiting for the browser sign-in"
        if process is not None:
            with contextlib.suppress(OSError):
                process.terminate()

    def _watch(self) -> None:
        """Read the CLI's output, parse the URL, then save the token on success."""
        process = self._process
        output: list[str] = []
        try:
            assert process is not None and process.stdout is not None
            for line in process.stdout:
                output.append(line)
                url, code_prompt = parse_setup_token_output("".join(output))
                with self._lock:
                    if self._status.state == "cancelled":
                        return
                    if url:
                        self._status.url = url
                        self._status.code_prompt = code_prompt
                        if self._status.state == "starting":
                            self._status.state = "waiting"
                            self._status.detail = (
                                "open the link in your browser, approve the sign-in, "
                                "then paste the browser code below"
                            )
            process.wait()
        except Exception as exc:  # a watcher must not kill the dashboard
            with self._lock:
                if self._status.state != "cancelled":
                    self._status = ClaudeSetupTokenStatus(state="failed", detail=str(exc))
            self._stop_timer()
            return
        self._stop_timer()
        with self._lock:
            if self._status.state == "cancelled":
                return
            token = extract_setup_token("".join(output))
            if token:
                try:
                    if self._write_secret is not None:
                        self._write_secret(CLAUDE_OAUTH_TOKEN_ENV, token)
                    else:
                        from jev_agent.core.secrets import write_secret

                        write_secret(CLAUDE_OAUTH_TOKEN_ENV, token)
                except Exception as exc:
                    self._status = ClaudeSetupTokenStatus(
                        state="failed",
                        detail=f"the sign-in completed but the token could not be saved: {exc}",
                    )
                else:
                    self._status = ClaudeSetupTokenStatus(
                        state="done",
                        detail="signed in with your Claude subscription",
                    )
            else:
                self._status = ClaudeSetupTokenStatus(
                    state="failed",
                    detail=(
                        "the sign-in did not complete — the browser step may not "
                        "have been finished or the code expired"
                    ),
                )
            self._process = None
            self._thread = None

    def _stop_timer(self) -> None:
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
