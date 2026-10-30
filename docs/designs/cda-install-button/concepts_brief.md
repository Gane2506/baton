# CDA install button — concepts brief

* **Author:** Hana (for Gane)
* **Date:** 2026-10-29
* **Tracking issue:** Gane2506/baton#366
* **Goldfish brainstorm review:** 2026-10-30, six lenses
  (design_critic, codebase_details, zoom_out, comprehension,
  implementation_readiness, structural). Raw lens reports in
  `docs/designs/cda-install-button/goldfish-brainstorm/`; synthesized findings folded into Analysis below.
  Counts: 16 Blockers, 9 Majors, 5 Nits. The lenses' shared verdict:
  the concept is not ready to draft from as written — the findings
  below are what the draft must answer, not reasons to abandon the
  button. The one finding that questions whether to proceed at all
  (the value question, Major 9) is for the human to decide, not for
  this brief to settle.

## Overview

The Code Grade page (`/code-grade`) shows the Coding Decision Agent as
"not installed" and tells the user to run
`pip install "jev-agent[code-orchestrator]"` in a terminal. This brief
covers replacing that instruction with an Install button on the same
page: one click installs the backend server-side, in the background, in
the dashboard's own environment, with progress in the browser and
verification afterwards. The same button must work on every machine the
dashboard runs on — Mac, Windows, Linux, and a VPS — because the
install runs where the dashboard server runs, not in the browser.

## Analysis

### What exists today

- The status panel (`src/jev_agent/dashboard/templates/code_grade.html`)
  renders Enabled, Model, Confidence threshold, and Backend (available
  vs "not installed"), plus the pip command when the backend is missing.
- Availability is `CodeOrchestrator.available`
  (`src/jev_agent/code_orchestrator/backend.py`): true only when a
  backend is already loaded or loadable now. A missing backend surfaces
  as `CodeOrchestratorUnavailable`, never as a guessed grade. That
  behavior must not change.
- Two background-job precedents already exist in the dashboard:
  - `BrowserInstallRunner`
    (`src/jev_agent/dashboard/browser_install.py`): one install at a
    time, output streams to a log file under `.jev/`, the page shows
    the log tail, the install can be stopped, and the command is fixed
    — no request can change it. The button asks for confirmation first,
    naming the exact command.
  - The grade runner (`src/jev_agent/dashboard/code_grade_runner.py`):
    one grade at a time in a background thread, with a status page
    whose live region polls the job, and honest cancel semantics (the
    model call itself cannot be interrupted mid-inference, so cancel
    discards the result and the runner drains).
- Grading is off by default. The enable toggle lives under Settings →
  Code orchestrator.

### Critique first: what is wrong, missing, or risky

- **The pip command breaks the product's own law.** Browser-first says
  no feature is done until it works from the browser with zero terminal
  use. A dashboard page that sends the user to a terminal to complete
  its own feature is unfinished by that standard.
- **"Install" is not one thing.** `pip install` gets laya plus torch.
  The ~842 MB model checkpoint downloads separately, on the first
  grade. An install button that reports success after pip alone leaves
  the user facing a multi-minute first grade with no warning. The
  design must say plainly what "installed" means: pip done, or pip plus
  checkpoint pre-warmed.
- **Docker deploys break the naive version.** The Linux deploy runs
  Baton from a pinned Docker container. A pip install inside that
  container vanishes on the next container restart. "Works on a VPS"
  is false unless the design answers persistence: a volume, a
  documented rebuild step, or an explicit statement that Docker
  installs are ephemeral and must be repeated. This is the largest
  open risk in the concept.
- **Environment detection is where installs fail.** The dashboard may
  run from a venv (Mac `~/jev-venv`), a system Python, or a service
  account (VPS, Windows service). `sys.executable -m pip` into the
  dashboard's own environment is the sane default, but a read-only or
  system Python needs a fallback (`--user`, or a dedicated venv) and a
  plain-English error when neither works. Silent failure here strands
  the user exactly where the pip command did.
- **A runaway or half-finished install is worse than none.** Pip
  interrupted mid-install leaves a partial environment; re-running
  must be safe (pip is idempotent per package, but the UI must say so
  and offer retry, not a dead page). A stop button that kills pip
  mid-torch-download needs the same honesty as grade cancel: what was
  stopped, what state the environment is in, and that retry is safe.
