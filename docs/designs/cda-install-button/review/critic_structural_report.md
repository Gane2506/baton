# Structural Goldfish review — CDA install button, feature-specification part

- **Target:** `docs/designs/cda-install-button/design.md`
- **Scope audited:** everything before the Detailed Implementation Plan —
  the header and sections 1–7, lines 1–764. Section 8 begins at line 765.
  Sections 8–12 were **not** audited as specification; section 9 is cited
  only where sections 1–7 themselves point to it, because that pointing is
  itself a structural property of the scoped part.
- **Standard applied:** `feature-spec-standard.md` — Problem Statement,
  Goals & Non-Goals, Proposed Architecture, Alternatives Considered,
  Risks & Mitigations, plus the Alternatives Rule.
- **Code checked, only where cited below:** `backend.py`
  (`CodeOrchestrator.available`, `load_backend`),
  `routes_code.py` (method names, not a route table),
  `browser_install.py` (state vocabulary, log tail/cap),
  `code_grade_runner.py` (state vocabulary, `unavailable_message`),
  `code_grade.html` (status panel as built today). No other code is
  relied on, and no claim below asserts anything about a file not listed here.
- **Mechanical check:** heading levels in scope run H1 → H2 → H3 with a
  single H4 (see Nit N2); no level is skipped, and the tables in
  sections 5.4, 6.2 and 6.8 are intact. The findings below are therefore
  not about Markdown syntax. They are about a specification that is
  internally undecided, duplicative, and incomplete as a state machine.

## Verdict in one paragraph

Sections 1–7 read as a decided, normative design — "must", "is
normative", "the panel must never" — while simultaneously deferring
the decisions that would make those statements true to option sets
that sit *after* the implementation plan. The result is not a spec
with open questions attached; it is a spec whose own normative
sentences contradict each other depending on which option is later
picked. Fix the decision structure first (one decided spec, or a
spec explicitly marked conditional section by section); fixing
wording inside individual sections will not close these findings.

## Findings

### Blockers

#### B1 — The scoped part is an undecided spec wearing decided language
- **Severity:** Blocker
- **Section / lines:** Header, lines 6–7; §3 Goals, lines 163–189;
  §5.1, lines 293–297; §5.4, lines 425–429; §9 as cross-referenced.
