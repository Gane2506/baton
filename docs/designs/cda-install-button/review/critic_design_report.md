# EGM Stage 7 — Design Critic report (design lens)

* **Target:** `docs/designs/cda-install-button/design.md`, feature-specification
  part only — everything before its Detailed Implementation Plan (section 8),
  i.e. sections 1–7. Section 9 option sets and sections 10–12 are cited only
  where sections 1–7 themselves claim to be answered by them.
* **Standard applied:** `elephant-goldfish-model/resources/document_standards/feature-spec-standard.md`
  (Problem Statement, Goals & Non-Goals, Proposed Architecture, Alternatives
  Considered, Risks & Mitigations; a fresh engineer must be able to understand
  the feature, its motivation, its trade-offs, and its major risks).
* **Files read, and nothing else:** the persona file, the standard above,
  `design.md`, `concepts_brief.md`, and, in the same worktree,
  `src/jev_agent/dashboard/templates/code_grade.html`,
  `src/jev_agent/code_orchestrator/backend.py`,
  `src/jev_agent/code_orchestrator/laya_backend.py`,
  `src/jev_agent/dashboard/browser_install.py`,
  `src/jev_agent/dashboard/code_grade_runner.py`,
  `src/jev_agent/dashboard/core.py`,
  `src/jev_agent/dashboard/routes_code.py`,
  `src/jev_agent/dashboard/security.py`,
  `src/jev_agent/dashboard/templates/learning.html`,
  `pyproject.toml`, `docs/code-orchestrator.md`, `docs/deploy.md`, `Dockerfile`.
* **Verdict on the scoped range:** it does **not** pass the standard as a
  stand-alone specification. The problem and goals are clear, but the
  scoped range contains no Alternatives evaluation at all (that material,
  wherever it sits elsewhere in the file, is outside this review's scope
  and does not cure the scoped spec's silence), and — more seriously —
  several sections claim a brainstorm blocker is answered when the
  answering section only restates the problem, defers the decision to an
  unpicked option set, or defers it to the implementer at code-review time.
  A fresh engineer could not implement sections 5.2, 5.3, 5.4 (branch 2
  and Docker), 6.4, or 6.5 from this text without inventing the missing
  mechanism. That is not a drafting polish issue; it is the core of the
  design being undecided while presented as decided.

Counts: **6 Blockers, 12 Majors, 6 Nits.**

---

## Blockers

### B1 — Section 5.2 (lines 299–332): the "loadability" verification layer does not exist in the cited code, and its probe command is wrong

* **Section/line:** §5.2, lines 299–332; also §3 Goals (lines 163–189) and
  §5.1 (lines 280–298).
* **What is wrong:** The design defines layer 2 as "a backend object can be
  constructed without loading the checkpoint" and says the probe constructs
  it, "if the backend API allows it; otherwise the probe stops at the import
  step". In the code read, there is no such middle layer:
  * `backend.py:load_backend` returns `LayaBackend(model_id)` without loading
    anything — construction is lazy by design. `LayaBackend.__init__` in
    `laya_backend.py` stores `model_id` and an optional agent; it imports
    nothing.
  * The only load path is `LayaBackend._agent_or_load`, which calls
    `laya.load(model_id)` — i.e. it loads the checkpoint (layer 3).
  * So the probe has exactly two real options, neither of which is layer 2:
    construct the object (trivially true once `laya` is importable, proves
    nothing about torch) or call `laya.load` (a full model load, which
    downloads the checkpoint and pulls 2–3 GB of torch into the probe —
    contradicting §5.2 step 3's "does not download it unless pre-warming
    is chosen" and §6.3's separation of verify from warm-up).
  * The presence probe itself is written as `sys.executable -m python -c ...`
    (line 305). That argv is nonsense: `sys.executable` already *is* the
    Python interpreter; `-m python` asks it to run a module named `python`.
    The correct form is `sys.executable -c ...`. A spec that gets its one
    concrete probe command wrong cannot be trusted on the subtler claim
    that the probe proves loadability.
  * §5.2's no-restart flip has no mechanism at all. It says "if verification
    passes but the running dashboard process still cannot see the backend
    (import cache), the panel says restart is needed" — but never says how
    the running process re-checks, and never mentions
    `importlib.invalidate_caches()`. A fresh-subprocess success does not
    prove the running process can see the package, and the design gives no
    rule for when the flip-without-restart in §3 is achievable versus when
    the restart message appears. The acceptance condition in §3 is therefore
    untestable from this spec.
* **Evidence:** `backend.py` (`load_backend`, `CodeOrchestrator.available`,
  docstring: "importing this module must not import torch", "both backends
  load lazily on first grade"); `laya_backend.py` (`LayaBackend.__init__`,
  `_agent_or_load` calling `laya.load(self.model_id)`); `docs/code-orchestrator.md`
  ("the first grade downloads the ~842 MB checkpoint", "laya.load
  reconstructs the checkpoint through the temp dir").
* **Recommendation:** Either define layer 2 honestly as "import `laya` and
  `torch` in a fresh subprocess and report versions" (and delete the
  backend-construction fiction), or add, as a *decided* spec requirement, a
  new no-checkpoint construction entry point in `backend.py` — and state
  the exact no-restart mechanism (`invalidate_caches` + re-run of
  `available`, or an explicit always-restart rule). Fix the probe argv.

### B2 — Section 5.3 (lines 333–372) vs §7.1 (lines 708–734): the torch strategy — the 2.5 GB decision — is deferred, and the two sections contradict each other on the argv

* **Section/line:** §5.3, lines 333–372; §7.1, lines 708–734.
* **What is wrong:**
  * §5.3 says detection is "done by the dashboard process (for example,
    whether CUDA is reported available)" and "the exact detection call is
    an implementation detail the implementer must name in code review,
    because a wrong detection on a CPU-only host is the 2.5 GB mistake
    this section exists to prevent." That is an admission, not an answer:
    the section whose stated purpose is to prevent the 2.5 GB mistake
    contains no detection method. Worse, it is a chicken-and-egg problem
    the design never names: before torch is installed there is no torch
    to report CUDA availability. A GPU check without torch (e.g. driver /
    device query, per OS) is a different mechanism on Mac, Windows, and
    Linux, and none is named, ranked, or given a fallback when it is
    inconclusive. §4 claims this blocker is "answered by section 5.3";
    it is restated there.
  * §5.3 says "the fixed install sequence installs torch before `laya`,
    in this order" on *both* paths (default wheel, then `laya`; CPU wheel,
    then `laya`). §7.1 says "the argv installs `laya` (and torch first,
    on the CPU path) only" — i.e. on the default path torch arrives only
    as a `laya` dependency. Both cannot be the fixed argv. The difference
    matters: a separate explicit torch install pins the variant; a
    dependency-resolved torch install can be re-resolved by pip to a
    different variant, which is exactly the failure §5.3 warns about.
  * §5.3 also says the CPU install must install `laya` "without letting
    pip re-resolve torch to the default wheel" but names no mechanism
    (`--no-deps` would break other `laya` dependencies; a constraints file
    is never mentioned). The implementer is left to invent the mechanism
    that makes the whole strategy work.
