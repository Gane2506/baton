# Comprehension review — Profile-based process flows (Baton issue #368)

Lens: COMPREHENSION. Mode: BRAINSTORM.
Reader position: a fresh reader with no prior conversation about this feature.
Grounding read before writing: `src/jev_agent/core/models.py` (`RunState`,
`AutonomyMode`), `src/jev_agent/core/pipeline.py` (the `_advance` loop,
`STEP_GATES`), `src/jev_agent/core/learning/profiles.py` (the existing
learning-profiles store), and `src/jev_agent/core/cache.py` (exact cache and
semantic similarity, default OFF).

This report does not resolve any gap it lists. Its job is to expose what a
fresh reader cannot follow, cannot pin down, or finds inconsistent.

## 1. The starting picture does not match the code a fresh reader is sent to

Severity: Blocker. Dimension: UX and developer ergonomics.
Usability friction: the proposal describes the pipeline as one fixed chain:
INTAKE -> PLANNING -> VALIDATING -> human approval gate -> EXECUTING ->
INTEGRATING -> DONE, with 7 work states mapped to handler methods. The code
does not read that way.

- `_advance` in `pipeline.py` maps 7 work states to handlers, but they are
  INTAKE, PLANNING, VALIDATING, DESIGNING, EXECUTING, INTEGRATING, SHIPPING —
  not the proposal's list. DESIGNING and SHIPPING are missing from the
  proposal's chain, and DONE is a stopping state, not a handler.
- The real pipeline loops: VALIDATING can send a plan back to PLANNING for
  revision, and INTEGRATING can send work back to EXECUTING with a fix task.
  A straight-line chain cannot show those loops, and the proposal never says
  whether a rearranged flow may keep, remove, or change them.
- The real pipeline branches on conditions the proposal never mentions: a
  design step runs only when the plan says it needs one (`Plan.needs_design`);
  shipping runs only in some ship modes, and otherwise a run can end at DONE
  without shipping, or wait at AWAITING_SHIP.
- The real pipeline has waiting states and stopping states the proposal never
  places: AWAITING_APPROVAL, AWAITING_PLANNING, AWAITING_VALIDATING,
  AWAITING_DESIGNING, AWAITING_INTEGRATING, AWAITING_SHIPPING, AWAITING_SHIP,
  plus NEEDS_HUMAN, FAILED, BUDGET_EXCEEDED, and CANCELLED. A run that stops
  in NEEDS_HUMAN can be resumed. None of these appear in the proposal's chain.
- Autonomy modes (`AutonomyMode`: STANDARD, AUTONOMOUS, ASK_EVERY_STEP,
  MANUAL, MEDIUM) already change which approval gates a run waits at, and
  `STEP_GATES` already maps work states to waiting states. The proposal never
  says whether a workflow profile replaces, sits on top of, or is ignored
  alongside an autonomy mode. If a profile drops an approval gate but the run
  is in ASK_EVERY_STEP, which one wins? A fresh reader cannot tell.

Ergonomic requirement: the design must state, in one place, the full set of
things a flow can contain — work steps, waiting states, loops, branches,
stopping states, and the learning step that runs after a run ends — and say
which of those the editor shows, hides, or forbids changing. It must also
state how a profile interacts with `AutonomyMode`.

## 2. Terms that are ambiguous or overloaded

For each term: what it could mean, and what the design must pin down.

### "Profile"

Could mean at least three different things already in this codebase, plus the
new one:

1. The proposed workflow profile: a saved flowchart of pipeline stages.
2. A learning profile: a named set of lessons in the learning database
   (`core/learning/profiles.py`: create, list, enable, flush, delete; names
   limited to letters, digits, `-` and `_`, starting with a letter or digit,
   at most 40 characters). A `/profiles` dashboard page already exists for
   these.
3. A workspace/server profile: `Pipeline.start` already takes a `profile`
   argument, copied to `.jev/runs/<id>/profile.json` so a resumed run reaches
   the same servers.
4. Possibly a user profile, in the ordinary sense, since the proposal says
   "per repo or per user".

