# Configuration

`jev init` writes a commented `jev.toml` to the root of your repository. Every key is optional;
an empty file (or no file) gives the defaults below.

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

Run `jev doctor` to check that config loads, which worker CLIs are on `PATH`, which API key
environment variables are set, and that every `prompt_file` exists and parses.

## Secrets

Config files never hold secrets. Any key whose name ends in `key` or contains `token`, `secret`
or `password` is rejected, in any table (names you choose for MCP servers, skills, plugins and
HTTP headers are exempt). Put the secret in an environment variable and name the variable with
`api_key_env` (or, for MCP servers, `env_from`, `bearer_token_env` and `headers_from_env`).

## `[dispatcher]`

| Key | Default | Meaning |
| --- | --- | --- |
| `primary` | `"jev"` | Dispatcher asked first: `"jev"` or `"rules"`. |
| `fallback` | `"rules"` | Dispatcher used when the primary fails. Only `"rules"` is used for now: whatever this says, the rules dispatcher backs up Jev. |

With `primary = "jev"`, every decision goes to Jev first. If the call fails (key unset, network
or HTTP error, an answer outside the allowed values, low confidence, oversized state), the rules
dispatcher answers that decision instead, and `jev log` shows the reason after `FALLBACK:`. If
the key variable is unset, `jev run`, `approve` and `resume` print one warning at the start and
every decision uses rules. `jev doctor` reports the dispatcher and whether its key is set.

## `[dispatcher.jev]`

How to reach Jev (TypeSafe's System One API). The defaults call it through OpenRouter.

| Key | Default | Meaning |
| --- | --- | --- |
| `base_url` | `"https://openrouter.ai/api/v1"` | API base URL; requests go to `{base_url}/systemone`. |
| `model` | `"~typesafe/jev-latest"` | Jev model slug. The `~` means "latest"; pin a version with e.g. `"typesafe/jev-1.13"`. |
| `api_key_env` | `"OPENROUTER_API_KEY"` | Name of the environment variable holding the key. Must be a valid variable name. |
| `timeout_s` | `30` | Per-request timeout in seconds (> 0). One retry is made on 429/5xx. |
| `min_confidence` | `0.55` | Between 0 and 1. If Jev's confidence on any answer is below this, the whole decision falls back to rules. |
| `instructions` | `{}` | Optional table replacing the instruction sent for a question. Keys are `"<point>.<question>"` (e.g. `"D4.effort"`) for one decision point or `"<question>"` (e.g. `model`) for all of them. |

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

## `[approval]`

| Key | Default | Meaning |
| --- | --- | --- |
| `require_plan_approval` | `true` | Stop for human approval after the plan is validated. |

## `[review]`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | After a task's tests pass, have the reviewer check its diff. A `revise` verdict counts as a failed attempt (retry, escalate, ...). The reviewer uses the role's first worker and model and its middle effort level. |

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

## `[models]`

Free OpenRouter models for roles that can use the api worker. See [Free models](#free-models)
below for how they are chosen and what happens when one hits its rate limit.

| Key | Default | Meaning |
| --- | --- | --- |
| `prefer_free` | `false` | Offer free models first to every role that may use the `api` worker. A role's own `prefer_free` overrides this. A role whose pool has no free model gets the `"free"` expansion added in front of it. |
| `free_models_limit` | `3` | How many cached free models one `"free"` entry in a role's `models` expands to (>= 0). |
| `cache_ttl_hours` | `24` | Age (> 0) after which the cached model list (`.jev/cache/openrouter-models.json`) is fetched again, at the start of a run that uses free models or by `jev models`. |

## `[workers.claude_code]` and `[workers.codex]`

Local agent CLIs. Both are registered whenever `enabled = true`; `jev doctor` warns if the
command is not on `PATH`. See [security.md](security.md) for the permission model.

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
| `default_model` | unset | Model used when the role's `models` pool is empty. If unset, a role that picks `api` without a model fails that attempt with an error. |
| `timeout_s` | `600` | Per-attempt timeout in seconds (> 0), retries included. |

The API worker runs the planner, validator and reviewer roles (it cannot edit files, so never
the coder). It is registered whenever `enabled = true`; a missing key makes its attempts fail
with status `error`, which the pipeline handles like any other failed attempt.

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
| `efforts` | `["low", "medium", "high"]` | Effort levels the dispatcher may pick. |
| `prefer_different_vendor_than` | unset | Name of another role whose vendor this role should avoid. The vendor is the `vendor/` prefix of the model slug, else the CLI's maker (`claude_code`: anthropic, `codex`: openai). |
| `mcp` | `[]` | MCP servers this role's agent may use, by name from `[tools.mcp]`. |
| `skills` | `[]` | Skills this role's agent gets, by name from `[tools.skills]`. |
| `plugins` | `[]` | Claude Code plugins this role's agent gets, by name from `[tools.plugins]`. |
| `prompt_file` | unset | Your own Jinja2 prompt template for this role, replacing the built-in one in `src/jev_agent/roles/<role>/prompt.md`. Relative paths are resolved against the repo root. `jev doctor` fails if the file is missing or not valid Jinja2. A bad template, or a variable it uses that jev does not provide, stops the run in `NEEDS_HUMAN`; fix it and `jev resume` (see [roles.md](roles.md#overriding-a-prompt)). |

A `[roles.<role>]` table is layered on top of that role's defaults, so a table that sets only
`models` keeps the default `workers` (and the validator keeps
`prefer_different_vendor_than = "planner"`).

`models` is ordered weakest to strongest: escalation moves to the next model once the effort is
already at the highest level. An empty pool means "let the worker use its default model".

Default `workers` per role:

| Role | `workers` | Other defaults |
| --- | --- | --- |
| `planner` | `["claude_code", "api"]` | |
| `validator` | `["codex", "api"]` | `prefer_different_vendor_than = "planner"` |
| `coder` | `["claude_code", "codex"]` | |
| `reviewer` | `["api", "codex"]` | |
| `code_reviewer` | `["api", "codex", "claude_code"]` | Read-only. Only used when `[quality] level` is not `"none"`; without a usable worker the AI review is skipped. |
| `learner` | `["claude_code", "api", "codex"]` | Read-only, like the planner. Without a usable worker, `jev doctor` warns and runs skip the learning step. |

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
  first paid model when the role cannot use `api`.
- **`"free"` in `models`** expands, when a run starts, to at most `free_models_limit` free
  models from the cached list. Roles that answer with JSON (planner, validator, reviewer,
  learner) skip models whose `supported_parameters` lack `structured_outputs`; models that
  list no parameters at all are kept and marked "structured output support unknown". The
  random `openrouter/free` router is never added this way (you can still list it by name).
- **`prefer_free`** moves the pool's free models first (adding the `"free"` expansion when the
  pool has none). The rules dispatcher then picks the first free model on the api worker; Jev
  sees each free option described as "free (rate-limited)" and is told to prefer free models
  unless the task is hard or earlier attempts failed. Escalation from a free model goes to
  the first paid model.
- **Rate limits.** A 429 from a free model counts as `quota_exhausted`. jev then tries the
  next free model, then the first paid model in the pool, then one other worker of the role.
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
| `XDG_DATA_HOME` | Where shared learning profiles live: `$XDG_DATA_HOME/jev/learning.db`, else `~/.local/share/jev/learning.db` ([learning.md](learning.md)). |
| `OPENROUTER_API_KEY` | Default key variable for both Jev (`[dispatcher.jev] api_key_env`) and the API worker (`[workers.api] api_key_env`). |
