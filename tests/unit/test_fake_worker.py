from pathlib import Path

import pytest

from jev_agent.config import JevConfig
from jev_agent.core.models import Plan, Role, ValidationResult
from jev_agent.roles import CoderSummary
from jev_agent.workers.base import Worker, WorkerJob
from jev_agent.workers.fake import FakeStep, FakeWorker, FakeWorkerCrash, default_plan
from jev_agent.workers.registry import WorkerNotAvailable, WorkerRegistry


def _job(tmp_path: Path, role: Role, task_id: str | None = None, attempt: int = 1) -> WorkerJob:
    return WorkerJob(
        role=role,
        prompt="PROMPT",
        workdir=tmp_path,
        log_path=tmp_path / "logs" / f"{role}-{attempt}.log",
        task_id=task_id,
        attempt=attempt,
    )


async def test_defaults_per_role(tmp_path: Path) -> None:
    fake = FakeWorker()
    assert isinstance(fake, Worker)
    assert (await fake.run(_job(tmp_path, "planner"))).parsed == default_plan()
    assert (await fake.run(_job(tmp_path, "validator"))).parsed == ValidationResult(
        verdict="approve"
    )
    out = await fake.run(_job(tmp_path, "coder", "T1"))
    assert isinstance(out.parsed, CoderSummary)
    assert (tmp_path / "jev_fake_T1.txt").exists()
    assert out.result.files_changed == ["jev_fake_T1.txt"]
    assert "PROMPT" in (tmp_path / "logs" / "coder-1.log").read_text()
    assert len(fake.jobs) == 3


async def test_most_specific_key_wins_and_lists_are_consumed(tmp_path: Path) -> None:
    plan = Plan(summary="mine", tasks=default_plan().tasks)
    fake = FakeWorker(
        script={
            "planner": FakeStep(parsed=plan),
            "coder:T1": [FakeStep(status="failure"), FakeStep(status="timeout")],
            "coder:T1:5": FakeStep(status="quota_exhausted", cost_usd=0.25),
        }
    )
    assert (await fake.run(_job(tmp_path, "planner"))).parsed == plan
    statuses = [(await fake.run(_job(tmp_path, "coder", "T1", n))).result.status for n in (1, 2, 3)]
    assert statuses == ["failure", "timeout", "timeout"]
    out = await fake.run(_job(tmp_path, "coder", "T1", 5))
    assert (out.result.status, out.result.cost_usd, out.parsed) == ("quota_exhausted", 0.25, None)


async def test_scripted_files_through_symlinks_stay_in_workdir(tmp_path: Path) -> None:
    """A scripted path through a symlinked directory — or a symlinked target file —
    must not let the fake worker write outside the workdir (issue #204)."""
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    link_dir = tmp_path / "linkdir"
    link_dir.symlink_to(outside, target_is_directory=True)
    fake = FakeWorker(script={"coder:T1": FakeStep(files={"linkdir/evil.txt": "x"})})
    with pytest.raises(ValueError, match="escapes the workdir"):
        await fake.run(_job(tmp_path, "coder", "T1"))
    assert not (outside / "evil.txt").exists()

    escape = tmp_path.parent / f"{tmp_path.name}-escape" / "alias-target.txt"
    (tmp_path / "alias.txt").symlink_to(escape)
    fake = FakeWorker(script={"coder:T1": FakeStep(files={"alias.txt": "x"})})
    with pytest.raises(ValueError, match="escapes the workdir"):
        await fake.run(_job(tmp_path, "coder", "T1"))
    assert not escape.exists()


async def test_scripted_files_and_crash(tmp_path: Path) -> None:
    fake = FakeWorker(
        script={
            "coder:T1": FakeStep(files={"src/a.py": "x = 1\n"}),
            "coder:T2": FakeStep(crash=True),
        }
    )
    await fake.run(_job(tmp_path, "coder", "T1"))
    assert (tmp_path / "src" / "a.py").read_text() == "x = 1\n"
    with pytest.raises(FakeWorkerCrash):
        await fake.run(_job(tmp_path, "coder", "T2"))


def test_registry() -> None:
    fake = FakeWorker()
    cfg = JevConfig.model_validate({"workers": {"codex": {"enabled": False, "command": "codex"}}})
    registry = WorkerRegistry.from_config(cfg, {"codex": fake, "api": fake})
    assert registry.names() == ["api"]
    assert registry.get("api") is fake
    with pytest.raises(WorkerNotAvailable):
        registry.get("codex")
    assert not WorkerRegistry()
    registry.register("claude_code", fake)
    assert registry.names() == ["api", "claude_code"]
