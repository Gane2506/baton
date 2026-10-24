"""`jev ship`: open a GitHub pull request for a finished run (docs/ship.md).

`prepare_ship` checks every precondition and builds a `ShipPlan` (remote, branch, base, title,
body, the exact commands) without changing anything; `execute_ship` runs it:

    git push <remote> jev/<run>/result:refs/heads/<branch_prefix><run_id>
    gh pr create --repo <owner/repo> --head <branch> --base <base> --title ... --body-file ...

It never force-pushes, never touches the user's checkout (it pushes an existing local branch)
and never enables auto-merge. `gh` runs with the user's own login. `sync_pr` reads a PR's state
back with `gh pr view --json` for the KPIs.

After D6 votes "ship" (issue #68), the pipeline's SHIPPING step runs `ship_and_merge`: the same
push and `gh pr create` (ready, not draft), then it polls the PR's checks and runs
`gh pr merge --<method> --match-head-commit <voted commit>`, so only the commit the votes
approved is ever merged. Anything unexpected raises `ShipError`, and the run stops for a person.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from jev_agent.config.schema import JevConfig, MergeMethod, ShipConfig, ShipMode
from jev_agent.core.kpi import (
    PR_VIEW_FIELDS,
    DiffStat,
    RunKpis,
    ci_status,
    compute_run_kpis,
    diff_stat,
    github_kpis,
    is_revert_of,
    pr_state,
)
from jev_agent.core.models import RunState, TaskCard
from jev_agent.core.store import (
    DecisionRecord,
    NotFoundError,
    PrState,
    ShippedPrRecord,
    Store,
)
from jev_agent.core.workspace import (
    GIT_QUICK_TIMEOUT_S,
    GIT_SLOW_TIMEOUT_S,
    result_branch,
    run_git,
    start_ref,
    timeout_message,
)
from jev_agent.quality.report import cell, latest_per_gate
from jev_agent.roles.schemas import SEVERITIES
from jev_agent.runlog.redact import redact

TITLE_MAX = 72
FOOTER = "Opened by Baton Agent Orchestrator (`jev ship`)."
_PR_URL = re.compile(r"https?://\S+/pull/(\d+)")
_GITHUB_URL = re.compile(
    r"^(?:[a-z][a-z0-9+]*://)?(?:[^@/]+@)?(?P<host>[^/:@]+)(?::\d+)?[/:]"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


class ShipError(Exception):
    """`jev ship` cannot (or did not) go ahead. The message is user-facing."""


class GhTimeoutError(ShipError):
    """`gh` did not finish in time: what it was doing may or may not have happened."""


# ---- running git and gh ------------------------------------------------------------------


def _git(root: Path, *args: str, slow: bool = False) -> subprocess.CompletedProcess[str]:
    """Run git without ever prompting (no terminal, no askpass program; credential helpers
    still work), bounded by a timeout: `slow` (the push) gets GIT_SLOW_TIMEOUT_S."""
    timeout_s = GIT_SLOW_TIMEOUT_S if slow else GIT_QUICK_TIMEOUT_S
    env = os.environ | {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
    try:
        return run_git(args, root, timeout_s, env=env)
    except subprocess.TimeoutExpired as exc:
        raise ShipError(timeout_message(args, timeout_s)) from exc


class Gh:
    """Runs the GitHub CLI (`gh`) in the repo, never interactively. Tests put a fake `gh`
    first on PATH (or pass `executable`)."""

    def __init__(self, cwd: Path, executable: str = "gh", timeout_s: float = 120) -> None:
        self.cwd = cwd
        self.executable = executable
        self.timeout_s = timeout_s

    def path(self) -> str | None:
        return shutil.which(self.executable)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ | {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
        try:
            return subprocess.run(
                [self.executable, *args],
                cwd=self.cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GhTimeoutError(
                f"gh {' '.join(args[:2])} timed out after {self.timeout_s:g} s"
            ) from exc
        except OSError as exc:
            raise ShipError(f"gh {' '.join(args[:2])} could not run: {exc}") from exc

    def out(self, *args: str) -> str:
        proc = self.run(*args)
        if proc.returncode != 0:
            detail = redact((proc.stderr or proc.stdout).strip())
            raise ShipError(f"gh {' '.join(args[:2])} failed: {detail}")
        return proc.stdout

    def json(self, *args: str) -> Any:
        text = self.out(*args)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ShipError(f"gh {' '.join(args[:2])} did not print JSON") from exc


def github_repo(url: str) -> str | None:
    """`owner/name` from a GitHub-style remote URL (https, ssh or scp form); None for local
    paths and anything else."""
    url = url.strip()
    if not url or url.startswith(("/", ".", "~", "file:")):
        return None
    match = _GITHUB_URL.match(url)
    if match is None or "." not in match.group("host"):
        return None
    return f"{match.group('owner')}/{match.group('name')}"


def pr_title(idea: str) -> str:
    """The idea's first non-empty line, at most `TITLE_MAX` characters."""
    line = next((ln.strip() for ln in idea.splitlines() if ln.strip()), "jev run")
    line = " ".join(line.split())
    return line if len(line) <= TITLE_MAX else line[: TITLE_MAX - 3].rstrip() + "..."


