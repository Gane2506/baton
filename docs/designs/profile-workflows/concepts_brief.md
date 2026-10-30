# Profile-based process flows (Baton issue #368)

* **Author:** Elephant (EGM design phase)
* **Date:** 2026-10-30

## Overview

Baton turns a natural-language idea into reviewed, tested code through a
pipeline of AI agents. Today that pipeline is fixed: one chain of stages,
hardcoded in code, identical on every run. A user who wants a cheaper run
cannot ask for a shorter flow, a user who wants a stricter run cannot ask
for a fuller one, and nothing suggests a flow from how that user actually
works.

This feature adds three things, in this order of risk: named, versioned
workflow profiles a run can pick; a flowchart editor in the dashboard for
building and rearranging those profiles; and flow suggestions drawn from
the user's own usage history, conditioned on whether their goal is saving
tokens or getting the most careful result. Today's pipeline stays the
default and stays unchanged for existing runs. A profile is opt-in per
run, never a silent change to the default.

Two corrections to the issue's starting picture, established by reading
the code: the pipeline is not 7 clean boxes. `RunState` carries 19
values — 7 work states handled by the `_advance` loop, 6 `AWAITING_*`
gate states plus `AWAITING_APPROVAL`, conditional `DESIGNING` and
`SHIPPING` branches, bounded revise/fix/score-rework loops inside the
step handlers, and 5 stopped states. And the word "profile" is already
taken three times: learning profiles (with a `/profiles` dashboard
page), workspace/server profiles passed to `Pipeline.start`, and
`QueueItem.profile`. The new concept needs a name that does not collide
before anything is built.

## Analysis

Six Goldfish lenses reviewed the hypothesis against the code. Their
findings, deduplicated, are:

**The editor misrepresents the machine.** Handlers choose the next state
internally (`_validate` and its D3 verdict, `_integrate` and its D6 vote,
the score gate after a ship vote), so there is no transition table for a
flowchart to execute. A node/edge editor either hides the loops, gates,
and conditional branches — in which case flows run differently from what
was drawn — or it grows into a small programming language with its own
interpreter, maintained forever alongside `_advance`. Every lens
converged on the same consequence: the flowchart editor is the most
expensive and least faithful part of the proposal, and the design must
either phase it after presets, a picker, and measurement, or prove why
not.

**Presets mostly rebundle knobs that already exist.** Autonomy modes
(`standard`, `autonomous`, `ask_every_step`), quality levels, planner
policy, cache settings, budget and round caps, ship mode, and the CDA
toggle already cover most of what the Efficiency and Accuracy presets
promise. That is not a reason to drop presets — it is a reason the
design must contain an explicit preset-to-setting mapping table and a
precedence order, or a preset will silently lose to a `jev.toml` value,
a per-run flag, or an autonomy mode, and the user will not know which
one won.

**The safety floor is scattered behaviour, not nodes.** The quality gate
lives inside `_integrate`, the ship gate lives in `after_approval` and
`AWAITING_SHIP`, approval lives in `STEP_GATES` and `AutonomyMode`.
Checking that a node is present proves nothing about those behaviours.
Worse, the code already contains bypasses the hypothesis calls
non-removable: quality level `none`, `AUTONOMOUS` skipping the plan
gate, `score_skip_human`, and a missing ship score that still ships.
The design must list the floor as enforced behaviours, stage by stage,
and either close those bypasses or label them too — a label that
applies only to new profiles while old configurations bypass silently
is misleading, not protective.

**There is no per-stage measurement today.** Totals live on the run
record; tokens sit inside attempt `result_json` or decision `details`
JSON or are `None` (Codex reports no cost); cache hits have no token
rule; gate waiting time is mixed into wall time. No suggestion can
honestly claim "dropping this stage saves roughly X tokens" until
recording points exist at stage entry/exit, stage-tagged decisions and
attempts, and gate/ship timing. Measurement is a prerequisite phase,
not a detail to fill in during the suggestion work.

**Suggestion inputs are thin or off by default.** Learning is off by
default; the CDA grader is off by default and backend-less on the Mac
(`unavailable`, no quality fields); semantic-cache shadow data measures
answer agreement, not whether skipping a stage is safe; no record
carries a goal label; there are no counterfactuals for short flows.
Suggestions must therefore start as presets plus explanations, with
stated minimum-data thresholds before any usage-based recommendation
may appear, and the engine must produce drafts plus explanations only
— never write a profile, never change a run.

**Runs must snapshot profiles, never reference them live.** A run that
points at a live profile changes behaviour when the profile is edited,
and the `resume_state` mechanism makes this non-optional: a resumed run
must resume under the flow it started with. Edit means a new version;
delete means archive; identity is a stable id, never a name.

