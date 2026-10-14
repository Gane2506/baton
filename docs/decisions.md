# Design decisions

Deviations from [PLAN.md](../PLAN.md) and choices it left open. Newest first.

## 2026-10-14: `jev mcp runs`: pipeline tools for external agents (workstream K)

- **A second minimal MCP server, same wire protocol.** `src/jev_agent/mcp_runs.py`
  reuses `servers/mcp.py`'s `Tool`/`McpServer` (stdio JSON-RPC, no SDK) for `run_list`,
  `run_status`, `quality_show` (read-only) plus `run_start` and `chat_send` (mutating).
  The mutating tools are hidden unless `jev mcp runs --allow-mutations` — the same
  shape as `mcp servers` hiding its write tools without a medium grant — so the default
  server is read-only.
- **`run_start` creates but never advances.** Advancing would run worker agents
  (unbounded time and spend inside a tool call); the `[autonomy]` mode is recorded on
  the run and `jev resume` / `jev approve` move it forward, so the tool never approves
  a gate (in `ask_every_step` mode it just creates the run). This differs deliberately
  from `jev run`, which advances.
- **`chat_send` returns the model's reply**, unlike `app/chat.py::turn`'s one-line
  outcome, so the calling agent can continue the conversation (the chat is the driver
  behind a manual-mode run).
- **Audit.** Every tool call gets one redacted JSON line in `.jev/audit/mcp_runs.log`
  via the existing `AuditLog` (same 0600/0700 permissions and credential redaction as
  the servers log).
- **`app/runs.py::create_run` now takes `RepoContext | AppContext`** (it only ever used
  root/config/store/learning); the MCP server opens no workers or dispatcher, so it
  starts fast and needs no model access for reads.

## 2026-10-14: Deterministic 0-100 ship score on the Laya scale

- **No second scale.** The ship score reuses the six Laya criteria and the
  configured `[dispatcher.laya]` weights (total = weighted 1-5 sum x 20); the
  repo already encodes those weights, so inventing another rubric failed the
  simplicity gate.
- **Deterministic, no LLM judge.** Every signal is static (check statuses,
  finding severities, diff-size bands, missing tools); user preference scores a
  neutral 3/5, documented honestly. An LLM judge would route through the
  dispatcher anyway — static signals were enough for a gate.
- **The gate has the last word, including `jev ship`.** Below threshold, a fix
  task with the weak-criteria breakdown goes back to EXECUTING (bounded by
  `[quality] score_max_retries = 2`); retries spent halts with NEEDS_HUMAN.
  `jev ship`'s `require_quality_pass` checks the score too, so the manual path
  cannot bypass the gate. Human contributions can skip via
  `[quality] score_skip_human` (still scored and recorded, marked skipped).

## 2026-10-14: Preview browser ships as polling v1, agent seam left for later

- **Polling, not realtime, for the agent browser preview.** The pane polls
  `/preview/live` every 3 s and the agent pushes frames; true realtime
  (websocket/SSE) would need a new dependency and a new live contract beside the
  purpose-built `JevShell.mountPane`/live.js pattern. Worst-case staleness is one
  pane poll plus one agent push — acceptable for watching an agent work.
- **The agent-side seam is an API, not a wiring.** The browser is driven by the
  pinned Playwright MCP server inside the agent CLI process, which this repo does
  not drive from Python — so the dashboard exposes `POST /preview/agent/*`
  (register/frame/poll) and documents the contract in `dashboard/preview.py`
  instead of pretending to hook a loop it cannot reach. Wiring the agent side
  waits for a workstream that owns browser driving.
- **Screenshots only, never DOM** in the feed; takeover input is never echoed or
  audit-logged; one driver at a time (`agent`/`user`), enforced with 409s.

## 2026-10-13: Settings editor and a shared chat lock (#9)

- **A curated settings editor for the repo `jev.toml`** (jev-decide, AGREE). Only fields
  outside `GUARDED_KEYS` are editable, enforced on the server; editing every field or raw TOML
  failed the safety gate. `tomlkit` keeps comments; `load_config` was split so the editor
  validates a candidate file with the loader's own checks (`config_from_layers`). Repo/user
  target selection and search from #9 are left for later.
- **One process per chat** (from review). The dashboard's per-chat lock only covered its own
  process, so a terminal `jev chat --resume` and a dashboard reply could both load and save the
  same `meta.json`. `core.chat.hold_chat` takes an exclusive `flock` on the chat's `chat.lock`
  (the kernel drops it if the holder dies); a terminal holds it for its session, a dashboard
  turn for one turn. The second comer fails at once instead of waiting, so neither blocks.

## 2026-10-13: Screenshots from the running app, with Playwright (#83)

- **Playwright as a dev dependency**, not more Chrome command-line flags: the quality image is
  one section of the run page, and Playwright clips it by the section's own box instead of a
  hand-measured crop, waits for the network and fonts, and switches light/dark per page. It
  drives an installed Chrome, so CI needs no browser download.
- **CI uploads the captures, it does not diff them**: run ids, the repo path in the header and
  relative ages change on every capture, so a pixel comparison would fail on every run. The job
  still fails when the seed script or a page breaks, which is how the old capture path rotted
  unnoticed (#83).

## 2026-10-13: Tune Laya's weights from run outcomes (#69)

- **Outcomes are derived, not recorded** (jev-decide, Jev then Laya: both picked this over an
  outcome table, ARBITRATED at 0.74 and 0.84). The state DB already holds every decision with
  its run and task, the tasks' and runs' final states and the D5 answers, so `core/tuning.py`
  reads them when `jev laya tune` runs. A `decision_outcomes` table written by the pipeline
  would add a migration and new writes in the run loop without adding information. Rejected
  too: only printing a `jev.toml` snippet, which drops the issue's "the learning profile
  carries the weights".
- **What counts as passed.** D4 passes when the task's next D5 answer is `done`: the most direct
  signal of whether the assignment worked, and known mid-run. Other decisions wait for an ended
  run (DONE, FAILED, BUDGET_EXCEEDED): a task's by the task's state, a run-level one by the
  run's. Cancelled runs and runs waiting for a person are unknown, not failed: a person stopped
  them, not the work. D7 decides after the outcome and is never counted.
- **The rule is simple on purpose.** Per criterion, the gap between the chosen answers' mean
  score in passed and in failed work, applied as a bounded multiplicative step (e^(0.25 x gap),
  at most +-50% per tune), then renormalised. It is explainable in one table, needs no
  dependency, and cannot swing the matrix on a handful of runs (5 passed and 5 failed samples
  at least). A regression over all six criteria was rejected: with tens of correlated samples
  it overfits and its weights are hard to explain.
- **Never silent.** `tune` only shows; `--apply` saves on a learning profile, and only runs that
  learn with that profile use them. Weights in `jev.toml` win, and `--apply` refuses while they
  are set, so a saved suggestion is never silently ignored. Each decision records the weights
  it used.
- **Per run, without shared state.** The harness builds a consensus dispatcher with the run's
  weights for each decision (`ConsensusDispatcher.with_weights`) instead of changing the shared
  one, so runs with different profiles in one process (dashboard, scheduler) never mix weights.
- **Stored weights are validated when read.** A profile's weights that no longer validate (a
  renamed criterion) stop the run for a person instead of being ignored; `jev laya reset`
  clears them without reading them.

## 2026-10-13: Ship and merge after both votes, or ask first (#68)

This reverses the "jev never merges" stance of `jev ship` (below, 2026-10-13: `jev ship`, KPIs
and benchmarks). The maintainer decided on 2026-10-13 that when Jev and Laya both approve,
merging goes ahead without asking him, and that a person can choose to be asked first.

- **When.** Only with `[ship] enabled = true` (still off by default: it pushes with your `gh`
  login). Then a D6 "ship" goes to `SHIPPING` or `AWAITING_SHIP` instead of `DONE`, per
  `[ship] after_approval`; unset means "merge" under `consensus`, whose two votes are the gate,
  and "ask" with `jev` or `rules`, where one dispatcher decides alone (as with plan approval).
  `--auto-ship` / `--ask-before-ship` on `jev run` and `jev queue add` (and the dashboard queue
  form) override it per run; they are stored on the run (migration 12, `ship_mode`).
- **Two new run states** (jev-decide: AGREE, Jev 0.95, matrix 0.79 vs 0.37). `SHIPPING` is a
  pipeline step like the others, so a crash or a stop resumes there; `AWAITING_SHIP` is a person
  gate like `AWAITING_APPROVAL`. Rejected: shipping as a hook after `DONE` (DONE would no longer
  mean finished, and "modify" would reopen a finished run), and reusing `NEEDS_HUMAN` with a
  reason text for the "ask" wait (three choices hidden behind `resume`).
- **No second vote after the checks** (jev-decide: ARBITRATED, Jev 0.96). The issue asked that
  both votes "approve the final state". D6 already votes on the integrated result, and the merge
  is pinned to that commit with `gh pr merge --match-head-commit`, so what merges is what was
  voted on; the checks are then a yes/no gate, and a second vote on a green PR would cost two
  more Jev calls without new information. A failed check, a moved head, a draft, a conflict, a
  closed PR or a timeout stops for a person.
- **A PR without checks** (jev-decide: ARBITRATED, low confidence 0.18; Jev's direct pick was a
  setting, the matrix preferred always stopping). `[ship] require_checks = true` (default) stops
  for a person once `checks_grace_s` passed with no checks; `false` merges without CI. Both
  options agreed that the default must not merge without CI.
- **"ask" pushes nothing before the person approves.** Integration rebuilds the result branch
  from the start commit, so after a "modify" the new result is not a descendant of the old one;
  a PR opened before it could only be updated by force-pushing, which jev never does.
- **"modify"** adds a task "Changes requested before shipping (round N)" with the notes
  (redacted) after all other tasks, then EXECUTING, integration and D6 again. It does not count
  as a fix round, so it never uses up `max_fix_rounds`. **"stop"** ends the run `DONE` with "not
  shipped"; `jev ship RUN` can still ship it by hand.
- **Safe to repeat.** The SHIPPING step looks up a recorded PR for the current result commit,
  a branch already pushed and a PR already open (issue #41's lookup), and after `gh pr merge`
  it reads the PR back: only `state: MERGED` counts, whatever gh's exit code (a lost answer, a
  merge queue). Never `--admin`, `--auto` or `--force`; the PR branch is not deleted.
- **Threads.** The store is used only on the pipeline's thread (SQLite connections are per
  thread); the push and `gh pr create` run inline, the polling and `gh pr merge` in threads.
