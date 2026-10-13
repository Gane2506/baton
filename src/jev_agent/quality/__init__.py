"""The optional quality and security gate (docs/quality.md).

- `levels`: the levels `none` < `basic` < `medium` < `production` < `scalable`.
- `models`: findings, check results and the stored gate result.
- `tools`: `ToolRunner`, which runs tool commands for checks (timeouts, truncation, redaction).
- `checks`: the registry of checks (`CHECKS`) and how to add one; the protocol and shared
  helpers are in `base`, the checks in `checks_basic`, `checks_medium`, `checks_production`.
- `policy`: which results block at which level.
- `review`: the AI code review (role `code_reviewer`).
- `report`: Markdown reports.
- `gate`: runs one gate (checks, AI review, policy, reports) for a task or the integration.

This file imports nothing so the config schema can import `levels` without cycles.
"""
