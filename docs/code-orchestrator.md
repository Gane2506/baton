# Code orchestrator: the Coding Decision Agent (issue #187)

## Roles, in one paragraph

**Jev is the main orchestrator.** It decides what kind of work a run is,
routes it to subsystems, and manages the pipeline
(INTAKE → PLANNING → VALIDATING → AWAITING_APPROVAL → EXECUTING →
INTEGRATING → DONE) and every decision point in it (D1–D8). Nothing in this
document changes that.

**The Coding Decision Agent is the orchestrator for code-writing agents.**
When the work is code, it decides the coding strategy: it routes a coding
task to a model tier and skill, grades a finished diff, and proposes a merge
action. Jev and the quality gate still decide what happens with that
proposal — the code orchestrator never merges, ships, or approves a plan on
its own.

## The model

`S4MPL3BI4S/Coding_Decision_Agent` on Hugging Face
(<https://huggingface.co/S4MPL3BI4S/Coding_Decision_Agent>), a Laya
derivative fine-tuned to grade coding agent work, Apache-2.0. It does not
write code and does not emit prose: given a state (a diff, a tool call, an
agent trace, or a user request) and a fixed set of typed questions, it
returns a label and a calibrated probability for every option in one forward
pass.

What it grades (from the model card):

| Family | Questions |
| --- | --- |
| `code_review` | `code_quality` (0–4), `instruction_followed` (no/yes), `likely_correct` (no/yes), `merge_action` (accept / request_changes / needs_tests / reject) |
| `tool_call` | `tool_selection`, `parameter_structure`, `sequence_accuracy`, `should_call_tool`, `call_verdict` |
| `agent_trace` | `action` (continue / observe / human_review / stop), `needs_review`, `outcome`, `risk` (0–3), `urgency` (0–3) |
| `routing` | `model_tier` (small_fast / mid / frontier / reasoning), `task_difficulty` (0–3), `skill` (code_edit / debug / write_tests / refactor / explain / shell_ops / research / plan) |

## The gate: calibrated confidence, not labels

Gate on `answer_confidence` — the calibrated probability — never on the
entropy-style `confidence` field and never on a label alone. Starting
threshold: **0.7** (the model card's starting point; measure it on your own
traces before trusting it in production).

`jev_agent/code_orchestrator/grades.py:gate_code_review` applies it in this
order, deliberately:

1. `answer_confidence[merge_action]` below the threshold → **human_review**,
   whatever the label says. An unconfident "accept" is not an accept.
2. Confident `accept` → **accept**.
3. Confident `request_changes` / `needs_tests` → **rework**.
4. Confident `reject` → **block**.

## Where it is wired

- **Quality gate (merge decisions).** When `[code_orchestrator] enabled`
  is true, `run_gate` grades the same diff the gate just checked, after the
  tool checks and the AI review. The grade is stored on the gate result
  (`code_grade`) and shown in the gate report ("Code orchestrator" section).
  A blocking grade blocks the gate, so D5/D6 see the work as failed and send
  it back or to a human. The gate's checks, the AI review, and the 0–100
  ship score are unchanged and still run.
- **D4 routing (coding strategy).** When enabled, the task runner adds the
  routing hint (`model_tier`, `skill`, `task_difficulty`,
  `suggested_effort`) to the D4 request state as `code_routing` — but only
  when the hint clears the configured confidence threshold (0.7 by default):
  a low-confidence hint is no hint at all (`routing_hint` returns `{}`),
  never a weak tier Jev might follow as if it were trusted. When the hint
  is present, D4's worker/model/effort instructions name
  `state.code_routing` and tell Jev to prefer it when it fits the allowed
  answers (`dispatch.jev.CODE_ROUTING_INSTRUCTION`). Jev still
  picks worker/model/effort at D4 — the hint informs that decision, it does
  not replace it. A missing model, a failed inference, or a mis-shaped
  verdict never crashes D4: it proceeds without a hint, exactly as before
  the feature (issue #203, C3). Phase 1 (issue #329) also enriches the
  hint with the rating ledger's evidence for its tier
  (`ledger_tier_grades`, `ledger_tier_mean_quality`, `ledger_best_tier`),
  and ledgers the routing grade itself — see "Phase 1: the coach" below.
- **CLI.** `jev code status` (enabled? model? backend present?),
  `jev code grade --task "..." --diff-file change.diff [--tests-file ...]`
  (the same gate the quality gate applies, printed as a table), and
  `jev code route "Refactor …"` (tier + skill + suggested effort).
  `jev code ratings` prints the rating ledger (below), aggregated per
  agent, tier and skill. Every CLI grade/route also writes to the ledger.
- **Dashboard.** The **Code Grade** page (`/code-grade`, issue #254) is the
  browser-first equivalent of `jev code grade` / `jev code route`: paste a
  diff (or upload a diff file), describe the task, and grade it with the
  same gate the quality gate applies; or route a coding task to a model
  tier and skill. The page shows the model's status (enabled? backend
  installed?) and, when the backend is missing, the install guidance
  instead of a grade -- a grade that did not run never looks like a grade.
  Config stays on the Settings page's Code orchestrator section. Grading
  runs in the background (issue #267): the POST only starts the job and the
  result waits on the **status page** (`/code-grade/status`), which polls
  the job and shows the loading stage, the elapsed time, and the grade when
  it lands -- leaving the page, closing the tab, or a dropped connection
  loses nothing, and the grade can be cancelled there (the model call
  itself cannot be stopped mid-inference, so a cancel discards the result
  and the job drains in the background; the status page says so). A grade
  takes a few minutes on CPU: the model loads first (about a minute), then
  grades (1-3 minutes for a small diff, up to about 10 for a large one).
  The grade result leads with the gate decision and its reason, then the
  detail table; `likely_correct` is almost always "no" (the model will not
  assume tests pass) and the page says so. The run review
  (`jev quality show`, the dashboard's Quality review section) now lists
  each gate's code grade -- merge action, confidence, and human_review
  routing -- instead of showing a blocked gate with no trace of the grade
  behind it. Finished dashboard grades and routes are ledgered too
  (below); a cancelled job's discarded result is not.

## Phase 1: the coach (issue #329)

The Coding Decision Agent becomes a coach in the `jev` workflow: grades
stop sitting only on gate results and start teaching the pipeline which
agents, tiers and skills do good work. Phase 1 adds no new grading calls
in the hot path — it records the grades that already happen (gate + D4
hint + CLI/dashboard) and puts them to work. The threshold stays 0.7;
recalibration is Phase 4.

### The rating ledger

Every grade is appended to `<repo>/.jev/cda-ratings.jsonl`, one JSON
object per line (`src/jev_agent/code_orchestrator/ledger.py`):
a diff grade at the gate, a routing grade at D4, a trace grade from
`jev code trace`, and CLI/dashboard grades. Each record carries the
grade's labels (code_quality, instruction_followed, likely_correct,
merge_action, calibrated confidence, gate decision) and the context that
makes it aggregable: agent (worker), model, effort, tier, skill, run and
task, source (`gate` | `d4` | `cli` | `dashboard`).

**Location, decided deliberately.** The ledger is repo-level
(the learning graph is per-repo) but a *separate file* from `state.db`
inside `.jev/`. The Mac update rule used to flush `state.db` on version
updates; a ledger inside that database would have been wiped with it, and a
file next to it survived the flush. The flush is retired (deploy Phase 5,
issue #347: the live Mac reproduction found no cause — the flush was curing
a stale dashboard process, and `jev dashboard stop` + restart is the fix),
but the separate file stays: a problem with the state database never takes
the grades with it. `.jev/` is gitignored, so the ledger
never lands in the repo.

**Rules.** A grade that did not run is never a score: a missing backend
records `status="unavailable"` with no quality fields, and an inference
failure records `status="error"`. Aggregates count graded diff grades
only for quality/accept rates; unavailable/error records count in `n`
and in their own counters. Writes are best-effort and never raise —
a ledger failure must never break a run — and in the async pipeline
they happen write-after-grade, in a thread. Writes are locked (Phase 2):
each append holds the sidecar `<ledger>.lock` file, so concurrent
writers cannot interleave rows; a lock timeout fails the write, never
a partial row. Reads tolerate corruption:
a bad line is skipped, never fatal. `jev code ratings` and the
dashboard's Learning page (Ratings tab) show the aggregates per agent,
tier and skill, plus recent grades.

**How it feeds learning.** The aggregates are read in two places: D4
(the routing hint carries the ledger's evidence for its tier —
`ledger_tier_grades`, `ledger_tier_mean_quality`, and `ledger_best_tier`
when another tier grades better — and D4's instructions tell Jev to
prefer tiers and skills with a strong ledger record), and the Learning
page, where the Ratings tab sits next to the lesson graph. The ledger
never decides anything on its own: the hint is still gated at 0.7, Jev
still chooses among its allowed answers, and a tier needs at least 3
graded diffs before it can be named the best tier.

### Rework briefs

When a gate blocks, the result carries `rework_brief`
(`src/jev_agent/code_orchestrator/briefs.py`, rendered into the gate
report under "Rework brief" and handed to the next attempt through D5's
feedback, so the coder sees it in its prompt). The model returns labels,
never prose, so the brief is composed from the three sources that do
carry reasons: the grade's structured outputs (which signals fired —
low code_quality, instruction_followed "no", likely_correct "no",
`needs_tests`/`request_changes`/`reject`, low-confidence human_review),
the gate's failed checks and findings, and the AI review's summary.
Every line names its source, and a signal that did not fire produces no
instruction.

## Phase 2: trace supervision + ledger locking (issue #379)

Phase 1 graded traces only when a human asked (`jev code trace`); the
pipeline never watched a running agent. Phase 2 adds the watcher:
`src/jev_agent/code_orchestrator/supervision.py` grades a running
agent's trace through the backend, runs the grade through `gate_trace`,
and returns a supervision decision the caller acts on:

| Gate decision | Supervision action | Meaning |
| --- | --- | --- |
| `accept` | `continue` | Confident trace grade says keep going. |
| `rework` | `observe` | Confident `observe` grade: keep going, watch the next step. |
| `human_review` | `human_review` | The grade says so, or it is not confident enough to trust. |
| `block` | `stop` | Confident trace grade says stop. |

The mapping is fixed, not interpreted per call site: the gate already
encodes the confidence rules (a low-confidence `continue` is not
trusted, a `stop` is never downgraded), and the supervisor only
translates its vocabulary into the loop's vocabulary. Two failure
rules, both fail-closed: a missing backend observes blind — no grade
ran, so there is nothing to act on yet, and the unavailable grade is
still recorded with no quality fields, never a score; an inference
failure, or a grade the gate cannot use, escalates to `human_review`.

**Async, by construction.** Grading takes minutes on CPU, so the
supervisor's async entry points (`supervise_trace_async`,
`supervise_at_boundary_async`, `supervise_tool_call_async`) run the
blocking model call in a thread, exactly like Phase 1's write-after-grade
ledger writes. Supervision must never block a running system: a caller
awaits it from a background task, never inline in the run's hot path.
The sync entry points exist for the CLI, which has no loop to block.

**Stage boundaries.** `supervise_at_boundary(stage, trace)` is the
named entry point for stage-boundary callers (pipeline stage
transitions, dashboard job checkpoints): the stage name rides in the
decision and in the ledger record's detail (`stage=EXECUTING`), so a
later reader knows where the trace was taken. The supervisor itself
never acts: it does not halt runs, cancel tasks, or change
assignments — the caller owns the act, and the decision carries the
gate's reason so the act is auditable.

**Tool calls are sampled, not gated.** A sampled tool call is graded,
recorded to the ledger, and always returned as `observe` with its
verdict in the reason. There is no gate function for tool calls because
the family documents no closed label set for
`should_call_tool`/`call_verdict` — `normalise_tool_call` validates
shape only and refuses to invent labels to coerce into — so inventing
one here would fake model semantics. The sample is watched; a human or
a later trace grade acts on it if it matters.

**First caller.** `jev code supervise --trace-file <file>` (or
`--tool-call-file`, exactly one of the two; `--stage` names the
boundary) runs supervision asynchronously and prints the decision, its
reason, and whether the grade reached the ledger. Unlike `trace`, a
missing backend does not fail the command: the supervisor observes
blind and says so — observing is the honest answer when no grade ran.

**What Phase 2 does not do.** No pipeline stage-boundary caller yet:
the decision has no consumer in the loop today (no loop policy reads
supervision decisions, no dashboard panel shows them), so wiring one
now would burn minutes of CPU per stage for output nobody reads. When
a consumer exists, it calls `supervise_at_boundary_async` from a
background task — the supervisor is built for that call, and the CLI
proves the path end to end. The threshold stays 0.7; supervision trusts
a trace grade exactly as far as the gate trusts a diff grade.

**Ledger locking.** Each ledger append now holds an exclusive lock on
a sidecar `<ledger>.lock` file (flock on POSIX, msvcrt locking on
Windows) for the single write, so concurrent writers — threads,
processes, dashboard and CLI at once — cannot interleave or corrupt
rows. A lock that cannot be acquired within 10 seconds fails the write
(`record` returns False), never a partial row; a platform with neither
locking API serializes in-process only, which `_FileLock` documents
rather than papering over. The lock file lives next to the ledger
inside `.jev/` and is never committed, like the ledger itself.

## Phase 3: plan critique + hint usage (issue #380)

Phase 2 watched running agents; Phase 3 looks before execution and
learns whether the advice it gave was any good.

**Pre-flight plan critique.** At VALIDATING, after D2's validator and
before D3's verdict, the pipeline routes a short plan request (summary
+ task titles, capped at 2000 characters — `route` does not compact)
and turns the routing grade into critique notes appended to
`validation.issues` (`src/jev_agent/code_orchestrator/critique.py`,
`pipeline._critique_plan`). Routing is the only family that assesses a
plan — difficulty, skill, tier confidence — where code review grades
diffs that do not exist yet and trace grades agents that are not
running yet. The critique is advisory only: D3's verdict logic is
unchanged, and the notes inform D3 and, on revise, the revising planner
(which already reads `validation.issues`). Below the 0.7 threshold
there is no critique at all, not a hedged one — the same rule as
`routing_hint` — and a missing backend means no critique either. When
the grade is confident, notes come from the model signal (difficulty
3: consider splitting into smaller tasks) and from structure (no tasks
at all; tasks with an empty title or description, named as counts).
Notes are prefixed `CDA plan critique: ` so they read apart from the
validator's own issues. Critiques are NOT written to the ledger: they
are not grades, and writing them would pollute the aggregates. Only
hint usage feeds the ledger in Phase 3.