**Boundaries keep the design maintainable.** The executor sees resolved
stage parameters, never profile names. The profile interpreter sees
graphs, never workers. The suggestion engine sees history, never the
executor. One validator is shared by the editor and the executor, so a
flow that saves cleanly cannot fail differently at run time.

**What "performance" means is unsettled.** The issue glosses Accuracy
as "performance", which overlaps Efficiency's token and time saving.
The design must pin down whether Accuracy means output quality (defects
caught, review depth) as opposed to speed, because the two goals
prescribe opposite flows and the suggestion engine cannot condition on
a goal it cannot define.

## Goals vs. Non-Goals

* **In-Scope Goals:**
  * A named, versioned workflow profile per repo or per user (scope
    rule decided in the draft); a run picks a profile at creation from
    the browser, the CLI, the queue, and chat creation paths.
  * A flowchart editor in the dashboard (phased after presets, picker,
    and measurement unless the human approval gate decides otherwise),
    with a shared editor/executor validator and plain-language refusal
    for invalid graphs.
  * Two built-in presets, Efficiency and Accuracy, defined as explicit
    setting tables over existing knobs, with a stated precedence order.
  * A suggestion engine that reads run history, lessons, CDA grades,
    and cache behaviour, explains its reasoning, and only ever proposes
    a flow the user approves before it runs.
  * A safety floor defined as enforced behaviours, with a visible
    reduced-assurance label wherever it applies, on the run page and
    on the shipped PR.
  * The default flow unchanged for existing runs, proven by a
    conformance test, not by assertion.
* **Out-of-Scope Non-Goals:**
  * Changing live semantic serving: it stays off until shadow data
    proves it safe; suggestions may read shadow data but must not
    present it as serving evidence.
  * Silent auto-rearrangement of any run, or a suggestion engine with
    authority over Jev's D1/D2/D3/D6 decisions or the learning loop.
  * A second orchestrator: the suggestion engine advises, the pipeline
    decides.
  * Replacing the existing learning-profiles or workspace-profile
    concepts; the new concept gets its own name and its own page.
  * A terminal-first or CLI-only flow editor; the editor is
    browser-first, with a non-drag (list-based) way to build the same
    graph for narrow screens and mouse-free use.

## Key Decisions & Staging for Next Steps

The following are carried into the draft as option sets for the human
approval gate (none ranked, none pre-picked), except where noted as
decided because only one answer survives the code:

* **Naming.** The new concept's name and route (a "flow" vocabulary is
  the leading candidate from the structural lens; the draft presents
  the options).
* **Safety floor shape.** Non-removable behaviours vs. reduced-assurance
  label vs. a mix, stage by stage — including how existing bypasses
  (quality `none`, `AUTONOMOUS` gate skip, `score_skip_human`, missing
  ship score) are treated.
* **Phasing.** Presets + picker + measurement first, editor and
  suggestions later — versus building the editor in the same phase.
  The lenses unanimously favour phasing; the gate decides.
* **Profile expressiveness.** Stage order only, or also conditions,
  loop bounds, gate modes, ship mode, and `plan_only` — and what is
  explicitly outside a profile's reach.
* **Execution mechanism.** Whether the default stays on the existing
  `_advance` path with a conformance test, or is re-expressed as a
  profile — and who owns the profile interpreter.
* **Precedence order.** Workflow profile vs. workspace profile vs.
  `jev.toml` vs. per-run flags vs. autonomy mode, stated as a full
  order.
* **Scope and storage.** Per repo, per user, or both with a precedence
  rule; where the definition lives; retention and archive rules.
* **Suggestion thresholds.** Minimum runs, graded diffs, cache
  observations, and freshness window before a usage-based
  recommendation may appear; what a preset does below the threshold.
* **Measurement store.** New stage records vs. derivation from attempt
  records (decided in the draft as a prerequisite phase either way:
  suggestions that change a run stay disabled or labelled "preset
  only" until measurement exists).
* **Decided, not optional.** Runs snapshot an immutable profile version
  at creation; the suggestion engine never writes definitions or runs;
  the default flow's behaviour is proven by conformance test; live
  semantic serving stays off; an unapproved suggestion never changes a
  run.

## Build order carried into the draft

1. Safety-floor list, allowed profile knobs, and the graph-to-handler
   contract.
2. Per-stage measurement.
3. Profile storage, versioning, and selection on every run-creation
   path (dashboard, CLI, queue, recurring, chat).
4. Graph engine plus the shared validator.
5. Flowchart editor.
6. Presets as data.
7. Suggestions last.
