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

Issue #396 adds one resolution function, `resolve_credentials`, that decides
for every combination of credentials which one workers use, which are
present, and which one usage is measured against (design §6.2 tables are its
test list). `get_login_source` is the single credential-source accessor both
detection and the usage adapters use. Labels follow D1: a dashboard-saved
OAuth token is "Signed in with your Claude subscription (saved by Baton)";
a CLI login is "Detected on this device".
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jev_agent.app.errors import JevError
from jev_agent.core.executables import resolve_executable

if TYPE_CHECKING:
    from jev_agent.core.usage import UsageSnapshot, UsageUnavailable

__all__ = [
    "ANTHROPIC_API_KEY_ENV",
    "CLAUDE_OAUTH_TOKEN_ENV",
    "CLAUDE_SETUP_TOKEN_PREFIX",
    "CODEX_DEVICE_URL",
    "OPENAI_API_KEY_ENV",
    "ClaudeSetupTokenFlow",
    "CodexDeviceFlow",
    "CredentialResolution",
    "DeviceFlowBusy",
    "DeviceFlowError",
    "DeviceFlowStatus",
    "LoginSource",
    "ProviderAuth",
    "check_claude_oauth_token",
    "claude_code_auth",
    "cli_version",
    "codex_auth",
    "extract_setup_token",
    "get_login_source",
    "parse_device_output",
    "parse_setup_token_output",
    "read_credential_file",
    "resolve_credentials",
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
    label: str  # design-verbatim auth description (D1), e.g. the long
    # "Detected on this device — the `codex` CLI is signed in with ChatGPT"
    detail: str  # one line on where it was found, names only
    warning: str | None = None  # config/auth mismatch worth surfacing
    # Issue #396: the detected login's source (set by `resolve_credentials`),
    # the cached usage snapshot for the card (set by `views.cli_providers`),
    # and whether usage checks are opted in for this provider (the cache
    # record's `enabled` flag; None when no record exists). All additive
    # with defaults, so existing constructions do not break.
    cli_version: str | None = None
    usage: UsageSnapshot | UsageUnavailable | None = None
    present: tuple[str, ...] = ()  # every present credential kind, precedence order
    source_path: str | None = None  # credential file path, when the login comes from a file
    usage_enabled: bool | None = None

    @property
    def badge_label(self) -> str:
        """Short auth-state text for the panel-head badge (§10 gate fix).

        The D1 `label` is design-verbatim and can be a full sentence
        ("Detected on this device — ..."); a non-wrapping `.badge` pill
        renders it 417px wide and overflows the card at 390px. The badge
        carries the short state; the full label still renders verbatim
        as plain text in the card body (the "Set" line), so nothing is
        lost. Short labels pass through unchanged.
        """
        if self.auth in (_AUTH_OAUTH_TOKEN, _AUTH_CLI_LOGIN):
            return "Signed in"
        return self.label


@dataclass(frozen=True)
class LoginSource:
    """One credential a CLI worker could use, and where it came from.

    `kind` is one of "env_api_key" | "env_oauth_token" | "saved_oauth_token" |
    "cli_login" | "api_key_file" | "none" ("keychain_login" arrives in Phase B).
    `source_path` is the credential file path when the login comes from a file
    (None for environment kinds and "none"). `cli_version` is the provider
    CLI's version (one `<cmd> --version` run per dashboard process start,
    cached; None on failure) — the same value on every source for a provider.
    """

    kind: str
    source_path: str | None
    cli_version: str | None


@dataclass(frozen=True)
class CredentialResolution:
    """Which credential workers use, which are present, and what usage is
    measured against — always the effective credential (design §6.2)."""

    effective: LoginSource  # the credential workers use
    present: tuple[LoginSource, ...]  # every kind present, top-to-bottom precedence order
    # None when usage is hidden (API key in effect, bare-mode CLI login, or no login).
    usage_source: LoginSource | None


_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+._A-Za-z0-9]*)?")
_CLI_VERSION_CACHE: dict[str, str | None] = {}  # command -> version (None = lookup failed)


def cli_version(
    command: str, *, which: Callable[[str], str | None] = resolve_executable
) -> str | None:
    """The CLI's version string ("0.167.0", "2.1.318"), or None.

    One `<cmd> --version` run per dashboard process start per command (5 s
    timeout), cached in memory; None when the binary is missing, the run
    fails, or no version-like token appears. Never raises.
    """
    if command in _CLI_VERSION_CACHE:
        return _CLI_VERSION_CACHE[command]
    version: str | None = None
    resolved = which(command)
    if resolved is not None:
        try:
            result = subprocess.run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            version = None
        else:
            match = _VERSION.search(result.stdout or "")
            version = match.group(0) if match else None
    _CLI_VERSION_CACHE[command] = version
    return version


