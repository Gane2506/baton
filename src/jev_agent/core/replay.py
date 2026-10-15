"""Decision-trace verification for `jev replay` (docs/events.md).

Verify mode (the only replay mode: cassette re-execution is deliberately out of
scope) re-derives everything deterministic from the recorded events and checks it
against what the log says happened — no LLM calls:

1. **Envelope integrity**: `run_seq` strictly increasing from 1 with no gaps, every
   payload validating against its type's model.
2. **State-transition consistency**: each `run.state_changed` / `task.state_changed`
   / `task.assigned` event's `from` matches the state reconstructed from the events
   before it. A mismatch means the log is corrupt, an emission was missed, or two
   writers interleaved outside the single-writer invariant.
3. **Decision contracts**: each `decision.recorded` names its `decision.requested`
   as its causal parent; the recorded point matches the request, every answer is one
   the request allowed, and the request's questions match the current code's question
   schema for that decision point (`config.schema.DECISION_QUESTIONS`) — so a change
   to what a D-point asks shows up as a divergence.
4. **Snapshot agreement** (`--diff`): the state reconstructed from the log must match
   the snapshot tables (`runs.state`, task states, spend). A mismatch is the dual-write
   drift the log exists to catch (design R1).

Recorded LLM answers are never re-derived: they are treated as inputs, like reading
a file. Full prompt re-derivation (rebuilding each D1-D8 `DecisionRequest` from the
events) is future work; the question-schema check above is the implemented subset.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jev_agent.config.schema import DECISION_QUESTIONS
from jev_agent.core.events import Event, EventType
from jev_agent.core.models import RunState, TaskState
from jev_agent.core.store import Store

#: Divergence kinds reported by `verify_run`.
DIVERGENCE_GAP = "gap"  # run_seq not strictly increasing from 1
DIVERGENCE_TRANSITION = "transition"  # an event's `from` disagrees with the replayed state
DIVERGENCE_QUESTIONS = "questions"  # a decision request's questions differ from the code's
DIVERGENCE_ANSWER = "answer"  # a recorded answer breaks the request's contract
DIVERGENCE_PARENT = "parent"  # a decision.recorded without its decision.requested parent
DIVERGENCE_SNAPSHOT = "snapshot"  # the replayed state disagrees with the snapshot tables

_SPEND_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Divergence:
    """One place where the log disagrees with itself, the code, or the snapshots."""

    run_seq: int | None
    kind: str
    message: str
    expected: str = ""
    recorded: str = ""


@dataclass
class ReplayedState:
    """The run's state as reconstructed from the event log alone."""

    run_state: str | None = None
    task_states: dict[str, str] = field(default_factory=dict)
    spend_usd: float = 0.0
    decisions: int = 0


@dataclass
class Verification:
    """The result of `verify_run`."""

    run_id: str
    events_checked: int
    divergences: list[Divergence] = field(default_factory=list)
    state: ReplayedState = field(default_factory=ReplayedState)

    @property
    def ok(self) -> bool:
        return not self.divergences


def verify_run(store: Store, run_id: str, *, to_seq: int | None = None) -> Verification:
    """Verify the run's event log (Mode A: decision-trace verification, no LLM calls).

    With `to_seq`, only the prefix up to that `run_seq` is verified and no snapshot
    comparison is done (mid-run snapshots are meaningless). Raises `NotFoundError`
    for an unknown run.
    """
    events = store.list_events(run_id)
    if to_seq is not None:
        events = [e for e in events if e.run_seq <= to_seq]
    full = to_seq is None
    result = Verification(run_id=run_id, events_checked=len(events))
    state = result.state
    requested: dict[int, Event] = {}  # run_seq -> decision.requested event

    for index, event in enumerate(events):
        if event.run_seq != index + 1:
            result.divergences.append(
                Divergence(
                    run_seq=event.run_seq,
                    kind=DIVERGENCE_GAP,
                    message="run_seq is not gapless: the log lost an event or two writers raced",
                    expected=str(index + 1),
                    recorded=str(event.run_seq),
                )
            )
        _verify_event(event, state, requested, result.divergences, full=full)

    if full:
        _verify_snapshot(store, run_id, state, result.divergences, events)
    return result


def _verify_event(
    event: Event,
    state: ReplayedState,
    requested: dict[int, Event],
    divergences: list[Divergence],
    *,
    full: bool,
) -> None:
    """Replay one event into `state`, recording divergences."""
    p = event.payload
    match event.type:
        case EventType.RUN_CREATED:
            state.run_state = str(RunState.INTAKE)
        case EventType.RUN_STATE_CHANGED:
            _check_from(
                event,
                divergences,
                what="run",
                current=state.run_state,
                from_state=str(p.get("from_state")),
            )
            state.run_state = str(p.get("to_state"))
        case EventType.TASKS_UPSERTED:
            for task_id in p.get("task_ids", []):
                state.task_states.setdefault(str(task_id), str(TaskState.PENDING))
        case EventType.TASK_STATE_CHANGED:
            task_id = str(p.get("task_id"))
            _check_from(
                event,
                divergences,
                what=f"task {task_id}",
                current=state.task_states.get(task_id, str(TaskState.PENDING)),
                from_state=str(p.get("from_state")),
            )
            state.task_states[task_id] = str(p.get("to_state"))
        case EventType.TASK_ASSIGNED:
            # set_task_assignment sets the state too; older payloads lack it.
            if p.get("state") is not None:
                state.task_states[str(p.get("task_id"))] = str(p.get("state"))
        case EventType.SPEND_ADDED:
            state.spend_usd = float(p.get("running_total", 0.0))
        case EventType.DECISION_REQUESTED:
            requested[event.run_seq] = event
            _check_questions(event, divergences)
        case EventType.DECISION_RECORDED:
            state.decisions += 1
            if full:  # a prefix may cut the request off; only the full log must link
                _check_decision(event, requested, divergences)


