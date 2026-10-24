# Queue, scheduler and board

jev can work through a list of ideas without you starting each run by hand:

- the **queue** holds ideas, ordered by priority;
- the **scheduler** takes them one at a time and runs each exactly as `jev run` would;
- the **board** shows the queue and every run as a kanban board.

```bash
jev queue add "add rate limiting to the API" --priority high
jev queue add "document the config loader" --plan-only
jev scheduler start                 # foreground; Ctrl-C to stop
jev board --watch                   # in another terminal
```

Everything lives in the repo's `.jev/state.db` (migration 5 adds the `queue` table) and runs
only on your machine. There are no new config keys: scheduler settings are command flags.

## Queue

| Command | Does |
| --- | --- |
| `jev queue add "idea" [--priority high\|normal\|low] [--plan-only] [--learning NAME \| --no-learning] [--profile NAME] [--quality LEVEL] [--auto-ship \| --ask-before-ship]` | Append an idea. The flags are stored with it and used when its run starts. |
| `jev queue list [--all]` | The queue in the order the scheduler takes it: id, priority, status, run, idea, flags, note. Without `--all`, only `queued`, `running` and `waiting` items. |
| `jev queue move ID --up\|--down\|--top` | Move a queued item one place, or to the front. |
| `jev queue remove ID` | Delete an item (not while a live scheduler is running it). Its run, if any, is kept. |
| `jev queue clear --yes` | Delete every `queued` item. Items that already have a run are kept. |

**Order.** Priority first (`high`, `normal`, `low`), then position (the order you added them).
Moving an item into another priority band gives it that band's priority, so the list always
reads top to bottom: `jev queue move 7 --top` on a `low` item makes it `high` if the first
item is `high`, and says so.

