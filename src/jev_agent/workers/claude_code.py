"""`ClaudeCodeWorker`: runs Claude Code headless (`claude -p`) as a subprocess (PLAN.md section 6,
docs/verification.md section 2, checked against Claude Code 2.1.114).

Command shape (prompt on stdin, cwd = job.workdir):

    claude -p --output-format json --no-session-persistence [--bare] [--model M] [--effort E]
        --permission-mode MODE --allowedTools "T1 T2 ..." [--disallowedTools "..."]
        [--json-schema '<schema>'] [--max-budget-usd X] [extra args...]

Permissions are always explicit (the docs warn the default may be `auto`):
- coder: `acceptEdits` (file edits in the cwd, which is the task's isolated worktree, are
  accepted; other tools only if listed in `allowed_tools`; anything else is denied because
  nobody can answer a prompt in `-p` mode).
- planner, validator, reviewer: `dontAsk` with read-only tools and file-writing tools
  explicitly disallowed. `dontAsk` "denies anything not pre-allowed" (docs/verification.md).
- `skip_permissions=True` replaces the coder's mode with `--dangerously-skip-permissions`. It is
  opt-in and dangerous: the agent may then run any command. Read-only roles never get it.

Agent tools (`job.tools`, docs/verification.md section 6), all in a private temporary directory
outside the workdir that is removed after the attempt:
- MCP servers: `--mcp-config <tmp>/mcp.json --strict-mcp-config`, so only the role's servers
  load (not the repo's `.mcp.json` or your user config), and `mcp__<server>` is added to
  `--allowedTools` so headless runs may call them. Env var values are resolved into that file
  (mode 0600) because `${VAR}` expansion is documented for `.mcp.json` only; they never reach
  argv or logs.
- Skills: copied to `<tmp>/skills/.claude/skills/<name>/` and loaded with `--add-dir`, which the
  docs say loads that folder's skills (also with `--bare`); `Skill(<name>)` is allowed.
- Plugins: `--plugin-dir <path>` each, plus the plugin's own `allowed_tools`.

The JSON result maps to `WorkerResult` through `classify_claude_code`; `total_cost_usd` (a
client-side estimate, not real spend on subscription auth) becomes `cost_usd`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jev_agent.config.schema import WORKER_EFFORTS
from jev_agent.core.models import WorkerStatus
from jev_agent.workers.base import WorkerJob, WorkerOutput
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

EFFORTS = WORKER_EFFORTS["claude_code"]  # other levels are ignored (with a note)
CODER_PERMISSION_MODE = "acceptEdits"
READ_ONLY_PERMISSION_MODE = "dontAsk"
DEFAULT_CODER_TOOLS: tuple[str, ...] = (
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "Bash(git status *)",
    "Bash(git diff *)",
)
DEFAULT_READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")
WRITE_TOOLS: tuple[str, ...] = ("Edit", "Write", "NotebookEdit")
MCP_CONFIG_FILE = "mcp.json"
SKILLS_ROOT = "skills"  # <tmp>/skills/.claude/skills/<name>/, passed with --add-dir
# User-facing limit messages (docs/verification.md section 2; errors.md). Curly apostrophes are
# normalised to straight ones before matching.
QUOTA_PATTERN = re.compile(
    r"you've hit your [\w ]*limit|credit balance is too low|request rejected \(429\)"
    r"|credits_required|out of extra usage",
    re.IGNORECASE,
)
AUTH_PATTERN = re.compile(
    r"not logged in|invalid api key|run /login|authentication[_ ]failed|authentication_error"
    r"|oauth token (?:has )?expired|\bunauthorized\b",
    re.IGNORECASE,
)
# Result subtypes for a run that ended without finishing the task (not a broken setup).
_FAILURE_SUBTYPES = frozenset({"error_max_turns", "error_max_budget_usd", "error_during_execution"})
# stream-json `system/api_retry` error kinds.
_QUOTA_RETRY_ERRORS = frozenset({"rate_limit", "billing_error"})
_AUTH_RETRY_ERRORS = frozenset({"authentication_failed"})


class ClaudeCodeWorker:
    """Worker for Claude Code. Never raises for agent failures; see `classify_claude_code`."""

    name = "claude_code"

    def __init__(
        self,
        command: str = "claude",
        runner: CommandRunner = run_command,
        *,
        permission_mode: str = CODER_PERMISSION_MODE,
        read_only_permission_mode: str = READ_ONLY_PERMISSION_MODE,
        allowed_tools: Sequence[str] | None = None,
        read_only_tools: Sequence[str] = DEFAULT_READ_ONLY_TOOLS,
        skip_permissions: bool = False,
        max_budget_usd: float | None = None,
        bare: bool = False,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.command = command
        self.runner = runner
        self.permission_mode = permission_mode
        self.read_only_permission_mode = read_only_permission_mode
        self.allowed_tools = tuple(DEFAULT_CODER_TOOLS if allowed_tools is None else allowed_tools)
        self.read_only_tools = tuple(read_only_tools)
        self.skip_permissions = skip_permissions
        self.max_budget_usd = max_budget_usd
        self.bare = bare
        self.extra_args = tuple(extra_args)
        self._env = env

    def build_argv(self, job: WorkerJob, tools_dir: Path | None = None) -> list[str]:
        """The command for `job`. `tools_dir` is where `prepare_tools` wrote the tool files;
        it is required when the job has tools."""
        if job.tools and tools_dir is None:
            raise ValueError("a job with tools needs a tools_dir")
        argv = [self.command, "-p", "--output-format", "json", "--no-session-persistence"]
        if self.bare:
            argv.append("--bare")  # API-key auth only (ANTHROPIC_API_KEY); see docs/security
        if job.model:
            argv += ["--model", job.model]
        if job.effort in EFFORTS:
            argv += ["--effort", job.effort]
        tool_rules = tool_permissions(job.tools)
        if job.role == "coder":
            if self.skip_permissions:
                argv.append("--dangerously-skip-permissions")
            else:
                argv += ["--permission-mode", self.permission_mode]
            allowed = [*self.allowed_tools, *tool_rules]
            if allowed:
                argv += ["--allowedTools", " ".join(allowed)]
        else:
            argv += ["--permission-mode", self.read_only_permission_mode]
            allowed = [*self.read_only_tools, *tool_rules]
            if allowed:
                argv += ["--allowedTools", " ".join(allowed)]
            argv += ["--disallowedTools", " ".join(WRITE_TOOLS)]
            if job.output_schema is not None:
                schema = job.output_schema.model_json_schema()
                argv += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        if tools_dir is not None:
            argv += tool_args(job.tools, tools_dir)
        if self.max_budget_usd is not None:
            argv += ["--max-budget-usd", f"{self.max_budget_usd:g}"]
        return argv + list(self.extra_args)

    async def run(self, job: WorkerJob) -> WorkerOutput:
        env = with_redact_names(dict(os.environ if self._env is None else self._env), job.tools)
        if job.effort is not None and job.effort not in EFFORTS:
            append_log_sections(
                job.log_path, [("note", f"effort {job.effort!r} not supported; ignored")], env
            )
        if not job.tools:
            return await self._run(job, env, None)
        with tempfile.TemporaryDirectory(prefix="jev-claude-") as tmp:
            tools_dir = Path(tmp)
            try:
                tools, notes = prepare_tools(job.tools, tools_dir, env)
            except OSError as exc:
                return make_output(job, "error", f"could not prepare agent tools: {exc}", env=env)
            append_log_sections(
                job.log_path, [("tools", "\n".join([tools.describe(), *notes]))], env
            )
            return await self._run(job.model_copy(update={"tools": tools}), env, tools_dir)

    async def _run(
        self, job: WorkerJob, env: dict[str, str], tools_dir: Path | None
    ) -> WorkerOutput:
        argv = self.build_argv(job, tools_dir)
        try:
            result = await self.runner(
                argv, job.workdir, env, job.timeout_s, job.prompt, log_path=job.log_path
            )
        except OSError as exc:
            return make_output(job, "error", f"could not run {self.command}: {exc}", env=env)
        payload = parse_result(result.stdout)
        status = classify_claude_code(result, payload)
        cost, tokens = usage_from(payload)
        if status != "success" or payload is None:
            return make_output(
                job, status, _failure_summary(status, result, payload), env=env,
                cost_usd=cost, tokens=tokens,
            )  # fmt: skip
        text = payload.get("result")
        return await finish_reply(
            job,
            text if isinstance(text, str) else None,
            runner=self.runner,
            env=env,
            structured=payload.get("structured_output"),
            cost_usd=cost,
            tokens=tokens,
        )


# --- agent tools ---------------------------------------------------------------------------


def tool_permissions(tools: AgentTools) -> list[str]:
    """`--allowedTools` rules for the job's tools: every tool of each configured MCP server
    (`mcp__<server>`), each skill (`Skill(<name>)`) and each plugin's own rules."""
    rules = [f"mcp__{name}" for name in tools.mcp]
    rules += [f"Skill({skill.name})" for skill in tools.skills]
    rules += [rule for plugin in tools.plugins for rule in plugin.allowed_tools]
    return list(dict.fromkeys(rules))


