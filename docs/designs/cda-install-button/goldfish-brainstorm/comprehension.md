# Comprehension Goldfish — BRAINSTORM

- **Mode:** BRAINSTORM (concept ideation, comprehension lens)
- **Target brief:** `~/workspace/baton-cda-install/concepts_brief.md` ("CDA install button", 2026-10-29, tracking issue Gane2506/baton#366)
- **Sources judged:** only the brief and the listed code/docs:
  - `src/jev_agent/dashboard/templates/code_grade.html`
  - `src/jev_agent/dashboard/routes_code.py`
  - `src/jev_agent/dashboard/browser_install.py`
  - `src/jev_agent/dashboard/code_grade_runner.py`
  - `src/jev_agent/code_orchestrator/backend.py`
  - `docs/code-orchestrator.md`
- **Reader stance:** a fresh reader with no prior Baton context. Nothing is assumed known unless the brief itself defines it.
- **Method note:** the persona file for this lens says the reviewer is strictly read-only. The parent task for this review explicitly orders one output file, this report, so this file is the single exception to that persona line. No source file was edited.

## 1. Restatement of the concept, in my own words

As far as the brief lets a fresh reader understand it, the concept is this:

> Today, the Code Grade page (`/code-grade`) tells a user whose grading model is absent to open a terminal and run a `pip install` command. Instead, the same page should show an Install button. Clicking it should make the dashboard's server — not the user's browser — install something, in the background, while the browser shows progress and then checks that the install worked. The same button is supposed to work whether the dashboard runs on a Mac, on Windows, on Linux, or on a VPS. Only one install should run at a time. The command that runs must be fixed on the server so a browser request cannot change it into something else. If the install fails, the page should say so in plain words and offer a retry. And nothing about this change should make the page invent a grade when the grading backend is still missing.

Places where even this restatement had to guess are marked `[guess]` below. A restatement that needs guesses is itself a comprehension failure: the brief does not pin down what is being restated.

- `[guess]` What exactly gets installed. The brief uses "the backend", "laya plus torch", "the code-orchestrator extra", and "the ~842 MB model checkpoint" in overlapping ways, without ever listing the install as a set of artifacts. A reader cannot tell whether "installed" means (a) Python packages present, (b) packages plus the model weights downloaded, or (c) packages plus weights plus a proven ability to grade.
- `[guess]` Where it gets installed. "The dashboard's own environment" is the brief's central phrase and is never defined. It might mean the Python that launched the dashboard, a virtual environment, a user-level package directory, or the inside of a Docker container. Each of those gives a different answer to "did the install work, and will it survive a restart?"
- `[guess]` What the user sees, and where. "Progress in the browser" and "a log tail visible in the browser" do not say which page shows them (the Code Grade page itself, a new page, or the existing `/code-grade/status` page), whether closing the tab loses the progress view, or what happens to the button while the install runs.
- `[guess]` What "done" unlocks. The brief leaves open whether installing also turns grading on, and whether the user can grade from the page immediately, after a dashboard restart, or only after also changing a setting. A reader cannot draw the end state of the flow.
- `[guess]` What "one click" means. The overview says "one click installs", but the browser-install precedent the brief cites asks for confirmation first, naming the exact command. That is at least two clicks, and the brief lists "confirm-first or direct install" as an open decision. The headline and the detail do not agree.

If two readers produce different versions of the paragraph above, they are not being careless. The brief gives them room to differ, and Section 4 lists the sentences where that happens.

## 2. Findings

Format follows the persona's BRAINSTORM output: severity, dimension, friction, requirement. "Ergonomic requirement" here means: what the draft must define before a fresh reader can understand the concept, not a design decision being pre-made.

### Blocker 1 — "Backend" and "installed" are never defined, and the brief uses them for three different things

- **Severity:** Blocker
- **Dimension:** UX & Developer Ergonomics — the core noun of the whole concept is ambiguous.
- **Usability friction:** The brief says the backend is "missing", "already loaded or loadable", "installed server-side", and "verified via `CodeOrchestrator.available`". The listed code shows at least three distinct artifacts behind that one word:
  1. Python packages (`laya`, plus `torch` pulled in with it) — what `pip install "jev-agent[code-orchestrator]"` provides;
  2. the ~842 MB model checkpoint, which the docs say downloads separately, on the first grade, into the Hugging Face cache — not by pip;
  3. a loadable runtime in the running dashboard process.
  
  `backend.py` confirms the split: `load_backend` checks for a `coding_decision_agent` package, else checks `find_spec("laya")`, and states explicitly that neither branch downloads weights. So `available` can return true while the checkpoint is still absent and the first grade still faces a large download. A reader who takes "verification via `available`" at face value will believe "installed" means "ready to grade". The brief itself flags the pip-vs-checkpoint question as open decision 2, but then its goal section still says verification happens via `available` and the panel "flips to installed". Those two passages cannot both be taken literally.
- **Ergonomic requirement:** The draft must define, in one place and in plain words: what a "backend" is; what "installed" means (packages only, or packages plus checkpoint pre-warmed); and what `available` does and does not prove. If "installed" means packages only, the draft must say what the user is told about the still-pending first-grade download. If it means packages plus checkpoint, the draft must say by what mechanism the checkpoint is fetched, since pip alone does not fetch it.

### Blocker 2 — "The dashboard's own environment" is the load-bearing phrase and is undefined

- **Severity:** Blocker
- **Dimension:** UX & Developer Ergonomics — the install target is ambiguous.
- **Usability friction:** The brief says the install goes "into the dashboard's own environment", then lists a venv (Mac `~/jev-venv`), a system Python, and a service account (VPS, Windows service) as possibilities, and suggests `sys.executable -m pip` as "the sane default" with a `--user` fallback or a dedicated venv. A fresh reader cannot answer:
  - How does the dashboard know which environment is "its own"? Is it always the Python running the dashboard process, or can it be something else?
  - If the fallback installs into `--user` or a dedicated venv, how does the running dashboard then see the backend? `available` in `backend.py` checks what the dashboard process itself can load (`find_spec("laya")` on its own path). The brief never explains how a package installed elsewhere becomes visible to that check. A reader can reasonably conclude the fallback installs successfully and the button still says "not installed".
  - What is a "system Python", what makes a Python "read-only", and how would a user recognise either? None of these terms is defined.
  - What is the dedicated venv's path, who creates it, and does it survive dashboard restarts and upgrades?
- **Ergonomic requirement:** The draft must name the target environment precisely, state how it is detected, state the fallback order and who chooses a fallback (automatic or user-chosen), and state how each fallback target is then detected by the availability check. If a fallback cannot be detected by that check, the draft must say so rather than listing it as a fallback.

### Blocker 3 — "Works on Mac, Windows, Linux, and VPS" collapses four different things, and the Docker answer is missing

- **Severity:** Blocker
- **Dimension:** UX & Developer Ergonomics — the platform claim cannot be evaluated as written.
- **Usability friction:** Mac, Windows, and Linux are operating systems. A VPS is a hosting arrangement, not an operating system; a VPS typically runs Linux, with or without Docker. The brief lists them side by side, then says "the Linux deploy runs Baton from a pinned Docker container" and "a pip install inside that container vanishes on the next container restart". A fresh reader cannot tell:
  - Whether every VPS install is a Docker install, or only one deploy mode is.
  - What a "pinned Docker container" is, what a "volume" would preserve, or what a "documented rebuild step" would ask the user to do. None of these is defined in the brief.
  - Whether "same endpoint on Mac, Windows, Linux, and VPS" means the same button, the same HTTP route, or the same server behaviour. "Endpoint" is used without definition, and the overview says "button" while the goal says "endpoint".
  - What is Windows-specific at all. The brief names a "Windows service" once and gives no Windows detail: no path, permission, or command difference. A reader cannot tell whether Windows is genuinely covered or just listed.
- **Ergonomic requirement:** The draft must give a platform-by-deployment matrix in plain words (operating system × how the dashboard was launched: foreground command, service, Docker container), and for each cell state whether the install persists, and if not, exactly what the user must repeat and when. The Docker persistence answer the brief calls the largest open risk must be an answer, not a restated question, before the "works on a VPS" sentence can be used.

### Blocker 4 — Installed, enabled, and gradeable are three states the brief blends together

- **Severity:** Blocker
- **Dimension:** UX & Developer Ergonomics — the end state of the flow is ambiguous.
- **Usability friction:** The brief says grading is off by default and the enable toggle lives under Settings → Code orchestrator. The template confirms the split: the status panel shows Enabled separately from Backend, and a hint says grading from the page "still works on demand" even when grading is off, while the gate will not grade every diff. The brief's open decision 4 (auto-enable grading, or leave it off) does not explain this three-way distinction to a fresh reader:
  - Installed but not enabled: can the user grade from the page on demand? (The template suggests yes, but the brief never says so.)
  - Installed and enabled: does the quality gate now grade every diff, and is that what the user consented to by clicking Install?
  - Not installed: the template still renders Grade and Route buttons. Does clicking them fail with the install guidance, or are they disabled? The brief never says what the grade/route forms do while the backend is missing or while an install runs.
  
  A reader can therefore finish the brief believing Install means "grading is now on everywhere", while another reader believes it means "only this page can now grade", and a third believes it means "nothing changes until Settings is visited". All three readings are compatible with the brief's words.
- **Ergonomic requirement:** The draft must define the post-install state explicitly: what works immediately after a successful install (on-demand page grading, gate grading, routing), what still requires the Settings toggle, and what the grade/route forms show and do in each of the missing / installing / installed-but-disabled states.

### Blocker 5 — Progress, location, and survival of the install job are unspecified

- **Severity:** Blocker
- **Dimension:** UX & Developer Ergonomics — the user-visible flow has no shape.
- **Usability friction:** The brief promises "progress and a log tail visible in the browser" but never says:
  - On which page. The grade runner precedent has a dedicated status page (`/code-grade/status`) whose live region polls; the browser-install precedent shows a log tail on the Connections page. The CDA install could live on the Code Grade page, on the existing status page, or on a new page. The template's job flash block only handles grade/route jobs (`job.kind`), so an install job has no described home in the current page.
  - What "progress" measures. Pip output lines? Named stages (downloading torch, installing, verifying)? A percentage? The brief never distinguishes a stream of log lines from staged progress, and gives no polling or refresh story for the install itself (unlike the grade runner, where the brief does describe polling).
  - Whether closing the tab, navigating away, or restarting the dashboard loses the install, the progress view, or neither. The grade runner's docstring promises the result waits on the status page; the brief makes no equivalent promise for installs, and `BrowserInstallRunner`'s lock is in-memory in the dashboard process, so a dashboard restart would end its single-flight state — the brief never addresses this.
  - What the button itself does while an install runs (disabled? replaced by progress? still clickable?). "One at a time" does not answer what the second click does: nothing, an error, or a view of the running job.
- **Ergonomic requirement:** The draft must walk the flow screen by screen: where the button sits, what replaces or accompanies it during the run, what progress means in concrete units, what survives tab close and dashboard restart, and what a second click or a page refresh shows.

### Blocker 6 — "One at a time" does not say what is limited: installs only, or installs plus grades

- **Severity:** Blocker
- **Dimension:** UX & Developer Ergonomics — the concurrency rule is ambiguous.
- **Usability friction:** The goal says "one at a time". The critique asks whether the install runner and the grade runner "share one lock or are independent". Both cannot be the reader's guide: "one at a time" most naturally reads as one install at a time (matching `BrowserInstallRunner`'s "at most one install at a time"), but the contention argument (CPU, memory, disk) points at install-vs-grade exclusion. The brief gives no numbers for install contention (download size, disk use, CPU use during pip/torch install) to compare with the grade numbers it does cite (multi-GB torch process, 2–3 GB RSS in the docs). A reader cannot tell whether starting a grade during an install is blocked, queued, warned about, or allowed, nor what message appears in each case. The same gap applies in reverse (install during a grade).
- **Ergonomic requirement:** The draft must state the lock scope in one sentence (installs only, or installs and grades together), state what the blocked action shows (disabled button, queue, or error text), and state whether the lock survives a dashboard restart.

