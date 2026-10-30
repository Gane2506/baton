# Structural brainstorm — CDA install button

Lens: structural. Mode: BRAINSTORM. Judge: only the concepts brief
(`~/workspace/baton-cda-install/concepts_brief.md`) and the listed code.
No production code was written or changed. Plain English, critique first.

## The shape as proposed

One button on the Code Grade status panel, shown exactly when the backend
is missing. Clicking it starts a server-side background install into the
dashboard's own environment, with progress and a log tail in the browser,
verification afterwards via `CodeOrchestrator.available`, plain-English
failure with retry, single-flight, and a fixed command no request can
change. The same endpoint is supposed to work on Mac, Windows, Linux,
and VPS.

The brief already names the hard parts honestly: what "installed" means,
Docker persistence, environment detection, install/grade contention, and
the fixed-command rule. This report is about what the shape still gets
wrong underneath those points: where the boundaries fall, what is
duplicated, what state machine the job actually needs, and what has no
home at all yet.

Existing shape, for reference:

- `BrowserInstallRunner` (`dashboard/browser_install.py`): in-memory
  state, one subprocess, log file under `.jev/browser-install.log`,
  states `idle | running | done | failed | stopped`, stop by
  terminating the process, log tail capped (last 64 KiB read, 256 KiB
  stored — the issue #200 fix).
- `CodeGradeRunner` (`dashboard/code_grade_runner.py`): in-memory state,
  one background thread, states
  `idle | running | done | failed | cancelled` plus a `draining` flag,
  cancel that discards the result but cannot stop the model call, a
  generation counter so a stale thread cannot write an outcome, results
  ledgered to `<repo>/.jev/cda-ratings.jsonl`.
- Both are owned by `DashboardCore` (`dashboard/core.py`), both are shut
  down from `shutdown_jobs()`, and each has its own status dataclass,
  its own polling surface, and its own template fragment.
- The Code Grade page (`templates/code_grade.html`) already carries two
  independent booleans in one panel — Enabled (config, toggled under
  Settings → Code orchestrator) and Backend available (computed per
  request by `_orchestrator(settings).available` in `routes_code.py`) —
  plus a grade-job flash driven by `job.state`.

---

## Findings

### 1. A third runner copies a pattern that is already duplicated twice

- **Severity:** Blocker
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The brief's open decision 1 — "a CDA-specific
  runner mirroring `BrowserInstallRunner`, or a shared install runner
  generalized from it" — understates the duplication. There are already
  two bespoke runners plus a third background-job shape
  (`ReverseRunner`, also owned by `DashboardCore`), each re-implementing
  the same skeleton: a lock, a thread, a state string, started/finished
  timestamps, a `busy()` check, a `status()` snapshot, and a shutdown
  hook. Each also carries a fix the others do not share: the browser
  runner has the log cap, the grade runner has the generation counter
  and the draining flag. A third copy means the next fix of that kind
  (log growth, stale-thread outcome, shutdown ordering) has to be
  ported three times, and the one that gets missed is the one that
  breaks in front of a user. In two years this is three runners drifting
  apart, each with its own state vocabulary (`stopped` vs `cancelled`,
  log tail vs result dict) and its own polling contract.
- **Maintainability Requirement:** The draft must not add a third copy
  of the skeleton without answering it. Either extract the genuinely
  common part — single-flight guard, thread lifecycle, status snapshot,
  shutdown registration — into one shared job scaffold that all three
  runners use, or state explicitly that the CDA runner is a deliberate
  third copy and name who ports future lifecycle fixes across all three.
  "Mirror the precedent" is not a structural decision; it is a decision
  to triple the maintenance surface.

### 2. The shared scaffold, if built, must not hide the one difference that matters

- **Severity:** Blocker
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The two existing runners have opposite
  termination semantics, and the CDA install sits on the browser side of
  that line, not the grade side. A pip subprocess can be terminated;
  a model `predict` cannot — grade cancel only discards the result and
  drains. A shared abstraction that offers one generic `stop()`/`cancel()`
  will either pretend pip can be "cancelled" the way a grade can (it
  cannot — killing pip mid-torch-download leaves a partial environment,
  which is a state, not just a stopped job), or pretend a grade can be
  killed (it cannot). The brief's decision 6 (stop button or
  install-to-completion only) is really a question about which
  termination contract the new job has, and a shared runner that papers
  over it will produce a stop button that lies about what it stopped.
