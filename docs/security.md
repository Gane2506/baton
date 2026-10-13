# Security

How jev limits what agents can do, how it handles keys, and what to check before you run it.
Background: [PLAN.md](../PLAN.md) sections 10 and 16.5, [verification.md](verification.md)
sections 2, 3 and 5. Config keys are in [config.md](config.md).

## Defaults

- **Worktree isolation.** Every coder task runs in its own git worktree under
  `.jev/worktrees/<run_id>/`, on a branch `jev/<run_id>/<task_id>`, never on your current
  branch. Integration merges task branches into a local `jev/<run_id>/result` branch for you to
  review.
- **No push.** jev never runs `git push`. Nothing leaves your machine through git.
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

## Keys and redaction

- Config files never hold secrets: any key named like `*key`, `*token*`, `*secret*` or
  `*password*` is rejected. You name the environment variable with `api_key_env` instead.
- Keys reach workers only through the environment: an `Authorization` header for HTTP calls,
  the inherited environment for CLIs. They are never put in argv, prompts or config.
- Every log is redacted before it is written (`jev_agent.runlog.redact`): `sk-...` keys, `Bearer`
  tokens, the values (8+ characters) of environment variables whose names contain KEY,
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
