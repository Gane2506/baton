"""`prepare_ship`: check every precondition and build a `ShipPlan`.

`prepare_ship` changes nothing; `execute_ship` (in `execute.py`) runs the plan.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from jev_agent.config.schema import JevConfig
from jev_agent.core.kpi import RunKpis, compute_run_kpis, diff_stat
from jev_agent.core.models import RunState
from jev_agent.core.ship.errors import ShipError
from jev_agent.core.ship.gh import Gh, _git
from jev_agent.core.ship.pr_body import render_pr_body
from jev_agent.core.ship.pr_lookup import ExistingPr, find_branch_pr
from jev_agent.core.ship.urls import github_repo, pr_title
from jev_agent.core.store import NotFoundError, Store
from jev_agent.core.workspace import result_branch, start_ref
from jev_agent.quality.report import latest_per_gate
from jev_agent.runlog.redact import redact


@dataclass
class ShipPlan:
    run_id: str
    remote: str
    remote_url: str
    repo: str
    branch: str
    base: str
    title: str
    body: str
    draft: bool
    labels: list[str]
    head_oid: str
    pushed_commits: int | None
    kpis: RunKpis
    skip_push: bool = False  # the branch is already on the remote at this commit
    # A PR already open (or closed) for the branch: recorded instead of opening another.
    existing_pr: ExistingPr | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def source(self) -> str:
        return result_branch(self.run_id)

    def push_command(self) -> list[str]:
        # Never `--force`: an existing branch with other commits makes the push fail.
        return ["git", "push", self.remote, f"{self.source}:refs/heads/{self.branch}"]

    def create_command(self, body_file: str) -> list[str]:
        command = [
            "gh",
            "pr",
            "create",
            "--repo",
            self.repo,
            "--head",
            self.branch,
            "--base",
            self.base,
            "--title",
            self.title,
            "--body-file",
            body_file,
        ]
        if self.draft:
            command.append("--draft")
        for label in self.labels:
            command += ["--label", label]
        return command

    def summary(self) -> list[tuple[str, str]]:
        sections = [line[3:] for line in self.body.splitlines() if line.startswith("## ")]
        return [
            ("remote", f"{self.remote} ({self.remote_url})"),
            ("repo", self.repo),
            (
                "branch",
                f"{self.source} -> {self.branch}"
                + (" (already pushed, push skipped)" if self.skip_push else ""),
            ),
            ("base", self.base),
            *(
                [("existing PR", f"#{e.number} ({e.state}): recorded, no new PR is opened")]
                if (e := self.existing_pr) is not None
                else []
            ),
            ("title", self.title),
            ("draft", "yes" if self.draft else "no"),
            ("labels", ", ".join(self.labels) or "-"),
            ("body", f"{len(self.body)} characters: {', '.join(sections)}"),
        ]


def _remote_head(root: Path, remote: str, branch: str) -> str | None:
    """The commit `branch` points to on `remote`, or None when it does not exist there."""
    proc = _git(root, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    if proc.returncode != 0:
        detail = redact(proc.stderr.strip()) or "git ls-remote failed"
        raise ShipError(f"cannot reach remote {remote!r}: {detail}")
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}":
            return sha.strip()
    return None


def _default_base(root: Path, config: JevConfig, gh: Gh | None, repo: str | None) -> str | None:
    if config.ship.base:
        return config.ship.base
    remote = config.ship.remote
    proc = _git(root, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    if proc.returncode == 0 and proc.stdout.strip().startswith(f"{remote}/"):
        return proc.stdout.strip()[len(remote) + 1 :]
    if gh is not None and repo is not None:
        try:
            data = gh.json("repo", "view", repo, "--json", "defaultBranchRef")
        except ShipError:
            data = None
        name = (data or {}).get("defaultBranchRef", {}) if isinstance(data, dict) else {}
        if isinstance(name, dict) and isinstance(name.get("name"), str) and name["name"]:
            return str(name["name"])
    proc = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def check_gh(gh: Gh) -> str | None:
    """Why `gh` cannot be used (not installed, not logged in), or None."""
    if gh.path() is None:
        return (
            f"`{gh.executable}` (the GitHub CLI) is not installed or not on PATH; "
            "install it from https://cli.github.com and run `gh auth login`"
        )
    proc = gh.run("auth", "status")
    if proc.returncode != 0:
        return "the GitHub CLI is not logged in: run `gh auth login`"
    return None


def prepare_ship(
    root: Path,
    config: JevConfig,
    store: Store,
    run_id: str,
    *,
    gh: Gh | None = None,
    force_new: bool = False,
    dry_run: bool = False,
    states: Collection[RunState] = (RunState.DONE,),
    draft: bool | None = None,
) -> ShipPlan:
    """Check every precondition and build the plan. Raises `ShipError` with the reason.

    With `dry_run`, problems with `gh` (missing, logged out) become warnings, so the body and
    commands can be previewed anywhere. `states` are the run states that may ship (`jev ship`:
    DONE; the pipeline: SHIPPING); `draft` overrides `[ship] draft`.
    """
    settings = config.ship
    if not settings.enabled:
        raise ShipError(
            "shipping is off in this repo: set `[ship] enabled = true` in jev.toml "
            "(see docs/ship.md)"
        )
    try:
        run = store.get_run(run_id)
    except NotFoundError as exc:
        raise ShipError(str(exc)) from exc
    if run.state not in states:
        allowed = " or ".join(states)
        raise ShipError(f"run {run_id} is {run.state}; only a {allowed} run can be shipped")
    source = result_branch(run_id)
    head = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{source}")
    if head.returncode != 0:
        raise ShipError(f"run {run_id} has no result branch {source} in this repo")
    head_oid = head.stdout.strip()
    if settings.require_quality_pass:
        blocked = [
            g.label
            for g in latest_per_gate([r.result for r in store.list_quality_reports(run_id)])
            if g.blocked
        ]
        if blocked:
            raise ShipError(
                f"the quality gate blocked run {run_id} ({', '.join(blocked)}); see "
                f"`jev quality show {run_id}`, or set `[ship] require_quality_pass = false`"
            )
        below_bar = [
            f"{g.label} ({g.score.total}/{g.score.threshold})"
            for g in latest_per_gate([r.result for r in store.list_quality_reports(run_id)])
            if g.score is not None and not g.score.passed
        ]
        if below_bar:
            raise ShipError(
                f"run {run_id} scored below the ship threshold ({', '.join(below_bar)}); see "
                f"`jev quality show {run_id}`, or set `[ship] require_quality_pass = false`"
            )
    shipped = store.list_shipped_prs(run_id)
    if shipped and not force_new:
        last = shipped[-1]
        where = last.url or f"branch {last.branch}"
        raise ShipError(
            f"run {run_id} was already shipped ({where}); use --force-new to open another PR "
            "from a new branch"
        )
    remote = settings.remote
    url_proc = _git(root, "remote", "get-url", remote)
    if url_proc.returncode != 0:
        raise ShipError(f"this repo has no remote {remote!r}; add one or set `[ship] remote`")
    remote_url = url_proc.stdout.strip()

    warnings: list[str] = []
    gh = gh or Gh(root)
    problem = check_gh(gh)
    usable_gh: Gh | None = gh
    if problem is not None:
        if not dry_run:
            raise ShipError(problem)
        warnings.append(problem)
        usable_gh = None
    repo = github_repo(remote_url)
    if repo is None and usable_gh is not None:
        try:
            data = usable_gh.json("repo", "view", "--json", "nameWithOwner")
        except ShipError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("nameWithOwner"), str):
            repo = data["nameWithOwner"]
    if repo is None:
        if not dry_run:
            raise ShipError(
                f"cannot tell the GitHub repository of remote {remote!r} ({remote_url}); "
                "`gh repo view` did not say either"
            )
        warnings.append(f"cannot tell the GitHub repository of remote {remote!r}")
        repo = "OWNER/REPO"
    base = _default_base(root, config, usable_gh, repo)
    if base is None:
        raise ShipError("cannot tell the base branch; set `[ship] base` in jev.toml")

    name = f"{settings.branch_prefix}{run_id}"
    taken = {pr.branch for pr in shipped}
    branch = name
    skip_push = False
    number = 1
    while True:
        if branch not in taken:
            on_remote = _remote_head(root, remote, branch)
            if on_remote is None:
                break
            if on_remote == head_oid and not shipped:
                skip_push = True  # pushed by an earlier `jev ship` whose PR step failed
                break
            if not force_new:
                raise ShipError(
                    f"branch {branch} already exists on {remote} with other commits; jev never "
                    "force-pushes. Delete it there, or use --force-new for a new branch name"
                )
        number += 1
        branch = f"{name}-{number}"

    existing: ExistingPr | None = None
    if skip_push and usable_gh is not None:
        # An earlier `jev ship` pushed this branch; its PR may exist although gh's answer was
        # lost (a timeout or an error after GitHub opened it).
        try:
            existing = find_branch_pr(usable_gh, repo, branch)
        except ShipError as exc:
            if not dry_run:
                raise ShipError(
                    f"cannot tell whether a PR is already open for branch {branch}: {exc}. "
                    f"Run `jev ship {run_id}` again"
                ) from exc
            warnings.append(f"cannot tell whether a PR is already open for {branch}")

    count = _git(root, "rev-list", "--count", f"{start_ref(run_id)}..{head_oid}")
    pushed = int(count.stdout.strip()) if count.returncode == 0 else None
    kpis = compute_run_kpis(store, run_id, diff_stat(root, start_ref(run_id), head_oid))
    return ShipPlan(
        run_id=run_id,
        remote=remote,
        remote_url=redact(remote_url),
        repo=repo,
        branch=branch,
        base=base,
        title=pr_title(run.idea.text),
        body=render_pr_body(store, run_id, kpis),
        draft=settings.draft if draft is None else draft,
        labels=list(settings.labels),
        head_oid=head_oid,
        pushed_commits=pushed,
        kpis=kpis,
        skip_push=skip_push,
        existing_pr=existing,
        warnings=warnings,
    )
