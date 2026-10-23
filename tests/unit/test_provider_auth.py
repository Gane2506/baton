"""`core.provider_auth`: CLI subscription-login detection and the Codex device flow.

No real CLIs, browsers, or credentials: homes and CLIs are faked, tokens are
obviously-fake strings, and only names / set-not-set are ever asserted."""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jev_agent.core import provider_auth as pa
from jev_agent.workers.base import WorkerJob
from jev_agent.workers.claude_code import ClaudeCodeWorker
from jev_agent.workers.codex import CodexWorker
from jev_agent.workers.subprocess_runner import CommandResult

FAKE_OAUTH = "sk-ant-oat01-fake-token-for-tests"
FAKE_API_KEY = "sk-ant-fake-api-key-for-tests"
FAKE_OPENAI_KEY = "sk-fake-openai-key-for-tests"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def _which(installed: bool) -> Callable[[str], str | None]:
    return (lambda cmd: "/fake/bin/" + cmd) if installed else (lambda cmd: None)


# --- Claude Code precedence ----------------------------------------------------


def test_claude_api_key_outranks_oauth(tmp_path: Path) -> None:
    auth = pa.claude_code_auth(
        env={pa.ANTHROPIC_API_KEY_ENV: FAKE_API_KEY, pa.CLAUDE_OAUTH_TOKEN_ENV: FAKE_OAUTH},
        home=_home(tmp_path),
        which=_which(True),
    )
    assert auth.auth == "api_key"
    assert auth.label == "API key"
    assert pa.ANTHROPIC_API_KEY_ENV in auth.detail


def test_claude_oauth_token(tmp_path: Path) -> None:
    auth = pa.claude_code_auth(
        env={pa.CLAUDE_OAUTH_TOKEN_ENV: FAKE_OAUTH},
        home=_home(tmp_path),
        which=_which(True),
    )
    assert auth.auth == "oauth_token"
    assert auth.label == "Subscription login (OAuth)"
    assert auth.warning is None


def test_claude_oauth_token_bare_mode_warns(tmp_path: Path) -> None:
    auth = pa.claude_code_auth(
        env={pa.CLAUDE_OAUTH_TOKEN_ENV: FAKE_OAUTH},
        home=_home(tmp_path),
        bare=True,
        which=_which(True),
    )
    assert auth.auth == "oauth_token"
    assert auth.warning is not None
    assert pa.ANTHROPIC_API_KEY_ENV in auth.warning


def test_claude_cli_login(tmp_path: Path) -> None:
    home = _home(tmp_path)
    creds = home / ".claude"
    creds.mkdir()
    # Only key presence is read; the file holds no real credential here.
    (creds / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "fake"}}), encoding="utf-8"
    )
    auth = pa.claude_code_auth(env={}, home=home, which=_which(True))
    assert auth.auth == "cli_login"
    assert auth.label == "Subscription login (OAuth)"


def test_claude_cli_login_bare_mode_warns(tmp_path: Path) -> None:
    home = _home(tmp_path)
    creds = home / ".claude"
    creds.mkdir()
    (creds / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "fake"}}), encoding="utf-8"
    )
    auth = pa.claude_code_auth(env={}, home=home, bare=True, which=_which(True))
    assert auth.auth == "cli_login"
    assert auth.warning is not None


def test_claude_nothing(tmp_path: Path) -> None:
    auth = pa.claude_code_auth(env={}, home=_home(tmp_path), which=_which(False))
    assert auth.auth == "none"
    assert auth.installed is False
    assert "not on PATH" in auth.detail


def test_claude_never_returns_values(tmp_path: Path) -> None:
    auth = pa.claude_code_auth(
        env={pa.CLAUDE_OAUTH_TOKEN_ENV: FAKE_OAUTH},
        home=_home(tmp_path),
        which=_which(True),
    )
    for text in (auth.label, auth.detail, auth.warning or ""):
        assert FAKE_OAUTH not in text


# --- Codex precedence ----------------------------------------------------------


def _codex_home(tmp_path: Path, auth_json: dict[str, Any] | None) -> Path:
    home = _home(tmp_path)
    if auth_json is not None:
        codex = home / ".codex"
        codex.mkdir()
        (codex / "auth.json").write_text(json.dumps(auth_json), encoding="utf-8")
    return home


