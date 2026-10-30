# Implementation Readiness — CDA install button (BRAINSTORM)

**Lens:** implementation readiness. **Mode:** BRAINSTORM.
**Brief judged:** `~/workspace/baton-cda-install/concepts_brief.md` (tracking issue Gane2506/baton#366).
**Code read (read-only):** `code_grade.html`, `routes_code.py`, `browser_install.py`,
`code_grade_runner.py`, `code_orchestrator/backend.py`, `docs/code-orchestrator.md`,
plus `pyproject.toml` (the `code-orchestrator` extra) and the browser-install route
wiring in `routes_connections.py` / `core.py`.

## Verdict

Not ready to draft from as written. The brief is honest about its biggest risks
(Docker persistence, what "installed" means, environment detection) but it
leaves them as open decisions, and it leaves several larger questions
unasked. An implementer picking this up would still have to invent: the
exact command, the target environment decision tree, the verification
procedure, the failure-to-English mapping, the install/grade lock scope,
and the rollback story. Worse, two of the seemingly obvious implementations
are actively hazardous and the brief does not rule them out: running the
page's current command verbatim, and trusting `CodeOrchestrator.available`
as post-install verification.

Nothing below is a complaint about the concept. Each item is a question
the draft spec must answer with a concrete decision before code is written.

---

## Blocker 1 — The fixed command is never named, and the obvious one is dangerous

- **Severity:** Blocker
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** The brief says the command must be fixed
  server-side (correct, following the `BrowserInstallRunner` precedent in
  `browser_install.py`, where `install_argv()` is fixed and no request can
  change it), but it never states what the CDA command is. The only command
  in evidence is the one on the page today:
  `pip install "jev-agent[code-orchestrator]"`. Running that verbatim from
  the dashboard is self-modification: it asks pip to install the dashboard's
  own package, into the running dashboard's own environment, while the
  dashboard is running. On a source checkout with an editable install, pip
  may reinstall, replace, or downgrade `jev-agent` itself; on a Docker
  deploy it may fight the pinned image. The safe alternative — install only
  the extra's contents (`laya`, currently `laya>=0.4.15` in `pyproject.toml`)
  without touching `jev-agent` — is a different command with different
  failure modes, and the brief does not choose between them.
- **Migration requirement:** The draft must print the exact argv, per
  platform, and state explicitly: the install never installs, upgrades, or
  replaces `jev-agent` itself. It must also say which version of `laya`
  gets installed (pinned, lower-bounded as today, or matched to the running
  dashboard version) and what happens when a future `laya` release breaks
  `laya_backend.py`, since there is no update mechanism (out of scope) to
  repair that later.

## Blocker 2 — "Verification via `CodeOrchestrator.available`" proves almost nothing

- **Severity:** Blocker
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** `available` (`backend.py`) is true when `laya`
  is merely *findable* — `load_backend` checks `importlib.util.find_spec("laya")`
  and deliberately does not import it, because `import laya` pulls torch.
  So a pip run that installs a broken `laya` (torch wheel wrong for the
  platform, missing DLL on Windows, CUDA libraries present but unloadable)
  passes verification, the panel flips to "installed", and the first real
  grade then fails with `CodeOrchestratorError` — which, by the error
  taxonomy in `backend.py`, blocks the quality gate fail-closed. That is a
  worse outcome than "not installed": the user went from a clear message
  to a blocked run. The same gap exists in reverse: `find_spec` passing
  while torch is absent means a partially installed backend also reads as
  installed.
- **Migration requirement:** The draft must define verification in layers
  and say which layer gates the "installed" flip: (a) package findable,
  (b) `laya` + torch importable in a fresh subprocess (not the dashboard
  process, which would need `importlib.invalidate_caches()` after pip and
  would still hold old module state), (c) a smoke load of the model. Layer
  (c) downloads the ~842 MB checkpoint, so if the draft picks it, it must
  also own the checkpoint-download story (see Blocker 4). Whatever is
  picked, the draft must state what verification does *not* prove.

## Blocker 3 — The target environment is a decision tree, not a default

- **Severity:** Blocker
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** The brief names `sys.executable -m pip` as "the
  sane default" with a `--user` or dedicated-venv fallback, but an
  implementer cannot code that sentence. The failure cases are concrete
  and platform-specific:
  - Modern Debian/Ubuntu, Homebrew, and recent macOS Pythons refuse pip
    installs into the system Python (PEP 668, externally managed
    environment) unless `--break-system-packages` is passed — which the
    draft must explicitly allow or forbid, not leave to the implementer.
  - A Windows service account or a VPS service account may have no
    writable home, no writable site-packages, or a `PATH` that does not
    include pip at all.
  - A dedicated venv, if chosen, needs a location, an owner, and an
    answer to how the *dashboard process* then finds it on the next start
    (a venv the dashboard does not know about is an install into the void).
  - Installing into the dashboard's environment while the dashboard runs
    means pip may replace shared dependencies (numpy, transformers) under
    a live process; already-imported modules keep old code, newly imported
    ones get new code.
- **Migration requirement:** The draft must contain the environment
  decision tree: how the runner detects venv vs system vs service Python,
  the ordered fallback list, the exact condition that selects each branch,
  and the plain-English error for each terminal failure. "A plain-English
  error when neither works" is a goal, not a spec — the error strings (or
  at least the mapping from pip failure to message) must be written down.

