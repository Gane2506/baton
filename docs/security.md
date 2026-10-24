# Security

How jev limits what agents can do, how it handles keys, and what to check before you run it.
Background: [PLAN.md](../PLAN.md) sections 10 and 16.5, [verification.md](verification.md)
sections 2, 3 and 5. Config keys are in [config.md](config.md).

## Defaults

- **Worktree isolation.** Every coder task runs in its own git worktree under
  `.jev/worktrees/<run_id>/`, on a branch `jev/<run_id>/<task_id>`, never on your current
  branch. Integration merges task branches into a local `jev/<run_id>/result` branch for you to
  review.
- **No push by default.** jev runs `git push` only when you ship a run as a pull request
  (`jev ship`, opt-in; see [Shipping PRs](#shipping-prs)). Otherwise nothing leaves your
  machine through git.
- **Read-only roles.** The planner, validator and reviewer cannot edit files:
  - Claude Code runs them with `--permission-mode dontAsk`, read tools only
    (`Read Glob Grep`) and write tools explicitly disallowed (`Edit Write NotebookEdit`).
  - Codex runs them with `-s read-only`.
  - The API worker has no file access at all and refuses the coder role.
- **Coder permissions.** Claude Code's coder uses `acceptEdits` inside its own worktree. In
  `-p` mode nobody can answer a prompt, so any tool that is neither auto-accepted nor in
  `allowed_tools` is denied; Bash is limited to `git status` and `git diff` by default. Codex's
  coder uses `-s workspace-write` with `approval_policy="never"`. jev never passes
  `--full-auto`, `--ask-for-approval` or `--dangerously-bypass-approvals-and-sandbox`.
- **Cost caps.** The harness enforces `[budget] per_run_usd` and `per_task_usd` itself,
  whatever the dispatcher answers: no worker starts once a cap is spent. The caps apply to
  *reported* costs: dispatcher calls, the API worker and Claude Code report theirs. Codex reports
  no cost, so its attempts count $0 and the caps never stop them; a Codex attempt is bounded only
  by `[budget] max_task_attempts` and `[workers.codex] timeout_s` (`jev doctor` notes this when
  Codex is in use). Set a spend limit with your OpenAI account as well. Claude Code also gets
  `--max-budget-usd` (default: the per-task cap) as a second cap. Work already running
  finishes, so spend can end slightly above a cap.
- **Quality gate tools** ([quality.md](quality.md)) run only when `[quality] level` is not
  `none`: linters, test runners and scanners run as you, in the task worktree or on the result
  branch, with a timeout, including copies found in the repo's `node_modules/.bin` or
  `.venv/bin`. Their output is redacted before it is shown or stored. pip-audit, `npm audit`
  and trivy contact their vulnerability databases over the network; Semgrep runs only with
  local rules (never `--config auto`, which downloads rules and sends metrics). The AI code
  review is a read-only role like the reviewer and sees the diff.
- **Your repo contents** go only to the workers and dispatcher you configured. The dispatcher
  sees compact cards (task titles, statuses, costs), never full transcripts or diffs.

## Agent tools (MCP, skills, plugins)

`[tools]` and `[roles.<role>] mcp/skills/plugins` ([config.md](config.md#agent-tools-tools))
give agents extra tools. They are opt-in per role, and they widen what an agent can do:

- **MCP servers run with the agent's reach and your credentials.** A local server is a program
  started on your machine with your privileges; a remote one acts with whatever token you pass
  it. Only configure servers you trust, with tokens scoped to what the role needs (for example
  a read-only GitHub token for the planner).
- **Read-only roles stop being strictly read-only.** The planner, validator and reviewer still
  cannot edit files, but they may call every tool of their MCP servers (jev allows
  `mcp__<server>` for them, because a headless run cannot ask). If a server can write (open
  issues, push, change a database), so can that role. Prefer read-only servers or tokens there;
  `jev doctor` prints a note for every read-only role with MCP servers.
- **Only the role's servers load in Claude Code.** jev passes `--strict-mcp-config`, so the
  repo's `.mcp.json` and your user-level servers do not load for a role with `mcp = [...]`.
  Codex has no such switch: servers in your `~/.codex/config.toml` still load.
- **Secrets stay in your environment.** Config names environment variables (`env_from`,
  `bearer_token_env`, `headers_from_env`); literal `env`/`headers` tables, and arguments or URLs
  that look like credentials, are rejected. Codex receives only the variable names. Claude Code's
  `--mcp-config` needs the values, so jev writes them to a file readable only by you, in a
  private temporary directory outside the worktree, deleted when the attempt ends; they are never
  put in argv. Every log is redacted by those variable names (`JEV_REDACT_NAMES`), whatever the
  variables are called.
- **Skills and plugins are code and instructions.** A skill can tell the agent to run commands
  (still subject to the role's permission rules); a plugin can bring hooks, MCP servers and
  commands. Review them like any dependency. Plugin tools are allowed only through the
  `allowed_tools` you list for the plugin.
- The API worker gets no tools.

## Web dashboard

`jev dashboard` ([dashboard.md](dashboard.md)) can approve and resume runs, so access to it is
as powerful as a shell in the repo. It binds 127.0.0.1 by default and checks the `Host`
header; any other address requires a token. Actions are POST-only with a same-origin check and
a CSRF token; pages are autoescaped under a strict Content-Security-Policy; only files inside
`.jev/runs/<id>/` are served, as plain text. It speaks plain HTTP: on a server, keep the
default and use `ssh -L 8765:127.0.0.1:8765 host`.

## Shipping PRs

`jev ship RUN` ([ship.md](ship.md)) is the one command that sends code to a remote. It
relaxes the "never push" default as an explicit action per run:

- **Opt-in.** Off unless `[ship] enabled = true`; then it still asks before pushing, unless
  you pass `--yes` (the dashboard's button has its own confirmation page). It refuses runs
  that are not DONE, and, by default, runs whose quality gate blocked.
- **What is pushed:** only the run's existing result branch `jev/<run>/result`, to a new branch
  `<branch_prefix><run>` on `[ship] remote`. Your checkout and other branches are untouched.
- **Never force.** The push has no `--force`; a branch of that name with other commits on the
  remote is never overwritten (`--force-new` picks a new name instead).
- **Merges only what Jev approved.** `jev ship RUN` runs `gh pr create` (a draft by
  default) and later only reads the PR (`gh pr view`, `gh pr list`). With shipping on, a run
  Jev approves at D6 is merged by the pipeline (`[ship] after_approval = "merge"`, the
  default under consensus) or after you approve it (`"ask"`): only once its checks pass, and
  pinned with `gh pr merge --match-head-commit` to the commit Jev approved. It never approves
  a PR, never uses `--admin` and never enables auto-merge ([ship.md](ship.md#shipping-after-the-approval)).
- **Your credentials.** git pushes with your git credentials and `gh` acts with your own `gh`
  login; jev never reads, stores or passes a token. What your account may do, jev may do
  through it, so the remote's branch protection, required reviews and checks still apply.
- **What GitHub sees:** the branch's commits, a title from the idea's first line, and a body
  with the idea, plan, task table, quality summary and KPIs, redacted like every log. The full
  quality review and the logs stay local. `jev ship RUN --dry-run` shows everything first.
- `jev ship sync` stores only counts and states from GitHub, not review or comment text.

## Servers and the browser

`[servers.*]`, `[browser]` and workspace profiles ([servers.md](servers.md)) let agents reach
machines and web pages. They are off until configured, and:

- **No raw SSH for agents.** Agents only get jev's `jev_servers` MCP tools; every command is
  checked against the server's access level (`basic`: read-only allowlist; `medium`: writes
  inside `allowed_paths`, package installs, service restarts) and an always-blocked list
  (disk formatting, reboot, user/sudoers/firewall changes, `rm` on `/`, `authorized_keys`,
  `git push --force`, `curl | sh`, ...). The words are re-quoted before they reach the remote
  shell. `access = "full"` (unrestricted) is not available.
- **Read-only roles stay read-only on servers:** every role but the coder is capped at `basic`;
  profiles can lower access, never raise it.
- **SSH is strict and non-interactive:** `BatchMode=yes`, `StrictHostKeyChecking=yes` (never
  `no` or `accept-new`), `ConnectTimeout=10`; keys come from ssh-agent or an `identity_file`
  path. jev never stores passwords or key contents, and refuses reads of credential files
  (`~/.ssh`, private keys, `.env`, `/etc/shadow`, ...).
- **Audit.** Every server command and file operation, allowed or refused, is a redacted line in
  `.jev/audit/servers.log`.
- **Prompt injection.** Output from servers and web pages is untrusted; it can try to steer the
  agent. The policy bounds the damage; keep levels low and secrets off servers agents read.
- **Browser.** The Playwright MCP server is pinned (`@playwright/mcp@0.0.83`) and runs with an
  in-memory profile (`--isolated`), headless by default. `jev browser install` shows the exact
  command and asks first; on a server it needs medium access and goes through the policy.

## Repository config files

Treat a cloned repository's `jev.toml` as untrusted input. jev only lets it change the keys
that decide where API keys are sent, which credential variables are read, which commands the
workers run with which permissions, and which tools, servers and browser agents get, when your
user config trusts that repository (`[trust] repos`, docs/config.md). A repository cannot trust
itself. Its test commands (`test_command`) still run in the repository, as running its tests
always does: read them in the plan before you approve it.

## Keys and redaction

- Config files never hold secrets: any key named like `*key`, `*token*`, `*secret*` or
  `*password*` is rejected. You name the environment variable with `api_key_env` instead.
- Keys reach workers only through the environment: an `Authorization` header for HTTP calls,
  the inherited environment for CLIs. They are never put in argv, prompts or config.
- Every log is redacted before it is written (`jev_agent.runlog.redact`), and text is redacted
  before it is cut short (summaries), so a cut never leaves part of a secret. Redacted:
  well-known credential shapes (`sk-...` keys; GitHub `ghp_`/`ghs_`/`github_pat_`, Slack
  `xox?-`, AWS `AKIA`, Google `AIza` tokens; private-key blocks; JWTs; `Bearer` and
  `Authorization: token|Basic` values; `user:password@` in URLs), the values (8+ characters) of environment variables whose names contain KEY,
  TOKEN, SECRET, PASSWORD or CREDENTIAL, and the values (4+ characters) of the variables named
  in `JEV_REDACT_NAMES` (set by jev to the MCP servers' variables). The API worker also
  scrubs the literal value of its
  `api_key_env`, whatever the variable is called. Request headers and the environment are never
  logged. Error messages from Jev never include the key or the response body of an auth error.
- `jev doctor` reports whether key variables are set, never their values.

## Recommended permission settings

- Keep the defaults above.
- Grant the Claude coder only the extra Bash commands it needs, as narrow patterns, for example
  `allowed_tools = [..., "Bash(uv run pytest *)"]`. Avoid bare `Bash`. The pipeline runs each
  task's test command itself afterwards anyway.
- **Do not** enable `[workers.claude_code] skip_permissions` (`--dangerously-skip-permissions`),
  `[workers.codex] sandbox = "danger-full-access"`, or pass
  `--dangerously-bypass-approvals-and-sandbox` through `extra_args`, except in a throwaway VM or
  container with no credentials and no network. With them, the agent can run any command with
  your privileges. These are opt-in only, and `jev doctor` warns while any is on.
- `claude -p` skips the workspace-trust dialog and still loads project hooks, `.mcp.json` and
  `CLAUDE.md`. Run jev only on repositories you trust, or set `bare = true`.
- Set `[workers.claude_code] max_budget_usd` if you want a tighter per-attempt cap than the
  per-task budget.

## Terms of use

- **API keys are the supported path** for automated use: `ANTHROPIC_API_KEY` for Claude Code
  (required with `bare = true`), `CODEX_API_KEY` or `OPENAI_API_KEY` for Codex, and
  `OPENROUTER_API_KEY` (or `TYPESAFE_API_KEY`) for the API worker and Jev. Codex's docs call API
  keys "the right default for automation".
- Anthropic reserves subscription (OAuth) login for ordinary use of Claude Code and tells
  developers building products to use API keys. No explicit OpenAI clause on scripted
  subscription use was found (unverified).
- jev never reads, stores or forwards subscription credentials; it only passes your environment
  through to the CLIs. If you run the CLIs on your own subscription login, check that your plan
  permits automated use.