What must be pinned down: the exact name shown in the dashboard for the new
kind of profile, so it cannot be confused with the other two; whether the new
profiles share the learning-profile name rules, namespace, or storage; what
page or route the editor lives on, given `/profiles` is taken; and whether
"per repo or per user" means two scopes, one scope chosen per profile, or
both at once — and if both, which one wins when a repo profile and a user
profile disagree, and what happens when a repo profile is used in a different
repo.

### "Flow" / "flowchart" / "rearranged flow"

Could mean: the order of stages; the set of stages included; the edges
between stages; or all of those together. It is unclear whether "rearrange"
means only reordering, or also removing, adding, duplicating, or skipping
stages. It is unclear whether a user can add a stage that does not exist in
the code, draw the same stage twice, or draw a loop that never reaches DONE.

What must be pinned down: the exact operations the editor allows (reorder,
remove, add, duplicate, loop), and the exact meaning of an edge — see
"transition" below.

### "Stage" / "node"

Could mean: a `RunState` value; a handler in the `_advance` map; a piece of
work such as planning or integrating; or a waiting state such as
AWAITING_APPROVAL. In the code these are different things: PLANNING is work,
AWAITING_PLANNING is waiting, and the quality gate and score gate are checks
inside INTEGRATING, not states at all.

What must be pinned down: the complete list of nodes the editor offers, and
for each one, which code state, handler, or check it maps to — including
whether DESIGNING, SHIPPING, the learning step, and the stopping states are
nodes, and whether a node can stand for something that is not a `RunState`.

### "Transition" / "edge"

Could mean: "always go here next"; "go here when this decision comes out
this way"; or "a person must approve before going here". The real pipeline
is full of the second kind: after validation, D3 can approve, send back for
revision, or escalate to a human; after integration, D6 can ship, add a fix
task, or stop for a human; after a ship vote, the score gate can ship, send
back for rework, or stop. A single edge between two nodes cannot express
those choices.

What must be pinned down: whether edges are unconditional, conditional on a
decision, or approval-gated; how the editor shows the existing
approve/revise/escalate and ship/fix/stop branches; and what happens to
those branches when a user deletes or reorders the nodes around them.

### "Goal"

Could mean: the preset a profile is built from (Efficiency or Accuracy); the
user's aim for one particular run; or the aim a suggestion is conditioned
on. It is unclear when a goal is chosen (when the profile is made, when the
run starts, or both), whether a run can have a different goal from its
profile, and whether changing the goal changes the profile or only the run.

What must be pinned down: where a goal lives, when it is set, whether it can
change mid-run, and what happens when a profile's goal and a run's goal
disagree.

### "Performance" (in "Accuracy (performance)")

Could mean: output quality (correct, well-tested code); speed (less time per
run); throughput (more runs finished); or all of these. The proposal glosses
the Accuracy preset as "performance", while the Efficiency preset already
covers saving tokens, and saving tokens usually also saves time. A fresh
reader cannot tell whether Accuracy is meant to be slower and better,
faster and better, or simply the full flow under a second name.

What must be pinned down: one plain definition of what each preset optimises
for, stated in the same units (tokens, time, quality, or some combination),
and whether Accuracy is different from the default flow at all — see
section 3.

### "Suggestion" / "suggested flow"

Could mean: a whole new flowchart offered for approval; a small change to an
existing profile (for example, dropping one stage); a change to a setting
inside a stage (for example, a cheaper model or a different rework cap); or
a warning that the current flow is a poor fit. The proposal uses it for all
of these at once: "suggested flows from the user's own usage history,
conditioned on their goal", built from per-stage outcomes, costs, learning
history, CDA ratings, and cache behaviour.

What must be pinned down: what form a suggestion takes; what data each kind
of suggestion rests on; how the reasoning is shown; where approval happens
(in the editor, at run creation, or both); whether one approval covers one
run or all future runs; and whether an approved suggestion changes the saved
profile, only the next run, or both. "An unapproved suggestion never changes
a run" is stated, but the scope of an approved one is not.

### "Safety stage" / "safety floor" / "quality gate" / "ship gate" / "human approval"

These are used as if they were one list, but in the code they are different
kinds of thing:

- The quality gate is a check inside INTEGRATING, not a stage.
- The ship score gate is a threshold check after a D6 ship vote, also inside
  the integration path, not a stage.
- Human approval is a waiting state (AWAITING_APPROVAL, AWAITING_SHIP, and
  the ASK_EVERY_STEP waiting states), not work.
