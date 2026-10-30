# Re-audit: codebase consistency of the "Decided picks" amendment

Scope: this report reviews ONLY the amendment subsection
"Decided picks (2026-10-29, never-stop directive)" in
`docs/designs/profile-workflows/design.md` (lines 2105–2157, immediately
before "## 17. Acceptance criteria"), against (a) the design's own
plan in sections 6–15, and (b) the repo's current code on branch
`design/profile-workflows`. It does not re-review the rest of the
design, and it does not judge whether the picks are good product
choices — only whether the amendment's factual claims are accurate,
whether its named artefacts exist in the plan, and whether any pick
needs implementation work the amendment does not acknowledge.

Method: read the amendment in full; read sections 15, 8.2, 9.2,
6.2, 6.5, 13.1, 10.2, 10.3, 9.1, 16 (option sets A–H), and 17
(rollback/acceptance) for cross-checks; grepped the worktree code
for pipeline states, config, gitignore, and any existing profile
machinery. No file other than this report was created or modified;
`design.md` was not touched, nothing was committed or pushed, and
no live machine was contacted.

## Verdict

- **Blockers: 0** — no amendment claim is factually wrong about the
  current code or about the plan's normative content.
- **Majors: 3** — each is a requirement the amendment creates or
  repeats that has no implementation or test home in section 15,
  or a contract-status claim the rest of the document contradicts.
- **Nits: 6** — wording-precision gaps that cannot by themselves
  change implementation, but should be fixed in the phase-0 sweep.

The single most important finding is Major 1 (H3): the Accuracy
priority row exists nowhere except in the amendment's own "Effect"
sentence. Section 9.1 has no priority row, section 13.1's picker
spec does not mention it, and neither the phase-4 bullet nor the
phase-4 or section-17 acceptance criteria check it — so an
implementation that follows sections 13.1/15/17 alone would pass
acceptance without ever showing the row, and the row as specified
has no executable effect in any case (see Major 1).

## What checked out (verified correct)

These amendment claims were verified against code or plan and are
accurate; they are listed so the absence of a Blocker is auditable,
not asserted.

1. **Hardcoded pipeline today.** `src/jev_agent/core/pipeline.py`
   lines 361–372: `_advance` builds a literal
   `steps: Mapping[RunState, Callable[...]]` dict
   (`INTAKE: _intake`, `PLANNING: _plan`, `VALIDATING: _validate`,
   `DESIGNING: _design`, `EXECUTING: _execute`,
   `INTEGRATING: _integrate`, `SHIPPING: _ship`) and loops over it.
   There is no transition table and no profile lookup in the path.
   The amendment's G1 premise ("runs without a profile execute
   today's code path unchanged") is therefore grounded.
2. **No profile machinery exists yet.** `grep -rn
   "workflow_profile|workflow-profiles|default_via_profile" src/`
   returns 0 hits; `src/jev_agent/core/workflow_profiles/` and
   `src/jev_agent/workflow_profiles/` do not exist; there is no
   `src/jev_agent/dashboard/routes_workflows.py` (existing routes
   files are board, chat, code, connections, design, learning,
   preview, queue, settings, studio). Every module/route/directory
   the amendment names is therefore future plan, not existing code
   — and the amendment never claims otherwise.