## Blocker 4 — "Installed" is still undefined, and the checkpoint half doubles the problem

- **Severity:** Blocker
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** The brief correctly flags that pip alone leaves
  a multi-minute first grade with no warning, and defers pip-only vs
  pip-plus-checkpoint-pre-warm to the draft. That deferral hides three
  further decisions. First, the checkpoint goes to the Hugging Face cache
  (`~/.cache/huggingface/hub` or `$HF_HOME`), which is a *different*
  location from site-packages with its own persistence story — a Docker
  volume for site-packages does not save the cache, and vice versa.
  Second, `docs/code-orchestrator.md` records a live trap: the first load
  reconstructs the checkpoint through the temp dir, and a default `/tmp`
  that is a 512M tmpfs fails with "No space left on device", so any
  pre-warm must set `TMPDIR` to a directory on a real disk — which
  directory, on each platform, is unspecified. Third, a partially
  downloaded checkpoint (killed pre-warm, dropped network) needs stated
  resume behaviour: does the next grade resume or restart the download,
  and what does the user see meanwhile?
- **Migration requirement:** The draft must define "installed" as one
  checkable state, name the checkpoint cache location per platform and
  deploy type, set `TMPDIR` handling for any load/pre-warm step, and
  state partial-download behaviour. If pre-warm is chosen, the draft must
  also say whether the pre-warm runs in the dashboard process (torch
  import, ~2–3 GB RSS, ~50 s load) or a subprocess, and what a failure
  there means for the "installed" flip.

## Blocker 5 — The naive install downloads ~2.5 GB of CUDA torch onto CPU-only machines

- **Severity:** Blocker
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** `docs/code-orchestrator.md` documents that
  `pip install laya` pulls the default torch wheel including CUDA
  libraries (~2.5 GB), and that CPU-only machines should install the CPU
  wheel first from the PyTorch CPU index, then `laya`. The brief never
  mentions this. An implementer following the brief literally ships a
  button that downloads ~2.5 GB + 842 MB on a CPU-only Mac, Linux VPS, or
  Windows box, takes far longer than any progress UI implies, and can
  fail on disk space that a CPU-wheel install would have fit in. CUDA
  detection (is a GPU present? is CUDA usable?) is itself a decision:
  a wrong guess in either direction wastes gigabytes or breaks grading.
- **Migration requirement:** The draft must state the torch strategy:
  CPU wheel first, CUDA wheel when detected, or default wheel accepted
  with its size stated honestly in the UI. It must give the exact index
  URL and install order for the CPU path, the detection method, and the
  disk-space preflight threshold that accounts for the chosen wheel plus
  the checkpoint plus temp staging — not just the 842 MB the brief quotes.

## Blocker 6 — Docker persistence has no answer, and "works on a VPS" depends on it

- **Severity:** Major (Blocker for the VPS claim specifically)
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** The brief names this as the largest open risk
  and offers three shapes (volume, documented rebuild, explicit
  ephemerality). That is the right list, but none is chosen, and each
  choice changes the implementation: a volume needs exact paths
  (site-packages? HF cache? both?) and must survive image upgrades, not
  just container restarts; a rebuild step needs a documented command the
  user runs outside the dashboard, which reintroduces the terminal the
  feature exists to remove; ephemerality needs UI copy that says the
  install will vanish, plus a re-install path that is not a surprise.
  There is a fourth shape the brief misses: the Docker image may run as
  non-root or with a read-only filesystem, in which case pip fails
  outright and the button must say so rather than spin.
