"""P0 security fixes from the 2026-10-16 code audit (Worker A).

Behavior tests for: the trust-gate keys `autonomy.mode` / `[ship]`, chat-transcript
extras redaction, secrets-file redaction registration, API-worker `with_redact_names`
routing, secrets-file fsync, `new_chat_id` collision retries, and API-worker redirect
following. Each test asserts observable behavior (redacted output, rejected config,
error messages), not implementation.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from jev_agent.config import ConfigError, load_config
from jev_agent.config.schema import McpServerConfig
from jev_agent.core import chat_session as chat_session_module
from jev_agent.core import secrets
from jev_agent.core.chat_backends import BackendName, EchoChatBackend, Reply
from jev_agent.core.chat_learning import ChatLearning
from jev_agent.core.chat_session import (
    TRANSCRIPT_FILE,
    ChatError,
    ChatSession,
    read_transcript,
)
from jev_agent.runlog.redact import REDACT_NAMES_ENV, redact
from jev_agent.workers.api import ApiWorker
from jev_agent.workers.base import WorkerJob
from jev_agent.workers.tools import AgentTools

FIXTURES = Path(__file__).parent.parent / "fixtures" / "openrouter"
KEY_ENV = "JEV_TEST_OPENROUTER_KEY"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def user_cfg(tmp_path: Path) -> Path:
    return tmp_path / "user" / "config.toml"  # not created unless a test writes it


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, "test-key")


# --- 1. trust gate: autonomy.mode and [ship] -------------------------------------------------


@pytest.mark.parametrize(
    ("toml", "key"),
    [
        ('[autonomy]\nmode = "autonomous"\n', "autonomy.mode"),
        ("[ship]\nenabled = true\n", "ship"),
    ],
)
def test_untrusted_repo_cannot_disable_approval_gates(
    repo: Path, user_cfg: Path, toml: str, key: str
) -> None:
    """An untrusted repo's jev.toml may not switch the run to autonomous mode or arm
    PR auto-merge; the error names the key and tells the user how to trust the repo."""
    _write(repo / "jev.toml", toml)
    with pytest.raises(ConfigError) as err:
        load_config(repo, user_cfg)
    message = str(err.value)
    assert key in message
    assert "[trust]" in message
    assert str(repo.resolve()) in message


def test_trusted_repo_may_change_autonomy_mode_and_ship(repo: Path, user_cfg: Path) -> None:
    _write(repo / "jev.toml", '[autonomy]\nmode = "autonomous"\n[ship]\nenabled = true\n')
    _write(user_cfg, f'[trust]\nrepos = ["{repo}"]\n')
    cfg = load_config(repo, user_cfg)
    assert cfg.autonomy.mode == "autonomous"
    assert cfg.ship.enabled is True


def test_untrusted_repo_may_repeat_the_defaults(repo: Path, user_cfg: Path) -> None:
    """Repeating the user's (default) values is not a change, so the gate stays quiet."""
    _write(repo / "jev.toml", '[autonomy]\nmode = "standard"\n[ship]\nenabled = false\n')
    cfg = load_config(repo, user_cfg)
    assert cfg.autonomy.mode == "standard"
    assert cfg.ship.enabled is False


# --- 2. chat transcript: extras are redacted -------------------------------------------------


def _session(tmp_path: Path, **kwargs: Any) -> ChatSession:
    return ChatSession.create(
        tmp_path, EchoChatBackend("api"), "system", ChatLearning(), None, 1.0, **kwargs
    )


