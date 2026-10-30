# Consolidated critic report — Stage 7 (design, structural, style lenses)

Target: `docs/designs/profile-workflows/design.md`, lines 1–995
(the FeatureRange: sections 1–14). Sections 15–19 were out of scope
for these three lenses and are covered by stages 8 and 9.

Method: the three stage-7 lens reports were read in full and merged
here by underlying issue, not by lens. A theme below cites every
lens finding that raised it, so the resolution report can answer
each theme once instead of answering the same issue three times.
Per-lens counts are preserved; the merged themes are fewer than
the per-lens finding counts because the lenses independently found
the same issues.

## Per-lens counts and verdicts

| Lens | Report | Blockers | Majors | Nits | Verdict |
|---|---|---|---|---|---|
| Design | `critic_design_report.md` | 8 | 12 | 6 | Not implementable until the Blockers are settled |
| Structural | `critic_structural_report.md` | 7 | 15 | 5 | Structure does not hold together as written |
| Style | `critic_style_report.md` | 7 | 14 | 12 | Fails the plain-language test in the places users will actually read |

Per-lens totals: **22 Blockers, 41 Majors, 23 Nits (86 findings).**
Merged unique themes below: 24. The overlap is the signal: the
snapshot contradiction, the refusal-list contradiction, the gate
table gap, and the preset-table breaks were found independently by
two or three lenses each.

## Merged themes (what the three lenses agree on, and what each adds)

### T1. The snapshot is defined two ways, with no merge rule
Design B1 (in part), structural B2, style B7, comprehension B2
(also stage 8 P2, stage 9 codebase B11). Section 5 defines the
snapshot as a version frozen onto a run (a reference); section 6.4
defines it as the full resolved parameter set stored on the run
record (a copy); section 8.1 adds a fallback to repo configuration
for parameters the profile does not set, which a full copy has
nothing left to fall back to. The snapshot also duplicates fields
the run record already carries (`quality_level`, `ship_mode`,
`learning_profile`) with no rule for which copy a handler reads.
Resolution: R2, R3 (see `resolution_report.md`).

### T2. Two different lists of stages that cannot be left out
Design B2 (in part), structural B4 (in part), style B3, comprehension
B4, stage 8 P1. Section 7.3 forbids omitting five stages (INTAKE,
PLANNING, VALIDATING, EXECUTING, INTEGRATING); section 10.2 refuses
omitting only three (VALIDATING, EXECUTING, INTEGRATING). Nothing
says what happens to a profile that omits INTAKE or PLANNING.
Resolution: R1.

### T3. Handler-chosen moves bypass the snapshot; gate parking has holes
Design B1, M1; structural B6; comprehension B1. Handlers move the
run themselves (`_apply_d3_verdict` to DESIGNING, `_integrate`
loop-backs to EXECUTING, `_after_ship_vote` to SHIPPING /
AWAITING_SHIP / DONE), and `Pipeline.approve` maps gates back to
steps with a static map — none with a snapshot check. A profile
that omits DESIGNING can still have a handler move the run into
it, and `_advance` breaks out of its loop (then calls `_finish`)
when the current state has no handler, instead of refusing.
`_integrate` moves directly to AWAITING_SHIP, which is in neither
`STEP_GATES` nor `execution_gate_target`. Resolution: R3, R4.

### T4. No gate-policy table, and the gate rules contradict each other
Design M1, structural B6 (in part), comprehension B1, style M8.
No step × autonomy mode × policy → gate state table exists. INTAKE
has no gate state, so a policy on it is meaningless. EXECUTING
shares AWAITING_APPROVAL with the plan gate, with no rule for
which step's policy governs which parking. `require` ("park even
when the mode would not") contradicts the AUTONOMOUS exemption.
The SHIPPING step's policy does not say whether it governs
AWAITING_SHIPPING, AWAITING_SHIP, or both. Resolution: R4.

### T5. The safety floor does not hold against the bypasses it names
Design B2, structural B4, comprehension B4. Four bypasses live
outside profiles and are not closed by profile refusals: a missing
ship score still ships (`ship_gate_outcome` returns "ship" on
`None`); `score_skip_human` in configuration still skips;
the plan gate is skipped in standard mode when plan approval is
off in config, not only under AUTONOMOUS; a quality-`none` run
can still be shipped later by hand. The label's runtime effect
is also undefined: a label in a shipped PR body is after the
fact and cannot protect a ship that already happened.
Resolution: R7, R17.

