"""Turn a CLI agent's final reply into a `WorkerOutput` (shared by the Claude Code and Codex
adapters).

- Roles with an output schema (planner, validator, reviewer): the reply (or the CLI's native
  structured output) must validate against the schema, else status "error".
- Coder: the reply must end with the summary block the coder prompt asks for, a fenced code
  block tagged `jev-summary` (`jev_agent.roles.schemas.CODER_SUMMARY_MARKER`) holding
  `{"summary": ..., "files_changed": [...]}`. Without it, status is "error" (PLAN.md section 6).
  `files_changed` comes from `git status --porcelain` in the workdir, run through the injected
  runner; the block's own list is used only if git fails.
- Other roles without a schema: the reply text, truncated, is the summary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from jev_agent.core.models import WorkerResult, WorkerStatus
from jev_agent.roles.schemas import CODER_SUMMARY_MARKER, extract_coder_summary
from jev_agent.workers.api import parse_json_content, structured_summary
from jev_agent.workers.base import WorkerJob, WorkerOutput
from jev_agent.workers.subprocess_runner import CommandRunner, append_log_sections, redact

SUMMARY_CHARS = 500
GIT_TIMEOUT_S = 60


def make_output(
    job: WorkerJob,
    status: WorkerStatus,
    summary: str,
    *,
    env: Mapping[str, str] | None = None,
    files_changed: list[str] | None = None,
    cost_usd: float | None = None,
    tokens: dict[str, int] | None = None,
    parsed: BaseModel | None = None,
) -> WorkerOutput:
    """Build the output and append a short result section to the job's log."""
    summary = redact(summary[:SUMMARY_CHARS], env)
    append_log_sections(
        job.log_path,
        [("result", f"status: {status}\nsummary: {summary}\nfiles_changed: {files_changed}")],
        env,
    )
    result = WorkerResult(
        status=status,
        summary=summary,
        files_changed=files_changed or [],
        cost_usd=cost_usd,
        tokens=tokens,
        raw_log_path=job.log_path,
    )
    return WorkerOutput(result=result, parsed=parsed)


async def finish_reply(
    job: WorkerJob,
    text: str | None,
    *,
    runner: CommandRunner,
    env: Mapping[str, str],
    structured: Any = None,
    cost_usd: float | None = None,
    tokens: dict[str, int] | None = None,
) -> WorkerOutput:
    """Build the output for a run the CLI reported as successful.

    `structured` is the CLI's native structured output, if it has one (Claude Code's
    `structured_output`); otherwise the schema roles parse `text` as JSON.
    """

    def out(status: WorkerStatus, summary: str, **kw: Any) -> WorkerOutput:
        return make_output(job, status, summary, env=env, cost_usd=cost_usd, tokens=tokens, **kw)

    if job.role == "coder":
        block = extract_coder_summary(text or "")
        if block is None:
            return out("error", f"coder reply has no valid ```{CODER_SUMMARY_MARKER} block")
        files = await git_files_changed(job.workdir, runner=runner, env=env, log_path=job.log_path)
        return out(
            "success",
            block.summary,
            files_changed=block.files_changed if files is None else files,
            parsed=block,
        )
    if job.output_schema is not None:
        schema = job.output_schema
        try:
            data = structured if structured is not None else parse_json_content(text or "")
            parsed = schema.model_validate(data)
        except (json.JSONDecodeError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                return out(
                    "error",
                    f"output does not match {schema.__name__}: {exc.error_count()} error(s)",
                )
            return out("error", "output is not valid JSON")
        return out("success", structured_summary(parsed), parsed=parsed)
    if not text or not text.strip():
        return out("error", "the agent returned an empty reply")
    return out("success", text.strip())


async def git_files_changed(
    workdir: Path, *, runner: CommandRunner, env: Mapping[str, str], log_path: Path
) -> list[str] | None:
    """Paths changed in `workdir` (tracked and untracked), or None if git failed."""
    argv = ["git", "-C", str(workdir), "status", "--porcelain", "--untracked-files=all"]
    result = await runner(argv, workdir, env, GIT_TIMEOUT_S, log_path=log_path)
    if result.timed_out or result.exit_code != 0:
        return None
    return parse_porcelain(result.stdout)


def parse_porcelain(text: str) -> list[str]:
    """Paths from `git status --porcelain` (v1) output; a rename yields its new path."""
    paths: list[str] = []
    for line in text.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
            path = _unquote(path[1:-1])
        paths.append(path)
    return paths


def _unquote(quoted: str) -> str:
    """Undo git's C-style quoting (`\\t`, `\\"`, octal UTF-8 bytes such as `\\303\\251`)."""
    raw = quoted.encode("ascii", "backslashreplace").decode("unicode_escape")
    return raw.encode("latin-1", "replace").decode("utf-8", "replace")
