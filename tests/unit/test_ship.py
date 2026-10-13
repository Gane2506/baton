"""`jev ship`, `jev ship sync` and `jev kpi`.

Never touches GitHub or a real remote: `origin` is a local bare repository in tmp_path, and
`gh` is `tests/fixtures/gh/fake_gh.py` (first on PATH), which records its calls and answers
with canned JSON.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jev_agent.cli.app import app
from jev_agent.cli.wiring import FAKE_WORKERS_ENV
from jev_agent.config import JevConfig
from jev_agent.core.models import RunState
from jev_agent.core.ship import Gh, github_repo, pr_title
from jev_agent.core.store import Store
from jev_agent.quality.models import GateResult

FAKE_GH = Path(__file__).resolve().parents[1] / "fixtures" / "gh" / "fake_gh.py"
SHIP_TOML = '[dispatcher]\nprimary = "rules"\n[ship]\nenabled = true\n'
PR_URL = "https://github.com/acme/widgets/pull/42"

runner = CliRunner()


class FakeGh:
    """The fake `gh` on PATH: its canned answers and the calls it saw."""

    def __init__(self, state: Path) -> None:
        self.state = state
        self.responses: dict[str, dict[str, Any]] = {}

    def answer(self, prefix: str, stdout: str = "", code: int = 0, stderr: str = "") -> None:
        self.responses[prefix] = {"stdout": stdout, "code": code, "stderr": stderr}
        (self.state / "responses.json").write_text(json.dumps(self.responses))

    def calls(self) -> list[list[str]]:
        log = self.state / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line)["argv"] for line in log.read_text().splitlines()]

    def commands(self) -> list[str]:
        return [" ".join(argv[:2]) for argv in self.calls()]

    def body(self) -> str:
        return (self.state / "body.md").read_text()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "gh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GH}" "$@"\n')
    wrapper.chmod(0o755)
    state = tmp_path / "fake-gh"
    state.mkdir()
    monkeypatch.setenv("FAKE_GH_DIR", str(state))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    fake = FakeGh(state)
    fake.answer("repo view --json nameWithOwner", json.dumps({"nameWithOwner": "acme/widgets"}))
    fake.answer(
        "repo view acme/widgets --json defaultBranchRef",
        json.dumps({"defaultBranchRef": {"name": "main"}}),
    )
    fake.answer("pr create", PR_URL + "\n")
    return fake


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _jev(repo: Path, *args: str, stdin: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--repo", str(repo)], input=stdin)
    return result.exit_code, " ".join(result.output.split())


def _store(repo: Path) -> Store:
    return Store.open(repo / ".jev" / "state.db")


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    return bare


@pytest.fixture
def done_run(fixture_repo: Path, origin: Path, gh: FakeGh) -> tuple[Path, str]:
    """A repo with a DONE run (fake workers) and a local bare `origin` holding main."""
    (fixture_repo / "jev.toml").write_text(SHIP_TOML)
    _git(fixture_repo, "remote", "add", "origin", str(origin))
    _git(fixture_repo, "push", "-q", "origin", "main")
    code, output = _jev(fixture_repo, "run", "add search")
    assert code == 0, output
    with _store(fixture_repo) as store:
        run_id = store.list_runs()[0].id
    code, output = _jev(fixture_repo, "approve", run_id)
    assert code == 0 and "DONE" in output, output
    return fixture_repo, run_id


def _remote_branches(origin: Path) -> dict[str, str]:
    out = _git(origin, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    return dict(line.split(" ", 1) for line in out.splitlines())


# ---- jev ship ------------------------------------------------------------------------------


def test_ship_pushes_the_result_branch_and_opens_a_draft_pr(
    done_run: tuple[Path, str], origin: Path, gh: FakeGh
) -> None:
    repo, run_id = done_run
    head_before = _git(repo, "rev-parse", "HEAD")
    branch_before = _git(repo, "symbolic-ref", "--short", "HEAD")
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 0, output
    assert f"opened PR #42: {PR_URL}" in output

    result_sha = _git(repo, "rev-parse", f"jev/{run_id}/result")
    assert _remote_branches(origin)[f"jev/{run_id}"] == result_sha
    # The user's checkout is untouched.
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == branch_before

    create = next(argv for argv in gh.calls() if argv[:2] == ["pr", "create"])
    assert create[create.index("--repo") + 1] == "acme/widgets"
    assert create[create.index("--head") + 1] == f"jev/{run_id}"
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--title") + 1] == "add search"
    assert "--draft" in create
    assert not any("merge" in arg for argv in gh.calls() for arg in argv)
    assert "auth status" in gh.commands()

    body = gh.body()
    for heading in ("## Idea", "## Plan", "## Quality", "## KPIs", "## Decisions"):
        assert heading in body
    assert "| T1 |" in body and "Opened by Baton Agent Orchestrator (`jev ship`)" in body

    with _store(repo) as store:
        [pr] = store.list_shipped_prs(run_id)
        assert (pr.number, pr.url, pr.branch, pr.base, pr.repo) == (
            42,
            PR_URL,
            f"jev/{run_id}",
            "main",
            "acme/widgets",
        )
        assert pr.draft and pr.state == "open" and pr.head_oid == result_sha
        assert pr.pushed_commits is not None and pr.pushed_commits >= 1
        assert store.get_run_kpis(run_id) is not None

    # A second ship is refused; --force-new opens another PR from a new branch.
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "already shipped" in output and "--force-new" in output
    gh.answer("pr create", "https://github.com/acme/widgets/pull/43\n")
    code, output = _jev(repo, "ship", run_id, "--yes", "--force-new")
    assert code == 0, output
    assert f"jev/{run_id}-2" in _remote_branches(origin)
    with _store(repo) as store:
        assert [p.number for p in store.list_shipped_prs(run_id)] == [42, 43]


def test_ship_labels_base_and_no_draft_from_config(done_run: tuple[Path, str], gh: FakeGh) -> None:
    repo, run_id = done_run
    (repo / "jev.toml").write_text(
        SHIP_TOML + 'draft = false\nlabels = ["jev", "bot"]\nbase = "develop"\n'
        'branch_prefix = "bots/"\n'
    )
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 0, output
    create = next(argv for argv in gh.calls() if argv[:2] == ["pr", "create"])
    assert "--draft" not in create
    assert create[create.index("--base") + 1] == "develop"
    assert create[create.index("--head") + 1] == f"bots/{run_id}"
    labels = [create[i + 1] for i, arg in enumerate(create) if arg == "--label"]
    assert labels == ["jev", "bot"]


def test_dry_run_prints_body_and_commands_and_runs_nothing(
    done_run: tuple[Path, str], origin: Path, gh: FakeGh
) -> None:
    repo, run_id = done_run
    code, output = _jev(repo, "ship", run_id, "--dry-run")
    assert code == 0, output
    assert "## KPIs" in output and "dry run: nothing was run" in output
    assert f"git push origin jev/{run_id}/result:refs/heads/jev/{run_id}" in output
    assert "gh pr create --repo acme/widgets" in output and "--force" not in output
    assert f"jev/{run_id}" not in _remote_branches(origin)
    assert "pr create" not in gh.commands()
    with _store(repo) as store:
        assert store.list_shipped_prs() == []


def test_confirmation_can_be_declined(done_run: tuple[Path, str], origin: Path, gh: FakeGh) -> None:
    repo, run_id = done_run
    code, output = _jev(repo, "ship", run_id, stdin="n\n")
    assert code == 1 and "Not shipped" in output
    assert "title" in output and "base" in output and "branch" in output
    assert f"jev/{run_id}" not in _remote_branches(origin)
    assert "pr create" not in gh.commands()


def test_ship_asks_and_ships_on_yes(done_run: tuple[Path, str], gh: FakeGh) -> None:
    repo, run_id = done_run
    code, output = _jev(repo, "ship", run_id, stdin="y\n")
    assert code == 0 and "opened PR #42" in output, output


def test_refusals(done_run: tuple[Path, str], gh: FakeGh, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, run_id = done_run
    (repo / "jev.toml").write_text('[dispatcher]\nprimary = "rules"\n')
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "shipping is off" in output
    (repo / "jev.toml").write_text(SHIP_TOML)

    code, output = _jev(repo, "ship", "20990101-000000-abcd", "--yes")
    assert code == 1 and "not found" in output

    gh.answer("auth status", code=1, stderr="You are not logged into any GitHub hosts")
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "gh auth login" in output
    gh.answer("auth status")

    (repo / "jev.toml").write_text(SHIP_TOML + 'remote = "upstream"\n')
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "no remote 'upstream'" in output
    (repo / "jev.toml").write_text(SHIP_TOML)

    with _store(repo) as store:
        store.add_quality_report(
            run_id,
            GateResult(level="basic", scope="integration", blocked=True, reasons=["tests fail"]),
        )
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "quality gate blocked" in output
    (repo / "jev.toml").write_text(SHIP_TOML + "require_quality_pass = false\n")
    code, output = _jev(repo, "ship", run_id, "--dry-run")
    assert code == 0, output

    only_git = repo.parent / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git") or "/usr/bin/git")
    monkeypatch.setenv("PATH", str(only_git))
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "not installed" in output
    assert "pr create" not in gh.commands()


def test_refuses_runs_that_are_not_done(fixture_repo: Path, origin: Path, gh: FakeGh) -> None:
    (fixture_repo / "jev.toml").write_text(SHIP_TOML)
    _git(fixture_repo, "remote", "add", "origin", str(origin))
    code, output = _jev(fixture_repo, "run", "add search")
    assert code == 0
    with _store(fixture_repo) as store:
        run_id = store.list_runs()[0].id
    code, output = _jev(fixture_repo, "ship", run_id, "--yes")
    assert code == 1 and "AWAITING_APPROVAL" in output and "only a DONE run" in output


def test_never_overwrites_a_remote_branch_with_other_commits(
    done_run: tuple[Path, str], origin: Path, gh: FakeGh
) -> None:
    repo, run_id = done_run
    _git(repo, "push", "-q", "origin", f"main:refs/heads/jev/{run_id}")
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "never force-pushes" in output
    assert _remote_branches(origin)[f"jev/{run_id}"] == _git(repo, "rev-parse", "main")
    # --force-new picks the next free name instead.
    code, output = _jev(repo, "ship", run_id, "--yes", "--force-new")
    assert code == 0, output
    assert f"jev/{run_id}-2" in _remote_branches(origin)


def test_retry_after_pr_create_failed_skips_the_push(
    done_run: tuple[Path, str], origin: Path, gh: FakeGh
) -> None:
    repo, run_id = done_run
    gh.answer("pr create", code=1, stderr="GraphQL: Base branch not found")
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "gh pr create failed" in output and "again to retry" in output
    assert f"jev/{run_id}" in _remote_branches(origin)
    with _store(repo) as store:
        assert store.list_shipped_prs() == []
    gh.answer("pr create", "")  # no URL printed: jev asks `gh pr view` for it
    gh.answer(f"pr view jev/{run_id}", json.dumps({"number": 7, "url": PR_URL[:-2] + "7"}))
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 0 and "already pushed" in output and "opened PR #7" in output, output


def test_base_branch_comes_from_the_remote_head(done_run: tuple[Path, str], gh: FakeGh) -> None:
    from jev_agent.core.ship import _default_base

    repo, _ = done_run
    config = JevConfig.model_validate({"ship": {"enabled": True}})
    # Without refs/remotes/origin/HEAD, gh repo view answers (the fake says "main").
    assert _default_base(repo, config, Gh(repo), "acme/widgets") == "main"
    _git(repo, "update-ref", "refs/remotes/origin/trunk", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    calls = len(gh.calls())
    assert _default_base(repo, config, Gh(repo), "acme/widgets") == "trunk"
    assert len(gh.calls()) == calls  # found locally, gh not asked
    # Without gh and a remote HEAD: the branch checked out in the repo.
    _git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert _default_base(repo, config, None, None) == "main"
    configured = JevConfig.model_validate({"ship": {"enabled": True, "base": "release"}})
    assert _default_base(repo, configured, None, None) == "release"


def test_push_command_never_forces() -> None:
    from jev_agent.core.kpi import RunKpis
    from jev_agent.core.ship import ShipPlan

    plan = ShipPlan(
        run_id="20261012-000000-abcd",
        remote="origin",
        remote_url="x",
        repo="a/b",
        branch="jev/20261012-000000-abcd",
        base="main",
        title="t",
        body="b",
        draft=True,
        labels=[],
        head_oid="0" * 40,
        pushed_commits=1,
        kpis=RunKpis(
            run_id="r",
            total_cost_usd=0,
            wall_clock_s=0,
            tasks=0,
            tasks_done=0,
            attempts_total=0,
            attempts_per_task=None,
            first_pass_rate=None,
            retries=0,
            escalations=0,
            reassigns=0,
            fix_rounds=0,
            quality_level="none",
        ),
    )
    assert plan.push_command() == [
        "git",
        "push",
        "origin",
        "jev/20261012-000000-abcd/result:refs/heads/jev/20261012-000000-abcd",
    ]
    assert not any(a.startswith("-f") or "force" in a for a in plan.create_command("f"))


@pytest.mark.parametrize(
    ("url", "repo"),
    [
        ("git@github.com:octo/thing.git", "octo/thing"),
        ("https://github.com/octo/thing", "octo/thing"),
        ("https://github.com/octo/thing.git/", "octo/thing"),
        ("https://user@github.com/octo/thing.git", "octo/thing"),
        ("ssh://git@github.example.com:2222/octo/my.repo.git", "octo/my.repo"),
        ("/tmp/origin.git", None),
        ("../origin.git", None),
        ("file:///tmp/origin.git", None),
        ("origin", None),
    ],
)
def test_github_repo_from_remote_url(url: str, repo: str | None) -> None:
    assert github_repo(url) == repo


def test_pr_title() -> None:
    assert pr_title("\n  Add search  \nmore detail") == "Add search"
    long = "word " * 30
    title = pr_title(long)
    assert len(title) <= 72 and title.endswith("...")


# ---- jev ship sync and jev kpi ---------------------------------------------------------------

PR_VIEW = {
    "state": "MERGED",
    "createdAt": "2026-10-12T10:00:00Z",
    "mergedAt": "2026-10-12T16:00:00Z",
    "closedAt": "2026-10-12T16:00:00Z",
    "isDraft": False,
    "reviews": [
        {"body": "LGTM with one nit", "state": "APPROVED"},
        {"body": "", "state": "APPROVED"},
    ],
    "comments": [{"body": "thanks"}, {"body": "ci is slow"}],
    "commits": [{"oid": "a"}, {"oid": "b"}, {"oid": "c"}, {"oid": "d"}, {"oid": "e"}],
    "additions": 40,
    "deletions": 3,
    "changedFiles": 4,
    "statusCheckRollup": [
        {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "StatusContext", "context": "lint", "state": "SUCCESS"},
    ],
}


def test_sync_reads_the_pr_back_and_kpi_reports_it(done_run: tuple[Path, str], gh: FakeGh) -> None:
    repo, run_id = done_run
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 0, output
    code, output = _jev(repo, "ship", "sync")
    assert code == 1 and "--all" in output
    gh.answer("pr view 42", json.dumps(PR_VIEW))
    revert = {"number": 50, "title": 'Revert "add search"', "body": "Reverts acme/widgets#42"}
    gh.answer("pr list", json.dumps([revert]))
    code, output = _jev(repo, "ship", "sync", "--all")
    assert code == 0, output
    assert "merged after" in output and "CI success" in output and "REVERTED" in output

    view = next(argv for argv in gh.calls() if argv[:2] == ["pr", "view"])
    fields = view[view.index("--json") + 1].split(",")
    assert {"state", "mergedAt", "reviews", "statusCheckRollup", "commits"} <= set(fields)
    with _store(repo) as store:
        [pr] = store.list_shipped_prs()
        assert pr.state == "merged" and pr.reverted is True and pr.ci_status == "success"
        assert pr.review_comments == 3  # two comments and one review with a body
        assert pr.reviews == 2 and pr.changed_files == 4
        assert pr.commits_after_open == 5 - (pr.pushed_commits or 0)
        assert pr.raw is not None and "comments" not in pr.raw and pr.raw["comments_count"] == 2
        assert pr.time_to_merge_h is not None and pr.last_synced_at is not None

    code, output = _jev(repo, "kpi")
    assert code == 0, output
    assert "#42" in output and "median" in output and "reverted after merging" in output

    result = runner.invoke(app, ["kpi", "--json", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    [row] = data["prs"]
    assert row["pr"]["number"] == 42 and row["pr"]["reverted"] is True
    assert row["run"]["tasks"] >= 1 and row["run"]["diff_files"] >= 1
    assert data["summary"]["prs"] == 1 and data["summary"]["merged"] == 1
    assert {t["kpi"] for t in data["trends"]} >= {"cost_usd", "first_pass_rate"}
    assert data["regressions"] == []


def test_sync_one_run_and_errors(done_run: tuple[Path, str], gh: FakeGh) -> None:
    repo, run_id = done_run
    code, output = _jev(repo, "ship", "sync", run_id)
    assert code == 1 and "has not been shipped" in output
    code, output = _jev(repo, "ship", "sync", "--all")
    assert code == 0 and "Nothing shipped yet" in output
    _jev(repo, "ship", run_id, "--yes")
    gh.answer("pr view 42", code=1, stderr="HTTP 404")
    code, output = _jev(repo, "ship", "sync", run_id)
    assert code == 1 and "gh pr view failed" in output
    gh.answer("pr view 42", json.dumps(PR_VIEW | {"state": "OPEN", "mergedAt": None}))
    code, output = _jev(repo, "ship", "sync", run_id)
    assert code == 0 and "open" in output
    assert "pr list" not in gh.commands()  # reverts are only looked for once merged


def test_kpi_without_shipped_prs(fixture_repo: Path) -> None:
    code, output = _jev(fixture_repo, "kpi")
    assert code == 0 and "No shipped PRs yet" in output and "jev ship" in output
    result = runner.invoke(app, ["kpi", "--json", "--repo", str(fixture_repo)])
    assert json.loads(result.output)["prs"] == []


def test_run_state_done_is_required_even_with_a_result_branch(
    done_run: tuple[Path, str], gh: FakeGh
) -> None:
    repo, run_id = done_run
    with _store(repo) as store:
        store.set_run_state(run_id, RunState.NEEDS_HUMAN, "stopped")
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "NEEDS_HUMAN" in output
