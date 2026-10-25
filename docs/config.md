# Configuration

`jev init` writes a commented `jev.toml` to the root of your repository. Every key is optional;
an empty file (or no file) gives the defaults below. The template sets no values: each key is a
commented-out line showing its built-in default (`# per_run_usd = 5.00`), so your user config
still applies until you uncomment a line. A `jev.toml` written by an older `jev init` sets every
key to the default of that time and so overrides your user config; delete the lines you did not
mean to set.

## Where config comes from

Layers, lowest to highest precedence:

1. Built-in defaults.
2. User config: `$XDG_CONFIG_HOME/jev/config.toml`, or `~/.config/jev/config.toml` if
   `XDG_CONFIG_HOME` is unset.
3. Repo config: `jev.toml` at the repository root.

Tables are merged key by key, so a repo file that sets only `[workers.codex] timeout_s` keeps the
`command` from your user config. Lists and plain values are replaced whole. Missing files are
skipped.

Unknown keys are errors (to catch typos). Errors name the file and the field, for example:

```
/path/to/repo/jev.toml: budget.per_task_usd: must be greater than 0
```

A repo file may not change the keys that decide where API keys go and what agents can run or
reach unless your user config trusts that repository: see [`[trust]`](#trust-user-config-only).

The dashboard's Settings page ([dashboard.md](dashboard.md)) can edit common keys of the repo's
`jev.toml` (dispatcher, budgets, approval, review, quality level, ship, learning,
`prefer_free`, and each role's `workers` and `models`): it shows a diff, checks the new file
like `jev` loads it, keeps comments and layout, and never writes guarded keys, `[trust]` or
your user config. The page opens on a **beginner view** — the ten settings most people change
(decision maker, spending caps, quality level and its score slider, plan approval, review,
learning, shipping and free models) — with an **Advanced** toggle revealing every other
setting. Each row carries a one-line plain-language explanation.

Run `jev doctor` to check that config loads, which worker CLIs are on `PATH`, which API key
environment variables are set, and that every `prompt_file` exists and parses.

## Secrets

Config files never hold secrets. Any key whose name ends in `key` or contains `token`, `secret`
or `password` is rejected, in any table (names you choose for MCP servers, skills, plugins and
HTTP headers are exempt). Put the secret in an environment variable and name the variable with
`api_key_env` (or, for MCP servers, `env_from`, `bearer_token_env` and `headers_from_env`).

## `[trust]` (user config only)

A `jev.toml` committed to a repository you cloned could otherwise point the API worker at
someone else's host with your `GITHUB_TOKEN` as the key, or give an agent your servers. So a
repo file may change these **guarded keys** only when your user config trusts the repository:

- `[dispatcher.jev]` and `[workers.api]` `base_url` and `api_key_env`;
- `[workers.claude_code]` `command`, `extra_args`, `permission_mode`,
  `read_only_permission_mode`, `allowed_tools`, `skip_permissions`, `bare`;
  `[workers.codex]` `command`, `extra_args`, `sandbox`;
- `[tools]`, `[servers]`, `[browser]`, `[profiles]`, and `servers`, `mcp`, `skills`, `plugins`
  under `[roles.<role>]`.

A repo value equal to what the defaults and your user config already give is always accepted.
Otherwise loading fails with an error that names each key and the `[trust]` lines to add.

| Key | Default | Meaning |
|---|---|---|
| `repos` | `[]` | Repositories whose `jev.toml` may change guarded keys: absolute paths (`~` allowed), or `"*"` for every repository. Relative entries resolve against the repo being checked, so `"."` trusts the repo itself wherever you run from. |

```toml
# ~/.config/jev/config.toml
[trust]
repos = ["~/code/my-app"]
```

A repository's `jev.toml` cannot contain `[trust]`.

## `[dispatcher]`

| Key | Default | Meaning |
| --- | --- | --- |
| `primary` | `"consensus"` | Dispatcher asked first: `"consensus"`, `"jev"` or `"rules"`. |

With `primary = "consensus"` (the default), Jev — the main orchestrator — makes every
decision. For code work, the Coding Decision Agent ([`[code_orchestrator]`](#code_orchestrator))
grades diffs and routes coding tasks; it never merges, ships, or approves a plan on its own.
A question Jev cannot settle confidently stops the run in `NEEDS_HUMAN` and `jev resume`
continues it once you have looked. The consensus dispatcher never falls back to rules, so a
missing key, a network error or an answer outside the allowed values also stops the run
instead of being decided without Jev. The saved decision records Jev's vote and the reason
of every unsettled round. Questions with a single allowed value are not asked.

> The former second vote is removed (issue #193): there is no `[dispatcher.laya]` table
> any more — see [Removed: `[dispatcher.laya]`](#removed-dispatcherlaya).

With `primary = "jev"`, every decision goes to Jev first. If the call fails (key unset, network
or HTTP error, an answer outside the allowed values, low confidence, oversized state), the rules
dispatcher answers that decision instead, and `jev log` shows the reason after `FALLBACK:`. If
the key variable is unset, `jev run`, `approve` and `resume` print one warning at the start and
every decision uses rules. With `primary = "rules"`, only the deterministic rules decide and no
model is called for decisions. `jev doctor` reports the dispatcher and whether its key is set.

With `"jev"` and `"rules"`, the rules dispatcher is always the fallback. The old `fallback` key
is deprecated: `fallback = "rules"` is still accepted and ignored, any other value is an error.

Plan approval follows the dispatcher: see [`[approval]`](#approval).

## `[dispatcher.jev]`

How to reach Jev (TypeSafe's System One API). The defaults call it through OpenRouter.

| Key | Default | Meaning |
| --- | --- | --- |
| `base_url` | `"https://openrouter.ai/api/v1"` | API base URL; requests go to `{base_url}/systemone`. |
| `model` | `"~typesafe/jev-latest"` | Jev model slug. The `~` means "latest"; pin a version with e.g. `"typesafe/jev-1.13"`. |
| `api_key_env` | `"OPENROUTER_API_KEY"` | Name of the environment variable holding the key. Must be a valid variable name. |
| `timeout_s` | `30` | Timeout in seconds (> 0) for the whole Jev call, the retry and its backoff included. One retry is made on 429/5xx or a connect error. |
| `min_confidence` | `0.55` | Between 0 and 1. If Jev's confidence on any answer is below this, the whole decision falls back to rules. |
| `instructions` | `{}` | Optional table replacing the instruction sent for a question. Keys are `"<point>.<question>"` (e.g. `"D4.effort"`) for one decision point or `"<question>"` (e.g. `model`) for all of them. Unknown keys are an error. Questions: D1 `clear`, `worker`, `model`, `effort`; D2 `worker`, `model`, `effort`; D3 `verdict`; D4 `complexity`, `worker`, `model`, `effort`; D5 and D6 `action`; D7 `learn`, `worker`, `model`, `effort`; D8 `worker`, `model`, `effort`. |

To call TypeSafe directly instead of through OpenRouter:

```toml
[dispatcher.jev]
base_url = "https://api.typesafe.ai/v1"
model = "jev-latest"
api_key_env = "TYPESAFE_API_KEY"
```

Native TypeSafe does not report a cost, so jev estimates it from input tokens at the list price.

`jev log` shows Jev's confidence per answer, e.g. `effort=high (0.91)`. Answers with no
confidence were either given by the rules dispatcher or had only one allowed value (Jev is not
asked those).

## Removed: `[dispatcher.laya]`

Removed (issue #193). The Laya second vote and `jev laya tune` are gone: delete any
`[dispatcher.laya]` table (including `[dispatcher.laya.weights]`) from `jev.toml` and
from your user config. Jev alone decides as main orchestrator; code grading and
routing now belong to the Coding Decision Agent — see
[`[code_orchestrator]`](#code_orchestrator) below.

## `[code_orchestrator]`

The Coding Decision Agent as orchestrator for code work (issue #187, see
[docs/code-orchestrator.md](code-orchestrator.md)). Jev stays the main orchestrator; this
section only covers how code is graded and routed. Off by default: enabling it without the
model installed grades nothing (the quality gate records the grade as unavailable, it does
not block), and `jev code status` says exactly what is missing.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Grade task diffs at the quality gate and add the routing hint to D4 requests. |
| `model_id` | `"S4MPL3BI4S/Coding_Decision_Agent"` | Hugging Face id of the Coding Decision Agent checkpoint. |
| `confidence_threshold` | `0.7` | Calibrated `answer_confidence` a merge action needs to be trusted; below it the gate asks a human instead of merging or rejecting. |
| `max_diff_chars` | `7000` | Characters of a diff kept before grading (the model sees 2048 tokens; longer diffs are compacted around the changed lines). |

```toml
[code_orchestrator]
enabled = true
confidence_threshold = 0.7
```

## `[budget]`

All values must be greater than 0, and `per_task_usd` must not exceed `per_run_usd`.

| Key | Default | Meaning |
| --- | --- | --- |
| `per_run_usd` | `5.0` | Hard cost cap for one run (one idea). |
| `per_task_usd` | `1.5` | Hard cost cap for one task. |
| `max_plan_rounds` | `3` | Planner/validator rounds before a human is asked. |
| `max_task_attempts` | `3` | Attempts per task before escalation. |
| `test_timeout_s` | `1200` | Timeout in seconds for each test command: a task's own `test_command` and the integration tests. |

## `[integration]`

After every task is done, jev merges the task branches in dependency order into a local
`jev/<run_id>/result` branch, runs each distinct task `test_command` there, then
`test_command` below. The first failure stops the tests; then the D6 decision ships, adds a fix
task, or asks a human.

| Key | Default | Meaning |
| --- | --- | --- |
| `test_command` | unset | Full test suite to run on the result branch, e.g. `"uv run pytest -q"`. Run through the shell. |
| `max_fix_rounds` | `2` | Fix tasks D6 may add for failing integration tests before a human is asked (>= 0). Merge conflicts always go to a human. |

## `[workspace]`

jev commits each task attempt and merges task branches in git worktrees under `.jev/worktrees/`.
Every git call has a timeout (1 minute; 10 minutes for commits, merges, worktree checkouts and
pushes), so a hook or lock that hangs stops the run with an error instead of hanging it. A
failed commit stops the run in NEEDS_HUMAN: fix the cause, then `jev resume`.

| Key | Default | Meaning |
| --- | --- | --- |
| `run_hooks` | `true` | Run the repository's git hooks (pre-commit, commit-msg, ...) on jev's commits and merges. `false` passes `--no-verify`. |

## `[approval]`

| Key | Default | Meaning |
| --- | --- | --- |
| `require_plan_approval` | unset | Stop for human approval after the plan is validated. Unset follows the dispatcher: off with `primary = "consensus"`, on with `"jev"` and `"rules"`. `true` or `false` always wins. |

Under `consensus`, Jev's D3 verdict is the gate: a plan Jev approves goes straight to
execution, and a plan Jev cannot settle stops the run in `NEEDS_HUMAN`. Set `require_plan_approval = true` to also stop for yourself after every
validated plan. `--plan-only` always stops at the plan, whatever the setting.

## `[autonomy]`

| Key | Default | Meaning |
| --- | --- | --- |
| `mode` | `"standard"` | How much jev acts on its own. `"standard"`: you approve the plan once, then it builds alone (today's behavior). `"medium"`: like `"standard"` but scoped to `project_folder`. `"ask_every_step"`: it asks before every step. `"autonomous"`: it never stops for approval. `"manual"`: it only acts when you tell it to. |
| `access_tier` | `"basic"` | What agents may touch. `"basic"` is the safest: work inside the repo, read-only outside it. `"medium"` additionally allows writes inside `project_folder`. `"unrestricted"` removes the limits (agents run any command with your privileges) and needs `unrestricted_acknowledged = true`; never pick it casually. |
| `project_folder` | unset | The folder the `"medium"` access tier may change. Unset means the repository root. The filesystem root (`/`) is rejected: it would scope the medium tier to the whole machine. |
| `unrestricted_acknowledged` | `false` | Explicit acknowledgment that you understand what `"unrestricted"` allows. The config is rejected while the tier is `"unrestricted"` and this is unticked; lower the tier before unticking it. |

`mode` is one of the eleven beginner settings: the dashboard's Settings › The basics shows it as
a plain-language picker ("How much should jev ask you?"). The access tier lives under Advanced
because it is safety-sensitive; the dashboard shows a warning and requires the acknowledgment
box before `"unrestricted"` can be saved, and the config itself is rejected without it.

Three "medium"s, not one: `[autonomy].mode = "medium"` is the folder-scoped run mode; `[servers.<name>].access = "medium"` is the limited-writes server tier; the historical default behavior is named `"standard"`.

## `[review]`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | unset | After a task's tests pass (and the quality gate, if on), have the reviewer check its diff. A `revise` verdict counts as a failed attempt (retry, escalate, ...). Unset follows the dispatcher: on with `primary = "consensus"`, off with `"jev"` and `"rules"`. `true` or `false` always wins. |

Before each review, decision point D8 picks the reviewer's worker, model and effort from
`[roles.reviewer]`, seeing the task card, the attempt number, the test and quality results, the
diff's size and the coder's assignment. The rules dispatcher answers as before D8 existed: the
first worker and model and the middle effort. Set `[roles.reviewer]
prefer_different_vendor_than = "coder"` to have D8 prefer a different vendor than the attempt's
coder, as D2 does for the validator.

Under `consensus` the review's verdict is part of the D5 state, so a task is done only when Jev
accepts an attempt the reviewer has checked. Each review adds one decision and one reviewer
call per attempt; set `enabled = false` to skip both.

## `[quality]`

The optional quality and security gate ([quality.md](quality.md)): after each task's coder and
tests, and on the result branch at integration, it runs lint, tests, scanners and an AI code
review, and blocks work that fails. Off by default.

| Key | Default | Meaning |
| --- | --- | --- |
| `level` | `"none"` | `"none"` (task test commands only), `"basic"`, `"medium"`, `"production"` or `"scalable"`. Levels are cumulative. Override per run with `jev run --quality LEVEL` (also `jev resume`, `jev queue add` and `/quality` in `jev chat`). |
| `test_command` | unset | The full test suite the gate runs, e.g. `"uv run pytest -q"`. Unset: `[integration] test_command`, else detected (pytest, `npm test`, `go test ./...`, `cargo test`). |
| `min_coverage` | unset | Minimum line coverage in percent (0-100) on the result branch. Unset: 60 at `medium`, 80 at `production` and `scalable`. Only blocks from `production`. |
| `required_tools` | `[]` | Tool names (e.g. `["ruff", "gitleaks"]`) that must be installed: a missing one fails its check, which blocks. Other missing tools only skip their check. `jev quality checks` lists the tool names. |
| `timeout_s` | `600` | Timeout in seconds (> 0) for each tool command. |
| `score_threshold` | `85` | Minimum review score (0-100) for the work to ship; a result scored below it goes back for rework instead of shipping. The dashboard's Settings page shows it as a slider. |
| `score_max_retries` | `2` | How many times a result below the score threshold goes back for rework before the run stops for a human; the run never ships below the threshold. |
| `score_skip_human` | `false` | Skip the ship score gate for human-authored code (your call); the score is still recorded. AI-generated code is always gated. |

Precedence for the level: the run's `--quality` flag > the queue item's `--quality` > this key.
The level a run used is stored with it. The AI review uses `[roles.code_reviewer]`.

## `[learning]`

Recursive learning: lessons distilled from past runs are added to future prompts. See
[learning.md](learning.md). Off by default because, when on, each run ends with one extra
model call (the learner) whenever there is something to learn.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Use and grow lessons in this repo's runs. `jev learning enable` / `disable` store an on/off setting in `.jev/state.db` that wins over this key. |
| `profile` | `"default"` | The active profile: the named lesson store runs use. `jev learning use NAME` stores a setting that wins over this key. A profile named here is created (repo scope) on first use. Letters, digits, `-` and `_`. |
| `max_lessons` | `8` | Most lessons added to one prompt (>= 0). |
| `max_chars` | `2000` | Most characters of lesson text added to one prompt (>= 0). |

Precedence: `jev run --learning NAME` / `--no-learning` > settings from `jev learning` > this
table > the defaults. The learner's workers and models are set in `[roles.learner]`.

## `[cache]`

Exact-match cache for dispatcher decisions (phase 1) and read-only role calls (phase 2;
issue [#10](https://github.com/Gane2506/baton/issues/10)). A cache hit serves the settled
answer without calling the model again: the decision/attempt is still recorded (with a
`cached` badge in `jev log` and on the dashboard), it costs $0, and the saved amount shows
on the Spend page (`jev cache stats` for the totals, or the Cache section of the dashboard
Tools page, which also clears entries and runs calibration).

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Master kill-switch. `jev run --no-cache` / `jev resume --no-cache` disables it for one run. |
| `ttl_hours` | `168` | Decision entries expire after this long (roles/chat have overrides below). |
| `max_entries` | `10000` | LRU eviction past this many entries. |
| `max_mb` | `256` | Evict least-recently-hit entries past this size. |
| `cache_roles` | `["planner", "validator", "reviewer", "code_reviewer"]` | Roles whose outputs may be cached. |
| `cli_workers` | `false` | Also cache role calls made through `claude_code`/`codex` workers. |
| `chat` | `false` | Opt-in exact-match caching for `jev chat` (phase 4; not implemented yet). |
| `d6_decisions` | `false` | Cache D6 ship/fix decisions. Default off: a wrong cached `ship` vote is costly. |

Hard rules, enforced in code rather than config: the **coder** and **learner** roles are
never cached, only `success` results are stored, and rules-fallback answers are never
stored. A repo change (HEAD, diff, untracked files) or a config change (`min_confidence`,
model, prompt template, lessons) is automatically a different key, so stale
answers can't be served after the code or settings moved. `jev cache clear` flushes
entries (`--type decisions|roles`, `--older-than HOURS`).

## `[cache.ttl_hours_overrides]`

Per-kind entry lifetimes, in hours (overriding `[cache] ttl_hours`).

| Key | Default | Meaning |
| --- | --- | --- |
| `roles` | `24` | Role-output entries expire after this long. |
| `chat` | `1` | Chat entries expire after this long (phase 4; not implemented yet). |

## `[cache.semantic]` (phase 3, default off)

Similarity matching for near-duplicate calls. Ships with the `Embedder` protocol and the
stdlib SimHash backend. Nothing is served semantically by default: `mode = "shadow"`
measures agreement against live calls without serving (`jev cache calibrate` picks the
thresholds from stored history first; `mode = "serve"` turns serving on for decisions —
role outputs stay exact-match until shadow data proves them safe). Live semantic serving is
gated on sustained ≥ 0.99 agreement (`min_agreement`); below it, serving auto-disables.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Serve near-duplicate calls from the cache. |
| `embedder` | `"simhash"` | Similarity backend: `"simhash"` (stdlib) or `"openrouter"` (embeddings API; needs `OPENROUTER_API_KEY`). |
| `decision_threshold` | `0.98` | Similarity floor for decisions (see `jev cache calibrate`). |
| `role_threshold` | `0.98` | Similarity floor for role outputs (shadow-measured before any serving). |
| `chat_threshold` | `0.98` | Similarity floor for chat turns (phase 4). |
| `mode` | `"off"` | `"off"`, `"shadow"` (measure only, never serve), or `"serve"`. |
| `min_agreement` | `0.98` | Live-sampling floor: below it, semantic serving auto-disables. |

## `[dashboard]`

The web dashboard's own display preferences (issue
[#116](https://github.com/Gane2506/baton/issues/116)). Read from the **user
config only**: a repo's `jev.toml` cannot override the user's own display
choice.

| Key | Default | Meaning |
| --- | --- | --- |
| `ui_style` | `"bento"` | The dashboard's UI style: one of the 16 styles in the sidebar Style picker (Bento Grid, Minimalism, Flat Design 2.0, Skeuomorphism, Neumorphism, Glassmorphism, Claymorphism, Cards-Based UI, Split-Screen Design, Brutalism, Neo-Brutalism, Bauhaus Style, Hand-Drawn, Cyberpunk, Y2K/Vaporwave, Pixel Art UI). An unknown value falls back to Bento Grid. |

## `[events]`

The event-sourced execution log: every run mutation is dual-written as an append-only event
in `run_events`, which powers `jev events`, `jev replay` and `jev bisect`. See
[events.md](events.md). On by default; the snapshot tables the pipeline reads are unaffected
either way.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Write the event log. Turn off if the log ever misbehaves; `jev replay` / `jev bisect` then report the log as disabled. |

## `[ship]`

`jev ship RUN` pushes a DONE run's result branch to a new branch on your remote and opens a
GitHub pull request with your own `gh` login. With shipping on, a D6 "ship" vote also ships the
run by itself: `after_approval = "merge"` opens the PR ready, waits for its checks and merges it;
`"ask"` waits for you to approve, modify or stop first. See [ship.md](ship.md) and
[security.md](security.md#shipping-prs). Off by default; jev never force-pushes.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Allow `jev ship` (and the dashboard's "Ship as PR" button) in this repo. |
| `remote` | `"origin"` | Git remote to push to. Its URL gives the GitHub `owner/repo` (else `gh repo view`). |
| `base` | `""` | Base branch of the PR. Empty: the repo's default branch (`refs/remotes/<remote>/HEAD`, else `gh repo view --json defaultBranchRef`), else the branch checked out in the repo. |
| `branch_prefix` | `"jev/"` | The pushed branch is `<branch_prefix><run_id>` (`-2`, `-3`, ... for `--force-new`). |
| `draft` | `true` | Open the PR as a draft. |
| `labels` | `[]` | Labels to add (`gh pr create --label`); they must already exist in the repo. |
| `require_quality_pass` | `true` | Refuse a run whose latest quality gate (any task or the integration) blocked. |
| `after_approval` | unset | What a run does when D6 votes "ship": `"merge"` ships and merges without asking, `"ask"` waits in `AWAITING_SHIP` for `jev ship approve RUN`, `jev ship modify RUN` or `jev ship stop RUN` (or the dashboard). Unset: `"merge"` when `[dispatcher] primary = "consensus"`, else `"ask"`. `jev run --auto-ship` / `--ask-before-ship` (and the queue's) override it per run. Needs `enabled = true`; otherwise D6 ends the run DONE as before. |
| `merge_method` | `"squash"` | How the PR is merged: `"squash"`, `"merge"` or `"rebase"` (`gh pr merge --squash` etc.). |
| `require_checks` | `true` | A PR that still reports no checks after `checks_grace_s` stops for you; `false` merges it without CI. |
| `checks_grace_s` | `120` | Seconds (>= 0) to wait for a new PR's checks to appear. |
| `checks_timeout_s` | `3600` | Seconds (> 0) to wait for the checks to finish before stopping for you. |
| `checks_poll_s` | `30` | Seconds (> 0) between two looks at the PR's checks. |

## `[models]`

Free OpenRouter models for roles that can use the api worker. See [Free models](#free-models)
below for how they are chosen and what happens when one hits its rate limit.

| Key | Default | Meaning |
| --- | --- | --- |
| `prefer_free` | `false` | Offer free models first to every role that may use the `api` worker. A role's own `prefer_free` overrides this. A role whose pool has no free model gets the `"free"` expansion added in front of it. |
| `free_models_limit` | `3` | How many cached free models one `"free"` entry in a role's `models` expands to (>= 0). |
| `cache_ttl_hours` | `24` | Age (> 0) after which the cached model list (`.jev/cache/openrouter-models.json`) is fetched again, at the start of a run that uses free models or by `jev models`. |

## `[workers.claude_code]` and `[workers.codex]`

Local agent CLIs. Each is used when `enabled = true` and its `command` is found on `PATH`;
otherwise a run leaves it out with a warning at the start, and `jev doctor` warns too (see
[Worker readiness](#worker-readiness)). See [security.md](security.md) for the permission model.

Shared keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Whether this worker may be used. |
| `command` | `"claude"` / `"codex"` | Executable name or path. |
| `timeout_s` | `1200` | Per-attempt timeout in seconds (> 0). The process group is killed on timeout. Codex reports no cost, so for Codex this and `[budget] max_task_attempts` are the only limits. |
| `extra_args` | `[]` | Arguments appended to every call, e.g. `["--max-turns", "40"]`. `jev doctor` warns about any containing "dangerous". |

`[workers.claude_code]` only:

| Key | Default | Meaning |
| --- | --- | --- |
| `permission_mode` | `"acceptEdits"` | `--permission-mode` for the coder, which works in its own worktree. One of `acceptEdits`, `auto`, `default`, `dontAsk`, `plan`. `bypassPermissions` is rejected: use `skip_permissions`. |
| `read_only_permission_mode` | `"dontAsk"` | Mode for the planner, validator and reviewer, which also only get read tools and have write tools disallowed. One of `dontAsk`, `plan`, `default`. |
| `allowed_tools` | unset | The coder's `--allowedTools`. Unset keeps the built-in list: `Read Edit Write Glob Grep Bash(git status *) Bash(git diff *)`. To let the coder run tests, add a narrow pattern such as `Bash(uv run pytest *)`; avoid bare `Bash`. |
| `max_budget_usd` | unset | Claude Code's own per-attempt cap (`--max-budget-usd`, > 0), a second cap behind jev's budget. Unset means `[budget] per_task_usd`. |
| `bare` | `false` | Run with `--bare`, which requires `ANTHROPIC_API_KEY`. |
| `skip_permissions` | `false` | **DANGEROUS.** Runs the coder with `--dangerously-skip-permissions`: it can run any command with your privileges. Opt-in only, for a throwaway VM or container without secrets or network. Read-only roles never get it. `jev doctor` warns while it is on. |

`[workers.codex]` only:

| Key | Default | Meaning |
| --- | --- | --- |
| `sandbox` | `"workspace-write"` | `--sandbox` for the coder: `read-only`, `workspace-write` or `danger-full-access` (no sandbox; `jev doctor` warns). Other roles always run `read-only`. |

## `[workers.api]`

An OpenAI-compatible HTTP API.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Whether this worker may be used. |
| `base_url` | `"https://openrouter.ai/api/v1"` | API base URL. |
| `api_key_env` | `"OPENROUTER_API_KEY"` | Name of the environment variable holding the key. Must be a valid variable name. |
| `default_model` | unset | Model used when the role's `models` pool is empty. If unset, `api` is not offered to a role whose pool is empty (and has no free models). |
| `timeout_s` | `600` | Per-attempt timeout in seconds (> 0), retries included. |

The API worker runs the planner, validator and reviewer roles (it cannot edit files, so never
the coder). It is used when `enabled = true` and the `api_key_env` variable is set, and only
for roles it has a model for; see [Worker readiness](#worker-readiness).

## `[dispatcher.cost]`

Cost-aware routing and pre-run cost estimates. Routing: when `prefer_cheaper_leg` is on
(the default), the cheaper Jev leg (native TypeSafe vs OpenRouter, by the prices below)
goes first when both are reachable. Cost never removes a leg -- the other still serves
on failure and the rules dispatcher still backs up both -- and never lowers the
confidence gate. Every cost-influenced ordering is logged and recorded in the
decision's `routing_reason`. Estimates: `jev run` shows an estimated total-cost range
before the plan approval gate, from the newest ended runs' per-phase spend (25th to
75th percentile) once at least `min_history_runs` runs exist, else model pricing times
expected tokens with an explicit wide band. `jev approve` refuses (unless `--force`)
when the estimate's high end reaches `refuse_fraction` of `[budget] per_run_usd`, and
warns from `warn_fraction` up.

| Key | Default | Meaning |
| --- | --- | --- |
| `prefer_cheaper_leg` | `true` | Try the cheaper leg first when both are reachable. |
| `native_price_per_million` | `0.042` | USD per 1M input tokens for one Jev decision call on the native TypeSafe leg (output tokens are free). |
| `openrouter_price_per_million` | `0.042` | USD per 1M input tokens for one Jev call through OpenRouter. |
| `min_history_runs` | `3` | Ended runs needed before the estimate trusts history; below this it uses pricing. |
| `history_runs` | `20` | How many of the newest ended runs feed the estimate. |
| `tokens_per_decision` | `8000` | Expected input tokens of one Jev decision call (pricing fallback). |
| `tokens_per_task` | `100000` | Expected input tokens of one task's worker calls (pricing fallback). |
| `worker_price_per_million` | `3.0` | USD per 1M input tokens for worker calls (pricing fallback); a guess until history exists. |
| `warn_fraction` | `0.8` | The estimate warns when its high end reaches this fraction of `[budget] per_run_usd`. |
| `refuse_fraction` | `1.0` | The estimate refuses approval when its high end reaches this fraction of `[budget] per_run_usd` (`--force` still approves). |

```toml
[dispatcher.cost]
prefer_cheaper_leg = true
openrouter_price_per_million = 0.06
warn_fraction = 0.7
```

### Worker readiness

Runs and `jev doctor` use one rule to decide which enabled workers can run (in fake-worker mode,
`JEV_FAKE_WORKERS=1`, every enabled worker is used):

- `claude_code` and `codex`: the `command` is found on `PATH`.
- `api`: the `api_key_env` variable is set, and, per role, a model it can run is available:
  `default_model`, a `vendor/name` slug in the role's `models`, or free models (`"free"` or
  `prefer_free` with a cached model list). It is never offered to the coder.

A worker that is not ready is not offered to the dispatcher, with one warning at the start of a
run (`api` that has a model for some roles only is offered to those). `jev doctor` shows the
same result per worker and per role.

## `[roles.<role>]`

Roles are `planner`, `validator`, `coder` and `reviewer`, plus `learner`, which distills
lessons at the end of a run when learning is on ([learning.md](learning.md)), and
`code_reviewer`, the AI review of the quality gate ([quality.md](quality.md)).

| Key | Default | Meaning |
| --- | --- | --- |
| `workers` | see below | Workers allowed for this role. Each must be `claude_code`, `codex` or `api`. |
| `models` | `[]` | Models allowed for this role. Empty means not configured yet. The entry `"free"` expands to cached free models (see [Free models](#free-models)). |
| `selection` | `"dispatcher"` | `"dispatcher"` lets the dispatcher choose; `"fixed"` skips that choice. |
| `prefer_free` | unset | Overrides `[models] prefer_free` for this role (`true` or `false`); unset follows it. Only has an effect when the role's `workers` include `api`. |
| `efforts` | `["low", "medium", "high"]` | Effort levels the dispatcher may pick; `[]` lets the worker decide. Each level must be accepted by at least one of the role's `workers` (`claude_code`: low, medium, high, xhigh, max; `codex`: those and ultra; `api`: low, medium, high), or loading fails. A worker given a level it does not accept ignores it and notes that in the attempt log. |
| `prefer_different_vendor_than` | unset | Name of another role whose vendor this role should avoid. The vendor is the `vendor/` prefix of the model slug, else the CLI's maker (`claude_code`: anthropic, `codex`: openai). |
| `mcp` | `[]` | MCP servers this role's agent may use, by name from `[tools.mcp]`. |
| `skills` | `[]` | Skills this role's agent gets, by name from `[tools.skills]`. |
| `plugins` | `[]` | Claude Code plugins this role's agent gets, by name from `[tools.plugins]`. |
| `servers` | `[]` | Servers this role's agent may reach through jev's server tools, by name from `[servers.<name>]` ([servers.md](servers.md)). Every role except `coder` is capped at `basic` access. |
| `browser` | `false` | Give this role the virtual browser (Playwright MCP server); needs `[browser] enabled = true`. |
| `prompt_file` | unset | Your own Jinja2 prompt template for this role, replacing the built-in one in `src/jev_agent/roles/<role>/prompt.md`. Relative paths are resolved against the repo root. A repo's `jev.toml` cannot point it outside the repo (absolute, `..`-escaping, or symlinked paths are rejected at load); your user config may still use an absolute path elsewhere. `jev doctor` fails if the file is missing or not valid Jinja2. A bad template, or a variable it uses that jev does not provide, stops the run in `NEEDS_HUMAN`; fix it and `jev resume` (see [roles.md](roles.md#overriding-a-prompt)). |
| `prompt_addendum` | `""` | Extra instructions appended to the role's prompt after the template is rendered, so they apply on top of the built-in template and any `prompt_file`. Up to 4000 characters, no control characters. Blank means none. Editable per role on the dashboard's Fleet page ("Additional prompt"). |

A `[roles.<role>]` table is layered on top of that role's defaults, so a table that sets only
`models` keeps the default `workers` (and the validator keeps
`prefer_different_vendor_than = "planner"`).

`models` is ordered weakest to strongest: escalation moves to the next model once the effort is
already at the highest level. An empty pool means "let the worker use its default model".

### Workers and models

A role's `models` pool is shared by all its `workers`, so jev checks each (worker, model) pair
by the model's name:

- a name with a `/` (an OpenRouter slug such as `anthropic/claude-sonnet-4.5`) runs only on
  `api`;
- a name without one (`sonnet`, `opus`, `claude-sonnet-4-6`, `gpt-5.2-codex`) runs only on
  `claude_code` and `codex`;
- the worker's default model (an empty pool) fits every worker.

The rules dispatcher only picks runnable pairs. Jev answers worker and model separately (it is
told the rule when the pool mixes both kinds); if the pair cannot run, jev keeps the worker and
gives it its first runnable model in the pool, or its default model when there is none. The
same applies on escalation (it skips models the worker cannot run) and when a task is
reassigned to another worker. So a mixed pool such as `["sonnet", "anthropic/claude-sonnet-4.5"]`
lets `claude_code` and `api` each run their own model. Codex with a custom `model_provider` whose
model names contain `/` is not supported: the name is treated as an OpenRouter slug.

Default `workers` per role:

| Role | `workers` | Other defaults |
| --- | --- | --- |
| `planner` | `["claude_code", "api"]` | |
| `validator` | `["codex", "api"]` | `prefer_different_vendor_than = "planner"` |
| `coder` | `["claude_code", "codex"]` | |
| `reviewer` | `["api", "codex"]` | |
| `code_reviewer` | `["api", "codex", "claude_code"]` | Read-only. Only used when `[quality] level` is not `"none"`; without a usable worker the AI review is skipped. |
| `learner` | `["claude_code", "api", "codex"]` | Read-only, like the planner. Without a usable worker, `jev doctor` warns and runs skip the learning step. |

## `[servers.<name>]`

Machines agents may reach over SSH, through jev's own MCP server (`jev mcp servers`), which
checks every command against the access level and writes `.jev/audit/servers.log`. Give them to
roles with `[roles.<role>] servers = [...]` or through a workspace profile. Details, the full
allow/deny tables and VPS notes: [servers.md](servers.md).

| Key | Default | Meaning |
| --- | --- | --- |
| `host` | required | Host name, address or `~/.ssh/config` alias (`Host` entry). |
| `user` | unset | Login user (`ssh -l`); unset: `~/.ssh/config` or your local user. |
| `port` | unset | SSH port; unset: `~/.ssh/config` or 22. |
| `identity_file` | unset | Path to a private key file (never the key itself); adds `-i FILE -o IdentitiesOnly=yes`. Unset: ssh-agent / `~/.ssh/config`. |
| `access` | `"basic"` | `"basic"` (read-only allowlist), `"medium"` (also writes inside `allowed_paths`, package installs, service restarts) or `"unrestricted"` (no policy checks at all: anything the server account may do). `"unrestricted"` needs `unrestricted_acknowledged = true` and its activation is written to the audit log (issue #3). `"full"` is rejected. |
| `unrestricted_acknowledged` | `false` | Explicit acknowledgment that `access = "unrestricted"` is intended. The tier is never granted without this flag; inert for the other levels. |
| `workdir` | unset | Absolute path commands run in; relative paths are resolved against it. |
| `allowed_paths` | `[workdir]` | Absolute paths `medium` writes may touch. `/`, top-level system folders and anything under `.ssh` are refused. |
| `project_folder` | unset | Scopes `medium` access to one project folder: the agent may read, write and run commands only inside it, and a symlink pointing out counts as a boundary violation. Unset: the medium scope is `allowed_paths`. |
| `description` | `""` | Shown to agents by `list_servers` and in `jev profile show`. |

No passwords: jev runs `ssh` with `BatchMode=yes` and `StrictHostKeyChecking=yes`, so the key
must come from ssh-agent or `identity_file`, and the host key must already be in `known_hosts`.

## `[browser]`

The virtual browser: Playwright's Chromium driven through the Playwright MCP server (pinned
version, see [servers.md](servers.md#virtual-browser)).

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Roles with `browser = true` get the browser. A workspace profile with `browser = true` gives it to the coder even when this is `false`. |
| `headless` | `true` | `false` runs a visible window (needs a display, for example Xvfb on a VPS). |
| `install_target` | `"local"` | Where `jev browser install` installs Chromium: `"local"` or a `[servers.<name>]` name (needs `medium`). |

## `[profiles.<name>]`

Workspace profiles bundle what a run may reach. Pick one with `--profile NAME` on `jev run`,
`jev resume`, `jev queue add` and `jev chat` (`--profile none` for none); `jev profile use NAME`
sets the repo default (`.jev/profile`). With more than one profile, `jev run` on a terminal asks.
The profile a run started with is copied to `.jev/runs/<run_id>/profile.json`.

| Key | Default | Meaning |
| --- | --- | --- |
| `servers` | `[]` | Servers for this run: the coder gets them at their level, every other role at `basic`. |
| `access_overrides` | `{}` | Per server, a lower level for this profile (`{ web = "basic" }`). Raising is rejected. |
| `browser` | `false` | Give the coder the browser in these runs. |
| `learning` | unset | A learning profile name, or `false` for no learning; unset follows the usual setting. `--learning` / `--no-learning` still win. |
| `quality` | unset | Quality gate level for these runs; `--quality` still wins. |
| `description` | `""` | Shown in `jev profile list` and the run menu. |

## Free models

OpenRouter offers some models for free: their ids end in `:free` or their prompt and
completion prices are `"0"` in `GET /api/v1/models`. They cost nothing but are rate-limited
(OpenRouter documents 20 requests per minute and 50 per day, or 1000 per day once the account
has bought at least 10 credits), and the providers serving them may log or train on prompts,
depending on your [OpenRouter privacy settings](https://openrouter.ai/settings/privacy), which
are separate for free and paid models. Details and sources: [verification.md](verification.md#7-free-models-openrouter).

Turn them on for every role that can use the api worker:

```toml
[models]
prefer_free = true

[roles.planner]
workers = ["api", "claude_code"]
models = ["free", "openai/gpt-5-mini"]   # free models first, then a paid fallback

[roles.reviewer]
prefer_free = false                       # this role keeps paying
```

- **Where they apply.** Only on the `api` worker. A role whose `workers` do not include `api`
  (the coder, on Claude Code or Codex) ignores `prefer_free` and drops `"free"`; `jev doctor`
  notes this. A free model chosen for another worker is moved to `api`, or replaced by the
  first model that worker can run when the role cannot use `api` (see
  [Workers and models](#workers-and-models)).
- **`"free"` in `models`** expands, when a run starts, to at most `free_models_limit` free
  models from the cached list. Roles that answer with JSON (planner, validator, reviewer,
  learner) skip models whose `supported_parameters` lack `structured_outputs`; models that
  list no parameters at all are kept and marked "structured output support unknown". The
  random `openrouter/free` router is never added this way (you can still list it by name).
- **`prefer_free`** moves the pool's free models first (adding the `"free"` expansion when the
  pool has none). The rules dispatcher then picks the first free model on the api worker; Jev
  sees each free option described as "free (rate-limited)" and is told to prefer free models
  unless the task is hard or earlier attempts failed. Escalation from a free model goes to
  the first paid model the api worker can run.
- **Rate limits.** A 429 from a free model counts as `quota_exhausted`. jev then tries the
  next free model, then the first paid model in the pool that the api worker can run, then one
  other worker of the role (with its first runnable model).
  For the planner, validator, learner and reviewer this happens inside the same step; for a
  task it is the D5 decision (`retry` moves to the fallback model). Each try is an attempt.
- **The model list** is cached in `.jev/cache/openrouter-models.json` and fetched again when
  older than `cache_ttl_hours`. If the fetch fails, the stale list is used and the run prints a
  warning. `jev models list [--free] [--supports json_schema] [--refresh]` and `jev models
  free` show it.

## Agent tools: `[tools]`

Give agents tools per role: MCP servers, skills and Claude Code plugins. Define each tool once
under `[tools]`, then list it by name in `[roles.<role>]` (`mcp`, `skills`, `plugins`). An
unknown name is a config error. Nothing is installed or written to your global CLI config: the
tools are passed to each agent run only (see [verification.md](verification.md) section 6 and
[security.md](security.md#agent-tools-mcp-skills-plugins)).

| Key | Meaning |
| --- | --- |
| `mcp` | Table of MCP servers, `[tools.mcp.<name>]` (keys below). |
| `skills` | Table of skills, `<name> = "<folder holding SKILL.md>"`. Relative paths are resolved against the repo root. |
| `plugins` | Table of Claude Code plugins, `[tools.plugins.<name>]` with `path` (plugin folder or `.zip`, relative to the repo root or absolute) and optional `allowed_tools` (permission rules the plugin's tools need in headless runs, e.g. `"mcp__plugin_<plugin>_<server>__*"` or `"Skill(<plugin>:<skill>)"`). |

Names use letters, digits, `-` and `_`. An MCP server's name is how Claude Code names its tools
(`mcp__<name>__<tool>`).

`[tools.mcp.<name>]` keys. Set exactly one of `command` (a local server jev's agent starts) or
`url` (a remote server):

| Key | Default | Meaning |
| --- | --- | --- |
| `command` | unset | Local (stdio) server executable. `jev doctor` warns if it is not on `PATH`. |
| `args` | `[]` | Arguments for `command`. |
| `env_from` | `[]` | Names of environment variables passed to the server (values are read from your environment when the agent starts, never from config). |
| `url` | unset | Remote server URL (`http://` or `https://`). |
| `transport` | `"http"` | Remote transport: `"http"` (streamable HTTP) or `"sse"` (Claude Code only). |
| `bearer_token_env` | unset | Environment variable holding a token sent as `Authorization: Bearer <token>`. |
| `headers_from_env` | `{}` | HTTP headers filled from environment variables: `{ "X-Api-Key" = "DOCS_API_KEY" }`. |

Literal `env` and `headers` tables are rejected, as are arguments or URLs that look like a
credential (`sk-...`, `ghp_...`, `Bearer ...`, `user:pass@`): keep secrets in your environment.

```toml
[tools.mcp.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env_from = ["GITHUB_PERSONAL_ACCESS_TOKEN"]

[tools.mcp.docs]
url = "https://docs.example.com/mcp"
bearer_token_env = "DOCS_MCP_TOKEN"

[tools.skills]
house-style = ".claude/house-style"   # a folder with SKILL.md

[tools.plugins.deploy-tools]
path = "tools/deploy-tools"
allowed_tools = ["mcp__plugin_deploy-tools_api__*"]

[roles.planner]
mcp = ["github", "docs"]

[roles.coder]
skills = ["house-style"]
```

What each worker does with them:

| Tool | Claude Code | Codex | API worker |
| --- | --- | --- | --- |
| MCP servers | `--mcp-config <tmp file> --strict-mcp-config` with only the role's servers, and `mcp__<name>` added to `--allowedTools`. The repo's `.mcp.json` and your user servers do not load for that role. | `-c mcp_servers.<name>...` overrides (env vars passed by name). SSE servers are skipped. Servers in your `~/.codex/config.toml` still load. | not supported yet |
| Skills | Copied to a temporary folder loaded with `--add-dir`; `Skill(<name>)` allowed. | not supported per run (left out, noted in the log) | not supported |
| Plugins | `--plugin-dir <path>`, plus the plugin's `allowed_tools`. | not supported per run | not supported |

Skills and agent configuration committed to the repo (`.claude/skills/`, `.agents/skills/`)
are found by the CLIs on their own, without any of this. `jev doctor` lists each role's tools,
warns when an `env_from` (or header/bearer) variable is unset or a `command` is not on `PATH`,
fails when a skill or plugin path is missing, and notes tools a role's workers cannot use.

## Environment variables

| Variable | Meaning |
| --- | --- |
| `JEV_FAKE_WORKERS=1` | Testing hook: register a scripted fake worker under every enabled worker name (replacing the real ones), so `jev run` can be tried end to end without any agent CLI or API key. The fake plans one task and writes a file `jev_fake_T1.txt` on its branch. |
| `JEV_REDACT_NAMES` | Set by jev for the agent CLIs it starts: comma-separated names of environment variables (the MCP servers' `env_from`, header and bearer variables) whose values are scrubbed from every log. You can set it yourself to redact more. |
| `JEV_DASHBOARD_TOKEN` | Access token for `jev dashboard` (same as `--token`, but not visible in the process list). Required when the dashboard listens on a non-loopback address; generated when unset ([dashboard.md](dashboard.md)). |
| `XDG_DATA_HOME` | Where shared learning profiles live: `$XDG_DATA_HOME/jev/learning.db`, else `~/.local/share/jev/learning.db` ([learning.md](learning.md)). |
| `OPENROUTER_API_KEY` | Default key variable for both Jev (`[dispatcher.jev] api_key_env`) and the API worker (`[workers.api] api_key_env`). |