- **Maintainability Requirement:** Keep execution and termination
  per-job even if the lifecycle scaffold is shared. The draft must name
  the install job's termination contract separately: what signal is sent
  to pip, what environment state a stop leaves behind, and why retry
  after a stop is safe — not just that a stop button exists.

### 3. All install state is in memory, so a dashboard restart mid-install has no defined outcome

- **Severity:** Blocker
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** Both existing runners keep state only in
  memory (`_state`, `_proc`, `_thread` under a lock). The browser
  runner's only durable artifact is the log file. That means a
  dashboard restart mid-install lands in an undefined state:
  the new process reports `idle`, the old log tail still sits in
  `.jev/`, and the fate of the old pip child is unspecified — the
  browser runner's `shutdown()` terminates the process and joins the
  thread for 30 seconds, but a crash or a restart that does not go
  through clean shutdown leaves an orphaned pip still writing to a log
  file that the next install truncates on start (`log.write_bytes(b"")`).
  Two pip processes can then run at once despite the in-memory
  single-flight guard, because the guard died with the old process.
  The brief's decision 7 asks about restart after a successful install;
  it never asks about restart *during* one, which is the case that
  corrupts state. The same gap applies to the grade runner, but a grade
  is recomputable; a half-installed Python environment is not.
- **Maintainability Requirement:** The draft must define the
  restart-mid-install state machine explicitly: a durable job record
  (separate from the log tail) holding command, target environment,
  started time, exit code, and verification outcome; an `unknown` /
  `interrupted` state on startup when a record says "running" but no
  process is alive; and a rule for what the next start does with a
  leftover child and a leftover log (never truncate a log another
  process may still be writing). If the draft chooses "no durable
  record," it must say so and accept that after any restart the page
  can only say "availability unknown, re-check and retry" — it cannot
  claim the previous install's outcome.

### 4. Single-flight is per-process, and the install/grade boundary is where it breaks

- **Severity:** Blocker
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** Each runner's lock guards only itself, in
  one process. The brief's decision 5 (shared lock or independent
  runners) is the load-bearing structural question, and the current
  shape cannot answer it from inside either runner. With independent
  locks, a grade can start while pip is replacing the very packages the
  grade is about to import — torch mid-reinstall under a running model
  load — and a pip install can start while a 2–3 GB torch process holds
  CPU, memory, and disk. With a shared lock, the two runners become
  coupled to each other, and neither runner's `busy()` answer is
  complete on its own: the Code Grade page would need to report "grade
  blocked by install" and "install blocked by grade" as distinct states,
  which neither status dataclass can express today. The page template
  already shows only one job flash (`job.state` from the grade runner);
  bolting a second job onto the same flash slot collapses the two
  blockages into one message.
- **Maintainability Requirement:** Put the coordination above the
  runners, not inside them — a single job-coordination point owned by
  `DashboardCore` (or whatever owns both runners) that can say which
  job holds the lock and why the other is waiting. The draft must
  define the lock scope (per-process only, with the multi-worker /
  restart caveat stated, not implied), the blocked states for both
  directions, and what each page shows while blocked. A lock that lives
  inside the new runner alone cannot coordinate with the grade runner
  at all.

### 5. "Installed" is three layers, and `available` only checks two of them

- **Severity:** Blocker
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The brief flags this, but the structure
  still treats verification as one boolean flip. In the code there are
  three distinct layers: (a) pip packages present (`laya` + torch
  importable), (b) backend loadable (`load_backend` in
  `code_orchestrator/backend.py` finds `laya` via `find_spec` without
  importing torch, and constructs `LayaBackend`), and (c) the ~842 MB
  checkpoint downloaded and loadable on first grade. `available`
  deliberately checks only (a)/(b) and never downloads weights — its
  docstring says so. So "verification after install via
  `CodeOrchestrator.available`" proves pip worked; it says nothing
  about (c). If "installed" means pip plus checkpoint pre-warmed, the
  verification step is not a cheap check but a second heavy background
  job (model load, ~50 s plus download, with the documented TMPDIR and
  Hugging Face cache requirements) that looks like a grade job — it
  loads the same model — but must never be ledgered as a grade, must
  never surface as a grade result, and must never be confused with a
  failed grade in the rating ledger. If "installed" means pip only, the
  first grade after a "successful" install is still a multi-minute
  download with no warning, which is the exact surprise the brief says
  the button must avoid.