### Blocker 7 — The "fixed command" is never stated, and the singular contradicts the install's actual steps

- **Severity:** Blocker
- **Dimension:** UX & Developer Ergonomics — the security-critical detail is asserted, not specified.
- **Usability friction:** The brief insists no request can change what gets installed, citing the browser-install precedent where the command is fixed server-side. But the brief never writes down the CDA install command. Candidates visible in the listed sources disagree:
  - the template and `code_grade_runner.py`: `pip install "jev-agent[code-orchestrator]"`;
  - `docs/code-orchestrator.md`: `pip install laya` as the whole install, with the extra described as "same thing";
  - the same doc's CPU-only guidance: install a CPU torch wheel first from a specific index URL, then install `laya` — that is two commands, not one.
  
  A confirmation dialog "naming the exact command" (the cited precedent) cannot name a command the brief has not fixed. A reader also cannot tell whether the fixed thing is a literal argv list, a shell string, or a sequence of steps, nor what the user-facing confirmation would show (`sys.executable -m pip ...` looks different from `pip install ...`, and the brief uses both phrasings). The security claim is therefore unverifiable from the brief alone.
- **Ergonomic requirement:** The draft must print the exact fixed command or command sequence, state whether a CPU-only variant changes it, and state exactly what confirmation text shows. If the sequence has more than one step, the draft must stop calling it "the command" in the singular.

