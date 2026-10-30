# EGM Stage 9 — Implementation Readiness Test, style lens

**Target:** `docs/designs/cda-install-button/design.md`, section 8 (lines 765–855)
and section 10 (lines 1131–1191).
**Context used only:** sections 1–7 and 9 of the same document, plus
`concepts_brief.md` in the same directory. No other files were relied on
for findings, and no code fact is asserted that was not read in the
design itself.
**Method:** every finding below is a language defect in section 8 or 10:
a vague verb that hides a decision, an unmeasurable criterion, a test
item with no stated assertion, a rollback step that is not a command,
or a term used in section 8/10 with a different meaning than in
sections 1–7. Where section 8 or 10 contradicts an earlier section, the
earlier section is cited, not assumed.

**Counts: 7 Blockers, 12 Majors, 6 Nits (25 findings).**

The central defect, stated plainly: sections 8 and 10 are written as a
decided contract and as measurable criteria, while their content is
still conditional on undecided option sets A, B, C, D, E, G and H, and
while section 10 contradicts sections 5.4, 5.5 and 6.8 on the most
checkable point in the whole plan — whether a pip command may appear
on the page at all. An implementer handed only sections 8 and 10 could
not determine what to build, what to test, or what "done" means, and
could satisfy section 10's letter while violating sections 5.4–5.5.

---

## Blockers

### B1. Section 8 calls itself a contract and section 10 calls its criteria measurable, while both depend on option sets no human has picked
- **Scope:** section 8, lines 767–769; section 10, lines 1133–1138.
- **What is wrong:** A contract cannot be conditional on seven undecided
  choices, and a criterion cannot be measurable when its success
  condition is a pointer to an undecided option. The final bullet of
  section 8 admits the document must be updated with decisions "before
  implementation starts," which concedes that the list above it is not
  yet a contract at all — it is a draft of a contract.
- **Evidence:**
  > "The list below is the contract for the implementation phase: every file it touches, and why."
  > "### Acceptance criteria (measurable)"
  > "the status panel reports installed per option set A."
- **Why it blocks:** Option sets A (what "installed" means), B (runner
  shape), C (where the button shows), D (post-install enablement),
  E (stop), G (lifecycle) and H (confirm) each change the files, routes,
  tests and criteria in sections 8 and 10. Until they are picked, the
  "contract" names files that may not be touched (confirm route,
  stop route, scaffold extraction) and criteria that may be impossible
  (one-click install under H1, no pip command under C3 — see B2, B5).

### B2. Section 10's "no pip command" criterion is contradicted by sections 5.4, 5.5 and by section 10's own rollback list
- **Scope:** section 10, lines 1150–1151, against section 5.4 branch 3,
  section 5.5, and section 10, lines 1180–1183.
