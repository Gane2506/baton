"""`jev chat`'s model backends: the `ChatBackend` protocol, `ApiChatBackend`
(OpenAI-compatible chat completions, by default OpenRouter), `ClaudeChatBackend`
(`claude -p --resume <session>`, read-only tools) and `EchoChatBackend` (a stand-in model
for tests and `JEV_FAKE_WORKERS=1`, which never calls a model or a CLI)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from jev_agent.core.chat_prompts import BRIEF_CLOSE, BRIEF_OPEN, BRIEF_REQUEST
from jev_agent.runlog.redact import REDACTED, redact
from jev_agent.workers.claude_code import classify_claude_code, parse_result, usage_from
from jev_agent.workers.subprocess_runner import CommandRunner, append_log_sections, run_command

BackendName = Literal["api", "claude_code"]
BACKENDS: tuple[BackendName, ...] = ("api", "claude_code")
DEFAULT_TIMEOUT_S = 600


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    text: str


@dataclass
class Reply:
    ok: bool
    text: str  # the model's answer, or a one-line error
    cost_usd: float | None = None
    session_id: str | None = None


class ChatBackend(Protocol):
    name: BackendName
    model: str | None
    # True when the backend keeps the conversation itself (a session id); history is then only
    # sent when there is no session yet.
    keeps_session: bool

    async def send(
        self,
        system: str,
        history: Sequence[Turn],
        message: str,
        *,
        session_id: str | None,
        budget_left_usd: float,
        log_path: Path,
    ) -> Reply: ...


def _history_text(history: Sequence[Turn]) -> str:
    return "\n\n".join(f"{t.role.capitalize()}: {t.text}" for t in history)


class ApiChatBackend:
    """`POST {base_url}/chat/completions` with the whole conversation each turn (stateless)."""

    name: BackendName = "api"
    keeps_session = False

    def __init__(
        self,
        base_url: str,
        api_key_env: str,
        model: str,
        *,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model: str | None = model
        self.timeout_s = timeout_s
        self._transport = transport

    def build_body(self, system: str, history: Sequence[Turn], message: str) -> dict[str, Any]:
        messages = [{"role": "system", "content": system}]
        messages += [{"role": t.role, "content": t.text} for t in history]
        messages.append({"role": "user", "content": message})
        return {"model": self.model, "messages": messages}

    async def send(
        self,
        system: str,
        history: Sequence[Turn],
        message: str,
        *,
        session_id: str | None,
        budget_left_usd: float,
        log_path: Path,
    ) -> Reply:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            return Reply(False, f"{self.api_key_env} is not set")
        body = self.build_body(system, history, message)
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://github.com/Gane2506/baton",
            "X-OpenRouter-Title": "Baton Agent Orchestrator",
        }
        sections = [("request", f"POST {url}\nmodel: {self.model}\nmessage: {message}")]
        try:
            async with httpx.AsyncClient(transport=self._transport) as client:
                response = await client.post(
                    url, json=body, headers=headers, timeout=self.timeout_s
                )
        except httpx.HTTPError as exc:
            reply = Reply(False, f"network error: {type(exc).__name__}")
        else:
            reply = _api_reply(response)
            sections.append(("response", f"HTTP {response.status_code}\n{response.text}"))
        sections.append(("result", f"ok: {reply.ok}\ncost_usd: {reply.cost_usd}"))
        append_log_sections(log_path, [(t, b.replace(key, REDACTED)) for t, b in sections])
        reply.text = redact(reply.text.replace(key, REDACTED))
        return reply


def _api_reply(response: httpx.Response) -> Reply:
    try:
        data: Any = response.json()
    except ValueError:
        data = None
    if not response.is_success:
        message = ""
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            message = str(data["error"].get("message", ""))
        return Reply(False, f"HTTP {response.status_code}: {message or response.reason_phrase}")
    if not isinstance(data, dict):
        return Reply(False, "response body is not a JSON object")
    usage = data.get("usage")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    cost_usd = float(cost) if isinstance(cost, int | float) else None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not isinstance(content, str) or not content.strip():
        return Reply(False, "response has no message content", cost_usd)
    return Reply(True, content, cost_usd)


# What `claude --resume` says when it cannot find the session.
SESSION_GONE = re.compile(r"no conversation found|session[\w ]*not found", re.IGNORECASE)


class ClaudeChatBackend:
    """Claude Code headless: `claude -p --output-format json`, continued with `--resume`.

    Read-only: `--permission-mode dontAsk` with only Read, Glob and Grep allowed and the
    file-writing tools disallowed; the cwd is the repo. The first call sends the instructions
    (and any earlier turns, e.g. after switching backends) on stdin; later calls send just the
    message with `--resume <session_id>`. Each call is capped with `--max-budget-usd` at what
    is left of the chat budget.
    """

    name: BackendName = "claude_code"
    keeps_session = True
    READ_ONLY_TOOLS = ("Read", "Glob", "Grep")
    DENIED_TOOLS = ("Edit", "Write", "NotebookEdit", "Bash")

    def __init__(
        self,
        root: Path,
        command: str = "claude",
        model: str | None = None,
        *,
        bare: bool = False,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        runner: CommandRunner = run_command,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.root = root
        self.command = command
        self.model = model
        self.bare = bare
        self.timeout_s = timeout_s
        self.runner = runner
        self._env = env

    def build_argv(self, session_id: str | None, budget_left_usd: float) -> list[str]:
        argv = [self.command, "-p", "--output-format", "json"]
        if self.bare:
            argv.append("--bare")
        if self.model:
            argv += ["--model", self.model]
        if session_id:
            argv += ["--resume", session_id]
        argv += [
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            " ".join(self.READ_ONLY_TOOLS),
            "--disallowedTools",
            " ".join(self.DENIED_TOOLS),
        ]
        if budget_left_usd > 0:
            # Never pass "0.00": a sub-cent budget would round to zero and the CLI
            # would reject it. A cent is the smallest unit it can express.
            argv += ["--max-budget-usd", f"{max(budget_left_usd, 0.01):.2f}"]
        return argv

    @staticmethod
    def first_prompt(system: str, history: Sequence[Turn], message: str) -> str:
        parts = [system.rstrip()]
        if history:
            parts.append("# Conversation so far\n\n" + _history_text(history))
        parts.append("# The user says\n\n" + message)
        return "\n\n---\n\n".join(parts)

    async def send(
        self,
        system: str,
        history: Sequence[Turn],
        message: str,
        *,
        session_id: str | None,
        budget_left_usd: float,
        log_path: Path,
    ) -> Reply:
        if session_id:
            reply = await self._call(session_id, message, budget_left_usd, log_path)
            if reply.ok or not SESSION_GONE.search(reply.text):
                return reply
            # The session is gone (another machine, cleared cache): start a new one, but keep
            # the cost of the lost call — the attempt was billed even though it failed.
            lost_cost = reply.cost_usd
            reply = await self._call(
                None, self.first_prompt(system, history, message), budget_left_usd, log_path
            )
            if lost_cost is not None:
                reply.cost_usd = (reply.cost_usd or 0.0) + lost_cost
            return reply
        prompt = self.first_prompt(system, history, message)
        return await self._call(None, prompt, budget_left_usd, log_path)

    async def _call(
        self, session_id: str | None, prompt: str, budget_left_usd: float, log_path: Path
    ) -> Reply:
        env = dict(os.environ if self._env is None else self._env)
        argv = self.build_argv(session_id, budget_left_usd)
        try:
            result = await self.runner(
                argv, self.root, env, self.timeout_s, prompt, log_path=log_path
            )
        except OSError as exc:
            return Reply(False, f"could not run {self.command}: {exc}")
        payload = parse_result(result.stdout)
        status = classify_claude_code(result, payload)
        cost, _ = usage_from(payload)
        text = payload.get("result") if payload else None
        new_session = payload.get("session_id") if payload else None
        sid = new_session if isinstance(new_session, str) else session_id
        if status != "success" or not isinstance(text, str):
            detail = text if isinstance(text, str) else (result.stderr.strip() or "no output")
            summary = " ".join(detail.split())[:300]
            return Reply(False, redact(f"claude {status}: {summary}", env), cost, sid)
        return Reply(True, redact(text, env), cost, sid)


class EchoChatBackend:
    """A stand-in model for `JEV_FAKE_WORKERS=1`: echoes messages and writes a fixed brief.
    Never calls a model or a CLI."""

    keeps_session = False

    def __init__(self, name: BackendName, model: str | None = None) -> None:
        self.name: BackendName = name
        self.model = model

    async def send(
        self,
        system: str,
        history: Sequence[Turn],
        message: str,
        *,
        session_id: str | None,
        budget_left_usd: float,
        log_path: Path,
    ) -> Reply:
        if message.endswith(BRIEF_REQUEST):
            ideas = [t.text for t in history if t.role == "user"] or ["(no idea yet)"]
            body = "\n".join(f"- {text}" for text in ideas)
            brief = f"# Fake brief\n\n## Goal\n{ideas[0]}\n\n## Notes\n{body}"
            return Reply(True, f"{BRIEF_OPEN}\n{brief}\n{BRIEF_CLOSE}", 0.0)
        return Reply(True, f"(fake model) You said: {message}", 0.0)


__all__ = [
    "BACKENDS",
    "DEFAULT_TIMEOUT_S",
    "SESSION_GONE",
    "ApiChatBackend",
    "BackendName",
    "ChatBackend",
    "ClaudeChatBackend",
    "EchoChatBackend",
    "Reply",
    "Turn",
    "_api_reply",
    "_history_text",
]