def tool_args(tools: AgentTools, tools_dir: Path) -> list[str]:
    """`--mcp-config`, `--add-dir` and `--plugin-dir` arguments for the prepared tools."""
    argv: list[str] = []
    if tools.mcp:
        argv += ["--mcp-config", str(tools_dir / MCP_CONFIG_FILE), "--strict-mcp-config"]
    if tools.skills:
        argv += ["--add-dir", str(tools_dir / SKILLS_ROOT)]
    for plugin in tools.plugins:
        argv += ["--plugin-dir", str(plugin.path)]
    return argv


def mcp_config(tools: AgentTools, env: Mapping[str, str]) -> tuple[dict[str, Any], list[str]]:
    """The `--mcp-config` JSON (`{"mcpServers": {...}}`) with env var values filled in, and
    notes naming the variables that are not set (their entries are left out)."""
    servers: dict[str, Any] = {}
    notes: list[str] = []

    def value(server: str, name: str) -> str | None:
        found = env.get(name)
        if not found:
            notes.append(f"mcp server {server}: {name} is not set; left out")
        return found or None

    for name, server in tools.mcp.items():
        if server.command is not None:
            server_env = {v: val for v in server.env_from if (val := value(name, v)) is not None}
            servers[name] = {
                "type": "stdio",
                "command": server.command,
                "args": list(server.args),
                "env": server_env,
            }
            continue
        headers = {
            header: val
            for header, var in server.headers_from_env.items()
            if (val := value(name, var)) is not None
        }
        if server.bearer_token_env and (token := value(name, server.bearer_token_env)):
            headers["Authorization"] = f"Bearer {token}"
        servers[name] = {"type": server.transport, "url": server.url, "headers": headers}
    return {"mcpServers": servers}, notes


