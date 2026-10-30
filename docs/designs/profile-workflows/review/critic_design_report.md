# Design Critic report — STAGE 7, design lens

Target: `docs/designs/profile-workflows/design.md`, lines 1–995 only
(branch `design/profile-workflows`, Baton repo).
Sections 15–19 (implementation plan, option sets, acceptance, alternatives,
open questions) sit beyond line 995 and were not reviewed here. Where
sections 1–995 defer a decision to section 16, this report records that
deferral as a gap inside the reviewed range, not as a verdict on section 16
itself.

Method: every load-bearing claim in lines 1–995 was checked against the
code the design cites in the same worktree (`core/models.py`,
`core/pipeline.py`, `core/learning/profiles.py`, `core/cache.py`,
`code_orchestrator/ledger.py`, `quality/gate.py`, `quality/levels.py`,
`quality/scoring.py`, `config/defaults.py`, `dashboard/app.py`) and against
the brainstorm brief `concepts_brief.md`.

## Verdict in one paragraph

The design states its own hard problems plainly, then leaves the hardest
of them undecided or described in a way the existing code does not
support. The "resolved parameters, not a new interpreter" mechanism does
not cover the transitions handlers choose internally. The safety floor
does not close the bypasses it names, because the bypasses live in
configuration and in score handling, not in the profile. The Efficiency
preset cuts checks that are security checks, and its round-cap cuts only
bind on runs that are already failing. The suggestion engine's evidence
base does not exist yet, and the stage-record schema it is given lacks
fields its own example reason lines and thresholds need. The editor
cannot rearrange anything, by the design's own rule. None of this is a
reason to drop the feature. It is a reason not to approve lines 1–995 as
an implementable design: the items marked Blocker below must be settled
in the text before implementation starts.

Counts: **8 Blockers, 12 Majors, 6 Nits.**

## Standard check (feature-spec standard)

- Problem statement: present (§1). Goals and non-goals: present (§3).
- Proposed architecture: present in outline (§6–§14), but the execution
  mechanism (§8) is asserted rather than mapped to handler code — see B1.
- Alternatives: **not verifiable inside lines 1–995.** The alternatives
  section is §18, beyond the reviewed range. Inside the reviewed range,
  the design mentions one rejected shape (restructure `_advance`, §2.1)
  without a documented rejection rationale. Under the standard's
  alternatives rule, the reviewed range alone fails that rule.
- Risks and limits: §4 lists ten failure modes, but several mitigations
  point to option set B, which is beyond the reviewed range, so the
  mitigation is a pointer, not a mitigation, inside lines 1–995 — see B2.

---

## Blockers

### B1. "Resolved parameters" does not cover handler-chosen transitions — §8.1, lines 551–584

Finding: §8.1 says the step lookup consults the snapshot stage list
instead of the hardcoded handler map, handlers keep owning their
outcomes, and "a step not in the snapshot is never entered." That does
not match how the code moves runs. Handlers do not return a next step
for a lookup to filter; they move the run themselves, to hardcoded
targets:

- `_apply_d3_verdict` moves to `DESIGNING` when `plan.needs_design`,
  else to execution, with no snapshot input (`pipeline.py`, D3 verdict
  handling).
- `_integrate` moves back to `EXECUTING` for fix tasks, and
  `_ship_after_score_gate` moves back to `EXECUTING` for score rework,
  regardless of any stage list.
- `_after_ship_vote` moves to `SHIPPING`, `AWAITING_SHIP`, or `DONE`
  from ship mode, not from the snapshot stage list.
- `_advance` itself breaks out of its loop when the current state has
  no handler (`steps.get(run.state)` returns nothing), then calls
  `_finish`. It does not raise, refuse, or reroute.

Consequence, worked through: a profile omits `DESIGNING` with
`designing: never`, a plan still has `needs_design` set, D3 approves,
and the handler moves the run to `DESIGNING`. If `DESIGNING` is no
longer in the lookup, `_advance` breaks and `_finish` runs. The run
stalls in `DESIGNING` with no error, or ends its learning step early,
depending on the learning-state set. The design never names this path.
The same shape applies to every handler loop-back: the snapshot can
remove a stage from a list, but it cannot stop a handler from moving
the run into that stage, because no snapshot check is specified at any
`_move` / `_move_to_step` / `_move_to_execution` call site.

Recommendation: either list every handler move call site and state,
per site, what snapshot check applies there (including what happens
when a handler targets an omitted stage: error, reroute, or refusal at
run start), or drop the claim that the change is narrow and describe
the handler edits as the mechanism. A conformance test (§8.2) cannot
substitute for that list, because it tests outcomes, not the move
sites that produce them.