def test_record_redacts_string_extras(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.record(
        "event",
        "done",
        run_id="r-1",
        note="leaked sk-or-abcdefghijklmnop in an extra",
        attempt=3,
    )
    entries = read_transcript(tmp_path, session.meta.chat_id)
    entry = entries[-1]
    assert entry["run_id"] == "r-1"  # ordinary strings pass through untouched
    assert entry["attempt"] == 3  # non-strings are never touched
    assert "sk-or-abcdefghijklmnop" not in json.dumps(entry)
    assert entry["note"] == "leaked [REDACTED] in an extra"


class _FailBackend:
    """A backend whose model call always fails, exercising the `say()` error path."""

    name: BackendName = "api"
    model: str | None = None
    keeps_session = False

    async def send(
        self,
        system: str,
        history: Sequence[Any],
        message: str,
        *,
        session_id: str | None,
        budget_left_usd: float,
        log_path: Path,
    ) -> Reply:
        return Reply(False, "boom: upstream exploded")


async def test_say_error_path_stores_no_raw_user_text(tmp_path: Path) -> None:
    """The error path records the user's message as an extra; a pasted secret must not
    persist raw in transcript.jsonl."""
    session = ChatSession.create(tmp_path, _FailBackend(), "system", ChatLearning(), None, 1.0)
    reply = await session.say("please use my key sk-or-abcdefghijklmnop today")
    assert not reply.ok
    entries = read_transcript(tmp_path, session.meta.chat_id)
    errors = [e for e in entries if e["kind"] == "error"]
    assert errors, "expected the failed turn to be recorded"
    dumped = json.dumps(errors)
    assert "sk-or-abcdefghijklmnop" not in dumped
    assert "[REDACTED]" in dumped


# --- 3. secrets-file values are registered for redaction -------------------------------------


@pytest.fixture
def sfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated secrets file; the process-level `_loaded` set and any
    `JEV_REDACT_NAMES` additions are restored afterwards."""
    file = tmp_path / ".jev" / "secrets.env"
    monkeypatch.setenv(secrets.FILE_ENV, str(file))
    secrets._loaded.clear()
    previous = os.environ.get(REDACT_NAMES_ENV)
    yield file
    for name in ("JEV_TEST_PROXY_AUTH", "JEV_TEST_SHORT_AUTH"):
        secrets._loaded.discard(name)
        os.environ.pop(name, None)
    if previous is None:
        os.environ.pop(REDACT_NAMES_ENV, None)
    else:
        os.environ[REDACT_NAMES_ENV] = previous


def _write_secrets_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def test_load_secrets_registers_benign_names_for_redaction(
    sfile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secrets-file value under a benign name (`PROXY_AUTH`) is redacted from logs."""
    _write_secrets_file(sfile, "JEV_TEST_PROXY_AUTH=proxy-hunter2-value\n")
    monkeypatch.delenv("JEV_TEST_PROXY_AUTH", raising=False)
    secrets.load_secrets()
    assert "JEV_TEST_PROXY_AUTH" in os.environ.get(REDACT_NAMES_ENV, "").split(",")
    assert redact("connecting with proxy-hunter2-value now") == ("connecting with [REDACTED] now")


def test_secrets_file_values_get_the_declared_secret_threshold(
    sfile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secrets-file value is a declared secret: the 4-char bar applies, not the 8-char
    bar for coincidental secret-named environment variables."""
    _write_secrets_file(sfile, "JEV_TEST_SHORT_AUTH=abcde\n")  # 5 chars
    monkeypatch.delenv("JEV_TEST_SHORT_AUTH", raising=False)
    secrets.load_secrets()
    assert redact("value abcde here") == "value [REDACTED] here"


def test_short_unregistered_env_values_are_not_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lower threshold must not leak onto ordinary environment variables."""
    monkeypatch.setenv("JEV_TEST_PLAIN_WORD", "abcde")
    monkeypatch.delenv(REDACT_NAMES_ENV, raising=False)
    assert redact("value abcde here") == "value abcde here"


def test_real_environment_wins_and_is_not_registered(
    sfile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only names jev actually injects are registered; a real environment variable keeps
    the previous behavior."""
    _write_secrets_file(sfile, "JEV_TEST_PROXY_AUTH=file-value\n")
    monkeypatch.setenv("JEV_TEST_PROXY_AUTH", "real-value")
    secrets.load_secrets()
    assert "JEV_TEST_PROXY_AUTH" not in os.environ.get(REDACT_NAMES_ENV, "").split(",")
    assert "JEV_TEST_PROXY_AUTH" not in secrets.loaded_names()


def test_write_secret_registers_the_name(sfile: Path) -> None:
    secrets.write_secret("JEV_TEST_PROXY_AUTH", "fresh-secret-value", path=sfile)
    assert "JEV_TEST_PROXY_AUTH" in os.environ.get(REDACT_NAMES_ENV, "").split(",")
    assert redact("using fresh-secret-value") == "using [REDACTED]"


# --- 4. API worker routes its log env through with_redact_names ------------------------------


def _mcp_tools() -> AgentTools:
    return AgentTools(
        mcp={"docs": McpServerConfig(command="docs-mcp", env_from=["JEV_TEST_MCP_DOCS_AUTH"])}
    )


async def test_api_worker_redacts_benign_named_mcp_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A benign-named MCP secret echoed in a provider error body is redacted from the
    API worker's log and summary, like the CLI workers already do."""
    secret = "docs-mcp-secret-value-123"
    monkeypatch.setenv("JEV_TEST_MCP_DOCS_AUTH", secret)
    body = json.dumps({"error": {"message": f"bad request, saw {secret} in a header"}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=body)

    worker = ApiWorker(
        "https://openrouter.test/api/v1",
        KEY_ENV,
        default_model="m",
        transport=httpx.MockTransport(handler),
    )
    job = WorkerJob(
        role="planner",
        prompt="plan it",
        workdir=tmp_path,
        log_path=tmp_path / "api.log",
        tools=_mcp_tools(),
    )
    output = await worker.run(job)
    assert output.result.status == "error"
    log_text = (tmp_path / "api.log").read_text(encoding="utf-8")
    assert secret not in log_text
    assert "[REDACTED]" in log_text
    assert secret not in output.result.summary


# --- 5. secrets file is fsynced before the rename --------------------------------------------


def test_write_secret_fsyncs_before_replace(sfile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    secrets.write_secret("JEV_TEST_PROXY_AUTH", "v1", path=sfile)
    assert synced, "expected os.fsync to be called during the secrets-file write"
    assert sfile.read_text(encoding="utf-8") == "JEV_TEST_PROXY_AUTH=v1\n"


# --- 6. new_chat_id collisions retry instead of merging ---------------------------------------


def test_create_retries_on_chat_id_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _session(tmp_path, chat_id="c-20261016-120000-aaaa")
    ids = iter(["c-20261016-120000-aaaa", "c-20261016-120000-bbbb"])
    monkeypatch.setattr(chat_session_module, "new_chat_id", lambda: next(ids))
    second = _session(tmp_path)
    assert second.meta.chat_id == "c-20261016-120000-bbbb"
    assert second.dir != first.dir
    assert second.dir.is_dir()
    # The first chat is untouched: no silent merge into its directory.
    assert (first.dir / TRANSCRIPT_FILE).exists() is False


def test_create_with_explicit_id_keeps_merge_behavior(tmp_path: Path) -> None:
    """An explicitly chosen id (e.g. the demo seeder's) still reuses its directory."""
    first = _session(tmp_path, chat_id="c-20261016-120000-aaaa")
    second = _session(tmp_path, chat_id="c-20261016-120000-aaaa")
    assert second.dir == first.dir


def test_create_gives_up_after_repeated_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(tmp_path, chat_id="c-20261016-120000-aaaa")
    monkeypatch.setattr(chat_session_module, "new_chat_id", lambda: "c-20261016-120000-aaaa")
    with pytest.raises(ChatError, match="unused chat id"):
        _session(tmp_path)


# --- 7. API worker follows redirects ----------------------------------------------------------


async def test_api_worker_follows_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": str(request.url)})
        return httpx.Response(
            200, content=(FIXTURES / "success_plan.json").read_text(encoding="utf-8")
        )

    worker = ApiWorker(
        "https://openrouter.test/api/v1",
        KEY_ENV,
        default_model="m",
        transport=httpx.MockTransport(handler),
    )
    job = WorkerJob(
        role="planner", prompt="plan it", workdir=tmp_path, log_path=tmp_path / "api.log"
    )
    output = await worker.run(job)
    assert output.result.status == "success"
    assert len(seen) == 2, "expected the client to follow the 302 to the same URL"