- **What is wrong:** The criterion is stated absolutely, but the design
  itself requires a pip command to be shown in at least three cases:
  the branch-3 guide fallback (section 5.4: "the page shows the
  environment-specific command"), the out-of-scope repair message
  (section 5.5: "Reinstall it with the command below"), and the
  rollback bullets (uninstall, cache purge). "Pip command" is also
  never defined: does it mean the substring `pip install`, any pip
  invocation, or only the original `jev-agent[code-orchestrator]`
  instruction? Under a literal reading, the rollback bullets fail the
  acceptance criterion.
- **Evidence:**
  > "The three pip-instruction surfaces show no pip command after the change."
  > "Remove a broken backend: the pip uninstall command for `laya` in the detected environment."
  > "Clear a corrupt pip cache: the pip cache purge command for the detected environment."
- **Why it blocks:** A tester cannot write a passing assertion for this
  criterion without first deciding which of the design's own required
  pip commands are exempt — a decision the criterion hides.

### B3. The rollback section contains no literal commands, and one of its steps cites a volume that option set G never defines
- **Scope:** section 10, lines 1177–1190.
- **What is wrong:** The task of this section is "manual recovery
  commands," but every bullet is a description of a command, not a
  command: "the pip uninstall command," "the pip cache purge command,"
  "remove the model directory," "free space, then retry." No argv, no
  path, no platform variant is given, so nothing here is executable
  or copyable. The Docker bullet is worse: it tells the user to "add
  the volume from option set G," but option set G (G1, G2, G3) defines
  no volume — no name, no mount path, no image-rebuild step. The
  reference dangles.
- **Evidence:**
  > "Manual recovery commands, per failure the button cannot fix (environment-specific; the page shows the one for the detected environment):"
  > "Remove a broken backend: the pip uninstall command for `laya` in the detected environment."
  > "Recover from a Docker wipe: re-run the install after recreate, or add the volume from option set G and reinstall once."
- **Why it blocks:** Rollback is the section a user reads when the
  button has already failed. Paraphrases of commands are not recovery
  steps, and a dangling cross-reference sends the user to an option
  set that does not answer the question.

### B4. Section 8 claims the shutdown order is "stated in section 6.11"; section 6.11 does not state it
- **Scope:** section 8, lines 779–783, against section 6.11.
- **What is wrong:** Section 6.11 says the install runner is stopped
  "after the browser installer and before or after the grade runner,"
  and that "the implementer must state it," then argues grade shutdown
  should precede install shutdown. That is a recommendation plus an
  open decision, not a stated order. Section 8 converts it into a
  settled fact ("in the order stated in section 6.11") and adds a
  second vague verb ("shutdown order is a `DashboardCore` decision"),
  so the plan simultaneously says the order is decided, is undecided,
  and belongs to a component rather than to the design.
- **Evidence:**
  > "wire it into `shutdown_jobs()` in the order stated in section 6.11, and own the install/grade coordination lock from section 6.4. Rationale: coordination must live above both runners, and shutdown order is a `DashboardCore` decision."
- **Why it blocks:** Shutdown order is load-bearing here: section 6.4
  and 6.11 both say terminating pip while a grade holds torch files is
  the hazard the order exists to prevent. A contract that cannot say
  which runner stops first is not a contract for `core.py`.

### B5. Section 10's first criterion uses "installed" as if it were a panel value, and "one click" as if H1 did not exist
- **Scope:** section 10, lines 1135–1138.
- **What is wrong:** Two term drifts in one bullet. First, section 1
  defines the Backend panel value as "available" versus "not
  installed," and section 6.8's state matrix renders "Backend
  available," "not installed," and "installed but not loadable" — never
  a bare panel value "installed." Section 10's "reports installed"
  therefore does not name an observable string; a tester must guess
  whether it means Backend = available, a literal "installed" label,
  or layer 2 under option set A. Second, "one click (plus a confirm
  click if option set H picks one)" redefines "one click" to mean
  one-or-two clicks. Option set H1 itself notes that under
  confirm-first, "one-click install" becomes "two-click install" and
  that the acceptance test's "one click" fails if read word for word.
  The criterion reproduces exactly that failure mode instead of fixing
  it.
- **Evidence:**
  > "From a fresh \"not installed\" state on the Mac live dashboard, one click (plus a confirm click if option set H picks one) installs the backend with no terminal use, and the status panel reports installed per option set A."
- **Why it blocks:** The criterion is the one live-verification test
  the implementation phase is told to run (lines 1164–1167). If its
  success string and its click count are both undefined, the live
  verification cannot pass or fail on its own terms.

### B6. The "install route pair" names one path for two routes, names no path for three more routes, and overstates the form protection
- **Scope:** section 8, lines 784–790.
- **What is wrong:** A pair needs two paths; the bullet gives one
  (`/code-grade/install`) followed by "status plus live fragment,"
  which reads as one path with two renderings. The live-fragment path
  is never written (section 6.7's grade precedent would suggest
  something like `/code-grade/install/live`, but section 8 does not
  say so, and this report does not invent it). The confirm, stop and
  repair routes are named as concepts with no paths at all, although
  the repair route is listed unconditionally while section 5.5 makes
  repair's shape depend on option set G. Finally, "under the same
  `checked_form` protection" misdescribes the mechanism: section 2
  says `checked_form` checks the per-session token on form posts.
  A status page and a polling fragment are not form posts; claiming
  the same protection for the whole pair hides whether GET polling
  is token-gated, CSRF-checked, or neither.
- **Evidence:**
  > "the install route pair (`/code-grade/install`, status plus live fragment), the confirm route if option set H picks one, the stop route if option set E picks one, and the repair route from section 5.5. Rationale: all install endpoints live with the code-grade routes, under the same `checked_form` protection."
- **Why it blocks:** Routes are the implementer's first deliverable
  from this bullet. Without paths and without a correct statement of
  which protection applies to which method, the routes cannot be
  built or tested from section 8 alone.

