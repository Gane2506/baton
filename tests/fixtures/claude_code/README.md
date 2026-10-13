# Claude Code fixtures

Stdout of `claude -p --output-format json` (and two `stream-json` samples), **synthesized from
the documented shapes, not captured from live runs**. Sources: the `SDKResultMessage`,
`rate_limit_event` and `system/api_retry` shapes in
https://code.claude.com/docs/en/agent-sdk/typescript.md and headless.md, and the user-facing
error strings in https://code.claude.com/docs/en/errors.md, as summarized in
`docs/verification.md` section 2 (checked 2026-10-12 against Claude Code 2.1.114). Values are
illustrative. Exit codes and stderr for each case are set in the tests.