**Flags.** `--plan-only`, `--learning` and `--no-learning` mean what they mean on `jev run`
(see [learning.md](learning.md)); a `--learning` profile must exist when you add the item.
`--profile` names a workspace profile ([servers.md](servers.md#workspace-profiles)); it must
exist when you add the item, and the scheduler starts the run with it, so the run gets that
profile's servers, browser, learning and quality. An item without one uses the repo default
(`jev profile use`), like `jev run`. If the profile is gone by the time the scheduler picks
the item, the item is marked `failed` with a note instead of running with other access.
`--quality LEVEL` sets the run's quality gate level ([quality.md](quality.md)); without it the
run gets `[quality] level` from the config at the time it starts. `--auto-ship` and
`--ask-before-ship` choose what the run does once Jev approves it at D6
([ship.md](ship.md#shipping-after-the-approval)); they need `[ship] enabled = true`.

**Statuses.**

| Status | Meaning |
| --- | --- |
| `queued` | Waiting to be picked. |
| `running` | The scheduler is running it, or its run is in progress. |
| `waiting` | Its run stopped for a person: plan approval (`AWAITING_APPROVAL`), ship approval (`AWAITING_SHIP`) or `NEEDS_HUMAN`, or the scheduler was interrupted during it. The note says what to do. |
| `done` | Its run reached `DONE`. |
| `failed` | Its run ended `FAILED` or `BUDGET_EXCEEDED`, or the run could not start (the note says why). |
| `cancelled` | Its run was cancelled with `jev cancel`. |

Once an item has a run, `jev queue list`, `jev scheduler status` and the board read its status
from the run. So when you approve a waiting item's run later (`jev approve RUN`) and it
finishes, the item shows `done` without any queue command.

## Scheduler

```bash
jev scheduler start [--once] [--max-runs N] [--poll SECONDS] [--daily-budget-usd USD]
jev scheduler status
```

`start` runs in the foreground. In a loop it takes the first `queued` item, marks it
`running`, starts a run through the same code path as `jev run`, and records the result:

| The run ends in | The item becomes | The scheduler |
| --- | --- | --- |
| `DONE` | `done` | moves on |
| `AWAITING_APPROVAL`, `AWAITING_SHIP`, `NEEDS_HUMAN` | `waiting` | moves on; you approve or fix later |
| `FAILED`, `BUDGET_EXCEEDED` | `failed` | moves on |
| (an error before or during the run) | `failed`, with the error in the note | moves on |

- `--once`: take at most one item, then exit (also exits at once on an empty queue).
- `--max-runs N`: exit after starting N runs.
- `--poll SECONDS` (default 30): how long to wait before looking again when the queue is
  empty. Without `--once` or `--max-runs` the scheduler keeps waiting for new ideas.
- `--daily-budget-usd USD`: before taking each item, add up what **all runs created today
  (UTC)** have spent, queue runs or not; at or above USD, the scheduler stops. The check is
  between runs, so the run in progress can take the total past the limit by up to its own
  per-run cap (`[budget] per_run_usd`).

**One scheduler per repo.** `start` takes `.jev/scheduler.lock` (JSON: pid, host, start time,
current item) and holds an exclusive `flock` on it while it runs, so a second `start` is
refused, and two starters racing for a stale lock never both win. The kernel drops the
`flock` when the scheduler's process ends, even when it is killed, so a dead scheduler never
blocks the next start. A lock file written by an older jev (no `flock`) keeps the old rules:
it is stale when its pid is no longer running on this host or it cannot be read; one written
on another host is respected (delete the file by hand if you know that scheduler is gone).

`status` shows whether a scheduler is running (pid, host, since when), the item it is on, the
count of items in each status, and today's spend.

**Stopping (Ctrl-C).** The scheduler stops taking items. If a run is in progress it is
interrupted where it is, like a `jev run` you interrupt: the worker step in flight is lost,
but the run's state is saved, so `jev resume RUN` continues it later. The item becomes
`waiting` with the note `interrupted; continue with jev resume RUN`. The scheduler does not
resume it by itself. If the interrupt came before the run was created, the item goes back to
`queued`.

**A scheduler that died** (killed, crashed, machine off) leaves its item `running`. The item
is linked to its run in the same database write that creates the run, so the next
`jev scheduler start` can settle it, before it takes anything new: an item whose run was in
progress becomes `waiting` with the note `the scheduler stopped; continue with jev resume
RUN`; an item whose run had already stopped takes that run's status; an item without a run
goes back to `queued`. Until then, `jev queue remove` (and the dashboard's Remove) can delete
such an item: a `running` item is refused only while a live scheduler holds the lock. A remove
never deletes an item the scheduler claims at the same moment, and if an item disappears
while its run goes on (removed by hand), the scheduler notes it and carries on with the queue.

### Approvals and unattended runs

`[approval] require_plan_approval` is unset by default, and then follows the dispatcher. With the
default `consensus` dispatcher it is off: Jev's D3 approve vote is the gate, so a run
whose plan Jev approves goes straight to execution, and one Jev cannot settle stops in
`NEEDS_HUMAN` and leaves its item `waiting` ([config.md](config.md#dispatcher)). With
`primary = "jev"` or `"rules"` it is on, and a run stops at `AWAITING_APPROVAL` once its plan is
validated, with nothing built until you approve it. The scheduler respects both: an unattended
scheduler leaves every stopped idea `waiting` for you. You come back to the validated plans (or
the questions Jev could not settle), read them with `jev status RUN`, and approve with
`jev approve RUN` or continue with `jev resume RUN` in your terminal.

To review every plan yourself under `consensus`, set `require_plan_approval = true` in
`jev.toml`. To let queued runs go all the way through without you under `jev` or `rules`, set it
to `false`. Only do this for ideas and repos where you are happy for agents to write code
unreviewed (always on separate branches and worktrees; jev never pushes unless you
`jev ship` a run). `--plan-only` on an item always stops at the plan, whatever the setting.

### Cost

Each run has the usual hard caps (`[budget] per_run_usd`, `per_task_usd`), so the most a
scheduler can spend is about the number of runs times the per-run cap. Bound it with
`--max-runs` and `--daily-budget-usd`. As with `jev run`, costs are what the agent CLIs
report: Codex reports none, so its attempts count as $0 here too, and a subscription login's
reported cost is not real spend. See the cost notes in the [README](../README.md#cost).

## Board

```bash
jev board [--watch] [--interval SECONDS] [--all]
```

| Column | Shows |
| --- | --- |
| Backlog | `queued` items, in queue order |
| Planning | runs in `INTAKE`, `PLANNING`, `VALIDATING` |
| Awaiting approval | runs in `AWAITING_APPROVAL` or `AWAITING_SHIP` |
| Running | runs in `EXECUTING`, `INTEGRATING` |
| Done | runs in `DONE`, the 10 most recent (all with `--all`) |
| Blocked | runs in `NEEDS_HUMAN`, `FAILED`, `BUDGET_EXCEEDED`, `CANCELLED` |

Every run is on the board, whether it came from the queue or from `jev run`. A card shows the
idea (shortened), the run id (or `queue #ID`), the current task (the first task in plan order
that is not done, with its state) or the run's state, the spend and the age. Columns sit side
by side in a wide terminal and are stacked in a narrow one. `--watch` redraws every
`--interval` seconds (default 2) from the state database until Ctrl-C; it only reads, so it is
safe next to a running scheduler.