### B7. Section 8's runner file is undecided across option set B, and the shared-scaffold bullet omits the files the scaffold must change
- **Scope:** section 8, lines 771–778 and 815–819.
- **What is wrong:** The first bullet says the runner is "(new, or the
  shared-scaffold file from option set B)," but option set B offers
  three different file outcomes: B1 extracts a scaffold (a new shared
  file plus changes to the two existing runners), B2 is a deliberate
  third copy (a new file), and B3 generalizes the existing browser
  runner (no new runner file at all). "The shared-scaffold file" does
  not name any of these. Worse, the `browser_install.py` bullet says
  only "extract the shared job scaffold … that all three runners
  use," but section 8 never lists modifying `code_grade_runner.py`
  or the browser runner itself to adopt the scaffold — the very
  regression risk B1's trade-off warns about. Section 8's claim to
  list "every file it touches" is therefore false on its own terms
  under B1.
- **Evidence:**
  > "`src/jev_agent/dashboard/cda_install_runner.py` (new, or the shared-scaffold file from option set B): the install job"
  > "extract the shared job scaffold (lock, thread, state, timestamps, `busy()`, `status()`, shutdown hook, log cap) that all three runners use."
- **Why it blocks:** The implementer cannot know whether to create a
  file, generalize an existing one, or refactor two working runners
  — and the test plan in section 8 has no regression item for the
  two runners a B1 refactor would touch.

---

## Majors

### M1. "Links to the install action" and "links to `/code-grade`" are two different, unnamed destinations
- **Scope:** section 8, lines 800–809.
- **What is wrong:** The grade-runner surface "links to the install
  action instead" while the Learning surface "links to `/code-grade`
  instead." "Install action" is not a route, a page, or a button name
  anywhere in sections 1–7: the candidates are the `/code-grade` page
  (which holds the button), the `/code-grade/install` status page,
  and a confirm route that may not exist (option set H). The two
  bullets therefore send two surfaces to destinations that may or
  may not be the same place, with no way to tell from section 8.
- **Evidence:**
  > "`unavailable_message()` stops printing the pip command and links to the install action instead"
  > "the Ratings tab empty state stops printing the pip command and links to `/code-grade` instead."

### M2. The live fragment template is the only template in section 8 without a filename
- **Scope:** section 8, lines 795–799.
- **What is wrong:** The status template is named
  (`code_grade_install_status.html`); its fragment is "a live fragment
  template (new)" with no filename, no path, and no statement of
  whether it mirrors the grade precedent's underscore-prefixed
  partial naming (`_code_grade_live.html`). "Mirroring" is doing all
  the work of a specification here and specifies nothing checkable.
- **Evidence:**
  > "`src/jev_agent/dashboard/templates/code_grade_install_status.html` (new) and a live fragment template (new): the install status page and its polling fragment, mirroring `code_grade_status.html` and `_code_grade_live.html`."

### M3. "Stops printing" and "retirement" never say what replaces the pip instruction, or when the removal applies
- **Scope:** section 8, lines 791–793, 800–809.
- **What is wrong:** "Stops printing" states a negative (the old
  string goes away) without stating the positive: is the text deleted,
  replaced by the button, replaced by a link, or hidden only while an
  install is possible? Section 5.5 says the button is keyed on
  "backend not usable," not on "missing," and section 5.4 branch 3
  requires a command to be shown — so removal cannot be unconditional
  in the way "retirement" implies. A test can assert string absence
  only if the plan says which string, on which surface, under which
  backend state; section 8 says none of these.
- **Evidence:**
  > "the retirement of the pip instruction."
  > "`unavailable_message()` stops printing the pip command and links to the install action instead"