# ---- finding a PR already opened for the branch (issue #41) -------------------------------


@dataclass(frozen=True)
class ExistingPr:
    number: int
    url: str | None
    state: PrState


def parse_pr_list(text: str, branch: str) -> ExistingPr | None:
    """The newest PR in `gh pr list --json number,url,state,headRefName` output whose head is
    `branch`, or None. gh's output shape is not verified live (docs/verification.md section 8):
    anything unexpected (no JSON, no list, no integer number) reads as "none found"."""
    try:
        data = json.loads(text) if text.strip() else []
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        head = item.get("headRefName")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        if isinstance(head, str) and head != branch:
            continue
        url = item.get("url")
        return ExistingPr(
            number, url if isinstance(url, str) else None, pr_state(item.get("state"))
        )
    return None


def find_branch_pr(gh: Gh, repo: str, branch: str) -> ExistingPr | None:
    """A PR (any state) whose head is `branch`. Raises `ShipError` when gh cannot tell."""
    text = gh.out(
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        branch,
        "--state",
        "all",
        "--json",
        "number,url,state,headRefName",
        "--limit",
        "10",
    )
    return parse_pr_list(text, branch)


# ---- the PR body ---------------------------------------------------------------------------


def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:.2f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes = round(seconds / 60)
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"


def kpi_rows(k: RunKpis) -> list[tuple[str, str]]:
    """The KPI card, as (name, value) rows; shared by the PR body and the confirmation."""
    tests = {True: "passed", False: "failed", None: "not run"}[k.tests_passed]
    diff = (
        "-"
        if k.diff_files is None
        else f"{k.diff_files} files, +{k.diff_additions} / -{k.diff_deletions}"
    )
    lessons = f"{k.lessons_used} used" + (
        f", {k.lessons_added} added" if k.lessons_added is not None else ""
    )
    return [
        ("Total cost", _usd(k.total_cost_usd)),
        ("Wall-clock time", _duration(k.wall_clock_s)),
        ("Tasks", f"{k.tasks_done} of {k.tasks} done"),
        (
            "Attempts per task",
            "-" if k.attempts_per_task is None else f"{k.attempts_per_task:.2f}",
        ),
        ("First-pass rate", _pct(k.first_pass_rate)),
        ("Retries / escalations / reassigns", f"{k.retries} / {k.escalations} / {k.reassigns}"),
        ("Fix rounds", str(k.fix_rounds)),
        ("Integration tests", tests),
        ("Diff", diff),
        ("Free model share (api attempts)", _pct(k.free_model_share)),
        ("Lessons", lessons),
    ]