def _check_from(
    event: Event,
    divergences: list[Divergence],
    *,
    what: str,
    current: str | None,
    from_state: str,
) -> None:
    """The event's `from` must match the replayed state (None: the log starts mid-run,
    e.g. events were enabled after the run began — accept whatever it says)."""
    if current is not None and current != from_state:
        divergences.append(
            Divergence(
                run_seq=event.run_seq,
                kind=DIVERGENCE_TRANSITION,
                message=f"{what} was {current}, but the event says it came from {from_state}",
                expected=current,
                recorded=from_state,
            )
        )


def _check_questions(event: Event, divergences: list[Divergence]) -> None:
    """The request's questions must match what the current code asks at that point."""
    point = str(event.payload.get("point"))
    asked = sorted(event.payload.get("questions", {}))
    want = sorted(DECISION_QUESTIONS.get(point, ()))
    if point not in DECISION_QUESTIONS:
        divergences.append(
            Divergence(
                run_seq=event.run_seq,
                kind=DIVERGENCE_QUESTIONS,
                message=f"unknown decision point {point}",
                recorded=point,
            )
        )
    elif asked != want:
        divergences.append(
            Divergence(
                run_seq=event.run_seq,
                kind=DIVERGENCE_QUESTIONS,
                message=(
                    f"{point} asked {asked}, but the current code asks {want}: the request"
                    " builders changed since this run"
                ),
                expected=", ".join(want),
                recorded=", ".join(asked),
            )
        )


def _check_decision(
    event: Event, requested: dict[int, Event], divergences: list[Divergence]
) -> None:
    """The recorded decision must honor its request's contract."""
    p = event.payload
    parent_seq = event.parent_run_seq
    parent = requested.get(parent_seq) if parent_seq is not None else None
    if parent is None:
        divergences.append(
            Divergence(
                run_seq=event.run_seq,
                kind=DIVERGENCE_PARENT,
                message="decision.recorded does not name its decision.requested parent",
            )
        )
        return
    questions: dict[str, list[str]] = parent.payload.get("questions", {})
    if str(p.get("point")) != str(parent.payload.get("point")):
        divergences.append(
            Divergence(
                run_seq=event.run_seq,
                kind=DIVERGENCE_PARENT,
                message="decision.recorded answers a different point than its request",
                expected=str(parent.payload.get("point")),
                recorded=str(p.get("point")),
            )
        )
    answers: dict[str, str] = p.get("answers", {})
    for name, answer in answers.items():
        allowed = questions.get(name)
        if allowed is None:
            divergences.append(
                Divergence(
                    run_seq=event.run_seq,
                    kind=DIVERGENCE_ANSWER,
                    message=f"answer {name!r} was not asked",
                    recorded=name,
                )
            )
        elif answer not in allowed:
            divergences.append(
                Divergence(
                    run_seq=event.run_seq,
                    kind=DIVERGENCE_ANSWER,
                    message=f"answer {name}={answer!r} is not one of the allowed {allowed}",
                    expected=", ".join(allowed),
                    recorded=str(answer),
                )
            )


def _verify_snapshot(
    store: Store,
    run_id: str,
    state: ReplayedState,
    divergences: list[Divergence],
    events: list[Event],
) -> None:
    """The log must fully explain the snapshot tables (the anti-drift check).

    Backfilled (synthetic) logs cannot reconstruct spend — the snapshots never
    recorded per-event totals — so the spend check is skipped for them; the state
    checks still apply.
    """
    run = store.get_run(run_id)
    if state.run_state is not None and state.run_state != str(run.state):
        divergences.append(
            Divergence(
                run_seq=None,
                kind=DIVERGENCE_SNAPSHOT,
                message="replayed run state disagrees with the snapshot",
                expected=str(run.state),
                recorded=state.run_state,
            )
        )
    for task in store.list_tasks(run_id):
        replayed = state.task_states.get(task.id)
        if replayed is not None and replayed != str(task.state):
            divergences.append(
                Divergence(
                    run_seq=None,
                    kind=DIVERGENCE_SNAPSHOT,
                    message=f"replayed state of {task.id} disagrees with the snapshot",
                    expected=str(task.state),
                    recorded=replayed,
                )
            )
    if any(e.synthetic for e in events):
        return  # backfilled logs cannot reconstruct spend; states were checked above
    if abs(state.spend_usd - run.spend_usd) > _SPEND_TOLERANCE:
        divergences.append(
            Divergence(
                run_seq=None,
                kind=DIVERGENCE_SNAPSHOT,
                message="replayed spend disagrees with the snapshot",
                expected=f"${run.spend_usd:.2f}",
                recorded=f"${state.spend_usd:.2f}",
            )
        )
