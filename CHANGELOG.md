# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Design detail for every entry is in
[docs/decisions.md](docs/decisions.md).

## [Unreleased]

## [0.2.0] - 2026-10-12

### Added

- **Learning profiles and the D7 learner.** Optional recursive learning: a learner step at the
  end of a run turns problems into short lessons, kept per profile in the repo or in a shared
  database, and injected into later prompts. `jev learning` manages profiles and lessons;
  `jev run --no-learning` skips them for one run. See [docs/learning.md](docs/learning.md).
- **`jev chat`.** Talk an idea through with a model that can read the repo, get a brief for the
  planner, then plan, run or queue it from the chat. See [docs/chat.md](docs/chat.md).
- **Idea queue, scheduler and Kanban board.** `jev queue`, `jev scheduler start` (run limits and
  a daily budget) and `jev board` (with `--watch`). See [docs/queue.md](docs/queue.md).
- **Free OpenRouter models.** `[models] prefer_free`, the `"free"` model entry for api roles,
  fallback on rate limits, and `jev models free` / `jev models list`.
- **Per-role agent tools.** MCP servers, skills and Claude Code plugins defined once under
  `[tools]` and given to roles by name; `jev doctor` lists them.
- **Quality and security gate.** `[quality] level` / `--quality` with levels `none`, `basic`,
  `medium`, `production` and `scalable`: linters, tests, scanners and an AI code review after
  each task and at integration, with reports via `jev quality show`. See
  [docs/quality.md](docs/quality.md).
- **`jev init --migrate`** moves a repo set up with v0.1.0 (`jao.toml`, `.jao/`, `jao/*`
  branches) to the new names.

### Changed

- **CLI renamed `jao` → `jev`**, with `jev.toml`, `.jev/`, `JEV_*` environment variables and
  `jev/<run_id>/...` branches. Run `jev init --migrate` in existing repos.
- **Import package is now `jev_agent`** (was `jao`).
- **Project renamed to Baton Agent Orchestrator.** PyPI distribution `baton-agent-orchestrator`
  (was `jev-agent-orchestrator`), long command alias `baton-agent-orchestrator` (the
  `jev-agent-orchestrator` alias is removed), repository
  `github.com/Gane2506/baton`.
- **Rules dispatcher D4 complexity heuristic:** files changed are now the main signal (hard
  needs 4+ files or 8+ acceptance criteria), so small tasks with many criteria are no longer
  rated "hard".

## 0.1.0 - 2026-10-12

Released before this public repository; its history is not included here.

First release, with the CLI named `jao`.

- Idea-to-code pipeline: plan, validate, approve, code, test, integrate, on local branches in
  isolated git worktrees; nothing is pushed.
- Jev dispatcher (via OpenRouter or native TypeSafe) at decision points D1 to D6, with a
  deterministic rules dispatcher as fallback.
- Workers: Claude Code CLI, Codex CLI and an OpenAI-compatible API worker (OpenRouter by
  default); planner, validator, coder and reviewer roles with custom prompt templates.
- Per-run and per-task budget caps, an audit log of every decision and attempt, redacted logs.
- Commands `init`, `doctor`, `run`, `approve`, `status`, `log`, `resume` and `cancel`.
- Times shown in UTC; a broken prompt template stops a run in a resumable `NEEDS_HUMAN`.
- Tag-triggered release workflow.

[Unreleased]: https://github.com/Gane2506/baton/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Gane2506/baton/releases/tag/v0.2.0