def render_pr_body(store: Store, run_id: str, kpis: RunKpis) -> str:
    """The PR description in Markdown, redacted like every log."""
    run = store.get_run(run_id)
    tasks = store.list_tasks(run_id)
    reports = store.list_quality_reports(run_id)
    lines = ["## Idea", ""]
    lines += [f"> {line}" if line.strip() else ">" for line in run.idea.text.strip().splitlines()]
    lines += ["", "## Plan", "", run.plan.summary if run.plan else "(no plan recorded)", ""]
    if tasks:
        lines += [
            "| Task | Title | Worker | Model | Effort | Attempts | Cost |",
            "| --- | --- | --- | --- | --- | ---: | ---: |",
        ]
        for t in tasks:
            row = [t.id, t.card.title, t.worker or "-", t.model or "default", t.effort or "-"]
            lines.append(
                "| " + " | ".join(cell(v) for v in row) + f" | {t.attempts} | {_usd(t.spend_usd)} |"
            )
        lines.append("")
    lines += ["## Quality", ""]
    gates = latest_per_gate([r.result for r in reports])
    if not gates:
        lines.append(f"Level `{run.quality_level}`: no quality gate ran.")
    else:
        blocked = [g for g in gates if g.blocked]
        blocking = sum(len(g.reasons) for g in blocked)
        passed = [g.counts() for g in gates if not g.blocked]
        nonblocking = {s: sum(c[s] for c in passed) for s in SEVERITIES}
        found = ", ".join(f"{n} {s}" for s, n in nonblocking.items() if n) or "none"
        verdict = (
            f"{len(blocked)} of {len(gates)} gate(s) blocked"
            if blocked
            else f"all {len(gates)} gate(s) passed"
        )
        lines += [
            f"Level `{run.quality_level}`: {verdict}.",
            "",
            f"- Blocking problems: {blocking}",
            f"- Non-blocking findings: {sum(nonblocking.values())} ({found})",
            "",
            f"The full review stays in the local repo: `jev quality show {run_id}`.",
        ]
    lines += ["", "## KPIs", "", "| KPI | Value |", "| --- | --- |"]
    lines += [f"| {cell(name)} | {cell(value)} |" for name, value in kpi_rows(kpis)]
    rules_only = kpis.decisions_total - kpis.jev_decisions - kpis.rules_fallbacks
    confidence = "-" if kpis.mean_jev_confidence is None else f"{kpis.mean_jev_confidence:.2f}"
    lines += [
        "",
        "## Decisions",
        "",
        f"{kpis.decisions_total} decisions: {kpis.jev_decisions} by Jev, "
        f"{kpis.rules_fallbacks} rules fallbacks ({_pct(kpis.fallback_rate)} of those asked of "
        f"Jev), {rules_only} by the rules dispatcher directly. Mean Jev confidence: "
        f"{confidence}.",
        "",
        "---",
        "",
        f"{FOOTER} Run `{run_id}`.",
        "",
    ]
    return redact("\n".join(lines))


# ---- preparing and shipping ----------------------------------------------------------------


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