- `gh pr merge` and the extra `gh pr view` fields are not verified live yet
  ([verification.md](verification.md) section 8); the tests use the fake `gh`.
## 2026-10-13: The validator challenges every plan choice (#67)

The validator now records, for each significant choice in the plan, the alternative it did not
take, the planner's reason and whether that reason holds (`ChoiceChallenge` in
`core/models.py`). Jev made both calls below with the jev-decide skill.

- **The rule lives in the model** (ARBITRATED, confidence 0.48, over deriving it in
  `Pipeline._validate` and over rejecting inconsistent replies). A validator on
  `ValidationResult` turns each challenge that does not hold into an issue and sets the
  verdict to `revise`. Every parse, from a worker reply or from the store, gives a consistent
  result; a reply that flags a choice but approves is corrected rather than stopping the run.
  The planner gets the mistake through the `issues` it already reads, so its prompt and
  `_plan` are unchanged. Reloading is idempotent: an issue already present is not added again.
- **An unstated reason is not a failure by itself** (ARBITRATED, confidence 0.78, over failing
  every unstated reason). The planner prompt does not ask for reasons, so failing those would
  send most plans back until `max_plan_rounds` ran out. The validator writes `(not stated)` and
  fails the challenge only when the stated reason is wrong or no sound reason exists.
- **D3 sees the failed challenges** (choice, alternative, reason and note, clipped, at most
  five) plus the counts, so Jev and Laya vote on them. Sound challenges are only counted.
- **Compatibility.** `challenges` defaults to empty; runs stored before this load unchanged,
  and the rules dispatcher still reads only the (now enforced) verdict.

## 2026-10-13: The dispatcher picks the task reviewer (#67)

The task reviewer ran with the role's first worker and model and its middle effort, and was off
by default. The maintainer's flow asks for every finished task to be rated and for Jev and Laya
to choose who does each piece of work.

- **A new decision point, D8.** Asked before each review with the same worker, model and effort
  questions as D2, over adding reviewer questions to D4 (jev-decide with the Laya CDA matrix:
  Jev and the matrix agree, Jev 0.72). D4 failed the "old runs keep working" gate: D4 records
  stored before the change have no reviewer answers, and prefixed question names would bypass
  the generic worker, model and effort handling (free models, worker/model pairing). D8 also
  sees what D4 cannot: the diff's size and the test and quality results.
- **State.** Task card, attempt, the coder's assignment, tests, quality (when the gate ran),
  diff size (`files`, `lines_changed`), and `prefer_different_vendor_than` when
  `[roles.reviewer]` sets it. That key did nothing for the reviewer before; `"coder"` means
  this attempt's coder.
- **Rules.** D8 is answered like D2: first worker and model, middle effort, a different vendor
  when asked. With `primary = "rules"` or the rules fallback, the review runs as before.
- **Default.** `[review] enabled` is tri-state like `require_plan_approval`: unset means on
  under `consensus` and off with `jev` and `rules`; an explicit value wins. D5 already carries
  the review verdict and needs both votes under `consensus`, so a task is done only when Jev
  and Laya accept a reviewed attempt. This changes the default for repos that use `consensus`
  and do not set the key: each attempt that passes its tests costs one more decision (two Jev
  calls) and one reviewer call.

## 2026-10-13: Consensus dispatcher, Jev then Laya (#66)

`[dispatcher] primary = "consensus"` is the new default. Every decision needs two votes that
agree; when they cannot, a person decides. Jev and Laya are not two models: the Laya model's
weights could not be reached from the cloud environment (huggingface.co is blocked), so Laya
here is Jev scoring inside the Laya CDA decision matrix. That keeps one API and one key, and
makes the second vote a different kind of question rather than a second sample of the first.

- **Votes.** Jev votes first with the existing choice call. Laya votes second: Jev scores every
  allowed answer 1-5 on correctness 0.30, safety 0.20, simplicity 0.15, maintainability 0.15,
  tool/resource fit 0.10 and user preference 0.10 (`[dispatcher.laya] weights`). The highest
  weighted total is Laya's pick. The 18-question CDA grader was not used: it routes and
  grades, it does not choose between options. Laya does not see Jev's vote in the first round.
- **Settled** means Jev is confident (`min_confidence`), Laya ranks the same answer first, and
  Laya's lead is at least `min_margin` (0.2 on the 1-5 scale) after every score Laya was unsure
  about is moved one level against it. A lead that exists only because of a guess does not
  count as agreement.
- **Follow-up.** An unsettled question is asked again (`follow_up_rounds`, default 2), with the
  reasons of the earlier rounds in the state. Jev votes again; Laya re-scores only the
  uncertain cells, and a new score replaces the old one only when it is at least as confident,
  so a retry cannot quietly lower the evidence.
- **Stop for a human.** After the last round the run stops in `NEEDS_HUMAN` and `jev resume`
  continues it. It does not fall back to the rules dispatcher, as `primary = "jev"` does: a
  fallback would decide exactly the questions the two votes found hard, with the least
  information. A failed call (key unset, network, an answer outside the allowed values) stops
  the run the same way, because a decision made without Jev is not a consensus. Questions with
  one allowed value are not asked, as before.
- **Plan approval.** `[approval] require_plan_approval` is now tri-state and unset by default:
  off under `consensus`, whose D3 approve vote is the gate, and on with `jev` and `rules`,
  where nothing else checks the plan. An explicit `true` or `false` always wins, and
  `--plan-only` still stops at the plan. This changes the default for new and existing repos
  that do not set the key: with `consensus` a plan both votes approve is built without a human
  step. To keep the old behavior, set `primary = "jev"` or `require_plan_approval = true`.
- **Cost.** Each round is two Jev calls, so a decision costs about twice as much as with
  `primary = "jev"`; follow-up rounds ask only what is still unsettled.
- **Kept.** `primary = "jev"` and `"rules"` behave as before, including the rules fallback.

## 2026-10-13: Run lease (#61)

Two processes (the dashboard and a terminal `jev resume`, say) could drive the same run at once.
Jev picked the design: a lease row in the state DB with a heartbeat (ARBITRATED 0.28), over a
per-run `flock` file like the scheduler's and over a hybrid of both.

- **Table `run_leases`** (migration 11): run id, a random token per claim, pid, host,
  `acquired_at`, `heartbeat_at`. Claim, renew and release are single `BEGIN IMMEDIATE`
  transactions; renew and release only touch the row with their own token.
- **Who holds it.** `Pipeline.start`, `approve`, `resume` and `advance` hold it for their whole
  work (`core/lease.py`, `hold_run_lease`); `resume` takes it before it closes interrupted
  attempts, so it never closes a live process's attempt. `cancel` only writes the store and
  needs no lease.
- **Staleness.** A holder on this host is live exactly while its pid is alive: a long blocking
  step never loses the lease, and a killed process frees it at once. A holder on another host
  (pid not checkable) is live while its heartbeat is under 60 s old; the holder renews every
  15 s. A holder that finds its lease taken over is cancelled and raises `RunLeaseLostError`.
- **Errors.** `RunInUseError` names the holder; the CLI prints it as an `error:` line, the
  dashboard as the job message. The dashboard's in-memory per-run lock stays, and
  `resume_risk` reports a live lease first.

## 2026-10-13: CodeRabbit configuration

- `.coderabbit.yaml` uses the "balanced" posture (Jev: ARBITRATED 0.64, over a strict gate and a
  light one):
  - an assertive review that never requests changes by itself;
  - pre-merge checks that warn, except two that fail: no secrets or personal data, and no
    network or real CLIs in tests.
- CI owns formatting, lint, types and tests. CodeRabbit runs ruff only as a cross-check.
  pylint, flake8, markdownlint and languagetool are off, to avoid duplicate or noisy findings.
- Path instructions encode the rules from CLAUDE.md and CONTRIBUTING.md for each package.
  CodeRabbit also reads those files as code guidelines.
- Fake credentials in provider formats are built at runtime, after GitHub secret scanning
  flagged a fake Google key in `tests/unit/test_redact.py`.

## 2026-10-13: Medium-priority review fixes

Tracking issue #59. Choices were decided with the `jev-decide` skill, as for batches A to E.
#35 is fixed in the contributor pull request #62 instead.

