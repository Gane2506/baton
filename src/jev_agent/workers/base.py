"""The worker interface (PLAN.md section 6).

A worker runs one agent (a CLI such as Claude Code or Codex, or a plain API model) with a
rendered role prompt and returns a `WorkerResult`. Workers never raise for agent failures:
they map them to `WorkerResult.status` ("failure", "error", "quota_exhausted", "timeout").
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from jev_agent.core.models import Role, WorkerResult
from jev_agent.workers.tools import AgentTools


class WorkerJob(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, protected_namespaces=())

    role: Role
    prompt: str  # rendered role prompt + task card
    workdir: Path  # worktree for coders; read-only copy for others
    log_path: Path  # where the worker writes its raw log
    model: str | None = None
    effort: str | None = None
    timeout_s: int = Field(default=1200, gt=0)
    # Planner/validator/reviewer return JSON validated against this schema.
    output_schema: type[BaseModel] | None = None
    # Bookkeeping, for logs and for scripting fakes: the task (None for run-level work such as
    # planning) and the attempt number within that task or run.
    task_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    # MCP servers, skills and plugins configured for the role (empty: none).
    tools: AgentTools = Field(default_factory=AgentTools)


class WorkerOutput(BaseModel):
    """What a worker returns: the result plus the parsed structured output, if any."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    result: WorkerResult
    parsed: BaseModel | None = None


@runtime_checkable
class Worker(Protocol):
    name: str

    async def run(self, job: WorkerJob) -> WorkerOutput: ...
