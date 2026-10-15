"""`CodexWorker`: runs Codex CLI headless (`codex exec`) as a subprocess (PLAN.md section 6,
docs/verification.md section 3, checked against codex-cli 0.162.1).

Command shape (prompt on stdin via the `-` argument):

    codex exec --json --ephemeral -C <workdir> -s <sandbox> -c approval_policy="never"
        [-m M] [-c model_reasoning_effort="E"] [--output-schema <tmp>/schema.json]
        -o <tmp>/last_message.txt [--skip-git-repo-check] [extra args...] -

- coder: `-s workspace-write` by default (the constructor's `sandbox`); every other role
  `-s read-only`. Approvals are off (`approval_policy="never"`), so the sandbox is the limit.
  `--full-auto` and `--ask-for-approval` are not used: neither is an `exec` flag in 0.162.1.
- `--skip-git-repo-check` only when the workdir is not inside a git repository.
- Schema and last-message files live in a temporary directory outside the workdir, so they never
  show up in the coder's `git status`.

Agent tools (`job.tools`, docs/verification.md section 6): each MCP server becomes
`-c mcp_servers.<name>.<key>=<TOML>` overrides (`command`, `args`, `env_vars` for a local server;
`url`, `bearer_token_env_var`, `env_http_headers` for a remote one). Only env var NAMES go on the
command line; Codex reads the values itself. Codex has no per-run way to add skills or plugins
(skills are discovered from `.agents/skills` folders, plugins are installed), and only
streamable-HTTP remote servers, so those are left out with a note in the log. Servers from your
own `~/.codex/config.toml` still load: Codex has no `--strict-mcp-config`.

Codex reports tokens but no cost, so `cost_usd` is always None. Status comes from
`classify_codex` over the JSONL events.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jev_agent.config.schema import WORKER_EFFORTS
from jev_agent.core.models import WorkerStatus
from jev_agent.workers.base import WorkerJob, WorkerOutput
from jev_agent.workers.structured import normalize_quotes, parse_json_lines, strict_json_schema
from jev_agent.workers.subprocess_runner import (
    EXIT_NOT_EXECUTABLE,
    EXIT_NOT_FOUND,
    CommandResult,
    CommandRunner,
    append_log_sections,
    run_command,
)
from jev_agent.workers.summary import finish_reply, make_output
from jev_agent.workers.tools import AgentTools, with_redact_names

EFFORTS = WORKER_EFFORTS["codex"]  # other levels are ignored (with a note)
READ_ONLY_SANDBOX = "read-only"
SANDBOXES = frozenset({"read-only", "workspace-write", "danger-full-access"})
SCHEMA_FILE = "schema.json"
LAST_MESSAGE_FILE = "last_message.txt"

# `UsageLimitReachedError` texts in codex-rs/protocol/src/error.rs, after quote normalisation.
QUOTA_PATTERN = re.compile(
    r"you've hit your usage limit|usage_limit_reached|out of credits|spend cap"
    r"|quota exceeded|insufficient_quota",
    re.IGNORECASE,
)
AUTH_PATTERN = re.compile(
    r"not logged in|codex login|invalid api key|incorrect api key|\b401\b|\bunauthorized\b"
    r"|authentication (?:failed|required|error)|token (?:has )?expired",
    re.IGNORECASE,
)


class CodexWorker:
    """Worker for Codex CLI. Never raises for agent failures; see `classify_codex`."""

    name = "codex"

    def __init__(
        self,
        command: str = "codex",
        runner: CommandRunner = run_command,
        *,
        sandbox: str = "workspace-write",
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> None:
        if sandbox not in SANDBOXES:
            raise ValueError(f"unknown codex sandbox {sandbox!r}; use one of {sorted(SANDBOXES)}")
        self.command = command
        self.runner = runner
        self.sandbox = sandbox
        self.extra_args = tuple(extra_args)
        self._env = env

    def build_argv(self, job: WorkerJob, tmp_dir: Path) -> list[str]:
        sandbox = self.sandbox if job.role == "coder" else READ_ONLY_SANDBOX
        argv = [
            self.command, "exec", "--json", "--ephemeral",
            "-C", str(job.workdir),
            "-s", sandbox,
            "-c", 'approval_policy="never"',
        ]  # fmt: skip
        if job.model:
            argv += ["-m", job.model]
        if job.effort in EFFORTS:
            argv += ["-c", f'model_reasoning_effort="{job.effort}"']
        argv += mcp_overrides(job.tools)
        if job.output_schema is not None and job.role != "coder":
            argv += ["--output-schema", str(tmp_dir / SCHEMA_FILE)]
        argv += ["-o", str(tmp_dir / LAST_MESSAGE_FILE)]
        if not in_git_repo(job.workdir):
            argv.append("--skip-git-repo-check")
        return [*argv, *self.extra_args, "-"]

    async def run(self, job: WorkerJob) -> WorkerOutput:
        env = with_redact_names(dict(os.environ if self._env is None else self._env), job.tools)
        if job.effort is not None and job.effort not in EFFORTS:
            append_log_sections(
                job.log_path, [("note", f"effort {job.effort!r} not supported; ignored")], env
            )
        if job.tools:
            notes = [job.tools.describe(), *unsupported_tools(job.tools)]
            append_log_sections(job.log_path, [("tools", "\n".join(notes))], env)
        with tempfile.TemporaryDirectory(prefix="jev-codex-") as tmp:
            tmp_dir = Path(tmp)
            if job.output_schema is not None and job.role != "coder":
                # Strict form: OpenAI structured outputs require additionalProperties: false.
                schema = strict_json_schema(job.output_schema)
                (tmp_dir / SCHEMA_FILE).write_text(json.dumps(schema), encoding="utf-8")
            argv = self.build_argv(job, tmp_dir)
            try:
                result = await self.runner(
                    argv, job.workdir, env, job.timeout_s, job.prompt, log_path=job.log_path
                )
            except OSError as exc:
                return make_output(job, "error", f"could not run {self.command}: {exc}", env=env)
            last_message = _read_text(tmp_dir / LAST_MESSAGE_FILE)
        events = parse_events(result.stdout)
        status = classify_codex(result, events)
        tokens = usage_from(events)
        if status != "success":
            return make_output(
                job, status, _failure_summary(status, result, events), env=env, tokens=tokens
            )
        text = last_message if last_message and last_message.strip() else final_message(events)
        return await finish_reply(job, text, runner=self.runner, env=env, tokens=tokens)


def mcp_overrides(tools: AgentTools) -> list[str]:
    """`-c mcp_servers.<name>.<key>=<value>` pairs for the job's MCP servers (values as TOML).

    Env vars are passed by name (`env_vars`, `bearer_token_env_var`, `env_http_headers`), so no
    secret value is ever on the command line. SSE servers are skipped (see `unsupported_tools`).
    """
    argv: list[str] = []

    def put(name: str, key: str, value: Any) -> None:
        argv.extend(["-c", f"mcp_servers.{name}.{key}={toml_value(value)}"])

    for name, server in tools.mcp.items():
        if server.command is not None:
            put(name, "command", server.command)
            put(name, "args", list(server.args))
            if server.env_from:
                put(name, "env_vars", list(server.env_from))
        elif server.transport == "http":
            put(name, "url", server.url)
            if server.bearer_token_env:
                put(name, "bearer_token_env_var", server.bearer_token_env)
            if server.headers_from_env:
                put(name, "env_http_headers", dict(server.headers_from_env))
    return argv


def unsupported_tools(tools: AgentTools) -> list[str]:
    """Notes for the configured tools Codex cannot use in one `codex exec` run."""
    notes = [
        f"mcp server {name}: Codex supports streamable HTTP only, not SSE; left out"
        for name, server in tools.mcp.items()
        if server.url is not None and server.transport != "http"
    ]
    notes += [f"skill {s.name}: Codex cannot load skills per run; left out" for s in tools.skills]
    notes += [
        f"plugin {p.name}: Codex cannot load plugins per run; left out" for p in tools.plugins
    ]
    return notes


def toml_value(value: Any) -> str:
    """A TOML literal for a string, list of strings or table of strings.

    JSON strings are valid TOML basic strings as long as non-ASCII text is kept as is (TOML has
    no surrogate-pair escapes), hence `ensure_ascii=False`.
    """

    def text(item: str) -> str:
        return json.dumps(item, ensure_ascii=False)

    if isinstance(value, str):
        return text(value)
    if isinstance(value, list):
        return "[" + ", ".join(text(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{text(k)} = {text(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"unsupported TOML value: {value!r}")


def in_git_repo(path: Path) -> bool:
    """True if `path` or a parent has a `.git` entry (a directory, or a worktree's file)."""
    path = path.resolve()
    return any((p / ".git").exists() for p in (path, *path.parents))


def parse_events(stdout: str) -> list[dict[str, Any]]:
    """The JSON objects on stdout, one per line; other lines are ignored. Only events with
    a string `type` are kept (callers index `event["type"]` directly)."""
    return [e for e in parse_json_lines(stdout) if isinstance(e.get("type"), str)]


def classify_codex(result: CommandResult, events: list[dict[str, Any]]) -> WorkerStatus:
    """Map a finished `codex exec --json` run to a worker status.

    - timed out -> "timeout"
    - exit 127/126 without JSON events (`run_command`: the binary is missing or not
      executable) -> "error"
    - exit 0, a `turn.completed` event and no `turn.failed` -> "success" (transient `error`
      events, such as reconnect notices, are ignored when the turn completed)
    - usage limit (`You've hit your usage limit`, matched with a curly or straight apostrophe;
      `out of credits`; `spend cap`) in an error message or stderr -> "quota_exhausted"
    - not logged in / 401 / invalid API key -> "error"
    - `turn.failed`, an `error` event or a non-zero exit -> "failure"
    - exit 0 without a completed turn (no or malformed JSONL) -> "error"
    """
    if result.timed_out:
        return "timeout"
    if result.exit_code in (EXIT_NOT_FOUND, EXIT_NOT_EXECUTABLE) and not events:
        return "error"
    types = [e["type"] for e in events]
    if result.exit_code == 0 and "turn.completed" in types and "turn.failed" not in types:
        return "success"
    text = normalize_quotes("\n".join([*error_messages(events), result.stderr]))
    if QUOTA_PATTERN.search(text):
        return "quota_exhausted"
    if AUTH_PATTERN.search(text):
        return "error"
    if "turn.failed" in types or "error" in types or result.exit_code != 0:
        return "failure"
    return "error"


def error_messages(events: list[dict[str, Any]]) -> list[str]:
    """Messages of `turn.failed` (`error.message`) and `error` (`message`) events."""
    messages: list[str] = []
    for event in events:
        if event["type"] == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                messages.append(error["message"])
        elif event["type"] == "error" and isinstance(event.get("message"), str):
            messages.append(event["message"])
    return messages


def final_message(events: list[dict[str, Any]]) -> str | None:
    """Text of the last completed `agent_message` item."""
    for event in reversed(events):
        item = event.get("item")
        if (
            event["type"] == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            return str(item["text"])
    return None


def usage_from(events: list[dict[str, Any]]) -> dict[str, int] | None:
    """Token counts summed over `turn.completed` events (`input`, `cached_input`, `output`, ...)."""
    tokens: dict[str, int] = {}
    for event in events:
        usage = event.get("usage") if event["type"] == "turn.completed" else None
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if key.endswith("_tokens") and isinstance(value, int) and not isinstance(value, bool):
                name = key.removesuffix("_tokens")
                tokens[name] = tokens.get(name, 0) + value
    return tokens or None


def _failure_summary(
    status: WorkerStatus, result: CommandResult, events: list[dict[str, Any]]
) -> str:
    if status == "timeout":
        return f"codex timed out after {result.duration_s:.0f}s"
    messages = error_messages(events)
    detail = messages[-1] if messages else (result.stderr.strip() or "no completed turn")
    return f"codex {status} (exit {result.exit_code}): {' '.join(detail.split())[:300]}"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
