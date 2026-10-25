# Event-sourced execution state

Every run mutation is dual-written as an append-only event in the `run_events`
table (`jev` schema v17). The snapshot tables (`runs`, `tasks`, `attempts`,
`decisions`, …) stay the source of truth for the pipeline, the dashboard and
the scheduler; the event log is a projection of it — the audit trail and the
input to `jev events`, `jev replay` and `jev bisect`.

## The log

One row per state change: a fixed envelope (`run_seq`, `type`, `ts`, `actor`,
`parent_run_seq`, `synthetic`, `schema_v`) plus a typed JSON payload. Payloads
**reference, never duplicate**: the full plan JSON, attempt results and
decision JSON stay in the snapshot tables; events carry ids plus the fields
needed to read the story without joins.

Event types (22), grouped by domain:

| Group | Types |
|---|---|
| Run | `run.created`, `run.state_changed`, `run.resumed` |
| Plan | `plan.set`, `design.set`, `validation.recorded`, `tasks.upserted` |
| Task | `task.state_changed`, `task.assigned` |
| Attempt | `attempt.started`, `attempt.tested`, `attempt.finished` |
| Decision | `decision.requested`, `decision.recorded` |
| Quality | `gate.checked` |
| Ship | `ship.recorded` |
| Spend | `spend.added` |
| Lease | `lease.claimed`, `lease.released`, `lease.taken_over` |
| Human | `human.approved`, `human.note` |

Causality is explicit: each event may name its `parent_run_seq` — the earlier
event that caused it (the `attempt.finished` a D5 decision judged, the
`decision.requested` a `decision.recorded` answers). Where the pipeline does
not know the cause, the parent is empty and readers fall back to the task's
previous event.

### Ordering and the single-writer invariant

