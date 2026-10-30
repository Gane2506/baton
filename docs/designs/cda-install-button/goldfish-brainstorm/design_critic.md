# Design Critic — BRAINSTORM: CDA install button on the Code Grade page

- **Role:** Skeptical Architect Brainstormer (design criticism lens)
- **Concept under review:** Replace the `pip install "jev-agent[code-orchestrator]"`
  instruction on `/code-grade` with an Install button that runs the install
  server-side, in the background, into the dashboard's own environment, with
  progress and verification in the browser — the same button on Mac, Windows,
  Linux, and VPS. Single-flight, fixed command, plain-English failures with
  retry, never a guessed grade while the backend is missing.
- **Brief reviewed:** `~/workspace/baton-cda-install/concepts_brief.md`
  (tracking issue Gane2506/baton#366)
- **Code read (read-only):** `code_grade.html`, `routes_code.py`,
  `browser_install.py`, `code_grade_runner.py`, `code_orchestrator/backend.py`,
  `docs/code-orchestrator.md`, plus `pyproject.toml` (the extra's definition),
  `routes_connections.py` (the browser-install route), and `dashboard/security.py`
  (loopback/token gating) for grounding only.
- **Scope note:** This report judges the concept and the brief, not the
  finished design. It does not validate the concept. Where the brief already
  names a risk as open, this report says whether naming it is enough — in
  several cases it is not, because the open question decides whether the
  concept works at all.

## Short verdict

The concept is aimed at the wrong end of the problem. The pip command is not
the hard part of installing this backend; the hard parts are picking the
right torch variant, having enough disk and RAM, surviving a container
restart, and proving the installed backend actually loads. A button that runs
one fixed pip command answers none of those, and on the platforms the
concept claims to cover (VPS, Docker, Windows service) it either installs
the wrong thing, installs something that vanishes, or reports success for a
backend that still cannot grade. The button helps most exactly where help is
least needed — a Mac venv, where the pip command already works — and helps
least where the terminal is actually painful.

That does not make the concept worthless. It makes the button, as specified
(one fixed command, same everywhere), the wrong shape. The draft needs to
either narrow the claim (button works where the dashboard's own environment
is writable and persistent, and says so elsewhere) or change the mechanism
(environment-aware install, not one fixed command). Shipping the button with
the current claim intact would be worse than the pip command, because a
failed button looks like a product failure while a pasted command looks like
the user's environment — and a half-installed backend is worse than a missing
one (see Finding 1).

The strongest counter-argument in one paragraph: this is a one-time setup
step for a feature that is off by default, whose measured value is a
human-review routing signal (merge-action confidence 0.36–0.52 on 13 real
diffs; nothing cleared the 0.7 threshold; the auto-accept and auto-reject
paths are effectively dead). The proposal adds a new privileged server-side
code-execution path — a pip install into the running application's own
environment, plus a runner, a lock, a log, a stop path, an error taxonomy,
and a verification step — to save one copy-paste for a feature most users
will never turn on. That cost/benefit ratio is the thing the draft has to
justify, not assume.

## Findings

### 1. A half-installed backend is worse than a missing one — verification by `available` is too shallow

- **Severity:** Blocker
- **Dimension:** Correctness / failure semantics
- **Risk description:** Today a missing backend is safe by construction:
  `CodeOrchestratorUnavailable` records "unavailable" at the quality gate and
  the run proceeds, and D4 proceeds without a hint. An inference failure is
  handled the opposite way: `CodeOrchestratorError` blocks the gate
  fail-closed. The availability check the concept plans to verify with,
  `CodeOrchestrator.available`, is shallow by design — for the `laya`
  branch it only runs `importlib.util.find_spec("laya")`, deliberately
  without importing anything, because importing `laya` pulls torch. So a
  pip run that installed `laya`'s metadata but left torch broken, partial,
  or unloadable (killed mid-download, disk full at 90%, wrong wheel for the
  platform) passes verification: `available` flips to true, and the first
  real grade then fails as an inference error instead of an unavailable
  backend. The user has gone from "grading unavailable, run proceeds" to
  "gate blocks on a grade that cannot be trusted" — a strictly worse state
  produced by the install feature itself. The same applies to a stale
  negative cache: after pip creates new site-packages entries, a running
  process's import system may need `importlib.invalidate_caches()` before
  `find_spec` sees them, so verification can also report a false negative
  after a genuinely successful install. The brief says "verification via
  `CodeOrchestrator.available`" as if that settled the question; it does
  not, in either direction.
- **Mitigation requirement:** The draft must define verification as
  loadability, not presence — for example, a subprocess import/smoke check
  (`sys.executable -c "import laya"` or equivalent, isolated from the
  dashboard process so a torch import crash cannot take the dashboard
  down) — or must explicitly accept and document that a verified-but-broken
  install converts unavailable into gate-blocking errors, and provide a
  repair path (reinstall, and a way back to a clean "not installed" state).
  "The status panel flips to installed" is not a verification story.

### 2. The fixed command installs the wrong torch on CPU-only machines

- **Severity:** Blocker
- **Dimension:** Cost / resource economics, correctness of the fixed command
- **Risk description:** The extra is declared as `laya>=0.4.15` with no
  upper bound, and `laya` pulls torch + transformers. The docs state the
  consequence plainly: the default torch wheel includes CUDA libraries
  (~2.5 GB), and on a CPU-only machine the documented install is the CPU
  wheel first, then `laya` — a two-step install, not one command. The
  concept's fixed command (`pip install "jev-agent[code-orchestrator]"`)
  therefore does the wrong thing on the majority of dashboard hosts (Mac,
  small VPS, CI-like Linux boxes): it downloads ~2.5 GB of CUDA libraries
  the machine will never use, takes far longer than the "~1 GB" the brief
  quotes, and risks filling a small VPS disk. The brief quotes "~1 GB"
  (pip plus ~842 MB checkpoint) and never mentions the torch variant at
  all. A fixed command is a security requirement, but a fixed *wrong*
  command is not a design — and "fixed" plus "environment-aware" are in
  tension the draft has to resolve explicitly (a fixed *sequence* chosen
  server-side from detected platform, not a fixed string, and not a
  client-chosen variant).
- **Mitigation requirement:** The draft must state the exact install
  sequence per platform class (CPU wheel first on CPU-only hosts, default
  wheel where CUDA is present, or a justified single-command alternative),
  how the server detects which class it is in without trusting the
  browser, and the true download size per class. If the answer is "always
  the default wheel," the draft must say the disk and time cost out loud
  and set a preflight disk check accordingly (see Finding 5).

### 3. "Same button on every platform" is a UI claim, not an install claim

- **Severity:** Blocker
- **Dimension:** Platform coverage / persistence
- **Risk description:** The brief itself names Docker persistence as the
  largest open risk, and it is right — but naming it does not shrink it.
  The Linux deploy runs Baton from a pinned Docker container, so a pip
  install inside that container vanishes on the next restart; on a VPS the
  dashboard may run as a service account with a read-only system Python;
  on Windows it may run as a service with its own site-packages rules,
  file locking, and PATH. The button is the same everywhere; what it does
  is not. Worse, the browser-install precedent the concept leans on
  resolves its executable from PATH (`resolve_executable`), which is the
  wrong resolution for this feature: PATH pip and the dashboard's own
  interpreter pip are different environments, and installing into PATH pip
  while verifying against the dashboard interpreter guarantees a
  "successful" install that verification then reports as still missing.
  The draft must use `sys.executable -m pip`, never PATH pip — and must
  say what happens when `sys.executable`'s environment is not writable
  (system Python, read-only container layer, service account) instead of
  silently falling back to an environment the dashboard cannot see.
- **Mitigation requirement:** The draft must give a per-platform answer,
  not a per-button answer: Mac venv, system Python, Windows service,
  Linux service, Docker container — for each, the target environment, the
  writable check, the fallback (if any) that the dashboard interpreter can
  actually observe, and the persistence story. For Docker, one of: a
  volume for site-packages (with its own breakage modes), a documented
  rebuild step, or an explicit "ephemeral, must be repeated" statement in
  the UI itself — not buried in docs. Until that table exists, the
  "works on a VPS" claim must not appear in the UI or the design.

### 4. Install success does not mean the first grade works (TMPDIR, cache, RAM)

- **Severity:** Major
- **Dimension:** Resource economics / hidden prerequisites
- **Risk description:** The install is only step one of three. Step two is
  the ~842 MB checkpoint download into the Hugging Face cache on first
  grade, under the dashboard process's environment (`HF_HOME` as the
  dashboard sees it, which may differ from the user's shell). Step three
  is the first load, which reconstructs the checkpoint through the temp
  dir — and the docs record a real failure here: the default /tmp is a
  512 MB tmpfs on the dev VM, smaller than the model, so the first load
  fails with "No space left on device" unless TMPDIR points at the home
  disk. A button that reports "installed" after pip alone leaves the user
  facing a multi-minute first grade that can fail for disk, temp-dir, or
  memory reasons (2–3 GB RSS; the docs say plainly "if that does not fit,
  keep the feature off"). If the button pre-warms the checkpoint instead,
  that pre-warm is itself a multi-minute, multi-GB job that needs TMPDIR,
  cache location, and RAM answers — and if it is implemented by running a
  grade through the existing runner, the synthetic grade must not pollute
  the rating ledger (`<repo>/.jev/cda-ratings.jsonl`), which records every
  finished dashboard grade. The brief lists "what installed means" as an
  open decision; the point this report adds is that pip-only success is
  actively misleading, not merely incomplete, because the failure it hides
  arrives later, inside a grade, where it looks like a grading failure.
- **Mitigation requirement:** The draft must pick a definition of
  "installed" that includes the first grade succeeding, or must label
  pip-only success honestly in the UI ("packages installed — the model
  checkpoint downloads on first grade, which takes several minutes and
  needs ~3 GB free memory and a temp dir larger than the model"). Either
  way it must state TMPDIR handling, HF cache location under the
  dashboard's environment, and a preflight check (disk, temp-dir space,
  RAM) with a plain-English refusal before the download starts, not after
  it fails.

### 5. No preflight is specified — the button invites disk-full and OOM on small VPS hosts

- **Severity:** Major
- **Dimension:** Cost / quota / resource economics
- **Risk description:** The numbers, added up from the docs: default torch
  ~2.5 GB (or the CPU wheel, smaller but unstated), checkpoint ~842 MB,
  2–3 GB RSS at load/grade time, plus pip's own working space. The concept
  specifies no check of free disk, free RAM, or PyPI reachability before
  starting a multi-GB download into the dashboard's own environment. On a
  small VPS — the exact audience the "works on a VPS" claim targets — a
  pip run that fills the disk mid-torch-install does not just fail the
  install; it can break the dashboard host itself (log writes, state
  database, other services). Pip interrupted mid-install also leaves a
  partial environment whose retry story the brief asserts ("pip is
  idempotent per package") without evidence: re-running pip usually
  repairs, but a killed install can leave metadata without files, and the
  draft needs a verification-after-retry step (see Finding 1) rather than
  an assumption. Peak load here is not requests per second — this is a
  single-user dashboard, QPS is ~1 and irrelevant — it is gigabytes and
  minutes per click, which is exactly the dimension the brief does not
  quantify.
- **Mitigation requirement:** Mandatory preflight before the install
  starts: free disk (with a stated threshold covering the torch variant
  actually being installed plus the checkpoint), available RAM, writable
  target environment, and PyPI reachability — each failure mapped to a
  plain-English refusal, not a started-then-failed download. The draft
  must also state the stop-and-retry contract precisely: what state a
  stopped install leaves, how retry detects and repairs it, and what the
  user sees if retry also fails.

### 6. The install is environment-scoped but the dashboard's jobs are repo-scoped

- **Severity:** Major
- **Dimension:** Architecture / state management
- **Risk description:** The grade runner and the browser-install runner
  are owned by the dashboard process, but the browser-install log lives
  under a repo's `.jev/` directory (`browser-install.log`), and the Code
  Grade page itself is rendered per repo (`views.open_repo`). A CDA
  install is not a property of a repo — it is a property of the
  dashboard's Python environment, shared across every repo the dashboard
  serves. Tying its log, status, or single-flight lock to one repo's
  `.jev/` directory means the status can disappear or disagree when the
  user switches repos, and two repos' pages could show different install
  states for the same environment. The brief says "one at a time" without
  saying one at a time per what scope. The in-memory lock also only holds
  within one dashboard process: the dashboard's own history includes a
  stale-process port race, so two dashboard processes (old one not yet
  dead, new one started) defeat an in-memory single-flight guarantee, and
  a dashboard restart mid-install orphans the pip child — a `Popen`
  child is not automatically killed when its parent dies — which can keep
  writing to a log the new process has truncated for a fresh install.
  The browser-install precedent handles shutdown by stopping the install;
  the grade runner handles it by waiting up to 30 s. The CDA install
  needs its own stated answer, and "daemon thread" is not one.
- **Mitigation requirement:** The draft must place install state
  (lock, log, status) at dashboard-process scope, not repo scope, with a
  stated log location and cap (the browser-install precedent caps at
  256 KiB and reads only the last 64 KiB per poll — the brief's "log
  tail" needs the same cap, or poll cost grows with install history);
  must state shutdown/restart semantics (stop, wait, or orphan handling,
  including what happens to a pip child if the dashboard dies); and must
  state whether the lock is per-process only and what the user sees if a
  second dashboard process exists.

### 7. Install/grade contention is not just a lock question

- **Severity:** Major
- **Dimension:** Architecture / resource contention
- **Risk description:** The brief frames install/grade interaction as
  "shared lock or independent runners." That understates it. A grade holds
  2–3 GB RSS for minutes and cannot be interrupted mid-inference (cancel
  only discards the result; the runner drains). An install downloads
  gigabytes and writes heavily. Running them concurrently contends for
  CPU, memory, disk I/O, and disk space simultaneously — on a small host,
  an install running during a grade can OOM the grade, and a grade
  running during an install can make the install fail for disk space the
  preflight just approved. A shared lock serialises them, which is the
  safe default, but then the user-visible question is queueing: does an
  install started while a grade runs wait, refuse, or queue — and for how
  long, given a grade can take ~10 minutes for a large diff? The same
  applies to the browser install: it is a separate runner with its own
  lock, so a browser install and a CDA install can currently run
  concurrently by construction. "One install at a time" is true per
  runner and false for the dashboard.
- **Mitigation requirement:** The draft must state one dashboard-wide
  contention policy (shared lock across install and grade runners, or a
  justified exception), what the user sees when an install is blocked by
  a running grade (and vice versa), and whether blocked work queues or
  refuses. Independent runners with no cross-talk are not an acceptable
  answer on a 2 GB host.

### 8. The security story is borrowed, not stated

- **Severity:** Major
- **Dimension:** Security / privileged operation
- **Risk description:** The fixed-command rule is necessary and the brief
  states it, but it is not sufficient. A server-side pip install into the
  running application's environment is remote code execution for anyone
  who can reach the endpoint — the fixed command only limits *which*
  code, not *who* can trigger it. The codebase already has the relevant
  mitigations, but the brief does not cite them: the dashboard binds
  loopback by default and requires a token for a non-loopback host
  (`app.py`, `security.py`), and the browser-install route uses
  `checked_form` (CSRF) plus a confirm-first page naming the exact
  command. The CDA install endpoint must inherit all three — loopback /
  token gating, CSRF-checked form, and confirm-first naming the exact
  command — and the draft must say so explicitly rather than assuming the
  route wiring carries it over. Two further gaps: the extra is unpinned
  above (`laya>=0.4.15`), so "the fixed command" installs whatever laya
  and torch versions are current at click time, not the versions the
  feature was tested against — a reproducibility and supply-chain gap
  for a one-click install; and re-running the install after a failure
  can silently upgrade an already-installed laya/torch, which contradicts
  the non-goal "no automatic updates of an installed backend" unless the
  draft pins versions or checks installed versions first.
- **Mitigation requirement:** The draft must state the endpoint's auth
  story (loopback/token inheritance, CSRF, confirm-first), the version
  policy (pinned versions, or installed-version check before any
  upgrade), and what happens on reinstall when a backend is already
  present (no-op, repair, or upgrade — chosen explicitly, not by pip's
  default behaviour).

### 9. Progress cannot be a percentage bar — pip output is not progress

- **Severity:** Nit (but it becomes a Major if the UI promises otherwise)
- **Dimension:** UX honesty
- **Risk description:** The concept promises "progress in the browser."
  Pip's output has per-file download lines but no overall percentage, and
  the checkpoint download (if pre-warmed) is a separate phase with its
  own output. The honest UI is the browser-install precedent: a streaming
  log tail plus a state (running/done/failed/stopped) and elapsed time.
  Anything shaped like a progress bar would be invented numbers. The
  grade status page already does this honestly with a live region and
  elapsed time; the install page should reuse that pattern, not invent a
  percentage.
- **Mitigation requirement:** The draft must specify progress as log
  tail + state + elapsed time, with no percentage unless a real
  denominator exists for the phase being shown.

### 10. The journey does not end at "installed"

- **Severity:** Major
- **Dimension:** UX completeness / under-specification
- **Risk description:** The status panel shows two independent facts:
  Backend (available vs not installed) and Enabled (yes/no). Grading is
  off by default and the toggle lives under Settings → Code orchestrator.
  The concept's success state — "the status panel flips to installed" —
  still leaves a user who cannot grade from the quality gate and who may
  reasonably believe the install finished the job. The brief lists
  post-install grading (auto-enable vs leave off) as open decision 4, but
  the concept statement itself ("with progress and verification in the
  browser") reads as a complete journey, and it is not: install → still
  off → Settings → enable → grade is a four-step journey with a
  context switch in the middle. Relatedly, the Grade diff and Route task
  forms on the same page remain submittable while the backend is missing
  (today they fail gracefully with install guidance), and the brief does
  not say whether the Install button replaces, disables, or sits
  alongside those forms, nor what a grade submitted mid-install does.
  A button that appears "exactly when the backend is missing" also needs
  a defined disappearance: after a failed install, does the button stay
  (with retry), and after a stopped install, does the page say the
  environment may be partial?
- **Mitigation requirement:** The draft must decide auto-enable vs
  leave-off explicitly (auto-enable changes the off-by-default promise
  and must be called out as such, not drifted into), must state the
  button's lifecycle across running/failed/stopped states, and must
  state the grade/route forms' behaviour while an install is running or
  has failed.

## What is over-engineered

- **A second bespoke runner.** Mirroring `BrowserInstallRunner` as a
  CDA-specific runner duplicates a privileged job type (spawn, watch,
  stop, log cap, status) for a one-time operation. If the shape is
  genuinely shared, generalise one install runner; if it is not,
  duplicating it doubles the surface that must be audited for the
  fixed-command rule, orphan handling, and log growth. The brief lists
  this as open decision 1; the criticism is that either option needs
  justification against the operation's frequency (once per environment,
  ever) — a dedicated runner is a lot of machinery for an operation that
  runs once.
- **Stop, if it cannot be honest.** A stop button that kills pip
  mid-torch-download sounds like control, but the honest semantics are
  awkward: pip may be killed between packages (recoverable) or mid-package
  (needs verification, see Finding 1), and a stop during checkpoint
  pre-warm has different resume behaviour (the HF cache resumes partial
  downloads; pip does not resume a killed wheel the same way). If stop
  cannot be specified precisely per phase, install-to-completion with a
  clear "this takes several minutes, do not close the dashboard" is
  simpler and more honest than a stop button whose result nobody can
  describe. The browser-install precedent has a stop button, but that
  precedent installs a browser tool, not a multi-GB model stack — copying
  the control without re-examining it is precedent worship, not design.
- **Pre-warming as part of install.** If "installed" means pip plus
  checkpoint pre-warmed, the install job now contains a model load
  (2–3 GB RSS, ~50 s plus download, TMPDIR-sensitive) inside an installer
  whose job was supposed to be pip. That is two jobs wearing one button,
  and it drags the grade runner's resource problem into the installer.
  Pip-only with honest labelling (Finding 4) is the simpler shape; the
  draft must justify pre-warm against that, not assume it.

## Simpler alternatives, ranked

1. **Copy button + "Check again" button (cheapest, and the honest
   baseline).** Replace the bare pip command with the exact command for
   the detected environment (CPU torch first on CPU-only hosts, Docker
   rebuild snippet on Docker), a copy-to-clipboard button, and a
   "Check again" button that re-runs `available`. Cost: no server-side
   code execution, no new runner, no lock, no stop semantics, no
   orphan handling, no log cap, no error taxonomy — and it works on
   Docker and read-only environments, where the button cannot, because
   it tells the user the right command for their environment instead of
   running the wrong one server-side. What it gives up: zero-terminal
   operation on easy platforms. Given the button also fails on hard
   platforms, that trade deserves a real comparison, not dismissal by
   the browser-first law. Note the law says no feature is done until it
   works from the browser; a one-time environment setup step is arguably
   not "the feature" (grading is), and the draft should argue that
   distinction rather than assume the law settles it.
2. **Environment-aware install guide (no execution, better diagnosis).**
   Detect the environment server-side (interpreter, writability, Docker
   marker, CPU vs CUDA, free disk/RAM) and render the correct command
   plus a preflight verdict ("3.1 GB free, need ~4 GB — free space
   first"). This solves the problems the button ignores (wrong torch,
   Docker persistence, disk) without taking on the button's risks, and
   it reuses the detection work the button needs anyway. If detection is
   built for the guide, the button becomes a smaller later step.
3. **Install button, but narrowed.** Button only where it can actually
   work: writable dashboard-owned environment, persistent (not Docker
   ephemeral), sufficient disk/RAM preflight passed. Everywhere else the
   page shows the environment-specific command (alternative 2) instead
   of a button that will fail. This keeps the zero-terminal win where
   it is real and stops claiming it where it is not. The cost is the
   full runner machinery, but at least it is spent on a claim that is
   true.
4. **Auto-install on first grade.** Already rejected by the brief, and
   the rejection stands: it hides a multi-minute, multi-GB download
   behind "Grade diff" with no consent and no progress story, and it
   fails on Docker without solving persistence. It is listed here only
   to confirm it should stay rejected.

## Questions the draft must answer (acceptance checklist)

1. Exact install sequence per platform class, including the torch
   variant — and whether "fixed command" means one string or one
   server-chosen sequence.
2. Version policy: pinned versions, or installed-version check before
   any reinstall/upgrade (non-goal: no automatic updates).
3. Target environment rule: `sys.executable -m pip` (never PATH pip),
   writability check, and the fallback — if any — that the dashboard
   interpreter can actually observe.
4. Docker persistence: volume, rebuild step, or explicit ephemeral
   statement shown in the UI.
5. Definition of "installed": pip only (with honest labelling) or pip
   plus checkpoint pre-warmed — and if pre-warmed, by what mechanism,
   with what TMPDIR/cache/RAM handling, and without polluting the
   rating ledger.
6. Verification as loadability (subprocess smoke check), including
   import-cache invalidation after install, and the repair path for a
   verified-but-broken install.
7. Preflight thresholds (disk, RAM, temp-dir space, PyPI reachability)
   with plain-English refusals before any download.
8. State scope: dashboard-process scope, log location and cap, and
   shutdown/restart/orphan semantics for the pip child.
9. Contention policy across install, grade, and browser-install
   runners, and what a blocked user sees (queue vs refuse).
10. Endpoint security: loopback/token inheritance, CSRF, confirm-first
    naming the exact command.
11. Stop semantics per phase (pip vs checkpoint download), and the
    retry-and-verify contract after a stop or failure.
12. Post-install grading: auto-enable or leave off (decided explicitly,
    with the off-by-default promise addressed), button lifecycle across
    failed/stopped states, and grade/route form behaviour during install.
13. Failure taxonomy: the mapping from pip/exit-code/disk/network
    failures to plain-English messages — the bulk of the user-facing
    work, currently unspecified.
14. Progress presentation: log tail + state + elapsed time, no invented
    percentage.

## Bottom line for the draft

Do not draft "an Install button." Draft an install *outcome* — a backend
that is present, loadable, persistent across restarts, and sufficient for
a first grade to succeed — and then decide which mechanism delivers that
outcome on each platform. On some platforms that mechanism is a button;
on others it is the right command, shown with a preflight verdict. A
design that starts from the button and works backwards will reproduce
every gap above; a design that starts from the outcome will either earn
the button or replace it with something cheaper and more honest.
