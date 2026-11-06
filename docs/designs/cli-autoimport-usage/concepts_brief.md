# Auto-import Claude Code / Codex CLIs + show account usage (weekly and session)

* **Author:** Hana (Baton program, issue #396)
* **Date:** 2026-11-06

## Overview

When the Baton dashboard runs on a device where the Claude Code CLI and/or the
Codex CLI are already installed and signed in, the Connections → Providers
section detects those logins automatically, records them as imported from the
device (no manual re-auth through the dashboard's paste-token / device-code
flows), and shows each account's remaining usage for the 5-hour session window
and the weekly window, refreshed in the background. Every state — imported,
CLI missing, not logged in, usage unavailable — is visible in the dashboard
with an actionable next step, and nothing needs the terminal.

## Anchored hypothesis

The dashboard host already holds working logins; Baton should reuse them
instead of asking the user to sign in a second time, and it should show what
is left of each account's allowance in the two windows the providers meter
(5-hour session, weekly), using each CLI's own mechanism for the numbers.

## What exists today (verified in the repo at main 61c5000)

- `src/jev_agent/core/provider_auth.py` already detects CLI logins, read-only:
  Claude Code precedence is `ANTHROPIC_API_KEY` env, then
  `CLAUDE_CODE_OAUTH_TOKEN` env, then `~/.claude/.credentials.json` holding
  `claudeAiOauth` (the interactive CLI login, kind `cli_login`), then nothing.
  Codex precedence is `OPENAI_API_KEY` env, then `~/.codex/auth.json`
  holding `tokens` (ChatGPT subscription OAuth) or `api_key`, then nothing.
  Values are never returned, logged, or rendered — names and set/not-set only.
- The Connections page (`routes_connections.py`, `views.cli_providers`,
  `templates/connections.html` Providers section) already shows the detected
  login: for Claude, "The `claude` CLI is signed in — workers reuse that
  login." Workers shell out to the CLIs with the full process environment, so
  a CLI login already works for non-bare runs. The manual flows (Claude
  `setup-token` 1-click + paste-token fallback, Codex device-code flow) are
  offered only when detection finds nothing.
- The live poll (`connections_live`) already re-runs detection, so a login
  made in a terminal appears on the next poll.
- Gaps: there is no explicit "imported from this device" state or record, no
  account identity is shown (which account / which plan), no usage is shown
  anywhere, and bare-mode workers do not read the CLI login (existing warning
  in `provider_auth.py`).

## Usage sources (verified, not invented)

### Claude Code — remaining usage

- The CLI's own source is an **undocumented** OAuth endpoint that Claude Code
  itself queries for its `/usage` screen:
  `GET https://api.anthropic.com/api/oauth/usage` with
  `Authorization: Bearer <oauth access token>` and header
  `anthropic-beta: oauth-2025-04-20`. Response carries `five_hour` and
  `seven_day` objects, each `{utilization: 0-100, resets_at: ISO-8601}`, plus
  `seven_day_opus` / `seven_day_sonnet` splits and a `limits[]` array.
  Verified live (HTTP 200) by independent community projects (ccstatusline,
  orca/tortie, ailimits, a VS Code status-bar extension) — not by Baton.
- The token is the CLI login's access token from
  `~/.claude/.credentials.json` (`claudeAiOauth.accessToken`, with
  `expiresAt`); an expired token is renewed against
  `https://console.anthropic.com/v1/oauth/token` with the refresh token from
  the same file. On macOS the credentials may live in the Keychain
  (`Claude Code-credentials` service) instead of the file.
- Limits of this source: the endpoint rate-limits itself (shared bucket with
  the CLI's own polling; HTTP 429 with `Retry-After`, roughly one request in
  three succeeds under an active session). Anthropic's terms reserve Pro/Max
  sign-in tokens for Claude Code and claude.ai. API-key auth has no
  subscription windows at all (only per-minute `anthropic-ratelimit-*`
  headers). Whether the endpoint accepts the long-lived `sk-ant-oat01-`
  setup-token (saved by the dashboard's own flow) is **unverified**.
- Fallbacks that are real but weaker: the status-line hook (Claude Code pipes
  a `rate_limits` block — `five_hour` / `seven_day` with `used_percentage`
  and `resets_at` — to a configured statusLine command, but only for Pro/Max
  subscribers and only after the first API response of a session, and only
  while a session runs); local session files (`~/.claude/projects/*.jsonl`)
  show consumption only — remaining allowance is never written to disk.
  Driving the interactive `/usage` screen through a PTY is fragile and is
  not a source.

### Codex — remaining usage

- The CLI ships a hidden local JSON-RPC server its own TUI uses:
  spawn `codex app-server` (stdio JSONL), send `initialize`, then
  `account/rateLimits/read`. Response carries `rateLimits.primary` and
  `.secondary`, each `{usedPercent: 0-100, windowDurationMins, resetsAt}`
  (300 = 5h, 10080 = weekly), plus `planType`, `rateLimitResetCredits`,
  `rateLimitReachedType`, `spendControlReached`, `ordinaryUsageAllowed`.
  **Verified live on this VM** (codex-cli 0.164.0-alpha.9): the initialize
  handshake answers, and the rate-limits call fails cleanly with
  `codex account authentication required to read rate limits` because this VM
  has no login — the protocol surface exists in 0.164.0 and the failure mode
  is explicit. `codex doctor` independently reports auth state.
- Rules from the protocol's own documentation: do not hardcode two windows —
  derive them from `windowDurationMins` (the 5-hour window is suspended for
  many accounts, so `secondary` may be null and only a weekly window
  remains). Do not drive the TUI (`/status` needs a real terminal).
  Spawn discipline: `cwd` a temp dir, scrubbed env, 15 s timeout, SIGTERM
  after the answer, 4 MiB output cap.
- Weaker fallback: the CLI writes a `rate_limits` snapshot into
  `~/.codex/sessions` files. The web dashboard
  (`chatgpt.com/codex/settings/usage`) needs a browser login and is not a
  source for Baton.

## Analysis

- The credential half of "auto-import" is mostly built: detection exists and
  workers already reuse a CLI login. The missing piece is making that reuse
  explicit (an imported state the user can see and trust), proving it is the
  device's own login (source path + detection time, names only), and covering
  the bare-mode gap where the CLI login is not read.
- The usage half is new for both providers, but both have a real source that
  uses the CLI's own auth and never moves a token off the device except to
  the provider's own endpoint (Claude) or not at all (Codex, local stdio).
- The two sources are asymmetric in risk: Codex's is the CLI's own local
  protocol (no network call by Baton, no undocumented endpoint); Claude's is
  an undocumented server endpoint that can change, 429, or reject a token
  kind. The design must treat them differently instead of pretending they
  are the same kind of source.
- "Remaining" is a server-side number in both cases. Anything computed from
  local session files is consumption, not remaining, and must never be
  presented as remaining.
- Refresh cadence matters for both: the Claude endpoint throttles its own
  callers, so Baton must cache and poll sparsely (minutes, not seconds) and
  show staleness honestly. The Codex app-server spawn is cheap but still a
  subprocess per refresh; it needs a timeout and a cached result.

## Goals vs. Non-Goals

* **In-Scope Goals:**
  * Detect installed / logged-in / missing / not-logged-in for each CLI on
    the dashboard host, and record + show the login as imported from the
    device with its source and detection time.
  * Show remaining usage for the 5-hour session window and the weekly window
    per provider, with reset times, from the verified sources above.
  * Every state visible and actionable in the dashboard with zero terminal
    use; manual sign-in flows stay as fallback when there is nothing to
    import.
  * Read-only auth handling: tokens/keys never displayed, logged, or copied
    out; usage calls send tokens only to the provider's own endpoint.
* **Out-of-Scope Non-Goals:**
  * Changing how workers authenticate or run (no worker changes).
  * Signing the user in automatically (import reuses an existing login; it
    never creates one).
  * Usage history, cost estimates, or cross-device usage (local session
    files only see this machine; the weekly bar is the account's).
  * A public API or CLI command for usage (dashboard only in this issue).

## Key Decisions & Staging for Next Steps

* Carry to the design doc as open decisions with evidence, not as settled
  facts: (1) where usage results are cached and how often they refresh
  (storage: state.db vs a JSON cache file; cadence per provider);
  (2) whether "imported" is a recorded state or derived live on each render;
  (3) what account identity can honestly be shown (Codex `planType` is in
  the rate-limits response; Claude plan is not in the usage response);
  (4) the posture for the undocumented Claude endpoint (always-on with
  honest fallback vs opt-in);
  (5) the bare-mode gap (workers in bare mode do not read the CLI login);
  (6) whether the `sk-ant-oat01-` setup-token is tried against the usage
  endpoint or only the CLI-login access token is.
* Staging: design doc under `docs/designs/` → five-stage Goldfish review →
  resolve → implement on the issue branch with a draft PR → CI green →
  orchestrator verification → Mac verification of the real device flow
  (real logins, real usage) → merge → close #396 manually.

## Brainstorm synthesis (six Goldfish lenses, 2026-11-06)

Reports in `~/workspace/baton-program/issue396-brainstorm/`:
`lens-yagni.md`, `lens-cost.md`, `lens-ux.md`, `lens-migration.md`,
`lens-observability.md`, `lens-debt.md`. All six were read in full. What
they agree on, and what the design doc must therefore decide (not leave
open):

* **Wording:** "imported" is wrong — nothing is copied or owned by Baton.
  One term everywhere: "detected on this device". The two OAuth kinds must
  stop sharing the label "Subscription login (OAuth)".
* **Import state is derived live, not recorded.** A recorded login claim
  goes stale when the user signs out in a terminal. Provenance lives in
  the usage cache record (fingerprint + `fetched_at`), not in a login
  record; live detection stays authoritative.
* **Precedence collision is undecided and load-bearing.** A dashboard-saved
  token outranks a CLI login silently; removal silently promotes the login
  underneath. The design needs a precedence table for (saved token × CLI
  login × API key) per provider: which credential workers use, which one
  usage is measured against, what the card shows. Default: never silently
  replace or hide a saved credential.
* **Failure taxonomy, enumerated.** One "unavailable" bucket hides eight
  causes (no login, refresh failed, 401/403 incl. setup-token rejection,
  429, no network, changed shape, hung app-server, API-key auth with no
  windows). Each gets a reason code, UI wording, and next step.
* **State machine and budgets.** `never` / `collecting` / `ready` /
  `stale` / `failed`, reusing the `ServerStatsCache` pattern; render and
  live poll never trigger or wait on a fetch; Claude 10–15 min minimum
  interval, Codex 5 min or refresh-on-view; `Retry-After` honored;
  exponential backoff to a 60-min cap; circuit breaker after 3
  consecutive failures (1 h quiet, then one probe); 401/403 retry once,
  then stop until auth changes; idle rule (no refresh while the usage UI
  has not been viewed recently); rate-limited manual Refresh.
* **Cache is host-level, not repo-level.** `~/.jev/` (user level), keyed
  by credential/account fingerprint, invalidated on auth change, errors
  never overwrite good cache, `fetched_at` / `next_retry_at` /
  `consecutive_failures` / `last_status` persisted so restarts do not
  reset backoff.
* **One adapter per provider behind a `ProviderUsageSource` protocol**
  returning a normalized snapshot (window list derived from the response,
  never hardcoded window names). No provider-specific literal outside its
  adapter. Per-source kill switch. Usage failure never breaks login
  detection, workers, or the page.
* **Token refresh: Baton never writes to `~/.claude/.credentials.json`.**
  A refreshed token lives in dashboard process memory only. Setup-token
  tried against the endpoint once per token; a rejection is recorded and
  never retried per cycle.
* **Keychain: file-only in this issue, with the gap documented.** A
  Keychain-only login is not detected by file-only detection; the card
  must name the location checked rather than claim "not logged in"
  absolutely. Keychain read is a named follow-up.
* **Bare mode:** usage is not shown for a CLI login that bare-mode
  workers cannot use; the card carries the existing warning.
* **API-key auth:** usage section hidden with a one-line explanation
  ("billed per token, no windows apply"), no bars, no fetch attempts.
* **Account identity:** show only what a source returns (Codex
  `planType`; Claude: none — render absence as absence, never infer a
  plan). Identity is part of the cache key.
* **Layout:** progressive disclosure — one compact line per provider
  ("Session 62% left · resets in 2 h · Weekly 81% left"), details behind
  a disclosure; bar direction decided once: "left" (remaining), reset
  times in human phrasing, never raw ISO.
* **Consent/removal:** plain-language note that Baton reads the CLI's own
  sign-in and never copies or stores it; to stop Baton using it, sign
  out of the CLI. A dashboard opt-out is out of scope, named as a gap.
* **CLI missing** stays a documented exception to zero-terminal (the npm
  install command); the zero-terminal promise narrows to viewing usage
  and signing in.
* **Value, stated honestly:** usage changes no automated Baton decision.
  Its value is the human decision before starting a long run — whether
  allowance remains. The user asked for it as a priority; that is the
  justification, stated plainly, not dressed up as automation.