def execute_ship(plan: ShipPlan, store: Store, root: Path, gh: Gh | None = None) -> ShippedPrRecord:
    """Push the branch, open the PR and record it (with the run's KPI snapshot).

    A PR that already exists for the branch (`plan.existing_pr`, or found after `gh pr create`
    failed or timed out) is recorded instead of opening another (issue #41).
    """
    gh = gh or Gh(root)
    if plan.existing_pr is not None:
        return _record(plan, store, plan.existing_pr)
    if not plan.skip_push:
        try:
            proc = _git(root, *plan.push_command()[1:], slow=True)
        except ShipError as exc:
            raise ShipError(
                f"{exc}. Nothing was opened; the branch may have reached {plan.remote}: run "
                f"`jev ship {plan.run_id}` again (the push is skipped if it did)"
            ) from exc
        if proc.returncode != 0:
            raise ShipError(f"git push failed (nothing was opened): {redact(proc.stderr.strip())}")
    retry = (
        f"run `jev ship {plan.run_id}` again to retry: it looks for a PR on branch "
        f"{plan.branch} first and records it instead of opening another (the push is skipped)"
    )
    with tempfile.TemporaryDirectory(prefix="jev-ship-") as tmp:
        body_file = Path(tmp) / "body.md"
        body_file.write_text(plan.body, encoding="utf-8")
        body_file.chmod(0o600)
        try:
            proc = gh.run(*plan.create_command(str(body_file))[1:])
        except GhTimeoutError as exc:
            found = _lookup_after_failure(gh, plan)
            if found is not None:
                return _record(plan, store, found)
            raise ShipError(
                f"{exc}: the outcome is unknown, GitHub may have opened the PR. "
                f"The branch {plan.branch} is on {plan.remote}; {retry}"
            ) from exc
    if proc.returncode != 0:
        detail = redact((proc.stderr or proc.stdout).strip())
        found = _lookup_after_failure(gh, plan)
        if found is not None:
            return _record(plan, store, found)
        raise ShipError(
            f"gh pr create failed: {detail}. The branch {plan.branch} is on {plan.remote}; {retry}"
        )
    number: int | None = None
    url: str | None = None
    match = _PR_URL.search(proc.stdout)
    if match:
        url, number = match.group(0), int(match.group(1))
    else:
        try:
            data = gh.json("pr", "view", plan.branch, "--repo", plan.repo, "--json", "number,url")
        except ShipError:
            data = None
        if isinstance(data, dict):
            number = data.get("number") if isinstance(data.get("number"), int) else None
            url = data.get("url") if isinstance(data.get("url"), str) else None
    return _record(plan, store, number=number, url=url)


def _lookup_after_failure(gh: Gh, plan: ShipPlan) -> ExistingPr | None:
    """After `gh pr create` failed or timed out: the PR GitHub opened anyway, if gh can say."""
    try:
        return find_branch_pr(gh, plan.repo, plan.branch)
    except ShipError:
        return None


def _record(
    plan: ShipPlan,
    store: Store,
    pr: ExistingPr | None = None,
    *,
    number: int | None = None,
    url: str | None = None,
) -> ShippedPrRecord:
    """Record the PR: one found on the branch (`pr`), or the one just opened."""
    record = store.add_shipped_pr(
        plan.run_id,
        remote=plan.remote,
        repo=plan.repo,
        branch=plan.branch,
        base=plan.base,
        number=number if pr is None else pr.number,
        url=url if pr is None else pr.url,
        title=plan.title,
        draft=plan.draft,
        head_oid=plan.head_oid,
        pushed_commits=plan.pushed_commits,
        state="open" if pr is None else pr.state,
    )
    store.set_run_kpis(plan.run_id, plan.kpis.model_dump_json())
    return record


# ---- reading PRs back (`jev ship sync`) ----------------------------------------------------


def _slim(view: dict[str, Any]) -> dict[str, Any]:
    """What is kept of `gh pr view`'s answer: counts instead of review and comment bodies."""
    slim: dict[str, Any] = {
        k: v for k, v in view.items() if k not in ("reviews", "comments", "commits")
    }
    for key in ("reviews", "comments", "commits"):
        if isinstance(view.get(key), list):
            slim[f"{key}_count"] = len(view[key])
    rollup = view.get("statusCheckRollup")
    if isinstance(rollup, list):
        slim["statusCheckRollup"] = [
            {
                k: c.get(k)
                for k in ("name", "context", "status", "conclusion", "state")
                if c.get(k) is not None
            }
            for c in rollup
            if isinstance(c, dict)
        ]
    return slim


def find_revert(gh: Gh, pr: ShippedPrRecord) -> bool:
    """Whether a merged PR reverts `pr` (`is_revert_of`), searching for its reference."""
    assert pr.number is not None
    found = gh.json(
        "pr",
        "list",
        "--repo",
        pr.repo,
        "--state",
        "merged",
        "--search",
        f'"{pr.repo}#{pr.number}"',
        "--json",
        "number,title,body",
        "--limit",
        "20",
    )
    if not isinstance(found, list):
        return False
    return any(
        isinstance(c, dict)
        and c.get("number") != pr.number
        and is_revert_of(c, pr.repo, pr.number, pr.title)
        for c in found
    )