- **What is structurally wrong:** The header says human approval of
  the option sets in section 9 is required before any code, and §5.4
  says its matrix "is normative". But §5.1 then supplies a fallback
  decision — "if the human picks no option, [the panel flips on
  layer 2, layer 3 reported separately]" — which directly contradicts
  the header's gate and §9's own rule (as cited from §§1–7) that an
  unpicked set blocks its dependent parts. §3's goals are likewise
  unconditional ("An Install button …", "The same endpoint on Mac,
  Windows, Linux, and VPS"), yet option set C, as cross-referenced
  from §3/§5.4, includes narrowing the button and removing it
  entirely in favour of a guide. A goal that is negated by a later
  option is not a goal; it is a placeholder.
- **Evidence:** Line 6–7 (approval required); lines 293–297 (default
  if no option picked); §3 goal list (no conditional marking on any
  goal); §5.4 line 425 ("The matrix is normative") sitting in the
  same section as a Docker row that reads "See option set G"
  (line 423).

#### B2 — §4's traceability claim is false on its own terms
- **Severity:** Blocker
- **Section / lines:** §4, lines 199–277, especially lines 201–203.
- **What is structurally wrong:** §4 opens: "The brainstorm lenses
  raised 16 blockers. The design answers each one in the section
  noted; a blocker with no answering section is a gap in this design,
  not a deferred detail." The section then lists **21 bullets**, not
  16, with no severity marking, so a reader cannot tell which bullets
  are the 16 blockers and which are majors/nits folded in. Worse,
  at least seven bullets are "answered" only by an option set or by
  a section outside the scoped part: runner shape → option set B;
  stop → option set E (plus §6.6); forms → option set F; post-install
  state → option set D; lifecycle/repair → option set G; value
  question → option set C; and one bullet bundles three distinct
  items — "no preflight …; no failure taxonomy; no acceptance
  criteria, test matrix, or rollback" — "answered by sections 6.1,
  6.2, 8, and 10", i.e. two of its four answers are outside the
  spec part. By §4's own rule, those are gaps, not answers.
- **Evidence:** 21 bullets counted in lines 199–277 against the
  "16 blockers" claim at line 201; the "Answered by option set …"
  endings throughout §4; the bundled bullet at §4 (preflight /
  taxonomy / acceptance / test matrix / rollback).

#### B3 — The §6.8 state matrix is incomplete and presupposes an undecided option
- **Severity:** Blocker
- **Section / lines:** §6.8, lines 632–655; §6.4, lines 525–554;
  §6.7, lines 615–630; §5.1, lines 280–297.
- **What is structurally wrong:** The matrix is presented as the
  rows "the UI must render correctly", but it omits most of the
  product of its own axes, including:
  - Enabled = yes × Backend = not installed × install job =
    running / failed / stopped / interrupted (only the "none" row
    is given, line 650);
  - Enabled = yes × Backend = installed but not loadable (any job);
  - Backend = available with an install job of failed / stopped
    (a failed reinstall/repair — only "running (reinstall/repair)"
    is given, line 655);
  - Backend = available with install job = none (a pre-existing
    install that predates any install job — the matrix preamble
    says the job "is none unless an install has run", which makes
    this the common case, yet it has no row);
  - install job = verifying / warming-up / preflight / queued or
    waiting — §6.4 explicitly allows a blocked install to be
    "queued or refused" (line 545), and §6.3 names phases, but none
    of these values appears as an install-job value in the matrix;
  - a draining grade blocking an install (§6.4), which is neither
    a grade-job state nor an install-job state in the matrix;
  - a repair in progress on an "installed but not loadable"
    backend (the repair row, line 652, covers only "failed (verify)").
  - The matrix also presupposes option A3: the "available" row
    (line 647) renders "Backend available, plus checkpoint state
    (downloaded / downloads on first grade)", which is exactly the
    two-line rendering that §5.1 calls its default and that option
    set A is supposed to decide. Under A1 that second line is not
    specified, so the matrix cannot be implemented as written
    without first deciding A.
- **Evidence:** The 10 rows at lines 639–655 against the axes the
  matrix itself declares (Enabled × Backend × Install job); §6.4
  line 545 ("queued or refused"); §5.1 lines 293–297 (default =
  layer-2 flip + separate checkpoint line).

#### B4 — There is no Proposed Architecture section, and ownership is hedged or delegated to the implementer
- **Severity:** Blocker
- **Section / lines:** §5–§7 passim; §6.4 line 531; §5.3 lines
  344–349; §5.4 lines 384–389; §6.4 line 545; §6.11 lines 693–704.
- **What is structurally wrong:** The standard requires a Proposed
  Architecture (component layout, data flows, ecosystem integration).
  Sections 5–7 are a sequence of behaviours, not an architecture:
  there is no component list, no data-flow diagram or description,
  and no ownership table for the new runner, the coordination lock,
  the durable record, and the status surface. Where ownership is
  named, it is hedged or handed off:
  - §6.4: the coordination point is "owned by `DashboardCore` (or
    its equivalent)" — "or its equivalent" is not an owner.
  - §5.3: "The exact detection call is an implementation detail the
    implementer must name in code review", for a decision the same
    section calls "the 2.5 GB mistake this section exists to
    prevent". A spec that leaves its highest-cost decision to code
    review has hidden the decision, not surfaced it.
  - §6.4: queuing vs refusal is left for "the implementer [to] pick
    one".
  - §6.11: shutdown order is stated three ways in one paragraph —
    "after the browser installer and before or after the grade
    runner", "the implementer must state it", then "grade shutdown
    precedes install shutdown". That is not a contract; it is a
    hedge followed by a decision, with no indication of which
    sentence governs.
  - §5.4 branch 2: the dedicated venv's "exact path is an
    implementation detail", yet §6.5/§6.9 require that same path to
    be fixed, recorded in the durable record, and re-discovered on
    the next start. A path cannot be both fixed-by-spec and
    TBD-by-implementer.
- **Evidence:** The lines cited; the absence of any architecture /
  component / data-flow heading among lines 13–764 (headings are
  problem, current state, goals, critique, installed-meaning, job,
  security — no architecture heading).

