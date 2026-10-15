"""`jev bisect`: first-bad-event scan + causal-chain walk (docs/events.md).

What "bad" means, operationally (flags are combinable; first match wins):

- `--gate`: the first `gate.checked` with `blocked=true` (quality gate).
- `--test`: the first `attempt.tested` with `tests="failed"`, or the first
  `attempt.finished` with a non-success status (worker failure).
- `--task TASK`: the first `task.state_changed → FAILED` for TASK (any task
  when TASK is omitted).
- `--mark SEQ`: human-marked — the named event is the bad one.

This is deliberately NOT a binary search: the predicate usually cannot be
re-run (it needed the original LLM outputs), so bisect scans the log once
(O(n), n ≈ hundreds) and walks causal parents via `parent_run_seq` to print
the blame path. When an event has no causal parent, the walk falls back to the
same task's previous event, which reconstructs the task's story in order.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime

from jev_agent.core.events import Event, EventType
from jev_agent.core.store import Store

_BAD_STATUSES = ("failure", "error", "timeout", "quota_exhausted")


@dataclass
class BisectResult:
    """The bad event and the blame path leading to it (oldest first)."""

    bad: Event
    chain: list[Event] = field(default_factory=list)
    criterion: str = ""


def find_first_bad(
    events: list[Event],
    *,
    gate: bool = False,
    test: bool = False,
    task: str | None = None,
    mark: int | None = None,
    task_any: bool = False,
    good_seq: int = 0,
) -> tuple[Event | None, str]:
    """Scan the log for the first event matching any given criterion.

    Returns (event, criterion-description). `--mark` names the bad event
    directly. `--good SEQ` restricts the scan to events after SEQ.
    """
    by_seq = {e.run_seq: e for e in events}
    if mark is not None:
        event = by_seq.get(mark)
        return (event, f"--mark {mark}") if event is not None else (None, f"--mark {mark}")
    for event in events:
        if event.run_seq <= good_seq:
            continue
        hit = _matches(event, gate=gate, test=test, task=task, task_any=task_any)
        if hit is not None:
            return event, hit
    return None, ""


def _matches(
    event: Event, *, gate: bool, test: bool, task: str | None, task_any: bool
) -> str | None:
    p = event.payload
    if gate and event.type == EventType.GATE_CHECKED and p.get("blocked"):
        scope = str(p.get("scope", ""))
        task_part = f" {p.get('task_id')}" if p.get("task_id") else ""
        return f"--gate: first blocked gate ({scope}{task_part})"
    if test:
        if event.type == EventType.ATTEMPT_TESTED and p.get("tests") == "failed":
            return "--test: first failed tests"
        if event.type == EventType.ATTEMPT_FINISHED and str(p.get("status")) in _BAD_STATUSES:
            return f"--test: first unsuccessful attempt ({p.get('status')})"
    if (
        (task is not None or task_any)
        and event.type == EventType.TASK_STATE_CHANGED
        and p.get("to_state") == "FAILED"
        and (task is None or p.get("task_id") == task)
    ):
        return f"--task: first FAILED transition of {p.get('task_id')}"
    return None


def blame_chain(store: Store, run_id: str, bad: Event) -> list[Event]:
    """Walk from the bad event to its causes, oldest first.

    Follows `parent_run_seq` where the pipeline recorded one; otherwise falls
    back to the same task's previous event, which keeps the task's story in
    order even for synthetic (backfilled) logs that have no causal links.
    """
    events = store.list_events(run_id)
    by_seq = {e.run_seq: e for e in events}
    chain = [bad]
    seen = {bad.run_seq}
    current = bad
    while len(chain) < 1000:
        parent: Event | None = None
        if current.parent_run_seq is not None:
            parent = by_seq.get(current.parent_run_seq)
        if parent is None:
            task_id = current.payload.get("task_id")
            if task_id is not None:
                current_seq = current.run_seq
                older = [
                    e
                    for e in events
                    if e.run_seq < current_seq
                    and (
                        e.payload.get("task_id") == task_id
                        or (
                            isinstance(e.payload.get("task_ids"), list)
                            and task_id in e.payload["task_ids"]
                        )
                    )
                ]
                parent = max(older, key=lambda e: e.run_seq) if older else None
        if parent is None or parent.run_seq in seen:
            break
        seen.add(parent.run_seq)
        chain.append(parent)
        current = parent
    chain.reverse()
    return chain


def bisect_run(
    store: Store,
    run_id: str,
    *,
    gate: bool = False,
    test: bool = False,
    task: str | None = None,
    task_any: bool = False,
    mark: int | None = None,
    good_seq: int = 0,
) -> BisectResult | None:
    """Scan for the first bad event and walk its blame path. None when no bad
    event matches (the run looks fine by the given criteria)."""
    events = store.list_events(run_id)
    bad, criterion = find_first_bad(
        events,
        gate=gate,
        test=test,
        task=task,
        task_any=task_any,
        mark=mark,
        good_seq=good_seq,
    )
    if bad is None:
        return None
    return BisectResult(bad=bad, chain=blame_chain(store, run_id, bad), criterion=criterion)


# ---- HTML timeline export ------------------------------------------------------------

_ACTOR_COLORS = {"pipeline": "#2563eb", "human": "#16a34a", "scheduler": "#9333ae"}


def _summary(event: Event) -> str:
    """Plain-text one-liner for an event (no markup)."""
    p = event.payload
    match event.type:
        case EventType.RUN_CREATED:
            return f"run created: {p.get('idea_text', '')[:80]}"
        case EventType.RUN_STATE_CHANGED:
            return f"{p.get('from_state')} → {p.get('to_state')}"
        case EventType.RUN_RESUMED:
            return f"resumed from {p.get('from_state')} via {p.get('via')}"
        case EventType.PLAN_SET:
            return f"plan round {p.get('round')}"
        case EventType.DESIGN_SET:
            return "design doc set"
        case EventType.VALIDATION_RECORDED:
            return f"validation: {p.get('verdict')}"
        case EventType.TASKS_UPSERTED:
            return f"tasks: {', '.join(p.get('task_ids', []))}"
        case EventType.TASK_STATE_CHANGED:
            return f"{p.get('task_id')}: {p.get('from_state')} → {p.get('to_state')}"
        case EventType.TASK_ASSIGNED:
            return f"{p.get('task_id')} → {p.get('worker')}"
        case EventType.ATTEMPT_STARTED:
            return f"attempt {p.get('attempt_no')} ({p.get('role')}) started"
        case EventType.ATTEMPT_TESTED:
            return f"attempt {p.get('attempt_id')} tests: {p.get('tests')}"
        case EventType.ATTEMPT_FINISHED:
            return f"attempt {p.get('attempt_id')}: {p.get('status')}"
        case EventType.DECISION_REQUESTED:
            return f"{p.get('point')} requested: {', '.join(sorted(p.get('questions', {})))}"
        case EventType.DECISION_RECORDED:
            answers = ", ".join(f"{k}={v}" for k, v in p.get("answers", {}).items())
            return f"{p.get('point')} → {answers} ({p.get('dispatcher')})"
        case EventType.GATE_CHECKED:
            verdict = "BLOCKED" if p.get("blocked") else "passed"
            return f"gate {p.get('scope')}: {verdict}"
        case EventType.SHIP_RECORDED:
            return f"PR #{p.get('number')}: {p.get('state')}"
        case EventType.SPEND_ADDED:
            return f"+${p.get('usd', 0):.2f} (total ${p.get('running_total', 0):.2f})"
        case EventType.LEASE_CLAIMED:
            return f"lease claimed ({p.get('reason')})"
        case EventType.LEASE_RELEASED:
            return "lease released"
        case EventType.LEASE_TAKEN_OVER:
            return "lease taken over"
        case EventType.HUMAN_APPROVED:
            return f"human approved {p.get('gate')}"
        case EventType.HUMAN_NOTE:
            return f"human note: {str(p.get('notes', ''))[:80]}"
    return event.type.value  # pragma: no cover - every type is matched above


def render_timeline_html(run_id: str, events: list[Event], generated_at: datetime) -> str:
    """A self-contained HTML timeline of the run's events (for `--export`)."""
    groups: dict[str, list[Event]] = {}
    for event in events:
        task_id = event.payload.get("task_id")
        groups.setdefault(str(task_id) if task_id else "run", []).append(event)
    sections = []
    for name in sorted(groups, key=lambda n: (n == "run", n)):
        rows = []
        for event in groups[name]:
            color = _ACTOR_COLORS.get(event.actor, "#52525b")
            marker = " ~synthetic" if event.synthetic else ""
            parent = f" ←{event.parent_run_seq}" if event.parent_run_seq else ""
            rows.append(
                "<tr>"
                f"<td class='seq'>{event.run_seq}</td>"
                f"<td class='ts'>{html.escape(event.ts.strftime('%H:%M:%S'))}</td>"
                "<td><span class='type'>"
                f"{html.escape(event.type.value)}</span>{marker}{parent}</td>"
                f"<td>{html.escape(_summary(event))}</td>"
                f"<td style='color:{color}'>{html.escape(event.actor)}</td>"
                "</tr>"
            )
        sections.append(f"<h2>{html.escape(name)}</h2><table>{''.join(rows)}</table>")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>jev timeline: {html.escape(run_id)}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #18181b; }}
h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-top: 1.5rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
td {{ border-bottom: 1px solid #e4e4e7; padding: 0.3rem 0.5rem; vertical-align: top; }}
.seq {{ color: #71717a; text-align: right; width: 3rem; }}
.ts {{ color: #71717a; white-space: nowrap; }}
.type {{ color: #2563eb; font-family: monospace; }}
</style></head><body>
<h1>jev timeline: {html.escape(run_id)}</h1>
<p>{len(events)} events, generated {html.escape(generated_at.isoformat(timespec="seconds"))}.</p>
{"".join(sections)}
</body></html>
"""
