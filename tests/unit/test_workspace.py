"""GitWorkspace robustness: git timeouts, hooks and reusing worktrees (issue #24)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from jev_agent.core.workspace import GitWorkspace, WorkspaceError, task_branch


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _hook(repo: Path, name: str, body: str) -> None:
    hook = repo / ".git" / "hooks" / name
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\n{body}\n")
    hook.chmod(0o755)


def _registered(repo: Path) -> list[str]:
    out = _git(repo, "worktree", "list", "--porcelain")
    return [line[len("worktree ") :] for line in out.splitlines() if line.startswith("worktree ")]


def test_a_hanging_hook_times_out_with_a_clear_error(fixture_repo: Path) -> None:
    # The hook's `sleep` is a grandchild that keeps git's pipes open: the whole process group
    # must be killed, or the call would still wait for it.
    _hook(fixture_repo, "pre-commit", "sleep 60")
    ws = GitWorkspace(fixture_repo, fixture_repo / ".jev", slow_timeout_s=1)
    workdir = ws.task_workdir("r1", "T1")
    (workdir / "new.txt").write_text("x\n")
    started = time.monotonic()
    with pytest.raises(WorkspaceError, match=r"git commit .*timed out after 1 s"):
        ws.commit_task(workdir, "jev: T1")
    assert time.monotonic() - started < 20


def test_a_hanging_quick_call_times_out(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = fixture_repo.parent / "hang-bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text("#!/bin/sh\nsleep 60\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    ws = GitWorkspace(fixture_repo, fixture_repo / ".jev", quick_timeout_s=0.5)
    with pytest.raises(WorkspaceError, match=r"git rev-parse .*timed out after 0\.5 s"):
        ws.start_commit("r1")


def test_hooks_run_by_default_and_can_be_skipped(fixture_repo: Path) -> None:
    _hook(fixture_repo, "pre-commit", "echo 'hook says no' >&2; exit 1")
    ws = GitWorkspace(fixture_repo, fixture_repo / ".jev")
    workdir = ws.task_workdir("r1", "T1")
    (workdir / "new.txt").write_text("x\n")
    with pytest.raises(WorkspaceError, match="hook says no"):
        ws.commit_task(workdir, "jev: T1")

    no_hooks = GitWorkspace(fixture_repo, fixture_repo / ".jev", run_hooks=False)
    assert no_hooks.commit_task(workdir, "jev: T1") is True
    assert _git(workdir, "log", "-1", "--format=%s") == "jev: T1"


def test_a_stray_directory_at_the_worktree_path_is_rebuilt(fixture_repo: Path) -> None:
    ws = GitWorkspace(fixture_repo, fixture_repo / ".jev")
    ws.start_commit("r1")
    stray = fixture_repo / ".jev" / "worktrees" / "r1" / "T1"
    stray.mkdir(parents=True)
    (stray / "junk.txt").write_text("left over\n")

    workdir = ws.task_workdir("r1", "T1")
    assert workdir == stray
    assert not (workdir / "junk.txt").exists()
    assert (workdir / "README.md").read_text() == "fixture\n"
    assert _git(workdir, "symbolic-ref", "--short", "HEAD") == task_branch("r1", "T1")
    assert str(workdir.resolve()) in _registered(fixture_repo)


def test_a_half_created_worktree_is_rebuilt_and_keeps_the_branch(fixture_repo: Path) -> None:
    ws = GitWorkspace(fixture_repo, fixture_repo / ".jev")
    workdir = ws.task_workdir("r1", "T1")
    (workdir / "work.txt").write_text("committed work\n")
    assert ws.commit_task(workdir, "jev: T1")
    # What a crash in the middle of `git worktree add` leaves: the worktree is still locked
    # ("initializing") and its checkout is incomplete.
    admin = Path(_git(workdir, "rev-parse", "--absolute-git-dir"))
    (admin / "locked").write_text("initializing")
    (workdir / "README.md").unlink()

    again = ws.task_workdir("r1", "T1")
    assert again == workdir
    assert (again / "README.md").read_text() == "fixture\n"
    assert (again / "work.txt").read_text() == "committed work\n"  # the branch kept its work
    assert "locked" not in _git(fixture_repo, "worktree", "list", "--porcelain")


def test_a_valid_worktree_is_reused_as_is(fixture_repo: Path) -> None:
    ws = GitWorkspace(fixture_repo, fixture_repo / ".jev")
    workdir = ws.task_workdir("r1", "T1")
    (workdir / "uncommitted.txt").write_text("keep me\n")
    assert ws.task_workdir("r1", "T1") == workdir
    assert (workdir / "uncommitted.txt").read_text() == "keep me\n"


def test_a_missing_registered_worktree_is_recreated(fixture_repo: Path) -> None:
    ws = GitWorkspace(fixture_repo, fixture_repo / ".jev")
    workdir = ws.task_workdir("r1", "T1")
    shutil.rmtree(workdir)  # git still lists it
    again = ws.task_workdir("r1", "T1")
    assert (again / "README.md").exists()
    assert _git(again, "symbolic-ref", "--short", "HEAD") == task_branch("r1", "T1")