def prepare_tools(
    tools: AgentTools, tools_dir: Path, env: Mapping[str, str]
) -> tuple[AgentTools, list[str]]:
    """Write the MCP config and copy the skills into `tools_dir`.

    Returns the tools that are usable (skills and plugins whose path is missing are dropped)
    and notes for the log. Never puts env var values in the notes.
    """
    notes: list[str] = []
    if tools.mcp:
        config, missing = mcp_config(tools, env)
        notes += missing
        _write_private(tools_dir / MCP_CONFIG_FILE, json.dumps(config, indent=2))
    skills = []
    for skill in tools.skills:
        if not (skill.path / "SKILL.md").is_file():
            notes.append(f"skill {skill.name}: no SKILL.md in {skill.path}; left out")
            continue
        target = tools_dir / SKILLS_ROOT / ".claude" / "skills" / skill.name
        shutil.copytree(skill.path, target, symlinks=True)
        skills.append(skill)
    plugins = []
    for plugin in tools.plugins:
        if not plugin.path.exists():
            notes.append(f"plugin {plugin.name}: {plugin.path} does not exist; left out")
            continue
        plugins.append(plugin)
    usable = tools.model_copy(update={"skills": tuple(skills), "plugins": tuple(plugins)})
    return usable, notes


def _write_private(path: Path, text: str) -> None:
    """Write `text` readable by the owner only (it may hold MCP credentials)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def parse_result(stdout: str) -> dict[str, Any] | None:
    """The `type: "result"` object from `--output-format json` (or the last one in stream-json
    output); None if there is none."""
    text = stdout.strip()
    if not text:
        return None
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        obj: dict[str, Any] = data
        return obj if obj.get("type", "result") == "result" else None
    for obj in reversed(_json_lines(text)):
        if obj.get("type") == "result":
            return obj
    return None


def classify_claude_code(result: CommandResult, payload: dict[str, Any] | None) -> WorkerStatus:
    """Map a finished `claude -p` run to a worker status.

    - timed out -> "timeout"
    - exit 127/126 without a result (`run_command`: the binary is missing or not
      executable) -> "error"
    - exit 0, result subtype "success", `is_error` false -> "success"
    - usage/rate limit (`You've hit your ... limit`, `Credit balance is too low`,
      `Request rejected (429)`, `credits_required`, a rejected `rate_limit_event`, an
      `api_retry` with `rate_limit`/`billing_error`) -> "quota_exhausted"
    - not logged in / invalid API key / `authentication_failed` -> "error"
    - subtype `error_max_structured_output_retries` -> "error"
    - subtype `error_max_turns`, `error_max_budget_usd`, `error_during_execution`, an
      `is_error` result, or any other non-zero exit -> "failure"
    - exit 0 without a parseable result -> "error" (malformed output)
    """
    if result.timed_out:
        return "timeout"
    if _not_started(result, payload):
        return "error"
    subtype = payload.get("subtype") if payload else None
    is_error = bool(payload.get("is_error")) if payload else True
    if result.exit_code == 0 and subtype == "success" and not is_error:
        return "success"
    events = _json_lines(result.stdout)
    detail = payload.get("result") if payload else None
    parts = [result.stdout, result.stderr, detail if isinstance(detail, str) else ""]
    text = normalize_quotes("\n".join(parts))
    if QUOTA_PATTERN.search(text) or any(_is_quota_event(e) for e in events):
        return "quota_exhausted"
    if AUTH_PATTERN.search(text) or any(_retry_error(e) in _AUTH_RETRY_ERRORS for e in events):
        return "error"
    if subtype == "error_max_structured_output_retries":
        return "error"
    if subtype in _FAILURE_SUBTYPES or (payload is not None and is_error):
        return "failure"
    if result.exit_code != 0:
        return "failure"
    return "error"


def _not_started(result: CommandResult, payload: dict[str, Any] | None) -> bool:
    """`claude` never ran: `run_command`'s exit code for a missing or non-executable binary,
    with no result and no JSON events on stdout."""
    missing = result.exit_code in (EXIT_NOT_FOUND, EXIT_NOT_EXECUTABLE)
    return missing and payload is None and not _json_lines(result.stdout)


def usage_from(payload: dict[str, Any] | None) -> tuple[float | None, dict[str, int] | None]:
    """`total_cost_usd` and token counts (`input`, `output`, `cache_read_input`, ...)."""
    if not payload:
        return None, None
    cost = payload.get("total_cost_usd")
    usage = payload.get("usage")
    tokens: dict[str, int] = {}
    if isinstance(usage, dict):
        for key, value in usage.items():
            if key.endswith("_tokens") and isinstance(value, int) and not isinstance(value, bool):
                tokens[key.removesuffix("_tokens")] = value
    return (float(cost) if isinstance(cost, int | float) else None), (tokens or None)


def _failure_summary(
    status: WorkerStatus, result: CommandResult, payload: dict[str, Any] | None
) -> str:
    if status == "timeout":
        return f"claude timed out after {result.duration_s:.0f}s"
    if payload is not None:
        detail = payload.get("result") or payload.get("errors") or ""
        subtype = payload.get("subtype", "?")
        return f"claude {status} ({subtype}): {_one_line(detail)}".rstrip(": ")
    detail = result.stderr.strip() or result.stdout.strip() or "no output"
    return f"claude {status} (exit {result.exit_code}): {_one_line(detail)}"


def _one_line(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    return " ".join(text.split())[:300]


def normalize_quotes(text: str) -> str:
    return text.replace("\u2019", "'").replace("\u2018", "'")


def _json_lines(text: str) -> list[dict[str, Any]]:
    objs: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def _is_quota_event(event: dict[str, Any]) -> bool:
    if event.get("type") == "rate_limit_event":
        info = event.get("rate_limit_info")
        if isinstance(info, dict) and (
            info.get("status") == "rejected" or info.get("errorCode") == "credits_required"
        ):
            return True
    return _retry_error(event) in _QUOTA_RETRY_ERRORS


def _retry_error(event: dict[str, Any]) -> str | None:
    if event.get("type") == "system" and event.get("subtype") == "api_retry":
        error = event.get("error")
        return error if isinstance(error, str) else None
    return None