`run_seq` is a per-run strictly increasing sequence number, assigned as
`MAX(run_seq)+1` **inside the same transaction** as the state change it
describes (`Store._emit` runs inside the caller's `_tx()` block). The run
lease guarantees at most one driver per run; `jev cancel` from another process
serializes on the SQLite write lock, so its event lands in a well-defined
position. If the event insert fails, the whole transaction rolls back — the
state write and the event never diverge. There is no new locking machinery;
the invariant is documented, not enforced, beyond the `UNIQUE(run_id,
run_seq)` constraint.

### What is deliberately not in the log

- **Prompts and transcripts.** They can contain secrets; they stay in the
  redacted `.jev/runs/<id>/` log files. Events store log *paths* (and lesson
  ids), never prompt text. `test_event_payloads_carry_no_secrets` asserts no
  payload model grows a free-text prompt/transcript/secret field.
- **Lease heartbeats.** Every 15s × long runs = pure noise; liveness derives
  from `lease.claimed` plus process state.
- **Streaming spend.** Only `add_spend` (per-attempt and per-decision costs)
  emits `spend.added`; streaming updates stay snapshot-only.
- **Queue, recurring tasks, settings, KPI snapshots, learning bookkeeping.**
  These are repo-scoped or derived documents outside the per-run taxonomy;
  `test_every_writing_method_is_classified` pins the full classification.

### Kill switch

`[events] enabled = true` (default). With `false`, `_emit` is a no-op, the
snapshot tables keep working, and `jev events` / `jev replay` / `jev bisect`
report "event log disabled for this repo".

## `jev events`

`jev events RUN` shows the run's timeline, one line per event, with parent
links (`←412`) and synthetic markers (`~`):

```
   1 16:04:54 run.created  run created: add search  (pipeline)
   2 16:04:54 run.state_changed  INTAKE → PLANNING  (pipeline)
```

Filters: `--type/-t` (repeatable), `--after`, `--limit/-n`, `--json` (raw JSON
lines). The same timeline is on the run page in the dashboard
([dashboard.md](dashboard.md)), with a backfill button for runs with no events
and a "Find where it went bad" form (`jev bisect`).

`jev events backfill RUN` synthesizes a history for runs that predate
the log, from the snapshot tables — every event `synthetic=true`, no causal
links, ordered by timestamp. Backfill runs once per run and refuses runs that
already have events; backfilled runs support timeline viewing and `--mark`
bisect, not full verification (spend cannot be reconstructed).

## `jev replay` — verify mode

`jev replay` re-derives the deterministic decision trace **without any LLM
calls**. Recorded LLM answers are treated as inputs, like reading a file: the
verifier checks that *given those answers*, the pipeline did the right thing.

Checks, in order:

1. **Envelope integrity** — `run_seq` strictly increasing from 1 with no gaps;
   every payload validates against its type's model.
2. **State-transition consistency** — each `run.state_changed` /
   `task.state_changed` / `task.assigned` event's `from` matches the state
   reconstructed from the events before it.
3. **Decision contracts** — each `decision.recorded` names its
   `decision.requested` parent; the recorded point matches the request; every
   answer was one the request allowed; and the request's questions match what
   the current code asks at that decision point — so a change to a D-point's
   questions shows up as a divergence.
4. **Snapshot agreement** (`--diff`) — the state replayed from the log must
   match `runs.state`, the task states and spend. This is the anti-drift
   check: a mutation written without an event shows up here.

Exit 0 when every derivation matches, exit 2 on divergence (with
`--diff`, each divergence prints expected-vs-recorded).

`jev replay RUN --to 412` verifies the prefix and prints the reconstructed
state at that point ("replay a run to a point"). `jev replay RUN --step` is
an interactive stepper — a debugger for runs:

```
[412] task.state_changed  T3 RUNNING → TESTING  (attempt 2 finished: success)
> step | next <type> | until <seq> | state | verify | quit
```

Cassette re-execution (re-running the pipeline against recorded LLM
responses) is deliberately **not implemented**: it is brittle by nature — any
prompt change invalidates the cassette at the first affected call — and is
documented as future work in the design doc (Phase 4).

## `jev bisect` — first-bad scan, not a binary search

Classic `git bisect` re-runs a testable predicate. Here the predicate usually
cannot be re-run (it needed the original LLM outputs), so `jev bisect` scans
the log once for the first event matching the criterion and walks its causal
parents to print the blame path.

What "bad" means (flags combinable; **first match wins**):

| Flag | "Bad" = |
|---|---|
| `--gate` | first `gate.checked` with `blocked=true` |
| `--test` | first `attempt.tested` with `tests="failed"`, or first `attempt.finished` with a non-success status |
| `--task TASK` | first `task.state_changed → FAILED` for TASK (`--task ''` for any task) |
| `--mark SEQ` | human-marked: the named event |

`--good SEQ` restricts the scan to events after SEQ (a known-good point).

```
$ jev bisect 20261015-093012-ab12 --gate

first bad (--gate: first blocked gate):
  [587] gate.checked  gate integration: BLOCKED  (pipeline)
blame path:
  [512] task.assigned  T7 → coder/openrouter-...  (pipeline)
  [560] decision.recorded  D5 → retry (rules)  (pipeline)  ← parent links where recorded
  [581] attempt.finished  attempt 3 finished: success  (pipeline)
  [587] gate.checked  gate integration: BLOCKED  (pipeline)  ← the bad event
```

`--export timeline.html` writes a self-contained HTML timeline (grouped by
task, colored by actor, decision answers inline) — a standalone file for
sharing. The dashboard renders the same log natively on the run page (the Event
timeline section, with the bisect form alongside it); the export remains for a
portable copy.

## Schema notes

- Migration 17 creates `run_events` and adds `run_leases.event_seq` (reserved
  for driver progress reporting). Migrations are append-only; 1–16 are never
  edited.
- `schema_v` on every event; payload models are pydantic. A new optional
  payload field is backward compatible; a removed/renamed field would need a
  version bump and a parser registry (not yet needed: still v1).
- Deleting a run cascades to its events (`ON DELETE CASCADE`), like its lease
  row.
