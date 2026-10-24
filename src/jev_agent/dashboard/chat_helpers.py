"""Chat view builders: context bar, approval rail, step timeline, starters."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from markupsafe import Markup, escape

from jev_agent.app.format import format_answers
from jev_agent.core.chat import BACKENDS, BackendName, ChatError, ChatLearning, ChatMeta
from jev_agent.core.models import RunState, TaskState
from jev_agent.core.store import (
    DecisionRecord,
    NotFoundError,
    ShippedPrRecord,
    StoreError,
    TaskRecord,
)
from jev_agent.core.workspace import result_branch, start_ref
from jev_agent.dashboard import views
from jev_agent.dashboard.helpers import _format_diff_stat, _git_branch, _safe_diff_stat
from jev_agent.dashboard.nav import _usd


def _chat_backend_name(value: str | None) -> BackendName:
    """A form's worker value, or a `ChatError` (the form lists both backends)."""
    if value in BACKENDS:
        return value
    raise ChatError("pick a chat backend: api or claude_code")


def _chat_learning(value: str | None) -> ChatLearning:
    """The New chat form's learning choice: empty is the repo default, "none" is off."""
    if not value:
        return ChatLearning()
    if value == "none":
        return ChatLearning(off=True)
    return ChatLearning(profile=value)


_STEP_BLOCK_RUNS = 10  # newest runs considered, so one chat page stays cheap


_STEP_BLOCK_ENTRIES = 20


_CHAT_SUGGESTIONS: tuple[str, ...] = (
    "Add pagination to the users API",
    "Fix the flaky date-parsing test",
    "Explain what run 20261014-3f1a changed",
    "Review the open pull requests",
)


def _chat_greeting(repo_name: str) -> str:
    """Warm empty-state greeting, aware of the time of day and the repo."""
    hour = datetime.now().hour
    part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    return f"Good {part} — what should we build in {repo_name} today?"


def _chat_starters(repo: views.Repo) -> list[str]:
    """Repo-aware empty-state starters: the static prompts plus live context
    (queued work, the latest run) so the empty state feels alive, not blank."""
    starters = list(_CHAT_SUGGESTIONS)
    try:
        queued = repo.store.list_queue()
    except (sqlite3.Error, StoreError):
        queued = []
    if queued:
        n = len(queued)
        plural = "s" if n != 1 else ""
        starters.insert(0, f"Review the {n} queued item{plural} waiting in the queue")
    try:
        runs = repo.store.list_runs()
    except (sqlite3.Error, StoreError):
        runs = []
    if runs:
        last = runs[0]
        state = str(getattr(last.state, "name", last.state)).replace("_", " ").lower()
        starters.append(f"Check on run {last.id} ({state})")
    return starters[:6]


_RAIL_STATUS_LABELS = {
    "running": "Running",
    "needs-you": "Needs you",
    "queued": "Queued",
    "briefed": "Briefed",
}


def _rail_chat_status(runner: Any, meta: ChatMeta, *, needs_you: bool) -> str:
    if runner.busy(meta.chat_id):
        return "running"
    if needs_you:
        return "needs-you"
    if meta.queue_items:
        return "queued"
    if meta.brief:
        return "briefed"
    return ""


def _rail_groups(
    runner: Any, repo: views.Repo, chat_id: str, approval_cards: list[dict[str, Any]]
) -> list[tuple[str, list[tuple[ChatMeta, str, str, str]]]]:
    """Rail chats grouped by recency (Today / Yesterday / This week / Older),
    each with a status key + label. Newest chat first inside every bucket."""
    buckets: dict[str, list[tuple[ChatMeta, str, str, str]]] = {
        "Today": [],
        "Yesterday": [],
        "This week": [],
        "Older": [],
    }
    today = datetime.now(UTC).date()
    for meta, title in views.chats(repo, "")[:30]:
        try:
            updated = datetime.fromisoformat(meta.updated_at)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            age_days = (today - updated.date()).days
        except ValueError:
            age_days = 10**6
        key = (
            "Today"
            if age_days <= 0
            else "Yesterday"
            if age_days == 1
            else "This week"
            if age_days < 7
            else "Older"
        )
        status = _rail_chat_status(
            runner, meta, needs_you=bool(approval_cards) and meta.chat_id == chat_id
        )
        buckets[key].append((meta, title, status, _RAIL_STATUS_LABELS.get(status, "")))
    return [(key, items) for key, items in buckets.items() if items]