**Hint-usage tracking.** D4 already records its routing grade; now it
also records the hint itself. When a hint is given and D4 assigns, an
offer record (`kind="hint_usage"`, `status="offered"`) lands in the
ledger with the hint's tier/skill, the assigned effort, and whether the
assignment followed the hint — `followed` is effort-match only, because
D4 chooses worker/model/effort, not skill, so skill-following cannot be
observed at D4. When the gate later grades that task's diff, it resolves
the task's latest still-open offer with an outcome record
(`status="outcome"`): `helped` is exactly gate-`accept`. Offers are
immutable and append-only, so "still open" is replayed from order: an
offer is open until an outcome written after it closes it. A gate
without an offer writes nothing extra, and unavailable/error gates
resolve nothing — helped is meaningless without a grade. Summaries
(`summarize_hints`, `RatingLedger.hint_summary`,
`LedgerRatings.hints`) count offers, follows, resolutions, and helps
overall and per tier/skill, with rates that are None — unknown, not
zero — when their denominator is zero. Hint records never join the
agent/tier/skill aggregates (they are not grades; counting them would
inflate tier `n`); `total` still counts every ledger line, hints
included, because it counts lines. The dashboard's Recent grades table
renders hint records generically and correctly (kind, tier, skill,
status, detail); the aggregate tables stay grade-pure.