3. **`.jev/` is gitignored, so repo profiles do not travel via git
   by default.** `.gitignore` line 13 is `.jev/`; `git check-ignore
   -v .jev/workflow-profiles/x/v1.json` resolves to
   `.gitignore:13:.jev/`. This matches design section 6.2
   ("They do not travel via git by default, because `.jev/` is
   ignored by the repo"). See Nit 1 for the qualifier the
   amendment drops.
4. **A1 names match the plan.** Route `/workflows` and its
   sub-routes are section 13.1 (lines 1433–1442);
   `src/jev_agent/dashboard/routes_workflows.py` is section 15
   phase 5; `src/jev_agent/core/workflow_profiles/` is section 15
   phase 2; `src/jev_agent/workflow_profiles/presets/` is section
   15 phase 4 (and section 6.2); `<repo>/.jev/workflow-profiles/`
   is section 6.2; creation field `workflow_profile` is section
   6.1. The amendment's A1 claim is correct for routes, modules,
   and directories (see Nit 3 for templates).
5. **B1 refusals and label surfaces match sections 10.2/10.3.**
   Section 10.2 (lines 1108–1113) refuses exactly the three cases
   the amendment lists: omitted non-omittable stages, quality
   `none` with shipping enabled, `score_skip_human` with shipping
   enabled. Section 10.3 stores assurance on the run record and
   renders the label on the run page, the board row, and the pull
   request body — the four surfaces the amendment lists.
6. **C1 phase order matches section 15.** Contract (phase 0),
   measurement (1), storage/selection/assurance (2), validator +
   conformance (3), presets + picker (4), editor (5), suggestions
   (6) — the amendment's parenthetical order is exact.
7. **D1 matches section 13.2** (canvas plus list, one validator,
   input-equivalence) and **E1 matches sections 6.5/6.2** (both
   scopes; repo-scope sharing by dashboard export/import in
   phase 5).
8. **F1 order matches sections 9.2 and 6.4 exactly:** per-run
   flags, then profile version/snapshot, then repo configuration
   plus workspace profile, then Baton built-in defaults, with the
   winning source in the snapshot's `resolved_from` map (section
   6.6). The amendment's paraphrase introduces no reordering.