def test_codex_env_api_key_first(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, {"tokens": {"access_token": "fake"}})
    auth = pa.codex_auth(
        env={pa.OPENAI_API_KEY_ENV: FAKE_OPENAI_KEY}, home=home, which=_which(True)
    )
    assert auth.auth == "api_key"
    assert pa.OPENAI_API_KEY_ENV in auth.detail


def test_codex_chatgpt_oauth(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, {"tokens": {"access_token": "fake"}})
    auth = pa.codex_auth(env={}, home=home, which=_which(True))
    assert auth.auth == "oauth_token"
    assert auth.label == "Subscription login (OAuth)"
    assert "ChatGPT" in auth.detail


def test_codex_stored_api_key(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, {"api_key": "fake"})
    auth = pa.codex_auth(env={}, home=home, which=_which(True))
    assert auth.auth == "api_key"
    assert "codex login" in auth.detail


def test_codex_nothing(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, None)
    auth = pa.codex_auth(env={}, home=home, which=_which(True))
    assert auth.auth == "none"
    assert auth.label == "Not connected"


def test_codex_malformed_auth_json_is_nothing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    codex = home / ".codex"
    codex.mkdir()
    (codex / "auth.json").write_text("not json", encoding="utf-8")
    assert pa.codex_auth(env={}, home=home, which=_which(True)).auth == "none"


# --- token validation + device output parsing ----------------------------------


def test_check_claude_oauth_token_ok() -> None:
    pa.check_claude_oauth_token(FAKE_OAUTH)  # must not raise


def test_check_claude_oauth_token_rejects() -> None:
    with pytest.raises(ValueError):
        pa.check_claude_oauth_token("")
    with pytest.raises(ValueError):
        pa.check_claude_oauth_token("   ")
    with pytest.raises(ValueError, match="sk-ant-oat01-"):
        pa.check_claude_oauth_token("sk-ant-api03-fake")  # an API key, wrong field


def test_parse_device_output() -> None:
    out = (
        "Welcome to Codex\n"
        "1. Open this link in your browser\n"
        "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n"
        "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n"
        "   \x1b[94mABCD-12EFG\x1b[0m\n"
    )
    assert pa.parse_device_output(out) == ("https://auth.openai.com/codex/device", "ABCD-12EFG")


def test_parse_device_output_partial() -> None:
    assert pa.parse_device_output("Welcome to Codex\n") == (None, None)
    url, code = pa.parse_device_output("go to https://auth.openai.com/codex/device now\n")
    assert url == "https://auth.openai.com/codex/device"
    assert code is None


# --- the device flow runner ----------------------------------------------------