#### B5 — The fixed command is never stated, and "installed" is defined for only one of the two load paths in code
- **Severity:** Blocker
- **Section / lines:** §5.3 lines 335–342; §7.1 lines 708–733;
  §5.1 lines 280–297; §2 lines 87–96.
- **What is structurally wrong:** §7.1 enforces a "constant argv
  sequence" and requires a test that `jev-agent` appears "nowhere in
  the built argv, on every branch", but the spec never prints the
  argv. §5.3 describes a *sequence* of two pip invocations (torch
  first, then `laya`), while §7.1 speaks of "the install command"
  and "the built argv" in the singular. A reviewer cannot tell
  whether the test applies to one argv, to each of two argv lists,
  or to their concatenation — and §6.5's job record likewise says
  "the fixed argv sequence" without resolving singular vs plural.
  Separately, §2/§5.1 define layer 1 as "`laya` is importable",
  but `backend.py`'s `load_backend` (read for this review) prefers
  a `coding_decision_agent` package when installed and only then
  falls back to the `find_spec("laya")` check; `available` tries
  that whole loader. The spec's definition of "installed" therefore
  does not cover the loader's first branch: a machine with
  `coding_decision_agent` but no `laya` is "installed" by code and
  "not installed" by §5.1.
- **Evidence:** §5.3 lines 335–342 (two-step sequence) vs §7.1
  lines 711, 719, 243–245 in §7.1's own terms ("constant argv
  sequence", "the built argv"); `backend.py` `load_backend`:
  `import_module("coding_decision_agent")` first, `find_spec("laya")`
  second; §2 lines 87–96 describing only the `find_spec` check.

### Majors

#### M1 — The §1 glossary and field list are misplaced and already out of date within the same document
- **Severity:** Major
- **Section / lines:** §1, lines 33–76 (glossary at 33, field list
  at 70); §5.5 lines 430–451; §6.7 lines 615–630; §6.8 lines 632–655.
- **What is structurally wrong:** A glossary and a field inventory
  belong in a standalone glossary/overview section or an appendix,
  not as H3 subsections of "The problem". Worse, the field list is
  wrong as a statement of the *proposed* panel: it gives Backend
  exactly two values ("available" or "not installed", lines 74–76),
  while §5.5/§6.8 later add "installed but not loadable" and
  install-job words ("installing", "failed", "stopped",
  "interrupted", §6.7). The glossary defines "draining" but not
  "stopped", "cancelled", "interrupted", "verifying", "warming up",
  "queued", or "waiting" — i.e. it defines the one inherited grade
  word and none of the new install vocabulary the matrix depends
  on. "Restart" is used for two different events with no
  disambiguation: restarting the dashboard to pick up an installed
  backend (§5.2) vs a dashboard restart interrupting an install
  (§6.5, §6.11).
- **Evidence:** §1 lines 33–76 vs §5.5/§6.7/§6.8 values; glossary
  entries at lines 33–69 (no stopped/cancelled/interrupted entry).

#### M2 — The §5.4 deployment matrix is normative except where it matters most
- **Severity:** Major
- **Section / lines:** §5.4, lines 373–429, especially the matrix
  at lines 412–424 and the normativity clause at lines 425–429.
- **What is structurally wrong:** The clause says an implementation
  that shows the button "without the row's UI statement has not
  implemented this design". But the Docker row — the row §4 itself
  calls the contradiction at the heart of "same button everywhere"
  — has no UI statement at all: its target is "See option set G"
  and its UI column is "The ephemerality warning, or the volume
  path, per option set G". A normative matrix cannot normatively
  defer its hardest cell. The shorthand rows compound this:
  Windows service, Linux system Python and VPS rows say "As
  Windows service" for persistence and/or UI text, so three rows
  have no self-contained statement and inherit a row that is itself
  conditional ("Branch 2 … else branch 3"); the VPS row adds "plus
  the cache location" without stating that location.
- **Evidence:** Matrix lines 412–424; normativity clause lines
  425–429; "See option set G" in the Docker row.