- **CLI errors and light contexts (#25, #26, #51).** `cli.wiring.CliError` carries user-facing
  errors: repo, config and a newer state DB.
  - The CLI turns a `CliError` into one `error:` line on stderr and exits 1.
  - The dashboard shows the same error as the job message. Jev: ARBITRATED 0.84, over a
    `typer.Exit` subclass.
  - Commands that run no workers use `RepoContext`: config, store and learning.
  - Read-only commands open a missing state DB as an empty in-memory one. They print their usual
    empty output and leave no `.jev/` behind. Jev: ARBITRATED 0.98, over an optional store and
    over failing.
- **Migrations under the write lock (#23).** `apply_migrations` takes `BEGIN IMMEDIATE`, then
  reads `user_version`, then runs each script statement by statement.
  - Statement by statement is needed because `executescript` commits first, before
    Python 3.12.
  - `configure_connection` retries the WAL switch, because it fails at once while another
    process holds the lock.
- **Ship recovery (#41).** `jev ship` runs `gh pr list --head <branch> --state all` in two cases
  and records a PR it finds:
  - before creating a PR on a branch that is already pushed;
  - after a `gh pr create` that failed or timed out.

  The JSON shape of `gh pr list` is not verified live, so parsing is tolerant: anything
  unexpected means "no PR found".
- **Learning counts (#42).** Learning steps store their counts in `runs.learning_counts_json`.
  - Migration 10 fills the column once from older runs' outcome text.
  - Counts are summed over the run's learning steps, and KPIs never parse text.
  - Jev: ARBITRATED 0.72, a JSON column over an integer column and over counting lessons by
    `source_run`.
  - The PR state is a `Literal`, and unknown gh values count as `open`.
- **Total Jev timeout (#36).** `asyncio.timeout(timeout_s)` wraps the retry loop.
  - An `httpx.ConnectError` gets the call's single retry after 1 s, shared with 429/5xx, so a
    call makes at most two requests. Jev: ARBITRATED 0.31.
  - Read errors are not retried, because the server may already have processed the request.
- **Config keys that did nothing (#30, #53).** `[dispatcher] fallback` is removed. `"rules"` is
  still accepted for old files. Jev: ARBITRATED 0.72, over `Literal["rules"]`.
  - A role's `efforts` must be accepted by at least one of the role's workers. Jev: ARBITRATED
    0.56, over requiring every worker and over accepting any known level.
  - Instruction keys are checked against `config.schema.DECISION_QUESTIONS`.
  - Tests run with COLUMNS pinned to 200, and narrow output has its own tests in
    `test_narrow_terminal.py`. Jev: AGREE 0.77, over `setdefault` with a known-red narrow run.
  - Follow-up: rich tables in `cli/` crop cells at 40 columns; fold them instead.
- **Contributor docs.** The contributor docs require no DCO sign-off: inbound equals outbound
  under Apache-2.0 section 5. Jev: ARBITRATED 0.73.
  - Blank issues are disabled, and questions go to Discussions. Jev: ARBITRATED 0.38, a low
    confidence, so revisit it if contributors find this awkward.
  - There is no CODEOWNERS file. Jev: AGREE 0.96.

## 2026-10-13: Fixes from the full code review (batches A to E)

Tracking issue #59. Each design choice below was decided with the `jev-decide` skill
(`.claude/skills/jev-decide/`): Jev's direct pick plus a gated weighted matrix; disagreements go
back to Jev, which arbitrates. Decision logs are kept with the review notes, not in the repo.

- **Process lifecycle (#11, #12, #13, #49).** `workers.subprocess_runner.run_command` is the one
  owner of child processes: it kills the process group on timeout *and* on cancellation.
  `ShellCommandRunner` delegates to it with `sh -c "exec 2>&1\n<command>"` (Jev: ARBITRATED 0.88
  over a `merge_stderr` flag on `run_command`), so test commands keep their stream order and
  their timeout holds when they start children. The dashboard's `RunLauncher` owns its jobs: a
  Starlette lifespan calls `shutdown()`, which cancels each job's coroutine through
  `run_job_coroutine` (Jev: AGREE, sync executors with a cancel hook, over async executors and
  over jobs as subprocesses). Server commands run under the server's `timeout -k 2 N sh -c`,
  without a limit where `timeout` is missing (Jev: ARBITRATED 0.70 over requiring it).
- **Trusted repository config (#16).** A repo `jev.toml` may change `config.loader.GUARDED_KEYS`
  (endpoints, credential variables, worker commands and permissions, tools, servers, browser,
  profiles, role grants) only when the user config's `[trust] repos` lists the repo; a value
  equal to the user/default value is always accepted, and a repo cannot contain `[trust]`.
  Jev: AGREE over a host allowlist for endpoints only, and over warnings (which failed the
  gate). `prompt_file` is not guarded: custom prompts are an ordinary repo feature.
- **One credential-pattern list (#27).** `runlog.redact.CREDENTIAL_PATTERNS` (kind, pattern,
  replacement) is used for redaction, by the quality gate's secret scan (plus its
  code-only "hard-coded credential" heuristic) and by the config check for literal secrets.
  Text is redacted before it is truncated.
- **Cancellation wins; plan-only is stored (#14, #22).** `Store.set_run_state(..., unless=)`
  is a conditional update; the pipeline writes with `unless={CANCELLED}` and `cancel_run`
  with `unless=STOPPED`, so neither a running step nor a cancel can overwrite the other
  between a check and the write (Jev, during the review: ARBITRATED 0.46 for guarded writes
  over a full transition table). Stopping the running worker on cancel is still a follow-up.
  `runs.plan_only` (migration 8) replaces the per-call `Pipeline._plan_only` attribute.
- **Resume while running (#15).** The dashboard treats a run as possibly running when it has an
  attempt with no result younger than its worker's `timeout_s` plus 300 s, or when the live
  scheduler lock's current queue item started it; plain Resume is then refused and Force
  resume needs a confirmation (Jev: ARBITRATED 0.72 for attempt age over "any open attempt"
  and over ownership only). A run lease (pid, host, heartbeat) remains the real fix.
- **D5 done preconditions (#21).** Worker success, tests not failed and an unblocked quality
  gate are hard preconditions for `done`; otherwise the action becomes `retry`, with the
  reason in the task note (Jev: ARBITRATED 0.16 for retry over the rules dispatcher's answer).
- **Worker readiness and the init template (#19, #20).** One predicate
  (`cli.wiring.worker_readiness`) for runs and `jev doctor`: CLI workers need their command on
  PATH; api needs its key and, per role, a runnable model, and never runs the coder. Unready
  workers are dropped with a warning; api without a model for some roles is hidden from those
  roles via `WorkerRegistry.names(role)` (Jev: ARBITRATED 0.66, per role over per worker). The
  `jev init` template comments out every value, so defaults < user < repo holds again.
- **Runnable (worker, model) pairs; error vs failure (#33, #34).** `requests.model_fits` infers
  compatibility from the name (`/` means api only, otherwise CLI only, the default fits all);
  `assign.fit_free` repairs dispatcher answers, escalation and reassignment by keeping the
  worker and taking its first runnable pool model or its default (Jev: ARBITRATED 0.92 over a
  per-worker model list). Codex custom providers with `/` names are unsupported. Exit 127/126
  without output is `error`, and D5 rules reassign on `error` at once (Jev: ARBITRATED 0.97);
  the api worker maps `httpx.NetworkError` and `RemoteProtocolError` to `failure`, other
  transport errors to `error` (Jev: ARBITRATED 0.33).
- **Git timeouts and repo hooks (#24).** `workspace.run_git` runs git in its own process group
  with no terminal and closed stdin; 60 s for quick calls, 600 s for commit, merge, worktree and
  push (Jev: AGREE). `jev ship` sets `GIT_TERMINAL_PROMPT=0` and an empty `GIT_ASKPASS`
  (ARBITRATED 0.30). Git runs off the event loop through `asyncio.to_thread` at the call sites;
  the `Workspace` protocol stays synchronous (ARBITRATED 0.31). Repo hooks run by default;
  `[workspace] run_hooks = false` adds `--no-verify` (ARBITRATED 0.09). A failed commit or diff
  halts NEEDS_HUMAN resumably, keeping the worktree (ARBITRATED 0.83 over FAILED and over a
  failed attempt).
- **`init --migrate` (#29).** Only pre-checks raise; the DB is migrated right after the
  directory rename; failed ref renames and steps are reported, and a rerun completes them.
- **Exit codes (#28).** One code, 3, for every stop short of DONE, not one per state (Jev:
  ARBITRATED 0.34; the matrix leaned to distinct codes). 1 stays a command error, 2 usage.
- **Queue items and a dying scheduler (#38, #39).** The scheduler links an item to its run with
  a per-connection TEMP trigger on `runs`, inside the run's insert (ARBITRATED 0.94 over editing
  `create_run` and over a callback through the pipeline). On start it recovers `running` items;
  `remove_item` refuses only while a live scheduler holds the lock (ARBITRATED 0.80). Deleting
  is one conditional statement. The lock is an `fcntl.flock` held for the scheduler's lifetime
  (ARBITRATED 0.82); unmarked lock files from older versions keep the pid/host rules.
- **Lesson ids and scoring (#40).** `lessons` is rebuilt with AUTOINCREMENT in state.db
  (migration 9, numbering after `L` ids attempts still name) and in the shared DB (shared
  migration 2); writes to a lesson also match its profile. Scoring is recorded in a
  `scored_tasks` ledger in the database that holds the scores, in the same transaction
  (ARBITRATED 0.95). Ids deleted before the upgrade in the shared DB cannot be recovered.
- **MCP server survives bad input (#47).** `servers/mcp.py` accepts only finite numbers;
  out-of-range values are still clamped by `ServerService` (Jev: ARBITRATED 0.04, "finite only"
  over also refusing out-of-range values). A tool exception becomes an `isError` result with the
  traceback logged to stderr; any other failing line gets a -32603 reply and serving continues.
- **Server policy hardening (#44, #45, #46, #48).** Recursive `grep` only inside
  workdir/allowed_paths, with `--exclude`/`--exclude-dir` for credential names and `-R`
  refused (Jev: ARBITRATED 0.35). `docker inspect` and `systemctl show` are dropped rather than
  filtered, since `--format` filters are easy to get around (Jev: ARBITRATED 0.82); `ps e`,
  `git show REV:path` and `git diff --no-index` are closed too. Recursive `cp` only from inside
  allowed_paths (Jev: ARBITRATED 0.20); a credential file that lives there can still be copied
  (documented). Writes go through a fresh `mktemp` file. `.git` paths are never writable, and
  every git command runs with `core.hooksPath=/dev/null` and `core.fsmonitor=false`.
  `pip install -r` stays allowed and is documented instead: medium access already means code
  execution as the server account (Jev: ARBITRATED 0.76). The audit log gets a `phase` field:
  a `started` line before ssh runs (nothing runs if it cannot be written) and a `finished`
  line after (Jev: ARBITRATED 0.78). Not covered: implicit bare repositories inside
  allowed_paths, and tokens in remote URLs readable at basic (docs/servers.md, known limits).
- **Quality tool errors are a status (#18, #17).** `CheckStatus` gains `error`: the tool is
  installed but crashed, timed out, printed unparseable output, or (bandit, Semgrep) reported
  scan errors; a check that raises is `error` too. One helper (`quality.base.tool_error`)
  builds it; it blocks only when the check is required, otherwise it is reported. Jev:
  ARBITRATED 0.22 for `error` over "fail without findings" and over "skipped plus a flag" (the
  direct pick was `error` in every round). Stored `quality_reports` JSON still parses. A
  failing or timed-out test suite stays `fail`: the code under test is the usual cause.
  gitleaks exiting 1 with nothing parsed fails with a critical finding (Jev: ARBITRATED 0.92
  over `error` and over switching to its JSON report).

## 2026-10-13: Servers over SSH, virtual browser, workspace profiles

Code: `src/jev_agent/servers/` (policy, ssh, audit, service, mcp, access, profiles, browser),
`config/servers_schema.py`, `cli/server_cmds.py`, `src/jev_agent/__main__.py`; tests in
`tests/unit/test_servers.py`. User docs: [servers.md](servers.md). Issue #3, option 1.
Verification: [verification.md](verification.md) section 9.

- **`full` access is deferred.** Only `basic` and `medium` exist; config validation rejects
  `access = "full"` (also in `access_overrides` and `--allow NAME:full`) with "access = \"full\"
  is not available yet; see issue #3". An unrestricted level means an agent can run anything
  with the server account's rights, including the destructive commands the always-blocked list
  exists for; that needs its own design (explicit confirmation, maybe a separate account) and
  a maintainer decision about Claude Code's permission settings. Adding it later is an
  additive change to `AccessLevel` and the policy.
- **Policy in an MCP server, not raw SSH for agents.** Agents never see an `ssh` binary or a
  shell on the server. Giving an agent `Bash(ssh *)` would leave enforcement to command-line
  pattern matching in each CLI's permission system (different in Claude Code and Codex, and
  bypassable through shell syntax in the remote command). Instead jev synthesizes a
  `jev_servers` MCP entry (`python -m jev_agent mcp servers --allow web:medium --role coder
  --run-id R`) next to the role's own tools; it parses each command, refuses shell syntax,
  re-quotes every word for the remote shell, checks it against one policy module
  (`servers/policy.py`) and audits it. The same code serves Claude Code and Codex.
- **A minimal MCP server, no SDK.** `mcp` 2.3.0 brings starlette, uvicorn, httpx2, pyjwt,
  jsonschema and opentelemetry for transports jev never uses; stdio JSON-RPC with
  `initialize`, `server/discover`, `ping`, `tools/list` and `tools/call` is ~200 lines and
  answers both legacy (handshake) and 2026-07-28 (per-request `_meta`) clients.
- **Policy shape.** Allowlist per level, not a denylist: a command is refused unless its
  checker accepts it. The always-blocked list is checked first at every level, so its message
  is clear and it still holds if an allowlist grows. Paths are normalized lexically for
  commands; the file tools also resolve symlinks on the server (`realpath -m`) before writing
  or reading. No globbing (words are quoted). `sudo` only as `sudo -n` before package
  installs, `systemctl` and the pinned browser install (the issue said "sudo only for
  package installs"; service restarts need root too, so `systemctl` was added). `ssh` units
  are never stopped or restarted (lock-out). Medium also allows `rm`/`mv`/`touch` strictly
  inside `allowed_paths` and `git add`/`switch` (needed for `commit`/`checkout`); `git push` is
  not allowed at all in this version. Reads of credential files are refused at every level.
- **Role caps.** Every role but the coder (planner, validator, reviewer, code_reviewer,
  learner, chat) is capped at `basic`, whatever the server or profile says. A profile's
  servers go to every role (others at basic), its `browser = true` to the coder only;
  `[roles.<role>] browser = true` needs `[browser] enabled`.
- **SSH.** System `ssh` (keys, agent, `~/.ssh/config`, `ProxyJump` all work as for the user),
  `BatchMode=yes`, `StrictHostKeyChecking=yes` (never `no`/`accept-new`: the user accepts a
  host key once by hand), `ConnectTimeout=10`, `ControlMaster=auto` with `%C` sockets under
  `.jev/ssh/` (0700, `ControlPersist=60`; off when the socket path would be too long). Files go
  through `head -c` and `cat > "$(mktemp DIR/.jev-tmp.XXXXXXXXXX)" && mv` (a fresh temp file since
  the review fixes, #45), so no scp/sftp. `SSH_AUTH_SOCK` is forwarded to the
  MCP process because MCP clients start stdio servers with a reduced environment.
- **No migrations.** Issue #8 owns the store this cycle, so state lives in files: the repo's
  default profile in `.jev/profile`, the run's copy of its profile in
  `.jev/runs/<id>/profile.json` (a snapshot, so resume and config edits do not change a running
  run's servers; servers removed from config are skipped), and the audit log in
  `.jev/audit/servers.log`. `Pipeline.start(profile=...)` writes the copy; the harness reads it
  when it builds each job's tools.
- **Profile hooks.** `start_run` now takes `profile`, so the scheduler and the chat pass a
  stored profile straight through (the `inspect.signature` hooks and their
  `ProfileUnsupportedError` are gone). `jev queue add` and `jev chat` reject unknown profile
  names up front; a queued item whose profile was removed later fails that item.
- **Browser.** Pinned `@playwright/mcp@0.0.83` with `--browser chromium --isolated` (and
  `--headless` by default); the install uses the exact Playwright version that release
  depends on, so the browser build matches. `jev browser install` always prints the command and
  asks (or `--yes`); on a server it needs medium and runs through the policy, which allows only
  that exact argv. Xvfb/noVNC are documented, not automated.
- **Chat itself gets no server tools** in this version; the chat's profile applies to the runs
  it launches.

## 2026-10-13: `jev ship`, KPIs and benchmarks

Code: `core/ship.py`, `core/kpi.py`, `core/benchmarks.py`, migration 7, `cli/ship_cmds.py`,
`dashboard/` (Ship as PR, the KPIs page, `render.render_trend_chart`); tests in
`tests/unit/test_ship.py` (local bare `origin`, fake `gh` from `tests/fixtures/gh/`),
`tests/unit/test_kpi.py`, `tests/unit/test_dashboard.py`. User docs: [ship.md](ship.md).
Issue #8.

- **Relaxes "never push" as an explicit, per-run action.** PLAN.md's default was that jev
  never pushes. `jev ship RUN` is the one exception: off unless `[ship] enabled = true`, asks
  for confirmation (`--yes` skips; the dashboard has a confirmation page), refuses runs that
  are not DONE or whose latest quality gate blocked (`require_quality_pass`). It pushes the
  existing `jev/<run>/result` branch with a plain `git push` (never `--force`; an existing
  remote branch with other commits stops it, `--force-new` takes `<prefix><run>-2`), opens the
  PR with `gh pr create` (draft by default) and never merges or enables auto-merge. The user's
  checkout is never touched. Security notes: [security.md](security.md#shipping-prs).
- **`gh`, not the GitHub API.** The user's own `gh` login does the work: jev never handles a
  token, and GitHub Enterprise hosts work as gh does. Verified flags and JSON fields are in
  [verification.md](verification.md) section 8.
- **One command, `jev ship RUN | sync`.** `jev ship sync [RUN | --all]` is dispatched from the
  same Typer command (a run id can never be `sync`), because a Typer group with both a
  positional run id and subcommands is ambiguous.
- **Storage: migration 7.** `shipped_prs` (one row per PR, with the GitHub data from the last
  sync, plus `title`, `head_oid` and `pushed_commits` beyond the issue's list: they drive the
  revert heuristic and "commits after open") and `run_kpis` (a JSON snapshot per run taken at
  ship time, so KPIs stay stable when branches or logs are cleaned up; runs without one are
  computed on the fly). `raw_json` keeps `gh pr view`'s answer with review and comment bodies
  reduced to counts.
- **KPI definitions.** Retries/escalations/reassigns count D5 answers (a `done` overridden by
  a blocking gate is not visible there). Fallback rate = rules fallbacks / decisions Jev was
  asked to make (None with `primary = "rules"`). Free model share = free-model attempts / api
  attempts. Review comments = conversation comments + reviews with a body (inline comments are
  not in `gh pr view --json`). Commits after open = PR commits - pushed commits. Reverted =
  a merged PR whose body says `Reverts owner/repo#N` or titled `Revert "<title>"`, searched with
  `gh pr list --state merged --search`.
- **Benchmarks.** Rolling median over 5; a point regresses against the median of the up to 5
  previous values (at least 3): costs, times and counts above 1.5x (never against 0), rates
  and confidence 15 points worse. Direction per KPI (`core.benchmarks.KPI_DEFS`). Pure
  functions, unit-tested; the dashboard draws them as server-side SVG (no JavaScript, CSS
  classes only under the strict CSP).
- **No new dependency**, no new decision point; `jev doctor` is unchanged (it would need a
  network call to check `gh auth status`; `jev ship` checks it itself).

## 2026-10-12: Web dashboard (`jev dashboard`)

Code: `src/jev_agent/dashboard/` (app, views, security, runner, render, templates, static),
`cli/dashboard_cmd.py`; tests in `tests/unit/test_dashboard.py`. User docs:
[dashboard.md](dashboard.md). Issue #1; PLAN.md's roadmap had "small FastAPI server + web
dashboard".

- **Starlette, not FastAPI.** The dashboard is server-rendered HTML and form POSTs; it needs
  no OpenAPI schema, request models or dependency injection. Starlette is the layer FastAPI is
  built on, with fewer moving parts, and its routes are a plain list in `create_app`.
  Templates are plain Jinja2 (already a core dependency) with `autoescape=True`; uvicorn
  serves it; python-multipart parses forms; markdown-it-py (already pulled in by rich) renders
  `review.md` with raw HTML disabled.
- **An optional extra, not core dependencies.** `[project.optional-dependencies] dashboard`.
  Most installs (CI machines, headless VPS runners) never open a browser, and an ASGI server
  is not small. `jev dashboard` checks with `importlib.util.find_spec` and prints
  `uv tool install 'baton-agent-orchestrator[dashboard]'` when something is missing. CI and
  release workflows use `uv sync --locked --all-extras`; contributors `uv sync --all-extras`.
- **No htmx.** The plan suggested vendoring htmx; the dashboard only needs "replace this
  element with the HTML at this URL every few seconds" and "confirm before submitting", so a
  40-line `static/live.js` does that (`data-live`, `data-confirm`). No third-party JavaScript to
  vendor, audit or update; it works under a strict CSP with no inline script; pages work
  without JavaScript.
- **Same functions as the CLI.** Logic that lived inside Typer commands moved to reusable
  functions, used by both: `core.queue.add_idea` / `remove_item` / `queue_counts`,
  `core.learning.use_profile` / `enable_learning` / `disable_learning` / `delete_profile`,
  `core.pipeline.cancel_run` (no pipeline needed to cancel), `cli.run_cmds.approve_run` /
  `resume_run`, `core.scheduler.scheduler_lock`, `cli.quality_cmds.review_text`,
  `core.chat.read_transcript`; `cli.board_cmds.load_board` takes a title width. Read-only
  pages reuse `load_board`/`build_board`, `format_answers`, `learning_line`, `quality_line`,
  `item_flags`, `effective_status`, `spent_today`, `run_checks` (doctor).
- **Runs execute in a background thread.** Approve and resume call `execute_run`, which is
  `jev approve` / `jev resume` (`cli_context` + `approve_run` / `resume_run`) in a daemon
  thread with its own store connection (SQLite connections are per thread) and event loop.
  `RunLauncher` keeps a per-run `threading.Lock`, held for the whole job, so one dashboard
  never runs two jobs for one run. A cross-process lock (dashboard vs. a terminal) is out of
  scope, as it already is between two terminals. Tests inject the executor.
- **Security model.** Loopback bind by default with a Host-header allowlist (DNS rebinding).
  Non-loopback binds refuse to start without a token (`DashboardConfigError`); `jev dashboard`
  generates one. Token via cookie, `X-Jev-Token` or `?token=` (swapped for an HttpOnly,
  SameSite=Strict cookie by a redirect). CSRF: POST only, same-origin check (`Sec-Fetch-Site`,
  `Origin`, `Referer`) plus a per-session form token, HMAC(session cookie, per-process secret).
  Strict CSP, no framing, `no-store`. Run files are served as `text/plain` only from inside
  `.jev/runs/<id>/` (resolved path must stay inside; symlinks out refused). Settings shows env
  var names and set/unset, never values. Flushing a profile needs a confirmation page.
- **No new config keys.** Options are flags; the token can also come from
  `JEV_DASHBOARD_TOKEN` (documented in config.md's environment variables), so it need not
  appear in the process list.
- **Scheduler start stays in the terminal.** The queue page shows the lock holder and current
  item; starting a long-lived scheduler from a web request was left out.

## 2026-10-12: Rename to Baton Agent Orchestrator

The project is now **Baton Agent Orchestrator** (v0.2.0). Why: the maintainer's choice. Only
the product name, the PyPI name, the long command alias and the repository URL change.

- **Title:** "Baton Agent Orchestrator" in the README, docs, `jev --help`, the package
  docstring and the OpenRouter attribution headers (`X-OpenRouter-Title`, `HTTP-Referer`).
- **PyPI distribution:** `baton-agent-orchestrator` (was `jev-agent-orchestrator`). The bare
  name `baton` is still taken on PyPI (see "Project name" below).
- **Console scripts:** `jev`, plus the long alias `baton-agent-orchestrator`; the
  `jev-agent-orchestrator` alias is gone.
- **Repository:** `https://github.com/Gane2506/baton` (renamed from
  `jev-agent-orchestrator`; GitHub redirects the old URL).
- **Unchanged:** the CLI `jev`, the import package `jev_agent`, `jev.toml`, `.jev/`, `JEV_*`
  variables, `jev/<run_id>/...` branches, and everything named after the Jev model
  (`[dispatcher] primary = "jev"`, `[dispatcher.jev]`, `JevDispatcher`). Older entries below
  keep the names they were written with.

## 2026-10-12: Quality and security gate

Code: `src/jev_agent/quality/` (levels, models, tools, base + checks_basic/medium/production,
policy, review, report, gate), role `code_reviewer`, migration 6, `cli/quality_cmds.py`; tests
in `tests/unit/test_quality.py`, `tests/unit/test_cli_quality.py`,
`tests/integration/test_quality_pipeline.py`. User docs: [quality.md](quality.md).

- **Beyond PLAN.md.** `[quality] level` (`none` default, then `basic`, `medium`, `production`,
  `scalable`, cumulative). Precedence: `--quality` on run/resume (and `/quality` in the chat) >
  the queue item's `--quality` > config. The level is stored on the run.
- **Storage: migration 6.** `runs.quality_level` (NULL reads as `none`), `queue.quality`, and a
  `quality_reports` table (one row per gate, result as JSON). Reports also go to
  `.jev/runs/<run_id>/quality/<T1-2|integration-1>.md` and a rebuilt `review.md`.
- **New role `code_reviewer`, not an extended `reviewer`.** Its output (findings with severity,
  category, location, checklists) and job (report, not approve/revise) differ from the
  reviewer's, and keeping both lets `[review]` and the gate be on independently. Default
  workers `["api", "codex", "claude_code"]`; read-only; no lessons.
- **No new decision point.** The code reviewer is chosen like the task reviewer (first worker
  and model, middle effort, free models kept on api, quota fallback, budget checked). The
  dispatcher sees the outcome through D5/D6 (`quality`, `quality_failed`, top 5 findings).
- **The gate is a hard limit.** A `done` (D5) or `ship` (D6) while it blocks is turned into a
  retry or a fix task, like the budget caps override the dispatcher.
- **Checks are a registry** (`QualityCheck` protocol, `CHECKS` list); contributors add a class.
  Missing tools skip (never fail a run) unless listed in `required_tools`. Tools run through the
  injectable `CommandRunner` with `[quality] timeout_s`; output redacted and clipped.
- **Judge only what changed.** File-list tools run on changed files; whole-tree tools
  (gitleaks, type checkers, trivy, checkov) only count findings in changed files, so
  pre-existing problems never block. Coverage runs only at integration; dependency audits on a
  task only when it changed a dependency file.
- **Semgrep only with local rules.** `--config auto` fetches registry rules (network, metrics),
  so it is not offline-capable; jev runs Semgrep only when `.semgrep.yml`/`.semgrep/` exists.
- **Secret scan fallback:** without gitleaks, a built-in scan of the diff's added lines reuses
  the log redactor's key patterns (`SECRET_PATTERNS`) and secret env values, plus a few
  well-known token formats.
- **Blocking policy:** basic/medium block on failing tests/lint/format and critical findings;
  production adds failing type/coverage/licence checks and major security findings; scalable
  adds major performance findings. Required tools block at every level.

## 2026-10-12: Rename jao → jev

The short name `jao` is replaced by `jev` everywhere users see it. The PyPI distribution stays
`jev-agent-orchestrator`.

- **Import package `jev_agent`, not `jev`.** The PyPI name `jev` belongs to an unrelated library
  (a TypeSafe decorator library) whose import name is `jev`; installing both would make one
  shadow the other. So the code lives in `src/jev_agent/`.
- **CLI:** `jev` (the `jev-agent-orchestrator` alias stays; the `jao` script is gone).
- **Files:** `jao.toml` → `jev.toml`, `.jao/` → `.jev/` (state.db, runs, chats, cache,
  worktrees, scheduler.lock), user config `$XDG_CONFIG_HOME/jev/config.toml` (else
  `~/.config/jev/config.toml`), shared lessons `$XDG_DATA_HOME/jev/learning.db`.
- **Git:** branches `jev/<run_id>/...`, start ref `refs/jev/<run_id>/start`, commit identity
  `jev <jev@localhost>`.
- **Env vars:** `JAO_*` → `JEV_*` (`JEV_FAKE_WORKERS`, `JEV_LIVE_TESTS`, `JEV_REDACT_NAMES`,
  `JEV_LIVE_*_MODEL`, `JEV_TEST_OPENROUTER_KEY`).
- **Coder summary block:** fenced `jev-summary` (was `jao-summary`).
- **The Jev dispatcher is unchanged:** `[dispatcher] primary = "jev"`, `[dispatcher.jev]` and
  `JevDispatcher` name the Jev model, not the tool.
- **Repos set up with v0.1.0.** When a repo has `jao.toml` or `.jao/` but neither `jev.toml` nor
  `.jev/`, every command stops and points to `jev init --migrate`. That command renames both,
  rewrites `.jao/` to `.jev/` in `.gitignore`, renames local branches `jao/*` → `jev/*` and refs
  `refs/jao/*` → `refs/jev/*` (never pushes), and rewrites the absolute log paths stored in
  `state.db` (`attempts.log_path` and its copy in `result_json`; idempotent). Worktrees that lived
  under `.jao/` are kept: after the move it runs `git worktree repair <new paths>`, which
  reconnects them, so nothing needs `git worktree prune`. A branch checked out in a worktree is
  skipped and reported (renaming it would change that worktree's HEAD under it); remove the
  worktree or rename the branch by hand. Other text that mentions old paths (task notes, chat
  transcripts) is left as written.

## 2026-10-12: `jev chat`

Code: `core/chat.py`, `cli/chat_cmds.py`, `roles/chat/prompt.md`; tests in
`tests/unit/test_chat.py` and `tests/unit/test_cli_chat.py`. User docs: [chat.md](chat.md).

- **Beyond PLAN.md**, which starts every run from a one-line idea. The chat turns a rough idea
  into a brief first; the brief's text becomes the run's idea. No new config keys: options are
  flags, and the backend reuses `[workers.api]`, `[workers.claude_code]` and
  `[roles.planner] models`.
- **Same code paths.** `/plan` and `/run` call `run_cmds.start_run` (as `jev run` and the
  scheduler do); `/queue` uses `Store.add_queue_item` (as `jev queue add` does). The approval
  gate and run budgets are unchanged.
- **Two backends behind a small `ChatBackend` protocol** so tests use fakes. The api backend
  sends the whole conversation each turn (multi-turn messages, which `ApiWorker`'s one-prompt
  job does not model). The Claude Code backend needs `--resume <session_id>`, so it builds its
  own argv (the worker's `--no-session-persistence` would make resume impossible) but reuses
  `run_command` (process group, timeout, redacted log) and the worker's result parsing and
  classification. It is read-only: `dontAsk` with Read/Glob/Grep only.
- **Learning choice has three states**: one profile, off, or the repo default (resolved at
  launch exactly as `jev run` without flags). Showing lessons never creates a profile; picking
  "with learning" on the start screen or `/learning on` creates the active profile, as
  `jev learning enable` would. Lessons are selected for the planner role.
- **Workspace profile**: same PROFILE HOOK as the scheduler. Passed to `start_run` only if its
  signature has `profile`; otherwise `/plan` and `/run` refuse rather than drop it. *(Superseded 2026-10-13: the chat
  passes the profile directly and it is applied to the runs it starts, #31.)*
- **Chat cost is separate**: checked against `--budget-usd` (default $1.00) before each model
  call, shown by `/cost`, never added to a run. A check-before-call budget can end slightly
  above the cap; Claude Code calls also get `--max-budget-usd` with what is left.
- **Persistence in files, not state.db**: `.jev/chats/<id>/{meta.json,transcript.jsonl,
  backend.log}`, redacted before writing. Chats are personal scratch work that does not need
  queries or migrations; the files are easy to read and to delete.

## 2026-10-12: Free OpenRouter models

Code: `workers/openrouter_models.py` (catalog and cache), `[models]` and
`[roles.<role>] prefer_free` in `config/schema.py`, `role_options` and D5 state in
`dispatch/requests.py`, `dispatch/rules.py`, `dispatch/jev.py`, `core/assign.py`
(`quota_fallback`, `fit_free`), `Harness.run_worker_with_fallback`, `classify_api_error`,
`cli/models_cmds.py`, doctor rows. Verified facts: [verification.md](verification.md#7-free-models-openrouter).
User docs: [config.md](config.md#free-models).

- **Free = price 0 or `:free`.** Both are documented ways a free model shows up; price-0
  models without the suffix exist today. The `openrouter/free` router is never added by
  `"free"` expansion because it picks a model at random, which makes attempts unreproducible.
- **Api worker only.** Free slugs are OpenRouter ids; Claude Code and Codex cannot take them.
  Roles without `api` ignore `prefer_free` and drop `"free"`. A dispatcher answer that pairs a
  free model with another worker is corrected by the harness (`fit_free`: move to `api`, else
  use the first paid model), like the existing "never trust the dispatcher" rule.
- **Capability filter.** Structured roles need `structured_outputs` in
  `supported_parameters` (what `response_format: json_schema` with `require_parameters` needs);
  models that list no parameters are kept and marked unknown rather than dropped.
- **Expansion at run start, from a cache.** The model list is fetched only when some role uses
  free models (and never with `JEV_FAKE_WORKERS=1`), cached for 24h, and a failed fetch falls
  back to the stale cache with a warning. Doctor never touches the network.
- **429 on a free model = `quota_exhausted`.** Free-model 429s carry no `limit_source`, and the
  per-minute and per-day limits cannot be told apart reliably, so jev does not wait: it moves
  on. Fallback chain: next free model, first paid model, one other worker. Each try is a
  recorded attempt. Run-level roles (planner, validator, learner) and the reviewer do this in
  `Harness.run_worker_with_fallback` within one step (they had no retry logic to extend);
  tasks do it through D5, where the rules dispatcher answers `retry` when
  `state.quota_fallback_model` is set, and the task runner moves to that model. In practice
  the coder never runs on `api`, so the D5 path is for completeness.
- **Escalate from free goes to paid.** "Try harder" after a free model means a paid model,
  not another free one.
- **No `models` fallback array.** OpenRouter supports it, but one request could then be served
  and billed by a model jev did not choose; explicit attempts keep cost and logs honest.
- **Prefer, not force.** With `prefer_free` free models come first and Jev is told to prefer
  them "unless the task is hard or earlier attempts failed"; Jev may still choose paid ones.

## 2026-10-12: Idea queue, scheduler and kanban board

Code: `core/queue.py`, `core/scheduler.py`, queue methods in `core/store.py` (migration 5),
`cli/{queue_cmds,scheduler_cmds,board_cmds}.py`; tests in `tests/unit/test_queue.py`,
`tests/unit/test_scheduler.py` and `tests/unit/test_cli_queue.py`. User docs:
[queue.md](queue.md).

- **Beyond PLAN.md**, which has one run per `jev run`. A `queue` table (migration 5) holds
  ideas with priority, position, the run flags (`plan_only`, `learning_profile`,
  `no_learning`, workspace `profile`) and a link to the run started for them.
- **Same code path as `jev run`.** The scheduler calls `run_cmds.start_run` with the item's
  flags; there is no second way to start a run. It runs in the foreground, one run at a time,
  with no daemon: easy to reason about, and Ctrl-C behaves like it does for `jev run`.
- **The scheduler never waits for a person.** A run that stops at `AWAITING_APPROVAL` or
  `NEEDS_HUMAN` leaves its item `waiting` and the scheduler moves on. The plan approval gate is
  unchanged (`require_plan_approval` still applies to queued runs).
- **Status follows the run.** Once an item has a run, its shown status is derived from the
  run's state at read time, so approving or resuming a run later is reflected without the
  queue being updated. The stored status is only the scheduler's last word.
- **Interrupts leave runs resumable** rather than finishing the current worker step: the run
  is interrupted where it is (as with `jev run`), its item becomes `waiting` with a
  `jev resume` note, and nothing restarts it automatically. The scheduler finds the run it
  started by idea text and start time when `start_run` did not return.
- **Lock file, not a database row** *(since #39 also held with `fcntl.flock`)*:
  `.jev/scheduler.lock` with pid and host, taken with `O_CREAT | O_EXCL`; a dead pid on this host (or an unreadable file) is stale and taken over;
  another host's lock is respected because its pid cannot be checked.
- **Daily budget** counts every run created today (UTC), not only queue runs, and is checked
  between runs, so it can be overshot by up to one run's cap.
- **No config schema changes**: scheduler settings are command flags.
- **Workspace profiles**: the item's `profile` is stored and passed to `start_run` as
  `profile=` when that function accepts it (`scheduler_cmds.apply_profile`, the hook for the
  profile feature). Until then an item naming a profile fails with a note instead of running
  with access it did not ask for. *(Superseded 2026-10-13: `start_run` always takes `profile`
  and `quality`; the scheduler passes them directly and profiles are applied, #31.)*
- **Board**: `build_board` is a pure function (tested without rendering); columns side by side
  from 140 columns, stacked panels below. Done shows the last 10 runs unless `--all`.

## 2026-10-12: Recursive learning (decision point D7, learner role)

Code: `core/learning.py` (profiles, lessons, selection, `resolve_learning`), `core/learner.py`
(scoring, D7, applying the learner's answer), `roles/learner/prompt.md`, `cli/learning_cmds.py`;
tests in `tests/unit/test_learning.py`, `tests/unit/test_cli_learning.py` and
`tests/integration/test_learning_pipeline.py`. User docs: [learning.md](learning.md).

- **Beyond PLAN.md.** PLAN.md has four roles and decision points D1-D6. Learning adds a fifth,
  read-only role, `learner`, and **D7** at the end of a run (DONE, FAILED, NEEDS_HUMAN,
  BUDGET_EXCEEDED; not CANCELLED): `learn` in {learn, skip} plus the learner's worker, model
  and effort. The rules dispatcher learns when the run had a plan revision, retry, escalation,
  reassignment, human hand-off, fix task, failure or NEEDS_HUMAN stop, or on a clean DONE
  while the profile has fewer than 3 lessons. Jev gets D7 in its instruction tables.
- **Off by default.** Learning costs an extra model call per run that has something to teach
  and sends lesson text to the workers, so it is opt-in: `[learning] enabled = false`.
  Precedence: run flag (`--learning NAME` / `--no-learning`) > repo settings stored by
  `jev learning` in a new `settings` table of state.db > `jev.toml` > defaults. A run records
  its profile (`runs.learning_profile`), so `resume` keeps it.
- **Profiles and scopes.** Repo-scope profiles live in state.db (migration 4); shared ones in
  `$XDG_DATA_HOME/jev/learning.db` (its own one-step migration list, same table SQL). Lesson
  ids carry the scope (`L12`, `S3`); names are unique across scopes. The shared file is only
  created when a shared profile is written.
- **Selection without embeddings.** Lessons for the role, not retired, ranked by tag overlap
  with the task card (files and title) and the idea, then score, then recency; capped by
  `max_lessons` and `max_chars`. Simple, deterministic and explainable from `jev log`, which
  lists the lesson ids injected into each attempt (`attempts.lesson_ids`).
- **Scoring** is harness-side and deterministic: per finished task, lessons in its coder or
  reviewer attempts get `uses + 1`, `score + 1` when the task was DONE on the first attempt,
  else `- 0.5`; a lesson at score -2 or below is retired. Planner/validator injections are not
  scored (no per-task outcome to attribute). Each task is scored once (`tasks.lessons_scored`).
- **The learner never sees transcripts**: only a compact run card (counts, titles, D5 actions,
  issues, short redacted failure excerpts) and up to 30 relevant lessons with ids. Its answer is
  limited (5 adds, 10 updates, 300 characters, valid roles), redacted, and may only update or
  retire lessons it was shown. The schema is lenient (plain strings) so one sloppy field does
  not void the reply; the limits are enforced when storing.
- **Never changes the run's outcome.** Every problem in the learning step (no learner worker,
  per-run budget spent, invalid reply, worker error) is recorded as `skipped: ...` in
  `runs.learning_outcome`. A resumed NEEDS_HUMAN run learns again when it ends.

## 2026-10-12: Agent tools per role (MCP servers, skills, plugins)

Code: `config/schema.py` (`ToolsConfig`), `workers/tools.py` (`AgentTools`, `tools_for_role`),
`workers/{claude_code,codex}.py`, `cli/doctor_cmd.py`; tests in `tests/unit/test_agent_tools.py`.
Findings and citations: [verification.md](verification.md) section 6.

- **Config, not code.** `[tools.mcp.<name>]`, `[tools.skills]` and `[tools.plugins.<name>]`
  define tools once; `[roles.<role>] mcp/skills/plugins` name them. Unknown names fail config
  validation. Secrets are only ever env var names (`env_from`, `bearer_token_env`,
  `headers_from_env`); literal `env`/`headers` and credential-looking args/URLs are rejected.
  Keys under the name tables (and header names) are exempt from the secret-key-name check.
- **`WorkerJob.tools`** carries the role's resolved tools (paths made absolute against the repo)
  from `Harness.run_worker` to every worker. Empty tools leave argv exactly as before.
- **Claude Code:** per-attempt `--mcp-config <tmp>/mcp.json --strict-mcp-config`, `mcp__<server>`
  added to `--allowedTools` (read-only roles included: the user opted in per role), skills copied
  under `<tmp>/skills/.claude/skills/` and passed with `--add-dir` (no per-skill flag exists),
  `Skill(<name>)` allowed, plugins via `--plugin-dir` plus their configured `allowed_tools`.
  Env values are written into the 0600 temp file because `${VAR}` expansion is only documented
  for `.mcp.json`; switch once verified.
- **Codex:** `-c mcp_servers.<name>.<key>=<TOML>` overrides with variable names only
  (`env_vars`, `bearer_token_env_var`, `env_http_headers`). Skills, plugins and SSE servers have
  no per-run form in Codex and are left out with a log note.
- **API worker:** no tools in this change (future work).
- **Redaction by name:** workers set `JEV_REDACT_NAMES` to the MCP variables, and
  `runlog.redact` scrubs those values (4+ chars) whatever the variables are called.
- **Doctor:** a row per role with tools, notes for tools a role's workers cannot use and for
  read-only roles with MCP servers, a warning per MCP server with an unset variable or a
  `command` not on PATH, a failure for a missing skill or plugin path.

## 2026-10-12: Live smoke test

Run on `examples/sample-repo` (copied to its own git repo) with Jev via native TypeSafe
(`jev-latest` resolving to `jev-1.13.0`), Claude Code 2.1.296 as planner and coder, Codex 0.162.1
as validator, the API worker disabled: `jev run --plan-only "Add a word_frequencies(text)
function with tests"`, then `jev approve`.

- The run reached DONE for $1.11 reported cost: planner $0.62, coder $0.49; the Codex validator
  reported no cost (as documented).
- Jev answered D1 (`clear=plan` 0.98, `effort=low` 0.94), D2 (`effort=low` 0.59), D3 (`approve`
  1.00), D5 (`done` 0.95) and D6 (`ship` 1.00). At D4 Jev's confidence was 0.42 < 0.55
  (`min_confidence`), so the rules dispatcher decided: `complexity=hard`, `effort=high`.
- The result branch had `word_frequencies` plus 5 new tests; all 10 tests passed. The user's
  branch was untouched and the worktrees were cleaned up.
- **Follow-up fixed:** the rules D4 heuristic rated this one-function task "hard" because it
  treated 5+ acceptance criteria as hard on their own, and planners routinely write 5-6
  criteria for small tasks. Now files are the main signal: hard needs 4+ files or 8+ criteria,
  trivial is at most 1 file and 2 criteria, everything else is standard
  (`dispatch/rules.py::complexity`, cases in `tests/unit/test_rules.py`). A two-file task with
  six criteria is now "standard"/medium.

## 2026-10-12: v0.1.0 polish (prompt errors, UTC times, Codex cost)

- **Bad prompt templates stop the run in NEEDS_HUMAN, resumably.** A missing or unreadable
  `prompt_file`, a Jinja2 syntax error, or any error while rendering (e.g. `UndefinedError`) is
  raised as `roles.PromptError` and turned into a NEEDS_HUMAN halt in one place
  (`Harness.prompt` / `Harness.check_prompts`); the reason names the role and the file.
  Previously a raw traceback escaped and left the run in PLANNING. Every custom template is
  loaded and parsed before each step, so missing files and syntax errors cost nothing; the coder
  prompt is rendered before the attempt is counted. Store schema v3 adds `runs.resume_state`:
  the state such a halt came from. `resume` accepts a NEEDS_HUMAN run only when it is set, and
  continues from there; every other NEEDS_HUMAN stop stays final (see "Not resumable yet"
  below). `jev doctor` fails a `prompt_file` that is missing or does not parse (undefined
  variables need a render, so they are only found at run time).
- **Times are shown in UTC.** Run ids were UTC but `jev status`/`jev log` printed local time.
  Both now print UTC with a `Z` suffix (`2026-10-12 15:30:12Z`), matching the run id.
- **Codex and the cost caps.** Codex reports no cost, so its attempts count $0 and the budget
  caps never stop them; they are bounded by `max_task_attempts` and `timeout_s` only.
  docs/security.md and the README say so, and `jev doctor` shows a `note` row when Codex is
  usable by some role.
- **Live tests** read only `JEV_LIVE_TESTS=1` (the old `BATON_LIVE_TESTS` alias is gone).

## 2026-10-12: M6 pipeline (dependent bases, integration, fix tasks, cleanup)

Code: `core/{workspace,integration,pipeline,tasks}.py`, `dispatch/{requests,rules}.py`; tests in
`tests/integration/test_integration_step.py`. Supersedes the M2 entry's "branches from HEAD"
and "fix_task -> NEEDS_HUMAN" points.

- **Start commit.** When a run first enters EXECUTING, HEAD is recorded as the git ref
  `refs/jev/<run_id>/start`, and every branch of the run grows from it, so resume rebuilds
  identical branches even if HEAD moved. The ref is the record (no store change) and is kept
  after cleanup (`git diff refs/jev/<id>/start jev/<id>/result`).
- **Dependent tasks** fork from `jev/<run_id>/base-<task>`: the start commit with the dependency
  branches merged in dependency order, built in a temporary worktree and removed straight after.
  A conflict leaves no base branch, sets the task NEEDS_HUMAN (naming the files) and stops the run
  in NEEDS_HUMAN before an attempt is spent. The reviewer's diff is relative to that base.
- **Integration.** Task branches merge in dependency order into `jev/<run_id>/result`, rebuilt
  from the start commit each time; then each distinct task `test_command`, then
  `[integration] test_command`; the first failure stops the tests.
- **D6** state gains `fix_rounds` and `max_fix_rounds`. Rules: conflicts -> `human`; failed
  tests -> `fix_task` while rounds remain, else `human`; otherwise `ship`. The harness enforces
  the bound and treats `fix_task` without a test failure as `human`. Jev's D6 instruction says
  the same.
- **Fix tasks.** `fix_task` appends `T<max+1>` "Fix integration failures (round k)", depending on
  every earlier task, with the failing command as its `test_command` and the last 2000 chars of
  its output (redacted) in the description. Rounds are counted from those titles, so a crash
  cannot double-count. The plan is written with `set_run_plan` (same `plan_round`) then
  `upsert_tasks`; an unfinished task seen at integration sends the run back to EXECUTING.
- **Cleanup on DONE** removes the run's worktrees (`git worktree remove --force`, then `prune`)
  and base branches; task and result branches stay. Other end states keep everything. Cleanup
  is idempotent, rejects unsafe run ids, only touches `<state_dir>/worktrees/`, and a failure is
  appended to the DONE reason rather than failing the run.
- **Config.** `[integration] test_command` (default none) and `max_fix_rounds` (default 2), and
  `[budget] test_timeout_s` (default 1200, per test command), reach `PipelineSettings` via
  `cli/wiring.py`. `test_timeout_s` sits in `[budget]` with the other limits because it applies
  to task tests too, not only integration.

## 2026-10-12: Jev and API wiring

Folds in the M3 (API worker), M4 (Jev) and M5 (Claude Code and Codex workers) notes. Code:
`dispatch/jev.py`, `workers/{api,claude_code,codex,subprocess_runner,summary}.py`,
`cli/wiring.py`; config: `[dispatcher.jev]`, `[workers.*]` (docs/config.md). Permission advice
and terms of use are in docs/security.md.

**Jev dispatcher**

- **Slug fix.** PLAN.md's `jev_model = "typesafe/jev-latest"` was wrong (missing `~`). The key
  is replaced by a `[dispatcher.jev]` table that maps 1:1 onto `JevSettings`, defined once in
  `config/schema.py`: `base_url`, `model = "~typesafe/jev-latest"`, `api_key_env`,
  `timeout_s = 30`, `min_confidence = 0.55`, optional `instructions`.
- **OpenRouter is the default path**; native TypeSafe (`https://api.typesafe.ai/v1`,
  `jev-latest`, `TYPESAFE_API_KEY`) takes the same request shape (`JevSettings.for_native()`).
- **System One endpoint, not chat completions.** `POST {base_url}/systemone` with
  `{state, model, questions}` (docs/verification.md section 1). `state` is
  `{"decision_point": {id, description}, **request.state}`.
- **Allowed answers as option -> description maps.** Every question is a `choice` whose
  `criteria` keys are exactly the allowed answers, each mapped to a short description for known
  values (efforts, complexity tiers, workers, D3/D5/D6 actions) or `null` (model slugs, anything
  unknown). Instructions: override `"D4.effort"`, override `"effort"`, built-in per point, built-in
  per question, generic sentence. `effort`/`complexity` are `choice`, not `score`; revisit if
  choices on ordered scales look poor. Pools over 255 options would get a 422 (falls back).
- **Single-option questions** are answered locally; if all are, no call is made and no key is
  needed.
- **Every problem raises `DispatchError`** and the guard falls back to rules for that decision:
  key unset; `state` + longest question over 32k tokens (estimated as chars / 3); 401/403 (no
  body echoed); 422 (server message, <= 300 chars); 429/529/5xx after one retry
  (`Retry-After` capped at 10 s, default 1 s); other statuses (incl. 402); timeout or network
  error (no httpx text); missing or disallowed answer; missing confidence. The key never appears
  in messages; error bodies have it replaced by `***`. The error body schema is unverified; the
  parser accepts `{"error": {"message"}}`, `{"error": "..."}`, `{"detail"}` or plain text.
- **Confidence gate.** If any answer's confidence is below `min_confidence`, the whole decision
  falls back (all-or-nothing; a partial merge would need the guard to combine two decisions).
- **`Decision.details`** (new, default `{}`) replaces `JevDispatcher.last_details`, which was not
  safe under concurrency. Jev stores `direct` (locally answered questions), `answers`
  (`{q: {choice, confidence, probabilities}}`) and `usage`. It is persisted inside
  `decision_json`, so no schema migration; `jev log` prints `effort=high (0.91)`. A rejected
  low-confidence decision keeps its confidences only in the fallback reason.
- **Cost** is `usage.cost` (OpenRouter), else `usage.input_tokens` x $0.042/M (Jev 1.13 list
  price, input only; native TypeSafe never returns a cost, so it is an estimate).
  `Decision.model` is the versioned id from the response.
- **Wiring.** `primary = "jev"` builds a `JevDispatcher` even when its key is unset; the guard
  falls back per decision and `jev run`/`approve`/`resume` print one warning. `fallback` is
  always rules for now. `jev doctor` shows the dispatcher, model, URL and whether the key
  variable is set (never its value), warning if not.

**API worker**

- **Endpoint.** `POST {base_url}/chat/completions` (OpenAI-compatible, default OpenRouter), one
  user message with the rendered prompt, key from `api_key_env` sent only as a Bearer header.
  Attribution headers `HTTP-Referer: PROJECT_URL` and `X-OpenRouter-Title`. `PROJECT_URL`
  (`https://github.com/Gane2506/jev-agent-orchestrator`) is a guess; fix it once the repo is public.
- **Roles and models.** Refuses `coder` (status `error`). Model is `job.model`, else
  `[workers.api] default_model` (new, unset by default), else status `error`. New
  `[workers.api] timeout_s = 600` bounds a whole attempt, retries included. Registered in the
  CLI whenever enabled; the M5 CLI workers plug in via `wiring.build_cli_workers` (empty for now).
- **Structured output.** `response_format: json_schema` with `strict: true` plus
  `provider: {require_parameters: true}`. The schema is post-processed: every object gets
  `additionalProperties: false`, `required` lists all properties, `default` is dropped, so
  defaulted fields are required on the wire; pydantic then validates against the real model
  (cross-field validators still apply). Other keywords (`pattern`, `minItems`) are kept; some
  providers may reject them under strict mode (unverified). Replies are parsed as JSON, else
  from the first fenced json block. The verification.md fallback to prompt-and-extract on a
  400 "unsupported parameter" is not implemented.
- **Effort** `low|medium|high` maps to `reasoning: {effort}` (other values not sent). Cost is
  `usage.cost`, tokens are `prompt/completion/total`; cost is kept even when validation fails.
- **Errors** (`classify_api_error`): 402 -> `quota_exhausted`; 429 with `limit_source` or quota
  wording (credit, insufficient, exhaust, spend/key limit, budget, per-day) -> `quota_exhausted`,
  other 429 -> `failure`; 403 with a guardrail/workspace/key budget `limit_source` ->
  `quota_exhausted` (deliberate deviation from "403 -> error"); other 401/403 -> `error`;
  408 -> `timeout`; 5xx -> `failure`; other 4xx -> `error`. Timeouts -> `timeout`, other httpx
  errors and a missing key -> `error`.
- **Retry** once, only for 429/503 classified `failure`, after `Retry-After` (<= 10 s) or 2 s;
  a longer or HTTP-date `Retry-After` means no retry.
- **Logging and redaction.** A sectioned log at `job.log_path` (request metadata, prompt, HTTP
  status per attempt, error bodies, usage, content, result; never headers). The log and every
  result summary are scrubbed of the literal key value (the variable may have any name) and then
  passed through `jev_agent.runlog.redact.redact`, now imported directly (the importlib shim and
  duplicate local regex are gone).
- **Live test** `tests/live/test_api_live.py` needs `JEV_LIVE_TESTS=1` (or `BATON_LIVE_TESTS=1`)
  and `OPENROUTER_API_KEY`; model `JEV_LIVE_API_MODEL`, default `openai/gpt-4o-mini`.

**Claude Code and Codex workers** (flags per docs/verification.md sections 2 and 3; Claude Code
2.1.114, codex-cli 0.162.1)

- **Subprocess runner.** `run_command` uses `create_subprocess_exec`, never a shell; the child
  gets its own process group, which gets SIGTERM then SIGKILL (5 s) on timeout. Missing or
  non-executable binaries return 127/126 instead of raising. The log is appended (agent call and
  `git status` share one file): argv, cwd, stdin, status, stdout, stderr, redacted; never the
  environment. The parent environment is passed through (the CLIs need HOME, PATH, their auth).
  Workers take an injectable `CommandRunner`.
- **Redaction** now calls `jev_agent.runlog.redact.redact` directly with `os.environ` merged with the
  child env; the lazy import and local regex are gone. To keep M5's coverage, runlog's
  secret-name rule widened from `_API_KEY$|TOKEN` to `KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL`.
- **Coder summary.** The coder ends with a fenced `jev-summary` JSON block (last one wins, via
  `roles.schemas.extract_coder_summary`); missing or invalid -> `error`. `files_changed` comes
  from `git status --porcelain --untracked-files=all` (renames, C-quoted paths), falling back to
  the block's list. Coder jobs ignore `output_schema`.
- **Claude Code** (`claude -p --output-format json --no-session-persistence`, prompt on stdin):
  coder `--permission-mode acceptEdits --allowedTools "Read Edit Write Glob Grep Bash(git status *)
  Bash(git diff *)"`; other roles `dontAsk`, read tools only, `--disallowedTools "Edit Write
  NotebookEdit"` and `--json-schema`. `plan` mode was rejected (its exit-plan flow targets
  interactive use). Effort outside low..max is dropped and logged. `structured_output` is
  validated, else the `result` text is parsed. `total_cost_usd` is an estimate (not real spend on
  subscriptions). Classification, in order: timeout; success; quota (`You've hit your ... limit`,
  `Credit balance is too low`, `Request rejected (429)`, `credits_required`, rejected
  `rate_limit_event`, `api_retry` rate_limit/billing_error); auth -> `error`;
  `error_max_structured_output_retries` -> `error`; `error_max_turns`, `error_max_budget_usd`,
  `error_during_execution`, other errors or non-zero exit -> `failure`; exit 0 without a result
  -> `error`. `error_max_budget_usd` is `failure`, not quota: it is our own cap, so reassigning
  would be wrong.
- **Codex** (`codex exec --json --ephemeral -C <workdir> -s <sandbox> -c approval_policy="never"
  ... -o <tmp>/last_message.txt -`): coder uses the configured sandbox, other roles `read-only`
  with `--output-schema` (strict form via `api.strict_json_schema`). `--skip-git-repo-check` only
  outside a git repo. Temp files live outside the workdir. Final text is the `-o` file, else the
  last `agent_message`; tokens are summed over `turn.completed`; `cost_usd` is always None.
  Classification: timeout; exit 0 with `turn.completed` and no `turn.failed` -> success; usage
  limit / `usage_limit_reached` / out of credits / spend cap / `insufficient_quota` -> quota; not
  logged in / 401 / invalid key -> `error`; `turn.failed`, `error` event or non-zero exit ->
  `failure`; exit 0 without a completed turn -> `error`.
- **Config and wiring.** `[workers.claude_code]` gains `permission_mode`,
  `read_only_permission_mode`, `allowed_tools`, `max_budget_usd`, `bare`, `extra_args` and
  `skip_permissions`; `[workers.codex]` gains `sandbox` and `extra_args`. `bypassPermissions`
  is not accepted as a `permission_mode`: the only way to it is the DANGEROUS, opt-in
  `skip_permissions`, which `jev doctor` warns about (as it does for
  `sandbox = "danger-full-access"` and "dangerous" `extra_args`). `max_budget_usd` defaults to
  `[budget] per_task_usd`, since `WorkerJob` has no budget field. Both workers are registered
  when enabled; `timeout_s` reaches `WorkerJob` through the harness.
- **Unverified.** Exit codes on quota/auth/turn failures are undocumented, so classification
  relies on text and events. Where Codex puts limit messages is inferred from source; auth
  strings for both CLIs are best guesses. Before 2.1.205 an invalid `--json-schema` was silently
  ignored (we fall back to parsing text). `--allowedTools` is one space-separated argument and
  relies on the CLI parsing `Bash(git status *)`. Fixtures are synthesized from docs; recapture
  them with `tests/live/test_cli_workers_live.py`.

## 2026-10-12: M2 pipeline (state machine, rules dispatcher, fake worker)

- **Worker interface.** `Worker.run` returns `WorkerOutput(result, parsed)` rather than a bare
  `WorkerResult`: `parsed` carries the validated structured output (a `Plan`, `ValidationResult`,
  `ReviewResult` or `CoderSummary`) so the harness never re-parses text. `WorkerJob` carries
  `log_path` (the harness decides where logs go, `.jev/runs/<run_id>/<task_id|run>/<attempt>.log`)
  plus `task_id` and `attempt` for logging and for scripting fakes. If a JSON role reports
  success without valid parsed output, the harness records it as `error`.
- **Reviewer stage is opt-in** (`[review] enabled = false`), answering PLAN.md section 17. No
  decision point chooses the reviewer, so it uses the role's first worker and model and its middle
  effort. Revisit when the reviewer becomes a standard stage (v0.3).
- **Question names** for D1-D6 are fixed in `dispatch/requests.py`. An empty model pool (or
  effort list) is offered as the single answer `"default"`: let the worker use its default.
- **Planner and validator failures** (no valid plan or verdict) stop the run in NEEDS_HUMAN, as
  do D1 `ask_human`, D3 `escalate_human` and running out of plan rounds. A dependency cycle in a
  plan stops the run in FAILED.
- **Budgets.** No worker is launched once the run or task has spent its cap; work already
  running finishes, so spend can end slightly above a cap. Run cap -> BUDGET_EXCEEDED. Task cap
  -> task FAILED and run FAILED. Task spend is added to the run total in one transaction
  (`Store.add_spend`), and dispatcher costs count too.
- **Attempts.** `max_task_attempts` counts every started attempt, including one interrupted by a
  crash. On `resume`, unfinished attempts are closed as `error` ("interrupted") and the task's
  current attempt is run again as a new attempt. Retry keeps the assignment; escalate raises the
  effort one level, then moves to the next model; reassign moves to the next worker in the role's
  list. Run-level attempts (planning, validation) share one counter per run.
- **Task branches** are created from the repo's HEAD, not from their dependencies' branches, as
  PLAN.md section 10 says. Dependent tasks therefore do not see each other's changes until
  integration. Revisit in M5/M6 (branching from the merged dependencies is the likely fix).
- **Integration (minimal).** After all tasks are DONE, task branches are merged in dependency
  order into a fresh `jev/<run_id>/result` branch (in a worktree under `.jev/worktrees/`), each
  distinct task test command runs there, and D6 is asked. `ship` -> DONE; `fix_task` and
  `human` -> NEEDS_HUMAN for now (automatic fix tasks are M6 work). jev commits with a fixed
  local identity (`jev <jev@localhost>`, unsigned). Nothing is pushed.
- **Store schema v2** adds `runs.reason`, `runs.validation_json`, and `tasks.worker/model/effort/
  note` so status shows why a run stopped and resume knows each task's next assignment.
- **Partial role tables keep role defaults.** Previously `[roles.coder] models = [...]` alone
  silently reset `workers` to `[]`; per-role defaults are now layered under each role table.
- **CLI in M2** uses the rules dispatcher and has no real workers; `JEV_FAKE_WORKERS=1` is a
  documented test hook that registers fakes. `jev approve` without workers only marks the plan
  approved (state EXECUTING); `jev resume` then continues once workers exist.
- **Not resumable yet:** NEEDS_HUMAN, FAILED and BUDGET_EXCEEDED runs are terminal; `resume`
  refuses them (raising a cap and resuming is future work). Exception since v0.1.0: a
  NEEDS_HUMAN stop caused by a bad prompt template (see the v0.1.0 polish entry).

## 2026-10-12: Config precedence and doctor severity

- PLAN.md section 8 says `jev.toml` is "overridable by" the user config, which reads as the user
  file winning. We chose the opposite, the usual convention: built-in defaults < user config
  (`$XDG_CONFIG_HOME/jev/config.toml`, else `~/.config/jev/config.toml`) < repo `jev.toml`.
  The repo file is the most specific, so it wins. Tables are deep-merged; lists and scalars
  are replaced whole.
- Workers are fixed fields (`claude_code`, `codex`, `api`) rather than a free-form table, so a
  misspelled worker table is an error. Roles may only name these workers.
- `jev doctor` reports a missing worker CLI or unset API key as a warning, and fails only when a
  role has no usable worker (or git/config checks fail). Default config enables both CLIs and
  most users will have only some of them.

## 2026-10-12: Project name

*Superseded by the "Rename jao → jev" entry above; kept as written for history.*

- `baton` is taken on PyPI. The project is named **Jev Agent Orchestrator**.
- PyPI distribution: `jev-agent-orchestrator`.
- Import package: `jao`.
- CLI: `jao` (short), with `jev-agent-orchestrator` as an alias pointing to the same entry point.
- Config file `jao.toml`, state folder `.jao/`, user config `~/.config/jao/config.toml`,
  branches `jao/<run_id>/<task_id>`. Everywhere PLAN.md says `baton`, read `jao`.

## 2026-10-12: Run-log package name

PLAN.md's layout has a `logging/` package. It is named `runlog/` instead so it can never be
confused with, or shadow, the standard library `logging` module.

## 2026-10-12: License

Apache-2.0, as PLAN.md proposed. Confirmed with the maintainer.
