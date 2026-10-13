"""A git failure while committing a task's work stops the run resumably (issue #24): the
worktree keeps the attempt's work and `jev resume` continues once the cause is fixed."""

from __future__ import annotations

from pathlib import Path

from jev_agent.config import JevConfig
from jev_agent.core.commands import FakeCommandRunner
from jev_agent.core.models import Idea, Plan, RunState, TaskCard
from jev_agent.core.pipeline import build_pipeline
from jev_agent.core.store import Store
from jev_agent.core.workspace import GitWorkspace, WorkspaceError
from jev_agent.workers.fake import FakeStep, FakeWorker
from jev_agent.workers.registry import WorkerRegistry

PLAN = Plan(
    summary="one task",
    tasks=[
        TaskCard(
            id="T1",
            title="task T1",
            description="do T1",
            files_hint=["src/t1.py"],
            acceptance_criteria=["a", "b"],
        )
    ],
)


class HookFailsOnce(GitWorkspace):
    """A repo pre-commit hook that hangs (git timed out) on the first commit only."""

    failures = 1

    def commit_task(self, workdir: Path, message: str) -> bool:
        if self.failures:
            self.failures -= 1
            raise WorkspaceError("git commit -q -m ... timed out after 600 s (a hook may hang)")
        return super().commit_task(workdir, message)


async def test_commit_failure_stops_resumably_and_resume_finishes(fixture_repo: Path) -> None:
    with Store.open(fixture_repo / ".jev" / "state.db") as store:
        worker = FakeWorker("fake", {"planner": FakeStep(parsed=PLAN)})
        registry = WorkerRegistry({n: worker for n in ("claude_code", "codex", "api")})
        config = JevConfig.model_validate({"approval": {"require_plan_approval": False}})
        workspace = HookFailsOnce(fixture_repo, fixture_repo / ".jev")
        pipeline = build_pipeline(
            fixture_repo,
            config,
            store,
            registry,
            workspace=workspace,
            commands=FakeCommandRunner(),
        )
        run = await pipeline.start(Idea(text="add t1", repo_path=fixture_repo))
        assert run.state is RunState.NEEDS_HUMAN, run.reason
        assert run.resume_state is RunState.EXECUTING
        assert run.reason is not None
        assert "T1: git commit failed" in run.reason and "jev resume" in run.reason

        done = await pipeline.resume(run.id)
        assert done.state is RunState.DONE, done.reason