### Blocker 8 — Stop semantics contradict themselves, and "safe, retryable state" is undefined

- **Severity:** Blocker
- **Dimension:** UX & Developer Ergonomics — the failure path is described with two incompatible mechanics.
- **Usability friction:** The brief's critique says a stop button "that kills pip mid-torch-download needs the same honesty as grade cancel". But grade cancel and browser-install stop are opposites in the listed code: `BrowserInstallRunner.stop()` terminates the installer process, while `CodeGradeRunner.cancel()` explicitly cannot kill the model call, discards the result, and reports "draining" until the thread finishes. The brief never says which mechanic an install stop uses, and its open decision 6 ("stop button or install-to-completion only") presupposes the answer is still open while the critique writes as if a stop button exists. Separately, "a stopped install leaves a safe, retryable state" and "pip is idempotent per package" are asserted without definitions:
  - What is "safe"? Partially installed torch? A half-downloaded checkpoint? A pip lock left behind?
  - Does retry resume a download or restart it from scratch? "Idempotent per package" (re-running pip ends in the same installed set) is not the same as "resumable" (a half-downloaded file continues where it stopped). A fresh reader will conflate them.
  - What does the user see after a stop: the pre-install "not installed" state, a distinct "stopped" state, or a warning that something partial remains?
- **Ergonomic requirement:** The draft must pick one stop mechanic, define "safe" as a checkable state (what is present, what is partial, what retry does about each), and state the exact post-stop message. If there is no stop button, the critique's stop paragraph must be removed rather than left as a dangling scenario.