#### M3 — The same facts are defined in four places, and two of them disagree about the log
- **Severity:** Major
- **Section / lines:** Backend definition: §1 lines 70–76, §2 lines
  81–86, §5.1 lines 280–297, §6.8 lines 632–655. Sizes/times: §2
  lines 107–112, §5.3 lines 333–371, §6.1 lines 454–480. Cache/TMPDIR:
  §2 lines 97–102, §5.3 lines 360–365, §5.4 lines 391–396, §6.1
  lines 468–470. Repair: §5.2, §5.5, §6.2. Restart: §5.2, §6.5, §6.11.
  Log: §2 lines 121–128, §6.5 lines 556–578, §6.10 lines 681–692.
- **What is structurally wrong:** None of these topics has a single
  canonical home; each is restated with slightly different scope,
  so a future edit will drift. The log is the concrete
  contradiction: §6.5 says a second install "truncates the old log
  only after copying it to the retained full log, so the
  interruption evidence is not destroyed by the retry", while §6.10
  says the full log is "capped the same way the browser precedent
  caps its log, 256 KB, when the job finishes" — a cap that
  *destroys* the tail of a retained log — and §2's precedent caps
  "when the install finishes", which for a truncated-then-copied
  log is ambiguous (finish of which install?). §6.5 also says the
  full log "is retained beyond the 30-line tail (section 6.10)",
  while §6.10 is the section that caps it. Retained-beyond-tail and
  capped-at-256 KB are compatible only with a retention policy
  (per-attempt? rolling? per-machine?) that neither section states.
- **Evidence:** The line ranges cited; §6.5's copy-before-truncate
  sentence vs §6.10's 256 KB cap sentence.

#### M4 — §6.6 is conditional dead text, but the rest of the spec assumes stop exists
- **Severity:** Major
- **Section / lines:** §6.6 lines 580–614, especially lines
  603–607; §6.2 line ~503 (stopped row); §6.8 line 644 (stopped row);
  §7.2 logging of "stop".