- **Maintainability Requirement:** Split verification into two named
  stages with different costs and different homes: a cheap
  package/backend check (the existing `available`) and, only if the
  draft chooses pre-warming, a warm-up job with its own state, its own
  progress, and an explicit rule that it writes nothing to the grade
  ledger. The status panel must be able to show "pip done, checkpoint
  not yet downloaded" as a real state, not as "installed."

### 6. The fixed-command precedent does not fit this install

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The browser precedent fixes one argv
  (`servers.browser.install_argv()`, resolved through
  `resolve_executable`, confirmed on a page naming the exact command).
  The CDA install is not one argv. The docs show the CPU-only path as
  two steps — install the CPU torch wheel first, then `laya` — because
  the default torch wheel drags ~2.5 GB of CUDA libraries onto a
  CPU-only machine. A single fixed `pip install
  "jev-agent[code-orchestrator]"` on a CPU machine therefore installs
  the wrong weight of dependency by default, and a fixed command that
  branches on platform/CPU/GPU is no longer a single command but a
  server-side install policy. There is a second wrinkle: the extra form
  names Baton's own distribution. When the dashboard runs from an
  editable checkout, `pip install "jev-agent[code-orchestrator]"` risks
  installing a second, PyPI copy of the package the running process came
  from, while `pip install laya` (the docs' equivalent form) does not
  touch Baton itself. The brief says "fixed command" without saying
  which of these two commands is fixed, and they are not interchangeable.
- **Maintainability Requirement:** Define the install as a fixed,
  server-side *sequence* chosen by server-detected environment facts
  (CPU vs GPU, platform), never by any request field, and pin down
  whether the command installs the `laya` package or Baton's own extra.
  The confirmation surface must name the whole sequence and its total
  size/time, not just one command line, or the fixed-command guarantee
  is true of a command the user never actually runs.

### 7. "The dashboard's own environment" is not one environment

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The brief lists venv, system Python, and
  service accounts, but the structure still assumes one default
  (`sys.executable -m pip`) with a fallback. Those are not fallbacks of
  one thing; they are different durability and visibility contracts:
  a venv install persists across dashboard restarts but not across
  venv rebuilds; a `--user` install is visible to a different
  interpreter's `sys.path` only sometimes; a system-Python install may
  be read-only, or may require privileges the dashboard user does not
  have, or may be the wrong target entirely when the dashboard runs
  under `pipx`, `uv`, or an editable install. Docker is the extreme
  case — an install into a container layer vanishes on restart — but
  the same shape of problem exists wherever the dashboard's interpreter
  and the install target can disagree. Worse, the dashboard process
  itself has no reliable way to know which case it is in: nothing in the
  listed code detects "running inside Docker" or "this site-packages is
  ephemeral," so the endpoint cannot honestly label an install
  "persistent" vs "repeat after restart" without a new detection
  mechanism the brief does not name.
- **Maintainability Requirement:** The draft must separate three
  decisions the brief currently merges: (1) how the target environment
  is detected (interpreter, writability, `--user` visibility), (2) how
  durability is determined and surfaced per deployment (a capability
  flag the UI can show, not a hope), and (3) what the plain-English
  error is when no writable, visible target exists. "Same endpoint on
  every platform" cannot mean "same durability promise on every
  platform" — the draft must say which platforms promise what.

### 8. Verification inside a running process is unreliable by construction

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The brief's goal says the panel "flips to
  installed without a restart if possible." The code makes "if possible"
  do heavy lifting. `available` is computed fresh per request by
  constructing a new `CodeOrchestrator` in `routes_code.py`, and
  `load_backend` uses `importlib.util.find_spec("laya")` rather than an
  import precisely so the check stays cheap — but `find_spec` results
  are cached by the import system, so a package pip just installed into
  the running interpreter's site-packages may not be visible until
  caches are invalidated, and a `--user` or venv-mismatched install may
  never be visible to this interpreter at all. Conversely, a check that
  passes after install does not prove the *next* dashboard process
  (after the restart the user was told they might not need) will see
  the same packages. The "flip without restart" goal therefore couples
  the UI to import-system cache behaviour, which is the wrong layer to
  depend on for a user-facing state transition.
- **Maintainability Requirement:** The draft must state the
  verification mechanism, not just the verification call: cache
  invalidation, a fresh-subprocess probe (which sees what a restarted
  dashboard would see), or an honest "restart required" outcome.
  A subprocess probe is the structurally honest option — it tests the
  environment a new process will inhabit — but it is a third job shape
  (short-lived probe vs long install vs heavy warm-up), and the draft
  should name it as such rather than letting `available` silently do
  double duty as both a cheap status check and a post-install proof.

### 9. The status panel cannot hold two jobs and three booleans in its current shape

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The template today renders Enabled,
  Backend, and one grade-job flash. The install adds a second job
  (with its own progress, log tail, and outcome) and a third boolean
  family (install running / done / failed / stopped, and the
  pip-vs-checkpoint distinction from finding 5). There is no home for
  install progress in the current structure. Reusing
  `/code-grade/status` conflates the two jobs: that page's live
  fragment expects a `CodeGradeStatus` (kind, task, threshold, result,
  draining), and an install has none of those — it has a command, an
  exit code, and a log tail. Keeping install progress only on
  `/code-grade` itself loses it the moment the user navigates away,
  which breaks the precedent both existing jobs set (the result waits
  on a status page; closing the tab loses nothing). The brief says
  "progress and a log tail visible in the browser" without saying on
  which page, and that omission is structural: the page topology is
  part of the design, not a template detail.
- **Maintainability Requirement:** Give the install job its own status
  surface (its own page/route or a clearly separated section with its
  own polling fragment), and write out the full state matrix the
  panel must render: Enabled × Backend × Install-job-state, including
  the blocked states from finding 4. If the draft reuses the grade
  status page, it must define a status envelope both jobs can share
  without forcing an install to fake a grade's fields.

### 10. A log tail is not progress for this install

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The browser precedent shows the last 30
  lines of a log. That is adequate for a short `npx` install. A CDA
  install is gigabytes across two phases (pip packages, then possibly
  a checkpoint download) with different progress signals in each, and
  pip's own output is verbose, non-monotonic, and gives no time
  remaining. The brief's "progress" goal, read against the precedent,
  risks collapsing into "show the log tail and call it progress" —
  which leaves the user watching download lines with no sense of
  whether 10% or 90% is done, and no preflight warning when disk space
  is insufficient. The docs already record a failure mode of exactly
  this kind: the default `/tmp` tmpfs (512 MB) is smaller than the
  model, so the first load fails with "No space left on device" unless
  TMPDIR points at the home disk. An install job that starts without
  checking disk, TMPDIR, and cache location will discover that failure
  after gigabytes of download, not before.
- **Maintainability Requirement:** Structure the job as named phases
  (preflight → pip → verify → optionally warm-up), each with its own
  progress signal and its own failure message, and put the disk/TMPDIR/
  cache preflight checks in the preflight phase, before any download
  starts. Inherit the issue #200 log discipline (bounded tail reads,
  capped stored log) explicitly — a new runner that re-implements log
  handling without the cap reintroduces the unbounded-growth bug the
  codebase already fixed once.

