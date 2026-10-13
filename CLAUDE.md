# CLAUDE.md: Baton Agent Orchestrator (`jev`)

Context for Claude Code sessions working on this repo. Read this first, then the files it points to **only when the task needs them** —
the repo docs are the source of truth; don't re-derive what's written there.

State as of 2026-10-13: `main` = `a6dc73c`, CI green (Python 3.11–3.13), 932 tests passing,
working tree clean, released **v0.2.0**; everything since (dashboard, quality gate, servers,
ship/KPIs) is unreleased.

## 1. What this is

An open-source Python CLI that turns an idea into reviewed, tested code by running a pipeline
of AI agents on a git repo. **Jev** (TypeSafe's "System One" decision model) is the dispatcher:
at every decision point it picks which agent/worker, model and effort; a deterministic rules
dispatcher is the fallback. Workers: Claude Code CLI, Codex CLI, OpenRouter API models.
Original spec: `PLAN.md` (it says "baton"/milestones M0–M6 — historical; all done).

Names (after two renames — don't rename again without being asked):
- Product: **Baton Agent Orchestrator**. PyPI dist: `baton-agent-orchestrator` (not yet
  published). Console scripts: `jev` (main) and `baton-agent-orchestrator` (alias).
- Import package: `jev_agent` (PyPI `jev` is an unrelated TypeSafe decorator lib — never name
  our import `jev`). Repo config `jev.toml`, state dir `.jev/`, branches `jev/<run_id>/...`,
  env vars `JEV_*`. "jev" in `[dispatcher] primary = "jev"` / `JevDispatcher` means the Jev
  *model*, not the tool.
- Old names in history: `baton` (plan), `jao` (v0.1.0 CLI). `jev init --migrate` upgrades
  v0.1 repos.

## 2. Repo and git

- Repo: https://github.com/Gane2506/baton, branch `main`. History starts at a single
  clean commit for v0.2.0; earlier history is kept privately and is not needed.
- Push with `git`/`gh` (the `workflow` scope is needed to change files under
  `.github/workflows/`).
- Commit messages end with the current Co-Authored-By attribution line. Reference issues:
  `Closes #N`.

## 3. How the maintainer wants work done (important)

- **Don't stop for approval between steps/milestones**; post a short summary and continue.
- **Delegate** substantial work to background subagents (general-purpose), with precise
  briefs: files they own, files they must not touch, "no git state changes" (you commit),
  finish command, and a concise report format. Parallel agents only on disjoint files; when
  they share files (config/schema.py, README, CHANGELOG, docs/config.md, cli/app.py), tell
  them to re-read before each edit and keep self-contained blocks, then commit together.
- **GitHub issues as the public progress log:** before starting any feature/fix, open an
  issue (or comment on the existing one) with the plan, label it `in progress`, post progress
  comments as pieces land, and close it from the commit (`Closes #N`). Labels in use:
  `enhancement`, `bug`, `in progress`, `priority: low`, `priority: medium`, `priority: next`,
  `needs decision`. After a review, file **every** finding as an issue before fixing any of them.
- **Decisions:** for non-trivial choices (design options, keep/refactor/rewrite, ranking
  findings) use the `jev-decide` skill (`.claude/skills/jev-decide/`). Jev decides: its direct
  pick plus gates/weighted matrix; disagreements go back to Jev automatically (follow-up
  rounds, then Jev's arbitration). Act on the result; never override it. Outward-facing steps
  still need the maintainer's approval.
- **Ask first** before outward-facing or hard-to-reverse steps not yet approved: publishing
  to PyPI, making any repo public/private, tagging releases (ask; the last two tags were
  approved per-release), enabling `access = "full"`, anything that costs real money (live
  tests with real models/APIs — a ~$1–2 cap was fine last time).
- After each change: `uv run ruff format . && uv run ruff check . && uv run mypy && uv run
  pytest -q` (also `COLUMNS=40 uv run pytest -q`), push, then watch CI:
  `RID=$(gh run list -R Gane2506/baton --workflow CI --limit 1 --json databaseId -q '.[0].databaseId'); gh run watch $RID -R Gane2506/baton --exit-status`.
- Before pushing anything public, scan for personal data:
  grep for home-directory paths (`/Users/`, `/home/`) and email addresses in changed files.
  Never commit secrets; tests use obvious fakes.

## 4. Safety rules learned the hard way

- **Headless Chrome** (used for screenshots/PNG renders): ALWAYS pass
  `--headless=new --user-data-dir=<fresh mktemp -d dir, verified to exist>` and delete it
  after, so a real browser profile is never touched. In zsh never name variables `path`,
  `status`, `prompt` (they clobber shell state); prefer Python scripts for loops.
- **Server access `"full"`** is deliberately unimplemented: Claude Code's auto-mode
  classifier blocked the first attempt. Config rejects it ("see issue #3"). Only revisit if
  the maintainer explicitly allows it in their permission settings.
- Tests never hit the network or real CLIs (claude/codex/gh/ssh/npx/browser): everything goes
  through injectable runners and fakes (`JEV_FAKE_WORKERS=1`, fake `gh` in
  `tests/fixtures/gh/fake_gh.py`, local bare git remotes). Live tests live in `tests/live/`,
  skipped unless `JEV_LIVE_TESTS=1`.
- Pages/images for docs use demo data from `examples/demo/seed_demo.py` in a
  `mktemp -d /tmp/baton-demo.XXXX` repo with XDG dirs inside it (no home paths leak).

## 5. Architecture map (read the code/doc only when needed)

`src/jev_agent/`:
- `cli/` — typer app (`app.py`) + one module per command group; `wiring.py` builds the
  dispatcher/workers/pipeline from config (`cli_context`, `start_run(profile=, learning=,
  quality=)`).
- `core/` — `models.py` (pydantic), `store.py` + `migrations.py` (SQLite `.jev/state.db`,
  **schema version 7**: runs, tasks, attempts, decisions, learning, queue, quality_reports,
  shipped_prs/run_kpis), `pipeline.py` (run state machine INTAKE→PLANNING→VALIDATING→
  AWAITING_APPROVAL→EXECUTING→INTEGRATING→DONE), `tasks.py`, `harness.py` (dumb: stores,
  asks dispatcher, executes; enforces budgets/limits), `workspace.py` (git worktrees,
  dependent-task bases, `refs/jev/<run>/start`), `integration.py`, `learning.py`/`learner.py`,
  `queue.py`/`scheduler.py`, `chat.py`, `ship.py`/`kpi.py`/`benchmarks.py`, `assign.py`.
- `dispatch/` — `requests.py` (D1–D7 compact cards), `rules.py`, `jev.py` (System One API,
  OpenRouter `/api/v1/systemone` or native TypeSafe; confidence gate 0.55), `guard.py`
  (answer validation + rules fallback).
- `workers/` — `claude_code.py`, `codex.py`, `api.py` (OpenRouter), `fake.py`, `tools.py`
  (per-role MCP/skills/plugins), `openrouter_models.py` (free models), `subprocess_runner.py`.
- `roles/` — Jinja prompt templates (StrictUndefined): planner, validator, coder, reviewer,
  code_reviewer, learner, chat; `schemas.py`.
- `quality/` — gate levels none/basic/medium/production/scalable, 17 checks, AI review.
- `servers/` — SSH policy (basic/medium), ssh transport, audit log, stdio MCP server
  (`jev mcp servers`), browser install, profiles.
- `dashboard/` — Starlette + Jinja2 (optional extra `dashboard`), vanilla `static/live.js`,
  pages: board, run, queue, learning, chats, spend, KPIs, settings.
- `config/` — `schema.py` (+ `servers_schema.py`), `defaults.py` (the `jev init` template;
  `examples/jev.toml` must equal it — a test checks), `loader.py` (defaults < user config <
  repo `jev.toml`).
- `runlog/` — redaction + log writer.

Docs (`docs/`): `decisions.md` (**newest first; every deviation/choice — read the top
entries before changing a feature**), `config.md` (a test fails if a schema field is
undocumented), `verification.md` (verified external API facts, numbered sections 1–9),
`security.md`, `roles.md`, `learning.md`, `queue.md`, `chat.md`, `quality.md`,
`dashboard.md`, `servers.md`, `ship.md`. Plus `README.md` (logo header, badges, feature
gallery from `docs/images/`), `CHANGELOG.md` (Keep a Changelog; "Unreleased" lists all
post-0.2.0 work), `CONTRIBUTING.md`.

Images: `docs/images/` (logo SVGs/PNGs, `social-preview.png`, `dashboard-*.png`,
`cli-*.svg`). Regenerate: `examples/demo/seed_demo.py` + `examples/demo/capture.py`
(terminal SVGs via rich) + headless Chrome for dashboard PNGs (see docs/dashboard.md).

CI: `.github/workflows/ci.yml` (lint+mypy, tests 3.11/3.12/3.13, `uv sync --locked
--all-extras`, ubuntu-24.04, checkout@v7, setup-uv@v10.3.0 — setup-uv has no floating major
tags). `release.yml` on `v*` tags: checks, tag==version, build, GitHub Release; PyPI job
gated by repo variable `PUBLISH_PYPI=true` + trusted publishing (not set up).

## 6. Feature status

Done and on `main`: full pipeline D1–D7 with Jev + rules fallback; Claude Code/Codex/API
workers; budgets, audit log, resume, cancel; per-role MCP tools/skills/plugins; recursive
learning profiles (repo/shared scope); `jev chat`; idea queue + scheduler + `jev board`; free
OpenRouter models (`prefer_free`, rate-limit fallback chain); quality & security gate;
`jev init --migrate`; web dashboard; servers over SSH (basic/medium) + browser + workspace
profiles (#3); `jev ship` + KPIs + benchmarks (#8); logo, screenshots, feature gallery.

Live-tested once (2026-10-12, ~$1.11): plan→approve→DONE on the sample repo with Jev via
native TypeSafe, Claude Code planner+coder, Codex validator.

**Never tested live yet:** OpenRouter api worker + free models; MCP tools in real
Claude Code/Codex runs; `jev chat` with real backends; quality gate with real scanners;
server tools over real SSH (incl. `SSH_AUTH_SOCK` forwarding to the MCP server); Playwright
browser install on Linux; `jev ship` against a real GitHub repo (`gh pr view --json` value
shapes unverified); the release workflow on the new action versions.

## 7. Open items / where we left off

Open issues on Gane2506/baton:
- **#59** tracking issue for the 2026-10-13 full code review: 48 findings filed as #11-#58,
  priorities set by Jev (30 next, 10 medium, 8 low). Fix only what the maintainer approves.
- **#10** (medium, needs decision): semantic cache for model calls (exact-match first;
  semantic match opt-in, never for the coder).
- **#9** (low): dashboard Settings — left sidebar, typed editors (toggle, enum, number+unit,
  tags, key/value, path, secret-ref, model picker, per-role matrix), validated save with diff,
  repo vs user target, comment-preserving (tomlkit), reset-to-default, search.
- **#2** (low): macOS app + full Mac control (needs stack decision, signing/notarization).

Pending maintainer decisions (ask, don't assume):
1. Tag **v0.3.0** (everything in CHANGELOG "Unreleased"): bump `pyproject.toml` version,
   move CHANGELOG entries under `## [0.3.0] - <date>`, update the README version badge
   (static `version-0.2.0` shield) and status line, `uv lock`, commit, `git tag -a v0.3.0`,
   push the tag, watch the Release workflow (first run on checkout@v7/artifact@v7/@v8).
2. Publish to PyPI (needs trusted publishing + `PUBLISH_PYPI=true`).
3. Upload `docs/images/social-preview.png` in repo Settings → Social preview (manual; no API).
4. A live test round (servers, browser, chat, free models, ship) with a spend cap.
5. Whether to allow `access = "full"` for servers.

Known small gaps (from agent reports): chat sessions get no server tools (only the runs
they start); `[workers.claude_code] extra_args` ignored by chat; dashboard queue form doesn't
validate profile names; Ship button only for the first PR per run; inline PR review comments
not counted; rich markup not escaped in `ChatError`/wiring messages (`[workers.api]` text
can vanish); `mkdir/cp/mv/rm` path checks on servers are lexical (file tools resolve
symlinks); dashboard per-run lock doesn't cover a concurrent `jev resume` from a terminal.

## 8. Useful commands

```bash
uv sync --all-extras                      # dev env (dashboard extra included)
uv run jev --help                         # all commands
uv run pytest -q                          # 932 tests, no network
gh issue list -R Gane2506/baton           # open work
grep -n "^## " docs/decisions.md | head   # latest design decisions
# demo repo for manual checks (run from the repo root):
REPO=$PWD; D=$(mktemp -d /tmp/baton-demo.XXXX); cp -R examples/sample-repo/. "$D" && cd "$D" && git init -q && git add . && git -c user.name=demo -c user.email=demo@example.com commit -qm init && uv run --project "$REPO" jev init && JEV_FAKE_WORKERS=1 uv run --project "$REPO" jev run --plan-only "Add a word_frequencies() function"
```