### Blocker 9 — Who is allowed to click Install is never mentioned

- **Severity:** Blocker
- **Dimension:** UX & Developer Ergonomics — an install button on a server is an authorization question, and the brief answers a different question.
- **Usability friction:** The brief's security paragraph addresses command injection (a request must not choose the package). It never addresses who may trigger the install at all. The dashboard is a server: on a local Mac the browser user and the server owner are usually the same person, but on a VPS they may not be. A fresh reader cannot tell whether any browser visitor who can reach the Code Grade page can run a multi-gigabyte pip install as the dashboard's server user, or whether the button is restricted, or whether the dashboard is assumed local-only. The listed code shows CSRF-checked forms for grade/route actions, but the brief does not say the install endpoint inherits any check, nor what happens on an unauthenticated or unprivileged request. The fixed-command rule does not settle this: a fixed command run by the wrong person is still a server-side install.
- **Ergonomic requirement:** The draft must state the authorization assumption explicitly (local-only dashboard, authenticated user, or any page visitor) and what the button shows to someone not allowed to use it. If the dashboard has no auth concept, the draft must say that plainly instead of leaving the question unasked.

### Blocker 10 — "Plain English failures with retry" has no failure list, and the prerequisites that cause failures are absent

- **Severity:** Major (borderline Blocker for a VPS reader; kept at Major only because the brief at least names the requirement)
- **Dimension:** UX & Developer Ergonomics — error behaviour is a promise without content.
- **Usability friction:** The brief requires failures "in plain English with retry" but gives no examples and no taxonomy. The listed docs name concrete failure causes the brief never mentions, so a fresh reader cannot anticipate them:
  - disk space: the default torch wheel includes CUDA libraries (~2.5 GB in the docs) on top of the ~842 MB checkpoint; the brief's "~1 GB download" framing understates the total;
  - temporary-directory space: `laya.load` reconstructs the checkpoint through the temp dir, and the docs warn the default `/tmp` (512 MB tmpfs on the dev VM) is smaller than the model, so a first load fails with "No space left on device" unless TMPDIR points at home disk;
  - checkpoint location and offline behaviour: the Hugging Face cache (`~/.cache/huggingface/hub`, or `$HF_HOME`), and `HF_HUB_OFFLINE=1` after the first download — none of this appears in the brief, so a reader cannot tell where the checkpoint goes or whether a later grade needs the network;
  - CPU-only torch: the two-step CPU wheel install is absent from the brief, so a CPU-only reader cannot tell whether the button will pull the large CUDA wheel;
  - hardware fit: 2–3 GB RSS for torch plus weights, and "if that does not fit, keep the feature off" — the brief never states a memory or disk precondition, so "retry" after a resource failure reads as if retry alone fixes it.
  
  The brief also never maps any pip failure (network, permission, disk full, package not found, interrupted download) to a sentence, nor says which failures are retryable and which need a human action (free disk, fix permissions, rebuild a container).