- **Install and grade must not fight.** The grade runner allows one
  grade at a time because the model is a multi-GB torch process. An
  install running while a grade runs (or the reverse) contends for CPU,
  memory, and disk. The design must state whether the two runners
  share one lock or are independent, and what the user sees when one
  blocks the other.
- **A fixed command is a security requirement, not a nicety.** The
  browser-install precedent fixes the command server-side so no
  request can change it. An install endpoint that accepted a package
  name from the browser would be remote code execution by design. The
  CDA install command must be equally fixed.

### Counter-arguments

- **Why not keep the pip command?** Because it works today and costs
  nothing to build. The answer is the product law, not engineering:
  every setup step must work for someone who never touches a terminal.
  Keeping the command keeps the feature terminal-gated.
- **Why not auto-install on first grade?** It would remove the button
  entirely. But it hides a multi-minute, multi-GB download behind a
  "Grade diff" click, with no consent and no progress story, and it
  fails the same way on Docker without solving persistence. An
  explicit button with progress is more honest than a surprise
  download.
- **Why not bundle the model with Baton?** Size and licensing weight:
  ~842 MB plus torch would bloat every install for a feature that is
  off by default. The opt-in extra stays the right shape; only the
  way it gets installed changes.

### Synthesized Goldfish findings (2026-10-30)

Deduplicated across all six lenses. Lens tags show which lenses raised
each finding (dc = design_critic, cd = codebase_details, zo = zoom_out,
co = comprehension, ir = implementation_readiness, st = structural).
Detail lives in the raw reports under `docs/designs/cda-install-button/goldfish-brainstorm/`.

#### Blockers

1. **Verification by `available` proves presence, not a working
   backend (all six).** `load_backend` checks `find_spec("laya")`
   only, deliberately without importing torch. A pip run that leaves
   torch broken, partial, or wrong for the platform still flips the
   panel to "installed" — and the first real grade then fails as an
   inference error, which blocks the quality gate fail-closed, where
   a missing backend would have let the run proceed. A half-installed
   backend is strictly worse than a missing one. The draft must define
   verification as loadability (for example a fresh-subprocess import
   smoke check, never an import inside the dashboard process), handle
   import-cache invalidation after install, and provide a repair path
   for a verified-but-broken install.
2. **The fixed command is never stated, and the obvious one is
   wrong (all six).** Three wordings exist today: the template and
   grade runner say `pip install "jev-agent[code-orchestrator]"`;
   `backend.py` says `pip install laya`; the docs' CPU path is a
   two-step CPU-torch-wheel-then-`laya` sequence. Running the page's
   command verbatim from the dashboard is self-modification: it asks
   pip to install the dashboard's own package into its own running
   environment. The draft must print the exact fixed sequence per
   platform, state that `jev-agent` itself is never
   installed/upgraded by it, and set a `laya` version policy (today
   unpinned above, `laya>=0.4.15`) — including what a reinstall does
   when a backend is already present.
3. **"Same button everywhere" contradicts the deploy model (all
   six).** The Docker image is dashboard-only and non-root with
   pull-a-new-image upgrades; a container-side pip install is refused
   or wiped on recreate. `pipx upgrade` recreates the native venv,
   stranding an extra pip-installed into it. "The dashboard's own
   environment" is not one thing across pipx, editable checkout, PEP
   668 system Python, service-managed dashboards, and Docker. The
   draft must give a platform-by-deployment matrix (OS × launch mode)
   with a persistence answer per cell — volume, rebuild step, or an
   explicit "ephemeral, must be repeated" statement shown in the UI
   — plus the read-only/non-root failure message. Until that table
   exists, the "works on a VPS" claim must not appear in the UI.
4. **The true footprint is not ~842 MB or ~1 GB (all six).** The
   default torch wheel carries ~2.5 GB of CUDA libraries; add `laya`,
   transformers, the 842 MB checkpoint, and temp staging, and first
   use is several gigabytes and many minutes. A CPU-wheel-first
   install is a different command with its own index URL and order.
   The draft must state the torch strategy (CPU wheel first, CUDA
   when detected, or default wheel accepted with its size stated
   honestly), the detection method, and honest size/time per path.
