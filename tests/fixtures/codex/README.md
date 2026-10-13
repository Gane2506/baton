# Codex CLI fixtures

Stdout of `codex exec --json` (JSONL events), **synthesized from the documented shapes, not
captured from live runs**. Sources: https://developers.openai.com/codex/noninteractive.md, the
event types in `sdk/typescript/src/events.ts`, and the usage-limit strings in
`codex-rs/protocol/src/error.rs` (`UsageLimitReachedError`, with its curly apostrophe), as
summarized in `docs/verification.md` section 3 (checked 2026-10-12 against codex-cli 0.162.1).
That limit messages arrive in `turn.failed.error.message` or an `error` event is inferred, not
documented. Values are illustrative; exit codes and stderr are set in the tests.
