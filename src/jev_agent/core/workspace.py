"""Git worktrees for coder tasks and the integration branch (PLAN.md section 10).

The first time a run touches git, its start commit (the repository's HEAD then) is recorded as
the ref `refs/jev/<run_id>/start`. Every branch of the run grows from that commit, so a resumed
run builds the same branches even if the user has moved HEAD since.

- A task without dependencies works on branch `jev/<run_id>/<task_id>`, forked from the start.
- A task with dependencies forks from a temporary base branch `jev/<run_id>/base-<task_id>`:
  the start commit with its dependencies' branches merged in, in dependency order. A merge
  conflict there raises `MergeConflict`; jev never resolves conflicts itself.
- Integration merges the task branches into `jev/<run_id>/result`, also from the start.
- `cleanup` removes a finished run's worktrees and base branches; task and result branches stay.

Worktrees live under `<state_dir>/worktrees/<run_id>/`. Nothing here pushes, ever.

Every git call has a timeout (`run_git`): a hung hook or a stale `index.lock` raises
`WorkspaceError` instead of hanging the run. Calls that can run repo hooks, check out a tree or
reach the network get `GIT_SLOW_TIMEOUT_S`; the rest get `GIT_QUICK_TIMEOUT_S`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# Files `commit_task` never commits (issue #201): a worker's stray `.env` or private
# key must never land on a branch that can be merged and shipped. `git add -A`
# already respects `.gitignore`, so this is the backstop for repos that do not
# ignore them. Matches are left in the worktree (unstaged for new files, modified
# but uncommitted for tracked ones) so nothing is lost, and the skip is logged.
# Deliberately name-based, not content-based: a content scan would false-positive
# on docs and fixtures, while these names are where real secrets live. The match
# is on the file's stem (`credentials`, `secret`, `secrets`) as well as its full
# name, so a credential file with an unexpected extension (`credentials.txt`,
# `secrets.env` — issue #224) is covered too. A stem that merely *contains*
# the word (`my-credentials.txt`) is not matched: the file must be the secret,
# not a doc about one.
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_SECRET_BASENAMES = frozenset(
    {
        ".env",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        ".netrc",
    }
)
_SECRET_STEMS = frozenset({"credentials", "secret", "secrets"})


def _is_secret_path(path: str) -> bool:
    """Whether a staged path looks like a secret file (see `_SECRET_BASENAMES`)."""
    name = path.rsplit("/", 1)[-1]
    stem = name.split(".", 1)[0] or name  # "credentials.txt" -> "credentials"; ".env" -> ".env"
    return (
        name in _SECRET_BASENAMES
        or stem in _SECRET_STEMS
        or name.startswith(".env.")
        or name.endswith(_SECRET_SUFFIXES)
    )


# Commits made by jev use a fixed identity and never sign, so they work on any machine.
_GIT_IDENTITY = [
    "-c",
    "user.name=jev",
    "-c",
    "user.email=jev@localhost",
    "-c",
    "commit.gpgsign=false",
]
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

GIT_QUICK_TIMEOUT_S = 60.0  # plumbing: rev-parse, update-ref, for-each-ref, diff, ls-remote ...
GIT_SLOW_TIMEOUT_S = 600.0  # commit, merge, add, worktree add/remove, push: hooks, checkouts
_KILL_GRACE_S = 5.0  # between SIGTERM (git removes its lock files) and SIGKILL


def run_git(
    args: Sequence[str],
    cwd: Path,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `git <args>` in its own process group and return the finished process.

    On timeout the whole group (git and any hook it started) is terminated, then killed, and
    `subprocess.TimeoutExpired` is raised. Callers map it to their own error. Git runs without a
    controlling terminal and with stdin closed, so it can never wait on a terminal prompt.
    """
    proc = subprocess.Popen(
        ["git", *args],
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # own process group, so a timeout reaches the hooks too
    )
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except BaseException:  # the timeout, or Ctrl-C (which does not reach the new session)
        _kill_group(proc)
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _kill_group(proc: subprocess.Popen[str]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            proc.communicate(timeout=_KILL_GRACE_S)  # also closes the pipes
            return
        except subprocess.TimeoutExpired:
            continue


def timeout_message(args: Sequence[str], timeout_s: float) -> str:
    shown = list(args)
    while len(shown) > 1 and shown[0] == "-c":  # leave out `-c key=value` options
        shown = shown[2:]
    return (
        f"git {' '.join(shown)} timed out after {timeout_s:g} s and was stopped "
        "(a hung hook, a credential prompt or a stale .git/index.lock?)"
    )


class WorkspaceError(Exception):
    """A git operation failed. The message includes git's output."""


class MergeConflict(WorkspaceError):
    """A task's dependencies could not be merged into its base branch."""


@dataclass(frozen=True)
class IntegrationResult:
    branch: str
    workdir: Path
    merged: list[str] = field(default_factory=list)  # task ids merged cleanly
    conflicts: list[str] = field(default_factory=list)  # task ids that did not merge

    @property
    def clean(self) -> bool:
        return not self.conflicts


class Workspace(Protocol):
    def start_commit(self, run_id: str) -> str:
        """The commit the run's branches grow from; recorded on the first call."""
        ...

    def task_workdir(self, run_id: str, task_id: str, depends_on: Sequence[str] = ()) -> Path:
        """Create (or reuse) the task's worktree, based on its dependencies, and return it.

        `depends_on` must be in dependency order. Raises `MergeConflict` if they do not merge.
        """
        ...

    def commit_task(self, workdir: Path, message: str) -> bool:
        """Commit all changes in `workdir`; return False if there was nothing to commit.

        Secret-looking staged files are never committed (issue #201): see
        `_is_secret_path`. They are unstaged and left in the worktree.
        """

    def task_diff(self, run_id: str, task_id: str) -> str:
        """The task branch's own changes, relative to the base it forked from."""
        ...

    def integrate(self, run_id: str, task_ids: list[str]) -> IntegrationResult:
        """Merge the task branches, in order, into a fresh `jev/<run_id>/result` branch."""
        ...

    def result_diff(self, run_id: str) -> str:
        """The result branch's changes relative to the run's start commit."""
        ...

    def cleanup(self, run_id: str) -> None:
        """Remove the run's worktrees and base branches; keep task and result branches."""
        ...


def task_branch(run_id: str, task_id: str) -> str:
    return f"jev/{run_id}/{task_id}"


def base_branch(run_id: str, task_id: str) -> str:
    return f"jev/{run_id}/base-{task_id}"


def result_branch(run_id: str) -> str:
    return f"jev/{run_id}/result"


def start_ref(run_id: str) -> str:
    return f"refs/jev/{run_id}/start"


class GitWorkspace:
    """`Workspace` backed by `git worktree`; worktrees live under `<state_dir>/worktrees/`.

    `run_hooks=False` passes `--no-verify` to jev's commits and merges, so the repo's
    pre-commit, commit-msg and pre-merge-commit hooks do not run on jev's branches.
    """

    def __init__(
        self,
        repo: Path,
        state_dir: Path,
        *,
        run_hooks: bool = True,
        quick_timeout_s: float = GIT_QUICK_TIMEOUT_S,
        slow_timeout_s: float = GIT_SLOW_TIMEOUT_S,
    ) -> None:
        self.repo = repo
        self.root = state_dir / "worktrees"
        self.run_hooks = run_hooks
        self.quick_timeout_s = quick_timeout_s
        self.slow_timeout_s = slow_timeout_s

    def start_commit(self, run_id: str) -> str:
        ref = start_ref(run_id)
        if not self._ref_exists(ref):
            self._git("update-ref", ref, self._git("rev-parse", "HEAD").strip())
        return self._git("rev-parse", ref).strip()

    def task_workdir(self, run_id: str, task_id: str, depends_on: Sequence[str] = ()) -> Path:
        path = self._run_dir(run_id) / task_id
        branch = task_branch(run_id, task_id)
        if path.exists():
            if self._is_usable_worktree(path, branch):
                return path
            # A stray directory, or a worktree a crash left half-created: build it again.
            self._remove_worktree(path)
        self._git("worktree", "prune")  # forget worktrees whose directory is gone
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._ref_exists(f"refs/heads/{branch}"):  # worktree removed; keep the branch's work
            self._git("worktree", "add", str(path), branch, slow=True)
        else:
            base = self._build_base(run_id, task_id, depends_on)
            self._git("worktree", "add", "-b", branch, str(path), base, slow=True)
        return path

    def commit_task(self, workdir: Path, message: str) -> bool:
        self._git("add", "-A", cwd=workdir, slow=True)
        staged = [
            path
            for path in self._git("diff", "--cached", "--name-only", "-z", cwd=workdir).split("\0")
            if path
        ]
        blocked = [path for path in staged if _is_secret_path(path)]
        if blocked:
            # Unstage only: new files stay untracked, tracked files keep their
            # modification in the worktree — nothing is deleted, nothing ships.
            self._git("reset", "-q", "--", *blocked, cwd=workdir)
            log.warning(
                "commit_task: not committing secret-looking file(s) in %s: %s",
                workdir,
                ", ".join(blocked),
            )
        if self._run(["diff", "--cached", "--quiet"], workdir, slow=False).returncode == 0:
            return False
        self._git(
            *_GIT_IDENTITY, "commit", "-q", *self._no_verify, "-m", message, cwd=workdir, slow=True
        )
        return True

    def task_diff(self, run_id: str, task_id: str) -> str:
        branch = task_branch(run_id, task_id)
        base_ref = f"refs/heads/{base_branch(run_id, task_id)}"
        if self._ref_exists(base_ref):
            base = base_ref
        elif self._ref_exists(start_ref(run_id)):
            base = start_ref(run_id)
        else:  # a run started before start commits were recorded
            base = self._git("merge-base", "HEAD", branch).strip()
        return self._git("diff", base, branch)

    def integrate(self, run_id: str, task_ids: list[str]) -> IntegrationResult:
        branch = result_branch(run_id)
        path = self._run_dir(run_id) / "_result"
        self._remove_worktree(path)  # a previous integration: start again from scratch
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "-B", branch, str(path), self.start_commit(run_id), slow=True)
        merged: list[str] = []
        conflicts: list[str] = []
        for task_id in task_ids:
            if self._merge(path, task_branch(run_id, task_id)) is None:
                merged.append(task_id)
            else:
                conflicts.append(task_id)
        return IntegrationResult(branch, path, merged, conflicts)

    def result_diff(self, run_id: str) -> str:
        return self._git("diff", start_ref(run_id), result_branch(run_id))

    def cleanup(self, run_id: str) -> None:
        run_dir = self._run_dir(run_id)
        if run_dir.exists():
            for child in sorted(run_dir.iterdir()):
                self._remove_worktree(child)
            shutil.rmtree(run_dir, ignore_errors=True)
        self._git("worktree", "prune")
        pattern = f"refs/heads/{base_branch(run_id, '*')}"
        for ref in self._git("for-each-ref", "--format=%(refname)", pattern).split():
            self._git("update-ref", "-d", ref)

    # ---- helpers ---------------------------------------------------------------------------

    def _build_base(self, run_id: str, task_id: str, depends_on: Sequence[str]) -> str:
        """The commit or branch the task forks from: the start, plus its dependencies."""
        start = self.start_commit(run_id)
        if not depends_on:
            return start
        base = base_branch(run_id, task_id)
        tmp = self._run_dir(run_id) / f"_base-{task_id}"
        self._remove_worktree(tmp)  # left over from an interrupted attempt
        self._git("worktree", "add", "-B", base, str(tmp), start, slow=True)
        try:
            for dep in depends_on:
                files = self._merge(tmp, task_branch(run_id, dep))
                if files is not None:
                    self._remove_worktree(tmp)
                    self._git("branch", "-D", base)
                    listed = ", ".join(files) or "unknown files"
                    raise MergeConflict(
                        f"dependency {dep} does not merge into the base of {task_id} "
                        f"(conflicts in: {listed})"
                    )
        finally:
            self._remove_worktree(tmp)
        return base

    def _merge(self, workdir: Path, branch: str) -> list[str] | None:
        """Merge `branch` into `workdir`; None on success, else the conflicting files."""
        args = [*_GIT_IDENTITY, "merge", "--no-ff", "--no-edit", *self._no_verify, branch]
        if self._run(args, workdir, slow=True).returncode == 0:
            return None
        files = self._git("diff", "--name-only", "--diff-filter=U", cwd=workdir).split()
        self._run(["merge", "--abort"], workdir, slow=True)
        return files

    @property
    def _no_verify(self) -> list[str]:
        return [] if self.run_hooks else ["--no-verify"]

    def _is_usable_worktree(self, path: Path, branch: str) -> bool:
        """`path` is a complete worktree of this repo, registered, with `branch` checked out.

        `git worktree add` keeps a new worktree locked ("initializing") until its checkout is
        done, so a locked one is what a crash during `worktree add` leaves behind.
        """
        inside = self._run(["rev-parse", "--is-inside-work-tree"], path, slow=False)
        if inside.stdout.strip() != "true":
            return False
        top = self._run(["rev-parse", "--show-toplevel"], path, slow=False).stdout.strip()
        if not top or Path(top).resolve() != path.resolve():
            return False  # a plain directory inside some other work tree
        entry = self._worktree_entry(path)
        return (
            entry is not None
            and f"branch refs/heads/{branch}" in entry
            and not any(line == "locked" or line.startswith("locked ") for line in entry)
        )

    def _worktree_entry(self, path: Path) -> list[str] | None:
        """The `git worktree list --porcelain` lines for `path`, or None if not registered."""
        target = path.resolve()
        for block in self._git("worktree", "list", "--porcelain").split("\n\n"):
            lines = block.splitlines()
            if lines and Path(lines[0].removeprefix("worktree ")).resolve() == target:
                return lines
        return None

    def _run_dir(self, run_id: str) -> Path:
        if not _SAFE_NAME.match(run_id):
            raise WorkspaceError(f"unsafe run id {run_id!r}")
        return self.root / run_id

    def _remove_worktree(self, path: Path) -> None:
        """Remove a worktree (or stray directory), but only one inside `self.root`."""
        if not path.exists():
            return
        if not path.resolve().is_relative_to(self.root.resolve()) or path == self.root:
            raise WorkspaceError(f"refusing to remove {path}: not under {self.root}")
        # Twice --force: also removes a worktree left locked by an interrupted `worktree add`.
        args = ["worktree", "remove", "--force", "--force", str(path)]
        if self._run(args, self.repo, slow=True).returncode != 0:
            shutil.rmtree(path)  # not a registered worktree: a plain directory

    def _ref_exists(self, ref: str) -> bool:
        return (
            self._run(["rev-parse", "--verify", "--quiet", ref], self.repo, slow=False).returncode
            == 0
        )

    def _git(self, *args: str, cwd: Path | None = None, slow: bool = False) -> str:
        proc = self._run(list(args), cwd or self.repo, slow=slow)
        if proc.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def _run(self, args: list[str], cwd: Path, *, slow: bool) -> subprocess.CompletedProcess[str]:
        """Run git with the timeout for its kind of call; a timeout raises `WorkspaceError`."""
        timeout_s = self.slow_timeout_s if slow else self.quick_timeout_s
        try:
            return run_git(args, cwd, timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError(timeout_message(args, timeout_s)) from exc