- **Migration requirement:** The draft must pick one Docker story, name
  the persisted paths, and state what happens to an install when the
  image version changes. Until it does, "same button on Mac, Windows,
  Linux, and VPS" is an untestable claim: the VPS case cannot pass
  acceptance.

## Major 1 — Single-flight scope is undefined across processes, repos, and restarts

- **Severity:** Major
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** `BrowserInstallRunner` is single-flight
  *within one dashboard process* (a lock, a thread, in-memory state).
  The brief says "one at a time" without saying one at a time *where*.
  Two dashboard processes serving two repos on one machine share one
  Python environment, and pip has no cross-process lock — two concurrent
  pip installs into the same site-packages can corrupt it. The dashboard
  lifespan (`core.py: shutdown_jobs`) stops background jobs on shutdown,
  so a dashboard restart during an install kills the install in the
  `BrowserInstallRunner` model — but the brief does not say whether the
  CDA runner mirrors that, or lets pip run orphaned. If it mirrors it,
  restart-during-install is a defined failure with a retry story; if it
  does not, the next start finds a running pip it does not own, a log
  file it may truncate (the browser precedent truncates the log on each
  start), and state it cannot report. Either way, the log lives under the
  repo's `.jev/` directory (`settings.root`), while the install targets
  the dashboard environment — switching repo roots orphans the install's
  log and status while the install itself continues.
- **Migration requirement:** The draft must state the lock scope
  (per-process only, with a documented cross-process hazard, or a real
  file lock), the restart-during-install behaviour (killed, orphaned, or
  reattached), where install state and logs live (per-repo `.jev/` or a
  per-environment location), and whether a second start truncates the
  previous log.

## Major 2 — Install/grade interaction is undecided, and the wrong default corrupts both

- **Severity:** Major
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** `CodeGradeRunner` allows one grade at a time
  because the model is a multi-GB torch process, and its cancel is honest
  that the model call cannot be interrupted (cancel discards the result
  and the runner drains). The brief lists shared-lock vs independent
  runners as open decision 5. The stakes are higher than contention:
  a grade cannot start without a backend, so a grade running while an
  install replaces `laya`/torch underneath it can import a half-written
  package or a new version mid-run; an install starting while a grade
  holds torch files can fail on Windows, where in-use files lock. The
  brief also does not say what the user sees when one blocks the other —
  a disabled button, a queued job, or an error — nor whether a cancelled
  grade's draining thread counts as "busy" for install purposes.
- **Migration requirement:** The draft must state the lock relationship
  (shared lock across both runners, or independent with stated hazards),
  the exact UI state when an install is requested during a grade and vice
  versa, and whether grade cancel's draining state blocks an install
  start.

## Major 3 — Stop semantics for pip are not the same as stop semantics for npx

- **Severity:** Major
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** The browser precedent's `stop()` terminates
  the installer process. Terminating pip mid-install is a different act:
  pip may be mid-uninstall of an old version, mid-write of a package, or
  mid-download of a multi-hundred-MB wheel. The brief requires "a stopped
  install leaves a safe, retryable state" and notes "pip is idempotent per
  package", but that sentence is doing too much work: pip re-running over
  a half-installed torch is usually recoverable, but it is not guaranteed,
  and a stop during the checkpoint pre-warm (if chosen) has different
  recovery. The brief also leaves stop itself as open decision 6
  (stop button vs install-to-completion only). If there is no stop button,
  a multi-minute install with no exit needs that stated plainly, plus
  what closing the tab or restarting the dashboard does.
- **Migration requirement:** The draft must decide stop vs
  install-to-completion, define what "safe, retryable" means after a stop
  at each phase (download, unpack, install, pre-warm), and state whether
  retry is a full re-run or a resume. A test case for stop-during-torch
  followed by retry must be in the acceptance list.

## Major 4 — Broken backends are invisible to a button shown "exactly when the backend is missing"