### T6. The preset table breaks its own intent
Design B3, M9; structural B3, M3, M4; comprehension B8, M3; style
M7. Efficiency `basic` drops secret scanning, SAST, and dependency
audit (those sit at `medium` and above) — a security cut presented
as efficiency. Accuracy `production` lowers a repo already at
`scalable`. "Cheapest configured model per role" has no price
source (`[dispatcher.cost]` holds leg prices, not per-role model
prices; Codex reports no cost). Efficiency `free_training_ok`
unconditionally overrides a repo's privacy-relevant planner policy.
Round caps are stated absolutely with no "at most / at least"
semantics against repo ceilings, and `max_task_attempts` bounding
itself is circular. The Efficiency stage-list cell ("full list
minus nothing required") says nothing. Resolution: R8, R9.

### T7. The suggestion engine's evidence base does not exist
Design B6, M7, M8; structural M5, M6; comprehension B12, M18.
Example reason lines cannot be computed from the section 11
schema: no per-stage defect attribution ("caught plan defects in
7 of your last 12 runs"), no price source, no effective model or
level per stage, no defect-escape count definition, no
quality-gate-failure attribution rule. "Shortening" (rounds) and
"dropping" (stages) are used interchangeably with one threshold
for both. Example numbers break section 11.3's own rules (no
window, no completeness flag, medians over mixed full/partial
rows undefined). Resolution: R13, R14.

### T8. The editor cannot rearrange anything, but the goals promise it
Design B8; style N8; stage 5 Blocker 1; stage 8 P3. Section 7.1
refuses every reordering; section 13.2 admits no reordering
passes in practice. Sections 3 and 17 still promise "rearrange"
as a goal and an acceptance criterion. The only shape changes
the editor can make are omitting DESIGNING / SHIPPING where
allowed and editing parameters. Resolution: R18 (wording) plus
option set D, which the human gate decides.

### T9. The new "profile" collides with existing uses, and scope claims are wrong
Design B5, M12; structural (profile collision, in B5 context);
comprehension M1 (in part); style M4. The header counts two
collisions (learning profiles, workspace/server profiles); the
brief and the code add a third (`QueueItem.profile`, the
workspace-profile name on queue items). No distinct parameter or
field name is given for workflow profiles. "Repo-scope profiles
travel with the repo" is false: the files live under `<repo>/.jev/`,
which the repo ignores. The existing workspace-profile snapshot
(`servers/profiles.py`, `.jev/runs/<id>/profile.json`) is never
related to the new snapshot. Resolution: R12, R15, R21.

### T10. The profile language contradicts itself on DESIGNING and SHIPPING
Design M11; comprehension B3. An omitted DESIGNING "still runs it
when the run has UI work" under `when_needed`, so the executed
stage list contradicts the snapshot stage list, and the design
never says whether DESIGNING under `when_needed` is in the list
or absent with a side parameter. "Ship mode `off`" does not exist
in code: `ShipMode` is `merge`/`ask`, and "off" is
`[ship] enabled = false` plus `after_approval` unset.
Resolution: R5, R6.

### T11. The validator is stated, never specified
Structural B5; comprehension B11; design (in B1 context). No
location, no owner, no input format (draft profile vs. resolved
snapshot — different shapes, one function claimed), no live-vs-
save split beyond two examples ("where cheap"), no schema for
it to validate against. Resolution: R10, R11.

### T12. Terms are used before they are defined, or defined inconsistently
Style B1, B2, M1, M8, M11, M13, M14; comprehension M1, M17.
"Stage", "step", "node", "state", "gate", "flow" are never
defined yet used interchangeably; "gate" carries five meanings;
"default" carries five meanings; "flow" means the concept, the
drawing, the execution, and the stored object. D1–D8, CDA tiers,
SimHash, repo fingerprint, leases, "checked forms with
off-loopback token gating", and calibration floors are used
without definitions. "Workspace profile" is used as a precedence
source without ever being defined. Goal values list three values
while `null` is a fourth, and `custom` is never explained;
preset-assigned vs. user-stated goal has no conflict rule.
Resolution: R21.

### T13. Preset versioning is contradictory
Style B4; structural M2, M4. Versions are "numbered per profile,
starting at 1" and presets are "versioned with the Baton release"
— a version cannot be both `3` and a release string, and the run
record does not say which form a preset run carries. Preset
versions "accumulate alongside the old one" across upgrades while
presets ship as package data an upgrade replaces. A user copy of
a preset has no stated numbering rule. Resolution: R11, R12.

### T14. Storage rules are incomplete
Design B5 (in part), M4; structural M4, M11; comprehension M10.
Version creation is two steps (write `v<n>.json`, then update
`current.json`) with no atomicity or concurrency rule. Retention
defers to "the repo's existing run-retention behaviour" without
naming one. "Archived" means two things: hidden from creation
lists (section 6.3) and a refusal message for a version string
(section 6.1). The store pattern (one JSON file per version plus
a pointer) diverges from the learning-profiles table pattern
without a reason. Resolution: R12.

### T15. The integration test command is a knob in one section, absent in another
Design B7. Section 2.3 lists `[integration]` test command among
the knobs; section 7.4 omits it from profile parameters; section
14 says profiles never run a command named by a profile. The
three statements together leave the command's status undecided.
Resolution: R21 (the command is not a profile parameter; a
profile never names a command; the run uses the repo's
configured command).

### T16. The learning-profile mechanism is underspecified
Comprehension M5. Section 7.4 names a learning profile on the
profile, but the run's existing setting (`[learning] profile`,
default `"default"`, plus a separate `[learning] enabled` flag,
off by default) is never stated; whether naming a profile also
enables learning, and whether an unknown name refuses when
learning is off, are unanswered; and no rule converts a lesson
(a text distillation with a role and retirement state) into a
comparable suggestion signal. Resolution: R21 (learning-profile
rule: naming a learning profile on a profile does not enable
learning; learning runs only when `[learning] enabled` is set;
an unknown learning-profile name refuses at save regardless of
the enabled flag, because a saved reference must resolve).