* **Evidence:** `pyproject.toml` lines 46–48 (`laya>=0.4.15`, no torch pin);
  `docs/code-orchestrator.md` CPU section ("install the CPU wheel first,
  then `laya`"); design §5.3 vs §7.1 wording quoted above.
* **Recommendation:** Decide, in the spec: the detection call per OS, its
  fallback when inconclusive, the exact argv for both paths (including the
  anti-re-resolution mechanism), and which of §5.3 / §7.1 is normative.

### B3 — Section 5.4 (lines 373–429): the deployment matrix is normative in words and empty in its hardest cell, and branch 2 contradicts §5.2

* **Section/line:** §5.4, lines 373–429, especially the Docker row (line 423)
  and branch 2 (lines 383–391).
* **What is wrong:**
  * The matrix is declared "normative: an implementation that shows the
    button on a deployment shape without the row's UI statement has not
    implemented this design." Yet the Docker row's Target environment is
    literally "See option set G" and its UI statement is "the ephemerality
    warning, or the volume path, per option set G". That is not a row; it
    is a pointer to an unpicked decision. §4 claims §5.4 answers the
    "same button everywhere contradicts the deploy model" blocker. For
    Docker — the deployment the brief calls "the largest open risk" — it
    does not. The same applies to the persistence column for Windows
    service / Linux system Python / VPS ("As Windows service"), which
    delegates to a row that itself delegates to branch 2 or 3.
  * Branch 2 (dedicated venv) contradicts §5.2: §5.2 says the probe runs
    "the dashboard's own interpreter (`sys.executable`)", while branch 2
    says "the dashboard process must then use that venv's interpreter for
    the verification probe and for later grades". No rule says which
    interpreter the panel flip, the probe, and the grade runner each use
    when branch 2 is active, and the design concedes "if it cannot [accept
    an interpreter override], this branch is unavailable" — so the matrix
    cell for the most fragile deployments (service accounts, PEP 668
    system Python) rests on a capability the design does not establish
    exists. In the code read, `CodeGradeRunner` has no interpreter
    parameter at all (its executor builds `CodeOrchestrator` in-process),
    so branch 2 as written requires redesigning the grade path, not just
    the install path.
  * The Mac uv-tool row asserts branch 1 ("own interpreter") as the normal
    case while the design's own open-questions section (outside this
    review's scope, but the contradiction is visible from §5.4 alone)
    admits the behaviour is unproven; §5.4 presents as fact what is at
    best a hypothesis ("a `uv tool install --force` reinstall recreates
    the tool environment and wipes the extra"). A normative matrix must
    not assert unproven persistence behaviour.
  * Docker writability is never established from the cited files: the
    image runs as non-root `jev` (`Dockerfile` line 42, `USER jev`),
    `WORKDIR /data`, and `/usr/local` is copied from the builder as root.
    The design never says whether `jev` can write to the site-packages
    directory at all, so branch 1 for Docker is not merely undecided —
    it may be impossible, and the matrix hides that behind "See option
    set G".
* **Evidence:** `Dockerfile` (non-root `jev`, `WORKDIR /data`, builder copy
  of `/usr/local`); `docs/deploy.md` (Docker: "image runs as the non-root
  user `jev`", "bind-mounted repo must be writable by that uid", named
  volume `jev-state` "shadows an existing `.jev/`"); `code_grade_runner.py`
  (no interpreter parameter; `_default_executor` builds the orchestrator
  in-process); design §5.4 lines 383–391, 423.
* **Recommendation:** Fill the Docker cell with a decided target, a
  decided persistence answer, and a writability finding (or collapse it
  to branch 3 with the exact command). Decide the branch-2 interpreter
  rule and whether the grade runner gains an override, or delete branch 2
  from the matrix.

### B4 — Sections 6.5 (lines 556–579) and 6.9 (lines 656–680): the durable job record cannot do what §6.5 claims, and its location does not exist in the cited code

* **Section/line:** §6.5, lines 556–579; §6.9, lines 656–680.
* **What is wrong:**
  * §6.5 says: "On dashboard start, a record that says 'running' but has
    no living child process becomes `interrupted`." The record fields
    listed in the same section are: fixed argv, target environment
    decision, started time, current phase, exit code when finished,
    verification outcome. There is **no PID, no process identity, no log
    path** in that list. From those fields alone, "no living child
    process" is uncheckable. The interrupted-detection mechanism is
    therefore unimplementable as specified.
  * "The dashboard state directory" is undefined. Every state location in
    the code read is repo-scoped: `BrowserInstallRunner` logs under
    `<repo>/.jev/browser-install.log`; `CodeGradeRunner`'s ledger is
    `ledger_path_for_repo(settings.root)` (i.e. `<repo>/.jev/cda-ratings.jsonl`);
    `DashboardCore` is constructed with `settings.root`, a repo root, and
    owns one runner set per process. There is no dashboard-wide,
    repo-independent state directory in the cited code, and §6.5/§6.9
    never name one (no path, no env var, no creation/ownership rule).
    The one concrete path §5.4 offers for branch 2, `<state>/.jev/cda-venv`,
    reuses the `.jev/` name the design elsewhere forbids for the job
    record, which only deepens the confusion.
  * §6.9 claims "the install status banner appears on the Code Grade page
    of every repo while an install runs" and "a finished install flips
    every repo's panel, because availability is a property of the shared
    environment". But coordination and state in the cited code are
    per-process (`DashboardCore` per repo/process; each runner's lock
    guards only itself). No cross-process notification, shared store, or
    polling of a shared record is specified in §§6.5/6.9. A second
    dashboard process would not see the first process's in-memory banner
    state, and — since the durable record's location is undefined — would
    not reliably see the durable state either. The cross-repo claim is
    therefore unsupported by the architecture the design itself describes
    in §6.4 ("the coordination lock is per dashboard process").
  * "Orphaned children are never left running past a dashboard stop"
    (line 570) is false for the crash case: §6.11's termination happens
    on graceful shutdown only. A killed dashboard, a host shutdown, or an
    OOM kill leaves the pip child alive, and §6.5's restart logic (which
    assumes a dead child) then contends with a live one. The design
    claims an absolute ("never") for a graceful-shutdown-only mechanism.