### 11. The install is a server self-modification, not another grade POST

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** A grade POST runs a model and returns a
  result; an install POST rewrites the Python environment of the server
  that is currently running, as the server's own user, with network
  access — and can break that server (a pip resolution that upgrades a
  shared dependency, a torch install that exhausts disk, a partial
  environment left by a stop). The browser-install precedent treats its
  install as a Tier-2 action with a confirmation page naming the exact
  command, and the CDA install deserves at least that, but the brief's
  decision 6 leaves confirmation open while the structural asymmetry
  is already settled: the two actions are not peers. Putting the
  install routes into `routes_code.py` alongside grade/route handlers,
  with the same CSRF-checked form pattern, hides that asymmetry in the
  code layout — a future reader sees two similar POSTs and assumes
  similar blast radius. The size/time/durability disclosure matters
  here too: confirming "run this command" without "this downloads
  gigabytes and may not survive a container restart" is confirmation
  theatre, not consent.
- **Maintainability Requirement:** Keep the install endpoint and its
  confirmation structurally distinct from grade/route handlers (its own
  route group or module, its own confirmation surface), and require
  that confirmation to disclose command sequence, approximate size and
  time, target environment, and durability — not just the command
  string. The draft should also state whether any dashboard user who
  can grade can also install, or whether install is a more privileged
  action; silence on that point defaults to "anyone who can reach the
  page can rewrite the server's environment."