5. **"Installed" is three layers, and `available` checks only two
   (all six).** (a) pip packages present, (b) backend loadable,
   (c) checkpoint downloaded and loadable. The checkpoint goes to the
   Hugging Face cache (`~/.cache/huggingface/hub` or `$HF_HOME`) —
   a different location from site-packages with its own persistence
   story — and the first load reconstructs it through the temp dir,
   which fails on a 512M tmpfs `/tmp` ("No space left on device")
   unless TMPDIR points at a real disk. No "download the checkpoint
   without grading" function exists; the only fetch path is a real
   grade, which loads torch (2–3 GB RSS) and takes minutes. The draft
   must define "installed" as one checkable state, name the cache
   location and TMPDIR handling per platform, state partial-download
   resume behaviour, and — if pre-warming is chosen — give it its own
   job that writes nothing to the grade ledger.
6. **The target environment is a decision tree, not a default (all
   six).** PEP 668 refuses system-Python pip installs without
   `--break-system-packages` (a stance the draft must allow or forbid
   explicitly); service accounts may have no writable home or no pip
   on PATH; a dedicated venv needs a location, an owner, and a
   discovery path the dashboard process will use on the next start;
   `--user` installs may not be visible to the dashboard interpreter's
   own `available` check. The draft must contain the detection rule,
   the ordered fallback list with the exact condition per branch, and
   the plain-English error per terminal failure.
7. **A third bespoke runner triples a duplicated skeleton (st, zo,
   dc).** Two runners plus `ReverseRunner` already re-implement the
   same skeleton (lock, thread, state, timestamps, `busy()`,
   `status()`, shutdown hook), each carrying fixes the others lack
   (log cap in the browser runner, generation counter and draining in
   the grade runner). The draft must either extract a shared job
   scaffold all runners use, or state explicitly that the CDA runner
   is a deliberate third copy and name who ports future lifecycle
   fixes across all three.
8. **All install state is in memory; restart mid-install is
   undefined (st, ir, dc, cd).** A restart reports `idle`, the old log
   tail lingers, the old pip child is orphaned (and the next install
   truncates its log), and two pip processes can run at once despite
   the in-memory single-flight guard. The draft must define a durable
   job record (command, target environment, started time, exit code,
   verification outcome), an `interrupted`/`unknown` state on startup
   when a record says "running" but no process is alive, and the rule
   for leftover children and logs. If it chooses no durable record, it
   must say the page can only report "availability unknown, re-check
   and retry" after any restart.
9. **Install/grade coordination is missing, and the hazard is file
   replacement, not just contention (st, ir, dc, cd).** Each runner's
   lock guards only itself, in one process. Pip replacing `laya`/torch
   files under a running grade thread, a grade holding torch files
   while pip installs (Windows file locking), and a grade submitted
   mid-install ledgering a spurious "unavailable" record are all
   unhandled. The coordination point must live above the runners
   (owned by `DashboardCore` or equivalent), with blocked states in
   both directions, a rule for whether a draining grade blocks an
   install, and what each page shows while blocked.
10. **The pip instruction lives on three surfaces, not one (cd,
    ir).** The status panel (`code_grade.html`), the failed-job error
    text (`code_grade_runner.unavailable_message()` on
    `/code-grade/status`), and the Learning page Ratings-tab empty
    state all print it. Changing only the panel leaves terminal
    instructions next to the button. The draft must enumerate every
    surface and state what each shows after the change.
11. **There is no precedent for a self-modifying Python install (cd,
    st, ir).** The browser precedent installs a Node tool via npx; it
    never runs pip, never touches the running interpreter, and its
    PATH-based resolution is the wrong resolution here (PATH pip ≠
    dashboard-interpreter pip; the draft must use
    `sys.executable -m pip`). The draft must name the new routes
    (today: `/code-grade`, `/grade`, `/route`, `/status`, `/live`,
    `/cancel` — no install/stop pair), the runner's home in
    `DashboardCore` and its place in `shutdown_jobs()`, CSRF handling
    per the `checked_form` precedent, and test seams (injectable
    spawn, availability seam) matching the existing runners.