def read_credential_file(path: Path) -> dict[str, Any] | None:
    """Parse a CLI credential JSON file. Returns the parsed object, or None
    when the file is missing, unreadable, invalid JSON, or not an object.
    Never raises. This is the one shared credential-file reader (§6.1):
    detection and the usage adapters both go through it."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _credentials_login(home: Path) -> bool:
    """The `claude` CLI's own interactive subscription login is present."""
    data = read_credential_file(home / ".claude" / ".credentials.json")
    return data is not None and bool(data.get("claudeAiOauth"))


def _present_sources(
    provider: str, env: Mapping[str, str], home: Path, version: str | None
) -> list[LoginSource]:
    """Every credential kind present for `provider`, top-to-bottom precedence
    order (design §6.2). Bare mode does not change presence — only which
    credential is effective (see `resolve_credentials`)."""
    if provider == "claude_code":
        from jev_agent.core.secrets import default_path, loaded_names

        sources: list[LoginSource] = []
        if env.get(ANTHROPIC_API_KEY_ENV, "").strip():
            sources.append(LoginSource("env_api_key", None, version))
        if env.get(CLAUDE_OAUTH_TOKEN_ENV, "").strip():
            if CLAUDE_OAUTH_TOKEN_ENV in loaded_names():
                sources.append(LoginSource("saved_oauth_token", str(default_path()), version))
            else:
                sources.append(LoginSource("env_oauth_token", None, version))
        if _credentials_login(home):
            sources.append(
                LoginSource("cli_login", str(home / ".claude" / ".credentials.json"), version)
            )
        return sources
    if provider == "codex":
        sources = []
        if env.get(OPENAI_API_KEY_ENV, "").strip():
            sources.append(LoginSource("env_api_key", None, version))
        data = read_credential_file(home / ".codex" / "auth.json")
        if data is not None:
            path = str(home / ".codex" / "auth.json")
            if data.get("tokens"):
                sources.append(LoginSource("cli_login", path, version))
            if data.get("api_key"):
                sources.append(LoginSource("api_key_file", path, version))
        return sources
    raise ValueError(f"unknown provider {provider!r}")