class FakeProcess:
    """A stand-in for the `codex login --device-auth` child.

    `wait()` blocks until the test releases it, so the watcher stays in the
    "waiting" state while assertions run — like the real CLI polling for the
    browser approval.
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.stdout = iter(lines)
        self.terminated = False
        self.release = threading.Event()

    def wait(self) -> int:
        self.release.wait(timeout=30)
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.release.set()


def _wait_for(flow: pa.CodexDeviceFlow, state: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while flow.status().state != state:
        assert time.monotonic() < deadline, f"never reached {state}"
        time.sleep(0.02)


def _device_lines(code: str = "ABCD-12EFG") -> list[str]:
    return [
        "Welcome to Codex\n",
        "1. Open this link in your browser\n",
        f"   {pa.CODEX_DEVICE_URL}\n",
        "2. Enter this one-time code\n",
        f"   {code}\n",
    ]


def _popen_fake(procs: list[FakeProcess]) -> Callable[..., FakeProcess]:
    """A `popen` stand-in that records the fake child it spawns."""

    def _popen(*args: Any, **kwargs: Any) -> FakeProcess:
        proc = FakeProcess(_device_lines())
        procs.append(proc)
        return proc

    return _popen


def test_device_flow_waiting_then_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _codex_home(tmp_path, None)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/bin/codex")
    procs: list[FakeProcess] = []
    flow = pa.CodexDeviceFlow(popen=_popen_fake(procs))
    flow.start(command="codex", home=home)
    try:
        _wait_for(flow, "waiting")
        status = flow.status()
        assert status.url == pa.CODEX_DEVICE_URL
        assert status.code == "ABCD-12EFG"
        # The user approves in the browser; the CLI stores its own auth.json.
        (home / ".codex").mkdir(exist_ok=True)
        (home / ".codex" / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": "fake"}}), encoding="utf-8"
        )
        procs[0].release.set()
        _wait_for(flow, "done")
        assert flow.status().detail  # names only
    finally:
        procs[0].release.set()
        flow.shutdown()


def test_device_flow_fails_without_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _codex_home(tmp_path, None)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/bin/codex")
    procs: list[FakeProcess] = []
    flow = pa.CodexDeviceFlow(popen=_popen_fake(procs))
    try:
        flow.start(command="codex", home=home)
        _wait_for(flow, "waiting")
        procs[0].release.set()  # process exits, no auth.json appeared
        _wait_for(flow, "failed")
        assert "did not complete" in flow.status().detail
    finally:
        procs[0].release.set()
        flow.shutdown()


def test_device_flow_busy_and_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _codex_home(tmp_path, None)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/bin/codex")
    procs: list[FakeProcess] = []
    flow = pa.CodexDeviceFlow(popen=_popen_fake(procs))
    try:
        flow.start(command="codex", home=home)
        _wait_for(flow, "waiting")
        with pytest.raises(pa.DeviceFlowBusy):
            flow.start(command="codex", home=home)
        assert flow.cancel() is True
        assert flow.status().state == "cancelled"
        assert procs[0].terminated
        assert flow.cancel() is False  # nothing left to cancel
    finally:
        procs[0].release.set()
        flow.shutdown()


def test_device_flow_needs_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    flow = pa.CodexDeviceFlow(popen=lambda *a, **k: FakeProcess([]))
    with pytest.raises(pa.DeviceFlowError, match="not on PATH"):
        flow.start(command="codex")
    assert flow.status().state == "idle"


# --- pipeline: auth present -> the worker's env has it --------------------------


@dataclass
class _Call:
    argv: list[str]
    env: dict[str, str]


@dataclass
class _FakeRunner:
    calls: list[_Call] = field(default_factory=list)

    async def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        stdin_text: str | None = None,
        *,
        log_path: Path | None = None,
    ) -> CommandResult:
        self.calls.append(_Call(list(argv), dict(env)))
        return CommandResult(1, "", "fake failure")


def _job(tmp_path: Path, name: str) -> WorkerJob:
    workdir = tmp_path / name
    workdir.mkdir()
    (workdir / ".git").mkdir()  # enough for in_git_repo; no git needed
    (workdir / "logs").mkdir()
    return WorkerJob(
        role="reviewer",
        prompt="PROMPT",
        workdir=workdir,
        log_path=workdir / "logs" / f"{name}.log",
    )


async def test_codex_worker_sees_oauth_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The subscription token in the environment reaches the `codex` child."""
    monkeypatch.setenv(pa.OPENAI_API_KEY_ENV, FAKE_OPENAI_KEY)
    runner = _FakeRunner()
    worker = CodexWorker(runner=runner, env=None)  # env=None: inherit os.environ
    await worker.run(_job(tmp_path, "codex-job"))
    assert runner.calls
    assert pa.OPENAI_API_KEY_ENV in runner.calls[0].env