* **Evidence:** `core.py` (`DashboardCore.__init__` takes `settings.root`;
  `shutdown_jobs`); `browser_install.py` (`_log_path` under `root/.jev`);
  `code_grade_runner.py` (`ledger_path` per repo); design §6.5 field list
  (lines 560–564).
* **Recommendation:** Name the state directory (path, owner, creation),
  add PID + log path + host/process identity to the record schema, specify
  the cross-process visibility mechanism (shared file + polling, or drop
  the every-repo claim), and weaken "never" to the graceful-shutdown case
  with a defined crash-recovery rule.

### B5 — Section 4 (lines 199–277) misdescribes its own coverage, and §5.1 (lines 280–298) silently pre-picks the option it claims is unpicked

* **Section/line:** §4, lines 199–277; §5.1, lines 293–298.
* **What is wrong:**
  * §4 opens: "The brainstorm lenses raised 16 blockers. The design
    answers each one in the section noted; a blocker with no answering
    section is a gap in this design, not a deferred detail." The list
    that follows contains **21 bullets**, not 16. The count mismatch alone
    breaks traceability: a reader cannot verify that each of the 16
    brainstorm blockers is answered, because the design's own list does
    not match the number it claims to cover (the brief's 16 blockers plus
    majors folded in without labelling which is which).
  * At least six bullets are marked "answered" by a deferred option set
    or by a section that itself defers: third runner → "option set B";
    stop semantics → "option set E and section 6.6" (but §6.6 itself says
    "whether a stop button exists at all is option set E"); grade/route
    forms → "option set F"; post-install state → "option set D";
    lifecycle → "option set G"; value question → "option set C".
    Under §4's own rule ("a blocker with no answering section is a gap,
    not a deferred detail"), a pointer to an unpicked option set *is* a
    deferred detail, not an answer. §4 therefore certifies as answered
    precisely the items the design leaves open.
  * §5.1 compounds this: "The default position of this design, if the
    human picks no option, is: the panel flips on layer 2 … and layer 3
    is reported separately." That *is* one of the option-set-A shapes
    (two states, not one) — a pre-pick — while the design elsewhere
    insists no option is pre-picked and that an unpicked set blocks the
    parts depending on it. The spec cannot both have a default that
    implements A3 and block implementation until A is picked. An
    implementer reading §5.1 alone would build A3; an implementer reading
    the gate rule alone would build nothing.