- **Severity:** Major
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** The goal says the button shows exactly when
  the backend is missing, where missing means `available` is false. But
  several real broken states read as available: `laya` present with torch
  absent or broken (see Blocker 2), `laya` present at a version whose API
  no longer matches `laya_backend.py`, or a checkpoint cache corrupted
  after a previous interrupted download. Those users never see the button
  and have no browser path to repair — the feature's own goal strands
  them. The same applies to users who installed manually before this
  feature and later break their environment. A reinstall/repair affordance
  is not scope creep here; without it, "failure states in plain English
  with retry" covers only first-install failures, not the failures users
  actually hit second.
- **Migration requirement:** The draft must either add a repair/reinstall
  path for backends that are present-but-broken (with the condition that
  surfaces it), or explicitly record that broken-but-present is out of
  scope and say what those users are told instead. It must also say what
  happens to an install when the dashboard itself upgrades and the
  installed `laya` version is now stale — there is no update mechanism,
  so staleness needs at least a stated detection and message.

## Major 5 — No failure taxonomy, so "plain English with retry" cannot be implemented

- **Severity:** Major
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** The brief requires plain-English failures with
  retry but gives no mapping. Pip failures an implementer will actually
  meet include: no network / index unreachable, proxy required, disk full
  mid-wheel, permission denied, PEP 668 refusal, package not found for
  this Python version or architecture (torch wheels do not exist for
  every combination — Windows ARM and very new/old Pythons are the
  usual gaps), hash mismatch, and a killed process. Each needs a distinct
  message and a distinct next step (retry helps for network, never for
  disk-full or PEP 668). Without the table, implementers will surface pip's
  raw stderr — which is exactly the terminal experience the button exists
  to replace — or collapse everything into "install failed, try again".
- **Migration requirement:** The draft must contain the failure table:
  detectable condition, user-facing message, whether retry is offered,
  and what changes before retry (free space, different environment,
  network). It must also set the log policy: pip output is verbose and
  progress-bar heavy, so the draft must say what the browser shows (tail
  lines? filtered lines? indeterminate progress?), how large the stored
  log may grow (the browser precedent caps at 256 KiB and tails 30 lines),
  and the poll interval for the progress UI.

## Major 6 — No acceptance criteria, no test matrix, no rollback

- **Severity:** Major
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** The brief has goals but no measurable
  acceptance criteria: no stated condition for "the status panel flips
  without a restart" vs "says a restart is needed" (open decision 7 — and
  note the mechanism matters: a fresh `CodeOrchestrator` per page render
  in `routes_code.py` sees a new install, but any long-lived orchestrator
  elsewhere, e.g. the quality gate, may hold a cached unavailable state),
  no timeout or duration expectation for the install, no poll/refresh
  behaviour, and no definition of done for the UI wiring. The UI wiring
  itself is unspecified: `code_grade.html` shows a generic `job` banner
  fed by the grade runner, and `/code-grade/status` polls the grade
  runner's live fragment — an install runner is a second job source, and
  the draft must say whether install progress gets its own page/fragment
  or shares the grade status page, and what the main page banner shows
  during an install. Rollback is entirely absent: there is no uninstall,
  no downgrade path, no "restore the previous environment" step, and no
  statement of what a failed install leaves behind. "Retry" is recovery,
  not rollback, and the two must not be conflated.
- **Migration requirement:** The draft must include: measurable acceptance
  criteria (button visibility rule, flip-without-restart condition per
  process, progress visibility, retry behaviour); the route/template
  wiring (new routes, CSRF handling per the `checked_form` precedent,
  confirmation step per the browser-install Tier 2 precedent or an
  explicit decision against it, and what the existing `job` banner shows
  during an install); a test matrix (Mac venv, Mac system Python, Windows
  user install, Windows service, Linux venv, Linux system Python, VPS
  service account, Docker) crossed with success, each failure in the
  taxonomy, stop-during-install, restart-during-install, grade-during-
  install, double-submit, and partial-install recovery; and a rollback
  section stating what can be undone (nothing, reinstall of a pinned
  version, manual pip uninstall) and what cannot.

## Major 7 — Security and exposure assumptions are unstated