def sync_pr(
    store: Store, gh: Gh, pr: ShippedPrRecord, *, check_revert: bool = True
) -> ShippedPrRecord:
    """Read the PR from GitHub and store its state and KPIs. Without `check_revert` (a PR
    merged moments ago) no revert is searched for and `reverted` stays unknown."""
    if pr.number is None:
        raise ShipError(f"PR on branch {pr.branch} has no number recorded; cannot sync it")
    view = gh.json(
        "pr", "view", str(pr.number), "--repo", pr.repo, "--json", ",".join(PR_VIEW_FIELDS)
    )
    if not isinstance(view, dict):
        raise ShipError(f"gh pr view {pr.number} did not return an object")
    k = github_kpis(view, pr.pushed_commits)
    reverted = find_revert(gh, pr) if k.state == "merged" and check_revert else None
    return store.update_shipped_pr(
        pr.id,
        state=k.state,
        draft=k.draft,
        merged_at=k.merged_at,
        closed_at=k.closed_at,
        review_comments=k.review_comments,
        reviews=k.reviews,
        ci_status=k.ci_status,
        commits_after_open=k.commits_after_open,
        additions=k.additions,
        deletions=k.deletions,
        changed_files=k.changed_files,
        reverted=reverted,
        raw=_slim(view),
    )


def prs_to_sync(store: Store, run_id: str | None) -> Sequence[ShippedPrRecord]:
    prs = store.list_shipped_prs(run_id)
    if run_id is not None and not prs:
        store.get_run(run_id)  # NotFoundError for an unknown run
        raise ShipError(f"run {run_id} has not been shipped")
    return prs


# ---- shipping after the D6 vote (issue #68) ------------------------------------------------

# What `ship_and_merge` reads of the PR while it waits for the checks.
MERGE_VIEW_FIELDS = ("state", "isDraft", "headRefOid", "mergeable", "statusCheckRollup")
CHANGES_TITLE = "Changes requested before shipping"


def after_approval(config: JevConfig, run_mode: ShipMode | None) -> ShipMode | None:
    """What a run does once D6 votes "ship". None: shipping is off and the run ends DONE, as
    before. Else the run's own choice (`--auto-ship` / `--ask-before-ship`), else
    `[ship] after_approval`, else "merge" under the consensus dispatcher (whose two votes are
    the gate) and "ask" with any other dispatcher."""
    if not config.ship.enabled:
        return None
    if run_mode is not None:
        return run_mode
    if config.ship.after_approval is not None:
        return config.ship.after_approval
    return "merge" if config.dispatcher.primary == "consensus" else "ask"


@dataclass(frozen=True)
class MergeCheck:
    """What to do with an open PR after one look at it: wait, merge, or stop (and why)."""

    action: Literal["wait", "merge", "merged", "stop"]
    reason: str = ""


def _failed_checks(rollup: object) -> list[str]:
    names = []
    for check in rollup if isinstance(rollup, list) else []:
        if isinstance(check, dict) and ci_status([check]) == "failure":
            names.append(str(check.get("name") or check.get("context") or "unnamed check"))
    return names