- Validation is a work stage (VALIDATING) plus two decisions (D2 picks a
  validator, D3 rules on the verdict).
- The intake clarity check (D1) and the validator's "different vendor"
  preference are also checks that could be called safety, but the proposal
  never says whether they count.

What must be pinned down: the exact list of things that count as safety, in
code terms (which states, which decisions, which gates, which thresholds);
whether dropping VALIDATING, dropping only the different-vendor preference,
and dropping only the D3 verdict are three different acts with three
different consequences; and whether the safety floor is enforced by refusing
to save, refusing to run, or labelling — see section 3, where the proposal
offers two answers at once.

### "Versioned"

Could mean: every save creates a new numbered version and old versions stay
runnable; a version number is shown but only the latest version runs; or a
profile carries a version field that is bumped by hand. It is unclear what
increments a version, whether versions are immutable, whether an old version
can be restored, and what happens to runs already using a profile when a
new version is saved, or when a profile is deleted.

What must be pinned down: the versioning rule, the effect of editing or
deleting a profile on runs already started or waiting at a gate, and what
"today's pipeline stays the default, unchanged for existing runs" means —
unchanged behaviour for users, or frozen flow for runs already in progress.

### "Default"

Could mean: the flow used when no profile is chosen; the flow existing runs
keep; or the starting point a custom profile is edited from. The proposal
says the default stays unchanged, and separately says Accuracy is the "full
flow". A fresh reader cannot tell whether Accuracy and the default are the
same flow, whether Accuracy adds something the default lacks (for example,
a different-vendor validator, which in the code is only a preference, not a
guarantee), or whether the default is itself a profile that can be edited.

What must be pinned down: whether the default is a profile in the same
system or a hardcoded fallback; whether it can be edited, renamed, or
deleted; and how Accuracy differs from it, if at all.

### "Cheaper models where history says they are safe"

Could mean: the profile stores a model choice per stage; the dispatcher
still picks models and the profile only constrains it; or the suggestion
engine proposes a model change the user approves. In the code, model choice
happens through role assignments and dispatcher decisions (D1, D2, and the
other decision points), not through the pipeline stages. "Safe" is never
defined: safe by what measure — no failed runs, a CDA rating above some
bar, a quality score above some bar, or simply "worked before"?

What must be pinned down: who picks the model (profile, dispatcher, or
suggestion), at what granularity (per stage, per role, per run), what
"safe" means as a measurable rule, and how much history is enough before a
cheaper model counts as safe — especially when per-repo history is admitted
to be thin.

### "Aggressive cache reuse"

Could mean: reusing more exact-cache hits; lengthening cache lifetimes;
turning on semantic serving; or all three. `cache.py` states the current
rules plainly: coder and learner roles are never cached; only successful
role results are stored; decisions are stored only from clean primary
answers; semantic similarity is default OFF, with a shadow mode that
measures agreement without serving, and live serving stays off until shadow
data proves it safe. "Aggressive" does not say which of those rules change,
and if it means turning semantic serving on, it contradicts the constraint
in the same proposal that live semantic serving stays OFF.

What must be pinned down: exactly which cache settings the Efficiency preset
changes, within the stated constraint that live semantic serving stays OFF;
and what "cache behaviour" in a suggestion can legitimately recommend if
semantic data is shadow-only — for example, can shadow data recommend
enabling serving, or only describe what serving would have done?

### "Fewer rework rounds"

Could mean: fewer fix tasks after integration (today capped by
`max_fix_rounds`, default 2); fewer plan revision rounds (today capped by
`max_plan_rounds`); fewer score-gate rework rounds (today capped by
`score_max_retries`); or fewer loops in general. These are caps on loops,
not stages, so "fewer rework rounds" is not the same kind of change as
"shorter flow". It is also unclear whether fewer rounds saves tokens or
costs quality: a run that stops sooner for a human has used fewer tokens
but has not finished the work.