- **Ergonomic requirement:** The draft must list the failure classes the button handles, give one example plain-English sentence per class, state which are retryable by button alone, and state the disk, memory, network, and temp-directory preconditions before the button is offered — or state explicitly that no precondition check exists.

### Major 1 — "Without a restart if possible" is circular, and the restart itself is undefined

- **Severity:** Major
- **Dimension:** UX & Developer Ergonomics — a conditional promise with no test.
- **Usability friction:** The goal says the panel "flips to installed without a restart if possible, or says a restart is needed if it is not". That sentence gives no criterion for "possible": what is checked, and who decides? It also never says what restarts (the dashboard process, the browser tab, the machine), nor how a user restarts a dashboard running as a service, a Docker container, or a VPS process. The listed code cuts both ways and the brief does not resolve it: the status panel rebuilds a fresh `CodeOrchestrator` per page load (`routes_code._status`), which suggests a re-check could see a new package without a restart, but a running grade thread or a cached backend object might not — the brief does not discuss either. A reader cannot tell whether "restart" is a rare edge or the normal path.
- **Ergonomic requirement:** The draft must state the restart rule as a testable condition (e.g. what is re-checked and when), name exactly what must restart, and give the restart instruction per deployment (or state that no restart path exists in the UI for some deployments).

### Major 2 — "Shown exactly when the backend is missing" does not match what `available` reports

- **Severity:** Major
- **Dimension:** UX & Developer Ergonomics — button visibility rule is imprecise.
- **Usability friction:** `available` returns false not only when nothing is installed, but also when a backend is present yet fails to load (`load_backend` raises `CodeOrchestratorUnavailable` for a load failure too, and `_call` raises it when an installed backend lacks the expected method). The brief's "exactly when the backend is missing" therefore misdescribes the trigger: the button would also show for a present-but-broken backend, where re-running pip may not be the fix. The brief also never says whether the button replaces the pip-command text or sits alongside it, and whether the old pip text remains as a fallback for users who prefer the terminal.
- **Ergonomic requirement:** The draft must define button visibility from the actual check (missing vs unloadable vs loadable), and state what happens to the existing pip instruction text.

### Major 3 — The two "precedents" have opposite mechanics, and the brief merges them

- **Severity:** Major
- **Dimension:** UX & Developer Ergonomics — the reader's mental model of a background job will be wrong.
- **Usability friction:** `BrowserInstallRunner` spawns a subprocess, streams output to a log file under `.jev/`, caps that log, and can terminate the process. `CodeGradeRunner` runs an in-process thread, holds its result in memory, and cannot interrupt the model call. The brief calls both "background-job precedents" and then asks whether the CDA install mirrors one or generalizes the other (open decision 1) — but its prose already borrows features across them without attribution: "output streams to a log file" (browser-install only), "live region polls the job" (grade only), "stop" (killable in one, draining in the other). A fresh reader will assemble a single imaginary runner that streams to a file, polls in memory, kills on stop, and drains on cancel. Jargon compounds this: "live region", "drains", "single-flight", and "log tail" are used without definitions.
- **Ergonomic requirement:** The draft must describe the CDA runner's mechanism in its own paragraph (process vs thread, where output lives, how polling works, what stop/cancel does) instead of pointing at two precedents and leaving the reader to merge them.