### Roadmap (issue #329)

- **Phase 1 (this):** ledger + rework briefs + dashboard ratings + D4
  reads the ledger.
- **Phase 2 (built, issue #379):** async trace supervision — the
  supervisor core, its async entry points, and `jev code supervise` as
  its first caller; trace grades feed the same ledger; ledger writes
  are file-locked. A pipeline stage-boundary caller waits on a consumer
  for the decision (see Phase 2, above).
- **Phase 3 (built, issue #380):** pre-flight plan critique at
  VALIDATING (advisory notes in `validation.issues`, never ledgered)
  plus hint-usage tracking (D4 offers, gate outcomes, separate summary;
  hint records excluded from grade aggregates).
- **Phase 3:** pre-flight plan critique at VALIDATING, before any code is
  written.
- **Phase 4:** recalibrate 0.7 once 50+ real grades exist in the ledger.
  Until then the threshold stays 0.7 and nothing changes it.

## Install

From the dashboard, no terminal needed: open **Code Grade**
(`/code-grade`) and use **Install backend**. It asks for confirmation
first, naming the exact command, the sizes and the persistence note for
this deployment, then installs in the background with progress, a log
tail and a verification probe — and warms the model checkpoint, so the
first grade after install needs no download. The status panel then reads
**warmed** for that model; another repo whose model was not warmed reads
**available — checkpoint downloads on first grade**. A **Verify**
action re-runs the probe, **Repair** reinstalls `laya` when verification
fails, and **Uninstall** removes `laya` only (never torch, never
`jev-agent`); the checkpoint cache is kept, so reinstalling is faster.
While an install, repair or uninstall runs, grades are refused (nothing
is recorded for a refused grade) and verification waits.

The fixed command the button runs, built server-side from the
dashboard's own interpreter (never from a request):

```bash
# Default path (Linux/Windows with an NVIDIA driver present):
<python> -m pip install "laya>=0.4.15"
# CPU path (macOS always; Linux/Windows without a working nvidia-smi):
<python> -m pip install torch --index-url https://download.pytorch.org/whl/cpu
<python> -m pip install "laya>=0.4.15"
# Repair: <python> -m pip uninstall -y laya, then the install sequence above.
# Uninstall: <python> -m pip uninstall -y laya
```

Sizes, before you click: torch about 2.5 GB (default wheel) or the
smaller CPU wheel; `laya` plus transformers about 0.5 GB; the checkpoint
about 842 MB; temp staging at least 1 GB free. Preflight refuses (with a
retry) when disk, network or cache space is short, and swaps the button
for the guide command above when the environment cannot be installed
into (permission denied, PEP 668, no wheel for this Python) — never with
`--break-system-packages`.

Persistence: an install survives dashboard restarts. On a Mac installed
via `uv tool install`, reinstalling the tool recreates its environment
and wipes the extra, so the button is offered again afterwards. **On
Docker, a container recreate wipes a container-side install** (no volume
is added for it): after a recreate, run the install from the button
again. A dashboard started with a small tmpfs (the dev VM's 512M
`/tmp`) still fails first grades until restarted with a real-disk
TMPDIR — the diagnostics page warns when temp space is below the 1 GB
staging need; the install itself always uses a real-disk temp directory.

Manual recovery, for failures the button cannot fix (with `<python>`
meaning the dashboard's own interpreter):

```bash
# Remove a broken backend:
<python> -m pip uninstall -y laya
# Recover from a broken torch (uninstall-first is intended: a plain
# reinstall no-ops on satisfied-but-broken torch). CPU path:
<python> -m pip uninstall -y torch
<python> -m pip install torch --index-url https://download.pytorch.org/whl/cpu
<python> -m pip install "laya>=0.4.15"
# Default path:
<python> -m pip uninstall -y torch
<python> -m pip install torch
<python> -m pip install "laya>=0.4.15"
# Clear a corrupt pip cache: <python> -m pip cache purge
# Clear a corrupt checkpoint cache: remove the model directory under the
# Hugging Face cache (~/.cache/huggingface/hub, or $HF_HOME), so the next
# grade re-downloads it.
# Recover from a filled disk: free space, then retry; the button re-runs
# preflight first.
```

The terminal equivalent still works:

```bash
pip install laya
# or, with Baton's own extra (same thing, declared in pyproject.toml):
pip install "jev-agent[code-orchestrator]"
```

That is the whole install: no `coding_decision_agent` package is needed.
Baton ships the model's fixed question wording itself (recovered verbatim
from the public training dataset `S4MPL3BI4S/coding-decision-cases`; see
`src/jev_agent/code_orchestrator/laya_backend.py`) and runs the
`S4MPL3BI4S/Coding_Decision_Agent` checkpoint through the `laya` runtime.
If the `coding_decision_agent` package ever becomes installable, Baton
prefers it; until then the built-in backend is the real one.

```bash
# Verify it works (grade a diff the way the quality gate does):
jev code status    # backend_available: yes
jev code grade --task "Fix the off-by-one in paginate()" --diff-file change.diff
```

### What the first grade downloads, and what it needs

- **Weights:** the first grade downloads the ~842 MB checkpoint
  (`model.safetensors`, bf16) into the Hugging Face cache
  (`~/.cache/huggingface/hub`, or `$HF_HOME`). Later grades reuse it;
  set `HF_HUB_OFFLINE=1` to run fully offline after the first download.
  **TMPDIR matters:** `laya.load` reconstructs the checkpoint through
  the temp dir, and the default /tmp is a 512M tmpfs on the dev VM --
  smaller than the model, so the first load fails with "No space left on
  device". Point TMPDIR at a directory on the home disk before the first
  grade (e.g. `TMPDIR=~/workspace/baton-program/cda/tmpdir`).
- **Torch:** `pip install laya` pulls the default torch wheel, which
  includes CUDA libraries (~2.5 GB). On a CPU-only machine install the CPU
  wheel first, then `laya` without its torch dependency resolution
  fighting you:
  ```bash
  pip install torch --index-url https://download.pytorch.org/whl/cpu
  pip install laya
  ```
- **Hardware:** a GPU is used automatically when CUDA is available, but
  none is required — the model runs on CPU. On a 2-core CPU VM a grade
  takes on the order of a minute: measured 2026-10-24, model load ~50 s
  (warm cache; the first load also downloads the weights), one
  `code_review` predict 76–209 s depending on state size — the training
  compact format is about half the tokens of a raw unified diff and
  roughly twice as fast. Memory: roughly 2–3 GB RSS for torch + the bf16
  weights. If that does not fit, keep the feature off — the gate records
  `unavailable` and the run proceeds exactly as before.
- **Never a hard dependency:** `laya` is a declared *optional*
  dependency (`[project.optional-dependencies] code-orchestrator` in
  `pyproject.toml`), not a core one: it is a 421M model behind `laya` +
  torch, and CI must not download weights. The backend loads lazily,
  only when grading is enabled and a grade is actually requested, and
  importing Baton never imports torch. If the package is absent, `jev
  code grade`/`route` fail with a clear message, and the quality gate
  records the grade as `unavailable` instead of blocking the run. A grade
  that did not run must never look like a grade.

> **History (2026-10-24):** `coding_decision_agent` is not on PyPI (its
> PyPI JSON endpoint 404s; the model card's install block only lists
> `pip install laya`), and the training repo is unpublished, so the model
> card's `CodingDecisionAgent` class ships nowhere importable. Baton
> therefore does not call `laya.load(...)` with invented question wording:
> the fixed wording ships in the `coding_decision_agent` package the card
> names, and scoring against guessed wording would make the calibrated
> confidence meaningless. The wording Baton uses is the dataset's own
> (`S4MPL3BI4S/coding-decision-cases`), pinned by a fixture test.

## Configuration

See `docs/config.md` (`[code_orchestrator]`):

```toml
[code_orchestrator]
enabled = true
model_id = "S4MPL3BI4S/Coding_Decision_Agent"
confidence_threshold = 0.7
max_diff_chars = 7000
```

Off by default, so existing runs are byte-for-byte unchanged. Enabling it
without the model installed changes nothing except a recorded
"unavailable" grade and a note from `jev code status`.

## Context: 2048 tokens

The model's context is 2048 tokens (`head_max_len` 320 for the options, the
rest for the state). Diffs are compacted before scoring
(`grades.compact_diff`, default 7000 characters): short diffs pass through
untouched; a long diff keeps its head and tail with an explicit marker
naming how much was dropped, so a grader never silently scores a
partial diff as if it were whole. The character budget is an approximation
(≈4 characters per token), not a token count — no tokenizer is a dependency.
Compaction is `CodeOrchestrator.grade_code`'s job, not the caller's: any
caller that forgot used to send an over-context diff to the model, and a
2-line diff of very long lines used to be sliced with no marker at all
(issue #203, C2/I10). The marker's count is computed from the head/tail
actually returned, and the result is never sliced after the marker is built.

## The input must look like training (measured, issue #259)

The model was trained on compacted diffs and full issue bodies, and a raw
unified diff plus a one-line task is out-of-distribution for it. Measured
live on commit 84eb7a7 (a known-good merged diff), 2×2:

| Input | code_quality | instruction_followed | likely_correct | merge_action |
| --- | --- | --- | --- | --- |
| raw diff + subject | 2 (0.42) | no (0.75) | no (0.90) | request_changes (0.43) |
| raw diff + full task | 2 (0.40) | no (0.52) | no (0.89) | request_changes (0.37) |
| training format + subject | 4 (0.38) | no (0.77) | no (0.89) | request_changes (0.39) |
| training format + full task | 4 (0.77) | yes (0.59) | no (0.86) | accept (0.45) |

The **diff format drives code_quality** (raw → 2, training → 4, whatever
the task), and **instruction_followed and merge_action need both** the
training format and the full task text. So the backend reformats every
diff into the training rows' compact shape before sending it
(`laya_backend.format_training_diff`: `### <path>` file headers, `@@`
hunk headers kept, runs of unchanged lines collapsed to
`  ... (N unchanged lines)`), and the quality gate grades against the
task's title **plus description** (`quality.gate._code_grade_task_text`,
capped at 2000 characters), not the title alone. The reformat also
roughly halves the state, so it grades about twice as fast on CPU.

Two honest caveats from the same measurement: **likely_correct stayed
"no" (0.86–0.90) in all four variants** — the state carries no evidence
the tests pass, and the model will not assume they do. Passing the test
outcome in `notes` moved its confidence 0.86 → 0.60 but did not flip it
(see "Measured threshold" below). And **merge_action confidence stayed
low (0.37–0.45) even in-distribution** — below the 0.7 threshold, so
this diff gates to human_review, the safe direction. The threshold
measurement on six known-good diffs (the four largest planned diffs were
not graded — the run was OOM-killed by concurrent Rust jobs on the VM,
and the gate grades single-task diffs, not 88 KB merges) decides whether
0.7 is right or needs adjusting; see below.

## Measured threshold: 0.7 stays (issue #259)

Measured live on the CPU VM (13 grades, `live_measurements.json` in the
issue notes): six known-good merged diffs, one synthetic bad diff (a real
fix with its guard removed), one tests-variant, and five per-file regrade
samples. The verdict is **keep 0.7 — do not lower it**.

- **merge_action confidence is 0.36–0.52 on every grade** — good diffs,
  the bad diff, and regrade samples alike. Nothing clears 0.7, so at 0.7
  the gate sends everything to human_review. The auto-accept and
  auto-reject paths are effectively dead on CPU-measured Baton diffs.
- **No threshold separates good from bad.** The bad diff scored
  request_changes at 0.44 — squarely inside the good diffs' 0.36–0.51
  range. Lowering the bar to ~0.45 would auto-accept three good diffs
  but rests on seven data points and a model whose benchmark
  merge_action accuracy is 0.59; it is not a calibration, it is a guess.
- **code_quality separates weakly.** Good diffs scored 4 on 5 of 6 (one
  2, on a large multi-file round); the bad diff scored 2. A 2 is not
  proof of bad — one good diff scored it too.
- **likely_correct is always "no"** (13 of 13, including the known-good
  diffs). Passing the test outcome in `notes` moved its confidence
  0.86 → 0.60 but did not flip it. It is not a usable gate signal; the
  gate already knows the test outcome from its own checks.
- **Regrade vs the swarm grades: mean absolute difference 1.0** (2 of 6
  agree, 2 off by 1 in the model's favour, 1 off by 1 against, 1 off by
  3 — nav.py model 1 vs swarm 4). The model grades the *change*, the
  swarm graded the *file*; they are not the same judgement, and the
  model does not reproduce the swarm's file grades.

So the feature's value is the human_review routing, the code_quality
signal, and the recorded grade — not auto-merge. The gate's 0.7 default
stands, and the model is a second opinion that can only block on a
confident reject (which never fired in 13 grades) or send work to a
human. That is the safe, honest configuration until a much larger
calibration set exists.

## Error taxonomy (decided, not accidental)

- Missing backend → `CodeOrchestratorUnavailable`. The quality gate records
  "unavailable" and does not block; D4 proceeds without a hint; the CLI
  reports it and stops.
- Backend failed during inference → `CodeOrchestratorError`. The quality
  gate blocks fail-closed: a grade was attempted and cannot be trusted,
  which is not the same as no grade being configured. D4 still proceeds
  without a hint (a hint is advisory); the CLI reports it and stops.
- Mis-shaped verdict (unknown label, out-of-range score, a yes/no field
  that is neither — "maybe" is not a "no") → `ValueError`. Same handling
  as an inference failure: fail-closed at the gate, no hint at D4, a clear
  CLI error instead of a traceback.

## Two confidence bars, on purpose

`[dispatcher.jev] min_confidence` (0.55) gates Jev's own decision answers:
below it the decision falls back to the rules dispatcher. The code gate's
0.7 gates the Coding Decision Agent's *calibrated* `answer_confidence` on a
merge action (the model card's starting point), and the same 0.7 gates
routing hints. They are different models' confidences on different
decisions, not one bar with two values (issue #203, I7).

## Laya removal (issue #193)

- The Laya second vote is removed. `[dispatcher.laya]` is no longer documented or
  supported, and `jev laya tune` / `jev laya reset` are removed. Delete any
  `[dispatcher.laya]` table (including `[dispatcher.laya.weights]`) from `jev.toml`
  and your user config. Jev alone decides as main orchestrator; code grading and
  routing belong to the Coding Decision Agent above, gated on calibrated confidence.
- What remains of "Laya" is lineage, not a vote: the Coding Decision Agent model is a
  Laya derivative (see The model above), and `laya` is still the PyPI package name
  for that model's runtime (see Install above). Neither is a Baton second vote.
- The deterministic 0–100 ship score in `quality/scoring.py` is a static score from
  check results, not a model vote; its weights source is owned by the core/quality
  track of this removal.

## Not yet wired (honest gaps)

- `tool_call` and `agent_trace` grading are wired through the live
  `laya` backend (`LayaBackend.grade_trace` / `grade_tool_call`,
  `jev code trace`, `jev code supervise`) with the trained question
  wording from the case dataset. The caveat is the state shape, not the
  wording: the training states are structured dicts, and Baton's
  callers pass free text, which the model reads as given. A caller with
  a structured trace or tool call should build the dict state instead
  of passing text.
- The quality gate grades the diff against the task's title **plus
  description**, and passes its test outcome as the state's `notes`
  (issue #254): the tests check's status and detail, capped at 2000
  characters. Measured live on a known-good diff, the notes move the
  likely_correct confidence 0.84 → 0.60 without flipping the verdict.
  `jev code grade --tests-file` passes test output through as `notes`
  when a human asks for it.
- Live-model evaluation (issue #259): the model now runs, and
  `tests/live/test_code_orchestrator_live.py` is the smoke test for a run
  that has it installed (`JEV_LIVE_TESTS=1`). The 0.7 threshold was
  measured on real Baton diffs during that issue; see its report for the
  numbers before trusting the default in production.
