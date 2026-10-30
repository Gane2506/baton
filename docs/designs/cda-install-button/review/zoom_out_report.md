# EGM Stage 5 — Initial Zoom Out (Skeptical Architect) — cda-install-button

- **Target:** `docs/designs/cda-install-button/design.md` (full document, §§1–12)
- **Mode:** REVIEW, against `resources/document_standards/zoom-out-standard.md`
- **Method:** Read the design and `concepts_brief.md` in the same directory, and
  only the code files the design cites in the worktree
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
  No other files were read, and no claim below rests on code outside that set.
- **Verdict on context sufficiency (§0 of the standard):** PASS, narrowly. The
  objective and motivation are stated plainly in §1. That is the only
  zoom-out gate this design clearly passes. It fails the simplicity,
  YAGNI, reuse, and minimal-data-flow gates below, and it does so
  *before* the human has decided whether the feature should exist at all.
- **Counts:** 3 Blockers, 7 Majors, 5 Nits.

This report is deliberately adversarial, as the persona requires. Where the
design is careful — the fixed-argv rule in §7.1, the refusal to flip the panel
on pip exit code alone in §5.2, the three-surface retirement in §2/§8 — that
care is noted only where it sharpens a finding. It does not offset the
findings: a carefully specified system that should not yet be built, or
whose central mechanism does not exist in the cited code, is still
over-engineered.

---

## Blockers

### B1 — The design builds the whole system before the human decides whether the button should exist

- **Severity:** Blocker
- **Section / lines:** §3 Goals (lines 163–189, "The same endpoint on Mac,
  Windows, Linux, and VPS"); §9 option set C (lines 922–970); §1 (lines
  13–32, "One click installs the backend").
- **What is wrong:** §9.C puts the only question that can delete most of
  this design — guide-only (C3), narrowed button (C2), or button everywhere
  (C1) — to a human decision that has not happened yet, while §§5–8 and
  §10 already specify a runner, a coordination lock, a durable job record, a
  new status surface, a failure taxonomy, a state matrix, and a platform ×
  failure test matrix as if C1/C2 were decided. The design itself concedes
  the point: "If that evidence does not hold, C3 (or C2) is the cheaper
  fit, and the rest of this design narrows to the guide plus detection
  work" (§9.C, end). A design that admits its §§5–10 may collapse to
  "detection plus rendering" is not a decided spec; it is a premature
  build-out of the most expensive option.
- **Evidence:** Design §9.C, quoted: "The human decides whether install
  friction, rather than the feature being opt-in, heavy, and advisory, is
  what blocks adoption — and whether the CDA backend crosses the
  discover-don't-install boundary worker CLIs do not." The value evidence
  cited there — "nothing cleared the 0.7 threshold (merge-action confidence
  0.36–0.52 across 13 grades), grading is advisory, off by default, and
  heavy (multi-GB, minutes per grade)" — is an argument *against* building
  first and deciding later. The zoom-out remedy is staging, not more
  specification: decide C first; if C3 wins, delete §§5–8 and §10 and keep
  only environment detection + the guide fallback (already specified as
  branch 3 in §5.4). If C2 wins, scope the runner to writable, persistent
  environments only. Do not carry the full runner design as the default
  while C is open.
- **Simplicity rationale (standard §2, YAGNI):** You do not design a
  durable, cross-repo, multi-phase job system for a feature whose
  existence is an open human question.

### B2 — The central verification layer (layer 2) rests on a backend API that does not exist in the cited code