12. **Authorization is unstated: who may click Install (co, ir, zo,
    dc, st).** The fixed command answers which package, not who may
    trigger a host mutation. The browser-install precedent is
    explicitly local-only; a VPS dashboard is token-gated and
    network-reachable, and a per-repo page triggering a machine-wide
    side effect sharpens the question. The draft must state the
    assumption (local-only, authenticated user, or any page visitor),
    inherit loopback/token gating and CSRF explicitly, keep or
    explicitly drop the confirm-first step with reasoning, and say
    what someone not allowed to use it sees.
13. **Broken-but-present backends never see the button (ir, co).**
    `available` is false for unloadable backends too, so "shown exactly
    when the backend is missing" misdescribes the trigger — and a
    present-but-broken backend (torch absent, version API drift,
    corrupted checkpoint cache) may read as available and never see
    any repair path at all. The draft must either add a
    repair/reinstall path with the condition that surfaces it, or
    explicitly record broken-but-present as out of scope with the
    message those users see.
14. **Stop semantics contradict themselves; "safe, retryable" is
    uncheckable (co, st, ir, dc).** Browser-install stop terminates
    the process; grade cancel cannot interrupt the model call and
    drains instead. The brief borrows both words without picking a
    mechanic. "Safe" is undefined (partial torch? half-downloaded
    checkpoint? pip lock left behind?), and retry-as-re-run is not
    retry-as-resume. The draft must pick one stop mechanic, define
    "safe" as a checkable state per phase (download, unpack, install,
    pre-warm), and state the exact post-stop message — or drop the
    stop button and say install-to-completion plainly.
15. **Progress has no home; the panel cannot hold two jobs and three
    booleans (st, co, cd).** The template renders Enabled, Backend,
    and one grade-job flash; an install adds a second job (command,
    exit code, log tail) and a third boolean family (install running /
    done / failed / stopped, pip vs checkpoint). Reusing
    `/code-grade/status` forces an install to fake a grade's fields;
    keeping progress only on `/code-grade` loses it on navigation,
    breaking the precedent that closing the tab loses nothing. The
    draft must give the install job its own status surface and write
    out the full Enabled × Backend × Install-job state matrix,
    including second-click behaviour and tab-close/restart survival.
16. **The install is machine-wide; the page and log are repo-scoped
    (dc, co, zo).** The button sits on a per-repo page (status built
    from that repo's config, including its `model_id`), and the
    browser precedent's log lives under that repo's `.jev/`. A pip
    install targets an environment shared by every repo the dashboard
    serves. The draft must state the install's scope (machine, venv,
    or repo), where the log lives, and what happens across repos and
    model IDs.

#### Majors

1. **No preflight before a multi-gigabyte download (dc, st, ir).**
   Free disk (covering the actual torch variant plus checkpoint plus
   temp staging), available RAM, writable target, PyPI reachability —
   each failure mapped to a plain-English refusal before any download
   starts, not a started-then-failed download. On a small VPS a pip run
   that fills the disk can break the host itself.