def merge_check(view: object, head_oid: str, waited_s: float, settings: ShipConfig) -> MergeCheck:
    """Decide from one `gh pr view --json` answer (`MERGE_VIEW_FIELDS`) whether the PR can be
    merged now. Only the voted commit `head_oid` is ever merged; anything that is not clearly
    fine stops for a person. gh's value shapes were verified live (docs/verification.md
    section 8, 2026-10-15); anything unexpected still stops for a person."""
    if not isinstance(view, dict):
        return MergeCheck("stop", "gh pr view did not return an object")
    state = pr_state(view.get("state"))
    if state == "merged":
        return MergeCheck("merged")
    if state == "closed":
        return MergeCheck("stop", "the PR was closed on GitHub")
    head = view.get("headRefOid")
    if not isinstance(head, str) or not head:
        return MergeCheck("stop", "gh did not report the PR's head commit")
    if head != head_oid:
        return MergeCheck(
            "stop",
            f"the PR's head moved to {head[:12]} after the vote on {head_oid[:12]}; "
            "jev only merges the commit the votes approved",
        )
    if view.get("isDraft") is True:
        return MergeCheck("stop", "the PR is a draft")
    if str(view.get("mergeable") or "").upper() == "CONFLICTING":
        return MergeCheck("stop", "the PR conflicts with its base branch")
    rollup = view.get("statusCheckRollup")
    checks = ci_status(rollup)
    if checks == "failure":
        return MergeCheck("stop", f"checks failed: {', '.join(_failed_checks(rollup))}")
    if checks == "pending":
        if waited_s < settings.checks_timeout_s:
            return MergeCheck("wait")
        return MergeCheck("stop", f"checks did not finish within {settings.checks_timeout_s} s")
    if checks == "none":
        if waited_s < settings.checks_grace_s:
            return MergeCheck("wait")
        if settings.require_checks:
            return MergeCheck(
                "stop",
                f"no checks reported after {settings.checks_grace_s} s; merge it yourself, or "
                "set `[ship] require_checks = false` to merge PRs without CI",
            )
    return MergeCheck("merge")


def merge_command(pr: ShippedPrRecord, method: MergeMethod) -> list[str]:
    """`gh pr merge`, pinned to the commit that was pushed: GitHub refuses the merge if the
    PR's head is anything else. Never `--admin` and never `--auto`."""
    assert pr.number is not None and pr.head_oid is not None
    return [
        "gh",
        "pr",
        "merge",
        str(pr.number),
        "--repo",
        pr.repo,
        f"--{method}",
        "--match-head-commit",
        pr.head_oid,
    ]


def _open_for_merge(
    root: Path, config: JevConfig, store: Store, run_id: str, gh: Gh
) -> ShippedPrRecord:
    """The run's PR for its current result commit: one an earlier try of the SHIPPING step
    recorded, else push and open it now (ready for review)."""
    head = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{result_branch(run_id)}")
    if head.returncode != 0:
        raise ShipError(f"run {run_id} has no result branch {result_branch(run_id)}")
    head_oid = head.stdout.strip()
    shipped = store.list_shipped_prs(run_id)
    for pr in reversed(shipped):
        if pr.head_oid == head_oid and pr.state != "closed":
            return pr
    plan = prepare_ship(
        root,
        config,
        store,
        run_id,
        gh=gh,
        force_new=bool(shipped),
        states=(RunState.SHIPPING,),
        draft=False,
    )
    return execute_ship(plan, store, root, gh)


def _view_for_merge(gh: Gh, pr: ShippedPrRecord) -> Any:
    assert pr.number is not None
    return gh.json(
        "pr", "view", str(pr.number), "--repo", pr.repo, "--json", ",".join(MERGE_VIEW_FIELDS)
    )


def _run_merge(gh: Gh, pr: ShippedPrRecord, method: MergeMethod) -> str:
    """Run `gh pr merge`; what went wrong ("" when gh reported success)."""
    try:
        proc = gh.run(*merge_command(pr, method)[1:])
    except GhTimeoutError as exc:
        return str(exc)
    return "" if proc.returncode == 0 else redact((proc.stderr or proc.stdout).strip())


