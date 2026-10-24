"""File names and the commented `jev.toml` that `jev init` writes.

The template sets no values (issue #20): each key is a commented-out line showing its built-in
default, so a new repo's jev.toml never overrides the user config or trips the trust gate.
"""

CONFIG_FILENAME = "jev.toml"
STATE_DIRNAME = ".jev"

DEFAULT_CONFIG_TOML = """\
# Baton Agent Orchestrator configuration. Reference: docs/config.md
# Every value below is commented out and shows the built-in default, so your user config
# (~/.config/jev/config.toml) and the defaults apply. Uncomment a line to override it for this
# repository only: settings here override your user config.
# Never put secrets in this file: keys are read from environment variables.

[dispatcher]
# primary = "jev"                  # "jev" | "rules"

# Jev via OpenRouter. For native TypeSafe use base_url = "https://api.typesafe.ai/v1",
# model = "jev-latest" and api_key_env = "TYPESAFE_API_KEY".
[dispatcher.jev]
# base_url = "https://openrouter.ai/api/v1"
# model = "~typesafe/jev-latest"   # pin a version with e.g. "typesafe/jev-1.13"
# api_key_env = "OPENROUTER_API_KEY" # name of the env var holding the key, not the key
# timeout_s = 30
# min_confidence = 0.55            # below this on any answer, the rules dispatcher decides

# [dispatcher.jev.instructions]    # optional: replace the instruction for a question
# "D4.effort" = "..."              # at one decision point
# model = "..."                    # at every decision point

# Removed (issue #193): the former [dispatcher.laya] second vote and its weights are
# gone. Delete any [dispatcher.laya] table from existing configs; code grading and routing
# now belong to [code_orchestrator] below, and Jev alone decides as main orchestrator.

# Cost-aware Jev leg ordering and pre-run cost estimates (the model prices live here, not
# in code: providers reprice). This changes the leg order, never the availability.
[dispatcher.cost]
# prefer_cheaper_leg = true        # try the cheaper keyed leg first; the other still serves
# native_price_per_million = 0.042 # USD per 1M input tokens, native TypeSafe leg
# openrouter_price_per_million = 0.042 # USD per 1M input tokens, OpenRouter leg
# min_history_runs = 3             # ended runs before the estimate trusts history
# history_runs = 20                # how many of the newest ended runs feed the estimate
# tokens_per_decision = 8000       # expected input tokens of one Jev decision call
# tokens_per_task = 100000         # expected input tokens of one task's worker calls
# worker_price_per_million = 3.0   # USD per 1M input tokens of worker calls (a guess)
# warn_fraction = 0.8              # the estimate warns at this fraction of per_run_usd
# refuse_fraction = 1.0            # the estimate refuses approval at this fraction

# The Coding Decision Agent as orchestrator for code work (issue #187):
# it grades task diffs at the quality gate and routes coding tasks at D4.
# Jev stays the main orchestrator. Off by default; enabling it without the
# model installed grades nothing (the gate records "unavailable").
[code_orchestrator]
# enabled = false                  # grade diffs and route coding tasks
# model_id = "S4MPL3BI4S/Coding_Decision_Agent"
# confidence_threshold = 0.7       # calibrated answer_confidence a merge action needs
# max_diff_chars = 7000            # diff characters kept before grading (2048-token model)

[budget]
# per_run_usd = 5.00               # hard cap for one run
# per_task_usd = 1.50              # hard cap for one task (must be <= per_run_usd)
# max_plan_rounds = 3              # planner/validator rounds before asking a human
# max_task_attempts = 3            # attempts per task before escalating
# test_timeout_s = 1200            # per test command (task tests and integration tests)

[integration]
# test_command = "uv run pytest -q" # full suite on the merged result branch; default: none
# max_fix_rounds = 2               # fix tasks added for failing integration tests before asking

[workspace]
# run_hooks = true                 # run the repo's git hooks on jev's task commits and merges

[approval]
# require_plan_approval = true     # human gate after plan validation; unset: on

# How much jev asks you, and what its agents may touch. The mode picker is in Settings >
# The basics; the access tier lives under Advanced (it is safety-sensitive).
[autonomy]
# mode = "standard"                # "standard": approve the plan once, then jev works alone |
#                                  # (today's behavior) | "ask_every_step": ask before every step |
#                                  # "autonomous": never stop for approval | "manual": only act when
#                                  # told to | "medium": like "standard", scoped to project_folder
# access_tier = "basic"            # "basic" (safest) | "medium" (writes inside project_folder) |
#                                # "unrestricted" (anything: needs unrestricted_acknowledged = true)
# project_folder = ""              # folder the medium tier may change; unset: the repo root
# unrestricted_acknowledged = false # tick only if you accept what "unrestricted" means

[review]
# enabled = true                   # reviewer on each task's diff; unset: off

# Quality and security gate (docs/quality.md), after each task and at integration. Levels are
# cumulative: "none" (task tests only) | "basic" (lint, full tests, AI code review) | "medium"
# (+ secrets, SAST, dependency audit, coverage) | "production" (+ types, licences, security
# checklist) | "scalable" (+ performance review, container/IaC scans, threat model).
# Override per run: `jev run --quality LEVEL`. `jev quality checks` shows the installed tools.
[quality]
# level = "none"
# test_command = "uv run pytest -q" # full suite for the gate; default: [integration] or detected
# min_coverage = 70                # percent; default 60 at medium, 80 at production and above
# required_tools = []              # e.g. ["ruff", "gitleaks"]: missing ones fail instead of skip
# timeout_s = 600                  # per tool command
# score_threshold = 85             # minimum review score (0-100) for the gate to pass
# score_max_retries = 2          # ship-score rework rounds before the run stops for a human
# score_skip_human = false       # true: skip the ship score gate for human-authored code

# Recursive learning (docs/learning.md): lessons from past runs are added to future prompts.
# Off by default because each run then ends with one extra model call (the learner). Toggle it
# without editing this file: `jev learning enable` / `jev learning disable`.
[learning]
# enabled = false
# profile = "default"              # lesson store to use; `jev learning list` shows them all
# max_lessons = 8                  # most lessons added to one prompt
# max_chars = 2000                 # most characters of lesson text added to one prompt

# The event-sourced execution log (docs/events.md): every run mutation is dual-written as
# an append-only event, powering `jev events`, `jev replay` and `jev bisect`.
[events]
# enabled = true                   # turn off if the log ever misbehaves; snapshots are unaffected

# Open a GitHub pull request for a finished run (docs/ship.md): `jev ship RUN` pushes
# jev/<run>/result to a new branch on `remote` and runs `gh pr create` with your gh login.
# Off by default. It never force-pushes; branch protection still applies. When it is on, a D6
# "ship" vote ships the run itself: "merge" opens the PR ready, waits for its checks and merges
# it; "ask" waits for you (`jev ship approve|modify|stop RUN`) first.
[ship]
# enabled = false
# remote = "origin"
# base = ""                        # PR base branch; "" = the repo's default branch
# branch_prefix = "jev/"           # pushed branch: <branch_prefix><run_id>
# draft = true                     # open PRs as drafts
# labels = []                      # e.g. ["jev"]; the labels must exist in the repo
# require_quality_pass = true      # refuse runs whose latest quality gate blocked
# after_approval = "merge"         # or "ask"; unset: "ask"
# merge_method = "squash"          # or "merge", "rebase"
# require_checks = true            # a PR with no checks stops for you instead of merging
# checks_grace_s = 120             # how long to wait for checks to appear
# checks_timeout_s = 3600          # how long to wait for checks to finish
# checks_poll_s = 30

# Free OpenRouter models (docs/config.md "Free models"): no cost, but rate-limited (OpenRouter
# documents 20 requests/minute and 50/day, or 1000/day once you bought 10 credits), and free
# providers may log or train on prompts (check https://openrouter.ai/settings/privacy). They
# only run on the api worker; Claude Code and Codex are unaffected. `jev models free` lists them.
[models]
# prefer_free = false              # true: offer free models first to roles that can use api
# free_models_limit = 3            # how many cached free models `models = ["free"]` expands to
# cache_ttl_hours = 24             # refetch the model list (.jev/cache/) when older than this

[workers.claude_code]
# enabled = true
# command = "claude"               # executable name or path
# timeout_s = 1200
# permission_mode = "acceptEdits"  # coder, inside its own worktree
# read_only_permission_mode = "dontAsk" # planner, validator, reviewer: read tools only
# allowed_tools = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(git status *)",
#                  "Bash(git diff *)", "Bash(uv run pytest *)"]  # coder tools; keep patterns narrow
# max_budget_usd = 1.50            # Claude Code's own cap per attempt; unset = budget.per_task_usd
# bare = false                     # true: --bare mode, needs ANTHROPIC_API_KEY
# extra_args = []                  # appended to every call, e.g. ["--max-turns", "40"]
# DANGEROUS: true runs the coder with --dangerously-skip-permissions, so it can run ANY
# command with your privileges. Only in a throwaway VM/container without secrets or network.
# skip_permissions = false

[workers.codex]
# enabled = true
# command = "codex"
# timeout_s = 1200
# sandbox = "workspace-write"      # coder: "read-only" | "workspace-write" | "danger-full-access"
# extra_args = []

[workers.api]
# enabled = true
# base_url = "https://openrouter.ai/api/v1"
# api_key_env = "OPENROUTER_API_KEY" # name of the env var holding the key, not the key
# default_model = "openai/gpt-5-mini"  # used when a role's `models` is empty
# timeout_s = 600

# Roles. `workers` lists who may do the job; `models` is empty until you fill it in.
# selection = "dispatcher" lets the dispatcher choose among them; "fixed" skips that choice.
# prompt_file = "prompts/planner.md" replaces the built-in prompt template for a role.
# prompt_addendum = "extra instructions" appends to the role's prompt (multiline ok).
# models = ["free", "openai/gpt-5-mini"]: "free" expands to cached free models (api worker only).
# prefer_free = true|false overrides [models] prefer_free for one role.

[roles.planner]
# workers = ["claude_code", "api"]
# models = []
# selection = "dispatcher"

[roles.validator]
# workers = ["codex", "api"]
# models = []
# prefer_different_vendor_than = "planner"

[roles.coder]
# workers = ["claude_code", "codex"]
# models = []
# efforts = ["low", "medium", "high"]

[roles.reviewer]
# workers = ["api", "codex"]
# models = []

[roles.code_reviewer]              # the AI code review of the quality gate ([quality] level)
# workers = ["api", "codex", "claude_code"]
# models = []

[roles.learner]                    # distills lessons at the end of a run, when learning is on
# workers = ["claude_code", "api", "codex"]
# models = []

[roles.designer]                    # designs the UI before it is approved (jev design)
# workers = ["api", "claude_code"]
# models = []

# Agent tools (docs/config.md "Agent tools"): define MCP servers, skills and Claude Code plugins
# once, then give them to roles by name, e.g. `mcp = ["github"]` under [roles.planner].
# Secrets stay in environment variables: `env_from` lists variable NAMES, never values.
#
# [tools.mcp.github]
# command = "npx"
# args = ["-y", "@modelcontextprotocol/server-github"]
# env_from = ["GITHUB_PERSONAL_ACCESS_TOKEN"]
#
# [tools.mcp.docs]                 # a remote server
# url = "https://docs.example.com/mcp"
# bearer_token_env = "DOCS_MCP_TOKEN"
#
# [tools.skills]
# house-style = ".claude/house-style"  # folder with SKILL.md, relative to the repo root
#
# [tools.plugins.deploy-tools]     # Claude Code only (--plugin-dir)
# path = "tools/deploy-tools"
# allowed_tools = ["mcp__plugin_deploy-tools_api__*"]

# Servers over SSH (docs/servers.md): agents reach them only through jev's server tools, which
# check every command against the access level and log it to .jev/audit/servers.log. Keys come
# from ssh-agent or ~/.ssh/config (never passwords); the host key must already be known.
# access: "basic" (read-only commands) | "medium" (+ writes inside allowed_paths, package
# installs, service restarts). Give servers to roles (`servers = ["web"]` under [roles.coder])
# or to a workspace profile; every role but the coder is capped at basic.
#
# [servers.web]
# host = "web1"                    # host name or ~/.ssh/config alias
# user = "deploy"
# access = "basic"
# workdir = "/srv/app"
# allowed_paths = ["/srv/app"]     # medium writes; default [workdir]
# description = "production web server"

# Virtual browser (Playwright MCP + headless Chromium) for roles with `browser = true`.
# Install Chromium with `jev browser install` (locally or `--server NAME`).
[browser]
# enabled = false
# headless = true
# install_target = "local"         # "local" or a [servers.NAME] (needs medium access)

# Workspace profiles: pick with `jev run --profile NAME` (or a menu); `jev profile use NAME`
# sets the repo default.
#
# [profiles.prod]
# servers = ["web"]
# access_overrides = { web = "basic" }  # may only lower a server's access
# browser = false
# learning = "default"             # a learning profile, or false
# quality = "basic"
# description = "read-only look at production"

# Exact-match cache for dispatcher decisions and read-only role calls (docs/config.md
# "[cache]", issue #10). Safe defaults: the coder and learner roles are hard-denied in code,
# CLI workers are excluded unless opted in, D6 ship/fix decisions are excluded, and only
# `success` results are stored.
[cache]
# enabled = true                   # master kill-switch; also `--no-cache` per run
# ttl_hours = 168                  # default entry lifetime, hours (decisions)
# max_entries = 10000              # LRU eviction kicks in past this many entries
# max_mb = 256                     # evict least-recently-hit entries past this size
# cache_roles = ["planner", "validator", "reviewer", "code_reviewer"]
# cli_workers = false              # also cache role calls made through CLI workers
# chat = false                     # opt-in exact-match caching for `jev chat`
# d6_decisions = false             # cache D6 ship/fix decisions (a wrong ship vote is costly)

# [cache.ttl_hours_overrides]      # per-kind TTLs, in hours
# roles = 24                       # role-output entries expire after this long
# chat = 1                         # chat entries expire after this long

# [cache.semantic]                 # phase-3 similarity matching for near-duplicate calls
# enabled = false                  # nothing is served semantically until shadow mode proves it
# embedder = "simhash"             # "simhash" (stdlib) or "openrouter"
# decision_threshold = 0.98        # similarity floor for decisions (`jev cache calibrate`)
# role_threshold = 0.98            # similarity floor for role outputs
# chat_threshold = 0.98            # similarity floor for chat turns (phase 4)
# mode = "off"                     # "off" | "shadow" (measure only) | "serve"
# min_agreement = 0.98             # live-sampling floor: serving auto-disables below this

# Repositories whose jev.toml may change the guarded keys. This section belongs in your
# user config (~/.config/jev/config.toml), never in a repo file: a repository cannot
# trust itself, so an active [trust] table here would fail to load.
# [trust]
# repos = []                       # e.g. ["/home/you/work/proj"]; "*" trusts every repository

# Dashboard display preferences (per-user, per-machine: read from your user config only).
# [dashboard]
# ui_style = "bento"               # one of the 16 UI styles; "bento" is the default
"""