9. **G1's flag matches section 8.2 exactly.** Section 8.2
   (line 951) says the default executes through the profile path
   "only when the configuration flag `[workflows]
   default_via_profile` is set to true (default false)".
   Note the flag does not exist in code today:
   `src/jev_agent/config/defaults.py` has no `[workflows]`
   section at all (its sections run `[dispatcher]` through
   `[cache]` with no `[workflows]`), so the flag is a future
   design artefact, correctly cited as such — but see Major 2 for
   the missing implementation home.
10. **Escalated/confirmed readings match the plan.** Hand ship:
    section 10.2 (lines 1128–1132) already says "the label
    travels and the ship proceeds" with no confirmation step, so
    the amendment's "no extra confirmation step is added" adds no
    new code. Efficiency quality: section 9.1's table gives
    Efficiency "Repo configuration (never lowered by the preset)",
    matching the amendment. "Rearrange" (sections 7.1/7.3) and
    the section 10.3 display exception for legacy runs (section
    8.2) are also stated correctly.

## Majors

### Major 1 — H3's priority row has no home in sections 9.1, 13.1, 15, or 17, and no executable effect

Amendment wording (design.md line 2125):

> "H3 — quality first, speed second | Sections 6–15 already define
> Accuracy mechanically as a production quality floor plus CDA
> grading on (section 9.1); H3 keeps those mechanics exactly and
> adds only the priority row on the picker entry ("quality first,
> speed second")."

and lines 2153–2155:

> "H3 adds one display requirement to phase 4: the Accuracy picker
> entry shows the priority row "quality first, speed second" before
> selection."

Evidence of the gap:

- Option H3 itself (section 16, lines 2093–2103) places the
  tie-break in a different location from the amendment: "The
  tie-break lives in the preset's table as a "priority" row
  reading "quality first, speed second", shown on the picker entry
  before selection." The amendment keeps only the second half
  (picker entry) and drops the first (preset's table). Section 9.1
  as written has no priority row in its preset table, and the
  amendment does not add one there.
- Section 13.1's picker spec (lines 1445–1454) requires the goal
  label and the assurance preview before the run starts; it does
  not mention a priority row.
- Section 15 phase 4's picker bullet (lines 1721–1724) requires
  "preset picker entries on the run-creation form with their
  documented behaviour shown before selection, including the
  assurance preview (section 13.1) and the unpriced-role notice
  where it applies (section 9.1)"; it does not mention a priority
  row, and phase 4's acceptance (Accuracy "runs the full canonical
  stage list with CDA grading on and a quality level at or above
  `production`") does not check for it.
- Section 17's main acceptance criterion for presets ("behave as
  their section 9.1 tables say") therefore cannot test the row,
  because the row is in neither the table nor any acceptance text.
  An implementation following sections 13.1/15/17 alone would pass
  acceptance without showing the row.
- The row as specified has no executable semantics in any case.
  H3's mechanism (section 16) says that "within equal-quality
  choices [Accuracy] prefers the faster one (fewer redundant
  rounds, no repeated work)", but the amendment itself concedes
  "without changing any preset value" — and section 9.1's Accuracy
  round caps are identical to the default's (`max_plan_rounds` 3,
  `max_fix_rounds` 2, `score_max_retries` 2), so no
  faster-within-equal-quality choice is actually implemented
  anywhere. The tie-break is display text only, and the amendment
  does not acknowledge that limitation; it presents H3 as
  "answer[ing] both readings in priority order" while changing
  nothing that could make a run faster.

Required fix before phase 0 freezes the contract: either (a) add
a `priority` row to the section 9.1 Accuracy column and to the
preset file format, add the row to the section 13.1 picker spec
and the phase-4 picker bullet, and add a phase-4/section-17
acceptance check for it; or (b) state explicitly that the row is
display-only documentation with no tie-break execution, and record
that the H3 speed reading is therefore unimplemented. The current
single sentence in "Effect on the rest of this document" does
neither — it is not a normative edit to any of the sections an
implementer will actually build from.

### Major 2 — G1's `[workflows] default_via_profile` flag has no implementation or test home, and its scope is ambiguous against section 13.1

Amendment wording (design.md line 2124):

> "G1 — default stays hardcoded, guarded by conformance | Zero risk
> to existing runs is the governing constraint: runs without a
> profile execute today's code path unchanged, and the `default`
> preset is proven equivalent by the conformance suite before any
> run executes through the profile path (and then only behind
> `[workflows] default_via_profile`, default false)."

Evidence:

- The flag string `default_via_profile` occurs only twice in the
  whole document: section 8.2 (line 951) and the amendment itself
  (line 2124). No section 15 phase bullet implements it: no phase
  defines the `[workflows]` config section, its parsing, its
  default-false handling, or which run-creation branch reads it.
  Phase 2 explicitly says "Runs without a profile keep the legacy
  path: the resolver is skipped, no snapshot is stored" — with no
  flag-gated exception — and phase 3's snapshot-aware lookup is
  "behind the profile path only".
- The amendment does not say which runs the flag flips, and the
  plan points both ways. Section 8.2 says "After the suite passes,
  the default executes through the profile path only when the
  configuration flag ... is set to true". Section 13.1 says
  "`default` in the picker means "no profile" (the legacy path),
  not a preset that runs." If picker-`default` means no profile,
  there is no "default preset run" for the flag to flip; if the
  flag flips no-profile runs onto the profile path, that
  contradicts the amendment's own first clause ("runs without a
  profile execute today's code path unchanged") whenever the flag
  is true. The amendment repeats section 8.2's wording without
  resolving this.
- No test covers the flag. Section 17's rollback tests exercise a
  different flag — `[workflows] enabled`, default true (lines
  2277–2282: "The flag is `[workflows] enabled` in `jev.toml` (a
  new configuration section), default true") — which disables
  profile selection, not the default path. The amendment never
  distinguishes `default_via_profile` (default false, section 8.2)
  from `enabled` (default true, section 17), so an implementer
  could conflate the two `[workflows]` flags or implement only
  the rollback one.
- The flag name and default themselves are correct against
  section 8.2 (see "What checked out" item 9); this Major is
  solely about the missing phase ownership, the unresolved
  picker-default interaction, and the unacknowledged second
  `[workflows]` flag.

Required fix: assign the flag to a phase (config definition in
`defaults.py`/config parsing, plus the run-creation branch that
reads it), state precisely which runs it affects (picker-`default`
runs, explicit `default`-preset runs, or both) reconciled with
section 13.1, and add a conformance/rollback test for the flag
distinct from the `[workflows] enabled` test.

### Major 3 — "No section text changes" and "phase-0 entry met" overstate the amendment's effect; decided status is still contradicted elsewhere in the document

Amendment wording (design.md lines 2151–2157):

> "Effect on the rest of this document: sections 6–15 already
> assume A1, B1, C1, D1, E1, F1, and G1, so no section text changes
> for those picks. ... Phase 0's entry condition (every option set
> picked, document amended by re-audit) is met by this amendment
> plus the re-audit report that follows it."

Evidence that text changes are in fact still required for the
contract to read as decided:

- Front matter (lines 20–26) still states: "The following points
  are proposed, not decided, and change under the option set
  named: scope (section 6.5; set E), precedence (section 9.2;
  set F), enforcement (sections 7.3, 10.2, 10.3; set B), routes
  and names (section 13.1, phase 5; set A), the default's
  representation (section 8.2, phase 3; set G), and what Accuracy
  optimizes (section 9.1; set H)." After the amendment, every one
  of those sets is decided, so this paragraph is now false as
  written.
- Per-section status qualifiers still read as undecided:
  section 6.5 heading "(proposed; option set E may narrow it)"
  (line 608); section 9.2 heading "(proposed; option set F may
  reorder it)" (line 1020); section 13.1 heading "(proposed;
  final names follow option set A)" (line 1431); section 10.2
  "decided finally by option set B" (line 1106); section 8.2
  "Whether the default is ever re-expressed as a profile, or
  stays hardcoded with the conformance test as its guard, is
  option set G" (lines 953–955); section 16 intro "The human
  picks one option per set before implementation. A set left
  unpicked blocks implementation" (lines 1823–1827).
- Section 19 still frames two amendment-decided items as open
  human-gate questions: "Manual ship of a reduced run" (lines
  2369–2377: "Whether a hand ship of a reduced run should also
  require an explicit confirmation is a human-gate question")
  and "Efficiency's quality promise" (lines 2382–2389: "The human
  gate confirms this change"). The amendment decides both (no
  confirmation; repo configuration, never lowered), but leaves
  both bullets unclosed.
- Phase 0's own contract bullet (lines 1565–1572) says "the
  amendment touches the sections the gate picks changed, listed
  in the re-audit report" — i.e. the re-audit is expected to list
  touches, not merely to bless a "no changes" claim.

This is a Major, not a Blocker, because the picks themselves are
unambiguous in the Decided-picks table and no normative value
(stage list, refusal list, precedence order, route) is misstated;
an implementer who reads the amendment will build the right thing.
But phase 0's entry condition cannot be treated as met by the
amendment plus this report alone: the touches listed here (status
qualifiers, front matter, section 19 bullets, plus Majors 1–2)
must actually land in the document text before the contract is
frozen, or the frozen contract will still describe its own core
decisions as proposed.

## Nits

### Nit 1 — E1 drops section 6.2's "by default" qualifier on git travel

Amendment wording (line 2122):

> "Repo profiles do NOT travel via git (`.jev/` is gitignored) —
> sharing is dashboard export/import only, as section 6.2 states."

Section 6.2 (lines 528–531) says: "They do not travel via git by
default, because `.jev/` is ignored by the repo". The amendment's
absolute phrasing omits "by default". In normal git operation the
effect is the same (verified: `.gitignore:13:.jev/`), but the
absolute form is strictly stronger than the plan: an ignored path
can still be force-added. Restore the qualifier so the amendment
quotes section 6.2 exactly.

### Nit 2 — H3's summary of Accuracy omits its third section 9.1 difference

Amendment wording (line 2125): Accuracy is defined "mechanically
as a production quality floor plus CDA grading on (section 9.1)".
Section 9.1 (lines 1011–1013) states Accuracy "differs from the
default in exactly three observable ways: CDA grading on, a
production quality floor, and the `accuracy` goal label." The
amendment lists two of the three and drops the goal label. The
omission does not change any value (the label is recorded, not
mechanical), but a summary that cites section 9.1 while listing
only two of its "exactly three" differences invites a later reader
to treat the list as complete.

### Nit 3 — A1's "templates" are not actually named in the section 15 plan

Amendment wording (line 2118): "Sections 6–15 and the section 15
plan already use the A1 names (routes, modules, templates,
directories, messages, labels)." Routes, modules, and directories
are verified (see "What checked out" item 4). Template files are
not: section 15 phase 5 says only "new templates following the
existing flat template pattern", with no A1-specific template
filenames, and messages/labels are likewise not enumerated in
section 15. The claim is true for the named artefacts that exist
in the plan; template filenames (presumably `workflows_*`) remain
to be fixed at implementation time rather than already fixed by
the plan. Narrow the sentence to routes, modules, and directories,
or name the templates.

### Nit 4 — F1 inherits the section 9.2 / 6.6 `resolved_from` split without resolving it

Amendment wording (line 2123) collapses tier 3 as "repo
configuration and workspace profile", matching section 9.2, which
says the design "does not change the intra-tier order between a
config file and a workspace profile". Section 6.6's
`resolved_from` map, however, lists five distinct sources —
`flag`, `profile`, `repo`, `workspace`, `default` — so a setting
won by tier 3 still needs a recorded winner of `repo` vs
`workspace`. This tension predates the amendment (it is in the
plan itself), and the amendment does not create it; but by
repeating the collapsed tier without noting the split, the
amendment leaves the implementer without a rule for which of the
two `resolved_from` values a tier-3 win records. One sentence in
phase 2's resolver bullet would close it.

### Nit 5 — G1's title shorthand ("stays hardcoded") overstates permanence

Amendment wording (line 2124, pick column): "G1 — default stays
hardcoded, guarded by conformance". The amendment's own rationale
in the same row correctly adds the flag-gated exception ("and then
only behind `[workflows] default_via_profile`"), and section 8.2
confirms that after conformance passes, the default can execute
through the profile path when the flag is true. Read alone, the
title suggests the default never executes via the profile path;
read with the rationale, it means "hardcoded unless the flag is
set". The title is the option set's own label (section 16, G1),
so this is a readability nit, not an error — but the flag
exception should travel with the shorthand wherever G1 is cited
outside the table.

### Nit 6 — B1's B3 critique omits B3's stage-core exception

Amendment wording (line 2119): "B3 would let a labelled reduced run
ship with nothing but a label standing between it and main."
That is true for behaviour-class reductions under B3, but section
16's B3 definition (and section 7.3) explicitly keeps the
five-stage core refused under B3: "The five-stage core (section
7.3) stays refused under B3: stage omissions are structural, not
behaviour reductions". The amendment's B1 row correctly lists
stage refusals for B1 itself, but the B3 sentence as written could
be read as claiming B3 labels stage omissions too. Add "for
behaviour reductions (the stage core stays refused)" to the B3
clause.

## Items explicitly checked and found not to be problems

- **A1 vs section 15 names:** no mismatch. The plan's routes
  (`/workflows`), module (`routes_workflows.py`,
  `core/workflow_profiles/`, `workflow_profiles/presets/`), and
  directories (`.jev/workflow-profiles/`) are all A1 names, as
  section 15's own preamble states ("the names below are the A1
  names").
- **G1's flag vs section 8.2:** no mismatch in name or default
  (`[workflows] default_via_profile`, default false). The problem
  is ownership/scope (Major 2), not the flag's identity.
- **F1 vs section 9.2:** no reordering. The amendment's order is
  section 9.2's order verbatim in substance.
- **H3 vs section 16 H3's values:** no preset value is changed,
  which is exactly what H3's "priority row" mechanism (as opposed
  to H2's speed work) requires. The problem is that the row has no
  normative home and no effect (Major 1), not that a value was
  altered.
- **Code claims:** the hardcoded-pipeline, no-profile-machinery,
  and `.jev/`-ignored claims all verify against
  `src/jev_agent/core/pipeline.py`, the absence of any
  `workflow_profiles` code, and `.gitignore:13` respectively.
  No Blocker arises from the code grounding.