def get_login_source(
    provider: str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    command: str | None = None,
    which: Callable[[str], str | None] = resolve_executable,
) -> LoginSource:
    """The highest-precedence credential present for `provider` (§6.1).

    The single place a storage move is fixed. `env` defaults to
    `os.environ`, `home` to `Path.home()`; tests pass fakes for both.
    Bare mode is not considered here — this is presence, not
    effectiveness (see `resolve_credentials`). Phase A is file-only for
    Claude: a login that lives only in the Mac's Keychain is not detected
    yet (the `keychain_login` kind lands in Phase B).
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    home = home or Path.home()
    cmd = command or ("claude" if provider == "claude_code" else "codex")
    version = cli_version(cmd, which=which)
    sources = _present_sources(provider, environ, home, version)
    if sources:
        return sources[0]
    return LoginSource("none", None, version)


def resolve_credentials(
    provider: str,
    *,
    env: Mapping[str, str],
    home: Path,
    bare: bool = False,
    command: str | None = None,
    which: Callable[[str], str | None] = resolve_executable,
) -> CredentialResolution:
    """Decide, for every combination of credentials, which credential workers
    use, which are present, and which credential usage is measured against —
    always the effective credential (design §6.2 tables are the test list).

    Precedence, top to bottom: API key > saved/env OAuth token > CLI login
    (Claude); API key > `tokens` > `api_key` (Codex, within `auth.json`
    `tokens` wins over `api_key`). In bare mode a CLI login is not usable
    by workers, so the effective credential is "none" and usage is
    suppressed; a saved token or API key is the working path in bare mode
    and usage is shown against it (D11). Usage is hidden (None) when an
    API key is in effect, for a bare-mode CLI login, or when there is no
    login (D12).
    """
    cmd = command or ("claude" if provider == "claude_code" else "codex")
    version = cli_version(cmd, which=which)
    present = _present_sources(provider, env, home, version)
    effective = present[0] if present else LoginSource("none", None, version)
    if provider == "claude_code" and bare and effective.kind == "cli_login":
        effective = LoginSource("none", None, version)
    usage_source = None if effective.kind in ("env_api_key", "api_key_file", "none") else effective
    return CredentialResolution(
        effective=effective, present=tuple(present), usage_source=usage_source
    )


def claude_code_auth(
    *,
    command: str = "claude",
    bare: bool = False,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    which: Callable[[str], str | None] = resolve_executable,
) -> ProviderAuth:
    """Which auth the `claude` CLI worker will use, in Claude Code's precedence.

    Labels are the D1 wording (issue #396): a dashboard-saved OAuth token is
    "Signed in with your Claude subscription (saved by Baton)", a CLI login is
    "Detected on this device". A saved token is the working path in bare mode
    (D11, §6.2 row 4) — only a CLI login is unusable there, so only that
    combination warns.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    home = home or Path.home()
    installed = which(command) is not None
    resolution = resolve_credentials(
        "claude_code", env=environ, home=home, bare=bare, command=command, which=which
    )
    effective = resolution.effective
    present_kinds = {s.kind for s in resolution.present}
    warning = None
    if effective.kind == "env_api_key":
        auth, label = _AUTH_API_KEY, "API key"
        detail = f"{ANTHROPIC_API_KEY_ENV} is set (outranks the subscription login)"
        if "cli_login" in present_kinds:
            detail += "; a subscription login is also present"
    elif effective.kind == "saved_oauth_token":
        auth, label = _AUTH_OAUTH_TOKEN, "Signed in with your Claude subscription (saved by Baton)"
        detail = f"{CLAUDE_OAUTH_TOKEN_ENV} is set (long-lived token from `claude setup-token`)"
    elif effective.kind == "env_oauth_token":
        auth, label = _AUTH_OAUTH_TOKEN, "Subscription login (OAuth)"
        detail = f"{CLAUDE_OAUTH_TOKEN_ENV} is set (long-lived token from `claude setup-token`)"
    elif effective.kind == "cli_login":
        auth, label = _AUTH_CLI_LOGIN, "Detected on this device — the `claude` CLI is signed in"
        detail = (
            "the `claude` CLI is signed in with your subscription "
            f"(credentials in {effective.source_path})"
        )
    else:
        auth, label = _AUTH_NONE, "Not connected"
        if bare and "cli_login" in present_kinds:
            login = next(s for s in resolution.present if s.kind == "cli_login")
            detail = (
                "the `claude` CLI is signed in "
                f"(credentials in {login.source_path}), but `--bare` mode "
                "does not read the CLI subscription login"
            )
            warning = (
                "`--bare` mode does not read the CLI subscription login; "
                f"set {ANTHROPIC_API_KEY_ENV} for bare mode"
            )
        else:
            detail = (
                "run `claude setup-token` and paste the token, or sign in with the `claude` CLI"
            )
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
        cli_version=effective.cli_version,
        present=tuple(s.kind for s in resolution.present),
        source_path=effective.source_path,
    )


def codex_auth(
    *,
    command: str = "codex",
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    which: Callable[[str], str | None] = resolve_executable,
) -> ProviderAuth:
    """Which auth the `codex` CLI worker will use."""
    environ: Mapping[str, str] = os.environ if env is None else env
    home = home or Path.home()
    installed = which(command) is not None
    resolution = resolve_credentials("codex", env=environ, home=home, command=command, which=which)
    effective = resolution.effective
    present_kinds = {s.kind for s in resolution.present}
    if effective.kind == "env_api_key":
        auth, label = _AUTH_API_KEY, "API key"
        detail = f"{OPENAI_API_KEY_ENV} is set"
        if "cli_login" in present_kinds:
            detail += "; a subscription login is also present"
    elif effective.kind == "cli_login":
        auth, label = (
            _AUTH_OAUTH_TOKEN,
            ("Detected on this device — the `codex` CLI is signed in with ChatGPT"),
        )
        detail = f"signed in with ChatGPT (`codex login`; credentials in {effective.source_path})"
    elif effective.kind == "api_key_file":
        auth, label = _AUTH_API_KEY, "API key"
        detail = (
            "API key stored by `codex login --with-api-key` "
            f"(credentials in {effective.source_path})"
        )
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
        cli_version=effective.cli_version,
        present=tuple(s.kind for s in resolution.present),
        source_path=effective.source_path,
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
            if resolve_executable(command) is None:
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
            if process is None or process.stdout is None:
                raise DeviceFlowError(
                    "codex sign-in ended before its output could be read; "
                    "try signing in again from the Connections page"
                )
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
            if resolve_executable(command) is None:
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
            if process is None or process.stdout is None:
                raise DeviceFlowError(
                    "claude sign-in ended before its output could be read; "
                    "try signing in again from the Connections page"
                )
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
