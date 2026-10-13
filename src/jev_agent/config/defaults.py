"""File names and the commented `jev.toml` that `jev init` writes."""

CONFIG_FILENAME = "jev.toml"
STATE_DIRNAME = ".jev"

DEFAULT_CONFIG_TOML = """\
# Baton Agent Orchestrator configuration. Reference: docs/config.md
# Settings here override your user config (~/.config/jev/config.toml).
# Never put secrets in this file: keys are read from environment variables.

[dispatcher]
primary = "jev"                    # "jev" | "rules"
fallback = "rules"                 # used when the primary dispatcher fails

# Jev via OpenRouter. For native TypeSafe use base_url = "https://api.typesafe.ai/v1",
# model = "jev-latest" and api_key_env = "TYPESAFE_API_KEY".
[dispatcher.jev]
base_url = "https://openrouter.ai/api/v1"
model = "~typesafe/jev-latest"     # pin a version with e.g. "typesafe/jev-1.13"
api_key_env = "OPENROUTER_API_KEY" # name of the env var holding the key, not the key
timeout_s = 30
min_confidence = 0.55              # below this on any answer, the rules dispatcher decides

# [dispatcher.jev.instructions]    # optional: replace the instruction for a question
# "D4.effort" = "..."              # at one decision point
# model = "..."                    # at every decision point

[budget]
per_run_usd = 5.00                 # hard cap for one run
per_task_usd = 1.50                # hard cap for one task (must be <= per_run_usd)
max_plan_rounds = 3                # planner/validator rounds before asking a human
max_task_attempts = 3              # attempts per task before escalating
test_timeout_s = 1200              # per test command (task tests and integration tests)

[integration]
# test_command = "uv run pytest -q" # full suite on the merged result branch; default: none
max_fix_rounds = 2                 # fix tasks added for failing integration tests before asking

[approval]
require_plan_approval = true       # human gate after plan validation

[review]
enabled = false                    # run the reviewer on each task's diff after its tests pass

# Quality and security gate (docs/quality.md), after each task and at integration. Levels are
# cumulative: "none" (task tests only) | "basic" (lint, full tests, AI code review) | "medium"
# (+ secrets, SAST, dependency audit, coverage) | "production" (+ types, licences, security
# checklist) | "scalable" (+ performance review, container/IaC scans, threat model).
# Override per run: `jev run --quality LEVEL`. `jev quality checks` shows the installed tools.
[quality]
level = "none"
# test_command = "uv run pytest -q" # full suite for the gate; default: [integration] or detected
# min_coverage = 70                # percent; default 60 at medium, 80 at production and above
required_tools = []                # e.g. ["ruff", "gitleaks"]: missing ones fail instead of skip
timeout_s = 600                    # per tool command

# Recursive learning (docs/learning.md): lessons from past runs are added to future prompts.
# Off by default because each run then ends with one extra model call (the learner). Toggle it
# without editing this file: `jev learning enable` / `jev learning disable`.
[learning]
enabled = false
profile = "default"                # lesson store to use; `jev learning list` shows them all
max_lessons = 8                    # most lessons added to one prompt
max_chars = 2000                   # most characters of lesson text added to one prompt

# Free OpenRouter models (docs/config.md "Free models"): no cost, but rate-limited (OpenRouter
# documents 20 requests/minute and 50/day, or 1000/day once you bought 10 credits), and free
# providers may log or train on prompts (check https://openrouter.ai/settings/privacy). They
# only run on the api worker; Claude Code and Codex are unaffected. `jev models free` lists them.
[models]
prefer_free = false                # true: offer free models first to roles that can use api
free_models_limit = 3              # how many cached free models `models = ["free"]` expands to
cache_ttl_hours = 24               # refetch the model list (.jev/cache/) when older than this

[workers.claude_code]
enabled = true
command = "claude"                 # executable name or path
timeout_s = 1200
permission_mode = "acceptEdits"    # coder, inside its own worktree
read_only_permission_mode = "dontAsk"  # planner, validator, reviewer: read tools only
# allowed_tools = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(git status *)",
#                  "Bash(git diff *)", "Bash(uv run pytest *)"]  # coder tools; keep patterns narrow
# max_budget_usd = 1.50            # Claude Code's own cap per attempt; unset = budget.per_task_usd
bare = false                       # true: --bare mode, needs ANTHROPIC_API_KEY
extra_args = []                    # appended to every call, e.g. ["--max-turns", "40"]
# DANGEROUS: true runs the coder with --dangerously-skip-permissions, so it can run ANY
# command with your privileges. Only in a throwaway VM/container without secrets or network.
skip_permissions = false

[workers.codex]
enabled = true
command = "codex"
timeout_s = 1200
sandbox = "workspace-write"        # coder: "read-only" | "workspace-write" | "danger-full-access"
extra_args = []

[workers.api]
enabled = true
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY" # name of the env var holding the key, not the key
# default_model = "openai/gpt-5-mini"  # used when a role's `models` is empty
timeout_s = 600

# Roles. `workers` lists who may do the job; `models` is empty until you fill it in.
# selection = "dispatcher" lets the dispatcher choose among them; "fixed" skips that choice.
# prompt_file = "prompts/planner.md" replaces the built-in prompt template for a role.
# models = ["free", "openai/gpt-5-mini"]: "free" expands to cached free models (api worker only).
# prefer_free = true|false overrides [models] prefer_free for one role.

[roles.planner]
workers = ["claude_code", "api"]
models = []
selection = "dispatcher"

[roles.validator]
workers = ["codex", "api"]
models = []
prefer_different_vendor_than = "planner"

[roles.coder]
workers = ["claude_code", "codex"]
models = []
efforts = ["low", "medium", "high"]

[roles.reviewer]
workers = ["api", "codex"]
models = []

[roles.code_reviewer]              # the AI code review of the quality gate ([quality] level)
workers = ["api", "codex", "claude_code"]
models = []

[roles.learner]                    # distills lessons at the end of a run, when learning is on
workers = ["claude_code", "api", "codex"]
models = []

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
"""