async def test_claude_worker_sees_oauth_token_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `claude setup-token` value reaches the `claude` child."""
    monkeypatch.setenv(pa.CLAUDE_OAUTH_TOKEN_ENV, FAKE_OAUTH)
    runner = _FakeRunner()
    worker = ClaudeCodeWorker(runner=runner, env=None)
    await worker.run(_job(tmp_path, "claude-job"))
    assert runner.calls
    assert pa.CLAUDE_OAUTH_TOKEN_ENV in runner.calls[0].env


async def test_claude_worker_sees_api_key_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(pa.ANTHROPIC_API_KEY_ENV, FAKE_API_KEY)
    runner = _FakeRunner()
    worker = ClaudeCodeWorker(runner=runner, env=None)
    await worker.run(_job(tmp_path, "claude-key-job"))
    assert runner.calls
    assert pa.ANTHROPIC_API_KEY_ENV in runner.calls[0].env


# --- `claude setup-token` output parsing ---------------------------------------


def test_parse_setup_token_output_url_and_prompt() -> None:
    text = (
        "To authenticate, visit:\n"
        "https://claude.ai/oauth/authorize?code=true&client_id=abc123\n"
        "After approval, paste the code shown:\n"
        "> "
    )
    url, code_prompt = pa.parse_setup_token_output(text)
    assert url == "https://claude.ai/oauth/authorize?code=true&client_id=abc123"
    assert code_prompt is True


def test_parse_setup_token_output_url_before_prompt() -> None:
    text = "Visit https://claude.ai/oauth/authorize?code=true in your browser\n"
    url, code_prompt = pa.parse_setup_token_output(text)
    assert url is not None and "claude.ai/oauth/authorize" in url
    assert code_prompt is False


def test_parse_setup_token_output_empty() -> None:
    assert pa.parse_setup_token_output("") == (None, False)
    assert pa.parse_setup_token_output("some unrelated output\n") == (None, False)


def test_parse_setup_token_output_strips_ansi() -> None:
    text = "\x1b[1mhttps://claude.ai/oauth/authorize?code=true\x1b[0m\npaste the code:\n"
    url, code_prompt = pa.parse_setup_token_output(text)
    assert url == "https://claude.ai/oauth/authorize?code=true"
    assert code_prompt is True


def test_extract_setup_token() -> None:
    text = "Token issued (valid 1 year):\nsk-ant-oat01-abcDEF123-_xyz\n"
    assert pa.extract_setup_token(text) == "sk-ant-oat01-abcDEF123-_xyz"


def test_extract_setup_token_none() -> None:
    assert pa.extract_setup_token("no token here\n") is None
    assert pa.extract_setup_token("") is None


def test_extract_setup_token_ignores_api_keys() -> None:
    # An Anthropic API key must not match the setup-token pattern.
    assert pa.extract_setup_token("sk-ant-api03-abc123\n") is None


# --- the setup-token flow runner -----------------------------------------------


class FakeSetupTokenProcess:
    """A stand-in for the `claude setup-token` child.

    Yields the initial lines (URL + prompt), then blocks like the real CLI
    waiting for the browser code on stdin. When the test's `stdin` receives a
    code, the generator yields the token lines and ends; `wait()` blocks until
    `release` is set, like the process exiting. `simulate_exit()` ends the
    generator without a token, like the CLI dying before completion.
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._code_received = threading.Event()
        self._stop = threading.Event()
        self._token: str | None = None
        self.stdout = self._generate()
        self.stdin_writes: list[str] = []
        self.stdin = self._FakeStdin(self)
        self.terminated = False
        self.release = threading.Event()

    class _FakeStdin:
        def __init__(self, proc: FakeSetupTokenProcess) -> None:
            self._proc = proc

        def write(self, data: str) -> int:
            self._proc.stdin_writes.append(data)
            # Like the real CLI: a code on stdin unblocks it to print the token.
            self._proc._token = "sk-ant-oat01-faketoken123"
            self._proc._code_received.set()
            return len(data)

        def flush(self) -> None:
            pass

    def _generate(self) -> Iterator[str]:
        yield from self._lines
        while not self._stop.is_set():
            if self._code_received.wait(timeout=0.05):
                break
        if self._token and not self._stop.is_set():
            yield "Token issued (valid 1 year):\n"
            yield f"{self._token}\n"

    def wait(self) -> int:
        self.release.wait(timeout=30)
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self._stop.set()
        self._code_received.set()
        self.release.set()

    def simulate_exit(self) -> None:
        """The CLI exits on its own without printing a token."""
        self._stop.set()
        self._code_received.set()
        self.release.set()