2. **No failure taxonomy; plain-English mapping is new work (ir, co,
   cd).** The browser runner reports raw exit codes; the grade runner
   maps through a jargon rewrite table; neither maps pip. The draft
   must table each condition (network, proxy, disk full, permission,
   PEP 668, no wheel for this Python/arch, hash mismatch, killed
   process) with its message, whether retry is offered, and what must
   change before retry — plus log policy (tail lines, cap inherited
   from the issue #200 fix, poll interval).
3. **No acceptance criteria, test matrix, or rollback (ir).** The
   draft needs measurable criteria (button visibility rule,
   flip-without-restart condition per process, progress visibility,
   retry behaviour), a platform × failure test matrix (Mac venv,
   Mac system Python, Windows user install, Windows service, Linux
   venv, Linux system Python, VPS service account, Docker — crossed
   with success, each failure class, stop-during-install,
   restart-during-install, grade-during-install, double-submit,
   partial-install recovery), and a rollback section (what can be
   undone, what cannot, the manual recovery command per failure).
4. **Grade/route forms are ungated on availability (cd, co).** They
   render and submit while the backend is missing, starting a failing
   job that ledgers a spurious "unavailable" record. The draft must
   decide: disabled with the button as the only action, or left live
   with the failed-job path linking to the install action — not both
   competing calls to action on one page.
5. **Post-install state: installed ≠ enabled ≠ grading (cd, st, dc,
   co).** Backend installed, `[code_orchestrator] enabled` (default
   false), and on-demand page grading are three independent gates.
   After a successful install with grading still off, the panel says
   "available" but the gate grades nothing. The draft must name which
   gates the button flips and what the user sees when the others are
   still closed — and whether the install job writes config at all
   (today no runner writes config; auto-enable would cross that
   boundary and must be named, with disclosure copy).
6. **No lifecycle after install (zo, ir).** No version display, no
   update, no uninstall, no repair; the next Baton upgrade may wipe or
   strand the extra, and staleness has no detection or message. The
   draft must either include the minimal lifecycle (installed version
   display, uninstall, repair tested against a killed pip) or state
   explicitly that first install is the whole scope and the rest
   stays terminal — and accept what that does to the browser-first
   justification.
7. **Progress honesty: a log tail is not progress (st, dc, ir).**
   Pip output has no overall percentage and no time remaining; the
   honest UI is the existing pattern (log tail + state + elapsed
   time), structured as named phases (preflight → pip → verify →
   optionally warm-up) each with its own progress signal and failure
   message. No invented percentage unless a real denominator exists
   for the phase shown.
8. **Observability gap (cd).** Neither runner emits metrics or
   structured events beyond its log file and in-memory status. The
   draft should require at minimum: attempt start/finish/failure with
   a failure class (not just an exit code), and retention of the full
   log beyond the 30-line tail for diagnosis.
9. **The value question (zo, dc) — for the human, not this brief.**
   Measured on real diffs, nothing cleared the 0.7 threshold
   (merge-action confidence 0.36–0.52 across 13 grades; auto-accept
   and auto-reject effectively dead), and the feature's value is a
   human-review routing signal. Grading is off by default, the backend
   is heavy, and the codebase's established pattern is
   discover-and-guide (worker CLIs are found, not installed; the
   browser install is the lone local-only exception). The draft must
   justify why the CDA backend crosses the discover-don't-install
   boundary that worker runtimes do not — and present evidence that
   install friction, rather than the feature being opt-in, heavy, and
   advisory, is what blocks adoption. If that evidence does not hold,
   the cheaper fits are install-time opt-in, a status page that
   diagnoses the exact missing piece with a copyable command, or a
   button narrowed to environments where it can actually work.

#### Nits

1. **State vocabulary drift (st).** Browser runner says `stopped`,
   grade runner says `cancelled` plus `draining`; the new job needs at
   least `verifying` and `interrupted`. Define one vocabulary for
   dashboard background jobs, or explicitly reject a shared one —
   drifting into a third dialect by accident is the failure mode.
2. **Keep the numbers distinct (cd, ir).** Parameters (421M),
   checkpoint bytes (842 MB), download bytes (torch ~2.5 GB default
   wheel vs CPU wheel), RSS (2–3 GB), load time (~50 s), grade time
   (1–3 min small, up to ~10 min large). "Multi-GB" must not cover all
   of them.
3. **Precedent wording precision (cd, co).** The browser-install
   confirm step is a separate template, not a dialog; install start
   and stop are different tiers and routes; the fixed thing is the
   server-side argv list (a sequence, not a package name); "the page
   shows the log tail" is the Connections page showing
   `.jev/browser-install.log`. The draft should cite the template or
   route that renders each claimed UI element.
4. **Glossary (co).** Backend, installed, environment, grade, route,
   threshold, gate, laya, torch, checkpoint, VPS, service account,
   volume, endpoint, single-flight, log tail, draining — defined on
   first use, plus one sentence stating the intended audience
   (terminal-free end user vs developer-operator).
5. **Status-panel fields unexplained (co).** Model (a Hugging Face
   model ID), Confidence threshold (0.7, gating merge-action
   confidence and routing hints), Enabled (gate grading vs on-demand
   grading) — one plain sentence per field in the draft's overview.

### Alternatives the lenses rank (carried to the draft, not decided here)

1. **Copy button + "Check again" (cheapest, honest baseline).**
   Replace the bare pip command with the exact command for the
   detected environment (CPU torch first on CPU-only hosts, Docker
   rebuild snippet on Docker), a copy-to-clipboard button, and a
   "Check again" button re-running `available`. No server-side code
   execution, no runner, no lock, no stop semantics, no orphan
   handling — and it works on Docker and read-only environments, where
   the button cannot. What it gives up: zero-terminal operation on
   easy platforms.
2. **Environment-aware install guide (no execution, better
   diagnosis).** Detect the environment server-side and render the
   correct command plus a preflight verdict ("3.1 GB free, need ~4 GB
   — free space first"). Solves the wrong-torch, Docker-persistence,
   and disk problems without the button's risks, and reuses the
   detection work the button needs anyway.
3. **Install button, but narrowed.** Button only where it can
   actually work: writable dashboard-owned environment, persistent
   (not Docker ephemeral), preflight passed. Everywhere else the page
   shows the environment-specific command (alternative 2) instead of
   a button that will fail. Keeps the zero-terminal win where it is
   real and stops claiming it where it is not.
4. **Auto-install on first grade.** Stays rejected (see
   Counter-arguments).

### What the draft must answer (checklist distilled from the lenses)

1. Exact fixed install sequence per platform class, including torch
   variant, and the rule that `jev-agent` itself is never touched;
   `laya` version policy.
2. Torch strategy, honest size/time/disk preflight per path.
3. Environment decision tree: detection, fallback order, PEP 668
   stance, service-account handling, error per terminal failure.
4. Definition of "installed"; checkpoint cache location per platform
   and deploy type; TMPDIR handling; partial-download resume.
5. Verification layers and which layer flips the panel;
   subprocess-based loadability check at minimum; what verification
   does not prove; repair path for verified-but-broken.
6. Docker story: persisted paths, image-upgrade behaviour, read-only /
   non-root failure message, or explicit ephemerality with UI copy.
7. Runner shape: shared scaffold vs deliberate third copy; lock scope
   (per-process vs cross-process); install/grade interaction;
   draining-state rules; restart-during-install behaviour; log/state
   location and truncation policy; shutdown contract and order.
8. Stop semantics per phase; retry as re-run vs resume;
   stop-during-torch test case.
9. Failure taxonomy table: condition → message → retry offered? →
   what must change first; log tail/cap/poll policy.
10. UI wiring: routes, CSRF, confirmation step, progress page/fragment,
    main-page banner during install, button lifecycle across
    running/failed/stopped states, grade/route form behaviour during
    install, Enabled × Backend × Install-job state matrix.
11. Broken-but-present repair path, or an explicit out-of-scope
    statement with the message those users see.
12. Post-install grading enablement decision and its disclosure copy;
    whether the install job writes config.
13. Security assumption: who may press Install on a network-reachable
    dashboard, loopback/token inheritance, rate limiting, logging.
14. Rollback section: what can be undone, what cannot, manual recovery
    per failure the button cannot fix.
15. Lifecycle scope: version display, update, uninstall, repair — or
    explicit first-install-only scope.
16. Measurable acceptance criteria and the platform × failure test
    matrix.
17. Value evidence: install friction vs opt-in/heavy/advisory as the
    adoption blocker; why CDA crosses the discover-don't-install
    boundary worker CLIs do not.

## Goals vs. Non-Goals

* **In-Scope Goals:**
  - Install button on the Code Grade status page, shown exactly when
    the backend is missing.
  - Server-side background install into the dashboard's own
    environment, one at a time, with progress and a log tail visible
    in the browser.
  - Verification after install via `CodeOrchestrator.available`; the
    status panel flips to installed without a restart if possible, or
    says a restart is needed if it is not.
  - Failure states in plain English with retry; a stopped install
    leaves a safe, retryable state.
  - Same endpoint on Mac, Windows, Linux, and VPS, with a stated
    answer for Docker persistence.
  - The fixed-command rule: no request can change what gets installed.
  - No change to grading semantics: no guessed grades while the
    backend is missing, threshold unchanged, off-by-default unchanged
    unless the design explicitly decides otherwise.
  - (Added by the Goldfish review.) Verification as loadability, not
    presence: the draft must prove the backend can load, not merely
    that a package is findable, and must name the repair path for a
    verified-but-broken install.
  - (Added.) Preflight before any download: disk, RAM, temp-dir
    space, PyPI reachability, each with a plain-English refusal.
  - (Added.) A durable install-job record and a defined
    restart-mid-install outcome; install/grade coordination with
    blocked states in both directions.
  - (Added.) The pip instruction retired consistently on all three
    surfaces (status panel, failed-grade status page, Learning
    empty state).
  - (Added.) An authorization statement: who may press Install, and
    what others see.
  - (Added.) A repair path for broken-but-present backends, or an
    explicit out-of-scope statement with the message those users see.
  - (Added.) Install telemetry beyond the log tail: attempt
    start/finish/failure with a failure class.
* **Out-of-Scope Non-Goals:**
  - Changing the model, the threshold default, or the grading itself.
  - Installing anything other than the code-orchestrator extra.
  - A general package manager for the dashboard.
  - Automatic updates of an installed backend.
  - (Added.) No install or warm-up outcome ever written to the grade
    ledger; no install outcome ever rendered as a grade outcome.
  - (Added.) No request-supplied package, flag, or index URL: the
    fixed thing is the server-side argv sequence, not a name.

## Key Decisions & Staging for Next Steps

Carried into `/egm-draft` as open decisions (the draft presents
options; the owner picks — nothing below is pre-decided):

1. Runner shape: a CDA-specific runner mirroring
   `BrowserInstallRunner`, or a shared install runner generalized
   from it. (Sharpened by the lenses: shared job scaffold vs
   deliberate third copy, with shutdown contract and order.)
2. What "installed" means: pip complete, or pip plus checkpoint
   pre-warmed (and if pre-warmed, by what mechanism). (Sharpened:
   three layers — packages, loadable backend, checkpoint — and the
   two-stage verification model that goes with them.)
3. Target environment: the dashboard's own Python, `--user`
   fallback, dedicated venv, and the Docker persistence story.
   (Sharpened: full decision tree with detection rule, fallback
   conditions, PEP 668 stance, and a platform-by-deployment
   persistence matrix.)
4. Post-install grading: auto-enable grading, or leave it off for the
   user to enable under Settings → Code orchestrator. (Sharpened:
   whether the install job writes config at all, and the disclosure
   copy either way.)
5. Install/grade interaction: shared lock or independent runners, and
   what the user sees when one blocks the other. (Sharpened:
   coordination above the runners, blocked states both directions,
   draining-grade rule, ledger-pollution guard.)
6. Confirmation and stop: confirm-first naming the exact command
   (browser-install precedent) or direct install; stop button or
   install-to-completion only. (Sharpened: stop mechanic per phase,
   checkable definition of "safe, retryable", retry as re-run vs
   resume.)
7. Restart: does the running dashboard pick up the new backend
   without a restart, and if not, what does the UI tell the user.
   (Sharpened: verification mechanism — cache invalidation vs
   fresh-subprocess probe — and the restart rule it implies, plus
   restart-mid-install semantics.)
8. (Added.) Verification layers: which layer flips the panel, and
   the repair path for a backend that passes presence but fails
   loadability.
9. (Added.) Failure taxonomy and preflight thresholds: the condition
   → message → retry table, and the disk/RAM/temp/PyPI checks.
10. (Added.) Durable job record, `interrupted` state, log location
    and cap, and the shutdown contract.
11. (Added.) Authorization and confirmation inheritance: loopback /
    token gating, CSRF, confirm-first, rate limiting, logging.
12. (Added.) Repair path for broken-but-present backends, or explicit
    out-of-scope with the message those users see.
13. (Added.) Progress home and state matrix: which page shows install
    progress, the Enabled × Backend × Install-job matrix, and
    second-click / tab-close / restart behaviour.
14. (Added.) Lifecycle scope: version display, update, uninstall,
    repair — or explicit first-install-only scope.
15. (Added.) Scope of the install itself: machine, venv, or repo; log
    location; behaviour across repos and model IDs.
16. (Added.) Value: is install friction the adoption blocker, and why
    does CDA cross the discover-don't-install boundary worker CLIs do
    not — for the human to decide.