### T17. Semantic cache: the document contradicts itself and the code
Comprehension M6. Section 2.5 says semantic similarity exists
behind `[cache.semantic] enabled` (default off); section 7.4 says
"there is no semantic serving mode to select, because serving
does not exist"; the code serves when semantic mode is explicitly
enabled. The interaction of a profile's `exact`/`exact_off` cache
mode with an enabled semantic flag is unspecified.
Resolution: R21 (serving exists only when explicitly enabled;
profiles never enable it; `exact_off` stops exact serves only
and leaves the semantic flag untouched; shadow logging
continues under `exact_off` because it measures, not serves).

### T18. Suggestion approval lifecycle is underspecified
Structural M6; comprehension M7. Approval "saved as a new profile
version, like any other edit" does not say whether an identical
draft creates version n+1, what provenance field records
"suggested", what counts as new evidence for re-offering a
discarded suggestion, or who enforces the re-offer rule.
Resolution: R14 (approval of an identical draft creates no new
version — it records approval of the existing version; provenance
field `origin: suggested` on the version; new evidence =
crossing a section 12.3 threshold or a new lesson in the window,
checked by the engine at request time).

### T19. Scope re-validation list is illustrative, not exhaustive
Comprehension M10. Section 6.5 names three checks (models,
quality levels, cache modes) and omits planner policy, ship
settings, autonomy interaction, learning-profile existence, and
CDA availability — all profile parameters under section 7.4.
Resolution: R12 (the re-validation list is exhaustive: every
section 7.4 parameter is re-validated against the target repo,
and the list in section 6.5 now names all of them).

### T20. Floor items 5 and 6 rely on mechanisms never defined
Comprehension M15. Item 5's "different vendor than the planner"
has no knob in the knob list and no configuration key named.
Item 6's branch / test record / quality record have no format,
field list, or storage location, and `[ship] require_quality_pass`
and `[ship] enabled` are never mentioned in the floor discussion.
Resolution: R7 (item 5 restated with the mechanism: the validator
role's model list is configured per repo, and "different vendor"
is checked against the planner's vendor only when the repo
configures a distinct validator model; otherwise the item is
satisfied by D3 validation alone and the assurance computation
records that. Item 6 restated: branch = the run's result branch,
test record = the integration-test result stored on the run,
quality record = the latest quality-gate report; all three are
already run-record data the run page shows, and the ship path
refuses when any is missing).

### T21. The legacy path both does and does not change
Structural B7; design B2 (in part). Section 8.2 says runs without
a profile execute exactly today's code path with no behaviour
change, while section 10.2 applies assurance computation to every
run and section 10.3 puts the label in the PR body for any
shipped result — a PR-body change on a legacy run is a behaviour
change on the legacy path, and the CLI ship path has no named
PR-body writer at all. Resolution: R17 (stated exception:
execution behaviour unchanged; display surfaces gain the label;
writer `core/ship/pr_body.py` on both ship paths; conformance
includes label output).

### T22. Preset assurance values are never stated
Comprehension M11. Section 9.1 never states either preset's
assurance, while section 13.1 requires the picker to show it
before the run starts. Resolution: R8 (preset assurance table
added to section 9.1: both presets `full` under the fixed table,
because neither drops a floor behaviour; a custom profile that
lowers quality below the repo level or sets a floor-relevant
flag is what produces `reduced`).

### T23. Plain-language failures in user-visible text
Style B5, M2, M5, M6, N3, N4, N10; design N3 (in part). The
reduced-assurance label text (`"quality gate: none"`,
`"Assurance: reduced — quality gate: none"`) and several
catalogue messages (`"Gate without a stage is not a flow"`,
bare capitalised stage-code lists, `<id>`/`<n>` placeholders)
fail the document's own plain-language test: they do not say
what was dropped, what it means for this run, or what the user
can do. Section 4 headings are metaphors ("The drawing lies")
rather than descriptions. "Honest/honesty" is used as
self-certification. "Badge" vs. "note" vs. "displayed" are three
wordings for one label. Capitalisation of defined terms is
inconsistent. Resolution: R21 (catalogue rewritten in
problem/reason/fix form with plain stage names; label rewritten
in plain form; one display term, "assurance label"; section 4
headings rewritten as descriptions; "honest" replaced by the
rule that does the work).

### T24. The absolute claim in section 7.5 is false by the design's own mechanism
Style B6; structural M13. "A flow that saves cleanly cannot fail
differently at run time for a validation reason" is contradicted
by the same section's run-start re-check (which exists because
on-disk edits between save and run start can invalidate a
profile) and by preset staleness (a model removed after save).
Resolution: R21 (claim replaced: the editor and the executor run
the same validator, so a save-time pass and a run-start pass use
the same rules, and a run-start failure names the changed
setting).
