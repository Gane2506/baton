<!-- Thanks for contributing. See CONTRIBUTING.md, "Before you open a pull request". -->

## What and why

<!-- A few sentences: what this changes and why. One issue per pull request. -->

Closes #

## How it was tested

<!-- Tests added or changed; confirm a new test failed before the fix. Any manual check,
     e.g. a run on the sample repo with JEV_FAKE_WORKERS=1. Screenshots for dashboard changes. -->

## Coding agents

<!-- Did an agent (Claude Code, Codex, ...) write part of this? Which parts, and what you
     checked yourself. Justify any new abstraction or dependency. "None" is fine. -->

## Changes for existing users

<!-- Config keys, defaults, CLI output, state database, what an agent may do. "None" is fine. -->

## Checklist

- [ ] `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest -q` passes
- [ ] Meets the code-quality bar in CONTRIBUTING.md: a reviewer can trace it, see how it
      fails and verify it without guessing
- [ ] No clutter: no unused helpers, speculative options, commented-out code, or new
      `# type: ignore` / `# noqa` / loosened checks
- [ ] A bug fix has a test that failed before the fix; new behaviour has tests
- [ ] No network or real CLIs in tests; no secrets, home-directory paths or email addresses
- [ ] Docs updated: `docs/config.md` for config fields, `examples/jev.toml` if the init
      template changed, the feature's doc in `docs/`, `docs/security.md` if agent permissions changed
- [ ] `CHANGELOG.md` "Unreleased" entry for user-visible changes
- [ ] `docs/decisions.md` entry for a design choice or a deviation from PLAN.md
- [ ] `uv.lock` updated if dependencies changed
- [ ] Commits are tidy and the branch is up to date with `main`
