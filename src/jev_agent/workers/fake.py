"""`FakeWorker`: a scripted worker for tests and dry runs. Never calls a model or a CLI.

A script maps keys to steps. For each job the most specific key wins:
`"<role>:<task_id>:<attempt>"`, then `"<role>:<task_id>"`, then `"<role>"`. A key may hold one
step or a list of steps; a list is consumed one step per job and its last step then repeats.
Roles without a scripted step get a sensible default: a one-task plan, an approving verdict,
a clean code review, a coder that writes a small file, or a learner that learns nothing.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from jev_agent.core.models import Plan, TaskCard, ValidationResult, WorkerResult, WorkerStatus
from jev_agent.roles.schemas import CoderSummary, LearnerOutput, QualityReview, ReviewResult
from jev_agent.runlog import write_log
from jev_agent.workers.base import WorkerJob, WorkerOutput


class FakeWorkerCrash(BaseException):
    """Raised by a step with `crash=True` to simulate the jev process dying mid-attempt.

    It derives from BaseException so the harness does not turn it into a failed attempt.
    """


@dataclass
class FakeStep:
    status: WorkerStatus = "success"
    summary: str = "fake worker finished"
    parsed: BaseModel | None = None  # None: the role's default output (when status is success)
    cost_usd: float | None = None
    files: dict[str, str] = field(default_factory=dict)  # coder: path (in workdir) -> content
    crash: bool = False


def default_plan() -> Plan:
    return Plan(
        summary="Fake plan with a single task.",
        tasks=[
            TaskCard(
                id="T1",
                title="Fake task",
                description="Created by FakeWorker.",
                files_hint=["jev_fake_T1.txt"],
                acceptance_criteria=["the file exists"],
            )
        ],
    )


def _default_output(job: WorkerJob) -> BaseModel:
    if job.role == "planner":
        return default_plan()
    if job.role == "validator":
        return ValidationResult(verdict="approve")
    if job.role == "reviewer":
        return ReviewResult(verdict="approve")
    if job.role == "code_reviewer":
        return QualityReview(summary="Fake review: no problems found.")
    if job.role == "learner":
        return LearnerOutput()  # nothing learned
    return CoderSummary(summary="fake change", files_changed=[_default_file(job)])


def _default_file(job: WorkerJob) -> str:
    return f"jev_fake_{job.task_id or 'run'}.txt"


ScriptValue = FakeStep | Sequence[FakeStep]


class FakeWorker:
    def __init__(self, name: str = "fake", script: Mapping[str, ScriptValue] | None = None):
        self.name = name
        self.script = dict(script or {})
        self.jobs: list[WorkerJob] = []  # every job received, for assertions
        self._calls: defaultdict[str, int] = defaultdict(int)

    async def run(self, job: WorkerJob) -> WorkerOutput:
        self.jobs.append(job)
        step = self._next_step(job)
        write_log(
            job.log_path,
            f"[{self.name}] role={job.role} task={job.task_id} attempt={job.attempt} "
            f"model={job.model} effort={job.effort}\n--- prompt ---\n{job.prompt}\n"
            f"--- result ---\nstatus={step.status} summary={step.summary}\n",
        )
        if step.crash:
            raise FakeWorkerCrash(f"{self.name} crashed on {job.role} {job.task_id}")

        files = dict(step.files)
        if job.role == "coder" and step.status == "success" and not files:
            files = {_default_file(job): f"written by {self.name}, attempt {job.attempt}\n"}
        for rel, content in files.items():
            target = job.workdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        parsed = step.parsed
        if parsed is None and step.status == "success":
            parsed = _default_output(job)
        result = WorkerResult(
            status=step.status,
            summary=step.summary,
            files_changed=sorted(files),
            cost_usd=step.cost_usd,
            raw_log_path=job.log_path,
        )
        return WorkerOutput(result=result, parsed=parsed)

    def _next_step(self, job: WorkerJob) -> FakeStep:
        keys = [f"{job.role}:{job.task_id}:{job.attempt}", f"{job.role}:{job.task_id}", job.role]
        for key in keys:
            value = self.script.get(key)
            if value is None:
                continue
            if isinstance(value, FakeStep):
                return value
            index = min(self._calls[key], len(value) - 1)
            self._calls[key] += 1
            return value[index]
        return FakeStep()