### B2. The safety floor does not hold against the bypasses it names — §10, lines 669–734, with §2.2, lines 83–115

Finding: §10.1 lists six floor behaviours, and §10.2 refuses three
profile settings (quality `none` with shipping on, `score_skip_human`
with shipping on, omitting `VALIDATING`/`EXECUTING`/`INTEGRATING`).
The four bypasses §2.2 names are not closed by those refusals, because
they do not live in the profile:

1. Missing ship score ships. `ship_gate_outcome` returns `"ship"` when
   the score is `None` (`quality/scoring.py`: "No score (no gate ran)
   or a passing/skipped score ships"). No profile setting controls
   whether a score exists. Floor item 3 ("a ship vote passes through
   the score gate at its threshold") is therefore false whenever the
   gate produced no score, and §10 never specifies fail-closed
   handling for a missing score.
2. `score_skip_human` in configuration marks the score passed
   (`quality/scoring.py`, `score_gate`: `passed=skipped or total >=
   threshold`; `quality/gate.py` reads the config flag). Refusing the
   flag inside a profile leaves the config path open, and §10.2
   explicitly says config bypasses are "not silently rewritten."
3. The plan gate is skipped without `AUTONOMOUS`. `execution_gate_target`
   returns `EXECUTING` directly when the mode is not autonomous and
   neither `plan_only` nor `[approval] require_plan_approval` applies
   (`pipeline.py`). Floor item 5 says the plan passes through the plan
   approval gate "except under `AUTONOMOUS` opt-in." That sentence is
   false for a standard-mode run with plan approval turned off in
   config. The floor statement must include the config case or stop
   claiming the gate is a floor item.
4. A quality-`none` result can still be shipped later by hand. §10.2
   allows quality `none` when shipping is off, on the grounds that
   the run ends at `DONE` unshipped. But the result branch stays, and
   a later manual ship command can open a PR for it (the `stop_ship`
   path in `pipeline.py` notes the same branch-and-ship-later shape).
   The design never addresses the manual ship path, so the
   none-with-shipping-off refusal is bypassed by waiting until after
   the run ends.

On top of that, the enforcement choice itself is deferred: §10.2 says
the mix of refuse vs. label is "decided finally by option set B,"
which is beyond the reviewed range. Inside lines 1–995, a run that
drops a safety behaviour is therefore either refused or labelled,
depending on a decision the reader cannot see. §3 states the goal in
the same disjunctive form ("either refused at save time or visibly
labelled"), so the goal is untestable until that decision lands. A
label in a PR body is also after the fact: by the time the label
renders in a shipped PR (§10.3), the ship has already happened. A
label cannot protect a ship that already occurred.

Recommendation: rewrite §10 as a table with one row per bypass, stating
for each whether the fix is refusal, a code change (for example
fail-closed on a missing score), or label-only, and name the manual
ship path explicitly. Do not call the list a floor while any row is
label-only for a behaviour that ships code.

### B3. The preset table breaks its own intent, and Efficiency's cuts are quality cuts — §9.1, lines 611–645

Finding: presets are absolute values, not floors or deltas, so they
change meaning with the repo they land in:

- Accuracy sets quality `production`. On a repo already configured at
  `scalable`, Accuracy lowers strictness below the default while being
  presented as "fullest checking." On a repo already at `production`,
  Accuracy changes nothing except turning CDA grading on (advisory
  only — see M8).
- Efficiency sets quality `basic`. On a repo configured at `none`,
  Efficiency raises strictness while being presented as cheaper. The
  table never says "at most" or "at least," so the implementer cannot
  tell whether the preset value wins or the repo value wins when they
  conflict. §9.2 precedence says the snapshot beats `jev.toml`, which
  implies the absolute value wins in both directions — including the
  Accuracy-lowers-scalable case.

Separately, `basic` is not a neutral efficiency setting. The level
definitions (`quality/levels.py`) put secret scanning, SAST, and
dependency audit at `medium`, and type checking, coverage, licences,
and the security checklist at `production`. `basic` is lint/format,
the full test suite, and AI code review. Moving `production` to
`basic` removes the secret scan, SAST, and dependency audit from the
gate. The gate still "runs" (§10.1 item 1 says the level may vary), so
the floor is satisfied on paper while the checks that would catch a
leaked secret or a known-bad dependency no longer run. That is a
security cut described as an efficiency setting, and §9.1 never names
which checks are dropped.

The round-cap cuts have a similar problem. `max_plan_rounds` 3→2,
`max_fix_rounds` 2→1, and `score_max_retries` 2→1 save nothing on a
run that succeeds first time, because caps only bind when a run is
already failing or being reworked. On a failing run they save tokens
by giving up earlier and stopping for a human. The design never states
the expected rise in `NEEDS_HUMAN` stops, and never measures it, so
"fewer tokens" is bought with more human interruptions and less
completed work, without saying so.

Recommendation: define presets as floors, caps, or deltas relative to
the repo configuration, with the direction stated per setting (for
example, Accuracy means "at least production," Efficiency means "at
most basic, and only when shipping is off or the floor allows it").
List, per preset, exactly which checks are added or removed, and state
the human-stop cost of each round-cap cut. If Efficiency keeps
`basic`, §10 must say plainly that secret/SAST/dependency checks are
outside the floor.

### B4. The conformance test, as specified, cannot pass with live model decisions — §8.2, lines 585–599

Finding: §8.2 requires the legacy path and the `default` preset path
to produce "identical stage sequences, gate parkings, and outcomes,
including Halt/resume and cancel cases." The pipeline's transitions
are driven by model decisions (D1 intake, D3 verdict, D6 vote) and by
worker output. Two live runs of the same idea will not reliably
produce identical decisions, verdicts, or outcomes. The design never
says the conformance suite stubs, replays, or fixes those decisions.
Without that sentence, the acceptance criterion is unimplementable as
written: either the suite is flaky by construction, or the implementer
must invent a stubbing harness the design never describes.

A second tension sits in the same section: until conformance passes,
picking `default` "is a no-op that records the choice and runs the
legacy path." §3's goal says a user picks a profile "and the run
executes that profile's flow." For the default preset, that goal is
false until an unimplementable test passes. The design should say
which is true during the interim: the default preset is a recorded
preference with legacy execution, and the §3 goal does not apply to
it yet.

Recommendation: specify the conformance harness (fixed decision
fixtures, replayed worker output, or recorded decisions) and the case
list, and state the interim behaviour of the default preset as a
limitation, not as a passing test.

### B5. The new "profile" collides with an existing `profile` parameter, and repo-scope profiles do not travel with the repo — §6, lines 345–422, with §2.7, lines 193–206

Finding: `Pipeline.start` already takes a `profile` argument, and it
already means something else: the workspace/server profile, copied to
`.jev/runs/<id>/profile.json` (`pipeline.py`, `start`). §2.7 says run
creation "already flows through `Pipeline.start` with flags" and the
design "maps profile selection onto that path." The design never says
whether workflow-profile selection reuses that parameter, adds a new
one, or renames one of them. An implementer following §2.7 literally
will collide with the existing parameter, and a reader cannot tell
from §6 whether `profile_id` on the run record is the workflow
profile, the workspace profile, or both.

Separately, §6.2 says "repo-scope profiles travel with the repo."
The repo ignores `.jev/` (`.gitignore` lists `.jev/`), so files under
`<repo>/.jev/workflow-profiles/` do not travel through version
control. They travel only if someone copies the directory by hand or
by another sync mechanism the design never names. Either the storage
path must move out of `.jev/`, or the travel claim must be corrected
to "visible on this machine, in this checkout."

Recommendation: rename one of the two concepts in code-facing text
(and say which), add the new `Pipeline.start` parameter by name, and
correct or re-site the repo-scope storage claim.

### B6. The suggestion engine's evidence base does not exist, and the stage schema cannot support its own examples — §11, lines 735–805, with §12, lines 806–904

Finding: three separate gaps stack here.

1. Goal labels are the engine's training signal (§5, §12.1), but a
   run without a stated goal records `null`, and §12.1 says the
   engine "never uses a run whose goal label is `null` as evidence
   for a goal-conditioned recommendation." Until users label at least
   10 runs per scope with the same goal (§12.3), there is no
   goal-conditioned evidence at all. The design gives no way to set
   or correct a goal after creation, and no statement of how long
   the cold start lasts. Preset-only mode (§12.4) could therefore be
   the permanent state for infrequent users, and the design never
   says so.
2. The stage-record schema (§11.1) lacks fields the §12.2 examples
   need. "VALIDATING caught plan defects in 7 of your last 12 runs"
   needs a per-stage defect-caught flag: neither a D3 revise/escalate
   verdict nor a validator verdict is, by itself, a defect count, and
   no such flag is in the schema. "3 of your last 10 runs needed a
   fix task from failed integration tests" needs a fix-task cause:
   `_d6_action` turns both quality-gate failures and failed
   integration tests into the same `fix_task` action, so the cause
   is not recoverable from the outcome field. "No routing change"
   needs a routing-change flag the ledger does not record. The
   cheaper-model threshold (§12.3) needs the effective model and
   quality level per stage; the schema carries `profile_id`,
   `profile_version`, and `goal`, but per-run flags (§9.2) can
   override the snapshot, so the effective model is not derivable
   from the profile alone.
3. The counterfactual in the §12.2 examples is unobservable.
   "Dropping VALIDATING would save a median 8,100 tokens against 7
   defect escapes" treats "caught by VALIDATING" as "would have
   escaped without VALIDATING." A defect caught at validation might
   have been caught later by the quality gate or by integration
   tests. The design has no control group, no before/after
   comparison, and no rule for attributing an escape. The same
   applies to attributing a quality-gate failure to one model or
   tier (§12.3): the gate runs on a merged diff produced by many
   models across many tasks, so per-model attribution of a gate
   failure is not defined anywhere in the design.

Recommendation: extend the §11.1 schema with the fields §12 actually
consumes (defect-caught flag with its definition, fix-task cause,
effective model and level per stage, routing-change flag), or cut the
§12.2 examples to claims the schema can support. State the
counterfactual rule explicitly: what counts as an escape, and what
evidence would falsify a stage-drop recommendation beyond a raw
count. State the cold-start consequence: how a user with fewer than
10 goal-labelled runs ever reaches measured suggestions.

### B7. The integration test command is a knob in §2.3 and absent in §7.4 — §2.3, lines 116–140, §7.4, lines 493–520, §14, lines 972–995

Finding: §2.3 lists "`[integration]` test command and `max_fix_rounds`"
among "the settings a profile can map onto." §7.4, the authoritative
parameter list, includes round caps but never mentions the test
command. §14 then claims "the executor never runs a command named by
a profile." Those three statements cannot all be true:

- If the test command is a profile parameter, §14 is false: the
  executor runs exactly the command the profile names, via
  integration-test execution. A repo-scope profile file, editable on
  disk and validated on read (§6.2), would then be a way to get a
  command executed by whoever runs the profile. The design's trust
  boundary for repo-scope files (who may author them, who is
  expected to run them) is never stated.
- If the test command is not a profile parameter, §2.3 overstates
  the knob list, and the design should say the command always comes
  from repo configuration.

Recommendation: decide in §7.4, in one sentence, whether the test
command is profile-settable. If it is, §14 must be rewritten with a
command-allowlist or a refusal rule, and §6 must state who may author
a repo-scope profile. If it is not, remove it from §2.3.

### B8. The editor cannot rearrange anything, and the design does not say so where it promises a flowchart editor — §1, lines 13–30, §3, lines 207–245, §7.1, lines 433–451, §13.2, lines 933–959

Finding: §1 promises "a flowchart editor in the dashboard for building
and rearranging those profiles." §3's goal repeats "create, rearrange,
save, and version." §7.1 forbids reordering ("Reordering two stages
is refused"), and §13.2 admits the consequence: "in practice: no
reordering passes." What remains is a chain renderer with omit
toggles (canvas drag-out, list include/omit) plus per-step parameter
fields. That is a settings form with nodes drawn on it, not a
flowchart editor in the sense §1 promises, and the design never
corrects the promise in §1 or §3.

The restriction also rules out flows users are likely to ask for
first: different handling for docs-only changes, extra review for
security-sensitive files, conditional stages beyond the two
hardcoded conditionals (`needs_design`, ship mode). The design never
states that conditional or branching flows are out of scope for the
editor, so a user who opens the editor expecting to draw a branch
will find the validator refusing every attempt without a prior
statement of the limit.

Recommendation: correct §1 and §3 to say the editor builds an ordered
stage list with omission and parameters, not a general flowchart,
and state in §7 that conditional and branching flows are out of
scope for this design. If "rearrange" is to stay as a goal, the
design must name at least one rearrangement that passes validation.

---

## Majors

### M1. Gate policy is underspecified at every enforcement point — §7.2, lines 452–464

`STEP_GATES` has no entry for `INTAKE`, so a gate policy of `require`
on `INTAKE` has no gate state to park in. `EXECUTING` shares
`AWAITING_APPROVAL` with the plan gate, so a gate policy on
`EXECUTING` cannot be distinguished from the plan approval gate in
the run record or in `approve()`. `skip` is defined as applying
"only where the existing rules already allow skipping," which makes
`skip` a restatement of existing behaviour rather than a policy:
if it cannot skip anything that would not already skip, it does
nothing, and if it can, the sentence is false. The ordering against
`plan_only`, `[approval] require_plan_approval`, and autonomy mode is
never resolved — §9.2 says a profile never turns an `AUTONOMOUS` run
into a gated run, but never says what happens when a profile says
`require` and the creation flags say autonomous. Finally, `approve()`
sets the run state directly from its own gate-to-step map; it does
not go through `_move_to_step`. Giving the `_move_to_step` helpers a
snapshot parameter (§8.1) therefore misses the approve path entirely.
Recommendation: define gate policy per step against the actual
`STEP_GATES` table (including the `INTAKE` gap and the shared
`AWAITING_APPROVAL`), state the precedence of gate policy against
each creation flag, and name the approve/resume enforcement points.

### M2. Precedence contradicts itself on autonomy, and two autonomy modes are missing — §9.2, lines 646–668

§9.2 ranks explicit per-run flags first, then the snapshot, then
`jev.toml`/workspace profile, then defaults. It then carves autonomy
out: a profile never turns an `AUTONOMOUS` run into a gated run or
vice versa. Autonomy mode is itself set at creation, so the carve-out
means a creation-time choice beats the snapshot for gates while the
ranking says creation flags beat the snapshot for everything — the
two statements agree for flags but leave the case of a profile
`require` gate combined with an autonomous creation choice
unresolved in either direction. `MANUAL` (run waits in `INTAKE`,
never advanced by `start`) and `MEDIUM` (folder-scoped) modes from
`AutonomyMode` never appear in §7 or §9 at all: what a snapshot means
for a manual run, and how profile scope interacts with a medium
folder boundary, are unstated. Precedence item 3 also lumps "workspace
profile settings" with `jev.toml`, but the workspace profile is the
server profile from B5, not a workflow setting — the ranking mixes
two different "profile" concepts in one line. Recommendation: write
the precedence table as a matrix (setting × source), including
`MANUAL`/`MEDIUM`, and separate workspace profile from workflow
profile.

### M3. Assurance for legacy runs changes behaviour the non-goals forbid, and the PR-body mechanism is missing — §10.3, lines 716–734, with §3, lines 207–245

§10.2 says the assurance computation "applies to every run, profiled
or not," so a legacy run using a config bypass carries the reduced
label. §3's non-goals say the feature does not change "the meaning of
any existing setting for runs that do not pick a profile." Adding a
board badge, a run-page badge, and a PR-body line to legacy runs
changes what those runs display and what their shipped PRs contain.
That may be a defensible choice, but it contradicts the non-goal as
written and expands the feature's scope to every run in the system.
The PR-body mechanism is also unspecified: the ship path pushes a
branch, opens a PR, waits for checks, and merges (`_ship` via
`ship_and_merge`). The design never says where the assurance line is
inserted, whether it survives a draft-to-ready transition, or what
happens if the PR body is edited after creation. Recommendation:
either narrow §10.2 to profiled runs and keep the non-goal, or amend
the non-goal and specify the PR-body insertion point.

### M4. Version storage has a race, and retention contradicts the snapshot — §6.2–§6.3, lines 361–396

A version write is two steps: write `v<n>.json`, then update
`current.json`. Two concurrent edits can both compute `n+1` and one
can overwrite the other, and a crash between the two steps leaves
the pointer behind the files. No locking or atomic rename is
specified, although the codebase already uses file locking elsewhere
for run state. Retention is contradictory: §6.4 stores the full
resolved set on the run record, so execution never needs the old
version file again — yet §6.3 refuses permanent deletion "while any
run references any version." If runs are retained indefinitely, no
profile can ever be permanently deleted. §6.3 defers to "the repo's
existing run-retention behaviour" without naming it; no such policy
is cited anywhere in the reviewed range, so the implementer cannot
tell when, if ever, an archived version may be removed.
Recommendation: specify atomic version creation (lock or
compare-and-swap on `current.json`) and separate audit retention
(keep old files for reading) from execution retention (snapshot
suffices), with a named retention rule.

### M5. Stage recording is underspecified at every boundary — §11.2, lines 769–785

The "wrapper is the only writer" claim does not cover the paths that
do not go through `_advance`'s step call: `approve(execute=False)`
passes a gate without running the step, `cancel` ends a run without
a step exit, and a process death leaves a row without `ended_at`.
None of these closings is specified. Gate-clock stop on
`approve(execute=False)` is undefined: the gate has been passed but
no work has started, so the row's work interval has not begun either.
Ship timing is contradictory: §11.2 says "the ship decision and the
shipping work are recorded as `SHIPPING` stage rows, not folded into
`INTEGRATING`," but the ship decision (D6 vote plus score gate)
executes inside `_integrate`, before any `SHIPPING` state exists.
Splitting that work into a `SHIPPING` row requires either rewriting
`_integrate`'s accounting or accepting that the decision stays in
`INTEGRATING` — the design picks neither. The learning step in
`_finish` is not a work state, so its model call (the learner call
noted in `config/defaults.py`) is excluded from stage totals, which
understates every learning run's cost. Waiting for PR checks during
`SHIPPING` is neither gate waiting nor model work, and is
unclassified. Finally, §11 gives a schema but no store, no migration,
and no retention: table or files, indexes for the §12 queries, and
backfill for pre-existing runs are all left to the implementer, and
the cold-start clock (the 10-run threshold starts counting only after
stage records land) is never stated. Recommendation: specify row
closing for cancel/crash/approve-without-execute, resolve the
ship-decision row assignment, include or explicitly exclude the
learning call, and name the store and migration.

### M6. Aggregation mixes unlike runs and unmapped cache events — §11.3, lines 786–805

Medians are computed across runs with no normalization: a one-task
fix and a twenty-task feature have wildly different stage token
counts, and a median over ten such unlike runs does not estimate the
next run's saving. No grouping by task count, diff size, repo, or
model is specified. The completeness rule is also unenforced by the
thresholds: §11.3 says a stage with any `null` attempt is quoted as
"at least N," but §12.3's thresholds never require `measured: full`,
so a median built from partial rows can still justify a
recommendation. Cache mapping is undefined: cache serves happen per
role, and roles do not map cleanly to stages (the reviewer role serves
during `EXECUTING` task work; the learner serves during `_finish`,
which has no stage row). A cache-served stage reporting "the tokens
of the original entry when known" compares entries that may come from
a different repo, model, or price — the design never says whether
cross-run original costs are comparable. The code already computes a
saved-dollars figure per entry (`cache.py`: entry cost times hit
count); §11.3's token rule never reconciles with that existing
figure, so two "saved" numbers will exist with different meanings.
Recommendation: define the aggregation groups, require a completeness
rule inside the thresholds, map roles to stages (including the
learner), and reconcile or retire the existing saved-dollars figure.

### M7. Thresholds are unjustified numbers, and stale drafts can be approved — §12.3–§12.4, lines 855–891

10 runs, a 90-day window, and 5 runs per model/tier appear without any
basis: no variance argument, no statement of what happens at 9 runs
versus 10, and no handling for users who run less than once a week
(the window expires faster than evidence accumulates, so measured
suggestions may never become available — unstated). Cross-repo
suggestions aggregate medians across repos with different
configurations and models, with only "say so" as the safeguard; no
normalization or prohibition is given. Approval has a staleness hole:
a draft is stored as a suggestion record, and approval saves it "like
any other edit," but nothing re-checks the evidence at approval time.
If new runs arrive between draft and approval, the reason lines may
no longer hold, and the user approves a stale case. §7.5 requires
validation at save and at run start, but that validation covers
schema and stage rules, not evidence freshness. Recommendation:
justify or soften the thresholds (for example, report a confidence
interval alongside the median instead of a hard gate), restrict or
normalize cross-repo aggregation, and require evidence re-check or
an explicit staleness warning at approval.

### M8. Lessons and CDA grades cannot carry the weight §12 puts on them — §12.1–§12.2, lines 808–854

Lessons carry no token or time data (§2.4), yet §12 must combine
them with token medians into one recommendation. No weighting rule
and no conflict rule is given: when a lesson says a role struggled
and the medians say the stage is cheap, the design does not say which
signal wins. CDA grading is off by default, advisory only, and the
0.7 threshold cleared nothing across 13 real diffs (§2.2). In
practice the grade signal is sparse or `unavailable`, and §12's
example ("last 9 graded diffs produced no routing change") misreads
the signal: advisory grades that nothing consults will always show
"no routing change," which proves the grades were ignored, not that
grading is useless. §12.1 also asserts "per-agent and per-tier
aggregates" without mapping them to ledger fields; the ledger's
documented aggregate is the best tier by mean diff quality with a
3-graded-diff floor, and the design never shows a per-agent aggregate
or how it would be computed. Recommendation: define the lesson/median
conflict rule, state the minimum grade volume below which CDA-based
reason lines are suppressed (not just the tier floor), and map every
§12.1 input to a concrete ledger or lesson field.

### M9. Efficiency's "cheapest model" and free-training policy are undefined and unpriced — §9.1, lines 611–645, with §2.6, lines 178–192

"Cheapest configured model per role where the repo has one
configured" has no price source. Tokens sit in attempt JSON or are
`None`, Codex reports no cost (§2.6), and no configured price table
is named in §7.4. If a role has a single configured model, the
Efficiency model dimension silently equals the default while the
preset is still labelled Efficiency — the user is promised savings
the preset cannot deliver on successful runs (see B3). The planner
policy `free_training_ok` means free providers may train on prompts
(the `config/defaults.py` comment states this, with a pointer to the
provider privacy setting). The preset table adopts that policy with
no consent surface, no warning, and no mention in the picker
description (§13.1 says the picker shows goal and assurance, not
training implications). Free-model rate limits (documented in the
same defaults comment as roughly 20 requests/minute and 50/day)
are also unaddressed: an Efficiency preset that routes planner work
to free models can hit those limits and slow or fail runs, and the
design never says what happens then. Recommendation: name the price
source (or drop "cheapest" to "the repo's configured low-cost list,
if any"), state the fallback when no cheaper model exists, surface
the training implication wherever Efficiency is picked, and state
the rate-limit failure behaviour.

### M10. Dashboard routes and validation feedback are not implementable from this range — §13, lines 905–971

- `/runs/new` is listed in §2.7 as a POST route only (`dashboard/app.py`
  confirms POST). The GET form the picker must extend is never
  identified, so the implementer must discover the actual new-run
  form route before the §13.1 picker can be built.
- Route names depend on option set A, beyond the reviewed range:
  §13.1 proposes `/workflows` while noting it becomes `/flows` if
  the human picks that name. Nothing in lines 1–995 can be built
  against a route name that may change.
- The base profile for a run-creation suggestion is undefined: §13.3
  highlights changes "against the profile it was suggested from,"
  but a suggestion requested at run creation has no base profile
  except the implicit default, which §8.2 says is a legacy no-op
  until conformance passes.
- "Lists every error, not just the first" (§13.2) has no aggregation
  format, and live validation "where cheap" has no scope: which
  checks run live, and which wait for save, is left open, so the
  behaviour is untestable.
- Omitted-stage parameters are unspecified: if a stage is omitted
  but its parameter fields still carry values (gate policy, round
  caps), the design never says whether they are kept, cleared, or
  validated. A file-edited profile can therefore carry a gate policy
  for a stage that does not exist, and §7.5's "gate without a stage"
  message does not say whether that is a refusal or a silent drop.
- Profile ids contain a colon (`<scope>:<slug>`, §6.1). The
  `/workflows/{id}` route never addresses URL encoding of that
  colon. Recommendation: identify the GET new-run route, pin the
  route names or mark §13 as draft pending option set A, define the
  suggestion base case, specify the multi-error format and the live
  check set, and state the omitted-parameter rule.

### M11. Omitting DESIGNING and SHIPPING does not remove their work cleanly — §7.3, lines 465–492

Skipping `DESIGNING` for UI work removes an input the rest of the
pipeline currently receives: `_design` stores a design document on
the run, and task work follows it. The design never says what coders
receive instead when design is skipped, or whether any task prompt
assumes a design document exists. That is a behaviour change to
`EXECUTING`, not just an omission from a list. For `SHIPPING`, the
omission is via ship mode `off`, but the D6 ship vote and the score
gate still run inside `_integrate` before the run ends at `DONE`.
The stage list therefore does not equal the work performed: a
profile that "omits SHIPPING" still pays for the ship decision.
Recommendation: state the no-design fallback for task prompts, and
either record the ship-decision work under its true stage or say
plainly that omitting `SHIPPING` omits only the push/merge work.

### M12. Naming disambiguation stops at the list page — §5–§6, lines 309–422, with §13.1, lines 907–932

§6.1 promises scope badges on lists, and §13.1 repeats the badge for
the list page. The run-creation picker (§13.1) is specified to show
goal label and assurance, but not the scope badge or the full id —
exactly the place where two profiles sharing a display name in
different scopes would be confused. The slug rule is asserted to
follow "the existing profile-name rule" without showing that the
learning-profile name check and the workflow slug rule are the same
rule; a reader cannot verify the claim from the design. The CLI is
out of scope (§3), so no disambiguation rule exists there either,
which is acceptable only if the design says the CLI must not accept
a bare display name later — it does not. Recommendation: require
scope badge plus id on the picker, cite the exact name-check
function the slug rule reuses, and record the bare-name prohibition
for any future CLI.

---

## Nits

### N1. The 64 KB cap has no basis — §14, lines 972–995

A per-version file cap of 64 KB is stated without a reason. Profile
JSON is small, so the cap is unlikely to bind — but an unjustified
constant in a security section reads as a real limit, and a future
profile format (for example, with embedded reason records) could hit
it. Either justify the number or state it as a placeholder constant
for implementation to confirm.

### N2. The run-start refusal message contradicts archiving — §6.1, lines 347–360, with §6.3, lines 379–396

§6.1 says a run referencing an id that "no longer resolves" refuses
with "flow <id> version <n> is archived or deleted." But §6.3 says
archived definitions stay on disk and existing snapshots keep
running, so an archived version does resolve. The message should
distinguish the two cases: deleted (refuse) versus archived (still
runnable from snapshot, not pickable for new runs). As written, the
message tells the user an archived flow is gone when it is not.

### N3. The reordering message hardcodes one pair — §7.5, lines 521–550, with §7.1, lines 433–451

§7.1 promises the validator names "the pair and the dependency" for
any reordering. §7.5's catalogue message names only the
EXECUTING/VALIDATING pair. Either the catalogue needs a general
message template with pair substitution, or §7.1 should say the
message is templated. Minor, but the catalogue is presented as the
exact message set, so the gap will be found by the first test that
reorders a different pair.

### N4. "Headline result" is undefined for multi-attempt stages — §11.1, lines 744–768

`cache_served` is defined as "the stage's headline result came from
cache." A stage with several attempts (retries, fix tasks) has no
single headline result defined anywhere in the design. State whether
`cache_served` means any attempt, the final attempt, or the attempt
that produced the stage's output.

### N5. The floor list never mentions INTAKE, PLANNING, or DESIGNING — §10.1, lines 671–692

§7.3 says omitting `DESIGNING` never reduces assurance, but §10.1's
floor list does not mention `DESIGNING` at all, and neither
`INTAKE` nor `PLANNING` appears in the floor list either. A reader
cannot tell from §10 alone whether those stages are floor items,
non-floor items, or simply unconsidered. One sentence per stage in
§10.1 would close this.

### N6. Suggestion visibility wording contradicts itself — §12.4, lines 876–891, with §13.1, lines 907–932

§12.4 says a draft "appears nowhere as a runnable flow until
approved," while §13.1 puts suggestion records on the profile detail
page. Both can be true (visible but not runnable), but the §12.4
wording ("appears nowhere") overstates the hiding. Reword to
"appears nowhere as a runnable choice" to match §13.1.

---

## What holds up (brief, after the findings)

- §2 states the pipeline shape, the bypasses, the knob list, and the
  absence of per-stage measurement from code, with file references
  a reader can check. The bypass list in §2.2 is the reason B2 can
  be stated precisely.
- §6.4's snapshot rule (executor reads only the snapshot) is the
  correct answer to the profile-edit-mid-run failure mode in §4,
  item 6, as far as it goes; B1 and M4 concern what the snapshot
  must contain and how versions are written, not the snapshot idea.
- §11.3's partial-vs-full rule and "at least N" quoting rule, and
  §12.5's prohibitions (no silent application, no shadow data
  presented as serving data, no token number without sample size),
  are the right constraints for the suggestion engine. B6 and M6
  concern the missing fields and thresholds underneath those
  constraints, not the constraints themselves.
- §13.2's list mode as a first-class path with the same validator
  answers the accessibility failure mode in §4, item 8, at the
  level of mechanism; M10 concerns the routes and formats around it.

## Items for resolution (in priority order)

1. B1: map every handler move site to its snapshot check, or
   redescribe the mechanism.
2. B2: close or explicitly label each bypass, including the missing
   score, the config flags, the plan-approval-off case, and the
   manual ship path; settle refuse vs. label per behaviour class.
3. B3: restate presets as floors/deltas and name the checks each
   preset drops, including the human-stop cost of round-cap cuts.
4. B5 + B7: disambiguate the two "profile" concepts and decide the
   test-command question.
5. B6 + M5 + M6: extend the stage schema to what §12 consumes, name
   the store and migration, and state the cold-start consequence.
6. B8: correct the editor promise in §1 and §3 to match §7.1.
7. B4: specify the conformance harness or soften the criterion.
8. M1 + M2: complete the gate-policy and precedence matrices,
   including `MANUAL`/`MEDIUM`.