def _wait_for_claude(flow: pa.ClaudeSetupTokenFlow, state: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while flow.status().state != state:
        assert time.monotonic() < deadline, f"never reached {state}"
        time.sleep(0.02)


def _wait_for_claude_status(
    flow: pa.ClaudeSetupTokenFlow,
    predicate: Callable[[pa.ClaudeSetupTokenStatus], bool],
    timeout_s: float = 10.0,
) -> pa.ClaudeSetupTokenStatus:
    """Poll until ``predicate(status)`` holds; return the settled status.

    The watcher's state flip and its field updates (url, code_prompt) land in
    separate loop iterations, so asserting on fields right after the state
    changes is a thread-timing race. Wait for the full expected condition.
    """
    deadline = time.monotonic() + timeout_s
    status = flow.status()
    while not predicate(status):
        assert time.monotonic() < deadline, "timed out waiting for flow status"
        time.sleep(0.02)
        status = flow.status()
    return status


def _setup_token_lines() -> list[str]:
    return [
        "To authenticate, visit:\n",
        "https://claude.ai/oauth/authorize?code=true&client_id=abc123\n",
        "After approval, paste the code shown:\n",
        "> ",
    ]


def _popen_setup_token_fake(
    procs: list[FakeSetupTokenProcess],
) -> Callable[..., FakeSetupTokenProcess]:
    def _popen(*args: Any, **kwargs: Any) -> FakeSetupTokenProcess:
        proc = FakeSetupTokenProcess(_setup_token_lines())
        procs.append(proc)
        return proc

    return _popen


def test_setup_token_flow_waiting_then_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/bin/claude")
    procs: list[FakeSetupTokenProcess] = []
    saved: dict[str, str] = {}
    flow = pa.ClaudeSetupTokenFlow(
        popen=_popen_setup_token_fake(procs),
        write_secret=lambda name, value: saved.update({name: value}),
    )
    try:
        flow.start(command="claude", timeout_s=30.0)
        # The URL line flips the state to "waiting" before the "> " prompt line
        # lands, so wait for the full settled condition, not just the state.
        _wait_for_claude_status(
            flow,
            lambda s: (
                s.state == "waiting"
                and s.url is not None
                and "claude.ai/oauth/authorize" in s.url
                and s.code_prompt is True
            ),
        )
        # The user approves in the browser and pastes the browser code.
        # Retry briefly: the watcher thread may still be settling into "waiting".
        submitted = False
        deadline = time.monotonic() + 5.0
        while not submitted and time.monotonic() < deadline:
            submitted = flow.submit_code("browser-code-123")
            if not submitted:
                time.sleep(0.02)
        assert submitted is True
        assert procs[0].stdin_writes == ["browser-code-123\n"]
        # The CLI prints the token; the flow saves it and reports done.
        procs[0].release.set()  # let wait() return after the token lines
        _wait_for_claude(flow, "done")
        assert saved == {pa.CLAUDE_OAUTH_TOKEN_ENV: "sk-ant-oat01-faketoken123"}
        assert "token" not in flow.status().detail.lower()  # names only
    finally:
        procs[0].release.set() if procs else None
        flow.shutdown()


def test_setup_token_flow_fails_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/bin/claude")
    procs: list[FakeSetupTokenProcess] = []
    flow = pa.ClaudeSetupTokenFlow(popen=_popen_setup_token_fake(procs))
    try:
        flow.start(command="claude", timeout_s=30.0)
        _wait_for_claude(flow, "waiting")
        procs[0].simulate_exit()  # process exits, no token printed
        _wait_for_claude(flow, "failed")
        assert "did not complete" in flow.status().detail
    finally:
        procs[0].release.set() if procs else None
        flow.shutdown()


def test_setup_token_flow_busy_and_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/bin/claude")
    procs: list[FakeSetupTokenProcess] = []
    flow = pa.ClaudeSetupTokenFlow(popen=_popen_setup_token_fake(procs))
    try:
        flow.start(command="claude", timeout_s=30.0)
        _wait_for_claude(flow, "waiting")
        with pytest.raises(pa.DeviceFlowBusy):
            flow.start(command="claude")
        assert flow.cancel() is True
        assert flow.status().state == "cancelled"
        assert procs[0].terminated
        assert flow.cancel() is False  # nothing left to cancel
        assert flow.submit_code("late-code") is False
    finally:
        procs[0].release.set() if procs else None
        flow.shutdown()


def test_setup_token_flow_needs_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    flow = pa.ClaudeSetupTokenFlow(popen=lambda *a, **k: FakeSetupTokenProcess([]))
    with pytest.raises(pa.DeviceFlowError, match="not on PATH"):
        flow.start(command="claude")
    assert flow.status().state == "idle"


def test_setup_token_flow_rejects_empty_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/bin/claude")
    procs: list[FakeSetupTokenProcess] = []
    flow = pa.ClaudeSetupTokenFlow(popen=_popen_setup_token_fake(procs))
    try:
        flow.start(command="claude", timeout_s=30.0)
        _wait_for_claude(flow, "waiting")
        assert flow.submit_code("") is False
        assert flow.submit_code("   ") is False
        assert procs[0].stdin_writes == []
    finally:
        procs[0].release.set() if procs else None
        flow.shutdown()