def _chat_context_bar(repo: views.Repo, meta: ChatMeta | None) -> dict[str, str]:
    """The quiet persistent context strip: repo · branch · backend · model."""
    backend = meta.backend if meta is not None else ""
    return {
        "repo": repo.root.name,
        "branch": _git_branch(repo.root),
        "backend": {"api": "OpenRouter API"}.get(backend, "Claude Code") if backend else "",
        "model": meta.model if meta is not None and meta.model else "default model",
        "profile": meta.profile if meta is not None and meta.profile else "",
    }


API_MODEL_OPTIONS: tuple[str, ...] = (
    "anthropic/claude-sonnet-4",
    "anthropic/claude-3.5-sonnet",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "google/gemini-2.0-flash-001",
    "deepseek/deepseek-chat",
    "meta-llama/llama-3.1-405b-instruct",
)


def _approval_cards(repo: views.Repo, meta: ChatMeta) -> list[dict[str, Any]]:
    """The chat's runs waiting on a person (plan or ship approval), for the approval rail."""
    cards: list[dict[str, Any]] = []
    for ref in meta.runs:
        run_id = ref.get("run_id")
        if not run_id:
            continue
        try:
            run = repo.store.get_run(run_id)
        except NotFoundError:
            continue
        if run.state not in (RunState.AWAITING_APPROVAL, RunState.AWAITING_SHIP):
            continue
        cards.append(
            {
                "run_id": run_id,
                "kind": ref.get("kind", ""),
                "plan_summary": run.plan.summary[:300] if run.plan else "",
                "cost_estimate": f"${run.spend_usd:.2f}" if run.spend_usd > 0 else "unknown",
                "diff_stat": _format_diff_stat(
                    _safe_diff_stat(repo.root, start_ref(run_id), result_branch(run_id))
                ),
            }
        )
    return cards


def _decision_verdict(record: DecisionRecord) -> str:
    """The decision's outcome in a few words (the step timeline's title tail)."""
    answers = record.decision.answers
    point = record.decision.point
    if point == "D3":
        return answers.get("verdict", "")
    if point in ("D5", "D6"):
        return answers.get("action", "")
    return ", ".join(f"{name}={value}" for name, value in answers.items())[:120]


def _decision_block(record: DecisionRecord) -> dict[str, Any]:
    """One D1-D8 decision card as a collapsed step entry."""
    decision = record.decision
    worker = decision.answers.get("worker", decision.dispatcher)
    who = f"{worker}/{decision.model}" if decision.model else worker
    scope = record.task_id or "run"
    verdict = _decision_verdict(record)
    lines = [format_answers(decision.answers, decision.details)]
    if record.fallback_reason:
        lines.append(f"rules fallback: {record.fallback_reason}")
    if decision.cost_usd is not None:
        lines.append(f"cost {_usd(decision.cost_usd)}")
    return {
        "title": f"{scope} · {who}" + (f" — {verdict}" if verdict else ""),
        "summary": f"{decision.point} decided by {decision.dispatcher}",
        "detail_html": Markup("<br>").join(escape(line) for line in lines),
    }


def _task_block(task: TaskRecord) -> dict[str, Any]:
    """A finished task as a step entry (a run milestone)."""
    return {
        "title": f"{task.id} · {task.card.title} — done",
        "summary": f"{task.attempts} attempt(s), {_usd(task.spend_usd)}",
        "detail_html": Markup(escape(task.note)) if task.note else Markup(""),
    }


def _shipped_block(pr: ShippedPrRecord) -> dict[str, Any]:
    """A shipped PR as a step entry (a run milestone)."""
    return {
        "title": f"shipped: {pr.title or pr.url or pr.branch}",
        "summary": f"{pr.repo} · {pr.base} ← {pr.branch}",
        "detail_html": Markup(""),
    }


def _step_blocks(repo: views.Repo, meta: ChatMeta) -> list[dict[str, Any]]:
    """Collapsed step entries for the chat's runs: the D1-D8 decision cards plus run
    milestones (finished tasks, shipped PRs). Newest last, capped at 20 entries."""
    dated: list[tuple[datetime, dict[str, Any]]] = []
    for ref in meta.runs[-_STEP_BLOCK_RUNS:]:
        run_id = ref.get("run_id")
        if not run_id:
            continue
        try:
            run = repo.store.get_run(run_id)
        except NotFoundError:
            continue
        for record in repo.store.list_decisions(run_id):
            dated.append((record.created_at, _decision_block(record)))
        for task in repo.store.list_tasks(run_id):
            if task.state is TaskState.DONE:
                dated.append((run.updated_at, _task_block(task)))
        for pr in repo.store.list_shipped_prs(run_id):
            dated.append((pr.created_at, _shipped_block(pr)))
    dated.sort(key=lambda item: item[0])
    return [block for _, block in dated[-_STEP_BLOCK_ENTRIES:]]