### 12. `DashboardCore` is already the owner of everything, and this adds a third shutdown line

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** `DashboardCore.__init__` constructs every
  runner and flow, and `shutdown_jobs()` lists them in order
  (scheduler, browser installer, reverse runner, grade runner, device
  flows, launcher, chat, scheduler again). A CDA runner becomes one
  more attribute, one more constructor argument, one more shutdown
  call — and shutdown order now matters in a new way: if the grade
  runner is shut down (joining its thread) while an install holds the
  shared lock from finding 4, or vice versa, shutdown can stall or
  leave the install half-done. The brief never mentions shutdown
  ordering, lifespan handling, or what `shutdown_jobs()` does with a
  running pip install (terminate it like the browser installer, or let
  it finish?). Every runner added to this core makes the core harder to
  reason about, and the install runner is the first one whose shutdown
  decision destroys user-visible state (a killed pip) rather than just
  discarding a result.
- **Maintainability Requirement:** Name the new runner's shutdown
  contract in the draft (terminate pip on shutdown, or detach and
  record it as interrupted per finding 3), its position in the
  shutdown order relative to the grade runner, and whether the core
  keeps growing by one attribute per job or gains a job registry that
  shutdown iterates. Adding the eighth ad-hoc field to the core
  without that answer is how the core becomes unmaintainable.

### 13. Install and enable are two trips to two pages, and the draft leaves the seam open

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** Grading is off by default, and the toggle
  lives under Settings → Code orchestrator, while the install button
  lives on the Code Grade page. The brief's decision 4 (auto-enable
  after install, or leave it off) is usually read as a product choice,
  but structurally it is a state-coupling choice. If install does not
  enable, the user completes a multi-minute install and lands back on a
  page that still says "Grading is off" — a success state that looks
  like nothing changed, and a second navigation to finish the job. If
  install does auto-enable, the install job gains write access to repo
  config, coupling a package-install job to configuration management —
  a boundary the current runners never cross (neither the browser
  installer nor the grade runner writes config). Either way the draft
  must pick a coupling and own it; leaving it as "the owner picks"
  without naming the coupling hides the cost of each option.
- **Maintainability Requirement:** The draft must state whether the
  install job writes config. If it does not, the post-install UI must
  close the loop in one place (an explicit "installed — enable grading
  in Settings" step on the same page, not a silent return to the status
  panel). If it does, the install runner's contract expands to config
  writes, and that expansion must be named, not smuggled in as a
  post-install nicety.

### 14. A stopped install's "safe, retryable state" is asserted, not structured

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The brief's goal says "a stopped install
  leaves a safe, retryable state," and its critique notes pip is
  idempotent per package. Idempotent per package is not the same as a
  safe environment: pip interrupted mid-transaction can leave a
  partially installed torch, a half-written `laya`, or metadata that
  reports a package present while its files are incomplete — and
  `find_spec` will happily report such a package as present, so the
  cheap `available` check can then flip the panel to "installed" for a
  broken backend. The first grade after that fails as an inference
  error (`CodeOrchestratorError`), not as "unavailable," which the
  error taxonomy treats as fail-closed at the gate — a worse outcome
  than a clean "not installed." Retry being safe is therefore not a
  property of pip alone; it needs a verification step strong enough to
  distinguish "package present" from "package usable" (at minimum, an
  import probe in a fresh subprocess, not just `find_spec`).
