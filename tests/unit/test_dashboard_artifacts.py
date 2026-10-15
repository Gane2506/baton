"""The global Artifacts page: aggregation, filters, and deep links.

Artifacts are the runs' work products (plans, diffs, quality reports) — not
Claude-style documents. The page aggregates them across all chats/runs and
deep-links to the existing viewers; it never duplicates a viewer.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from jev_agent.core.models import Idea, Plan, RunState, TaskCard
from jev_agent.core.store import Store
from jev_agent.dashboard import app as dashboard_app_module
from jev_agent.dashboard import views
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings
from jev_agent.quality.models import GateResult


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


def _store(repo: Path) -> Store:
    return Store.open(repo / ".jev" / "state.db")


def _plan() -> Plan:
    return Plan(
        summary="add the thing",
        tasks=[
            TaskCard(
                id="T1",
                title="do the thing",
                description="d",
                files_hint=["src/"],
                acceptance_criteria=["works"],
                test_command="pytest",
            )
        ],
    )


def test_artifacts_aggregates_plans_diffs_and_reports(fixture_repo: Path) -> None:
    with _store(fixture_repo) as store:
        run = store.create_run(Idea(text="add the thing", repo_path=fixture_repo))
        store.set_run_plan(run.id, _plan(), 1)
        store.add_quality_report(run.id, GateResult(level="basic", scope="integration"))
        store.set_run_state(run.id, RunState.DONE)
        run_id = run.id
    with dashboard_app_module.views.open_repo(fixture_repo) as repo:
        rows, _ = views.artifacts(repo)
    kinds = sorted(a.kind for a in rows)
    assert kinds == ["diff", "plan", "tests"]
    by_kind = {a.kind: a for a in rows}
    assert by_kind["plan"].run_id == run_id
    assert by_kind["plan"].url == f"/runs/{run_id}"
    assert by_kind["diff"].url == f"/runs/{run_id}"
    assert by_kind["tests"].url == f"/runs/{run_id}#quality-review"
    assert "add the thing" in by_kind["plan"].title


def test_artifacts_filters(fixture_repo: Path) -> None:
    with _store(fixture_repo) as store:
        run = store.create_run(Idea(text="add the thing", repo_path=fixture_repo))
        store.set_run_plan(run.id, _plan(), 1)
        store.set_run_state(run.id, RunState.DONE)
    with dashboard_app_module.views.open_repo(fixture_repo) as repo:
        plans, _ = views.artifacts(repo, kind="plan")
        diffs, _ = views.artifacts(repo, kind="diff")
        tests, _ = views.artifacts(repo, kind="tests")
        miss, _ = views.artifacts(repo, q="no such idea here")
    assert len(plans) == 1 and plans[0].kind == "plan"
    assert len(diffs) == 1 and diffs[0].kind == "diff"
    assert tests == []
    assert miss == []


def test_artifacts_page_renders_with_empty_state(client: TestClient) -> None:
    text = client.get("/artifacts").text
    assert "Artifacts" in text
    assert "Artifacts appear here as runs produce plans" in text


def test_artifacts_page_lists_rows_and_nav(client: TestClient, fixture_repo: Path) -> None:
    with _store(fixture_repo) as store:
        run = store.create_run(Idea(text="add the thing", repo_path=fixture_repo))
        store.set_run_plan(run.id, _plan(), 1)
        store.set_run_state(run.id, RunState.DONE)
    text = client.get("/artifacts").text
    assert "Plan: add the thing" in text
    assert "Diff: add the thing" in text
    assert f"/runs/{run.id}" in text
    # the nav item is present and marked active on this page
    assert 'href="/artifacts"' in text
    assert "Artifacts" in text


def test_artifacts_page_search_filters_rows(client: TestClient, fixture_repo: Path) -> None:
    with _store(fixture_repo) as store:
        run = store.create_run(Idea(text="add the thing", repo_path=fixture_repo))
        store.set_run_plan(run.id, _plan(), 1)
    assert "Plan: add the thing" in client.get("/artifacts").text
    assert "Plan: add the thing" not in client.get("/artifacts?q=zzzznothing").text