### M4. "Modify only if needed" gives no criterion for needing the backend-construction entry point
- **Scope:** section 8, lines 810–814.
- **What is wrong:** The bullet is a conditional with an untestable
  condition: "if the loadability probe needs a backend-construction
  entry point that does not load the checkpoint, add it here;
  otherwise no change." It does not say how the implementer
  determines need (section 5.2 already says the probe constructs the
  backend "if the backend API allows it; otherwise the probe stops
  at the import step"), does not name the entry point, and does not
  say what the probe does if the entry point cannot be added. This is
  a decision deferred into the implementation phase disguised as a
  plan item.
- **Evidence:**
  > "`src/jev_agent/code_orchestrator/backend.py` (modify only if needed): if the loadability probe needs a backend-construction entry point that does not load the checkpoint, add it here; otherwise no change."

### M5. The test bullets list categories, not assertions — and the matrix test conflates two different matrices
- **Scope:** section 8, lines 820–833.
- **What is wrong:** No test bullet states its assertion. "Preflight
  refusal tests" does not say which of section 6.1's checks, or what
  refusal string each produces. "Failure-classification tests" does
  not map to section 6.2's eleven rows. "Stop-per-phase tests" is
  listed unconditionally although option set E may pick no stop at
  all (E2) or phase-gated stop (E3). The routes bullet says "CSRF
  refusal" without a response (status? redirect? message?) and
  "single-flight double-submit" without section 6.8's stated outcome
  (the second submit sees the running job's status, not an error).
  The matrix bullet says "the platform × deployment decision-tree
  tests from section 10," but section 10's matrix is platform ×
  failure, while the decision tree lives in section 5.4 — two
  different tables with different cells, merged into one phrase.
  "With the environment detection stubbed per cell" is also
  self-defeating as written: stubbing detection per cell means the
  tests cannot catch the misclassification failure mode option set C
  names as its own failure case.
- **Evidence:**
  > "argv tests (including \"jev-agent never in argv\" on every branch), preflight refusal tests, failure-classification tests, stop-per-phase tests, restart/interrupted tests, and the coordination tests"
  > "route tests for the install pair, CSRF refusal, single-flight double-submit, and the three retired pip-instruction surfaces."
  > "the platform × deployment decision-tree tests from section 10, with the environment detection stubbed per cell."

### M6. Section 8 omits the implementation work of option sets F, D and G2, despite claiming to list every file
- **Scope:** section 8, lines 765–849 (omission, evidenced by what the
  bullets do name).
- **What is wrong:** Option set F changes the grade/route forms
  (F1 disables them; F2 keeps them live with a changed failure path),
  but no bullet itemizes a form change — the `code_grade.html` bullet
  names only "the Install button, the install banner, the Backend
  row's install states, and the retirement of the pip instruction."
  Option set D2 makes the install job a config writer and D3 adds a
  "Turn on gate grading" button, but no bullet names the settings
  file, the settings route reuse, or the new button. Option set G2
  adds an Uninstall action and an installed-version display, but no
  bullet names an uninstall route or a version-display change; only
  the repair route appears, and it appears unconditionally (see B6).
  A plan that omits three option sets' deliverables is not "every
  file it touches."
- **Evidence:**
  > "The list below is the contract for the implementation phase: every file it touches, and why."
  > "the Install button, the install banner, the Backend row's install states, and the retirement of the pip instruction."

### M7. The progress and enablement criteria are unmeasured: no location, no interval, no mechanism
- **Scope:** section 10, lines 1139–1143.
- **What is wrong:** "Visible in the browser" does not say where:
  section 6.7 distinguishes the install status page, the main-page
  banner, and the Backend row's short state word, but the criterion
  collapses them. "Closing the tab loses nothing" is absolute and
  unmeasurable: nothing — the job, the log tail, the phase, the
  elapsed time, the confirm state? Section 6.5's durable record
  covers the job, not every UI state, and its own fallback says that
  without a durable record the restart criterion fails — a
  qualification the criterion omits. "Grading can be enabled from
  the UI per option set D (or the panel states the Settings path,
  per D1)" collapses three different mechanisms (D1 states a path
  and enables nothing from the page; D2 auto-enables; D3 adds a
  button) into one sentence that is true under all of them and
  therefore tests none of them.
- **Evidence:**
  > "Progress (phase, elapsed time, log tail), success, and failure states are all visible in the browser, and closing the tab loses nothing."
  > "After install, grading can be enabled from the UI per option set D (or the panel states the Settings path, per D1)."

### M8. "While the backend is missing" narrows section 5.5's trigger, and "a working retry" is never defined
- **Scope:** section 10, lines 1144–1151.
- **What is wrong:** Section 5.5 keys the button on "backend not
  usable," expressly wider than "missing": absent or unloadable, and
  present-but-failing-loadability. The criterion's "while the backend
  is missing" drops the unloadable and broken-but-present cases, so
  the no-guessed-grades guarantee is stated for a subset of the
  states the design actually handles. "A working retry" is equally
  empty: section 6.6 defines retry as re-running the full sequence
  from preflight with the UI copy "Retry install," but the criterion
  does not say whether "working" means the retry starts, re-runs
  preflight, or succeeds — nor how long after a restart the
  `interrupted` state must appear before "stuck running" is declared.
  "No install outcome is written to the grade ledger" states no
  verification method (ledger file inspection? query?) either.
- **Evidence:**
  > "No guessed grades while the backend is missing (existing behavior, unchanged), and no install outcome is written to the grade ledger."
  > "a dashboard restart mid-install yields the `interrupted` state and a working retry, not a stuck \"running\"."

### M9. The test matrix's platform list drifts from section 5.4, its cells overlap, and its live verification covers a fraction of its own scope
- **Scope:** section 10, lines 1155–1167.
- **What is wrong:** Three defects. (1) Taxonomy drift: section 5.4's
  matrix has eight deployment rows and no Mac system-Python row;
  section 10 lists nine platforms including "Mac system Python,"
  which has no persistence answer and no UI statement anywhere in
  sections 5.4 or 10. "Mac venv" also splits section 5.4's combined
  "Mac, venv or editable checkout" row without explanation.
  (2) Overlapping cells: "verification failure after pip exit 0" is
  already a row of section 6.2's failure taxonomy ("Pip exit 0 but
  verification fails"), and "checkpoint-cache unwritable" overlaps
  both section 6.1 preflight (writable cache) and section 6.2's
  checkpoint-download row, while "TMPDIR on a small tmpfs" overlaps
  the temp-directory-full row. The matrix therefore cannot be
  counted: nine platforms × how many distinct failure cells? The
  plan never says. (3) Live verification contradicts the matrix's
  scope: it covers only the Mac, and only one failure injection
  ("network off") with retry, while the matrix claims nine platforms
  crossed with every failure class. No other platform and no other
  failure ever gets a live run under this section.
- **Evidence:**
  > "Platforms: Mac venv, Mac uv tool environment, Mac system Python, Windows user install, Windows service, Linux venv, Linux system Python (PEP 668), VPS service account, Docker. Each crossed with: success; each failure class from section 6.2; stop during install (where option set E provides one); restart during install; grade during install; double-submit; partial-install recovery; verification failure after pip exit 0; checkpoint-cache unwritable; TMPDIR on a small tmpfs."
  > "Live verification on the Mac (implementation phase only) covers: fresh \"not installed\" state, one-click install, progress visibility, success flip, grading enablement, and one failure injection (network off) with retry."

### M10. Rollback conflates retry with undo, and "disk spent" contradicts the uninstall bullet above it
- **Scope:** section 10, lines 1171–1176.
- **What is wrong:** "A failed or stopped install" is "undone" "by
  retry" — but retry is not undo, it is a second attempt that leaves
  the first attempt's partial packages in place until the next pip
  run overwrites them (section 6.6's own account). "By removing the
  installed packages" is plural, while option set G2's uninstall
  removes `laya` only ("never torch alone, never `jev-agent`") and
  keeps the checkpoint cache; the rollback bullet does not say
  whether torch or the checkpoint survive an undo. Then "what cannot
  be undone: time and disk spent on a download" contradicts the
  uninstall bullet: uninstalling does free the disk the download
  spent, so disk is at least partly undoable. The sentence is true
  only for time, and only if "disk spent" means something narrower
  than disk occupied — which it does not say.
- **Evidence:**
  > "What can be undone: a failed or stopped install, by retry or by removing the installed packages (see option set G for uninstall)."
  > "What cannot be undone: time and disk spent on a download; a checkpoint download that filled a disk (freeing space is manual)."

### M11. The documentation bullets list option-dependent topics as if they were settled content
- **Scope:** section 8, lines 834–845.
- **What is wrong:** "The fixed sequence per platform" conflates two
  different axes: section 7 builds argv from server-side detection
  (CPU vs default torch path), while section 5.4's matrix is by
  deployment shape. "Per platform" names neither axis. "The Docker
  persistence answer" and "the manual recovery commands from section
  10" are both undecided (G, and B3's missing commands). "The
  uv-tool reinstall caveat from section 5.4" quotes no caveat text.
  The `dashboard.md` bullet is the weakest item in the plan: "if
  that doc describes the page" is a condition the plan never
  resolves, so the bullet may name a file that needs no change, or a
  change to a file whose existence the plan has not established.
- **Evidence:**
  > "`docs/code-orchestrator.md` (modify): the install button, the fixed sequence per platform, the torch sizes, the Docker persistence answer, and the manual recovery commands from section 10."
  > "`docs/dashboard.md` (modify): the Code Grade page's install states, if that doc describes the page."

### M12. "Everything else is wiring" dismisses the plan's own non-runner work with a vague verb
- **Scope:** section 8, lines 777–778.
- **What is wrong:** The runner bullet's rationale claims "this is
  the only new stateful component; everything else is wiring." But
  section 8's own list includes two more new templates, a durable
  job record, a coordination lock owned by `DashboardCore`, three
  new test files, a possible scaffold refactor of two working
  runners, and three documentation updates. None of that is wiring;
  calling it wiring tells the implementer that the hard decisions
  (lock scope, record location, scaffold adoption) are trivial
  connective work, when sections 6.4, 6.5 and option set B treat them
  as the design's central risks.
- **Evidence:**
  > "Rationale: this is the only new stateful component; everything else is wiring."

---

## Nits

### N1. "Retirement" is a euphemism for a deletion or replacement that is never named
- **Scope:** section 8, line 793.
- **Evidence:** "the retirement of the pip instruction."
- **Why:** "Retire" sounds like a lifecycle event. The plan means
  remove the text, replace it with a button or a link, or hide it
  conditionally — three different edits. Use the verb that names the
  edit.

### N2. Two documentation rationales are tautologies, not reasons
- **Scope:** section 8, lines 837–838 and 843–845.
- **Evidence:**
  > "Rationale: the doc is the surface users read when the UI message is not enough."
  > "Rationale: dashboard docs must match the shipped UI."
- **Why:** Both restate that documentation should document. Neither
  says what must change in the doc, which is the only information a
  rationale in a file list exists to carry.

### N3. "Progress needs its own home" is metaphor, not justification
- **Scope:** section 8, lines 797–799.
- **Evidence:** "Rationale: progress needs its own home (section 6.7)."
- **Why:** The cross-reference does the justifying; the metaphor
  adds nothing. The rationale should state the reason section 6.7
  gives — that reusing `/code-grade/status` would force an install
  to fake grade fields — or be deleted.

### N4. "On every branch" is ambiguous between argv branches and code branches
- **Scope:** section 8, line 821; section 10, lines 1146–1147.
- **Evidence:**
  > "argv tests (including \"jev-agent never in argv\" on every branch)"
  > "The fixed-argv test passes on every branch: `jev-agent` never appears in the built argv."
- **Why:** Section 7.1's decision tree has branches (CPU vs default
  torch path, `laya` version policy under G). "Every branch" most
  plausibly means those, but the phrase also reads as every code
  branch or every test branch. Name the decision tree's branches.

### N5. PEP 668 is annotated on one platform row and not on the parallel one
- **Scope:** section 10, lines 1155–1157.
- **Evidence:** "Mac system Python, Windows user install, Windows service, Linux venv, Linux system Python (PEP 668), VPS service account, Docker."
- **Why:** Section 5.4 and 6.2 treat PEP 668 refusal as a property of
  system Python generally, not of Linux system Python specifically.
  Annotating it once implies the Mac system-Python row (itself new
  — see M9) is exempt from the refusal, or that the annotation is
  decorative. Apply it consistently or drop it.

### N6. "Free space, then retry" and "reinstall once" state no quantity and no count
- **Scope:** section 10, lines 1187–1190.
- **Evidence:**
  > "Recover from a filled disk: free space, then retry; the button re-runs preflight first."
  > "or add the volume from option set G and reinstall once."
- **Why:** "Free space" does not say how much (section 6.1's
  preflight thresholds are the obvious reference and are not cited),
  and "once" does not say once after what — once after adding the
  volume, once total, or once per recreate. Both read as complete
  instructions and are neither.

---

## What must change before sections 8 and 10 can serve as an implementation contract

1. Resolve option sets A, B, C, D, E, G and H first, and rewrite
   sections 8 and 10 against the picked options — deleting the
   branches not picked — rather than presenting conditional bullets
   as a contract.
2. Replace section 10's "no pip command" criterion with a scoped
   rule that names the exempt cases (branch-3 guide, repair message,
   rollback commands) and defines "pip command" as a testable string.
3. Replace the rollback paraphrases with literal, per-environment
   commands, and either define the Docker volume in option set G or
   delete the volume bullet.
4. State the shutdown order once, in one place, without the
   "before or after" hedge, and make section 8 cite it verbatim.
5. Give every route and template a path and filename, state which
   protection applies to GET polling versus form posts, and name
   the link destination for each of the three retired surfaces.
6. Give every test bullet its assertion, and separate the section
   5.4 decision-tree tests from the section 10 platform × failure
   tests, with a countable cell list.