- **Severity:** Blocker
- **Section / lines:** §5.1 (lines 280–298); §5.2 (lines 299–332, esp.
  "constructs the backend object without loading the checkpoint, if the
  backend API allows it; otherwise the probe stops at the import step and
  says so plainly"); §12.4 (lines 1242–1246, open question).
- **What is wrong:** §5.1's rule — "The panel must never show 'installed'
  on layer 1 alone" — is the design's main defence against the
  half-installed state (§4, first bullet). But §5.2's layer-2 probe is
  conditional on an API the cited code does not provide. In
  `src/jev_agent/code_orchestrator/laya_backend.py`,
  `LayaBackend.__init__` (lines 459–461) stores `model_id` and `agent` and
  "never imports `laya` or downloads weights"; the only load path,
  `LayaBackend._agent_or_load` (lines 463–488), calls `laya.load(self.model_id)`,
  which is the checkpoint-loading path. There is no "construct without
  loading the checkpoint" entry point that proves loadability, and
  constructing the object proves nothing at all — it succeeds whether or
  not `laya`/torch work. So the probe's two outcomes are: (a) import
  `laya` + torch, which is layer 1, not layer 2; or (b) `laya.load`, which
  is layer 3 (checkpoint download, minutes, 2–3 GB RSS). The design's own
  fallback — "otherwise the probe stops at the import step" — therefore
  violates its own §5.1 rule: it flips, or permits flipping, on layer 1
  alone, exactly the outcome §4 warns is "worse than a missing one"
  (a broken backend that blocks the quality gate fail-closed, per
  `backend.py`'s `CodeOrchestratorError` vs `CodeOrchestratorUnavailable`
  distinction, lines 301–307).
- **Evidence:** Design §5.2: "The same subprocess constructs the backend
  object without loading the checkpoint, if the backend API allows it."
  Code: `laya_backend.py:459-461` (`__init__` does no import/load) and
  `laya_backend.py:483` (`self._agent = laya.load(self.model_id)`).
  Design §8 compounds this by making the backend change "modify only if
  needed", so the loadability entry point — the thing layer 2 depends on —
  is an optional, implementation-phase afterthought, not a decided
  interface. Until that entry point is specified (or the design admits
  layer 2 = import probe and drops the "never layer 1 alone" rule), §5.1
  is unenforceable.
- **Simplicity rationale (standard §§1, 3):** A three-layer model whose
  middle layer has no implementation is not a simplification; it is a
  distinction without a mechanism, and it hides the real choice, which is
  import-probe vs full-load-probe, with their very different costs.

### B3 — The "normative" deployment matrix is incomplete at its hardest cell, and its fallback branch depends on an unproven grade-path change

- **Severity:** Blocker
- **Section / lines:** §5.4 (lines 373–429, deployment matrix, Docker row:
  "See option set G"); §3 Goals (line ~176, "with a stated persistence
  answer for every deployment shape, including Docker"); §12.2, §12.6
  (lines 1233–1256).
- **What is wrong:** §5.4 declares: "The matrix is normative: an
  implementation that shows the button on a deployment shape without the
  row's UI statement has not implemented this design." Yet the Docker
  row — the deployment the brief calls "the largest open risk in the
  concept" — contains no UI statement, only a pointer to option set G,
  which itself defers the volume-vs-ephemerality decision to the human.
  Likewise, branch 2 (dedicated venv, §5.4) is the only path for PEP 668
  system Python, Windows service, and VPS service accounts, but it
  requires that "the grade runner must accept an interpreter override;
  if it cannot, this branch is unavailable" — and §12.6 admits whether
  that override "exists or must be added is an implementation-phase
  finding". In the cited code, `code_grade_runner.py` has an executor seam
  (`CodeGradeExecutor`, `_default_executor`), not an interpreter override,
  so branch 2 is currently a hope, not a path. If branch 2 is unavailable,
  the PEP 668 / service / VPS rows collapse to branch 3 (the guide) —
  which is option C3 again (see B1). The design therefore claims
  universality ("same endpoint everywhere") while its two hardest
  deployment families are placeholders.
- **Evidence:** Design §5.4 Docker row: "See option set G … The button
  must never be shown on Docker without that statement" — a statement
  that does not yet exist. Design §5.4 branch 2: "if it cannot, this
  branch is unavailable and the design falls to branch 3." Dockerfile
  (verified): `USER jev`, non-root, workdir `/data` — nothing in the
  cited files proves that user can `pip install` into the dashboard's
  environment at all, which is exactly §12.2's open question.
- **Simplicity rationale (standard §§1, 2):** Do not present a matrix as
  normative when its hardest row is "to be decided". Either narrow the
  button to the rows that are proven (C2) or decide Docker/G before
  calling the matrix complete.

---

## Majors

### M1 — Option sets D, E, G, and H turn a button into a config writer, a lifecycle manager, and a stop/confirm subsystem (scope creep)

- **Severity:** Major
- **Section / lines:** §9.D (lines 971–1000), §9.E (1001–1029), §9.G
  (1053–1096), §9.H (1097–1130); §2 (lines 149–152, "No runner today writes
  config"); §5.5/§6.6/§10 Rollback.
- **What is wrong:** The issue is an Install button. The design adds, as
  open options: auto-enabling gate grading (D2 — the install job becomes
  "the first runner that writes config", crossing a boundary §2 flags as
  unprecedented); a stop mechanic with per-phase semantics (E1/E3, plus
  §6.6's four stop rows); uninstall with a pre-existing-torch guard and a
  checkpoint-cache policy (G2); and a confirm page (H1). Each is
  defensible alone; together they make the install job a lifecycle
  manager for a backend whose version policy is itself undecided (G:
  pin vs `laya>=0.4.15`, `pyproject.toml:46-48` confirms the unpinned
  extra). YAGNI says: ship G1 (first install only) + D1 (no config write)
  + the smallest stop/confirm choice, and let update/uninstall/auto-enable
  earn their place from a real user request. Instead the design specifies
  all of them in enough detail (§10 rollback's manual commands, §6.6's
  resume rules) that an implementer must build or explicitly reject each.
- **Evidence:** Design §9.D2: "the install job becomes the first runner
  that writes config"; §9.G2: "uninstall must define what happens to the
  checkpoint cache … and to other tools sharing torch … guarded by
  refusing uninstall when torch was pre-existing, which the implementer
  must detect and state." That guard alone is a new detection subsystem.
- **Simplicity rationale (standard §2):** Every option set that survives
  into implementation is scope. Eight option sets, most with three
  options, is not a minimal design; it is a menu of future features.

### M2 — Reuse is deferred, not decided: the design copies the existing runners' machinery instead of reusing it

- **Severity:** Major
- **Section / lines:** §9.B (lines 891–921); §6.3, §6.6, §6.10, §6.11, §8
  (lines 765–855).
- **What is wrong:** The cited code already contains the machinery this
  design re-specifies. `BrowserInstallRunner` (`browser_install.py:76-237`)
  already has: an injectable spawn seam (`_spawn`, line 64), a lock
  (`_guard`), `status()`/`busy()` (lines 99, 154), a 30-line tail
  (`_LOG_TAIL_LINES = 30`, line 35; `_tail`, line 114), a 256 KB cap
  (`_MAX_LOG_BYTES = 262_144`, line 41; `_cap_log`, line 133),
  terminate-on-stop (line 193), and `shutdown(timeout)` (line 231).
  `CodeGradeRunner` (`code_grade_runner.py:145-385`) already has the
  live-polling status pattern and draining-cancel semantics, and
  `routes_code.py:163-169` already computes elapsed time for a live
  fragment. The design restates all of these constants and patterns
  (§6.3 "last 30 lines", §6.10 "capped … 256 KB", §6.6 "terminates the
  pip process") as if they were new decisions, then offers B2
  ("Deliberate third copy") as a coequal option — i.e. it legitimises
  the triplication that §4 itself calls out ("A third bespoke runner
  triples a duplicated skeleton"). B1/B3 are the reuse answers; B2
  should not be on the menu without a stronger justification than "no
  regression risk", especially since the design's own B2 failure mode
  is "a later fix lands on one runner and not the others".
- **Evidence:** Design §9.B2: "The install runner mirrors
  `BrowserInstallRunner` and does not share code." Code:
  `browser_install.py:35,41,99,154,193,231` (tail, cap, status, busy,
  stop, shutdown). `DashboardCore` (`core.py:63-72`) already owns five
  runners/flows (chat, scheduler, browser-install, reverse, grade);
  §8 adds a sixth plus a new coordination lock without ever asking
  whether the coordination lock belongs in a runner at all (see M4).
- **Simplicity rationale (standard §4, Leverage Existing):** When
  equivalent components exist and are stable, reuse is the default.
  Presenting deliberate duplication as an equal option violates it.

### M3 — Altitude drops: the design freezes implementation detail (copy strings, test files, log-truncation rules) before any option is picked

- **Severity:** Major
- **Section / lines:** §6.2 (lines 481–505, 11-row taxonomy), §6.5
  (lines 556–579, log-copy rule), §6.8 (632–655, 10-row matrix),
  §6.10 (681–692, redaction), §8 (765–855, file and test-file list),
  §10 (1131–1190, 9 platforms × ~12 failure classes).
- **What is wrong:** A high-level design should decide architecture,
  interfaces, and data flow. This design also decides: exact UI copy for
  eleven failure classes; that "a second install … truncates the old log
  only after copying it to the retained full log"; that pip output is
  "stored as-is except that any credential-looking token in a URL is
  redacted"; three new test-file names
  (`test_cda_install_runner.py`, `test_cda_install_routes.py`,
  `test_cda_install_matrix.py`); and a test matrix crossing nine
  platforms with every failure class, stop-during-install,
  restart-during-install, grade-during-install, double-submit, and
  partial-install recovery. That is implementation-phase work, and
  freezing it now — while §§9.A–H are still open — guarantees rework
  when the human picks, e.g., E2 (no stop route, invalidating §6.6's
  stop rows and the stop tests) or G1 (no uninstall, invalidating
  §10's uninstall rollback). The standard's anti-weeds rule exists for
  exactly this: detail that will be rewritten is not rigour, it is
  drag.
- **Evidence:** Design §6.5: "A second install after an interruption
  truncates the old log only after copying it to the retained full log,
  so the interruption evidence is not destroyed by the retry." Design
  §8: "tests/unit/test_cda_install_matrix.py (new): the platform ×
  deployment decision-tree tests from section 10, with the environment
  detection stubbed per cell."
- **Simplicity rationale (standard §3, Anti-Weeds):** Keep the design at
  interface level; move copy, truncation rules, and the full test matrix
  to the implementation plan after the option sets are decided.

### M4 — Non-minimal data flow: install state is fanned out to every repo page, a new status surface, a durable record, and structured events

- **Severity:** Major
- **Section / lines:** §6.4 (525–555), §6.5 (556–579), §6.7 (615–631),
  §6.9 (656–680), §6.10 (681–692), §6.11 (693–705).
- **What is wrong:** One button's state is specified to flow through:
  a banner on the Code Grade page of *every* repo (§6.9, "because the
  backend is shared"); a new route pair plus live fragment (§6.7);
  a durable job record in the dashboard state directory (§6.5, a new
  location distinct from both the repo `.jev/` log and the ledger);
  structured attempt events (§6.10); a coordination lock in
  `DashboardCore` (§6.4); and a shutdown ordering rule (§6.11). That is
  at least five surfaces for one job, before counting the panel's
  Backend row. The minimal flow for the narrowed/guide options is far
  smaller: detection → render (C3) or detection → one runner → one
  status page (C2). The cross-repo banner is the clearest
  overreach: it is justified by "the backend is shared", but the
  checkpoint that makes a grade work is per-repo `model_id` (§6.9
  admits pre-warming "warms the current repo's model only"), so the
  banner advertises a machine-wide fact on pages where the next grade
  may still download a different model. A shared banner for a
  partially-shared outcome is misleading, not just extra.
- **Evidence:** Design §6.9: "The install status banner appears on the
  Code Grade page of every repo while an install runs, because the
  backend is shared" and, in the same section, "pre-warming warms the
  current repo's model only, and other repos' models still download on
  their first grade."
- **Simplicity rationale (standard §2, Minimalist Data Flow):** Data
  should pass through the minimal number of components. Five surfaces
  for one install is not minimal.

### M5 — A new durable subsystem and observability layer are specified without evidence they are needed (premature infrastructure)

- **Severity:** Major
- **Section / lines:** §6.5 (durable job record, `interrupted` state);
  §6.10 (structured attempt events, retained full log); §6.1 (preflight
  subsystem: disk, memory, network, cache checks).
- **What is wrong:** Both existing runners are in-memory only:
  `BrowserInstallRunner._state` (browser_install.py:86-94) and
  `CodeGradeRunner`'s in-memory status (code_grade_runner.py:183-198)
  lose their state on restart, and the product has shipped with that.
  The design replaces that with a durable record (fixed argv, target
  decision, phase, exit code, verification outcome), an
  `interrupted`-on-startup reconciliation rule, orphan-child handling,
  and structured events with failure classes — a small persistent job
  store, with its own location, retention, and truncation policy, none
  of which is justified by a measured restart-mid-install frequency.
  The design's own fallback concedes the point: "If the implementer
  cannot provide a durable record, the fallback is … 'availability
  unknown — re-check and retry' after any restart, and the design's
  restart acceptance criterion (section 10) fails" (§6.5). A fallback
  that fails the acceptance criterion is not a fallback; it is an
  admission that the durable store is load-bearing for a rare event.
  The same applies to §6.1's preflight: disk/memory/network/cache
  checks are a new detection subsystem whose thresholds are vague
  ("at least the 2–3 GB the backend needs at runtime, or a warning"),
  not measured.
- **Evidence:** Design §6.5: "The install job record is durable, not
  in-memory only. It lives in a fixed location (the dashboard state
  directory, not a repo's `.jev/` directory …)". Code:
  `browser_install.py:86-94` (in-memory `_state`, `_proc`, `_thread`)
  and `core.py:110-126` (`shutdown_jobs` already handles the existing
  runners without any durable record).
- **Simplicity rationale (standard §2, Premature Optimization):** Do
  not build persistent infrastructure for an unproven frequency. Start
  in-memory like the existing runners; add durability only if
  restart-mid-install is shown to strand users.

### M6 — The trust-boundary expansion is noted but not narrowed: a host-mutating pip install inherits the browser precedent's local-only assumption without its local-only scope

- **Severity:** Major
- **Section / lines:** §7.2 (lines 735–764); §2 (lines 140–148,
  loopback/token rules); §9.H (confirm step deferred).
- **What is wrong:** The browser-install precedent the design leans on
  is, by the design's own words, "explicitly local-only" (§7.2). The
  CDA install is categorically different: it runs `pip` into the
  dashboard's own interpreter — a host mutation that persists across
  restarts (except on Docker) and touches a multi-GB dependency tree —
  yet §7.2's answer is to inherit the existing gating unchanged: on
  loopback, "any local visitor can press Install" ("Anyone on this
  machine can install the backend"), and off loopback, any
  token-holder can. Single-flight is then offered as the rate limit
  ("No additional rate limit is specified; a second submit sees the
  running job … which bounds abuse to one pip process"). Bounding
  abuse to *one* multi-GB host mutation is not a bound a sceptical
  architect accepts without argument, and the confirm step that would
  at least force a deliberate click is deferred to option set H — so
  the design as written permits H3 (no confirm, small text under the
  button) for a host mutation. The zoom-out point is not that the
  token/CSRF mechanics are wrong (they are correctly inherited from
  `security.py`'s `checked_form`, lines 265+, and the token middleware);
  it is that the design expands *what* an authenticated visitor can do
  to the host without ever justifying why the narrowest scope
  (local-only, or confirm-first as the default) is not the default.
- **Evidence:** Design §7.2: "On loopback, the existing rule applies:
  any local visitor can reach the dashboard, so any local visitor can
  press Install" and "Rate limiting: one install at a time
  (single-flight) is the limit. No additional rate limit is specified."
- **Simplicity rationale (standard §4, Identify Anti-Patterns):** A
  host-mutating endpoint should default to its narrowest scope and
  expand only on justification, not inherit a precedent whose scope
  was local-only.

### M7 — The install conflates two scopes (shared runtime vs per-repo checkpoint), and the design papers over it with UI copy

- **Severity:** Major
- **Section / lines:** §6.9 (lines 656–680); §5.1/§5.2 (checkpoint layer);
  §6.8 (state matrix).
- **What is wrong:** The runtime (`laya` + torch) is machine-wide; the
  checkpoint is per `model_id`, and `model_id` is per-repo config
  (§6.9: "the status is built from that repo's config (including its
  `model_id`)"). The design resolves this mismatch not structurally
  (e.g. separate runtime-install and checkpoint-warm actions, or a
  machine-level page) but with copy: "The UI must say which model's
  checkpoint was warmed, by model id" and "a finished install flips
  every repo's panel, because availability is a property of the shared
  environment". But availability in the cited code is *not* purely
  shared-environment: `CodeOrchestrator.available`
  (backend.py:331-340) constructs a backend for a given `model_id`,
  and a repo whose model differs from the warmed one still faces a
  first-grade download. Flipping every repo's panel to "available"
  while only one repo's checkpoint is warm overstates what was
  installed, and the state matrix (§6.8) has no row for
  "runtime installed, this repo's checkpoint not warmed, another
  repo's warmed". This is a structural scope mismatch, not a copy
  problem, and the right zoom-out fix is to split the actions or
  scope the button to the runtime only (with checkpoint download
  honestly left to first grade, i.e. option A1) — not to add a
  banner to every repo.
- **Evidence:** Design §6.9: "Across repos, a finished install flips
  every repo's panel" alongside "pre-warming warms the current repo's
  model only". Code: `backend.py:243` (`load_backend(model_id)`) and
  `routes_code.py:48-55` (`_status` builds availability from the
  current repo's settings).
- **Simplicity rationale (standard §3, Right Placement):** Logic that
  belongs to two scopes should not be presented as one button's
  outcome. Split it, or scope it honestly.

---

## Nits

### N1 — "One-click" in the title and §1 is contradicted by option H1

- **Severity:** Nit
- **Section / lines:** Title (line 1, "one-click backend install");
  §1 (line ~22, "One click installs"); §9.H1 (lines 1097–1112);
  §10 acceptance (lines 1133–1139, "one click (plus a confirm click
  if option set H picks one)").
- **What is wrong:** If H1 wins, the flow is two clicks by the design's
  own admission ("so 'one-click install' becomes 'two-click install'
  with the first click being consent"). The title should not promise
  one click before H is decided. Rename to "Install button" until H
  is picked, or make H1's cost explicit in the title's claim.
- **Evidence:** Design §9.H1: "it is a second click, so 'one-click
  install' becomes 'two-click install'".

### N2 — "Honest sizes" includes a non-size

- **Severity:** Nit
- **Section / lines:** §5.3 (lines 333–372, "Honest sizes the UI must
  state before the user clicks").
- **What is wrong:** "`laya` plus transformers: the size pip reports
  for the resolved set, or 'a few hundred MB' when it cannot be known
  in advance" is not an honest size; it is a placeholder that the
  section's own standard ("the UI must state") forbids elsewhere
  (the same section correctly refuses an invented percentage in
  §6.3). Either resolve the size at preflight (pip dry-run) or admit
  the size is unknown and say so, rather than substituting a vague
  range under an "honest sizes" heading.
- **Evidence:** Design §5.3, quoted above.

### N3 — The state matrix is incomplete despite claiming to be the rows the UI "must render correctly"

- **Severity:** Nit
- **Section / lines:** §6.8 (lines 632–655).
- **What is wrong:** The matrix has 10 rows but omits combinations its
  own rules create: Enabled=yes with Backend=not installed and
  Install job=running/failed/stopped/interrupted, and
  Enabled=either with Backend=installed-but-not-loadable and a
  repair job running. If the matrix is the contract, it must be
  exhaustive or explicitly say omitted rows collapse to a named row.
  (The deeper zoom-out point is in M3/M4: a 10+-row matrix for one
  button is itself a complexity smell; exhaustiveness would make it
  larger, which is an argument for fewer states, not more rows.)
- **Evidence:** Design §6.8 rows list only "yes / not installed / none"
  and "either / available / running (reinstall/repair)" for the
  enabled-yes and running cases respectively.

### N4 — No unified job-state vocabulary, despite three existing dialects

- **Severity:** Nit
- **Section / lines:** §6.5 (`interrupted`), §6.6 (`stopped`),
  §6.8 (installing/failed/stopped/interrupted); §2 precedents.
- **What is wrong:** The design introduces `interrupted` and
  `verifying` alongside the existing dialects —
  `BrowserInstallRunner` uses `idle/running/stopped/done/failed`
  (browser_install.py:86-94, 193, 205-230) and `CodeGradeRunner`
  uses running/cancelled/draining (code_grade_runner.py:20-22, 248).
  No mapping table is given, so the new status page, the panel's
  Backend row, and the banner can each render a different word for
  the same state. This was flagged as a nit in the brainstorm brief
  (state vocabulary drift) and the design still does not resolve it.
- **Evidence:** Design §6.8 panel words: "installing", "failed",
  "stopped", "interrupted" — none mapped to the two existing runners'
  vocabularies.

### N5 — §8's file list is conditional in a way a "contract for the implementation phase" should not be

- **Severity:** Nit
- **Section / lines:** §8 (lines 765–855, esp. `backend.py` "modify only
  if needed", `browser_install.py` "modify only if option set B picks
  the shared scaffold", `docs/dashboard.md` "if that doc describes the
  page").
- **What is wrong:** Three of the listed files are conditional, and
  the backend conditional (B2's loadability entry point, see B2) is
  the most consequential of them. A contract that says "modify only
  if needed" for the file that would carry the central verification
  mechanism is not a contract. Decide the probe interface first (B2),
  then list files unconditionally.
- **Evidence:** Design §8: "`src/jev_agent/code_orchestrator/backend.py`
  (modify only if needed): if the loadability probe needs a
  backend-construction entry point that does not load the checkpoint,
  add it here; otherwise no change."

---

## What would make this design pass Zoom Out

1. **Decide C first, in isolation.** If C3 or C2 wins, this design
   shrinks to detection + guide (or detection + a narrowed runner)
   and §§5–8/§10 are largely deleted. Do not approve the full design
   with C still open.
2. **Specify the loadability probe as a real interface, or drop
   layer 2.** Either add a no-checkpoint-construction entry point to
   `LayaBackend` (and prove it distinguishes broken torch from working
   torch) or admit the probe is an import probe and amend §5.1's
   "never layer 1 alone" rule accordingly.
3. **Decide the runner shape (B) and the Docker row (G) before
   calling the matrix normative.** B2 (deliberate third copy) should
   be removed from the menu unless a stronger justification than
   "no regression risk" is given; the Docker persistence statement
   must exist, not be pointed at.
4. **Cut the option sets to the minimal set:** D1, G1, one stop choice,
   one confirm choice — and move copy strings, the full failure
   taxonomy, the exhaustive test matrix, and the durable-record
   design to the implementation phase, after those choices are made.
5. **Scope the button honestly:** runtime-only, or split runtime
   install from per-model checkpoint warming, instead of flipping
   every repo's panel and adding a banner to every repo.

Until (1)–(3) are done, the design is a thorough specification of a
system that has not yet earned its right to exist at this size.
