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
from jev_agent.config.schema import ShipConfig
from jev_agent.core.kpi import PR_VIEW_FIELDS
from jev_agent.core.models import RunState, TaskCard
from jev_agent.core.ship import (
    MERGE_VIEW_FIELDS,
    Gh,
    after_approval,
    changes_task_card,
    github_repo,
    merge_check,
    pr_title,
)
from jev_agent.core.store import Store
from jev_agent.quality.models import GateResult, ScoreBreakdown

FAKE_GH = Path(__file__).resolve().parents[1] / "fixtures" / "gh" / "fake_gh.py"
SHIP_TOML = '[dispatcher]\nprimary = "rules"\n[ship]\nenabled = true\n'
PR_URL = "https://github.com/acme/widgets/pull/42"

runner = CliRunner()


def _trust(repo: Path) -> None:
    """Trust a fixture repo in the isolated test user config.

    `[ship]` is a guarded key: an untrusted repo's jev.toml may not set it (trust gate,
    audit 2026-10-16), so the fixture repo is opted in, mirroring a user who trusts
    their own checkout. Idempotent within a test.
    """
    user_config = Path(os.environ["XDG_CONFIG_HOME"]) / "jev" / "config.toml"
    if "[trust]" not in user_config.read_text():
        with user_config.open("a") as fh:
            fh.write(f'\n[trust]\nrepos = ["{repo}"]\n')


def _ship_config(repo: Path, extra: str = "") -> None:
    """Enable shipping in the repo's jev.toml and trust the fixture repo (see `_trust`)."""
    (repo / "jev.toml").write_text(SHIP_TOML + extra)
    _trust(repo)


class FakeGh:
    """The fake `gh` on PATH: its canned answers and the calls it saw."""

    def __init__(self, state: Path) -> None:
        self.state = state
        self.responses: dict[str, Any] = {}

    def answer(self, prefix: str, stdout: str = "", code: int = 0, stderr: str = "") -> None:
        self.responses[prefix] = {"stdout": stdout, "code": code, "stderr": stderr}
        (self.state / "responses.json").write_text(json.dumps(self.responses))

    def answers(self, prefix: str, *answers: dict[str, Any]) -> None:
        """The Nth call matching `prefix` gets the Nth answer (the last one repeats)."""
        self.responses[prefix] = list(answers)
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
    monkeypatch.setenv(FAKE_WORKERS_ENV, "1")


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
    """A repo with a DONE run (fake workers) and a local bare `origin` holding main. With
    shipping on, the rules dispatcher's run waits to ship ("ask"); `jev ship stop` ends it
    DONE, not shipped, which `jev ship RUN` can still ship by hand."""
    _ship_config(fixture_repo)
    _git(fixture_repo, "remote", "add", "origin", str(origin))
    _git(fixture_repo, "push", "-q", "origin", "main")
    code, output = _jev(fixture_repo, "run", "add search")
    assert code == 0, output
    with _store(fixture_repo) as store:
        run_id = store.list_runs()[0].id
    code, output = _jev(fixture_repo, "approve", run_id)
    assert code == 0 and "AWAITING_SHIP" in output, output
    code, output = _jev(fixture_repo, "ship", "stop", run_id)
    assert code == 0 and "DONE" in output and "not shipped" in output, output
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
    assert "by Jev," in body
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
    _ship_config(
        repo,
        'draft = false\nlabels = ["jev", "bot"]\nbase = "develop"\nbranch_prefix = "bots/"\n',
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
    _ship_config(repo)

    code, output = _jev(repo, "ship", "20990101-000000-abcd", "--yes")
    assert code == 1 and "not found" in output

    gh.answer("auth status", code=1, stderr="You are not logged into any GitHub hosts")
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "gh auth login" in output
    gh.answer("auth status")

    _ship_config(repo, 'remote = "upstream"\n')
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "no remote 'upstream'" in output
    _ship_config(repo)

    with _store(repo) as store:
        store.add_quality_report(
            run_id,
            GateResult(level="basic", scope="integration", blocked=True, reasons=["tests fail"]),
        )
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "quality gate blocked" in output
    _ship_config(repo, "require_quality_pass = false\n")
    code, output = _jev(repo, "ship", run_id, "--dry-run")
    assert code == 0, output

    only_git = repo.parent / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git") or "/usr/bin/git")
    monkeypatch.setenv("PATH", str(only_git))
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "not installed" in output
    assert "pr create" not in gh.commands()


def test_refuses_below_threshold_score(done_run: tuple[Path, str], gh: FakeGh) -> None:
    """`jev ship` honors the score gate, not just blocked gates."""
    repo, run_id = done_run
    _ship_config(repo)
    low = ScoreBreakdown(
        criteria={
            "correctness": 2,
            "safety": 2,
            "simplicity": 2,
            "maintainability": 2,
            "tool_fit": 2,
            "user_preference": 3,
        },
        weights={
            "correctness": 0.30,
            "safety": 0.20,
            "simplicity": 0.15,
            "maintainability": 0.15,
            "tool_fit": 0.10,
            "user_preference": 0.10,
        },
        total=44,
        threshold=85,
        passed=False,
    )
    with _store(repo) as store:
        store.add_quality_report(
            run_id,
            GateResult(level="basic", scope="integration", score=low),
        )
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "scored below the ship threshold" in output


def test_refuses_runs_that_are_not_done(fixture_repo: Path, origin: Path, gh: FakeGh) -> None:
    _ship_config(fixture_repo)
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


def _git_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hang_on: str = "") -> Path:
    """A `git` first on PATH that logs each call's prompt settings, then runs the real git
    (or, for `hang_on`, hangs in a child process that keeps the pipes open)."""
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "git-bin"
    bin_dir.mkdir()
    log = tmp_path / "git-env.log"
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'echo "$1 prompt=${GIT_TERMINAL_PROMPT-unset} askpass=${GIT_ASKPASS-unset}"'
        f' >> "{log}"\n'
        f'if [ -n "{hang_on}" ] && [ "$1" = "{hang_on}" ]; then sleep 60; fi\n'
        f'exec "{real_git}" "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_ship_git_never_prompts(
    done_run: tuple[Path, str], gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, run_id = done_run
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("GIT_ASKPASS", "/usr/bin/some-gui-askpass")
    log = _git_wrapper(tmp_path, monkeypatch)
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 0, output
    # Every git call of ship.py (kpi.diff_stat's local `git diff` is not one of them).
    lines = [line for line in log.read_text().splitlines() if not line.startswith("diff ")]
    assert {"push", "ls-remote"} <= {line.split()[0] for line in lines}, lines
    assert all(line.endswith(" prompt=0 askpass=") for line in lines), lines