What must be pinned down: which cap or caps each preset changes, by how
much, and what happens when the cap is hit (the run stops in NEEDS_HUMAN —
is that outcome shown to the user as part of the preset's bargain?).

### "Per-stage outcomes and costs" / "real measurement"

Could mean: tokens and time per `RunState`; per role or per worker attempt;
per task inside EXECUTING (which contains many tasks); or per decision
point. Worker results already carry an optional cost and token count, and
decisions carry cost and latency, but the proposal does not say what is
recorded per stage today, what is missing, or what counts as a stage for
measurement when a stage loops (does a revised plan count once or twice?).
It is also unclear whether human waiting time at an approval gate counts as
stage time, whether a cache hit counts as zero cost or as the original
cost, and whether failed or retried attempts count.

What must be pinned down: the unit of measurement (stage, role, task,
attempt), what is included and excluded (waiting time, retries, cache
hits), and where the numbers are stored so a suggestion can read them.

### "What struggled and what worked (learning loop)" and "CDA ratings per agent/tier"

A fresh reader is given no definition of either. "Struggled" could mean
failed tasks, repeated fix rounds, escalations to a human, low quality
scores, or low CDA ratings. "Tier" is never defined: tier of what — model,
agent, quality level? CDA grading is named as part of the Accuracy preset,
but the grounding files do not define its rating scale, what it grades
(a diff, an agent, a run), or how a rating maps to a flow suggestion.

What must be pinned down: the definition of "struggled" and "worked" in
terms of stored records; the meaning of "tier"; the CDA rating scale and
what it is attached to; and the rule that turns those records into a
suggestion, rather than a narrative summary of them.

### "Invalid graph" and "plain-language refusal"

Could mean: a graph with no path to DONE; a disconnected node; a cycle with
no exit; a missing INTAKE; a node the code cannot run; or a graph that
drops a safety stage. "Plain-language refusal, not a crash" does not say
when the refusal happens (at save time, at run time, or both), what words
the user sees, whether a partially valid graph can be saved as a draft, or
whether the refusal blocks saving or only blocks running.

What must be pinned down: the list of invalid shapes, the moment of refusal,
the exact user-facing wording pattern, and whether invalid graphs can be
saved but not run.

### "Entirely from the browser" / "zero terminal use"

Could mean: creating, rearranging, saving, and running a custom flow without
a terminal — which the acceptance section states. It does not say whether
CLI runs can use profiles at all, whether a CLI run ignores profiles,
defaults to the default flow, or errors, or whether a profile can be
changed for a run that is already waiting at a gate. It also does not say
what happens on the dashboard when a profile a run is using is edited
elsewhere.

What must be pinned down: the CLI's behaviour with profiles, and whether a
profile is fixed at run creation or can change while a run is in flight.

## 3. Contradictions and gaps a fresh reader cannot fill

Severity: Blocker. Dimension: UX and developer ergonomics.

1. **The safety floor is two different designs.** The constraints say either
   some stages are non-removable, or a flow that drops validation ships with
   a visible reduced-assurance label. The acceptance section says "refused
   or visibly labelled". A reader, and a tester, cannot tell which behaviour
   to expect, for which stages, or whether a labelled flow is allowed to run
   at all. The design must choose, and must list which stages each treatment
   applies to.
2. **"Shorter flow" and "non-removable safety stages" pull in opposite
   directions without a worked example.** If validation, the quality gate,
   the ship gate, and human approval all cannot be removed, what is left to
   shorten in the Efficiency preset? The proposal never names a stage that
   the Efficiency preset actually removes. A fresh reader cannot picture
   the shorter flow.
3. **Accuracy may be the default under a second name.** Accuracy is described
   as "full flow, plan validated by a different vendor's agent, the quality
   gate, CDA grading, human approval gates kept". The default already
   includes validation, the quality gate, and approval gates, and the
   different-vendor validator is a configurable preference in the code, not
   a guaranteed step. The proposal never states the difference between
   Accuracy and the default, so a reader cannot tell whether Accuracy adds,
   restates, or renames the default.
4. **"Performance" contradicts the Accuracy/Efficiency split.** If Accuracy
   means performance in the usual sense of speed, then it overlaps
   Efficiency, which is the token-saving, shorter preset. If it means output
   quality, the word is wrong. Either way the two presets' tradeoff is
   unstated: what does each preset give up to gain what?
5. **Cache: "aggressive reuse" contradicts "semantic serving stays OFF".**
   The only cache expansion beyond exact hits is semantic serving, which
   the proposal itself forbids turning on. The design must say what
   aggressive reuse means without semantic serving, or admit that it means
   nothing more than the exact cache already provides.
6. **Suggestions from thin history contradict suggestions from history.**
   The proposal says early per-repo history is thin, so suggestions start as
   presets plus explanations — and also that suggestions are conditioned on
   per-stage outcomes, costs, learning history, CDA ratings, and cache
   behaviour. It never says how much history is enough to move from preset
   to history-based suggestion, who measures that, or what an explanation
   says when there is no history behind it. A suggestion with no data behind
   it is a preset wearing a suggestion's clothes, and the proposal does not
   say how the user is told the difference.
7. **Approval scope is unstated.** "The user approves a suggested flow
   before it ever runs — no silent auto-rearrangement" is clear about one
   run. It says nothing about the next run: does approval stick to the
   profile, to the repo, or to the single run? If approval sticks, later
   runs are rearranged without a fresh approval, which is the silent
   rearrangement the constraint forbids. If it does not stick, the user is
   asked again every time, which the proposal never acknowledges as a cost.
8. **Approval gates are described three incompatible ways.** In the chain
   they are a single gate between VALIDATING and EXECUTING. In the Accuracy
   preset they are plural gates that are "kept". In the safety floor they
   are items that must not be "silently bypassable". In the code they are
   states (AWAITING_APPROVAL, AWAITING_SHIP, the ASK_EVERY_STEP states),
   chosen by autonomy mode, plan-only flags, and ship mode. The proposal
   never says whether a gate is a node, an edge property, or a property of
   a stage — so a reader cannot tell what deleting a gate, deleting a stage
   next to a gate, or reordering around a gate would do.
9. **Loops and branches have no place in the editor as described.** Nodes
   and edges suggest a flowchart, but the proposal never says whether the
   editor can draw the two real loops (revise, fix task), the conditional
   design branch, the conditional ship branch, or the D3/D6 decision
   branches. If it cannot, a rearranged flow is not a flowchart of the real
   pipeline but a reordering of a simplified list — and the proposal never
   says which of those two it is.
10. **"A rearranged flow must map to real pipeline states" has no mapping.**
    No table is given of editor node to `RunState`, handler, or check. A
    reader cannot verify the claim, and cannot tell what happens to a node
    with no code counterpart, or to a code state with no editor node.
11. **Measurement is demanded but not defined.** "Per-stage token and time
    measurement must be real, not estimated" is a rule without a
    specification: what is measured, where it is stored, and how loops,
    retries, waiting time, and cache hits are counted. Without that, the
    rule cannot be checked, and suggestions built on the measurements
    cannot be trusted or questioned.
12. **The `/profiles` collision is unaddressed.** A dashboard page already
    exists for learning profiles. The proposal adds a second kind of profile
    and a flowchart editor without naming its page, route, or displayed
    name. A user who opens "Profiles" cannot tell which kind of profile
    they are editing, and a reader cannot tell whether the two systems
    share names, storage, or deletion effects. Deleting a learning profile
    deletes its lessons; the proposal never says what deleting a workflow
    profile deletes, or what happens to runs using it.
13. **Shipping is missing from the story.** The proposal's chain ends at
    DONE and never mentions SHIPPING, the ship gate, or the ship modes
    (merge, ask, or end without shipping). A shorter Efficiency flow could
    silently skip shipping, and an Accuracy flow could keep a ship gate the
    proposal never names. The design must place shipping in the flow and
    say whether it is removable, and under what label.
14. **The learning step is missing from the story.** After a run ends, the
    learner runs for runs with a learning profile. The proposal's suggestion
    engine feeds on learning history, but never says whether the learning
    step is part of a flow, whether it can be removed, or whether removing
    it would starve the very suggestions the feature promises.
15. **"Per repo or per user" has no precedence or identity rule.** If a user
    has a profile and a repo has a profile with the same name, which is
    used? If a profile is shared across repos, do its per-repo history
    suggestions follow it? If a repo is renamed or moved, does its profile
    follow? None of this is stated.
16. **Model choice has no owner.** The Efficiency preset promises cheaper
    models "where history says they are safe", but model choice lives in
    dispatcher decisions and role assignments, not in pipeline stages. The
    proposal never says whether the profile overrides the dispatcher, the
    dispatcher reads the profile, or a suggestion proposes a model change.
    A reader cannot tell which component to change or to test.
17. **"Safe" is never defined.** No threshold, no measure, and no failure
    mode is given for a cheaper model being safe. Without a definition, the
    Efficiency preset's central promise cannot be checked, and a suggestion
    to switch models cannot be challenged by the user it is shown to.

## 4. Smaller comprehension gaps

Severity: Major. Dimension: UX and developer ergonomics.

- **Who picks a profile, and when?** At run creation, at profile creation,
  or by default per repo? Can a run change profile after it starts? Can a
  waiting run (at an approval gate) change profile?
- **What does saving validate?** Name uniqueness, version rules, graph
  shape, and safety rules are four different validations, and the proposal
  does not say which block saving, which block running, and which only
  warn.
- **What does the reduced-assurance label say, and where does it appear?**
  On the profile, on the run, on the result, or all three? Does it survive
  to the shipped output (a PR, a merge) so a later reader knows the flow
  was reduced?
- **What does "verified live on the Mac dashboard" cover?** The acceptance
  section lists behaviours, but not which of them are checked live, in what
  order, or with what evidence. A reader cannot tell whether "both presets
  behave as described" is checked by running one run per preset or by
  inspecting settings.
- **What happens to a run when its profile is edited mid-run?** The
  proposal says the default stays unchanged for existing runs, but says
  nothing about custom profiles: does an in-flight run follow the old
  version, the new version, or stop?
- **Does a custom profile built from scratch start empty, from the default,
  or from a preset?** "A preset edited or built from scratch" allows all
  three readings, and an empty starting graph raises the invalid-graph
  question immediately.
- **How are stages ordered when the user drags them?** The proposal says
  stages are nodes and transitions are edges, but does not say whether the
  editor enforces an order, allows any order, or warns when an order makes
  no sense (for example, INTEGRATING before EXECUTING, or DONE in the
  middle).
- **What counts as "history" for a suggestion?** Runs in this repo only, or
  runs across repos for the same user? Failed runs, cancelled runs, and
  runs that stopped in NEEDS_HUMAN — included or excluded? The proposal
  says "the user's own usage history" without bounding it.

Severity: Nit. Dimension: UX and developer ergonomics.

- The proposal writes "7 work states" while also describing a chain with an
  approval gate in the middle; a fresh reader counting the chain gets a
  different number depending on whether gates count as states.
- "Goal presets" and "goal" are used for both the preset and the aim; one
  word for two things invites confusion in the editor's labels.
- "Shadow data" is used without explaining, in the proposal itself, that
  shadow mode measures without serving. A reader who has not read
  `cache.py` will assume shadow data is a kind of cache hit.

## 5. What the design must pin down before it can be followed

This is a checklist for the next draft, not a set of answers:

1. The full node list, with each node's mapping to code (state, handler,
   decision, gate, or check), including loops, branches, DESIGNING,
   SHIPPING, stopping states, and the learning step.