- **Severity:** Major
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** A button that runs pip into a server
  environment is a privileged operation. The browser-install precedent
  mitigates it with a confirmation page naming the exact command and
  with CSRF-checked forms. The brief carries the fixed-command rule but
  does not carry the confirmation decision (open decision 6), does not
  state the dashboard's exposure assumption (local-only vs reachable on a
  VPS network), and does not say whether any dashboard session may
  trigger a multi-GB download or only some role. On a VPS, where the
  brief explicitly wants this to work, that assumption is load-bearing:
  an unauthenticated or shared dashboard with an install endpoint is a
  remote code execution-adjacent feature even with a fixed command,
  because the command itself installs and executes code.
- **Migration requirement:** The draft must state the threat assumption
  (who can reach the dashboard, who may press Install), keep or
  explicitly drop the confirm-first step with reasoning, keep CSRF
  protection on the new routes, and say whether repeated install
  attempts are rate-limited or logged.

## Nit 1 — Install size, time, and progress honesty

- **Severity:** Nit
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** The brief quotes "~842 MB" and "~1 GB" but
  the real total on the default path is ~2.5 GB torch + laya +
  transformers + 842 MB checkpoint + temp staging. Progress UI that
  implies a short install will read as hung. The draft should state the
  honest size/time per path (CPU vs CUDA wheel) and what the progress UI
  shows when pip gives no percentage (indeterminate state with log tail,
  not a fake percentage).
- **Migration requirement:** Put the real numbers in the UI copy and in
  the disk-space preflight, per torch path.

## Nit 2 — Post-install grading enablement changes gate behaviour

- **Severity:** Nit
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** Open decision 4 (auto-enable grading vs leave
  off) is not just UX: grading is off by default, and the quality gate
  treats an enabled-but-unavailable backend as "not graded" while an
  enabled-and-available backend grades every diff and can block on
  inference failure. Auto-enabling on install silently changes every
  subsequent quality-gate run. The draft must pick one and, if
  auto-enable is chosen, say so in the install confirmation and in the
  post-install message.
- **Migration requirement:** One sentence in the draft deciding this,
  plus the UI copy that discloses it.

## Nit 3 — The pip command copy on the page and in `unavailable_message` must be retired consistently

- **Severity:** Nit
- **Dimension:** Migration & Backward Compatibility
- **Compatibility hazard:** The pip instruction appears in two places:
  `code_grade.html` and `code_grade_runner.unavailable_message()` (used
  for failed grades). Replacing only the page copy leaves failed grades
  still telling the user to open a terminal. The draft's file-change list
  should name both, plus `docs/code-orchestrator.md` (which documents the
  manual install and the CPU-wheel procedure the button may or may not
  follow).
- **Migration requirement:** Name all three locations in the draft's
  change list and state which copy changes and which stays as fallback.

---

## What the draft spec must nail down (checklist for the drafter)

1. Exact fixed argv per platform, and the rule that `jev-agent` itself is
   never installed/upgraded by it; `laya` version policy.
2. Torch strategy: CPU vs CUDA detection, index URL, install order, and
   honest size/time/disk preflight per path.
3. Environment decision tree: detection, fallback order, PEP 668 stance,
   service-account handling, and the error message per terminal failure.
4. Definition of "installed", checkpoint cache location per platform and
   deploy type, `TMPDIR` handling, partial-download resume behaviour.
5. Verification layers and which layer flips the panel; subprocess-based
   import check at minimum; what verification does not prove.
6. Docker story: persisted paths, image-upgrade behaviour, read-only /
   non-root failure message, or explicit ephemerality with UI copy.
7. Lock scope: per-process vs cross-process, install/grade interaction,
   draining-state rules, restart-during-install behaviour, log/state
   location and truncation policy.
8. Stop semantics per phase, retry as re-run vs resume, and the
   stop-during-torch test case.
9. Failure taxonomy table: condition → message → retry offered? →
   what must change first.
10. UI wiring: routes, CSRF, confirmation step, progress page/fragment,
    main-page banner during install, poll interval, log tail/cap policy.
11. Broken-but-present repair path, or an explicit out-of-scope statement
    with the message those users see.
12. Post-install grading enablement decision and its disclosure copy.
13. Security assumption: who may press Install on a network-reachable
    dashboard, rate limiting, logging.
14. Rollback section: what can be undone, what cannot, and the manual
    recovery command for each failure the button cannot fix.
15. Measurable acceptance criteria and the platform × failure test matrix
    in Major 6.