### Major 4 — Sizes, times, and the total cost are understated and scattered

- **Severity:** Major
- **Dimension:** UX & Developer Ergonomics — the user cannot judge the wait they are consenting to.
- **Usability friction:** The brief gives "~842 MB" and "multi-minute first grade" and "~1 GB download" in different places. The listed docs give a larger picture the brief never totals: torch's default wheel adds ~2.5 GB of CUDA libraries; model load is ~50 s; a small-diff grade is 1–3 minutes and a large one up to ~10 minutes; torch plus weights run at ~2–3 GB RSS. The brief never states the pip install's own duration (download-bound, network-dependent), nor the combined first-use time (pip download + checkpoint download + model load + grade). A confirmation dialog that names "the exact command" but not the expected size and time does not give informed consent, and a progress display without a scale cannot distinguish "slow" from "stuck".
- **Ergonomic requirement:** The draft must state the expected download size and time range for each stage (packages, checkpoint, first grade) on a stated assumption (e.g. CPU-only vs default wheel), and state what the UI shows when a stage exceeds that range.

### Major 5 — The brief contradicts itself on scope in at least three places

- **Severity:** Major
- **Dimension:** UX & Developer Ergonomics — self-negating sentences force the reader to pick a meaning.
- **Usability friction:**
  1. "One click installs" (overview) vs "confirm-first naming the exact command" (precedent) vs "confirm-first or direct install" (open decision 6). Three click-counts for one button.
  2. "No change to grading semantics: … off-by-default unchanged unless the design explicitly decides otherwise." The exception swallows the promise: a reader cannot tell whether off-by-default is a constraint or a suggestion.
  3. "Installing anything other than the code-orchestrator extra" is out of scope, while the CPU-only path in the docs installs a torch wheel first as a separate step. A reader cannot tell whether that torch step is inside or outside scope.
  4. "Same endpoint" (goal) vs "same button" (overview/task framing). An endpoint and a button are different things; the brief uses them interchangeably.
- **Ergonomic requirement:** The draft must resolve each contradiction to one sentence and delete the losing sentence, rather than carrying both forward as options without marking which one the overview's claims depend on.

### Major 6 — Per-repo page vs machine-wide install: scope is unstated

- **Severity:** Major
- **Dimension:** UX & Developer Ergonomics — the reader cannot tell what one click changes.
- **Usability friction:** The button sits on a per-repo Code Grade page (the status is built from that repo's config: `repo.config.code_orchestrator`, including its `model_id`), and the log precedent writes under that repo's `.jev/` directory. But a pip install targets a Python environment, which is machine-wide (or venv-wide), not repo-wide. The brief never says whether installing from repo A's page also installs for repo B, whether the log lives per repo or per dashboard, or what happens if two repos configure different model IDs. `.jev/` itself is never defined for a fresh reader (a per-repo hidden directory holding dashboard state, per the docs' ledger discussion — but the brief does not say so).
- **Ergonomic requirement:** The draft must state the install's scope (machine, venv, or repo), where the log lives, and what happens across repos and model IDs.

### Nit 1 — Status-panel fields are listed, not explained

- **Severity:** Nit
- **Dimension:** UX & Developer Ergonomics — a fresh reader sees labels without meanings.
- **Usability friction:** Enabled, Model, Confidence threshold, and Backend are listed as what the panel renders. "Model" (a Hugging Face model ID), "Confidence threshold" (0.7, gating merge-action confidence and routing hints, per the docs — never on labels alone), and "Enabled" (gate grading vs on-demand grading) are never defined in the brief. A reader who does not already know the Coding Decision Agent cannot interpret the panel the button sits on.
- **Ergonomic requirement:** One plain sentence per field in the draft's overview, or a link to the doc section that defines them.

### Nit 2 — Unexplained terms and assumed navigation