- **Maintainability Requirement:** Define "safe" as a checkable state:
  after a stop or failure, the job records what phase it stopped in,
  and the next verification probes usability (import, not just spec
  presence) before the panel claims installed. If the draft cannot
  define that probe, it must downgrade the goal from "safe, retryable"
  to "retryable, but a stopped install must be re-verified before any
  grade is trusted."

### 15. The ledger and the install must not touch each other — the draft should say so

- **Severity:** Nit
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The grade runner ledgers every finished
  grade and every unavailable backend (`status="unavailable"`, no
  quality fields). An install job that reuses grade-runner machinery —
  or a warm-up that runs a model load — risks writing ledger records
  for events that are not grades: an install failure is not an
  unavailable grade, and a warm-up load is not a graded diff. The
  ledger's aggregates count unavailable records in `n`; polluting them
  with install events would quietly shift the calibration data the
  threshold decision (Phase 4, 50+ real grades) depends on. The brief
  never mentions the ledger, which is exactly how a shared-runner
  refactor introduces this coupling by accident.
- **Maintainability Requirement:** State the negative rule in the
  draft: the install job and any warm-up write nothing to
  `cda-ratings.jsonl`, and no install outcome is ever rendered as a
  grade outcome. The boundary between "job that grades" and "job that
  prepares grading" must be explicit in the code layout, not just in
  the operator's head.

### 16. State vocabulary drift is a small debt that compounds

- **Severity:** Nit
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The browser runner says `stopped`, the
  grade runner says `cancelled` plus `draining`, and the new job needs
  at least `verifying` and `interrupted` (findings 3 and 5) on top of
  the shared set. If each runner keeps inventing its own vocabulary,
  every template, polling fragment, and test that switches on state
  strings diverges a little further, and the next job inherits three
  dialects. This is a nit on its own; it becomes a blocker the moment
  finding 9's shared status envelope is attempted without it.
- **Maintainability Requirement:** The draft should define one state
  vocabulary for dashboard background jobs (or explicitly reject a
  shared vocabulary and accept per-job strings). Either answer is
  maintainable; drifting into a third dialect by accident is not.

---

## What is missing from the structure (checklist for the draft)

1. A durable install-job record and the `interrupted` state for
   restart mid-install (finding 3).
2. The coordination point and blocked states for install vs grade
   (finding 4).
3. The two-stage verification model — cheap check vs warm-up job —
   and the ledger exclusion for warm-up (findings 5, 15).
4. The fixed install *sequence* (CPU torch step, `laya` vs Baton's
   extra) and its size/time/durability disclosure (findings 6, 11).
5. Environment detection and a per-deployment durability flag the UI
   can show (finding 7).
6. The post-install verification mechanism — cache invalidation or
   fresh-subprocess probe — and the restart rule it implies
   (finding 8).
7. A home for install progress that survives leaving the page, and the
   full Enabled × Backend × Install-job state matrix (finding 9).
8. Named install phases with preflight disk/TMPDIR/cache checks and
   the inherited log-cap discipline (finding 10).
9. The shutdown contract and shutdown order relative to the grade
   runner (finding 12).
10. Whether the install job writes config (auto-enable) or the UI
    closes the loop to Settings (finding 13).
11. A usability probe after stop/failure, so "retryable" means
    "verified usable," not "pip can run again" (finding 14).

## Bottom line

The concept's centre — one button, server-side, single-flight, fixed
command, no guessed grades — is coherent as a user story. As a
structure it is not yet coherent: it adds a third in-memory-only runner
to a codebase that already has two, gives it a second job to coordinate
with and no place to coordinate from, defines "installed" with a check
that cannot see the checkpoint, and promises persistence on platforms
where the dashboard cannot tell whether an install persists. The draft
needs to resolve the runner question (shared scaffold vs deliberate
third copy), the durable job record, the install/grade lock, and the
two-stage verification model before it resolves button copy or
confirmation wording. Those four decisions are the structure; the rest
follows from them.
