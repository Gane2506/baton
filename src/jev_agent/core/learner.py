"""The learning step at the end of a run (docs/learning.md): score lessons, ask D7, run the
learner role, store its changes.

1. **Scoring** (deterministic, always, no model call). For each task that ended (DONE, FAILED,
   NEEDS_HUMAN) and was not scored yet, every lesson injected into the task's attempts (coder,
   reviewer) gets `uses += 1`, and `score += 1` if the task was DONE on its first attempt,
   else `-0.5`. A lesson whose score reaches -2 is retired. Lessons given only to the planner
   or validator are not scored (there is no per-task outcome to credit).
2. **D7**: learn or skip, and the learner's worker, model and effort. Skipped without asking
   when the learner role has no worker or the run's budget is spent.
3. **Learner**: a read-only role that sees a compact run card (never transcripts) and the
   profile's relevant lessons with their ids, and answers with lessons to add, update and
   retire. Its changes are limited and redacted here before they are stored.

Nothing here changes the run's state: every problem becomes a "skipped: ..." outcome, which the
pipeline stores on the run (`jev status`, `jev log`).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from jev_agent.core.harness import Halt, Harness
from jev_agent.core.integration import FIX_TITLE
from jev_agent.core.learning import (
    MAX_LESSON_CHARS,
    LearningError,
    Lesson,
    LessonDB,
    context_terms,
    normalize_tag,
    select_lessons,
)
from jev_agent.core.models import LESSON_ROLES, RunState, TaskState
from jev_agent.core.store import RunRecord, Store
from jev_agent.dispatch.requests import clip, d7_learn
from jev_agent.roles import LearnerOutput
from jev_agent.runlog import redact

LEARN_STATES = frozenset(
    {RunState.DONE, RunState.FAILED, RunState.NEEDS_HUMAN, RunState.BUDGET_EXCEEDED}
)
MAX_ADDS = 5
MAX_UPDATES = 10
MAX_TAGS = 8
MAX_TAG_CHARS = 60
# Existing lessons shown to the learner, so it can refine, merge and retire them.
SHOWN_LESSONS = 30
SHOWN_CHARS = 6_000

SCORE_FIRST_TRY = 1.0
SCORE_STRUGGLED = -0.5
_ENDED = frozenset({TaskState.DONE, TaskState.FAILED, TaskState.NEEDS_HUMAN})


@dataclass(frozen=True)
class Applied:
    added: list[str]
    updated: list[str]
    retired: list[str]

    def describe(self) -> str:
        return f"{len(self.added)} added, {len(self.updated)} updated, {len(self.retired)} retired"


async def learn(h: Harness, run: RunRecord) -> str:
    """Score, decide (D7) and distill for a run that ended. Returns the outcome line."""
    profile = run.learning_profile
    assert profile is not None
    if h.learning is None:
        return "skipped: learning is not available"
    try:
        db = h.learning.db_for(profile)
    except LearningError:
        return f"skipped: profile {profile!r} no longer exists"
    score_lessons(h.store, run, db)
    try:
        return await _distill(h, run, db, profile)
    except Halt as halt:
        return f"skipped: {halt.reason}"


async def _distill(h: Harness, run: RunRecord, db: LessonDB, profile: str) -> str:
    options = h.options("learner")
    if not options.workers:
        return "skipped: no available worker for role learner (`jev doctor`)"
    cap = h.budget
    if cap.run_exhausted(h.store.get_run(run.id).spend_usd):
        return f"skipped: the run's ${cap.per_run_usd:.2f} budget is spent"
    card = run_card(h.store, run)
    active = db.list_lessons(profile)
    req = d7_learn(
        str(run.state),
        card["signals"],
        len(card["tasks"]),
        len(active),
        options,
        h.store.get_run(run.id).spend_usd,
        cap.per_run_usd,
    )
    decision = await h.decide(run.id, None, req)
    if decision.answers["learn"] == "skip":
        return "skipped: D7 found nothing worth learning"
    spent = h.store.get_run(run.id).spend_usd
    if cap.run_exhausted(spent):
        return f"skipped: the run's ${cap.per_run_usd:.2f} budget is spent"

    tasks = h.store.list_tasks(run.id)
    terms = context_terms(
        [run.idea.text, *(t.card.title for t in tasks)],
        [path for t in tasks for path in t.card.files_hint],
    )
    shown = select_lessons(active, None, terms, SHOWN_LESSONS, SHOWN_CHARS)
    shown.sort(key=lambda lesson: lesson.number)  # oldest first reads naturally
    prompt = h.prompt(
        "learner",
        shown,
        run_card=json.dumps(card, indent=2, ensure_ascii=False),
        profile=profile,
        max_adds=MAX_ADDS,
    )
    output = await h.run_worker_with_fallback(
        run_id=run.id,
        task_id=None,
        role="learner",
        assignment=h.assignment("learner", decision.answers),
        prompt=prompt,
        workdir=h.repo,  # read-only: the learner never edits the repo
        lesson_ids=[lesson.id for lesson in shown],
    )
    parsed = output.parsed
    if not isinstance(parsed, LearnerOutput):
        result = output.result
        return f"skipped: the learner gave no valid output ({result.status}: {result.summary})"
    applied = apply_learner_output(db, profile, parsed, shown, run.id)
    return f"learned into {profile}: {applied.describe()}"


# ---- scoring -----------------------------------------------------------------------------


def score_lessons(store: Store, run: RunRecord, db: LessonDB) -> None:
    """Update uses and scores of the lessons this run injected (see the module docstring)."""
    attempts = store.list_attempts(run.id)
    for task in store.list_tasks(run.id):
        if task.lessons_scored or task.state not in _ENDED:
            continue
        ids = {i for a in attempts if a.task_id == task.id for i in a.lesson_ids}
        first_try = task.state is TaskState.DONE and task.attempts <= 1
        delta = SCORE_FIRST_TRY if first_try else SCORE_STRUGGLED
        for number in _numbers_in(db, ids):
            db.record_use(number, delta)
        store.mark_task_lessons_scored(run.id, task.id)


def _numbers_in(db: LessonDB, ids: set[str]) -> list[int]:
    """Row numbers of the ids that belong to `db` (an `L` id for the repo database)."""
    prefix = db.lesson_id(0)[0]
    return sorted(int(i[1:]) for i in ids if i[:1] == prefix and i[1:].isdigit())


# ---- the run card ------------------------------------------------------------------------


def learning_signals(store: Store, run: RunRecord) -> list[str]:
    """What needed extra work in the run, as stable words (input to D7 and the learner)."""
    found: list[str] = []
    decisions = store.list_decisions(run.id)
    if any(
        d.decision.answers.get("verdict") == "revise" for d in decisions if d.request.point == "D3"
    ):
        found.append("revision")
    d5 = Counter(d.decision.answers.get("action") for d in decisions if d.request.point == "D5")
    found += [action for action in ("retry", "escalate", "reassign", "human") if d5[action]]
    if any(t.card.title.startswith(FIX_TITLE) for t in store.list_tasks(run.id)):
        found.append("fix_task")
    state_signal = {
        RunState.FAILED: "failed",
        RunState.NEEDS_HUMAN: "needs_human",
        RunState.BUDGET_EXCEEDED: "budget_exceeded",
    }.get(run.state)
    if state_signal:
        found.append(state_signal)
    return found


def run_card(store: Store, run: RunRecord) -> dict[str, Any]:
    """A compact, redacted summary of the run for D7 and the learner. Never transcripts."""
    decisions = store.list_decisions(run.id)
    plan_issues: list[str] = []
    for d in decisions:
        if d.request.point == "D3" and d.decision.answers.get("verdict") == "revise":
            plan_issues += d.request.state.get("validation", {}).get("issues", [])
    tasks = []
    for task in store.list_tasks(run.id):
        actions = [
            d.decision.answers.get("action", "")
            for d in decisions
            if d.request.point == "D5" and d.task_id == task.id
        ]
        entry: dict[str, Any] = {
            "id": task.id,
            "title": clip(task.card.title, 120),
            "files_hint": task.card.files_hint[:10],
            "test_command": task.card.test_command,
            "state": str(task.state),
            "attempts": task.attempts,
            "d5_actions": actions,
        }
        struggled = task.state is not TaskState.DONE or task.attempts > 1
        if struggled and task.note:
            entry["failure"] = clip(redact(task.note), 400)
        tasks.append(entry)
    return {
        "idea": clip(redact(run.idea.text), 500),
        "outcome": str(run.state),
        "reason": None if run.reason is None else clip(redact(run.reason), 300),
        "plan_summary": None if run.plan is None else clip(redact(run.plan.summary), 400),
        "plan_rounds": run.plan_round,
        "plan_issues": [clip(redact(issue), 200) for issue in plan_issues[:8]],
        "signals": learning_signals(store, run),
        "tasks": tasks,
    }


# ---- applying the learner's answer -------------------------------------------------------


def clean_text(text: str) -> str:
    """One line of redacted text, at most `MAX_LESSON_CHARS` characters."""
    return clip(" ".join(redact(text).split()), MAX_LESSON_CHARS)


def clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for tag in tags:
        norm = normalize_tag(redact(tag))[:MAX_TAG_CHARS]
        if norm and norm not in out:
            out.append(norm)
    return out[:MAX_TAGS]


def apply_learner_output(
    db: LessonDB,
    profile: str,
    output: LearnerOutput,
    shown: list[Lesson],
    run_id: str | None = None,
) -> Applied:
    """Store the learner's changes, within limits.

    At most `MAX_ADDS` adds and `MAX_UPDATES` updates. Text is redacted, put on one line and
    clipped to 300 characters; unknown roles are dropped (no roles left: every role); tags are
    normalized. Updates and retirements only apply to lessons that were shown to the learner,
    so it cannot touch other profiles. Adds that repeat an existing lesson are ignored.
    """
    by_id = {lesson.id.upper(): lesson for lesson in shown}
    existing = {" ".join(lesson.text.lower().split()) for lesson in db.list_lessons(profile)}
    added: list[str] = []
    for draft in output.add[:MAX_ADDS]:
        text = clean_text(draft.text)
        key = text.lower()
        if not text or key in existing:
            continue
        roles = [r for r in LESSON_ROLES if r in {x.strip().lower() for x in draft.roles}]
        lesson = db.add_lesson(
            profile, text, roles or list(LESSON_ROLES), clean_tags(draft.tags), run_id
        )
        existing.add(key)
        added.append(lesson.id)
    updated: list[str] = []
    for edit in output.update[:MAX_UPDATES]:
        target = by_id.get(edit.id.strip().upper())
        text = clean_text(edit.text)
        if target is None or not text or target.id in updated:
            continue
        db.update_text(target.number, text)
        updated.append(target.id)
    retired: list[str] = []
    for lesson_id in output.retire:
        target = by_id.get(lesson_id.strip().upper())
        if target is None or target.id in retired:
            continue
        db.retire(target.number)
        retired.append(target.id)
    return Applied(added, updated, retired)