def _merged(gh: Gh, store: Store, pr: ShippedPrRecord, failure: str) -> ShippedPrRecord:
    """Read the PR back after `gh pr merge`: GitHub's answer, not gh's exit code, says whether
    it merged (gh's answer may be lost to a timeout; a merge queue may hold the PR)."""
    try:
        synced = sync_pr(store, gh, pr, check_revert=False)
    except ShipError as exc:
        raise ShipError(
            f"cannot tell whether PR #{pr.number} merged ({failure or 'gh pr merge finished'}): "
            f"{exc}"
        ) from exc
    if synced.state == "merged":
        return synced
    if failure:
        raise ShipError(f"gh pr merge failed for PR #{pr.number}: {failure}")
    raise ShipError(
        f"gh pr merge finished but PR #{pr.number} is {synced.state}: a merge queue or a branch "
        "rule may still hold it; check it on GitHub"
    )


async def ship_and_merge(
    root: Path,
    config: JevConfig,
    store: Store,
    run_id: str,
    *,
    gh: Gh | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_wait: Callable[[], None] = lambda: None,
) -> ShippedPrRecord:
    """The SHIPPING step: open the PR (or find the one an earlier try opened), wait for its
    checks and merge it. Returns the merged PR; raises `ShipError` for anything that needs a
    person (failed or missing checks, a moved head, a closed PR, a refused merge). Safe to run
    again after a failure or a crash: the push, the PR and the merge are all looked up first.
    `on_wait` runs before every wait (the pipeline stops a cancelled run there).

    The store is used on the calling thread only (SQLite connections are per thread), so the
    steps that write it (push and open, reading the PR back) run inline; the polling and the
    merge itself run in worker threads."""
    gh = gh or Gh(root)
    pr = _open_for_merge(root, config, store, run_id, gh)
    if pr.state == "merged":
        return pr
    if pr.number is None or pr.head_oid is None:
        raise ShipError(f"the PR on branch {pr.branch} has no number recorded; check it on GitHub")
    started = clock()
    while True:
        view = await asyncio.to_thread(_view_for_merge, gh, pr)
        check = merge_check(view, pr.head_oid, clock() - started, config.ship)
        if check.action == "merged":
            return sync_pr(store, gh, pr, check_revert=False)
        if check.action == "stop":
            raise ShipError(f"PR #{pr.number} not merged: {check.reason}")
        if check.action == "merge":
            break
        on_wait()
        await sleep(config.ship.checks_poll_s)
    failure = await asyncio.to_thread(_run_merge, gh, pr, config.ship.merge_method)
    return _merged(gh, store, pr, failure)


def changes_task_card(cards: list[TaskCard], notes: str) -> TaskCard:
    """A new task, after all of `cards`, carrying a person's notes from `jev ship modify`."""
    # `default=0`: a run whose task rows are gone still gets a T1 instead of a
    # ValueError from max() on an empty sequence (issue #174 — never a 500).
    next_no = 1 + max((int(card.id[1:]) for card in cards), default=0)
    rounds = sum(1 for card in cards if card.title.startswith(CHANGES_TITLE))
    return TaskCard(
        id=f"T{next_no}",
        title=f"{CHANGES_TITLE} (round {rounds + 1})",
        description=(
            "The work passed the votes, but a person reviewing it before it ships asks for "
            "these changes. Make them, keeping what the other tasks built:\n\n"
            + redact(notes.strip())
        ),
        depends_on=[card.id for card in cards],
        acceptance_criteria=["the requested changes are made", "the existing tests still pass"],
        test_command=None,
    )


@dataclass(frozen=True)
class ShipPreview:
    """What a person sees before choosing approve, modify or stop (AWAITING_SHIP)."""

    title: str
    branch: str
    diff: DiffStat | None
    vote: DecisionRecord | None  # the latest D6 decision, with Jev's vote


def ship_preview(root: Path, store: Store, run_id: str) -> ShipPreview:
    run = store.get_run(run_id)
    votes = [d for d in store.list_decisions(run_id) if d.request.point == "D6"]
    return ShipPreview(
        title=pr_title(run.idea.text),
        branch=result_branch(run_id),
        diff=diff_stat(root, start_ref(run_id), result_branch(run_id)),
        vote=votes[-1] if votes else None,
    )