* **Evidence:** §4 lines 201–204 and its 21 bullets; §5.1 lines 293–298
  ("default position … flips on layer 2 … reported separately");
  §6.6 lines on option set E; §5.4 Docker row (B3).
* **Recommendation:** Fix the count and label each bullet as Blocker or
  Major with its brief ID; change §4's claim to "answered, deferred to
  option set X, or open" per bullet, honestly; delete the §5.1 default
  or state explicitly that it *is* the decision unless the human overrides
  it (and then the gate rule must say so).

### B6 — Section 5.5 (lines 430–451): "Repair" as specified will not repair, and the panel logic contradicts the `available` implementation it relies on

* **Section/line:** §5.5, lines 430–451; §6.6 (lines 580–614); §2 (lines 78–160).
* **What is wrong:**
  * "Repair runs the same sequence as install" — but the install sequence
    is `pip install laya` (plus torch on the CPU path). When `laya` is
    already present and satisfied, pip reports "requirement already
    satisfied" and does nothing. A corrupted, partially unpacked, or
    version-drifted install is *not* repaired by re-running a satisfied
    install. No `--force-reinstall`, `--ignore-installed`, uninstall-first,
    or cache-clear step is named anywhere in §§5.5/6.2. The repair path
    is therefore a no-op for the exact failure class it exists for.
  * §6.6's safety argument — "no half-installed package is reported as
    installed (verification has not run, so the panel still says 'not
    installed')" — is false under the current panel logic. The panel in
    `code_grade.html` renders `available` from `CodeOrchestrator.available`,
    which returns True when `find_spec("laya")` succeeds (see B1), not
    when verification has run. A partial install that leaves a findable
    `laya` package *will* flip the panel to "available" immediately,
    verification or not, unless the design also changes `available`
    semantics — which the scoped spec never requires (it only says in
    §5.1 "the panel must never show 'installed' on layer 1 alone", with
    no mechanism). The design's own §4 warning ("a half-installed backend
    is worse than a missing one") is thus reintroduced by its own stop
    semantics.
  * §5.5's trigger is also imprecise: it says the button is keyed on
    "backend not usable", and that "present but failing loadability"
    shows Repair — but the only loadability signal the design defines is
    the post-install probe, which runs only after an install. There is no
    specified on-page probe for a backend that was installed *before* this
    feature (e.g. by the old terminal command) and is now broken; the
    panel would show it via `available` (layer 1) and never surface Repair.
    The pre-existing broken backend — the most likely real-world repair
    case — has no defined detection path.
* **Evidence:** `code_grade.html` (`badge("available") if available`);
  `backend.py` (`available` property, `find_spec("laya")`);
  `code_grade_runner.py` (`unavailable_message`); design §5.5, §6.6.
* **Recommendation:** Specify the repair argv explicitly (force-reinstall
  or uninstall-then-install, with its torch consequences), require a
  change to `available`/panel logic so layer-1-only never renders as
  installed, and define how a pre-existing broken backend is detected on
  page load (not only after an install's probe fails).

---

## Majors

### M1 — §6.1 (lines 454–480): preflight is a checklist without thresholds, methods, or a consistent refusal rule

* §6.1 requires disk "covering the torch variant plus checkpoint plus
  temp staging" but never sums the numbers §5.3 gives (≈2.5 GB + 842 MB
  + "at least 1 GB" ≈ 4.3 GB on the default path; unstated on the CPU
  path, where the CPU wheel size is never given). No formula, no margin,
  and no statement of which disk (target env disk vs temp disk vs HF
  cache disk — three locations that may be three different filesystems)
  each number applies to.
* The memory check contradicts the section's own rule: "Each check maps
  to a plain-English refusal when it fails" — but the memory check is
  "at least the 2–3 GB … or a warning". Is memory a refusal or a warning?
  The design picks both, in one sentence.
* Network reachability is unspecified as a mechanism: HEAD to PyPI? DNS?
  A pip dry-run? Proxy handling (env vars, auth) is mentioned only in
  the failure message ("check the network or proxy"), not in the check.
  A preflight reachability pass does not guarantee pip success (index
  auth, package availability, TLS), so the design over-promises what
  preflight proves.
* Ordering is incoherent with §5.4: §5.4 says the environment decision
  "is shown to the user before the install starts", while §6.1 says
  preflight "runs synchronously when the user starts an install". If the
  decision display depends on preflight (writability, disk), it cannot
  precede preflight; if it precedes it, it is shown before the facts it
  reports are known. The spec never orders confirm → preflight → start.

### M2 — §6.4 (lines 525–555): coordination is half-decided, and its ledger guard is placed at the wrong layer

* "The install may be queued or refused — queuing is the friendlier
  choice, but the implementer must pick one" (§6.4). A coordination
  spec that leaves queued-vs-refused to the implementer has not
  specified coordination: the two behaviours need different UI (queue
  position, wait messaging), different durable state (a queued job is
  not in §6.5's record), and different tests. The same paragraph also
  says the blocked side "shows 'waiting'", which is queue language
  applied to a possibly-refused design.
* No timeout or bound on waiting: a draining grade can run up to ~10
  minutes for a large diff (per `docs/code-orchestrator.md`), and a
  queued install would wait that long with no stated timeout, no
  cancellation of the queue entry, and no restart behaviour for a queued
  (never-started) job.
* Cross-process single-flight is not solved by §6.5: a durable record
  without a file lock (never named) does not stop two dashboard
  processes from starting two pip runs. §6.4 admits the two-process
  case is "out of scope" and falls back to "whatever pip surfaces" —
  but §6.9's every-repo banner (B4) means the two-process case is the
  normal multi-repo case, not an edge case. The design cannot both
  promise every-repo visibility and declare cross-process contention
  out of scope.
* The ledger guard ("jobs refused because an install is running are not
  recorded") is trivially true if refusal happens in the route handler
  *before* `code_grade_runner.start` is called — `routes_code.py` only
  ledgers inside `_work` after a started job. The design frames it as a
  runner-level guard without saying where refusal is enforced (handler
  vs runner), so an implementer could build a runner guard that never
  fires because the handler already refused, or vice versa.

### M3 — §6.6 (lines 580–614) and §6.2 (lines 481–505): stop is defined per phase but its existence is deferred, and the taxonomy omits the stop failure modes

* §6.6 gives detailed per-phase stop rows, then says "whether a stop
  button exists at all is option set E. If option set E picks
  install-to-completion, this section's stop rows do not apply." The
  scoped spec therefore both defines and undefines stop. §4's claim
  that stop semantics are "answered by option set E and section 6.6"
  is, per B5, a deferral labelled as an answer.
* "Safe to retry" (§6.6, downloading phase) rests on the false panel
  premise in B6: a terminated pip run can leave a findable partial
  package, so "the panel still says 'not installed'" is not guaranteed.
* §6.2 has no row for the failure modes stop creates: a pip lock left
  behind, a partial wheel that makes the next run fail until the cache
  is cleared, or a terminated warm-up download that the HF cache does
  not resume. The design mentions cache clearing only as a hypothetical
  ("the failure taxonomy must then name cache clearing as the remedy"
  in the option-set-E discussion), not as a taxonomy row with a message
  and a retry rule. An implementer following §6.2 alone would have no
  message for the most likely post-stop failure.

### M4 — §6.8 (lines 632–655): the state matrix is incomplete and conflates the panel value with the install state

* Missing rows: (a) Enabled=yes with Backend=not installed and Install
  job=running / failed / stopped / interrupted — the matrix covers
  yes+not installed+none and yes+available+done, but not the failure
  states under an enabled gate, which is exactly when the gate's
  "records unavailable" behaviour matters; (b) Backend="restart needed"
  (§5.2's explicit restart message) — no row renders it, so the matrix
  cannot express §5.2's own outcome; (c) preflight-refused — is that
  "failed", "none", or a distinct state? §6.1 refusals have no matrix
  row; (d) queued install (§6.4's queue option) — no row.
* The "Backend" column is defined as "the panel value after section 5.1",
  but §5.1's panel value depends on unpicked option set A, and the
  "installed but not loadable" row uses a value that is neither layer 2
  nor layer 3. The matrix therefore renders states whose definitions
  live in an unpicked option set, while claiming to be the complete
  rendering contract.
* Second-click behaviour ("button is disabled and links there") is
  stated, but double-submit from two tabs is said to be "stopped by
  the single-flight guard" — in the cited code the guard is an
  in-memory lock per process (`BrowserInstallRunner._guard`,
  `CodeGradeRunner._guard`), so a second tab hitting a second process,
  or a second tab after a restart, is not stopped by it. The matrix
  promises a cross-tab guarantee the cited mechanism does not provide.

### M5 — §7.2 (lines 735–764): the authorization story has three holes — tunnels, token-holder abuse, and an unsupported logging claim

* Loopback: "any local visitor can press Install" and the UI copy
  "Anyone on this machine can install the backend." But `docs/deploy.md`
  recommends remote access via SSH tunnel
  (`ssh -L 8765:127.0.0.1:8765 <host>`). A tunnelled remote user arrives
  on loopback and, under §7.2's rule, can trigger a multi-GB host
  mutation with no token. The design never addresses the tunnel case,
  which its own deploy doc makes the standard remote-access pattern.
  (The loopback Host check in `security.py` prevents DNS rebinding; it
  does not distinguish a local user from a tunnelled remote user.)
* Rate limiting: "one install at a time (single-flight) is the limit.
  No additional rate limit is specified … which bounds abuse to one
  pip process." Single-flight bounds *concurrency*, not *total* abuse:
  a token holder can loop install → finish → install indefinitely,
  churning disk, CPU, and network. The sentence claims a bound it does
  not provide.
* Logging: "every install attempt … is logged … with the requesting
  context the dashboard already records." No such recording exists in
  the code read: `security.py`'s middleware sets session/CSRF cookies
  and checks tokens but records no per-request audit context, and
  `core.py`'s flash system is per-session UI messaging, not an audit
  log. §6.10's "attempt events" are a new store the design never
  locates (see M11), so the §7.2 logging claim leans on a practice
  that is not in evidence.

### M6 — §5.2 step 3 / §6.3 (lines 506–524): warm-up has no mechanism, and HF cache persistence across processes is unspecified

* There is no "download the checkpoint without grading" function in the
  cited code (the brief itself says so: "the only fetch path is a real
  grade"). §6.3's warm-up phase ("the checkpoint download, with bytes
  downloaded when the download reports them") never names the API that
  performs a download-only fetch (e.g. a Hub download call) versus
  `laya.load`, which loads the model into memory. If warm-up *is*
  `laya.load` in a subprocess, it is a full 2–3 GB model load, not a
  download, and its cost/time profile is that of a grade, not of a
  download — the design's time/size honesty (§5.3) then understates it.
* §5.4 says `$HF_HOME` "must be set explicitly … before any checkpoint
  download" for service accounts, but never says who sets it, to what
  path, or how it persists for later grades. An install subprocess can
  set `$HF_HOME` for itself; the dashboard process that later grades
  (a different process, possibly after restart) will not inherit it.
  A preflight pass in the install job therefore does not guarantee the
  grade can find the cache — the design checks writability at install
  time for a location the grade process may never be told about.

### M7 — §2 (lines 78–160): several "what exists today" claims are inaccurate against the cited code

* "`available` checks only the first two [layers]" (§4 bullet, repeated
  in §2's framing): `CodeOrchestrator.available` returns True if a
  backend is already loaded, else if `load_backend` succeeds — and
  `load_backend` succeeds on `find_spec("laya")` alone, without importing
  torch and without constructing anything that touches torch. That is
  layer 1 only, not layers 1–2. The design's own §4 presence bullet
  ("still flips a `find_spec` check") contradicts the "first two"
  wording in the next bullet.
* Backend loading is described as "`load_backend` … and
  `LayaBackend._agent_or_load` … which calls `laya.load(model_id)`",
  omitting `load_backend`'s first branch: it first tries
  `importlib.import_module("coding_decision_agent")` and prefers that
  package if present. The design's fixed-argv and probe reasoning
  assumes the `laya` path is the only path; a future
  `coding_decision_agent` install would change what "installed" means
  without the design noticing.
* "On the Mac used for live verification, `jev` is installed editable
  from the checkout via `uv tool install`" conflates two different
  rows of §5.4's own matrix (editable checkout vs uv tool install).
  `uv tool install` creates an isolated tool environment; calling it
  "editable from the checkout" is at best imprecise and at worst the
  wrong branch-1 assumption for the Mac row.
* §2 cites `src/jev_agent/dashboard/app.py` for the route list, but
  `app.py` is not among the code files this review was permitted to
  read, and `routes_code.py` alone shows handlers, not URL mappings.
  The route list in §2 is therefore cited to a file outside the design's
  own evidence set. Similarly, the Settings toggle ("Settings → Code
  orchestrator") is asserted without any cited settings file.
* §2 says the browser precedent's "log is capped at 256 KB when the
  install finishes" — correct (`_MAX_LOG_BYTES = 262_144`) — but §6.10
  then reuses that cap for the install's "full log … retained", which
  contradicts §6.5's "full log is retained beyond the 30-line tail"
  (see M11). The precedent is cited accurately; its reuse is not.

### M8 — No timeouts anywhere in §§6.1–6.6 or §6.11

* Pip over a slow connection, the verification probe (a torch import +
  possible model load can take minutes and can hang on a corrupt cache),
  the warm-up download, and the preflight network checks have no
  timeout, no hang detection, and no stated behaviour on timeout in
  §6.2's taxonomy. A hung pip process is indistinguishable, in §6.5's
  terms, from a long legitimate install, and §6.11's shutdown contract
  never states the install runner's shutdown timeout (contrast
  `core.py:shutdown_jobs`, which passes a timeout to launcher/chat/
  scheduler shutdowns but calls `browser_installer.shutdown()` and
  `code_grade_runner.shutdown()` with their own 30 s defaults). A
  multi-GB install that hangs past shutdown has no defined exit in
  this spec.

### M9 — Windows temp and service-account cache handling are glossed, not specified

* §5.3: "On Windows the equivalent is a temp directory on a real disk,
  not a RAM disk." The design names `TMPDIR` as the mechanism for
  "every platform", but never addresses the Windows temp variables
  (`TEMP`/`TMP`) or which one Python's tempfile / the HF cache
  reconstruction actually honours on Windows. A spec that names one
  env var for all platforms, then waves at a Windows "equivalent"
  without naming it, has not specified the Windows temp behaviour —
  and the temp-dir failure ("No space left on device") is one of the
  design's own headline failure modes.
* §5.4's service-account paragraph assumes "`$HF_HOME` can be pointed
  at a writable directory" owned by the service, but never says how
  that directory is created, who owns it, or what happens when the
  service account cannot create one (branch 2? branch 3? preflight
  refusal with which message?). The Windows service row inherits all
  of this vagueness.

### M10 — §6.3 (lines 506–524): pip progress is not as observable as the phase model assumes

* "Downloading and installing — the log tail (last 30 lines, as the
  browser precedent), elapsed time, and the current pip line." Pip's
  progress output is carriage-return-updated, not line-based, and is
  buffered when stdout is a file (as in `browser_install.py`'s
  `_spawn`, which redirects to a log file). The "current pip line"
  may therefore be stale or absent for long stretches, and the
  30-line tail may show nothing new while gigabytes download. The
  design never addresses pip buffering or `\r` handling.
* No poll interval is stated for the install status surface, although
  the brief's checklist explicitly required "log tail/cap/poll policy"
  and the dashboard has a concrete refresh interval
  (`DashboardSettings.refresh_s = 3.0` in `security.py`). The phase
  boundaries themselves ("downloading" vs "unpacking" vs "installing")
  are not cleanly observable in pip output — the same observability
  problem the design itself flags for option set E3's phase-gated stop
  ("pip's phases are not cleanly observable") applies to §6.3's phase
  display, but §6.3 does not acknowledge it.

### M11 — §6.10 (lines 681–692): observability promises a store and a redaction practice that are not in evidence, and contradicts §6.5 on log retention

* "No secrets appear in the log or the events: pip output is stored
  as-is except that any credential-looking token in a URL is redacted
  before storage, following the dashboard's existing redaction
  practice for displayed tokens." No redaction practice appears in the
  code read — `security.py` has no redaction function, and
  `code_grade_runner.py` uses `friendly_error` rewrites, not URL
  redaction. "Credential-looking" is not a implementable pattern
  (which tokens? which URL positions? what replacement string?), and
  "stored as-is except redacted" is self-contradictory as a storage
  rule.
* §6.5 says "the full log is retained beyond the 30-line tail";
  §6.10 says the full log is "capped the same way the browser
  precedent caps its log, 256 KB, when the job finishes". A 256 KB
  cap *is* a truncation, not full retention — for a multi-GB torch
  install, pip output can exceed 256 KB, so the "full log for
  diagnosis" is not full. The design must pick one: capped tail
  archive, or genuinely retained log with a stated cap and rotation.
* The structured attempt events (start, phase change, finish, failure
  class, stop, interruption) have no named store, format, or retention
  — unlike the ledger, whose file (`<repo>/.jev/cda-ratings.jsonl`) is
  precisely specified in the cited code. An event stream without a
  store is not observability; it is a wish.

### M12 — §3 (lines 161–189) vs the value question: the goals are unconditional, but the feature's justification is deferred — scope is built before scope is decided

* §3 states unconditional goals: an Install button on the panel, the
  same endpoint on Mac/Windows/Linux/VPS, the pip instruction retired
  on all three surfaces. §4's final bullet (the value question) says
  the design must justify why CDA crosses the discover-don't-install
  boundary, "or the cheaper fits … may be the right scope", and
  defers that question to option set C — where one option (guide only,
  no execution) would make most of §§5–6 unnecessary. A spec whose
  goals already commit to the button, while its own critique section
  says the button's scope may be the wrong answer, is building the
  mechanism before deciding the scope. Under the standard's
  Alternatives Rule, the trade-off (button vs narrowed button vs guide
  only) must be *evaluated in the spec*, not merely listed as an
  unpicked option elsewhere: the scoped range evaluates none of them.
  The cost of this inversion is concrete — §§6.5, 6.9, 6.10 (durable
  record, cross-repo banner, structured events) are substantial
  infrastructure for a feature whose measured value signal is, by the
  design's own numbers, advisory-only (nothing cleared 0.7 across 13
  grades).

---

## Nits

### N1 — §5.3 (lines 333–372): "honest sizes" are incomplete

* The CPU wheel size is never given (only "the CPU wheel size for the
  CPU path"), `laya` plus transformers is "the size pip reports …
  or 'a few hundred MB' when it cannot be known in advance", and temp
  staging is "at least 1 GB" with no source. The section never sums
  its own numbers into the one figure the confirm step needs ("you
  need about X GB free"). "A few hundred MB" is not an honest size;
  it is a placeholder in a section whose purpose is honesty.

### N2 — State vocabulary drifts across §§6.5–6.8 without a table

* Browser runner states: `idle | running | done | failed | stopped`
  (`browser_install.py`); grade runner: `idle | running | done |
  failed | cancelled` plus `draining` (`code_grade_runner.py`); install
  design adds `interrupted`, `verifying`, and phase names, while §6.8
  uses `stopped` for installs and §6.5 uses `interrupted` for
  restart-killed jobs and §6.11 uses `interrupted` for shutdown-killed
  jobs (two different causes, one state word). The brief's Nit 1 asked
  for one vocabulary or an explicit rejection of one; the scoped spec
  provides neither — it just adds a third dialect.

### N3 — "One-click" in the title (§1) vs the confirm reality in §5.3/§7.2

* Title and §3 promise a one-click install; §5.3 hedges sizes as
  "(the confirm step, or the button's detail text when there is no
  confirm step)", and §10 (outside scope) counts "one click (plus a
  confirm click if option set H picks one)". If the confirm step is
  picked, the feature is two-click by construction. The title should
  not promise what an unpicked option set may negate.

### N4 — §6.2 taxonomy lumps distinct failures and omits named ones

* "Process killed (out of memory, host shutdown)" lumps OOM-kill during
  pip, OOM-kill during the verification probe, and host shutdown into
  one row with one message and one retry rule, although the remedy
  differs (probe OOM means the machine cannot run the backend at all,
  not merely that the install was interrupted). There is no row for
  wrong-torch-variant (detection error), corrupt checkpoint cache, or
  probe timeout (see M8) — all failure classes the design's own §§5.3
  and 5.5 imply exist.

### N5 — §5.4's example path contradicts §6.9's location rule

* Branch 2's example location is `<state>/.jev/cda-venv` — but §6.9
  forbids job state under "any repo's `.jev/` directory" precisely
  because `.jev/` is repo-scoped in the cited code. Using a
  `.jev/`-suffixed path as the example for the one non-repo location
  invites the implementer to put the venv exactly where the design
  says machine-wide state must not go. Name the state root first,
  then give the example under it.

### N6 — Chronology in the header (lines 3–11)

* The design is dated 2026-10-29 and lists as an input the concepts
  brief's "brainstorm, six Goldfish lenses, 2026-10-30" — a day *after*
  the design's own date. Either the design date or the input date is
  wrong; a spec whose inputs postdate it cannot have been written from
  them. (Also: the header says review reports live in `review/` with a
  `resolution_report.md` "records how each finding was handled" — for a
  design-phase document, that report cannot yet exist for this review's
  findings; the header describes a future state as current.)

---

## What the scoped spec gets right (brief, so the critique stays honest)

* §4's framing — "a blocker with no answering section is a gap, not a
  deferred detail" — is the right rule; this report's central complaint
  is that §4 then violates its own rule (B5).
* The fixed-argv principle (§7.1: argv list, never a shell, no request
  field) and the PEP 668 stance (§5.4: never `--break-system-packages`)
  are correctly decided in-spec rather than deferred, and the
  `jev-agent`-never-in-argv test requirement is the right shape of
  check — it is the rest of the fixed argv (torch variant, version
  policy) that is left open (B2).
* §6.3's refusal to invent a percentage is correct, and §5.1's
  "must never show 'installed' on layer 1 alone" is the right
  invariant — the report's complaint is that no mechanism in the
  scoped spec enforces it (B1, B6), not that the invariant is wrong.
