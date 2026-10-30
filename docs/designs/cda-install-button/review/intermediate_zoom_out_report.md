# EGM Stage 8 — Intermediate Zoom Out (Skeptical Architect) — cda-install-button

- **Target:** `docs/designs/cda-install-button/design.md` (full document, §§1–12, 1,263 lines)
- **Mode:** REVIEW, against `resources/document_standards/zoom-out-standard.md`
- **Method:** Read the design, `concepts_brief.md`, and the Stage 5 zoom-out,
  Stage 6 comprehension, and Stage 7 design / structural / style critic
  reports in `review/`, plus only the code files the design cites
  (`src/jev_agent/dashboard/templates/code_grade.html`,
  `src/jev_agent/code_orchestrator/backend.py`,
  `src/jev_agent/code_orchestrator/laya_backend.py`,
  `src/jev_agent/dashboard/browser_install.py`,
  `src/jev_agent/dashboard/code_grade_runner.py`,
  `src/jev_agent/dashboard/core.py`,
  `src/jev_agent/dashboard/routes_code.py`,
  `src/jev_agent/dashboard/security.py`,
  `src/jev_agent/dashboard/templates/learning.html`,
  `pyproject.toml`, `docs/code-orchestrator.md`, `docs/deploy.md`, `Dockerfile`).
  No other files were read, and no claim below rests on code outside that
  set. In particular the `resolution_report.md` named in the design header
  was not in the permitted set for this stage and was not read, so this
  report does not credit any resolution it may record.
- **What this stage is:** Not a re-litigation of Stage 5. The findings
  below are (a) new zoom-out findings exposed by the Comprehension and
  Critic stages, (b) Stage 5 findings whose verdict has changed, and
  (c) a plain statement of where each Stage 5 finding stands. Where a
  Stage 5 finding is unchanged it is listed in §2, not re-argued.
- **Counts for this report:** 5 Blockers, 7 Majors, 4 Nits.

## Verdict in one paragraph

The document's root-problem statement (§1) is still exactly right, and
its honesty about the value question (§4, §9.C) is still its best
quality. But the intermediate verdict is worse than Stage 5's, not
better. Stage 5 said the design was undecided in its largest question
(option set C). The Comprehension and Critic stages show it is worse
than undecided: it is inconsistently decided. §5.1 smuggles in a
default that implements one option while the header says no option may
be assumed; §5.3 and §7.1 give two different fixed commands; §6.6's
safety argument rests on a panel behaviour the cited `available`
check does not have; the Repair action re-runs a pip install that pip
will report as already satisfied; and the cross-repo banner and
durable record demand a cross-process, repo-independent store that
does not exist in the cited code and whose record cannot even detect
a dead child (no PID). A design that is merely open can be decided.
A design that contradicts itself in its central mechanisms has to be
corrected first, and the correction will delete or rewrite the
infrastructure in §§6.5, 6.9, and 6.10 that is currently the bulk of
the proposal. The zoom-out gates on simplicity, YAGNI, reuse, and
minimal data flow still fail; the root-problem gate now fails in a
second way, described in §4.

---

## 1. New and changed findings

### Blockers

#### B1 — The design now pre-picks the option it claims is unpicked (§5.1 default vs the approval gate) — changed verdict on Stage 5 B1

- **Severity:** Blocker
- **Section / lines:** §5.1, lines 293–297 ("The default position of
  this design, if the human picks no option, is: the panel flips on
  layer 2 … and layer 3 is reported separately"); header, lines 6–7
  (human approval of §9 "is required before any code is written");
  §9 header (a set left unpicked "blocks implementation of the parts
  that depend on it"); §6.8, line 647 (matrix renders the two-line
  checkpoint state unconditionally).
- **What is wrong:** Stage 5 treated option set A as open. It is not
  merely open: §5.1's default *is* option A3 (two states, not one),
  and §6.8 builds on it as if decided. An implementer reading §5.1
  alone builds A3; an implementer reading the gate rule alone builds
  nothing. From a zoom-out view this is worse than an open question,
  because the "default" lets the most UI-heavy rendering (two panel
  states) slip in without a human ever choosing it, while the gate
  language gives false assurance that nothing is pre-decided. Either
  delete the default and let the gate mean what it says, or state
  plainly that A3 is the decision unless the human overrides it —
  and then the gate rule, §6.8, and §10's acceptance criteria (which
  are written unconditionally while C3 specifies no button and D1
  specifies no on-page enablement) must all be rewritten to match.
- **Evidence:** Design §5.1, lines 293–297, quoted above; §9.C's own
  admission that under C3 "the rest of this design narrows to the
  guide plus detection work" — which cannot be reconciled with a
  §5.1 default that already implements A3.
- **Simplicity rationale (standard §§1, 2):** A spec cannot both
  have a default implementation and block implementation until a
  choice is made. The contradiction is not editorial; it decides
  what gets built by accident.

#### B2 — The Repair action cannot repair, and the panel premise behind its safety claim is false — new (from Design B6, Comprehension B5)