- **Severity:** Nit
- **Dimension:** UX & Developer Ergonomics — small gaps that accumulate into "I must already know this project".
- **Usability friction:** Undefined or assumed in the brief: Coding Decision Agent (what it grades/routes, in one sentence); quality gate (what it gates and what "gate grading" means); `laya` (a runtime package, not a vote — the docs' Laya-removal section exists precisely because the name confuses readers, and the brief never disambiguates it); `torch` (the ML runtime, and why it is large); Hugging Face cache; `pip`, `venv`, `--user`, `sys.executable` (assumed Python knowledge — acceptable for a developer tool only if the brief says its audience is developers, which it does not); "VPS"; "service account"; "pinned container"; "live region"; "drains"; "single-flight"; "endpoint"; "log tail". Navigation is also assumed: "Settings → Code orchestrator" with no statement of where Settings lives or whether the install button appears there too. The tracking issue is cited as a number with no link or summary.
- **Ergonomic requirement:** A short glossary or inline definitions on first use for every term above, plus one sentence stating the intended audience (terminal-free end user vs developer-operator) — because "plain English" means something different for each.

### Nit 3 — The brief asserts precedent details the listed code does not show

- **Severity:** Nit
- **Dimension:** UX & Developer Ergonomics — a fresh reader cannot verify the brief from its own citations.
- **Usability friction:** The brief says the browser-install "button asks for confirmation first, naming the exact command". `browser_install.py` contains the runner and the fixed `install_argv()` but no button and no confirmation UI (that lives in a template the brief does not list). Likewise "the page shows the log tail" and "the status page whose live region polls the job" are UI claims whose templates are not in the listed set (except the grade page, which shows no install UI). This does not make the claims false, but it means the brief is not self-verifying: a reader checking the citations finds runners, not buttons.
- **Ergonomic requirement:** Cite the template or route that actually renders each claimed UI element, or mark the claim as describing intended behaviour rather than existing behaviour.

## 3. What a reader needs to know that the brief never states

These are not design opinions. Each item is a fact present in the listed code or docs that the brief omits, and each one changes how a reader understands the install:

1. **What the Coding Decision Agent does.** Grades a diff (code quality 0–4, instruction followed, likely correct, merge action) and routes a task (model tier, skill, difficulty), gated on calibrated confidence at 0.7. The brief mentions "grading" and "threshold" without ever saying what is graded or what the threshold gates.
2. **What "no guessed grade" means mechanically.** A missing backend raises `CodeOrchestratorUnavailable`; the page shows install guidance instead of a grade, and the quality gate records "unavailable" and proceeds rather than blocking. The brief states the rule but not the mechanism, so a reader cannot tell a missing backend apart from a failed grade (which blocks fail-closed at the gate — a different outcome the brief never mentions).
3. **The checkpoint is lazy and separate.** Neither pip install nor the availability check downloads weights; the first grade downloads ~842 MB into the Hugging Face cache. Any "installed" definition that ignores this will mislead.
4. **The CPU-torch two-step and the CUDA size.** Default torch pulls ~2.5 GB of CUDA libraries; CPU-only machines need a CPU wheel first. The brief's fixed single command does not accommodate this without comment.
5. **TMPDIR and temp space.** Checkpoint reconstruction goes through the temp dir; a small `/tmp` fails the first load. An install button that succeeds at pip and then leaves the first grade failing on temp space has not finished the user's journey.
6. **Memory and time budgets.** ~2–3 GB RSS; load ~50 s; grades 1–3 minutes (small) to ~10 minutes (large). These numbers belong next to any progress or consent story.
7. **The log precedent's concrete shape.** `BrowserInstallRunner` writes a fresh log per install under `.jev/`, shows the last 30 lines, reads only the last 64 KiB on poll, and caps a finished log at 256 KiB. The brief says "log tail" without carrying over any of these specifics, so a reader cannot tell whether the CDA install reuses them.
8. **The grade runner's concrete cancel semantics.** Cancel discards the result; the thread runs to completion ("draining"); a new grade cannot start while the old thread lives. The brief summarises this as "the runner drains" without defining draining, and then borrows the word for installs where the mechanic may differ.
9. **The current page's exact wording.** The template says "not installed on this machine" and gives the pip command plus the checkpoint note and a pointer to `docs/code-orchestrator.md`. The brief paraphrases this but never quotes it, so a reader cannot tell what text the button replaces and what (if anything) remains.
10. **The existing grade/route error path.** A grade attempted without a backend fails with the install guidance as its error text (`unavailable_message()`), not a traceback. The brief never says whether the Install button changes that path or leaves it as the fallback.

## 4. Sentences two reasonable readers can interpret differently

| Brief sentence (paraphrased closely; see brief for exact wording) | Reading A | Reading B |
|---|---|---|
| "One click installs the backend server-side, in the background, in the dashboard's own environment" | Click once; the server installs packages into the Python running the dashboard; a progress view appears. | Click once, then confirm; the server installs packages plus weights into some environment the dashboard picks (venv, user site, or container); progress appears somewhere unspecified. |
| "The same button must work on every machine the dashboard runs on — Mac, Windows, Linux, and a VPS" | The button's behaviour and persistence are identical on all four. | The button is present on all four, but persistence and restart steps differ per deployment (especially Docker). |
| "Server-side background install into the dashboard's own environment, one at a time" | One install at a time, grades unaffected. | One job at a time across installs and grades (shared lock). |
| "Verification after install via `CodeOrchestrator.available`; the status panel flips to installed without a restart if possible" | After pip succeeds, the panel flips; restart is an unusual fallback. | After pip succeeds, the panel flips only if the running process can see the new packages; otherwise the user must restart something (unspecified) first. |
| "Failure states in plain English with retry; a stopped install leaves a safe, retryable state" | Any failure or stop can be retried from the button and will succeed or explain itself. | Retry re-runs pip from scratch; partial torch/checkpoint downloads may or may not resume; "safe" is a property the UI asserts, not a state the brief defines. |
| "No change to grading semantics: … off-by-default unchanged unless the design explicitly decides otherwise" | Off-by-default is frozen. | Off-by-default is open to the draft's decision. |
| "Shown exactly when the backend is missing" | Button appears only when no packages are present. | Button appears whenever `available` is false, including present-but-broken backends. |
| "The install runs where the dashboard server runs, not in the browser" | On a remote VPS, the install happens on the VPS, not the user's laptop. | On a local Mac dashboard, server and browser are the same machine, so the distinction is moot — and the brief never says which case the reader is in. |
| "A general package manager for the dashboard" is out of scope | Only this one extra may ever be installed this way. | Only this button's scope is limited; a future general installer is a separate question the brief does not foreclose (the wording does not distinguish). |
| "Automatic updates of an installed backend" is out of scope | After install, the button never reappears and versions never change. | After install, a future version bump or broken install has no defined path back to the button. |

## 5. What the draft must define before this concept is understandable

In order, as comprehension requirements (not design picks):

1. A glossary: backend, installed, environment, grade, route, threshold, gate, laya, torch, checkpoint, VPS, service account, volume, endpoint, single-flight, log tail, draining.
2. The install artifact list and the definition of "installed" (packages vs packages-plus-checkpoint), and what `available` proves.
3. The target environment, its detection, the fallback order, and how each fallback is detected by the availability check.
4. The platform-by-deployment matrix, with the Docker persistence answer stated as a fact.
5. The screen-by-screen flow: button location, confirmation (or not), progress units and page, log location, second-click behaviour, tab-close and dashboard-restart survival.
6. The lock scope (installs only vs installs-plus-grades) and the blocked-action message.
7. The exact fixed command or sequence, and the exact confirmation text.
8. The stop mechanic (kill vs drain vs none) and the checkable definition of "safe, retryable".
9. The authorization assumption: who may click.
10. The failure taxonomy with one example plain-English sentence per class, plus disk/memory/network/temp preconditions.
11. The restart rule as a testable condition, naming what restarts and how on each deployment.
12. The post-install state: what works immediately (on-demand grading, gate grading, routing), what still needs Settings, and what the grade/route forms do while missing or installing.
13. The button-visibility rule from the actual `available` semantics (missing vs unloadable), and the fate of the existing pip text.
14. The install's scope (machine vs venv vs repo), the log's location, and cross-repo/model-ID behaviour.
15. Total size and time per stage, so "progress" and "consent" have numbers behind them.

## 6. Bottom line for the orchestrator

A fresh reader can follow the brief's motivation and its list of risks, but cannot reconstruct the feature from it: the central nouns ("backend", "installed", "the dashboard's own environment") are undefined and overloaded, the headline flow ("one click", "same button everywhere", "flips without a restart if possible") contradicts or outruns the detail, and the failure, stop, platform-persistence, and authorization stories are named but not specified. The draft stage should treat Section 5 above as its definition checklist; until those items are answered in the draft itself, reviewers will keep producing different versions of the same feature from the same brief.