def test_ship_git_hang_times_out(
    done_run: tuple[Path, str],
    origin: Path,
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    import jev_agent.core.workspace as workspace

    repo, run_id = done_run
    monkeypatch.setattr(workspace, "GIT_QUICK_TIMEOUT_S", 0.5)
    _git_wrapper(tmp_path, monkeypatch, hang_on="ls-remote")
    started = time.monotonic()
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1
    assert "git ls-remote" in output and "timed out after 0.5 s" in output, output
    assert time.monotonic() - started < 20
    assert f"jev/{run_id}" not in _remote_branches(origin)


def test_prepare_ship_calls_latest_per_gate_once(
    done_run: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3-02 (issue #233): the quality gate list is computed once, not twice."""
    import jev_agent.core.ship.prepare as prepare
    from jev_agent.config import load_config
    from jev_agent.core.ship import Gh
    from jev_agent.core.ship.prepare import prepare_ship
    from jev_agent.quality.report import latest_per_gate

    repo, run_id = done_run
    calls: list[int] = []

    def counting(results: list[GateResult]) -> list[Any]:
        calls.append(1)
        return latest_per_gate(results)

    monkeypatch.setattr(prepare, "latest_per_gate", counting)
    with _store(repo) as store:
        prepare_ship(repo, load_config(repo), store, run_id, gh=Gh(repo), dry_run=True)
    assert len(calls) == 1


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


def test_failed_rev_list_fails_the_ship_loudly(done_run: tuple[Path, str]) -> None:
    """R4-C (issue #244): a failed `git rev-list --count` used to silently yield
    `pushed_commits=None`; the ship now fails loudly instead."""
    from jev_agent.config import load_config
    from jev_agent.core.ship import Gh
    from jev_agent.core.ship.errors import ShipError
    from jev_agent.core.ship.prepare import prepare_ship
    from jev_agent.core.workspace import start_ref

    repo, run_id = done_run
    _git(repo, "update-ref", "-d", f"refs/jev/{run_id}/start")
    assert start_ref(run_id) == f"refs/jev/{run_id}/start"
    with _store(repo) as store, pytest.raises(ShipError, match="cannot count the commits"):
        prepare_ship(repo, load_config(repo), store, run_id, gh=Gh(repo), dry_run=True)


def test_default_base_ignores_a_malformed_default_branch_ref(
    done_run: tuple[Path, str], gh: FakeGh
) -> None:
    """R4-C (issue #244): `_default_base` falls through to the checked-out branch when
    `gh repo view` answers a `defaultBranchRef` that is not a dict with a string `name`."""
    from jev_agent.config import JevConfig
    from jev_agent.core.ship import Gh, _default_base

    repo, _ = done_run
    config = JevConfig.model_validate({"ship": {"enabled": True}})
    gh.answer(
        "repo view acme/widgets --json defaultBranchRef",
        json.dumps({"defaultBranchRef": {"name": 123}}),
    )
    assert _default_base(repo, config, Gh(repo), "acme/widgets") == "main"
    gh.answer(
        "repo view acme/widgets --json defaultBranchRef",
        json.dumps({"defaultBranchRef": "main"}),
    )
    assert _default_base(repo, config, Gh(repo), "acme/widgets") == "main"
    gh.answer(
        "repo view acme/widgets --json defaultBranchRef",
        json.dumps([{"name": "main"}]),
    )
    assert _default_base(repo, config, Gh(repo), "acme/widgets") == "main"


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


# ---- a PR opened while gh reported failure or timed out (issue #41) -----------------------


def _pr_list_prefix(run_id: str) -> str:
    return f"pr list --repo acme/widgets --head jev/{run_id}"


def _listed(number: int, run_id: str, state: str = "OPEN") -> dict[str, Any]:
    return {
        "stdout": json.dumps(
            [
                {
                    "number": number,
                    "url": f"https://github.com/acme/widgets/pull/{number}",
                    "state": state,
                    "headRefName": f"jev/{run_id}",
                }
            ]
        )
    }


def test_pr_created_but_reported_failed_is_recorded_not_duplicated(
    done_run: tuple[Path, str], gh: FakeGh
) -> None:
    repo, run_id = done_run
    # gh opened the PR, then failed; the lookup right after the failure is lost too.
    gh.answer("pr create", code=1, stderr="error connecting to api.github.com")
    gh.answers(
        _pr_list_prefix(run_id), {"code": 1, "stderr": "connection reset"}, _listed(9, run_id)
    )
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 1 and "gh pr create failed" in output, output
    with _store(repo) as store:
        assert store.list_shipped_prs() == []

    # The retry finds the PR on the branch and records it instead of opening another.
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 0 and "#9" in output, output
    assert gh.commands().count("pr create") == 1
    with _store(repo) as store:
        [pr] = store.list_shipped_prs(run_id)
        assert (pr.number, pr.url, pr.state) == (
            9,
            "https://github.com/acme/widgets/pull/9",
            "open",
        )
        assert store.get_run_kpis(run_id) is not None


def test_failed_create_is_checked_at_once_and_a_closed_pr_keeps_its_state(
    done_run: tuple[Path, str], gh: FakeGh
) -> None:
    repo, run_id = done_run
    gh.answer("pr create", code=1, stderr="HTTP 502")
    gh.answer(_pr_list_prefix(run_id), **_listed(11, run_id, "MERGED"))
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 0 and "#11" in output, output
    with _store(repo) as store:
        [pr] = store.list_shipped_prs(run_id)
        assert (pr.number, pr.state) == (11, "merged")
    lookup = next(argv for argv in gh.calls() if argv[:2] == ["pr", "list"])
    assert lookup[lookup.index("--state") + 1] == "all"


def test_pr_create_timeout_is_an_unknown_outcome(done_run: tuple[Path, str], gh: FakeGh) -> None:
    from jev_agent.config.loader import load_config
    from jev_agent.core.ship import ShipError, execute_ship, prepare_ship

    repo, run_id = done_run
    gh.answer("pr create", PR_URL + "\n")
    gh.responses["pr create"]["sleep"] = 5
    gh.answer("auth status")  # rewrite responses.json with the sleep
    fast = Gh(repo, timeout_s=0.5)
    with _store(repo) as store:
        plan = prepare_ship(repo, load_config(repo), store, run_id, gh=fast)
        with pytest.raises(ShipError, match="outcome is unknown") as caught:
            execute_ship(plan, store, repo, gh=fast)
        assert f"jev ship {run_id}" in str(caught.value)
        assert store.list_shipped_prs() == []
    # Meanwhile GitHub did open it: the retry records it, with no second create.
    gh.answer("pr create", code=1, stderr="must not be called")
    gh.answer(_pr_list_prefix(run_id), **_listed(12, run_id))
    code, output = _jev(repo, "ship", run_id, "--yes")
    assert code == 0 and "#12" in output, output
    assert gh.commands().count("pr create") == 1


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json",
        json.dumps({"number": 3}),
        json.dumps([{"number": "3"}, {"url": "x"}]),
        json.dumps([{"number": 3, "headRefName": "someone/else"}]),
    ],
)
def test_pr_lookup_is_tolerant_of_unexpected_output(tmp_path: Path, stdout: str) -> None:
    from jev_agent.core.ship import parse_pr_list

    assert parse_pr_list(stdout, "jev/r1") is None


def test_pr_lookup_reads_gh_pr_list() -> None:
    from jev_agent.core.ship import ExistingPr, parse_pr_list

    text = json.dumps(
        [
            {"number": 5, "url": "https://x/pull/5", "state": "CLOSED", "headRefName": "jev/r1"},
            {"number": 4, "url": "https://x/pull/4", "state": "OPEN"},
        ]
    )
    assert parse_pr_list(text, "jev/r1") == ExistingPr(5, "https://x/pull/5", "closed")
    # A missing headRefName is no match (issue #233): an item that does not say which
    # branch it is for cannot be the PR for this branch.
    assert parse_pr_list(json.dumps([{"number": 4}]), "jev/r1") is None
    assert parse_pr_list(
        json.dumps(
            [
                {"number": 4, "state": "OPEN"},
                {"number": 6, "state": "OPEN", "headRefName": "jev/r1"},
            ]
        ),
        "jev/r1",
    ) == ExistingPr(6, None, "open")


# ---- shipping after the D6 vote (issue #68) ------------------------------------------------

MERGE_KEY = "pr view 42 --repo acme/widgets --json " + ",".join(MERGE_VIEW_FIELDS)
SYNC_KEY = "pr view 42 --repo acme/widgets --json " + ",".join(PR_VIEW_FIELDS)
GREEN = [{"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}]


def _open_view(head: str, checks: list[dict[str, Any]] | None = None, **extra: Any) -> str:
    """`gh pr view --json` for an open, ready PR at `head` (gh prints JSON on one line)."""
    view = {
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": head,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": GREEN if checks is None else checks,
    }
    return json.dumps(view | extra)


def _ship_repo(fixture_repo: Path, origin: Path, toml: str = SHIP_TOML) -> Path:
    (fixture_repo / "jev.toml").write_text(toml)
    _trust(fixture_repo)  # `[ship]` is a guarded key (trust gate, audit 2026-10-16)
    _git(fixture_repo, "remote", "add", "origin", str(origin))
    _git(fixture_repo, "push", "-q", "origin", "main")
    return fixture_repo


@pytest.fixture
def waiting_run(fixture_repo: Path, origin: Path, gh: FakeGh) -> tuple[Path, str, str]:
    """A run both votes approved at D6, waiting for a person (rules dispatcher: "ask")."""
    repo = _ship_repo(fixture_repo, origin)
    code, output = _jev(repo, "run", "add search")
    assert code == 0, output
    with _store(repo) as store:
        run_id = store.list_runs()[0].id
    code, output = _jev(repo, "approve", run_id)
    assert code == 0 and "AWAITING_SHIP" in output, output
    assert "PR title: add search" in output and "D6 vote (rules): action=ship" in output
    return repo, run_id, _git(repo, "rev-parse", f"jev/{run_id}/result")


def _state(repo: Path, run_id: str) -> RunState:
    with _store(repo) as store:
        return store.get_run(run_id).state


def test_after_approval_defaults_and_overrides() -> None:
    def config(ship: dict[str, Any], primary: str = "jev") -> JevConfig:
        return JevConfig.model_validate({"dispatcher": {"primary": primary}, "ship": ship})

    assert after_approval(config({}), "merge") is None  # shipping off: D6 ends DONE
    # Unset after_approval is "ask" now that the consensus dispatcher is gone
    # (issue #193); its two votes used to make the unset default "merge".
    assert after_approval(config({"enabled": True}), None) == "ask"
    assert after_approval(config({"enabled": True}, "rules"), None) == "ask"
    assert after_approval(config({"enabled": True}, "jev"), None) == "ask"
    assert after_approval(config({"enabled": True, "after_approval": "ask"}), None) == "ask"
    assert after_approval(config({"enabled": True, "after_approval": "ask"}), "merge") == "merge"
    with pytest.raises(ValueError):
        config({"after_approval": "yes"})
    with pytest.raises(ValueError):
        config({"merge_method": "fast-forward"})


def test_merge_check_merges_only_the_voted_commit_with_green_checks() -> None:
    settings = ShipConfig(checks_grace_s=60, checks_timeout_s=600)
    sha = "a" * 40

    def check(waited: float = 0, **view: Any) -> tuple[str, str]:
        found = merge_check(json.loads(_open_view(sha)) | view, sha, waited, settings)
        return found.action, found.reason

    assert check() == ("merge", "")
    assert check(state="MERGED")[0] == "merged"
    assert check(state="CLOSED") == ("stop", "the PR was closed on GitHub")
    action, reason = check(headRefOid="b" * 40)
    assert action == "stop" and "head moved" in reason and "votes approved" in reason
    assert check(headRefOid=None)[0] == "stop"
    assert check(isDraft=True) == ("stop", "the PR is a draft")
    assert check(mergeable="CONFLICTING")[0] == "stop"
    failed = [*GREEN, {"context": "lint", "state": "FAILURE"}]
    assert check(statusCheckRollup=failed) == ("stop", "checks failed: lint")
    pending = [{"name": "test", "status": "IN_PROGRESS", "conclusion": None}]
    assert check(statusCheckRollup=pending)[0] == "wait"
    assert check(600, statusCheckRollup=pending)[0] == "stop"
    assert check(statusCheckRollup=[])[0] == "wait"  # checks may not have appeared yet
    action, reason = check(60, statusCheckRollup=[])
    assert action == "stop" and "require_checks = false" in reason
    no_ci = ShipConfig(require_checks=False, checks_grace_s=60)
    assert merge_check(json.loads(_open_view(sha, [])), sha, 60, no_ci).action == "merge"
    assert merge_check([], sha, 0, settings).action == "stop"


def test_changes_task_card_carries_the_notes() -> None:
    cards = [
        TaskCard(id="T1", title="a", description="a"),
        TaskCard(id="T3", title="b", description="b"),
    ]
    card = changes_task_card(cards, "  Rename foo to bar.\n")
    assert card.id == "T4" and card.depends_on == ["T1", "T3"]
    assert card.title == "Changes requested before shipping (round 1)"
    assert card.description.endswith("Rename foo to bar.")
    again = changes_task_card([*cards, card], "more")
    assert again.title.endswith("(round 2)")


def test_ask_waits_then_approve_ships_ready_pr_and_merges_it(
    waiting_run: tuple[Path, str, str], origin: Path, gh: FakeGh
) -> None:
    repo, run_id, sha = waiting_run
    assert gh.calls() == []  # nothing is pushed or opened before a person approves
    assert f"jev/{run_id}" not in _remote_branches(origin)
    code, output = _jev(repo, "resume", run_id)
    assert code == 1 and "waits for a person" in output

    gh.answer(MERGE_KEY, _open_view(sha))
    gh.answer(SYNC_KEY, json.dumps(PR_VIEW))
    code, output = _jev(repo, "ship", "approve", run_id)
    assert code == 0 and "DONE" in output and f"merged PR #42: {PR_URL}" in output, output

    assert _remote_branches(origin)[f"jev/{run_id}"] == sha
    create = next(argv for argv in gh.calls() if argv[:2] == ["pr", "create"])
    assert "--draft" not in create
    merges = [argv for argv in gh.calls() if argv[:2] == ["pr", "merge"]]
    assert merges == [
        ["pr", "merge", "42", "--repo", "acme/widgets", "--squash", "--match-head-commit", sha]
    ]
    assert not any(a in ("--admin", "--auto", "--force") for argv in gh.calls() for a in argv)
    with _store(repo) as store:
        [pr] = store.list_shipped_prs(run_id)
        assert pr.state == "merged" and not pr.draft and pr.head_oid == sha
        assert pr.reverted is None  # no revert search right after merging
        run = store.get_run(run_id)
        assert run.state is RunState.DONE and run.ship_mode is None


def test_auto_ship_stops_for_a_person_and_resume_finishes_without_reopening(
    fixture_repo: Path, origin: Path, gh: FakeGh
) -> None:
    repo = _ship_repo(fixture_repo, origin, SHIP_TOML + 'merge_method = "rebase"\n')
    code, output = _jev(repo, "run", "--auto-ship", "add search")
    assert code == 0, output
    with _store(repo) as store:
        run = store.list_runs()[0]
        assert run.ship_mode == "merge"
    gh.answer(MERGE_KEY, _open_view("f" * 40))  # someone pushed to the PR branch
    code, output = _jev(repo, "approve", run.id)
    assert code == 3 and "NEEDS_HUMAN" in output and "head moved" in output, output
    with _store(repo) as store:
        assert store.get_run(run.id).resume_state is RunState.SHIPPING
    assert "pr merge" not in gh.commands()

    sha = _git(repo, "rev-parse", f"jev/{run.id}/result")
    gh.answer(MERGE_KEY, _open_view(sha))
    gh.answer(SYNC_KEY, json.dumps(PR_VIEW))
    code, output = _jev(repo, "resume", run.id)
    assert code == 0 and "merged PR #42" in output, output
    assert gh.commands().count("pr create") == 1  # the PR from the first try is reused
    merge = next(argv for argv in gh.calls() if argv[:2] == ["pr", "merge"])
    assert "--rebase" in merge


def test_failed_checks_and_refused_merge_stop_for_a_person(
    waiting_run: tuple[Path, str, str], gh: FakeGh
) -> None:
    repo, run_id, sha = waiting_run
    red = [{"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"}]
    gh.answer(MERGE_KEY, _open_view(sha, red))
    code, output = _jev(repo, "ship", "approve", run_id)
    assert code == 3 and "checks failed: test" in output, output
    assert "pr merge" not in gh.commands()

    gh.answer(MERGE_KEY, _open_view(sha))
    gh.answer("pr merge", stderr="the base branch policy prohibits the merge", code=1)
    gh.answer(SYNC_KEY, json.dumps(PR_VIEW | {"state": "OPEN", "mergedAt": None}))
    code, output = _jev(repo, "resume", run_id)
    assert code == 3 and "gh pr merge failed" in output and "policy" in output, output
    assert _state(repo, run_id) is RunState.NEEDS_HUMAN


def test_no_checks_stop_unless_require_checks_is_off(
    waiting_run: tuple[Path, str, str], gh: FakeGh
) -> None:
    repo, run_id, sha = waiting_run
    _ship_config(repo, "checks_grace_s = 0\n")
    gh.answer(MERGE_KEY, _open_view(sha, []))
    code, output = _jev(repo, "ship", "approve", run_id)
    assert code == 3 and "no checks reported" in output, output

    _ship_config(repo, "checks_grace_s = 0\nrequire_checks = false\n")
    gh.answer(SYNC_KEY, json.dumps(PR_VIEW))
    code, output = _jev(repo, "resume", run_id)
    assert code == 0 and "merged PR #42" in output, output


def test_pending_checks_are_polled_until_green(
    waiting_run: tuple[Path, str, str], gh: FakeGh
) -> None:
    repo, run_id, sha = waiting_run
    _ship_config(repo, "checks_poll_s = 1\n")
    pending = [{"name": "test", "status": "QUEUED", "conclusion": None}]
    gh.answers(
        MERGE_KEY,
        {"stdout": _open_view(sha, pending)},
        {"stdout": _open_view(sha)},
    )
    gh.answer(SYNC_KEY, json.dumps(PR_VIEW))
    code, output = _jev(repo, "ship", "approve", run_id)
    assert code == 0 and "merged PR #42" in output, output
    views = [argv for argv in gh.calls() if " ".join(argv).startswith(MERGE_KEY)]
    assert len(views) == 2


def test_pending_checks_forever_hit_the_timeout_and_stop(
    waiting_run: tuple[Path, str, str], gh: FakeGh
) -> None:
    repo, run_id, sha = waiting_run
    _ship_config(repo, "checks_poll_s = 1\nchecks_timeout_s = 1\n")
    pending = [{"name": "test", "status": "IN_PROGRESS", "conclusion": None}]
    gh.answer(MERGE_KEY, _open_view(sha, pending))
    code, output = _jev(repo, "ship", "approve", run_id)
    assert code == 3 and "checks did not finish within 1 s" in output, output
    assert "pr merge" not in gh.commands()  # nothing is merged on a timeout
    assert _state(repo, run_id) is RunState.NEEDS_HUMAN


def test_merge_timeout_with_github_merged_counts_as_merged(
    waiting_run: tuple[Path, str, str], gh: FakeGh
) -> None:
    import asyncio

    from jev_agent.config.loader import load_config
    from jev_agent.core.ship import ship_and_merge

    repo, run_id, sha = waiting_run
    with _store(repo) as store:
        store.set_run_state(run_id, RunState.SHIPPING)
    gh.answer(MERGE_KEY, _open_view(sha))
    gh.responses["pr merge"] = {"code": 0, "stdout": "", "sleep": 5}
    gh.answer("auth status")  # rewrite responses.json with the sleep
    gh.answer(SYNC_KEY, json.dumps(PR_VIEW))

    async def no_sleep(_: float) -> None:
        return None

    with _store(repo) as store:
        pr = asyncio.run(
            ship_and_merge(
                repo, load_config(repo), store, run_id, gh=Gh(repo, timeout_s=0.5), sleep=no_sleep
            )
        )
    assert (pr.number, pr.state, pr.head_oid) == (42, "merged", sha)
    merges = [argv for argv in gh.calls() if argv[:2] == ["pr", "merge"]]
    assert len(merges) == 1 and "--match-head-commit" in merges[0]
    with _store(repo) as store:
        [record] = store.list_shipped_prs(run_id)
        assert record.state == "merged"


def test_modify_adds_a_task_with_the_notes_and_votes_again(
    waiting_run: tuple[Path, str, str], gh: FakeGh
) -> None:
    repo, run_id, _ = waiting_run
    code, output = _jev(repo, "ship", "modify", run_id)
    assert code == 1 and "--notes" in output
    code, output = _jev(repo, "ship", "modify", run_id, "--notes", "Rename foo to bar.")
    assert code == 0 and "AWAITING_SHIP" in output, output
    with _store(repo) as store:
        tasks = store.list_tasks(run_id)
        assert tasks[-1].card.title == "Changes requested before shipping (round 1)"
        assert "Rename foo to bar." in tasks[-1].card.description
        assert tasks[-1].state.value == "DONE"
        votes = [d for d in store.list_decisions(run_id) if d.request.point == "D6"]
        assert len(votes) == 2  # the changed work was voted on again
    assert gh.calls() == []

    code, output = _jev(repo, "ship", "stop", run_id)
    assert code == 0 and "not shipped" in output
    code, output = _jev(repo, "ship", "stop", run_id)
    assert code == 1 and "cannot stop" in output


def test_ship_flags_need_shipping_on_and_exclude_each_other(fixture_repo: Path, gh: FakeGh) -> None:
    code, output = _jev(fixture_repo, "run", "--auto-ship", "idea")
    assert code == 1 and "needs shipping on" in output
    _ship_config(fixture_repo)
    code, output = _jev(fixture_repo, "run", "--auto-ship", "--ask-before-ship", "idea")
    assert code == 1 and "cannot be used together" in output
    code, output = _jev(fixture_repo, "queue", "add", "--ask-before-ship", "queued idea")
    assert code == 0, output
    with _store(fixture_repo) as store:
        [item] = store.list_queue()
        assert item.ship_mode == "ask"
    code, output = _jev(fixture_repo, "queue", "list")
    assert "ask-before-ship" in output