- **What is structurally wrong:** §6.6 defines stop per phase in
  detail, then says: "If option set E picks install-to-completion,
  this section's stop rows do not apply." A spec section that
  deletes itself on an undecided option is not a specification of
  stop; it is a draft of one branch. But §6.2's taxonomy, §6.8's
  matrix, and §7.2's attempt-event list all contain a "stopped"
  state/event unconditionally. If E2 wins, those sections do not
  merely lose a row — the stopped row, the stopped matrix row, and
  the stop event all have to be rewritten, and the "interrupted"
  state (§6.5/§6.11, shutdown-only) has to absorb the user-stop
  case it currently explicitly excludes ("not `stopped`, because
  the user did not stop it", §6.11). The dependency is not marked
  in any of those sections.
- **Evidence:** §6.6 lines 603–607 vs §6.2/§6.8/§7.2 stopped
  entries; §6.11 lines 701–704 (interrupted ≠ stopped).

#### M5 — The option sets overlap each other and contradict the normative sections that cite them
- **Severity:** Major
- **Section / lines:** §5.1 (A), §5.3 lines 351–352 and §5.4 line
  375 (H), §5.5 lines 430–451 (G), §6.4 lines 525–554 and §6.8
  (F), §3 lines 163–189 (C).
- **What is structurally wrong:** As cross-referenced from §§1–7:
  - **A vs §5.1/§6.8:** §5.1's default *is* the A3 rendering, and
    §6.8 renders it unconditionally (see B3). A1 and A3 therefore
    differ, on the spec's own text, only in whether a checkpoint
    line is rendered — yet they are presented as distinct
    definitions of "installed", not as a rendering choice.
  - **H vs §5.3/§5.4:** §5.3 says honest sizes "must [be stated]
    before the user clicks (the confirm step, or the button's
    detail text when there is no confirm step)" and §5.4 says the
    environment decision "is shown to the user before the install
    starts". An H2-style summary shown *after* the click, and an
    H3-style small-text warning, do not satisfy "before the user
    clicks" in the same sense as H1's confirm page — yet all three
    are presented as valid options with no reconciliation of which
    normative "before" sentence each one violates.
  - **G vs §5.5:** "repair" is used for three different scopes —
    repair after a failed verification (§5.2/§6.2), repair of a
    broken-but-present backend (§5.5), and the lifecycle repair
    action in option set G — with no disambiguation, and §5.5
    simultaneously gives the out-of-scope panel message while
    deferring whether repair is in scope to G.
  - **F vs §6.4:** §6.4's ledger guard covers jobs *refused*
    because an install is running; F2 (as cited) leaves *failed*
    (not refused) jobs ledgering "unavailable" as today. The spec
    never states, in one place, which failed/refused classes ledger
    and which do not, and §6.8 marks grade/route forms as "per
    option set F" on only the first two rows, leaving the forms'
    state undefined for failed/stopped/interrupted/repair rows.
  - **C vs §3:** see B1 — C's no-button option negates §3's first
    goal rather than parameterising it.
- **Evidence:** The line ranges cited; §6.8 rows 639–640 (forms
  "per option set F") vs rows 641–655 (no forms column value).

#### M6 — §6.4's coordination fallback points at a taxonomy row that does not exist
- **Severity:** Major
- **Section / lines:** §6.4 lines 525–554; §6.2 lines 481–505.
- **What is structurally wrong:** §6.4 admits the per-process lock
  does not cover two dashboard processes on one machine, then says
  that case's "failure message … is the pip failure taxonomy entry
  for the condition that surfaces (file lock, disk, or
  verification failure), not a new lock". But §6.2's taxonomy has
  no file-lock / cross-process-contention entry at all — its rows
  are network, disk full, temp full, permission, PEP 668, no wheel,
  hash mismatch, killed process, verify failure, checkpoint
  failure, user stop. So the fallback resolves to "whichever row
  happens to surface", which is precisely the unowned failure
  message §6.4 claims to avoid. Separately, §6.4's blocked-side
  behaviour is left as "queued or refused — … the implementer must
  pick one", while §6.1 lists "no grade running or draining" as a
  *preflight refusal* — so the same contention is a preflight
  refusal in §6.1, a "waiting" display in §6.4, and a queue-or-
  refuse choice in the next bullet, with no single state for it
  (see also B3: no queued/waiting value in §6.8).
- **Evidence:** §6.4 lines 550–554 (fallback) vs §6.2 table rows
  (no file-lock row); §6.1 preflight bullet vs §6.4 "waiting"
  bullet vs §6.4 queue-or-refuse bullet.

#### M7 — As a standalone spec, §§1–7 fail the standard's Alternatives Rule and its acceptance requirement
- **Severity:** Major
- **Section / lines:** §§1–7 as a whole; standard's Alternatives
  Rule and Risks & Limits; §4 lines 199–277.
- **What is structurally wrong:** The standard requires the
  specification to evaluate alternatives with documented rejection
  reasons, and to call out its constraints, failure modes, security
  implications and mitigations. Within §§1–7 there is no
  Alternatives section at all: rejected alternatives (copy button,
  guide-only, auto-install, bundling) appear only after the
  implementation plan, and the in-scope "alternatives" are the
  undecided option sets, which by definition reject nothing. Risks
  are listed in §4, but as shown in B2 their mitigations are
  largely pointers to undecided options or to sections outside the
  scope. There are likewise no acceptance criteria inside §§1–7
  (they sit after the implementation plan), so §3's goals have no
  testable form within the spec part — a reader of §§1–7 alone
  cannot tell what would count as the feature working. Either the
  scope boundary is wrong (alternatives and acceptance belong
  *before* the implementation plan), or §§1–7 must be marked as
  an incomplete spec, not a reviewable one.
- **Evidence:** Absence of any Alternatives or Acceptance heading
  in lines 1–764; §4's deferral pattern (B2).

#### M8 — §2's "what exists today" mixes verified inventory with unverifiable and conditional claims
- **Severity:** Major
- **Section / lines:** §2 lines 78–160, especially lines 147–150
  (routes), 119–128 (browser runner), 141 (settings toggle).
- **What is structurally wrong:** Three structural defects in the
  inventory the rest of the spec builds on:
  - The route list is cited to `routes_code.py` *and* `app.py`,
    but `routes_code.py` as read contains handler methods, not a
    route table — the URL paths themselves rest on a file that is
    not among the spec's named inputs in the header (header says
    "the code files named in section 2", yet §2 also names
    `app.py` without it being evidenced in the allowed set). The
    inventory therefore cannot be checked from the spec's own
    input list.
  - The browser precedent is described as repo-scoped (log under
    the repo's `.jev/`, §2), then §6.9 forbids reusing that location
    for the install log, while §6.10 reuses its cap ("capped the
    same way") and §6.3 reuses its tail (30 lines). The spec never
    states, in one place, which browser-runner properties carry over
    and which do not — and that boundary is exactly what option
    set B (outside scope) decides, so §§6.3/6.10's normative numbers
    are conditional without being marked conditional.
  - §2's settings-toggle note ("No runner today writes config;
    the install job must not silently become the first one to do
    so without a decision") is a decision *about* a decision: it
    forbids a silent outcome but does not state the outcome, and
    it is the only place in §§1–7 where the config-writing boundary
    is stated, even though §6.8 and the post-install state depend
    on it.
- **Evidence:** `routes_code.py` method names (`code_grade_page`,
  `code_grade_grade`, `code_grade_route`, `code_grade_status`,
  `code_grade_live`, `code_grade_cancel`) with no URL table in
  that file; §2 lines 119–128 vs §6.9/§6.10; §2 line 141.

#### M9 — Phase vocabulary is never enumerated, and "probe" vs "warm" are conflated
- **Severity:** Major
- **Section / lines:** §6.3 lines 506–524; §6.6 lines 580–614;
  §6.7 lines 615–630; §6.5 lines 556–578; §5.2 lines 299–332.
- **What is structurally wrong:** §6.5's durable record stores "the
  current phase", but no section enumerates the phase set. §6.3
  names Preflight / Downloading and installing / Verifying /
  Warming up; §6.6 uses the same four names for stop, but §6.7's
  panel words are different again ("installing", "failed",
  "stopped", "interrupted") — a job *state* vocabulary mixed with
  a *phase* vocabulary, with no mapping between them (is
  "installing" the Downloading phase, or the whole job? Does a job
  in Verifying show as "installing"?). §5.2 step 3 compounds this
  by calling a checkpoint *existence check* a "Checkpoint probe
  (only when option set A includes pre-warming)" and then saying
  "It does not download it unless pre-warming is the chosen
  option" — a probe that downloads is not a probe, it is the
  warm-up phase of §6.3, and tying a read-only existence check to
  the pre-warming option makes the checkpoint state unknowable
  under A1/A3, which is exactly when the panel needs it (see B3).
- **Evidence:** §6.3 phase bullets vs §6.7 short-state words vs
  §6.5 "current phase" with no enumeration; §5.2 step 3 wording.

### Nits

#### N1 — Front-matter chronology and stage count do not line up
- **Severity:** Nit
- **Section / lines:** Header, lines 3–11.
- **What is structurally wrong:** The document is dated 2026-10-29
  (line 4) but lists as an input a brainstorm dated 2026-10-30
  (lines 8–9) — an input dated after the document. The header also
  says the review directory holds "five Goldfish stages" (line 10)
  while this review is Stage 7. Either the stage numbering, the
  header, or both are stale; a front matter that cannot be trusted
  on dates and counts undermines the "records how each finding was
  handled" claim in the same line.
- **Evidence:** Lines 4, 8–10.

#### N2 — Glossary placement and the lone H4
- **Severity:** Nit
- **Section / lines:** §1 lines 33–76; §5.4 line 412.
- **What is structurally wrong:** The only H4 in §§1–7 is
  "Deployment matrix" under §5.4, and the only H3s under §1 are a
  glossary and a field list. The hierarchy is legal (no level is
  skipped) but asymmetric: the document's most normative table is
  buried one level deeper than every other subsection, and its
  glossary is buried under "The problem". Move the glossary to a
  standalone section (or appendix) and promote the matrix, or
  demote the §1 subsections — the current shape signals that both
  are afterthoughts, when the rest of the spec depends on them.
- **Evidence:** Heading list for lines 1–764 (H4 occurs once, at
  line 412).

#### N3 — The title promises "one-click" while the spec defers to a two-click option
- **Severity:** Nit
- **Section / lines:** Title, line 1; §5.3 line 351; §7.2 line 762
  (confirm step as option set H).
- **What is structurally wrong:** The title is "one-click backend
  install", but §§1–7 leave the confirm step to option set H, whose
  confirm-first branch is by its own description a two-click flow.
  A title that asserts the outcome of an undecided option is
  another instance of B1 at small scale; retitle to "Install button"
  until H is decided, or mark the title conditional.
- **Evidence:** Line 1 vs the H cross-reference at line 762.

#### N4 — §6.8's "either" row and "done" value hide two distinctions
- **Severity:** Nit
- **Section / lines:** §6.8 lines 632–655, especially lines 647,
  655.
- **What is structurally wrong:** The last row collapses Enabled
  to "either", breaking the matrix's own Enabled axis, and "done"
  is used both for a completed install (line 647) and implicitly
  for a pre-existing backend with no job (no row — see B3). If
  "done" means "an install job finished", it cannot also mean
  "backend present"; the matrix needs a separate value (or row)
  for pre-existing availability.
- **Evidence:** Lines 647, 655.

#### N5 — Three names for the fallback, four words for the checkpoint step
- **Severity:** Nit
- **Section / lines:** §5.4 line 396 ("guide fallback"), §6.2
  line ~493 and §7.1 line 729 ("branch-3 command"); §5.2 line 316
  ("Checkpoint probe"), §6.3 line 517 ("Warming up"), §6.6 line
  596 ("Warming up"), §5.1 ("checkpoint … reported separately").
- **What is structurally wrong:** The branch-3 outcome is called
  the "guide fallback", the "branch-3 command", and (via option
  set C) the guide — none of which is in the §1 glossary. The
  checkpoint step is called a probe, pre-warming, warm-up, and
  warming up. Pick one term per concept and put both in the
  glossary (see M1); as written, a reader searching for "guide"
  will miss the branch-3 command rows, and vice versa.
- **Evidence:** The line citations above.

#### N6 — §7.2 presents absences as decisions and leaves "requesting context" undefined
- **Severity:** Nit
- **Section / lines:** §7.2 lines 735–764, especially lines
  749–751, 757–760.
- **What is structurally wrong:** "No additional rate limit is
  specified" is not a rate-limiting policy; it is the absence of
  one, stated in a section whose job is to say who may press
  Install and what happens otherwise. Likewise, attempt events are
  logged "with the requesting context the dashboard already
  records" — but §§1–7 never say what that context is (token?
  loopback? repo? nothing?), so the logging requirement cannot be
  checked. The double-submit rule is also stated here, in §6.8,
  and in §6.4 — three homes for one guard (see M3).
- **Evidence:** Lines 749–751, 757–760; double-submit at §6.4,
  §6.8, §7.2.

#### N7 — §5.3's "honest sizes" list contains a non-size
- **Severity:** Nit
- **Section / lines:** §5.3 lines 351–359.
- **What is structurally wrong:** The list the UI "must state
  before the user clicks" gives torch as "about 2.5 GB (default
  wheel) or the CPU wheel size for the CPU path" — "the CPU wheel
  size" is a pointer to a number, not a number, and `laya` plus
  transformers as "the size pip reports … or 'a few hundred MB'
  when it cannot be known". A list that demands honest sizes
  before the click cannot itself contain placeholders for two of
  its four entries; either state the CPU wheel size (or a range)
  or mark the list as "sizes to be filled in by preflight" and say
  where preflight gets them.
- **Evidence:** Lines 354–357.

## What must change before this spec part is approvable (structural, not editorial)

1. Decide the option sets, or rewrite §§1–7 so every normative
   sentence is explicitly conditional on the option it depends on
   — a spec cannot be both normative and undecided (B1, B2, M5).
2. Replace §6.8 with a complete state machine: enumerated job
   states, enumerated phases, a state↔phase mapping, and every
   Enabled × Backend × job combination, including pre-existing
   availability, queued/waiting, draining-blocked, and failed
   reinstall/repair (B3, M9).
3. Add a real Proposed Architecture section (components, data flow,
   ownership without "or its equivalent") and move Alternatives and
   Acceptance before the implementation plan, or explicitly scope
   §§1–7 as incomplete (B4, M7).
4. State the fixed argv (one list or two, per branch), and fix the
   definition of "installed" to cover the `coding_decision_agent`
   load path in `backend.py` (B5).
5. Give the deployment matrix a self-contained Docker row and
   remove the "As Windows service" shorthand rows (M2), and give
   every duplicated topic (Backend value, sizes, cache/TMPDIR,
   repair, restart, log retention) a single canonical home (M3).

## Counts

- **Blockers:** 5 (B1–B5)
- **Majors:** 9 (M1–M9)
- **Nits:** 7 (N1–N7)
- **Total:** 21