- **Severity:** Blocker
- **Section / lines:** §5.5, lines 430–451 ("Repair runs the same
  sequence as install"); §6.6, lines 587–593 ("safe to retry" defined
  as "no half-installed package is reported as installed …
  the panel still says 'not installed'"); §2, lines 89–92
  (`available` / `find_spec` description); §6.2, lines 481–505
  (taxonomy has no force-reinstall, uninstall-first, or cache-clear
  row).
- **What is wrong:** Two independent failures, either of which kills
  §5.5 as a zoom-out matter (scope that does not work is worse than
  scope that is merely large):
  1. Repair = re-running `pip install laya` when `laya` is already
     present and satisfied. Pip reports "requirement already
     satisfied" and does nothing. A corrupted, partially unpacked,
     or version-drifted install — the exact failure class Repair
     exists for — is not repaired. No `--force-reinstall`,
     `--ignore-installed`, uninstall-first, or cache-clear step is
     named anywhere in §§5.5/6.2, and §9.G's out-of-scope branch
     then replaces the Repair *route* (§8 lists it unconditionally)
     with a manual-command message, so the same word means three
     different UI elements depending on the pick.
  2. The safety argument is false under the cited panel logic.
     `CodeOrchestrator.available` (verified in `backend.py`) returns
     true on `find_spec("laya")` alone. A stopped or partial pip run
     that leaves a findable `laya` package *will* flip the panel to
     "available" immediately, verification or not. §6.6's claim that
     "verification has not run, so the panel still says 'not
     installed'" is therefore not a property of the design; it
     requires a change to `available`/panel semantics that the
     design never requires (its §8 entry for `backend.py` is
     "modify only if needed", with no criterion for "needed").
  3. The trigger is missing for the most likely real case. §5.2's
     probe runs only after an install the user started. A backend
     installed earlier by the old terminal command and now broken
     is never probed on page load or dashboard start, so it never
     surfaces Repair at all — while `available` may still read it
     as available (see B3 on the loader's first branch).
- **Evidence:** Design §5.5, quoted above; `backend.py`
  `CodeOrchestrator.available` / `load_backend` (find_spec-gated);
  §6.2's row list (no pip-lock, partial-wheel, or cache-clear row,
  although §9.E1's fails-when assumes cache clearing is the remedy).
- **Simplicity rationale (standard §§1, 3):** Do not add a Repair
  subsystem whose defined mechanism is a no-op and whose safety
  argument depends on a panel behaviour the code does not have.
  The honest zoom-out alternative is the one §5.5 already writes
  for the out-of-scope case — a manual command — until a real
  repair argv and a real panel change are specified.

#### B3 — "Installed" is defined for only one of the two load paths in code — new (from Structural B5, Design M7, Style B2)

- **Severity:** Blocker
- **Section / lines:** §5.1, lines 284–290 (layer 1 = "`laya` is
  importable"); §2, lines 87–96 (availability summary, `laya` only);
  §7.1 (fixed argv installs `laya` only).
- **What is wrong:** `load_backend` in the cited `backend.py`
  (verified) first tries `importlib.import_module("coding_decision_agent")`
  and prefers that package when present, falling back to the
  `find_spec("laya")` check only second. The design's definition of
  "installed", its fixed argv, and its probe all assume the `laya`
  path is the only path. A machine with `coding_decision_agent`
  but no `laya` is "installed" by code and "not installed" by §5.1;
  a machine where the install button installs `laya` while the
  loader would prefer a broken or stale `coding_decision_agent`
  install is "fixed" by a mechanism that does not touch the loader's
  first branch. From a zoom-out view this is root-problem drift:
  the design optimises the wrong target. It specifies a multi-GB
  install, a durable record, and a cross-repo banner for a backend
  whose own availability function it has described incompletely.
  The §4 claim that "`available` checks only the first two" layers
  is additionally false on the design's own terms: the `find_spec`
  step proves layer 1 only, and §5.1 itself says so two sections
  later.
- **Evidence:** `backend.py:load_backend` (coding_decision_agent
  first, `find_spec("laya")` second); design §5.1, lines 284–288
  ("the `find_spec` check today proves layer 1 only").
- **Simplicity rationale (standard §§1, 4):** Fix the definition
  to cover the actual loader before building anything on top of it.
  Every downstream state (panel value, matrix row, repair trigger)
  inherits this error.

#### B4 — The cross-repo banner and the durable record demand infrastructure the cited architecture does not have — Stage 5 M4/M5 upgraded to Blocker

- **Severity:** Blocker
- **Section / lines:** §6.9, lines 656–680 (banner on "every repo",
  "a finished install flips every repo's panel"); §6.5, lines
  556–579 (durable record; "running but has no living child process
  becomes `interrupted`"); §6.4, lines 525–555 (lock "per dashboard
  process"; two-process case "out of scope"); §5.4, lines 384–388
  (branch-2 path example `<state>/.jev/cda-venv`).
- **What is wrong:** Stage 5 called the five-surface fan-out
  non-minimal. The later stages show it is not merely non-minimal
  but architecturally unsupported:
  1. `DashboardCore` is constructed per repo (`settings.root`, a
     single root — verified in `core.py`); the grade ledger is
     per-repo (`ledger_path_for_repo(settings.root)`); each
     runner's lock guards only itself, in one process. There is no
     dashboard-wide, repo-independent state directory in the cited
     code, and the design never names one: "dashboard state
     directory" is absent from the §1 glossary, §5.4 gives only an
     example path, and that example reuses the `.jev/` name the
     design elsewhere forbids for machine-wide state (§6.9).
  2. The durable record's field list (fixed argv, target decision,
     started time, phase, exit code, verification outcome) contains
     no PID, no process identity, and no log path. "No living child
     process" is therefore uncheckable from the record as specified
     — the `interrupted` mechanism cannot be implemented, not merely
     has not been justified. The design's own fallback ("if the
     implementer cannot provide a durable record … the restart
     acceptance criterion fails") concedes the store is load-bearing
     for a rare event while failing to specify it.
  3. The cross-repo promise and the out-of-scope disclaimer
     contradict each other: §6.9 promises every-repo visibility
     while §6.4 declares cross-process contention out of scope and
     falls back to "whatever pip surfaces" — yet §6.2's taxonomy
     has no file-lock / cross-process-contention row for that
     fallback to point at. In the cited architecture the multi-repo
     case *is* the multi-process case, so it is the normal case,
     not an edge case.
  4. "Orphaned children are never left running past a dashboard
     stop" (§6.5) is true only for graceful shutdown (§6.11). A
     killed dashboard, host shutdown, or OOM kill leaves the pip
     child alive, and §6.5's restart logic (which assumes a dead
     child) then contends with a live one.
- **Evidence:** `core.py` (`DashboardCore.__init__` takes
  `settings.root`; `shutdown_jobs`); design §6.5 field list,
  lines 560–564; design §6.4, lines 550–554.
- **Simplicity rationale (standard §§2, 3):** Do not build a
  persistent, cross-process job store — with a new location,
  retention, and truncation policy — on top of a per-process,
  per-repo architecture, for a restart-mid-install frequency the
  design never measures, when both existing runners ship in-memory
  state. Start in-memory like the precedents; add durability only
  if restart-mid-install is shown to strand users, and then specify
  the store (path, PID, lock) completely or not at all.

#### B5 — The fixed command is two different commands, and its 2.5 GB decision is a chicken-and-egg problem — new (from Design B2, Style B5/M13)

- **Severity:** Blocker
- **Section / lines:** §5.3, lines 335–342 (torch first, then `laya`,
  on *both* paths); §7.1, lines 710–722 ("The argv installs `laya`
  (and torch first, on the CPU path) only"); §5.3, lines 344–349
  (detection "for example, whether CUDA is reported available";
  "the exact detection call is an implementation detail the
  implementer must name in code review"); §5.2, line 305
  (probe written as `sys.executable -m python -c ...`).
- **What is wrong:** §5.3 and §7.1 cannot both be the fixed argv.
  On the default path, §5.3 installs torch explicitly before `laya`
  (pinning the variant); §7.1 lets torch arrive only as a `laya`
  dependency, which pip may re-resolve to a different variant —
  exactly the failure §5.3 warns about. The design also names no
  anti-re-resolution mechanism for the CPU path ("without letting
  pip re-resolve torch to the default wheel" — `--no-deps` would
  break other `laya` dependencies; a constraints file is never
  mentioned). And the GPU decision is circular: before torch is
  installed there is no torch to report CUDA availability, yet no
  per-OS driver/device query, fallback, or inconclusive behaviour
  is named. This is the design's highest-cost decision (~2.5 GB),
  and it is the one left to code review. Finally, the one concrete
  probe command in the document is untypeable as written:
  `sys.executable` already *is* the interpreter, so `-m python`
  asks it to run a module named `python` (the design's own correct
  form, two sections later, is `sys.executable -m pip`).
- **Evidence:** Design §5.3 vs §7.1 wording quoted above;
  `pyproject.toml` (`laya>=0.4.15`, no torch pin).
- **Simplicity rationale (standard §§1, 4):** The fixed-argv rule
  is the design's main security claim (§7.1: a request-supplied
  package name would be remote code execution by design). A
  security claim resting on two contradictory argv descriptions
  and an unnamed detection call is not a claim; it is a placeholder
  with a test ("`jev-agent` never in argv") written against an argv
  the document never prints.

### Majors

#### M1 — The option menu contains options that violate the design's own normative rules — new synthesis (from Comprehension M13/M14/M15, Structural M5)

- **Severity:** Major
- **Section / lines:** §9.H2 vs §5.4 (decision "shown to the user
  before the install starts"); §9.H3 vs §5.4 (matrix "normative";
  button "must never be shown on Docker without that statement");
  §9.F2 vs §4 (forms "can ledger a spurious 'unavailable' record"
  claimed answered by F); §9.G (G1/G2 keep `>=0.4.15` "unless the
  implementer finds a breaking version during testing, in which
  case the policy becomes a pin and the design is amended *before
  implementation*").
- **What is wrong:** Stage 5 treated the eight option sets as a
  large but valid menu. The later stages show several options are
  not valid members of it: H2 shows environment, sizes, and
  persistence only *after* the click, violating §5.4's before-start
  rule (and H2's own fails-when admits a Docker user "has already
  started a download"); H3 offers the persistence warning as small
  text under the button, with no ruling on whether that satisfies
  the normative matrix requirement — in the Docker case, where
  missing the warning "costs the most"; F2 guards only jobs
  *refused* because an install is running, while its own fails-when
  admits failed (not refused) jobs "still ledger 'unavailable' as
  today", so under F2 the §4 complaint is unanswered; and G's
  pin-after-testing rule is temporally impossible (testing happens
  during implementation, so the amendment cannot both follow testing
  and precede implementation). A menu in which some picks break the
  design's stated invariants is not a decision procedure; the human
  cannot pick from it safely without first deleting the invalid
  options. This also means §4's "answered by option set X" map is
  circular for the hardest items (stop → E, which defers to §6.6,
  which defers to E; Docker → §5.4, which points to G, which never
  mentions Docker — see §2, Stage 5 B3).
- **Simplicity rationale (standard §2):** Prune the menu to valid
  options before asking a human to pick. Eight sets, most with three
  options, several invalid, is not minimal decision-making; it is
  decision debt carried into implementation.

#### M2 — Reuse is not merely deferred; its scope is miscounted — Stage 5 M2 strengthened (from Comprehension M16)

- **Severity:** Major
- **Section / lines:** §9.B, lines 891–921 (scaffold "from the
  browser and grade runners"); §2 (two runners described); §8
  (scaffold component list: lock, thread, state, timestamps,
  `busy()`, `status()`, shutdown hook, log cap).
- **What is wrong:** The cited `core.py` (verified) owns a third
  background runner, `ReverseRunner`, alongside the browser and
  grade runners, and the concepts brief names three runners
  including it. The design never says whether the scaffold covers
  it, excludes it deliberately, or leaves it as a fourth dialect.
  Worse, §8's scaffold list omits the grade runner's generation
  counter (verified in `code_grade_runner.py`: "only the current
  generation may write the outcome"), the mechanism that makes its
  draining semantics correct. A B1 extraction built from the listed
  components would lose a correctness mechanism the document never
  mentions. And B2 ("Deliberate third copy") remains a coequal
  option, i.e. the design still legitimises the triplication §4
  itself calls out, with no stronger justification than "no
  regression risk" — while its own B2 failure mode is "a later fix
  lands on one runner and not the others". Reuse is therefore not
  just undecided; the reuse option as specified is wrong in scope.
- **Evidence:** `core.py:67` (`ReverseRunner`); `code_grade_runner.py:154–170`
  (generation counter); design §9.B2 ("mirrors `BrowserInstallRunner`
  and does not share code").
- **Simplicity rationale (standard §4, Leverage Existing):** When
  equivalent components exist and are stable, reuse is the default
  and its scope must be counted correctly. A scaffold spec that
  miscounts the runners and drops a correctness mechanism is not a
  reuse plan.

#### M3 — Preflight is a subsystem with no computable thresholds — new detail (from Comprehension M1/M2, Design M1)

- **Severity:** Major
- **Section / lines:** §6.1, lines 454–480; §5.3, lines 351–362.
- **What is wrong:** §6.1 requires disk "covering the torch variant
  plus checkpoint plus temp staging" but §5.3 never gives the CPU
  wheel size, gives `laya` plus transformers only as "the size pip
  reports … or 'a few hundred MB' when it cannot be known", and
  gives temp staging only as "at least 1 GB" (uncited — the cited
  sources give a 512M tmpfs and an 842 MB checkpoint, from which
  1 GB does not follow). No formula, no margin, and no statement of
  which disk each number applies to (target-env disk, temp disk,
  and HF-cache disk may be three filesystems) is given, and §5.3
  never sums its own parts into the one figure the confirm step
  needs (2.5 GB + 842 MB + a few hundred MB + 1 GB is well over
  4 GB on the default path; §9.C3's example "3.1 GB free, need
  about 4 GB" does not reproduce from those numbers). The memory
  check then contradicts §6.1's own rule ("Each check maps to a
  plain-English refusal") by being "at least the 2–3 GB … or a
  warning" — refusal or warning is never decided, and "at least"
  a range is not a checkable threshold. Network reachability has
  no method, host, or timeout (DNS? TCP? HEAD? pip dry-run?), and
  a reachability pass does not guarantee pip success anyway. A
  preflight subsystem that cannot compute its own refusal threshold
  will either refuse arbitrarily or pass vacuously — which is a
  worse outcome than no preflight, because it carries the authority
  of a check.
- **Simplicity rationale (standard §2):** Do not specify a new
  detection subsystem before its thresholds are measurable. The
  honest sizes in §5.3 are the prerequisite, and they are still
  placeholders.

#### M4 — Warm-up (pre-warming) has no mechanism, and if it is `laya.load` it is a full model load, not a download — new (from Comprehension B4, Design M6)

- **Severity:** Major
- **Section / lines:** §5.2, step 3; §6.3, lines 517–519 ("bytes
  downloaded when the download reports them"); §9.A2.
- **What is wrong:** No "download the checkpoint without grading"
  function exists in the cited code; the only fetch path is a real
  grade (`laya.load` on first use). The design never names the
  alternative (a Hub download call vs `laya.load` in the probe
  subprocess), never states the warm-up phase's input, output, or
  success criterion, and presupposes a download API that reports
  bytes without naming one. If warm-up *is* `laya.load`, it loads
  torch (2–3 GB RSS) and takes minutes — the cost profile of a
  grade, not of a download — and §5.3's size/time honesty
  understates it. It also warms only the current repo's model (§6.9
  admits this), so A2's "complete promise" is complete for one repo
  and incomplete for every other repo whose panel §6.9 flips
  anyway (see Stage 5 M7, standing). From a zoom-out view, A2 is a
  speculative heavy phase added to an already heavy install, for a
  feature whose value is advisory, before the mechanism that would
  implement it has been shown to exist.
- **Simplicity rationale (standard §2, YAGNI):** A1 (flip on probe,
  checkpoint downloads on first grade, honestly stated) is the
  minimal honest shape. A2 should not be carried as a coequal
  option until a download-without-grading mechanism is named and
  its cost is stated.

#### M5 — The trust-boundary expansion gains a new hole: the tunnel case — Stage 5 M6 strengthened (from Design M5, Style M16)

- **Severity:** Major
- **Section / lines:** §7.2, lines 735–764 ("any local visitor can
  press Install"; "No additional rate limit is specified"); §2,
  lines 135–137 (loopback rule); `docs/deploy.md` (SSH-tunnel
  remote access, as cited by the Design Critic).
- **What is wrong:** Stage 5 already flagged that a host-mutating
  pip install inherits the browser precedent's local-only scope
  without its local-only justification. The Design Critic adds the
  case that breaks the inherited rule outright: the deploy doc's
  standard remote-access pattern is an SSH tunnel
  (`ssh -L 8765:127.0.0.1:8765 <host>`), and a tunnelled remote user
  arrives on loopback — so under §7.2's rule they can trigger a
  multi-GB host mutation with no token. The cited `security.py`
  Host check prevents DNS rebinding; it does not distinguish a
  local user from a tunnelled remote user, and §7.2 never addresses
  the distinction. Separately, "rate limiting" is a mislabel:
  single-flight bounds *concurrency* (one pip process at a time),
  not *rate* — a token holder can loop install → finish → install
  indefinitely, churning disk, CPU, and network, and nothing in
  §7.2 stops them. "Bounds abuse to one pip process" is not a bound
  a sceptical architect accepts for a host mutation, and the
  confirm step that would at least force a deliberate click is
  deferred to option set H, so H3 (no confirm, small text) remains
  available for a host mutation. The logging claim is also
  unsupported: "the requesting context the dashboard already
  records" names no field, and no per-request audit context appears
  in the cited `security.py` or runners; §6.10's redaction claim
  ("following the dashboard's existing redaction practice") cites
  a practice that does not appear in the cited files (verified: no
  redaction function in `security.py`, `code_grade_runner.py`, or
  `browser_install.py`).
- **Simplicity rationale (standard §4, Identify Anti-Patterns):**
  A host-mutating endpoint should default to its narrowest scope
  (local-only, or confirm-first as the default) and expand only on
  justification — not inherit a precedent whose scope it has
  already exceeded.

#### M6 — There is no architecture section at all; ownership is hedged or delegated — new (from Structural B4)

- **Severity:** Major
- **Section / lines:** §§5–7 passim (headings: problem, current
  state, goals, critique, installed-meaning, job, security — no
  architecture heading); §6.4, line 531 ("owned by `DashboardCore`
  (or its equivalent)"); §6.4, line 545 (implementer picks queue
  vs refuse); §6.11, lines 693–704 (shutdown order stated three
  ways: "before or after", "the implementer must state it", then
  "grade shutdown precedes install shutdown"); §5.4, lines 384–388
  (branch-2 path "exact path is an implementation detail" yet must
  be "one fixed path, recorded … and re-discovered").
- **What is wrong:** Sections 5–7 are a sequence of behaviours, not
  an architecture: no component list, no data-flow description, no
  ownership table for the new runner, the coordination lock, the
  durable record, and the status surface. Where ownership is named
  it is hedged ("or its equivalent") or handed to the implementer
  at exactly the decision points where a spec must decide (the
  2.5 GB detection call, queue-vs-refuse, shutdown order, the fixed
  venv path). For a zoom-out review this matters directly: without
  a component/data-flow picture, minimal data flow cannot be
  verified — and Stage 5's M4 (five surfaces for one job) can only
  be re-derived from behaviour text, never checked against an
  architecture. The absence is itself a zoom-out finding, not a
  formatting complaint.
- **Simplicity rationale (standard §3):** A high-level design must
  show its components and data flow. Behaviour lists hide coupling;
  this one hides a cross-process store, a coordination lock, and a
  second interpreter path behind "or its equivalent".

#### M7 — The state matrix is incomplete in ways that now include the design's own states — Stage 5 N3 upgraded to Major

- **Severity:** Major
- **Section / lines:** §6.8, lines 632–655 (10 rows, presented as
  "rows the UI must render correctly"); §5.2, lines 326–331
  ("installed, restart required" — no matrix row); §6.4, line 545
  (queued/waiting — no matrix value); §6.1 (preflight refusal —
  no matrix row); §6.8, line 647 ("available" row renders the A3
  checkpoint line unconditionally).
- **What is wrong:** Stage 5 noted the matrix omitted some
  combinations. The later stages show the omissions include states
  the design itself defines: the §5.2 restart-required state has no
  row at all; pre-existing availability (job = none, the common
  case — the preamble says the job "is none unless an install has
  run") has no row; Enabled = yes with install running/failed/
  stopped/interrupted is missing (only yes + not installed + none
  is given); a failed reinstall/repair from "available" is missing
  (only "running (reinstall/repair)" is given); queued/waiting and
  draining-blocked installs have no value in the matrix at all.
  And the matrix presupposes option A3, so it cannot be implemented
  as written under A1. A matrix that omits its own spec's states
  and presupposes an unpicked option is not a rendering contract;
  the zoom-out point is that exhaustiveness would make it larger,
  which is an argument for fewer states, not more rows.
- **Simplicity rationale (standard §2):** Fewer states, fully
  enumerated, beat a 10-row matrix that is neither complete nor
  decidable.

### Nits

#### N1 — §4's traceability claim is false on its own terms — new precision (from Design B5, Structural B2)

- **Severity:** Nit
- **Section / lines:** §4, lines 199–204 ("The brainstorm lenses
  raised 16 blockers. The design answers each one in the section
  noted"); §4, lines 205–277 (21 bullets, unlabelled by severity).
- **What is wrong:** 21 bullets are listed against a claim of 16
  blockers, with no marking of which bullets are blockers and which
  are majors folded in, so the claim cannot be verified. At least
  six bullets are "answered" only by an unpicked option set (B, E,
  F, D, G, C) — which, under §4's own rule ("a blocker with no
  answering section is a gap, not a deferred detail"), are gaps,
  not answers. One bullet bundles three distinct items (preflight,
  taxonomy, acceptance/test-matrix/rollback) into one "answer".
  The count error is small; the self-certification error is not,
  because §4 is the section a reader trusts to know what is
  decided.

#### N2 — "Constant" argv, "full" log, and "256 KB" all overstate what they describe — new wording findings (from Style M8/M15/M19)

- **Severity:** Nit
- **Section / lines:** §7.1, lines 710–720 ("constant argv sequence"
  vs a builder with three variable inputs: `sys.executable`, torch
  decision, version policy); §6.5, line 563 ("full log is retained")
  vs §6.10, line 686 (log "capped … 256 KB, when the job finishes");
  §2/§6.10 ("256 KB" vs cited `_MAX_LOG_BYTES = 262_144`, i.e.
  256 KiB, verified in `browser_install.py`).
- **What is wrong:** "Constant" can only mean "not taken from the
  request", but as written it claims the sequence itself does not
  vary, which it does by machine and by unpicked option. A log
  truncated to 256 KB is not a full log, and a retry that "truncates
  the old log only after copying it to the retained full log" is a
  capped copy of a truncated log. None of these changes behaviour
  alone; together they show the document's assurance language
  ("normative", "by construction", "never") running ahead of its
  mechanisms — the same pattern as B1 at small scale.

#### N3 — "One-click" title vs the two-click option — Stage 5 N1, unchanged

- **Severity:** Nit
- **Section / lines:** Title, line 1; §9.H1, lines 1097–1112
  ("'one-click install' becomes 'two-click install'").
- **What is wrong:** Unchanged from Stage 5. If H1 wins, the flow is
  two clicks by the design's own admission, and §10's acceptance
  criterion hedges it as "one click (plus a confirm click if option
  set H picks one)". Rename to "Install button" until H is picked,
  or make H1's cost explicit in the title's claim.

#### N4 — Glossary gaps for load-bearing terms — new detail (from Comprehension N3, Structural M1)

- **Severity:** Nit
- **Section / lines:** §1, lines 33–69 (glossary defines "endpoint"
  but not "dashboard state directory", "warm-up/pre-warming",
  "probe", "attempt events", "full log", or "checkpoint file");
  §1, lines 70–76 (Backend field list allows only "available" /
  "not installed", while §5.5/§6.8 add "installed but not loadable"
  and install-job words).
- **What is wrong:** The terms that carry §§5–7 are undefined, while
  less-loaded terms are defined; and the field list is wrong as a
  statement of the proposed panel (it predates the third Backend
  value the design later mandates — the same third value Style B6
  notes violates §5.1's own ban on showing "installed" on layer 1
  alone, since "installed but not loadable" shows the word
  "installed" for a presence-only backend). Small alone; it matters
  for zoom-out because a design whose central vocabulary is
  undefined cannot be checked for scope creep.

---

## 2. Where Stage 5 findings stand

| Stage 5 finding | Status at Stage 8 |
|---|---|
| B1 — Whole system built before C is decided | STANDS, strengthened by B1 and M1 above: the design is now also internally inconsistent, not merely open. |
| B2 — Layer-2 probe rests on a nonexistent API | STANDS unchanged; the probe command is additionally wrong as written (`-m python`, see B5). |
| B3 — Matrix normative but Docker row empty; branch 2 unproven | STANDS unchanged. The Docker cell still reads "See option set G", and option set G still never mentions Docker, a volume, or a volume path (Comprehension B10). Branch 2 still requires an interpreter override the grade runner does not have (in-process executor, no interpreter parameter). |
| M1 — Option sets D/E/G/H turn a button into a lifecycle manager | STANDS, plus M1 above: several options are invalid members of the menu, not just large ones. |
| M2 — Reuse deferred, third copy legitimised | STANDS, strengthened by M2 above (ReverseRunner miscounted, generation counter omitted). |
| M3 — Altitude drops (copy strings, test files, truncation rules frozen before options picked) | STANDS unchanged. E2 would invalidate §6.6's stop rows and the stop tests; G1 would invalidate §10's uninstall rollback — the detail is still frozen before the choices that would rewrite it. |
| M4 — Non-minimal data flow (five surfaces) | UPGRADED to Blocker (B4 above): the fan-out is architecturally unsupported, not just large. |
| M5 — Durable record + observability premature | UPGRADED to Blocker in its record half (B4 above): the record as specified cannot detect a dead child, and the redaction practice it cites is not in evidence. The observability half (structured events with no schema, sink, or retention) stands as premature. |
| M6 — Trust-boundary expansion without narrowing | STANDS, strengthened by M5 above (tunnel case, rate-limit mislabel). |
| M7 — Runtime vs per-repo checkpoint conflated, papered with copy | STANDS unchanged. §6.9 still resolves a structural scope mismatch with UI copy ("say which model's checkpoint was warmed") and still flips every repo's panel while warming one repo's model. |
| N1 — "One-click" contradicted by H1 | STANDS (N3 above). |
| N2 — "Honest sizes" contains a non-size | STANDS unchanged; §5.3 still gives the CPU wheel size as a pointer, `laya` + transformers as "a few hundred MB", temp staging as an uncited "at least 1 GB", and no summed total. |
| N3 — State matrix incomplete | UPGRADED to Major (M7 above). |
| N4 — No unified job-state vocabulary | STANDS unchanged; `interrupted`, `verifying`, `stopped` are still unmapped to the browser runner's `idle/running/stopped/done/failed` and the grade runner's `running/cancelled/draining` dialects. |
| N5 — §8 conditional ("modify only if needed") | STANDS unchanged; the backend conditional is still the most consequential one (it carries B2's loadability entry point), and §8 additionally omits the settings/config change D2/D3 would require, any install-route HTTP methods, and the live-fragment template filename (Comprehension M8/B11). |

## 3. What the design already answers (Stage 5 overstated, or later stages confirm as decided)

Credit where it is due — an adversarial report that denies decided
points loses credibility on the undecided ones:

- **PEP 668 stance is decided (§5.4, line 400).** The install never
  passes `--break-system-packages`; a refusing system Python goes to
  branch 2 or 3. That is a real decision, not a deferral.
- **The fixed-argv principle is decided (§7.1).** No request field
  may change the command; argv list, never a shell; only the PyPI
  default and the PyTorch CPU index, both constants; a test must
  assert `jev-agent` never appears in the argv. What is missing is
  the literal argv and the torch-variant mechanism (B5) — the
  principle is answered, the command is not.
- **The three-surface retirement is enumerated (§2, §8).** Status
  panel, failed-grade message, and Learning-page Ratings empty state
  are all named as surfaces that must change together. The rendering
  details are missing (Comprehension), but the scope question —
  "which surfaces?" — is answered.
- **Progress honesty (§6.3) and grading invariants (§3) are decided.**
  No invented percentage; no guessed grades while the backend is
  missing; threshold unchanged at 0.7; off-by-default unchanged
  unless D decides otherwise. These are genuine constraints, and
  they are the right ones.
- **Branch 3 (guide fallback) is a real terminal branch (§5.4).**
  Not every deployment row is a placeholder: for an unwritable
  environment the design does specify refusal plus an
  environment-specific command and no job start. The gaps are
  Docker and branch 2, not the whole matrix.
- **The value question is surfaced, not hidden (§4, §9.C).** The
  design states the adverse evidence plainly (nothing cleared 0.7
  across 13 grades; advisory; off by default; heavy) and puts the
  discover-don't-install boundary question to the human with the
  cheaper fits named (C2, C3). Stage 5 B1 stands as an
  implementation blocker, but as problem framing this is honest:
  the failure is that the decision has not been made, not that the
  evidence has been concealed.
- **Auto-install on first grade and bundling are rejected with
  reasons (§11).** The alternatives evaluation the structural
  review finds missing from §§1–7 does exist in the document, after
  the implementation plan. Its placement is a structural defect, not
  an absence of reasoning.

## 4. Root-problem focus: does the document as a whole stay on the root problem?

**The problem statement does. The design as a whole does not.**

The root problem (§1) is precise and still the right one: a dashboard
feature that sends a terminal-free user to a terminal violates the
product's own browser-first law, and the fix is a browser-visible
install of the CDA backend in the dashboard's own environment. §§1–4
stay on that problem, and §4's critique-first structure is the
document's best discipline.

Drift begins where specification turns into infrastructure, and the
Comprehension and Critic stages locate it in five places:

1. **Infrastructure for an undecided feature.** A durable,
   cross-process job store (§6.5), structured attempt events (§6.10),
   a cross-repo banner (§6.9), and a platform × failure test matrix
   (§10, nine platforms × ~12 failure classes, with detection stubbed
   so the riskiest behaviours — GPU choice, PEP 668 writability,
   service-account cache writability — are never actually exercised)
   are all specified before option set C decides whether the button
   exists at all. If C3 wins, all of it is deleted; if C2 wins, most
   of it is scoped away. That is premature optimisation in the
   zoom-out sense: complexity built before its necessity is proven,
   for a feature whose measured value signal is advisory-only.
2. **A second feature inside the first.** The dedicated-venv branch
   (§5.4, branch 2) is not an install detail; it requires redesigning
   the grade path, which is in-process today, to run grades under a
   different interpreter — an architecture change the design never
   designs (Comprehension B6) and conditions away ("if it cannot,
   this branch is unavailable"). A branch that silently collapses
   for the most fragile deployments (PEP 668, service accounts, VPS)
   is not a decision tree; it is scope that evaporates on contact
   with the code.
3. **A lifecycle manager inside a button.** Option sets D (config
   writer), G (version display, repair, uninstall with a
   pre-existing-torch guard that is itself a new detection subsystem
   with no detection method named), E (per-phase stop), and H
   (confirm page) together make the install job a lifecycle manager
   for a backend whose version policy is undecided (pin vs
   `>=0.4.15`). None of these earns its place from a user request in
   the document; all of them are specified in enough detail to
   require building or explicitly rejecting each.
4. **Speculative phases.** Pre-warming (A2, M4 above) adds a heavy
   phase with no mechanism; the checkpoint probe (§5.2, step 3) is
   tied to the pre-warming option, so under A1/A3 the checkpoint
   state the panel needs is unknowable by the design's own rule
   (Structural M9) — a probe that downloads is not a probe, and a
   read-only existence check should not depend on a warming option.
5. **Copy as architecture.** The runtime/checkpoint scope mismatch
   (Stage 5 M7, standing) is still resolved with UI copy rather than
   structure: flip every panel, banner every repo, and "say which
   model's checkpoint was warmed". Copy cannot fix a scope error,
   and the banner advertises a machine-wide fact on pages where the
   next grade may still download a different model.

The minimal shape the root problem actually requires — environment
detection, one fixed install sequence, a subprocess probe, progress
on one status page, plain-English failure with retry — is present in
the document, but buried under the five drifts above. Until C is
decided, B1/B5 are corrected, and the durable/cross-repo/lifecycle
machinery is either justified by a measured need or cut, the document
is a thorough specification of a system that has not yet earned its
size.

## 5. What would make this design pass Zoom Out

1. **Decide C first, in isolation, and delete what loses.** If C3
   or C2 wins, §§5–8/§10 shrink to detection + guide (or detection +
   a narrowed runner). Do not approve the full design with C open.
2. **Remove the §5.1 default or promote it to the decision.**
   Resolve B1 explicitly; rewrite §6.8 and §10 to match whichever
   posture is chosen.
3. **Specify the probe as a real interface, or drop layer 2.**
   Either add a no-checkpoint-construction entry point to
   `LayaBackend` and prove it distinguishes broken torch from working
   torch, or admit the probe is an import probe, amend §5.1's "never
   layer 1 alone" rule, fix the `sys.executable -m python` argv, and
   change `available`/panel semantics so §6.6's safety claim is true.
4. **Decide the fixed argv and the GPU detection call per OS, with
   the anti-re-resolution mechanism, and reconcile §5.3 with §7.1.**
   Fix "installed" to cover the `coding_decision_agent` load path.
5. **Decide the runner shape (B) with the correct runner count
   (including `ReverseRunner` and the generation counter), and
   decide the Docker row (G) with a written persistence statement —
   or collapse Docker to branch 3.** Remove B2 from the menu unless
   a stronger justification than "no regression risk" is given, and
   delete the invalid menu options (H2 as written, F2's ledger
   claim, G's temporally impossible pin rule).
6. **Cut the option sets to the minimal set** (D1, G1, one stop
   choice, one confirm choice), specify Repair as a real argv
   (force-reinstall or uninstall-first) with a defined trigger for
   pre-existing broken backends, and move copy strings, the full
   failure taxonomy, the exhaustive test matrix, and the durable
   record to the implementation phase — after those choices are
   made, and only with a path, a PID, and a lock if durability is
   kept at all.
7. **Scope the button honestly:** runtime-only, or split runtime
   install from per-model checkpoint warming, instead of flipping
   every repo's panel and adding a banner to every repo. If
   cross-repo visibility is kept, specify the shared store and the
   cross-process mechanism that makes it true.

Until (1)–(4) are done, this design should not be approved for
implementation at its current size, however thorough its failure-mode
coverage is.