2. The meaning of an edge, and how decision branches (D3, D6, score gate)
   are drawn.
3. The exact safety list, in code terms, and one enforcement choice per
   item: non-removable, refused, or labelled — not "refused or labelled".
4. The exact name, page, and route for workflow profiles, distinct from
   learning profiles and workspace/server profiles.
5. Scope and precedence: per-repo vs per-user, name collisions, versioning,
   editing and deletion effects on in-flight runs.
6. The owner of model choice (profile, dispatcher, or suggestion), the
   definition of "safe", and the amount of history required.
7. The exact cache settings the Efficiency preset changes, within the rule
   that live semantic serving stays OFF.
8. The measurement specification: unit, inclusions, exclusions, storage.
9. The suggestion format, its reasoning, its approval scope (one run vs
   persistent), and the threshold at which history replaces presets.
10. The invalid-graph list, the moment and wording of refusal, and whether
    invalid graphs can be saved as drafts.
11. The CLI's behaviour with profiles, and whether a profile can change
    mid-run.
12. One plain definition of each preset's tradeoff, in the same units, and
    a statement of how Accuracy differs from the default, if it does.

Ergonomic requirement for the next draft: every term in section 2 must be
defined where it is first used, in one sentence, in plain words, and every
behaviour in section 3 must have exactly one stated outcome — not a choice
between two outcomes left for the reader to guess.
