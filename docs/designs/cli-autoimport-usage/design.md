# Detect Claude Code / Codex CLI logins + show account usage (weekly and session)

* **Author:** Hana (Baton program, issue #396)
* **Status:** Draft — resolve pass after the §9.0 probe (2026-11-06): the Codex 43200/monthly gap is resolved and the Claude Keychain path is decided as a Phase B prerequisite; Phase A go, Phase B blocked on the re-probe (§9.0)
* **Date:** 2026-11-06

--------------------------------------------------------------------------------

## Overview

When the Baton dashboard runs on a device where the Claude Code CLI and/or
the Codex CLI are already installed and signed in, the Connections →
Providers section detects those logins automatically and shows them as
detected on this device — no manual re-auth through the dashboard's
paste-token / device-code flows when a working login already exists. For
each provider it also shows how much is left, in the windows the provider
reports: the 5-hour session window, the weekly window, and (Codex)
the monthly window, each with its reset time. Codex may report only
one window — weekly, or monthly on the Mac's free-plan account (its
5-hour window is suspended for many accounts). Every state —
detected login, CLI missing, Not connected, usage unavailable, usage stale
— is visible in the dashboard with a next step where one exists, and
viewing usage needs no terminal.

Usage is fetched only when the user asks for it: a provider's usage is
checked the first time the user clicks "Check usage" on its card, and
after that whenever the Connections page is opened while the cached
snapshot is older than the cadence floor. There is no background
refresher. A probe ran before implementation starts (§9.0, records in
`probe/`, summary `probe/PROBE.md`, 2026-11-05 on the Mac): the Codex
success shape was observed live (item 1 passed — primary +
usedPercent present), and it exposed one mapping gap (a 30-day
window the design's table did not cover) resolved in §11; the Claude
items could not run because no file-stored token exists on the Mac
(the tokens live in the macOS Keychain), which this resolve turns
into a Phase B prerequisite (D10) with a re-probe (§9.0) that runs
on the Phase B branch. Delivery is two PRs: Codex first (plus the
shared plumbing), Claude second.

## 1. The problem

Today the dashboard can tell the user a CLI login exists, but it cannot
tell them what is left of the account behind it. A user about to start a
long run has no way, inside Baton, to answer "do I have enough left for
this, or will I hit the limit halfway through?" The answer lives in the
CLIs themselves (`/usage` in Claude Code, `/status` in Codex) or on the
providers' web pages, which means leaving the dashboard and opening a
terminal or a browser. The issue also asks for the login side to be
explicit: when a working login already exists on the device, Baton should
treat it as the login in use and say so plainly, instead of offering a
sign-in flow for a problem that does not exist.

Two facts shape the whole design. First, the credential half is built:
detection already exists and workers already reuse a CLI login, so the
new work there is wording, precedence, and honesty about edge cases —
not a new auth mechanism. Second, the usage half is new for both
providers, and the two sources are different kinds of thing: Codex usage
comes from the CLI's own local protocol (no network call by Baton), while
Claude usage comes from an undocumented server endpoint that can change,
throttle, or reject a token kind. The design treats them differently
instead of pretending they are the same kind of source.

### Words used in this design

* **Detected on this device** — a login the CLI on the dashboard host
  created and owns, which Baton found by reading the CLI's own files.
  Baton never copies, stores, or owns it. The UI does not use
  "imported". This document uses "import" only when it names the
  rejected alternative in §7.
* **Saved by Baton** — a token the user pasted or the dashboard's own
  flow produced, stored in `~/.jev/secrets.env` (mode 600). An
  environment variable is not "saved by Baton" — it is an env var, and
  the tables in §6.2 label it as one.
* **Left** — the share of a window not yet used, shown as "62% left".
  The providers report *used* (`utilization` / `usedPercent`); the UI
  always shows 100 minus that value. "Allowance" and "remaining usage"
  are not used.
* **Session window** — the provider's short meter (5 hours when the
  provider reports one; Codex may report no secondary window, §6.3).
  **Weekly window** — the long meter (7 days). **Monthly window** —
  the 30-day meter (Codex only, observed on the Mac's free-plan
  account, §6.3). The UI labels are **Session**, **Weekly**, and
  **Monthly** only. `five_hour` / `seven_day` are response field
  names, never window names. Codex durations are written once, in one
  unit: 300 minutes (= 5 hours) is the Session window; 10080
  minutes (= 7 days) is the Weekly window; 43200 minutes (= 30 days)
  is the Monthly window.
* **Usage snapshot** — one provider's last fetched usage: the
  provider, its windows, its plan when the source reports one, the
  source version, and the time it was fetched. The outcome of the
  fetch lives in the separate `UsageUnavailable` type (§6.1).
* **Reason code** — a short stable string naming why a usage fetch did
  not produce fresh data (§6.5). The UI renders the reason in plain
  words; logs carry the code.
* **Single-flight** — at most one fetch per provider runs at a time; a
  second request returns the cached value instead of starting another
  fetch.
* **Kill switch** — a config setting (`[usage] claude_code = false`)
  that turns a usage source off without a code change.
* **Background fetch** — a fetch that runs on a daemon thread while the
  page render continues; the render never waits on it.
* **Mode 600** — the file can be read and written only by its owner.
* **Atomic write** — write to a temp file in the same directory, then
  rename over the target, so a crash mid-write leaves the previous good
  file intact.
* **JSON-RPC** — the request/response message format the Codex
  app-server speaks over its standard input and output (one JSON object
  per line).
* **App-server** — the hidden local server inside the Codex CLI
  (`codex app-server --listen stdio://`), which its own TUI uses and
  which Baton asks for rate limits.
* **Opt-in** — nothing is fetched for a provider until the user clicks
  "Check usage" on its card once (D3).

## 2. What exists today

* `src/jev_agent/core/provider_auth.py` detects CLI logins read-only.
  Claude Code precedence: `ANTHROPIC_API_KEY` env, then
  `CLAUDE_CODE_OAUTH_TOKEN` env, then `~/.claude/.credentials.json`
  holding `claudeAiOauth` (kind `cli_login`), then nothing. Codex
  precedence: `OPENAI_API_KEY` env, then `~/.codex/auth.json` holding
  `tokens` (ChatGPT subscription OAuth) or `api_key`, then nothing.
  Values are never returned, logged, or rendered — names and set/not-set
  only. The two OAuth kinds share the label "Subscription login (OAuth)".
* The Connections page (`routes_connections.py`, `views.cli_providers`,
  `templates/connections.html`, Providers section) shows the detected
  login. For Claude it says "The `claude` CLI is signed in — workers
  reuse that login." The manual flows (Claude `setup-token` 1-click +
  paste-token fallback, Codex device-code flow) are offered only when
  detection finds nothing. Workers shell out to the CLIs with the full
  process environment, so a CLI login already works for non-bare runs.
* The live poll (`connections_live`, via `static/conn.js` every 15 s)
  re-runs detection, so a login made in a terminal appears on the next
  poll. The poll JSON carries `provider`, `auth`, `label`, `installed`
  per provider — no usage, no timestamps.
* `ServerStatsCache` (`src/jev_agent/servers/stats.py`) is the pattern
  this design reuses: `get()` never blocks, `refresh()` starts a
  background collection and returns False when one is already running,
  results persist as JSON, last-good is kept on failure, states are
  `never` / `collecting` / `ready` / `failed`. (The usage fetcher
  follows the pattern but returns a `RefreshResult`, not a bool —
  §6.4/§9.1.)
* `CodexDeviceFlow` / `ClaudeSetupTokenFlow` in `provider_auth.py` are
  the existing background-flow pattern: a lock, a watcher thread, a
  state machine, `cancel()` / `shutdown()`.
* `httpx` is a core dependency (used by the dispatcher, chat backends,
  Figma, workers). No new HTTP library is needed.
* No usage code exists anywhere: no usage endpoint call, no app-server
  spawn, no usage cache, no usage UI. `codex` 0.164.0-alpha.9 is present
  on this VM; `claude` is not.

## 3. Goals and non-goals

### Goals

* Detect installed / logged-in / missing / Not connected for each CLI
  on the dashboard host, and show the login as detected on this device
  with its source. **Detection is file-first:** detection checks the
  CLI's credentials file; on macOS a Claude login whose token lives
  in the Keychain (no credentials file) is detected via the
  `oauthAccount` metadata in `~/.claude.json` (presence only, values
  never read) and the card names the Keychain as the source (D10).
  The token itself is obtained from the Keychain only on explicit
  user action, in Phase B. The card names the source checked.
* Show what is left for the session window, the weekly window, and
  (Codex) the monthly window per provider, with reset times, from the
  verified sources in §6.3. All windows come in one response, so one
  fetch covers them all. Codex may report only one window.
* Every state visible in the dashboard; states with a user action name
  the next step, and states with none say so. Viewing usage and signing
  in needs no terminal, except for the documented exceptions in §6.6
  (CLI missing, Keychain-only login, signing in again inside the CLI).
* Read-only auth handling: tokens/keys never displayed, logged, or
  copied out. A usage call sends a token only to the provider's own
  endpoint (Claude) or nowhere at all (Codex, local stdio).
* Usage failure does not change the login state shown on the card, and
  does not break workers or the rest of the Connections page. A dead
  usage source degrades to "usage unavailable" with a reason; the login
  state next to it is unaffected.

### Acceptance criteria

  fetch on view succeeds unless the provider rate-limits or the fetch
  fails (network, timeout, auth, unexpected shape); on a 429 the card
  shows the reason and the next-retry time, and the last-good value is
  hidden once stale (D19). At the unmeasured one-in-three success
  estimate, ~3 attempts reach a fresh value — ~30 minutes of
  15-minute-spaced attempts; the probe (§9.0 item 3) measures the real
  rate and sets the expected-freshness note.
* A 429 never produces a wrong number and never feeds a failure
  backoff (D7).
* Usage failure never changes the login state shown on the card.
* No token value appears in logs, cache, or UI (test-enforced, §9).
* The Mac gates in §10 pass per phase (Codex live, then Claude live).

### Non-goals

* Changing how workers authenticate or run (no worker changes), except
  the bare-mode wording qualification in D11.
* Signing the user in automatically. Detection reuses an existing login;
  it never creates one.
* Usage history, cost estimates, or cross-device usage. The cache holds
  one snapshot per provider, overwritten in place.
* A public API or CLI command for usage (dashboard only in this issue).
* A dashboard on/off toggle for usage (named follow-up, §8). The
  off control in this issue is the `[usage]` kill switch (config) plus
  the per-provider opt-in (D3).
* Fixing bare-mode workers to read the CLI login (named follow-up,
  D11). The design names the effective credential instead.

## 4. Critique first: what can go wrong

Six Goldfish lenses reviewed the concepts brief
(`docs/designs/cli-autoimport-usage/concepts_brief.md`,
`goldfish-brainstorm/lens-*.md`), and nine Goldfish reviewers reviewed
the first draft of this design (`review/consolidated_review_report.md`).
The findings that changed this design:

* **The credential half is a label change.** Detection and worker reuse
  already exist. A recorded login state would be a second source of
  truth that goes stale when the user signs out in a terminal — so the
  design derives the detected-login state live and records nothing
  about the login itself.
* **Usage changes no automated Baton decision.** Nothing pauses, queues,
  or switches providers on what is left. Usage does not change any
  automated Baton decision. The value is the human decision before a
  long run. The design states that plainly instead of dressing it up
  as automation.
* **"Imported" is the wrong word.** It promises a copy Baton holds. The
  design uses "detected on this device" everywhere and splits the two
  OAuth kinds into distinct labels. (The first draft broke its own ban;
  the resolve pass fixed every use.)
* **Dual auth is invisible today.** A saved token silently outranks a
  CLI login, and removing it silently promotes the login underneath
  while the badge text does not change. Section 6.2 has a precedence
  table for every combination, and §6.1 makes the table the test list
  for one function — the UI renders whatever that function returns.
* **One "unavailable" bucket hides eight failures.** Section 6.5 has an
  enumerated reason-code taxonomy; 429, 401, and 403 are different
  states with different next steps.
* **The 15-second live poll is the risk.** Riding usage on it would
  send a usage request on every poll: 15 s poll × 2 tabs × 3600 s =
  480 requests/hour. Section 6.4 decouples fetching from the poll and
  the render path: the poll never triggers a fetch, and a render only
  starts a background fetch it never waits on.
* **The cache must be host-level, not repo-level.** Usage is per account
  on the device; per-repo caches would multiply fetchers by repo count.
  Section 6.4 pins the cache to `~/.jev/`, one file per provider.
* **Two providers, two mechanisms, one shared module would put both
  providers' parsing, transport, and error handling in one module.**
  Section 6.1 puts one adapter per provider behind a
  `ProviderUsageSource` protocol returning a normalized snapshot; no
  provider-specific response key, URL, or method name lives outside its
  adapter.
* **Baton never writes to the CLI's credentials file, and never
  refreshes the CLI's token.** The first draft refreshed the token in
  memory; the review showed that can destroy the login (refresh-token
  rotation). An expired token is now `token_expired_refresh_failed` and
  the CLI, which owns the file, does the refresh (D8).
* **The bare-mode claim is false today.** The card says workers reuse
  the login while bare-mode workers do not read it. D11 names the
  effective credential instead of fixing the worker path.
* **The Claude endpoint is undocumented and rate-limited; the Codex
  protocol is hidden and versioned.** Both get a failure contract and a
  kill switch (§6.5, §6.9). The Codex success shape had never been
  observed by Baton when this design was written — only the
  auth-required failure — so a probe ran before implementation (§9.0)
  and observed it (§11 R1); Mac verification re-observes it on the
  merged code (§10).
* **The first draft's own numbers defeat its Claude half.** A
  15-minute cadence at the stated (unmeasured) one-in-three success
  rate means ~45 minutes to one successful refresh, and backoff then
  guarantees the card stays stale. The rewrite treats 429 as routine
  (D7) and drops the backoff ladder and circuit breaker: the cadence
  floor already bounds the attempt rate.
* **A stale number the user trusts is worse than no number.** The
  first draft showed the stale value with a label; the rewrite hides
  the value when stale (D19).

## 5. Decisions

Every decision is recorded here with its rationale. Picks follow the
user's standing principles: browser-first, least surprise, lowest
resource use. The user corrects a wrong pick after the fact; the design
does not stall waiting for a pick. Decisions marked "amended" changed in
the Goldfish resolve pass; the reasons are in
`review/consolidated_review_report.md`.

* **D1 — Wording: "detected on this device", not "imported" (amended).**
  Nothing is copied or owned. The UI says "Detected on this device —
  the `claude` CLI is signed in" and "Detected on this device — the
  `codex` CLI is signed in with ChatGPT". The two OAuth kinds stop
  sharing one label: a dashboard-saved OAuth token is "Signed in with
  your Claude subscription (saved by Baton)"; a CLI login is "Detected
  on this device". For Codex there is no "saved by Baton" label: the
  dashboard's own device-code flow writes tokens into the same
  `~/.codex/auth.json` detection reads, so a dashboard-created Codex
  login is indistinguishable by file location from a CLI-created one,
  and the detected-login label is used for both. *Rationale:* least
  surprise — the words must not promise what the code does not do.
* **D2 — Detected-login state is derived live on every render/poll;
  nothing is recorded about the login (amended).** Detection re-runs on
  each page render and each 15-second poll (as today). No `detected_at`
  is shown: it would always read "just now" and carries no
  information. Provenance lives in the usage cache record (`fetched_at`
  and, for Codex, `account_hash`), not in a login record. *Rationale:*
  live derivation cannot go stale when the user signs out in a
  terminal.
* **D3 — Opt-in per provider, not always-on (rewritten).** Nothing is
  fetched for a provider until the user clicks "Check usage" on its
  card once. The choice persists in the cache record (`enabled: true`).
  After that, fetching follows D6. The per-provider off-ramp is the
  card's "Turn off usage checks" action (`POST
  /connections/providers/{provider}/usage/disable` → `enabled: false`;
  the card returns to the never state with a note that checks are off).
  The kill switch (`[usage]`) remains the admin off control (config
  file, restart to apply). *Rationale:*
  the user asked for the numbers visible as a priority, and the
  opt-in is one click on the card itself — not a settings page. With
  fetch-on-view (D6) there is no background use of the token to opt
  out of; the only fetch is the one the user triggers. A dashboard
  Settings toggle is a named follow-up.
* **D4 — Two PRs: Codex first, Claude second (rewritten).** Phase A PR
  = Codex adapter + shared plumbing (model, cache, fetcher, UI,
  precedence, wording) — merge gate: Codex live on the Mac. Phase B
  PR = Claude adapter only — merge gate: Claude live on the Mac. Each
  PR is independently verifiable and mergeable. *Rationale:* one PR
  with one merge gate couples the halves at merge time; a Codex
  failure would block the Claude half even though the adapters are
  independent in code.
* **D5 — Cache is one host-level JSON file per provider (rewritten).**
  `~/.jev/usage-cache-claude_code.json` and
  `~/.jev/usage-cache-codex.json`, mode 600, atomic write (temp file +
  rename, reusing the approach of `write_secret` in
  `src/jev_agent/core/secrets.py`). One snapshot per provider,
  overwritten in place. Record per provider: `schema_version` (1),
  `enabled` (D3), `fetched_at` (last successful fetch),
  `last_attempt_at` (last attempt), `last_status`, `last_reason`,
  `next_retry_at` (ISO-8601 UTC, null when none — set from
  `Retry-After` on a 429, cleared on a successful fetch),
  `source_version` (CLI version / adapter version), `account_hash`
  (Codex only — SHA-256 hex digest of the UTF-8 bytes of `accountId`,
  §6.7), and the `windows`
  payload. No token values, no usage history. `~/.jev/` is created if
  missing (mode 700). A corrupt or unparseable cache file is treated
  as empty and logged once; the card then shows not-checked-yet visibly
  ("Check usage"). *Rationale:* usage is per account on the
  host device, not per repo — a repo-level cache would run one fetcher
  per open repo against the same per-token bucket. One file per
  provider kills the lost-update race (each process writes only its
  own provider's file). `~/.jev/` is where `secrets.env` already lives.
* **D6 — Trigger model and cadence (rewritten).** **Trigger:** the
  Connections page render **may start a background fetch** (fire-and-
  forget, in a daemon thread) when the cached snapshot is absent or
  older than the cadence floor **and** the provider is enabled (D3)
  **and** the kill switch is on (`[usage] <provider>`, §6.9).
  The render **never waits** on it and always serves the cached
  snapshot immediately. The 15-second live poll **never triggers** a
  fetch; it serves cached usage only. Manual Refresh starts a fetch
  subject to the same floor. Dashboard start triggers **nothing**.
  **Cadence floors (minimum time between attempts, manual or
  otherwise, measured from `last_attempt_at`):** Claude 15 minutes (4 requests/hour max); Codex
  5 minutes. A manual click inside the floor is ignored (redirect +
  flash naming the wait). One timer per provider covers both windows
  (they arrive in one response). The 15-minute Claude cadence is a
  **starting value, to be checked by the probe (§9.0 item 3) and at
  Mac verification** — the success rate it rests on is unmeasured
  (D7, §6.3). *Rationale:* lowest
  resource use — the numbers are only needed when the user looks at
  them, and the Claude endpoint shares its rate-limit bucket with the
  CLI's own `/usage` polling.
* **D7 — 429 is routine; 401/403 has a defined re-arm (rewritten).**
  **429:** honour `Retry-After` exactly when present, parsed as
  seconds or HTTP-date (`email.utils.parsedate_to_datetime`);
  unparsable → treated as absent (wait at least the cadence floor).
  A 429 sets `next_retry_at` in the cache record (D5) and does **not**
  trigger backoff. There is no exponential backoff ladder, no circuit
  breaker, and no failure tally: the cadence floor already bounds the
  attempt rate, and backoff on routine 429s guarantees a stale card.
  **Non-429 failures** (network, timeout, unexpected shape) simply
  wait for the next view or manual attempt after the floor.
  **401/403:** retry once, then stop **in process memory only**. The
  retry is not immediate: the first 401/403 is counted, and exactly
  one retry is allowed on the next trigger after the floor; if the
  retry also returns 401/403, the fetcher stops that provider until
  (a) the detected login changes (kind or source path, per live
  detection — Codex also re-arms on an `account_hash` change), or
  (b) the user clicks Refresh (manual Refresh overrides the stop). A
  dashboard restart clears the stop; nothing about it is persisted.
  For Claude there is no identity signal: if the CLI refreshes its
  own token (same file, same kind/path), the fetcher cannot see the
  change — the way back is the Refresh click, named in the next step
  (§6.5). For a Keychain login the Refresh click re-reads the Keychain
  (a manual click always re-reads, D10), so a CLI-rotated Keychain token
  is picked up and the stop clears; a background fetch never re-reads,
  so it cannot re-arm the provider on its own. *Rationale:*
  the design's own numbers showed backoff is the normal path under a
  one-in-three success rate, not the exception — and a permanent
  401 wedge with no way back is worse than a retry.
* **D8 — Never refresh the CLI's token (rewritten).** The adapter
  reads the access token and `expiresAt` from the `claudeAiOauth`
  object and sends the access token as-is. If the token is expired,
  the fetch fails with reason `token_expired_refresh_failed` ("Sign
  in again in Claude Code") and the CLI, which owns the file, does
  the refresh. Baton never refreshes, never writes a refreshed token
  anywhere, and never writes to `~/.claude/.credentials.json`.
  *Rationale:* if Anthropic rotates refresh tokens, Baton consuming
  the CLI's refresh token and keeping the replacement only in memory
  leaves the CLI's stored refresh token dead — the refresh path
  itself could destroy the login it was meant to protect. The
  rotation behaviour is unproven, so the safe default is not to
  refresh at all.
* **D9 — Setup-token acceptance is settled by the probe, as a general
  fact (rewritten).** Whether the `sk-ant-oat01-` setup token is
  accepted by the usage endpoint is settled by the probe (§9.0) for
  all users at once — not tried once per token at runtime. If
  accepted, setup tokens are treated as ordinary OAuth tokens. If
  rejected, the adapter never tries them, and the card shows reason
  `setup_token_rejected` ("Usage can't be read with a setup token.
  Use the CLI sign-in instead."). There is no per-token tracking.
  *Rationale:* "tried exactly once per token" has no mechanism (no
  token identity, no storage field) and builds permanent logic for an
  unverified guess.
* **D10 — Keychain: the Claude token path on macOS, a Phase B
  prerequisite (rewritten — probe resolve 2026-11-06).** The probe
  (§9.0 item 2) found no file-stored Claude token on the Mac:
  `~/.claude/.credentials.json` does not exist and `~/.claude.json`
  holds `oauthAccount` profile metadata only — while the login
  itself exists (Claude Code works). On Claude Code 2.1.318 the
  tokens live in the macOS Keychain (service
  `Claude Code-credentials`). So the Keychain read is not a
  follow-up: Phase B cannot fetch Claude usage without it, and the
  permission UX is part of this design. The rules:
  * **Detection is file-first and permission-free.** Detection checks
    the credentials file first (`~/.claude/.credentials.json` with
    `claudeAiOauth` → kind `cli_login`, as today). If the file is
    absent and `~/.claude.json` carries `oauthAccount`, the login is
    detected as kind `keychain_login` (new `LoginSource` kind, §6.1)
    — presence only, from a plain metadata file the dashboard can
    already read; no Keychain access is needed to detect. The
    `~/.claude.json` read goes through the shared
    `read_credential_file` (missing/corrupt/non-dict → None, never
    raises); `oauthAccount` must be a non-empty dict to count as a
    Keychain login (null/empty → no Keychain login), and its `uuid`
    is the token-cache key (absent → the constant `"keychain"`).
    If both the file and the Keychain hold a login, the file wins
    (the file token is used; the Keychain token is never read, so
    the usage shown is the file token's account — the two could in
    principle belong to different accounts, and the file account is
    the one shown). If neither exists, the
    card shows Not connected as today.
  * **The token is read only on explicit user action, never on
    render, poll, or background fetch.** For a `keychain_login`, the
    card's "Check usage" click is the grant action (one click, not
    two): the click reads the token from the Keychain, caches it in
    process memory (below), and starts the usage fetch. Before the
    click the card names the Keychain source and warns that a system
    prompt will appear on the Mac — "choose 'Always Allow' so you
    are not asked again". The Keychain prompt names the `security`
    command-line tool (the helper Baton spawns to read the item);
    "Always Allow" grants that helper, and the grant persists for
    future `security` invocations — the user is granting the helper,
    not the Baton dashboard. Item 0 (§9.0) records the prompt's
    actual wording; if it names something else, the card copy is
    corrected to match. A background fetch never touches the
    Keychain: it uses the cached token only, and if no token is
    cached it does not run (the card stays in the awaiting-grant
    state, not "failed"). The seam is in §6.3: the adapter's
    `fetch()` takes a pre-resolved token and never calls the Keychain
    reader; only the manual "Check usage"/Refresh handler calls
    `read_keychain_item`, on the request thread (up to the 120 s
    bound — the page shows a "waiting for the Keychain prompt"
    state), and `maybe_fetch` skips a `keychain_login` with no
    cached token.
  * **Transport: the `security` CLI.** `security
    find-generic-password -s "Claude Code-credentials" -w`,
    invoked through an injected spawner (same injection seam as the
    Codex spawner, §8 testability — tests pass a fake). Read-only:
    the Keychain item is never written. The stored JSON is parsed
    the same way as the file's `claudeAiOauth` object (same parser,
    same D8 no-refresh rule); the item's exact key shape is
    unverified until the re-probe (§9.0), which records the key
    names (never Keychain item values). If the shape differs, the re-probe
    records it and the design returns for resolve.
  * **Caching: process memory only.** The token is cached in memory,
    keyed to the `oauthAccount` uuid (the login identity; absent →
    the constant `"keychain"`), and never written to disk, to the
    usage cache, or to any log. **A manual "Check usage" click always
    re-reads the Keychain** — a fresh token on every manual click —
    so a CLI-rotated token is picked up and Refresh is a real way
    back from a 401 (D7); the cost is a possible re-prompt per click
    unless "Always Allow" was chosen, and the card copy says so.
    Background fetches reuse the cached token and never re-read; a
    cached token whose `expiresAt` has passed is not used by a
    background fetch (the adapter returns
    `token_expired_refresh_failed` without a request). The cache is
    invalidated when the `oauthAccount` uuid changes (a different
    login), when the login disappears (detection no longer finds it
    — including sign-out while a token is cached), and on dashboard
    restart (memory is cleared — the next manual click re-reads; if
    the user chose "Always Allow" no prompt reappears, if "Allow
    once" it does, and the card copy says so). A 401/403 after a
    non-expired token follows D7.
  * **Failures have their own reason codes (§6.5).**
    `keychain_denied` — access was not granted (denied, dismissed,
    or not answered within the 120-second read bound); the card's
    next step is to click "Check usage" and choose Allow.
    `keychain_unavailable` — the Keychain itself could not be
    reached (no `security` command, non-macOS platform, headless
    session with no login session to prompt in, unexpected error).
  * **Scope: macOS only.** On Linux and Windows there is no
    Keychain path; detection and token reading stay file-only
    (unchanged). The Keychain code path is gated on darwin.
  *Rationale:* the probe proved the file path does not exist on the
  very device Phase B must verify on — a design that keeps Keychain
  as a follow-up would ship a Phase B that cannot run on the Mac.
  Splitting by permission boundary keeps the work honest: detecting
  needs no permission (plain metadata file), reading needs the
  user's explicit grant, so the grant rides on the click the user
  already makes (D3) instead of inventing a second permission step.
  The whole Keychain story (detection, reader, grant UX, caching,
  reason codes) lands in Phase B as one unit (§9.2); Phase A keeps
  file-only Claude detection with the limitation stated and
  time-boxed to Phase B. The `security` CLI is chosen over the
  native Keychain API because it is a stdlib subprocess with no new
  dependency and the read is rare and user-triggered; the native
  API is the named alternative if the subprocess proves fragile.
* **D11 — Bare mode: name the effective credential (amended).** The
  card shows the CLI login as detected, but names the credential
  workers actually use, and usage is shown only for that credential.
  In bare mode a CLI login is not usable by workers, so no usage bars
  are shown for that source; a saved token or API key is the working
  path in bare mode, and usage **is** shown against it. Codex has no
  bare mode. *Rationale:* showing a number next to "workers reuse
  that login" when bare workers cannot spend it is a false claim.
  Fixing bare-mode workers to read the CLI login is a separate issue
  (named follow-up).
* **D12 — API-key auth: usage section hidden, not "unavailable".** No
  usage calls are attempted for `api_key` auth, and no bars are shown;
  one line explains: "Billed per token — no subscription windows apply
  to an API key." *Rationale:* an API key has no windows to show, and
  "unavailable" would invite support questions and retry clicks.
* **D13 — Account identity: show only what a source returns
  (amended).** Codex's response carries `planType` — shown, rendered
  verbatim (`plus` / `pro` / etc.; unknown values verbatim). Claude's
  usage response carries no plan — the card says "plan not reported by
  this provider" and no plan is inferred from the token, the login
  file, or the usage numbers. The Opus/Sonnet splits **are** shown
  when present: they are windows the source returns, and the lowest
  remaining among the weekly windows is the binding one (§6.3).
  *Rationale:* inferring a plan from a number is a number the source
  did not report; discarding data the source does return hides the
  binding limit.
* **D14 — Bars show "left", reset times in relative phrasing
  (amended).** One compact line per provider, details behind a
  disclosure: "Session 62% left · resets in 2 h 14 min · Weekly 81%
  left · resets in 3 d" — and for a monthly-only account, "Monthly
  52% left · resets in 20 d" (the Mac's observed values at probe
  time, §6.3). The compact line lists the windows the response
  carries, each with its own label (Session, Weekly, Monthly, and
  the split labels); there is no Weekly line when no weekly window
  exists, and no Monthly line when no monthly window exists. All times are relative ("updated 12 min ago",
  "last seen 2 h ago"); no clock times anywhere. Rounding rule:
  under 1 hour → minutes ("9 min"); under 1 day → hours + minutes,
  minutes shown when ≥ 1 ("2 h 14 min", "1 h 30 min"); 1 day or more
  → days only, rounded down ("3 d"); under a minute → "just now".
  Percent is shown as an integer, rounded half up (62.5 → 63). The bar
  fills to **remaining**.
  *Rationale:* the issue asks for what is left; the reset time is the
  decision-relevant half.
* **D15 — CLI missing stays a documented exception to zero-terminal.**
  When a CLI is not installed, the card keeps the install command
  (existing behaviour: `npm i -g @anthropic-ai/claude-code` /
  `npm i -g @openai/codex`). The zero-terminal promise narrows to
  "viewing usage and signing in needs no terminal". An in-dashboard
  install is out of scope. *Rationale:* browser-first, but a dashboard
  package install is a separate feature, not a line of this one.
* **D16 — Consent note describes the network call (rewritten).** Each
  detected-login card carries: "Clicking "Check usage" lets Baton
  check usage with the provider whenever you open this page, until you
  turn it off. Baton sends your sign-in to <provider> for each check.
  It does not write the sign-in to disk or share it. To stop Baton using it, turn
  off usage checks on this card or sign out in the CLI." The provider
  name is "Claude" or "Codex" (shared copy owned by the template, name
  substituted). Plus: "If a token saved by Baton is also present, that
  token stays in effect until you delete it in the dashboard." For a
  login stored in the Mac's Keychain (D10, Phase B), the card adds:
  "Your sign-in lives in this Mac's Keychain — clicking "Check usage"
  reads it from there. A system prompt will appear; choose "Always
  Allow" so you are not asked again. Baton keeps the sign-in in memory
  only, never on disk."
  *Rationale:* the new act in this issue is sending the sign-in over
  the network, not reading a file — the note must describe that act,
  including that one click authorises later page opens until stopped.
* **D17 — No locally computed "left" ever (amended).** If a source is
  down, the card shows "usage unavailable" with the reason and the
  last-fetched time — never a number derived from local session files
  (`~/.claude/projects/*.jsonl`, `~/.codex/sessions`), which record
  consumption only. The rule is enforced by a test (§9: no locally
  computed remaining appears in the snapshot), not by the model
  shape — a missing dataclass field prevents nothing. *Rationale:* a
  computed number labelled as left is a number the source did not
  report, and once users have seen a number, removing it looks like
  a regression even when the number was wrong.
* **D18 — Probe gate before implementation (new).** Implementation does
  not start until the probe records exist (§9.0). *Rationale:* the
  design was written before either usage source was observed; the
  contracts between its parts were guesses. A probe that fails costs
  an afternoon; building the full architecture on a wrong guess costs
  the whole PR.
* **D19 — Stale value hidden, not shown with a label (new).** When a
  snapshot is older than max-age, the value is hidden; the card shows
  "last seen <relative time> — check again before a long run" +
  Refresh. *Rationale:* §4's principle — a stale "72% left" the user
  trusts to start a long run is worse than no number. A label does
  not undo the trust the number creates, and with fetch-on-view the
  user is one click from a fresh attempt.
* **D20 — Max-age is per window (amended).** A window is fresh while
  younger than its own max-age: the session window, four times the
  cadence floor (Claude 60 min, Codex 20 min); the weekly window,
  24 hours; the monthly window, 24 hours — the same as weekly.
  A window older than its max-age is hidden (D19); the card
  is stale only when no window is fresh, and the compact line lists
  only the fresh windows. *Rationale:* the max-ages were arbitrary in
  the first draft; the 4× rule ties the fast window to the cadence it
  depends on, while the slow weekly window does not need re-checking
  every hour. The monthly window takes the weekly rule because its
  value moves just as slowly; a longer max-age would only delay
  hiding a stale value, not save fetches (the cadence floor, not
  max-age, is what bounds the attempt rate).

## 6. Technical plan

### 6.1 Architecture: one adapter per provider behind a protocol

New module `src/jev_agent/core/usage/`:

* `__init__.py` — the protocol, the normalized model, the
  reason-code constants, and `derive_display_state` (§6.4).
* `claude.py` — the Claude adapter (HTTP endpoint; no token refresh).
* `codex.py` — the Codex adapter (local app-server JSON-RPC).
* `cache.py` — host-level cache record per provider (read/write,
  atomic, mode 600) and storage only — single-flight lives in the
  fetcher (§6.4), not the cache.
* `fetcher.py` — one background-fetch owner shared by both providers:
  starts a fetch when the page asks for one, enforces the cadence
  floor, owns single-flight and the daemon threads, `shutdown()`.

The protocol:

```python
class ProviderUsageSource(Protocol):
    def fetch(self) -> UsageSnapshot | UsageUnavailable: ...
```

The normalized model (the only shape the page, route, live poll, and
template ever see):

```python
@dataclass(frozen=True)
class UsageWindow:
    kind: str            # "session" | "weekly" | "monthly" | "weekly_opus" | "weekly_sonnet" — derived by the adapter
    used_percent: float  # 0-100, as reported
    resets_at: datetime | None  # as reported; None → bar with "no reset reported"

@dataclass(frozen=True)
class UsageSnapshot:
    provider: str
    windows: list[UsageWindow]   # derived from the response, never hardcoded as two fields
    plan: str | None             # Codex planType, verbatim; Claude: None
    fetched_at: datetime         # last successful fetch
    source_version: str          # "cli/<cli_version> parser/<PARSER_VERSION>", e.g. "cli/0.164.0-alpha.9 parser/1"; "unknown" when the CLI version is unavailable

@dataclass(frozen=True)
class UsageUnavailable:
    provider: str
    reason: str                  # reason code, section 6.5
    fetched_at: datetime | None  # last successful fetch, if any
    last_good: UsageSnapshot | None  # last-good windows, if any
```

Rules: each adapter parses its vendor response exactly once, inside its
own module, behind a versioned parser (the parser version is a module
constant, e.g. `PARSER_VERSION = 1`, recorded in `source_version`).
Unknown fields are ignored; missing or mistyped expected fields produce
`UsageUnavailable(reason="unexpected_response")`, never an exception and
never a fabricated zero. The template renders windows from the list, so
adding or removing a window is a data change, not a template change.
**Provider response keys, URLs, and method names live only in the
adapter. Reason codes are provider-neutral. The human wording lives in
one mapping per provider, inside that provider's adapter module**
(reason code → UI wording + next step); the shared layer and the
template only pass and render reason codes. Each adapter module
exposes the mapping as `WORDING: dict[str, tuple[str, str]]`
(reason code → (UI wording, next step)); the §6.5 table is the
specification of the two mappings' contents, and the template holds no
reason wording of its own. File paths shown on cards
come from the accessor's returned values, not literals. Each adapter is
registered behind the protocol; dropping or replacing a source is
unregistering one adapter and deleting one module. The kill switch is
config: `[usage] claude_code` / `[usage] codex` (default true) — a
broken source becomes `UsageUnavailable` with reason
`disabled_by_config`, with no code change. All datetimes are
timezone-aware UTC: Codex unix seconds convert to UTC datetimes;
Claude ISO-8601 parses with `datetime.fromisoformat`, a naive value
treated as UTC.

`provider_auth.py` keeps detection, plus one shared credential-source
accessor used by both detection and the usage adapter:

```python
@dataclass(frozen=True)
class LoginSource:
    kind: str            # "env_api_key" | "env_oauth_token" | "saved_oauth_token" | "cli_login" | "keychain_login" | "api_key_file" | "none"
    source_path: str | None   # credential file path, when the login comes from a file; None for "keychain_login" (the card renders the fixed Keychain service name from the adapter's constant, D10)
    cli_version: str | None   # one "<cmd> --version" run per dashboard process start, 5 s timeout, cached in memory, None on failure
```

`get_login_source(provider, *, env=None, home=None) -> LoginSource`
is the single place a storage move is fixed, where `provider` is
`"claude_code"` | `"codex"`, `env` defaults to `os.environ` and `home`
to `Path.home()` (tests pass fakes for both). `claude_code_auth` /
`codex_auth` call it internally. The precedence tables in §6.2 are the
**test list** for one resolution function in `provider_auth.py` that
decides, for every combination of credentials, which credential workers
use, which credentials are present, and which credential usage is
measured against — always the effective credential. The function is:

```python
@dataclass(frozen=True)
class CredentialResolution:
    effective: LoginSource            # the credential workers use
    present: tuple[LoginSource, ...]  # every credential kind present, top-to-bottom precedence order
    usage_source: LoginSource | None  # the credential usage is measured against; None when usage is hidden (API key in effect, bare-mode CLI login, or no login)

def resolve_credentials(
    provider: str, *, env: Mapping[str, str], home: Path, bare: bool = False
) -> CredentialResolution: ...
```

`claude_code_auth` / `codex_auth` call `resolve_credentials`
internally and render whatever it returns; the adapter receives its
token through the shared reader below.

**Token values:** "values are never returned" applies to the view, the
template, and the logs. The adapter, inside the trust boundary, reads
token values through **one shared reader** in `provider_auth.py`:

```python
def read_credential_file(path: Path) -> dict | None:
    """Parse a CLI credential JSON file. Returns the parsed object, or
    None when the file is missing, unreadable, invalid JSON, or not an
    object. Never raises."""
```

`read_credential_file` is the existing credential-file reader,
generalised: `_credentials_login` and `_codex_auth_file` are rewritten
on top of it (presence = non-None return plus the expected key), and
the Claude adapter reads the access token and `expiresAt` through it.
No second file reader exists anywhere. The macOS Keychain is not a
file, so it has its own reader, also in `provider_auth.py` (Phase B,
D10) — the only other credential reader in the codebase:

```python
@dataclass(frozen=True)
class KeychainResult:
    outcome: str         # "ok" | "denied" | "unavailable" | "missing"
    payload: dict | None # the stored JSON object on "ok", else None

def read_keychain_item(
    service: str, *, popen: PopenFn = subprocess.Popen, timeout_s: float = 120.0
) -> KeychainResult:
    """Read one macOS Keychain generic-password item via the `security`
    CLI (`security find-generic-password -s <service> -w`), parse the
    stored JSON, and return it. "denied" = access was not granted
    (denied, dismissed, or not answered within the bound); "unavailable"
    = the Keychain could not be reached (no `security` command,
    non-darwin platform, headless session, unexpected error); "missing"
    = the item itself is gone. Never raises; never logs or returns a
    token value outside the payload."""
```

Outcome mapping (by behaviour, not by number — the exact exit codes
and stderr strings for the deny/dismiss cases are recorded by
re-probe item 0, §9.0, which runs the real `security` CLI through
each case): exit 0 with non-empty stdout (stripped) → "ok", payload
the parsed JSON; exit 0 with empty stdout → "unavailable" (the item
holds no password); non-zero with stderr matching "could not be
found" → "missing" (the item is gone — the handler drops the cached
token and the card shows `keychain_unavailable` with the re-sign-in
next step, §6.5; the distinction is logged, not shown); prompt
answered Deny, dismissed, or unanswered when the 120 s bound expires
(the child is killed) → "denied"; `security` binary absent, non-darwin
platform, no GUI session to prompt in (headless), spawn failure, or a
non-zero exit with unrecognized stderr → "unavailable". Subprocess:
stdin DEVNULL, cwd a temp dir, stdout/stderr captured with a 4 KiB
stderr cap, 120 s timeout. The adapter's `keychain_reader` parameter
defaults to None, meaning the production `read_keychain_item`; tests
inject a fake. None is the production path, not a "no Keychain" mode.
Re-probe item 0 records Keychain item key names, never Keychain item
values; the only value read outside the token itself is the
`oauthAccount` uuid from `~/.claude.json` (the cache key, D10).

`LoginSource` is defined in
`provider_auth.py` and imported by `usage/__init__.py`, never
redefined. `ProviderAuth` gains additive fields with
defaults, so existing constructions do not break:

```python
cli_version: str | None = None
usage: UsageSnapshot | UsageUnavailable | None = None
present: tuple[str, ...] = ()        # all present credential kinds, from LoginSource.kind vocabulary
source_path: str | None = None       # credential file path, when the login comes from a file
```

### 6.2 Detected-login state and the precedence table

The detected-login state is derived live (D2). What changes is the
card: the source path, the effective credential, and which credentials
are present. **Baton never silently replaces or hides a saved
credential.** A CLI login never overrides a saved token or API key for
workers, and the card names which credential is in effect and which
others exist. Removing a saved credential (dashboard delete) promotes
the login underneath — the card text changes on the next poll to name
the newly effective credential, so the promotion is visible, not
silent. **Usage is always measured against the effective credential**
— the same one workers use.

**Claude Code — workers use the first present, top to bottom:**

| Env `ANTHROPIC_API_KEY` | Saved `CLAUDE_CODE_OAUTH_TOKEN` (secrets.env) | CLI login | Bare mode | Workers use | Usage measured against | Card shows |
|---|---|---|---|---|---|---|
| yes | — | — | no | API key | (none — hidden, D12) | "API key" + "a subscription login is also present" when a CLI login exists |
| yes | — | — | yes | API key | (none — hidden, D12) | "API key" (bare-mode working path) |
| no | yes | — | no | Saved token | Saved token | "Signed in with your Claude subscription (saved by Baton)" |
| no | yes | — | yes | Saved token | Saved token | "Signed in with your Claude subscription (saved by Baton)" (bare-mode working path) |
| no | no | yes | no | CLI login | CLI access token | "Detected on this device — the `claude` CLI is signed in" |
| no | no | yes | yes | CLI login **not** usable | (none for that source, D11) | Detection + bare-mode warning; usage suppressed; effective credential named (none) |
| no | no | Keychain (no file, `oauthAccount` present) | no | CLI login (Keychain) | Keychain token, after the grant (Phase B, D10) | "Detected on this device — the `claude` CLI is signed in (Keychain)" + "Check usage" with the system-prompt warning; before the grant, no usage section |
| no | no | Keychain (no file, `oauthAccount` present) | yes | CLI login (Keychain) **not** usable | (none for that source, D11) | Detection + bare-mode warning; usage suppressed; effective credential named (none) |
| no | no | no | — | — | — | Not connected; sign-in flows |

"—" means "don't care" (the row governs whatever the state of that
column). The top-to-bottom order is the precedence: API key >
saved token > CLI login. When both a saved token and a CLI login exist,
workers spend the saved token's share and the card shows the saved
token's usage; the CLI login is named as also present. When both a
credentials file and a Keychain login exist, the file wins (the file
token is used; the Keychain is the fallback when the file is
absent, D10 — the two could in principle belong to different
accounts, and the file account is the one shown).

**Codex — workers use the first present, top to bottom:**

| Env `OPENAI_API_KEY` | `auth.json` `api_key` | `auth.json` `tokens` | Workers use | Usage measured against | Card shows |
|---|---|---|---|---|---|
| yes | — | — | API key | (none — hidden, D12) | "API key" + note when a subscription login is also present |
| no | no | yes | Subscription (OAuth) | app-server rate limits | "Detected on this device — the `codex` CLI is signed in with ChatGPT" |
| no | yes | yes | Subscription (OAuth) | app-server rate limits | "Detected on this device — the `codex` CLI is signed in with ChatGPT" (both present; `tokens` wins) |
| no | yes | no | API key (stored in `auth.json`) | (none — hidden, D12) | "API key" |
| no | no | no | — | — | Not connected; device flow |

Within `auth.json`, `tokens` takes precedence over `api_key`
(matching existing detection order; pinned by test). **Codex has no
bare mode** — only `claude_code.bare` exists in the config. The
dashboard's own device-code flow writes tokens into the same
`~/.codex/auth.json` detection reads, so there is no "saved by Baton"
Codex label (D1).

### 6.3 Usage sources (verified, not invented)

**Codex — local app-server JSON-RPC (lower risk).** Source:
[`rate-limits-protocol.md`](https://github.com/prontsevich/codex-limitbar/blob/HEAD/skills/codex-limitbar/references/rate-limits-protocol.md)
(codex-limitbar; verified against codex-cli 0.147.0, 0.154.0,
0.155.0-alpha.16.4, 0.158.0-alpha.2.1, and probe-observed on
codex-cli 0.167.0 on the Mac, §9.0 item 1). Spawn `codex app-server
--listen stdio://`, binary resolved via `resolve_executable`
(PATH lookup, falling back to the well-known install locations in
`src/jev_agent/core/executables.py::_platform_search_dirs()` — on
darwin: `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`,
`~/.npm-global/bin`, plus `~/.nvm` version bin dirs — the probe's
first attempt failed because the launch shell's PATH lacked
`/opt/homebrew/bin` and the `codex` wrapper died with `env: node: No
such file or directory`; the lookup must cover those dirs, not just
the launch PATH), temp-dir cwd (created by the
adapter, deleted in `finally`),
environment preserved = `HOME`, `PATH`, `TMPDIR`, `LANG`, `LC_ALL`
only (all `*_API_KEY` / `*_TOKEN` variables removed, so a saved key
cannot leak into the child or be picked up by it instead of the file
login — and the `PATH` passed to the child is the launch PATH
augmented with the same `_platform_search_dirs()` set (deduped), so the wrapper's `env
node` resolves even under a thin launch PATH (the probe-proven necessary dir is `/opt/homebrew/bin`, where node lives on Apple Silicon)), stdio pipe/pipe/ignore, 15-second timeout covering the whole
spawn-to-answer, 4 MiB output cap on total buffered stdout (enforced by
incremental read with a running byte count — exceeding it kills the
process, reason `app_server_failed`), SIGTERM
on completion then SIGKILL after a 5-second grace, one app-server alive
at a time (single-flight). JSONL over stdio. Sequence: (1)
`{"method":"initialize","id":1,"params":{"clientInfo":{"name":"jev","title":"Baton","version":"<package version>"}}}` — params carry **only**
`clientInfo`, no `protocolVersion`; the package version comes from
`jev_agent.__version__`, fallback `"unknown"`. An `initialize`
acknowledgement carrying an error result (`{"id":1,"error":{...}}`) is
a handshake failure, reason `app_server_failed`, same as no ack; (2) wait for `{"id":1,"result":{...}}`,
skipping any line with `method` and no matching id (notifications such
as `remoteControl/status/changed`); (3) send
`{"method":"initialized","params":{}}` (notification, no id); (4) send
`{"method":"account/rateLimits/read","id":2}` (no params); (5) read
until the line with `"id":2`. An error object on id 2
(`{"id":2,"error":{...}}`) is reason `app_server_failed` — including
the auth-required failure observed on the VM, whose error names
authentication ("codex account authentication required to read rate
limits"). A non-zero exit with no id-2 answer is likewise
`app_server_failed`. Parse `result.rateLimits` only (ignore the
`rateLimitsByLimitId` duplicate). Response fields: `primary` /
`secondary`, each with `usedPercent` (0–100), `windowDurationMins`,
`resetsAt` (unix seconds); plus `planType` (`plus` / `pro` / `free` / etc., rendered verbatim —
read from `result.rateLimits.planType`; the probe's top-level record
field was null only because the probe script read a top-level key
that does not exist, while the response carries `"free"` inside
`rateLimits`), `rateLimitResetCredits`, `rateLimitReachedType`
(names the exhausted window, null otherwise), `spendControlReached`,
`ordinaryUsageAllowed` (`false` = usage blocked for the account),
`accountId`. Windows are derived from `windowDurationMins` — 300 (=
5 hours) → session, 10080 (= 7 days) → weekly, 43200 (= 30 days) →
monthly — **never from primary/secondary position**. A response may carry fewer windows than the full set (the 5-hour window is suspended for many accounts since ~July 2026, so `secondary` is often null); the card shows the windows the response carries, with copy naming the surviving window(s): "Your plan has no
separate 5-hour limit — weekly only" when only the weekly window is
present; "Your plan has no separate 5-hour or weekly limit —
monthly only" when only the monthly window is present; no such copy
when two or more windows are present (each window gets its own
line). The Weekly line shows the lowest remaining among the weekly
windows; when no weekly window exists there is no Weekly line, and
the monthly window is the long line. An unknown duration is
dropped, not fatal: the window is dropped, the known windows are
shown, and the unknown duration is logged once per dashboard
process start — **except when no known window remains, which is
reason `unexpected_response` (§6.5), not an empty snapshot**.
The drop applies to unknown *durations* only: a null `primary` or a
duplicated duration is a malformed response (`unexpected_response`),
not a droppable window.
Trade-off recorded: a dropped window could be the binding one; the
all-unknown rule exists because an empty card is worse than an
unavailable one — it looks like "no limits" instead of saying the
response could not be read. `primary` null and duplicate durations
remain `unexpected_response` (§6.5). On a known-duration window, `usedPercent` is required —
missing or mistyped → `unexpected_response` (§6.1); `resetsAt` is
optional (missing or null → `resets_at` None, "no reset reported"),
while a present-but-mistyped `resetsAt` is `unexpected_response`.
`resetsAt` in the past renders
as "resetting…".
`ordinaryUsageAllowed` false → reason `usage_blocked` (§6.5).
`spendControlReached` true → the snapshot is shown as reported, with
the sub-line "Spend limit reached". A non-null `rateLimitReachedType`
needs no special handling — the bar already shows 0% left.
`accountId` is hashed (SHA-256 hex digest of its UTF-8 bytes) into the
cache record's `account_hash` (D5); credit
ids, descriptions, and `rateLimitUpsell` are ignored. Do not drive the
TUI. **The success shape was observed by the probe on the Mac
(2026-11-05, codex-cli 0.167.0, §9.0 item 1, records in `probe/`):**
primary `windowDurationMins` 43200 (= 30 days), `usedPercent` 48,
`resetsAt` 1795687699 (unix seconds — a milliseconds value would be
~1.8e12), secondary null, `planType` "free" inside `rateLimits`.
The 43200 reading as monthly is consistent with the response's own
rate-limit-reset credit titled "Full reset (Monthly)" — a one-off credit title, not a window field (it expires 2026-11-06, ~19.5 days before the window resets); the deciding evidence is that 43200 minutes is exactly 30 days. Item 1's
shape check passed (`result.rateLimits.primary` and `usedPercent`
present); the gap was in mapping, not shape — the design's table
had no 43200 row, and the old drop rule would have left this
account's card showing no windows at all. Mac verification of the
shape remains a merge gate (§10). The CLI also
writes a `rate_limits` snapshot into `~/.codex/sessions` files —
reading it is free, but its freshness semantics are unverified (it may
update only while the TUI is open); the design does not use it as a
source in this issue, and if a later issue does, it must first verify
whether the CLI updates it on its own cadence.

**Claude — undocumented OAuth endpoint (higher risk).**
`GET https://api.anthropic.com/api/oauth/usage` with Bearer access
token and header `anthropic-beta: oauth-2025-04-20`. Response:
`five_hour` / `seven_day` objects (`utilization` 0–100, `resets_at`
ISO-8601, nullable), plus `seven_day_opus` / `seven_day_sonnet`
splits and a `limits[]` array. Sources: ccusage (PyPI), kamranahmedse/
claude-statusline (DeepWiki), tclaude `usageapi` (Go), ssdnodes
rate-limit-headers explainer — all confirm the endpoint, the header,
and the field names; Baton has not called it. **The flat
`seven_day_*` keys are all null now — the live quota data lives in a
structured `limits` array, which ccusage reads.** The parser therefore
reads the flat keys when present and non-null, and parses `limits[]`
when they are null; the probe (§9.0) records the array's structure
before implementation. When both carry data, `limits[]` wins on
conflict (it carries the live data per ccusage); windows are deduped
by kind. The splits are included as windows when present
and non-null; the card's Weekly line shows the lowest remaining among
the weekly windows, labelled with which one it is when it is a split
("Weekly (Opus) 45% left"). Splits present with no headline windows →
show the split windows. `five_hour` may be absent or null
(verified Ultra accounts have no five-hour limit); `resets_at` may be
null (→ `UsageWindow.resets_at` None, "no reset reported"). Only one
of the two headline windows present → show the one present; neither
headline nor split windows present → `unexpected_response`. The
endpoint rate-limits itself (429 + `Retry-After`); **no cited source
states a success rate** — "roughly one request in three succeeds" is
an unmeasured estimate from community reports, and the re-probe
measures it (§9.0). `Retry-After` parses as seconds or HTTP-date
(`email.utils.parsedate_to_datetime`); absent or unparsable → wait at
least the cadence floor. The adapter compares `expiresAt` to the clock
BEFORE the call: an expired token fails with
`token_expired_refresh_failed` without a request being sent; a 401
after a non-expired token is `unauthorized`. A missing, null, or
unparsable `expiresAt` is treated as non-expired (the call proceeds;
a 401 then means `unauthorized`). Token refresh: none (D8). Timeout 10 s (a network call gets a shorter
leash than the local Codex spawn). `httpx` is the transport, injected
for tests. `rateLimitResetCredits` equivalents and `limits[]` entries
that are not windows are ignored. **Token source (D10, Phase B):**
the access token and `expiresAt` come from the `claudeAiOauth`
object. **Token selection follows the effective credential (D3):**
a file token, when present, is used and the Keychain is never read
— a saved/file token never triggers a Keychain prompt, on any path.
**The seam:** the adapter's `fetch()` takes a pre-resolved token and
`expiresAt`; it never reads the Keychain (or the file) itself. Token
acquisition is the caller's job: the manual "Check usage"/Refresh
handler resolves it — file first, else the macOS Keychain (service
`Claude Code-credentials`) via `read_keychain_item` (a manual click
always re-reads, D10), run synchronously on the request thread (up to
the 120 s bound; the page shows a "waiting for the Keychain prompt"
state) — caches it in process memory only, then calls `fetch()`. The background fetcher
resolves the file token itself on each fetch (cheap, permission-free)
but uses only the already-cached Keychain token, and skips a
`keychain_login` with no cached token (`maybe_fetch` condition, §6.4)
— so a page render can never pop a system prompt. A token is
classified as a setup token by the `sk-ant-oat01-` prefix; any other
`sk-ant-` token is treated as an OAuth token (D9). The Keychain item's
exact key shape is unverified until the re-probe (§9.0), which
records the key names (never Keychain item values); the file and Keychain paths
parse through the same `claudeAiOauth` parser. No HTTP call is made
when no token is obtainable — the outcome is the Keychain reason
(`keychain_denied` / `keychain_unavailable`, §6.5), not a fetch
failure.

### 6.4 Cache and fetching (fetch on view)

* **Cache location and record:** D5 — one file per provider under
  `~/.jev/`, mode 600, atomic write, one snapshot per provider. The
  record holds `schema_version`, `enabled`, `fetched_at`,
  `last_attempt_at`, `last_status`, `last_reason`, `next_retry_at`,
  `source_version`,
  `account_hash` (Codex only), `windows`. No token values, no usage
  history, no `consecutive_failures`, no `fetch_started_at` — there is
  no background scheduler whose state needs persisting. `next_retry_at`
  is the one persisted timing beyond the attempt: it survives a
  dashboard restart, so a restart never retries into a rate limit
  early. A cache write applies a stale-write guard to the snapshot
  payload: it replaces the stored windows and `fetched_at` only when
  the incoming `fetched_at` is at least as recent as the stored one;
  attempt metadata (`last_attempt_at`, `last_reason`,
  `next_retry_at`, `last_status`) always updates — so a stale write
  can never overwrite a fresher snapshot.
* **Why not reuse `ServerStatsCache`:** its entries are typed
  `ServerStats` (mounts, top CPU/memory) keyed by server name under a
  per-repo `state_dir`; usage snapshots are a different type keyed by
  provider under the host-level `~/.jev/`. Generalising its type
  parameter and home would change a working, tested component's
  contract for no benefit. The new cache **reuses the pattern**
  (get-never-blocks, single-flight refresh returning False, JSON
  persistence, last-good on failure, states `never` / `collecting` /
  `ready` / `failed`) but not the class. Atomic write reuses the
  approach of `write_secret` (`src/jev_agent/core/secrets.py`:
  `os.replace` of a temp file, mode 0600).
* **Trigger model (one rule, no contradiction):** the Connections page
  render **may start a background fetch** (fire-and-forget, daemon
  thread) when the cached snapshot is absent or older than the cadence
  floor (D6) **and** the provider is enabled (D3) **and** the kill
  switch is on (`[usage] <provider>`, §6.9). The render **never
  waits** on it and always serves the cached snapshot immediately.
  The 15-second live poll **never triggers** a fetch; it serves cached
  usage only. Manual Refresh starts a fetch subject to the same floor
  (D6). Dashboard start triggers nothing. First fetch timing: no fetch
  happens before the user enables the provider (D3).
* **Single-flight:** one fetch per provider at a time, in process
  (second `refresh()` returns
  `RefreshResult(outcome="already_running")` while one runs — the
  `ServerStatsCache` pattern). **Cross-instance: timestamp check only;
  no file lock in this issue.** A second dashboard process reads the
  shared cache file and starts a fetch only if `last_attempt_at` is
  older than the floor — the floor is measured from the last attempt,
  not the last success, so two processes cannot both fire at a
  just-failed fetch. One file per provider means two processes
  writing different providers never collide; a concurrent write of the
  same provider's file is last-writer-wins on the same shape, guarded
  by the stale-write rule above.
* **Throttling:** the cadence floor (D6) and `Retry-After` (D7) are the
  only throttling mechanisms. No attempt happens earlier than
  max(`last_attempt_at` + floor, `next_retry_at`). No backoff ladder,
  no circuit breaker, no idle rule, no sunset rule as a separate
  mechanism — the sunset rule is folded into §6.9 as the trigger for
  flipping the kill switch. Expected freshness, written plainly: with
  fetch-on-view, a fetch on view succeeds unless the provider
  rate-limits or the fetch fails (network, timeout, auth, unexpected
  shape); on a 429 the card shows the reason and the next-retry time,
  and the last-good value is hidden once stale (D19). At the
  unmeasured one-in-three success estimate, ~3 attempts reach a fresh
  value — ~30 minutes of 15-minute-spaced attempts; the probe (§9.0
  item 3) measures the real rate and sets the expected-freshness note.
  A 429 never produces a wrong number.
* **Staleness:** freshness is per window (D20): the session window is
  fresh while younger than four times the cadence floor (Claude
  60 min, Codex 20 min); the weekly window while younger than 24 h;
  the monthly window while younger than 24 h.
  Staleness is a **read-time age label**, not a stored state. Stored
  states are `never` / `collecting` / `ready` / `failed` — exactly the
  `ServerStatsCache` set.
* **Display derivation (one total function):** the card's display state
  is derived at read time by `derive_display_state` in
  `usage/__init__.py`:

  ```python
  def derive_display_state(
      *,
      record: UsageCacheRecord | None,
      fetch_running: bool,
      now: datetime,
      session_max_age: timedelta,
      weekly_max_age: timedelta,
      monthly_max_age: timedelta,
  ) -> Literal["never", "collecting", "ready", "failed", "stale"]: ...
  ```

  A window is fresh iff `now - record.fetched_at` is within that
  window kind's max-age (session kinds use `session_max_age`; weekly
  kinds use `weekly_max_age`; monthly kinds use `monthly_max_age`);
  the compact line lists only fresh windows. The connections route
  passes the provider's max-ages on every call (Claude: session 60
  min, weekly 24 h, monthly 24 h; Codex: session 20 min, weekly
  24 h, monthly 24 h). The value is shown only while a window is fresh — stale
  wins over collecting/failed for showing the value:
  * no record → `never`.
  * fetch running + no record → `collecting` (no value, "Checking…").
  * fetch running + fresh window(s) → `collecting` (fresh windows
    shown + "Checking…").
  * fetch running + no fresh window → `collecting` (value hidden,
    "Checking…").
  * fresh window(s), no fetch running → `ready`.
  * no fresh window + record exists → `stale` (value hidden,
    "last seen <relative time> — check again before a long run" +
    Refresh).
  * no record + last attempt failed → `failed` ("Usage unavailable —
    <reason>" + next step).
  * A failed refetch never overwrites the snapshot; on a still-fresh
    snapshot it adds the sub-line "last check failed — <reason>".
  * `last_status` is `ok` after a successful fetch, otherwise the last
    attempt's reason code — there is no separate status vocabulary.
  `fetched_at` is always the last **successful** fetch;
  `last_attempt_at` is the last attempt.
* **Lifecycle:** the `UsageFetcher` is held on `DashboardCore`
  (`self.usage_fetcher`, next to `self.codex_flow`,
  `self.claude_flow`, `self.server_stats`), constructed in
  `DashboardCore.__init__`, shut down from `shutdown_jobs` —
  `shutdown_jobs` calls `self.usage_fetcher.shutdown(5.0)`, the
  fetcher's own 5-second bound inside the 30-second lifespan budget —
  after which daemon threads are abandoned. It
  follows the existing flow pattern — no new thread pattern. Errors
  in a fetch thread never raise into the dashboard process — a failed
  fetch records a failed state, nothing more.
* **Live poll:** the poll JSON gains `usage` **nested inside each
  `providers[]` entry**: `{state, windows: [{kind, left_percent,
  resets_at}], fetched_at, reason, plan}`. `left_percent` is computed
  by the route serializer from `used_percent` (100 − used, integer,
  nearest, half up); timestamps are ISO-8601 UTC with a Z suffix;
  `state` uses the display-state vocabulary (`never` / `collecting` /
  `ready` / `failed` / `stale`); `plan` is the Codex `planType`
  verbatim, or null for Claude. `conn.js` updates
  `[data-provider-usage]` blocks per state. The poll never triggers a
  fetch.

### 6.5 Failure taxonomy and the state matrix

Every usage outcome is one of these reason codes. The code is logged
(§6.8); the UI renders the plain-words column. The adapter's `fetch()`
produces only the fetch-failure subset; the non-fetch outcomes
(`not_checked_yet`, `not_applicable_api_key`, `no_login`,
`disabled_by_config`) are produced by the fetcher or the view without
calling an adapter — they are legitimate `UsageUnavailable` reasons,
not fetch outcomes. Wording is per
provider: each row carries Claude wording and Codex wording where they
differ.

| Reason code | Meaning | Claude wording | Codex wording | Next step |
|---|---|---|---|---|
| `not_checked_yet` | Never fetched (not yet enabled) | "Usage not checked yet" | "Usage not checked yet" | "Check usage" button |
| `no_login` | No CLI login found (detection checks the credentials file and `~/.claude.json`'s `oauthAccount` entry; the Keychain item itself is never checked) | "No Claude login found in the credentials file or the Claude config" — Phase A adds: "This phase detects file-stored logins only — a login in the Mac's Keychain is not detected yet." | "No CLI login found in `~/.codex/auth.json`" | "Sign in in the CLI, or use the dashboard sign-in flow." |
| `not_applicable_api_key` | API key in effect — no windows | — (usage section hidden, D12) | — (usage section hidden, D12) | "Billed per token — no subscription windows apply to an API key." |
| `keychain_denied` | Keychain access was not granted (denied, dismissed, or not answered within the 120-second read bound) | "Keychain access wasn't granted" | — (Codex has no Keychain path) | "Click "Check usage" and choose Allow in the system prompt — "Always Allow" avoids being asked again." (If re-probe item 0 finds a Deny is remembered and no prompt re-appears, the card instead directs the user to grant access in Keychain Access.) |
| `keychain_unavailable` | The Keychain could not be reached (no `security` command, headless session, unexpected error), or the Keychain item itself is gone (reader outcome "missing", §6.1) | "The Keychain couldn't be reached" | — (Codex has no Keychain path) | "Sign in again in Claude Code, then click Refresh usage. If that does not work, use the dashboard sign-in flow." |
| `token_expired_refresh_failed` | Access token expired; the CLI must refresh it | "Your sign-in expired" | — (Codex tokens are read by the CLI itself) | "Sign in again in Claude Code, then click Refresh usage. If that does not work, use the dashboard sign-in flow." |
| `unauthorized` | 401 or 403 — token rejected | "Claude rejected this sign-in" | — (Codex auth failures surface as `app_server_failed`) | "Sign in again in Claude Code, then click Refresh usage. If that does not work, use the dashboard sign-in flow." (Claude) / — (Codex) |
| `setup_token_rejected` | `sk-ant-oat01-` setup token not accepted by the endpoint (probe, D9) | "Usage can't be read with a setup token. Use the CLI sign-in instead." | — | "Use the CLI sign-in instead." |
| `rate_limited` | 429 — honour `Retry-After` | "Claude is rate-limiting usage checks right now" | — (Codex fetch is local; no 429) | Shows the next-retry time; a Refresh inside the floor is ignored with a flash naming the wait (the button stays clickable) |
| `network_error` | No network / DNS / connection | "Couldn't reach Anthropic" | — (Codex fetch is local; no network call) | "Refresh usage" (rate-limited) |
| `timeout` | Fetch exceeded its timeout (Claude 10 s, Codex 15 s) | "The usage check timed out" | "The usage check timed out" | "Refresh usage" |
| `unexpected_response` | Shape changed (missing/mistyped fields) | "Usage couldn't be read — the provider changed its response" | "Usage couldn't be read — the provider changed its response" | None — maintainer action; logged once per dashboard process start with a redacted diagnostic (§6.8) |
| `endpoint_changed` | 404 / 410 from the usage endpoint | "The usage endpoint changed" | — | None — maintainer action; flip the kill switch (§6.9) |
| `app_server_failed` | Codex spawn / handshake / `rateLimits/read` failed | — | "Couldn't ask the Codex CLI for usage" | "Refresh usage"; check the CLI is installed |
| `usage_blocked` | Usage blocked for the account | — | "Usage is blocked for this account" | "Check your ChatGPT plan." |
| `disabled_by_config` | Kill switch off (`[usage]`) | "Usage is turned off for this provider" | "Usage is turned off for this provider" | None — enable it in `jev.toml` and restart the dashboard |

State matrix for the card (each row is one rendered state):

* **Detected + ready:** compact line "Session 62% left · resets in
  2 h 14 min · Weekly 81% left · resets in 3 d" + "updated 12 min ago";
  disclosure opens the per-window bars, plan (Codex), and source note
  (source path + relative fetched time + provider, per D14).
* **Detected + collecting:** compact line hidden; card shows
  "Checking…" and the last fetched value, if any window is still
  fresh, with its age.
* **Detected + stale:** value hidden; "last seen 2 h ago — check again
  before a long run" + "Refresh usage" button (D19).
* **Detected + failed:** "Usage unavailable — <plain-words reason>" +
  next step from the table; last successful value, if any window is
  still fresh, shown as "last seen <relative time>".
* **Detected + never:** "Usage not checked yet" + "Check usage" button.
* **API key in effect:** usage section hidden; one-line explanation
  (D12); when a subscription login is also present, one line names it
  ("a subscription login is also present — the API key is in use").
* **Bare mode + CLI login:** detection + warning; no usage bars (D11);
  the effective credential is named.
* **Not connected:** no usage section; existing sign-in flows.
* **CLI missing:** no usage section; install command (D15).
* **Keychain login, awaiting grant (Phase B, D10):** card names the
  Keychain source ("Detected on this device — the `claude` CLI is
  signed in (Keychain)") and the "Check usage" button carries the
  system-prompt warning; no usage section until the grant, and no
  usage fetch is attempted. After a denied grant: "Usage unavailable
  — Keychain access wasn't granted" + the Allow next step (§6.5).
  After an unreachable Keychain: the `keychain_unavailable` wording
  + next step (§6.5). After a dashboard restart the cached token is
  gone: the card shows the persisted snapshot as stale ("last seen
  … ago" + Refresh) with the "Check usage" button — no prompt, no
  error — and with no snapshot at all it shows this awaiting-grant
  row.
* **Kill-switched:** "Usage is turned off for this provider" (reason
  `disabled_by_config`); login state unaffected; the fetcher does not
  attempt fetches for that provider.

Usage failure does not change the login state shown on the card, and
does not break workers or the rest of the page. Each provider's fetch
fails independently — Claude being slow or down must not delay or blank
the Codex card. The live poll's JSON shape is extended additively (a
`usage` key inside each `providers[]` entry); existing fields and tests
do not change.

### 6.6 UI: where it lives and how it looks

* **Where:** Connections → Providers section, inside each provider's
  card. The existing card (badge, label, detail, hint, sign-in flows)
  gains a usage block below the label line. The consent note (D16)
  sits under the detected-login card. No new page.
* **Layout:** progressive disclosure. One compact line per provider
  (D14). Details (per-window bars with left percent and reset time,
  plan, source note, "updated … ago") behind a `<details>`
  disclosure — the page already renders several providers, and two full
  bar blocks per card would double the section height.
* **Bars:** left direction, one bar per window, labelled "Session",
  "Weekly", and "Monthly" (plus "Weekly (Opus)" / "Weekly (Sonnet)"
  when a split is the binding window). The bar is a plain `<div>`
  progress bar in the existing dashboard style (spacing tokens; no
  literal spacing declarations — the spacing CI guard applies). The
  bar fills to **remaining**. No new CSS file.
* **Buttons:** one button per provider. Until the first successful
  fetch it reads **"Check usage"**; after that **"Refresh usage"**.
  While a fetch runs it reads **"Checking…"**. The button is ignored
  inside the cadence floor (D6) — the flash names the wait. The button
  is a `<form method="post">` to the refresh route with `csrf_input()`
  (the existing pattern in `connections.html`); `conn.js` holds no
  button code and never posts. A second control on each detected-login
  card, "Turn off usage checks", posts to the disable route (D3
  off-ramp). For a Keychain login (Phase B, D10) the "Check usage"
  click is also the grant action: it reads the token from the
  Keychain (a system prompt may appear on the Mac — the card warns
  this before the click), caches it in memory, and starts the fetch;
  no Keychain read happens on render, poll, or background fetch.
* **Live updates:** the 15-second poll carries cached usage
  (`fetched_at` + windows + state + reason); `conn.js` updates the
  `[data-provider-usage]` block in place without a reload. The poll
  never triggers a fetch. The block's inner contract, shared by the
  template and `conn.js`: `[data-usage-line]` (the compact line),
  one `[data-window-bar]` per window with `[data-window-label]`,
  `[data-window-fill]` (width %), and `[data-window-reset]`,
  `[data-usage-updated]` ("updated … ago"), and `[data-usage-button]`
  (the form submit button, whose label the poll updates between
  "Check usage" / "Refresh usage" / "Checking…").
* **Live updates:** the 15-second poll carries cached usage
  (`fetched_at` + windows + state + reason); `conn.js` updates the
  `[data-provider-usage]` block in place without a reload. The poll
  never triggers a fetch.
* **Documented exceptions to zero-terminal:** CLI missing (install
  command, D15); Keychain login first use (a system prompt appears
  on the Mac — choose "Always Allow" so it does not reappear, D10);
  signing in again inside the CLI when the token expired (the
  dashboard sign-in flow is the alternative). Everything else —
  viewing usage, refreshing, seeing states — needs no terminal.

### 6.7 Security and privacy

* **What Baton reads:** the CLI's own credential files, read-only,
  for presence and token *values* used only to call the provider's own
  endpoint (Claude) or not at all (Codex, which reads its own file).
  File reads go through the one shared reader in `provider_auth.py`
  (§6.1).
* **What Baton never does:** never displays, logs, or copies a token or
  key value (existing rule, unchanged); never writes to
  `~/.claude/.credentials.json` or `~/.codex/auth.json` (D8); never
  refreshes a token (D8); never stores a token in the usage cache
  (numbers, timestamps, reason codes, and a hash only); never sends a
  token to any endpoint other than the provider's own (Claude:
  `api.anthropic.com`); never writes to the macOS Keychain — the
  Keychain read (Phase B, D10) is read-only, the token it returns
  lives in process memory only (never on disk, never in a log), and
  the `security` subprocess runs with a scrubbed environment (the
  same scrub as the Codex spawn: no `*_API_KEY` / `*_TOKEN`
  variables leak in).
* **How the UI proves the login is the device's own:** the card names
  the source path and which credential is in effect (§6.2). The user
  can verify it by opening the CLI in a terminal and seeing the same
  account; the dashboard never shows the account identifier itself.
  The Codex cache record carries `account_hash` (SHA-256 hex digest of
  the UTF-8 bytes of `accountId`), used only to tell whether the login
  changed; it contains no token value. On a change the record is
  treated as empty and the card shows not-checked-yet until the next
  fetch. For Claude
  there is no account-identifying input, so the record carries no
  fingerprint — the limitation is stated in D5 and bounded by
  fetch-on-view (the next view after the floor refetches, and the
  snapshot always shows its `fetched_at`).
* **Codex spawn discipline:** temp-dir cwd (created by the adapter,
  deleted in `finally`), preserved environment `HOME`, `PATH`,
  `TMPDIR`, `LANG`, `LC_ALL` only, 15 s timeout, SIGTERM then SIGKILL
  after 5 s, 4 MiB output cap, one app-server at a time. A leaked
  process is a resource leak on the dashboard host; the fetcher owns
  the lifecycle (§6.4).
* **Cache files:** `~/.jev/usage-cache-<provider>.json`, mode 600,
  atomic write. They hold numbers, timestamps, reason codes, and (for
  Codex) a hash of the account id — never a token value.
* **Assumption stated:** the dashboard and the CLI run as the same
  user. On a host where they do not (or under Docker), the credential
  reads fail with permission errors and the card shows "Not connected".
* **Clock:** reset times are the provider's `resets_at` minus the host
  clock. A skewed host clock skews the display; no correction is
  attempted.

### 6.8 Observability

* Structured log line per fetch attempt: provider, source, reason code,
  status, duration, `Retry-After` when present, attempt number. Never
  token values, account identifiers, or verbatim exception text. Cache
  holds numbers and timestamps only.
* `unexpected_response` diagnostic (redacted): which fields were
  present, which expected fields were missing or mistyped, and the
  response's top-level keys only — no values. Logged once per dashboard
  process start.
* `last_status` / `last_reason` / `fetched_at` are visible in the UI
  (§6.5), so a user can report them without a log dive.
* Per-provider health is visible in the live poll JSON (`usage.state`),
  so the dashboard never serves a stale number as fresh.
* No new metrics endpoint. No usage history is stored (non-goal).

### 6.9 Rollout, rollback, and kill switches

* Ships in two PRs (D4): Phase A (Codex + shared plumbing), Phase B
  (Claude adapter). Each has its own merge gate (§10).
* Kill switches: config section `[usage]` in `jev.toml`:
  `claude_code = true` / `codex = true` (defaults true). Schema:
  `UsageConfig` model in `src/jev_agent/config/schema.py`
  (`claude_code: bool = True`, `codex: bool = True`),
  `JevConfig.usage: UsageConfig`, registered in `TOP_SECTIONS`
  (`src/jev_agent/app/settings.py`), commented template lines in
  `DEFAULT_CONFIG_TOML` (`src/jev_agent/config/defaults.py`).
  `JevConfig` is `_Strict` (`extra="forbid"`), so `[usage]` fails
  validation until the schema change lands. **Flipping a switch needs
  a dashboard restart** (config is read at start). "No code change, no
  redeploy" holds; "no restart" is not claimed. Turning a source off
  renders it as `UsageUnavailable` with reason `disabled_by_config`
  and **stops fetch attempts** for that provider — the login state is
  unaffected. Re-enabling resets nothing persisted (nothing about the
  stop is persisted — D7).
* Sunset rule, folded into the kill switch: if the Claude endpoint
  returns `endpoint_changed` (404/410) or `unexpected_response`
  repeatedly, the maintainer's action is to flip the kill switch, file
  a tracking issue, and re-verify against community sources. The
  adapter is deleted in one place when the source is dropped (§6.1).
* Rollback is the kill switch plus a normal revert; the cache files are
  disposable (deleting one just re-fetches on the next view).
* Version drift: the CLI version is recorded with the detected-login
  state (`ProviderAuth.cli_version`) and the source version with each
  cache entry, so a future breakage is diagnosable. No version range
  is enforced in this issue (`unsupported_version` is dropped from the
  taxonomy) — versions are recorded, not gated. A known-good range is
  recorded in §10 (Codex 0.164.0-alpha.9 verified on the VM; Claude
  CLI version recorded at Mac verification).

## 7. Alternatives considered

* **Record "imported" in state.db.** Rejected: a recorded login claim
  goes stale when the user signs out in a terminal; live derivation
  cannot go stale. The cache record carries provenance instead.
* **Opt-in usage.** Adopted in part (D3): per-provider opt-in via the
  "Check usage" button. A dashboard Settings toggle is a follow-up.
* **Always-on usage with no opt-in.** Rejected (D3): with no
  background use to opt out of, always-on would fetch on the first
  page view before the user asked for anything.
* **Background refresher (fetch on a timer).** Evaluated and rejected
  (D6): the numbers are only needed when the user looks at them, so a
  timer buys one fetch of latency and costs a scheduler, an idle rule,
  backoff, a circuit breaker, cross-instance coordination, and
  persisted retry state. Fetch-on-view serves the same need with a
  cadence floor, `Retry-After`, and the failure taxonomy. If real use
  shows view-time fetching is too slow, a background job can be added
  later — there is no evidence in this design that it is.
* **Usage on the render path / live poll.** Rejected: the poll fires
  every 15 s; riding it would send 480 Claude requests/hour with two
  tabs open (15 s × 2 tabs × 3600 s) against a bucket the CLI shares.
  Background-only fetch was also rejected (see above); fetch-on-view
  with a background thread the render never waits on is the adopted
  shape.
* **Per-repo cache under the repo state dir.** Rejected: one fetcher
  per open repo against the same per-token bucket. Host-level cache
  (D5).
* **Reuse `ServerStatsCache` as-is for usage snapshots.** Rejected
  with a stated reason (§6.4): typed `ServerStats` entries keyed by
  server name under a per-repo `state_dir` cannot hold usage snapshots
  keyed by provider under `~/.jev/`. The pattern is reused, not the
  class.
* **Baton refreshes and writes the token back to the CLI file.**
  Rejected (D8): two writers to one credentials file can corrupt the
  user's login — and refresh-token rotation means Baton consuming the
  refresh token can kill the CLI's stored copy. No refresh at all.
* **Setup token tried once per token at runtime.** Rejected (D9): no
  mechanism exists to track "once per token", and it builds permanent
  logic for an unverified guess. The probe settles acceptance as a
  general fact.
* **Show "left" computed from local session files when the server
  source is down.** Rejected (D17): consumption files cannot produce
  what is left; a computed number labelled as left is a number the
  source did not report.
* **Drive the CLI's `/usage` / `/status` screen with a PTY.** Rejected:
  it needs a real terminal, the interaction is timing-fragile, and the
  CLI's own polling would compete with Baton for the same rate-limit
  bucket. The app-server protocol (Codex) and the endpoint (Claude)
  are the non-interactive paths the providers' own tools use.
* **Scrape the provider web dashboard.** Rejected: no auth, no stable
  surface, and it would need the user's web session rather than the
  CLI's own sign-in.
* **Call OpenAI's `/backend-api/codex/usage` endpoint for Codex
  usage.** Considered and not adopted: it needs a ChatGPT backend
  session token, a different credential surface from the CLI login
  this issue is about; the local app-server protocol answers the same
  question with the CLI's own sign-in.
* **Show the `limits[]` array and opus/sonnet splits (Claude).**
  Reversed during review: the splits are windows and the lowest
  remaining among them can be the binding limit (§6.3, D13); the
  `limits[]` array is parsed because the flat keys are now null in
  live responses.
* **In-dashboard CLI install.** Rejected (D15): a package install is a
  separate feature; the card keeps the install command.

## 8. Non-functional requirements

* **Performance:** render and live poll never block on network or
  subprocess (§6.4). Claude fetch timeout 10 s; Codex spawn 15 s + 5 s
  SIGKILL grace. Fetches run on daemon threads owned by the
  `UsageFetcher`; dashboard shutdown joins them with a 5-second bound.
* **Scale:** the fetch budget is per provider per host, not per repo
  and not per tab (D5, D6). Two open tabs do not double the request
  rate — the second tab reads the same cache file and sees the same
  `fetched_at`.
* **Reliability:** usage failure does not change the login state shown
  on the card, and does not break workers or the page (goal). Each
  provider fails independently. A failed fetch never overwrites a good
  cache entry (errors keep last-good).
* **Security:** §6.7. No new credential storage. No token in logs,
  cache, or UI (test-enforced).
* **Testability:** every adapter takes its transport by injection
  (HTTP client, process spawner, clock, cache, home directory) — no
  direct `httpx.get`, `subprocess.Popen`, `time.time()`, or file read
  at a call site. Tests use fakes: no network, no real CLI, no
  wall-clock sleeps. The fake-clock seam is the `clock` constructor
  argument (`Callable[[], datetime]`, default `_utcnow`) on the cache
  and the fetcher; tests pass a fake clock (a callable returning a
  settable datetime). The home seam is the `home` constructor argument
  (default `Path.home()`); tests pass a temp directory.
* **Follow-ups (named, not built):** dashboard on/off toggle for usage in Settings;
  bare-mode workers reading the CLI login; `~/.codex/sessions`
  snapshot as a Codex source (verify freshness first); `jev workflow`-
  style CLI for usage; usage history/trends. (macOS Keychain reading
  was a follow-up before the probe; it is a Phase B prerequisite
  now, D10 — not a follow-up.)

## 9. Implementation plan

### 9.0 Probe (pre-implementation gate — runs first, on the Mac)

A throwaway script (not a subsystem), written by the orchestrator at
probe time (Python 3 stdlib + `httpx` if available, else `curl` for
Claude and the `codex` binary for Codex), run by the orchestrator on
the Mac where real logins live. Records go to
`docs/designs/cli-autoimport-usage/probe/` (raw responses with token
values stripped, plus a `PROBE.md` summary). Redaction procedure:
strip token values (`accessToken`, `refreshToken`, `api_key`,
`tokens.*`, setup tokens) from every raw response before writing —
field names, types, and redacted values only; `accountId` is recorded
as its SHA-256 hex digest, never raw. `PROBE.md` carries one section
per checklist item: outcome (pass/fail), the measured numbers, the
`limits[]` field-name/type table, CLI versions, the cadence decision,
and the go/no-go per phase. **Implementation does not start until the
probe records exist AND every item passes or has a recorded
failure-branch outcome. A failed item blocks its phase; it does not
block the other phase.**

**Outcome — first run, 2026-11-05 on the Mac (records in `probe/`,
summary `probe/PROBE.md`).** Item 1 PASSED (shape confirmed:
`result.rateLimits.primary` and `usedPercent` present — the pass
condition is a shape check, and it passed; the gap it exposed was in
*mapping*, not shape: primary `windowDurationMins` 43200 has no row
in the design's table, and the old drop rule would have left the
card showing no windows). Items 2/3/5/6 could not run: no
file-stored Claude token exists on the Mac (Keychain instead) —
blocked, not failed; the design returns for resolve on token
acquisition (D10, §11). Item 4 unresolved (no distinct setup token
on the device) — Phase B defaults to rejected
(`setup_token_rejected`, never tried), per the item-4 failure
branch below. Item 7 passed (claude 2.1.318, codex-cli 0.167.0).
**Phase A: CONDITIONAL GO** — item 1 passed; the 43200 resolve
(§11) unblocks it. **Phase B: NO-GO** until the re-probe below
passes or records failure-branch outcomes.

**Re-probe for Phase B (runs on the Phase B branch, before it
merges).** The Claude probe items depend on the implementation — the
Keychain reader (D10) *is* the token path — so they cannot run
before Phase B exists. The re-probe runs the Phase B branch's own
reader to obtain the token (it does not hand-roll Keychain access),
then re-runs items 2, 3, 5, and 6 below against that token, plus one
new item: (0) Keychain item structure — record the stored item's key
names (never Keychain item values) and confirm the `claudeAiOauth`
shape the adapter parses; the stored password is either (a) the whole
stored JSON object containing `claudeAiOauth`, or (b) the inner
`claudeAiOauth` object itself — item 0 records which; if the shape
is neither, record it and the design returns for resolve before
Phase B merges. Item 0 also records, for each reader case (allow,
deny, dismiss/timeout, item-not-found, no `security` binary,
malformed stdout), the exit code and stderr of the real `security`
CLI (the mapping table in §6.1 is by behaviour; item 0 fills in the
numbers); the prompt's actual wording and which binary it names; and
whether a second invocation after a Deny re-prompts (if it does not,
the `keychain_denied` card directs the user to grant access in
Keychain Access, §6.5). The re-probe needs a human present at the
Mac: on the first Keychain prompt choose "Always Allow", so items
2/3/5/6 run without re-prompting (otherwise item 3's five attempts
would each need a prompt answer). The harness is a throwaway script
on the Phase B branch that imports `read_keychain_item` and the
Claude adapter's fetch from the branch (no hand-rolled Keychain
access), run on the Mac. Item 4 is not re-probed (no distinct setup
token exists to test); the rejected default stands. Phase B merges
only when every item has a recorded outcome — a pass, or a
failure-branch outcome with the corresponding `UsageUnavailable`
reason wired (§9.0 failure branches). An item that fails in a way
with no failure branch (an unhandled shape) returns the design for
resolve and blocks the merge.

Checklist (first run — kept as the spec of what was run):

1. Codex: run `codex app-server --listen stdio://` with a real login;
   send `initialize` (id 1, `clientInfo` only, `title: "Baton"`),
   `initialized`, then `account/rateLimits/read` (id 2); record the
   actual success response in full (redacted), explicitly recording
   the `windowDurationMins` values and the `resetsAt` units (unix
   seconds) — the two fields the parser derives windows from. Pass:
   `result.rateLimits.primary` and `usedPercent` present. Failure
   branch: if the success shape differs from the parser in §6.3 (no
   `rateLimits`, different field names), the probe FAILS for Codex —
   implementation does not start and the design returns for a resolve
   with the actual shape recorded. If the call fails with
   auth-required (no login on the Mac), the probe cannot run — record
   that and re-run when a login exists; that is a scheduling block,
   not a design failure.
2. Claude: call `GET https://api.anthropic.com/api/oauth/usage` with
   a real access token and the `anthropic-beta: oauth-2025-04-20`
   header; record the actual response in full (redacted), including
   the `limits[]` structure (field names and types — the parser in
   §9.2 depends on them), whether the flat `five_hour` /
   `seven_day` keys are null, and whether the Opus/Sonnet splits are
   present. Pass: a 200 with at least one window carrying
   `utilization` and `resets_at` (flat or in `limits[]`), AND the
   `limits[]` field names and types recorded. Failure branch: 404/410
   → `endpoint_changed` — Phase B is dropped from this issue (the
   design returns for a resolve on whether to drop or redesign the
   Claude half). Any other non-200 → retry once; still failing → the
   probe FAILS for Claude.
3. Claude success rate: one attempt every 15 minutes for one hour, 5
   attempts total, during normal Claude Code use; record how many
   attempts returned 200 vs 429. Decision rule: 0 successes in 5 →
   probe FAILS for Claude (the endpoint does not answer under normal
   use; Phase B does not start, design returns for a resolve). ≥ 1
   success → pass; the measured rate is recorded and sets the
   expected-freshness note (expected time to a fresh value ≈ 15 min ÷
   rate; at 1-in-3 ≈ 45 min). The 15-minute floor stands regardless —
   the floor bounds the attempt rate, not the success rate.
4. Setup token: call the endpoint with an `sk-ant-oat01-` setup token
   once and record accepted or rejected (D9). Pass: accepted or
   rejected, either way. Ambiguous (network failure) → re-run once;
   still ambiguous → record "unresolved" and Phase B defaults to
   rejected (`setup_token_rejected`, never tried).
5. Account identity: record whether the Claude response carries any
   account-identifying field (for the D5 `account_hash` question).
   Pass: the question answered either way — record the field name
   (not the value) if one exists, else record "none"; that settles
   D5 (no Claude fingerprint).
6. Splits: record whether the Opus/Sonnet splits are present and
   whether either ever binds below the headline weekly window (D13).
   Pass: recorded either way; the outcome decides the card's Weekly
   line (D13) and becomes the Phase B test fixture.
7. Record both CLI versions (`claude --version`, `codex --version`).
   Pass: both recorded, or "not installed" recorded for a missing CLI.

### 9.1 Phase A — Codex PR (shared plumbing + Codex adapter)

Checkpoint: Phase A merges on its own; the Codex Mac gate (§10) applies
to it alone.

**New files**

* `src/jev_agent/core/usage/__init__.py` — `ProviderUsageSource`
  protocol (`fetch() -> UsageSnapshot | UsageUnavailable`, no
  arguments — adapters take what they need in their constructors),
  dataclasses `UsageWindow(kind, used_percent, resets_at:
  datetime | None)`,
  `UsageSnapshot(provider, windows, plan, fetched_at, source_version)`,
  `UsageUnavailable(provider, reason, fetched_at, last_good)`,
  `RefreshResult(outcome, wait_seconds)` with `outcome` one of
  `"started" | "already_running" | "too_soon" | "disabled" |
  "not_enabled" | "auth_stopped" | "no_login"`, `derive_display_state`
  (§6.4), `WORDING` mapping type, reason-code constants, and the
  `_utcnow()` clock function (`datetime.now(UTC)`, the default for
  the `clock` constructor argument). `LoginSource` is imported from
  `provider_auth`, not redefined. `source_version` format:
  `"cli/<cli_version> parser/<PARSER_VERSION>"`, e.g.
  `"cli/0.164.0-alpha.9 parser/1"`; `"unknown"` when the CLI version
  is unavailable. Window kinds: `"session" | "weekly" | "monthly" |
  "weekly_opus" | "weekly_sonnet"`.
* `src/jev_agent/core/usage/codex.py` — Codex adapter, constructor
  `CodexUsageSource(*, popen: PopenFn = subprocess.Popen, which:
  Callable[[str], str | None] = resolve_executable, clock:
  Callable[[], datetime] = _utcnow)`, where `PopenFn =
  Callable[..., Any]` (the same shape as the existing `popen`
  injection in the flows) and the call is `popen(["codex",
  "app-server", "--listen", "stdio://"], stdin=subprocess.PIPE,
  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
  cwd=str(tmpdir), env=scrubbed_env)`. `resolve_executable` covers
  `_platform_search_dirs()` (`src/jev_agent/core/executables.py`),
  not just the launch PATH (§6.3 probe note), and the `PATH` in
  `scrubbed_env` is the launch PATH augmented with those dirs
  (deduped, `/opt/homebrew/bin` included). Spawn with temp-dir cwd
  (created by the adapter, deleted in `finally`), preserved env
  `HOME`, `PATH`, `TMPDIR`, `LANG`, `LC_ALL` only, 15 s timeout over
  the whole spawn-to-answer, 4 MiB cap on total buffered stdout
  (incremental read with a running byte count; exceeding it kills the
  process, reason `app_server_failed`), SIGTERM on completion then
  SIGKILL after a 5 s grace; send `initialize` (id 1, params
  `{"clientInfo":{"name":"jev","title":"Baton","version":"<package
  version>"}}` only, no `protocolVersion`; package version from
  `jev_agent.__version__`, fallback `"unknown"`), wait for
  `{"id":1, "result":{...}}` skipping lines with `method` and no
  matching id — an error result on id 1 is a handshake failure,
  reason `app_server_failed` — send
  `{"method":"initialized","params":{}}`, send
  `{"method":"account/rateLimits/read","id":2}` (no params), read until
  the line with `"id":2` — an error object on id 2 is reason
  `app_server_failed`, including the auth-required failure; parse
  `result.rateLimits` only; derive windows from `windowDurationMins`
  (300 → session, 10080 → weekly, 43200 → monthly; an unknown
  duration drops that window — known windows shown, duration logged
  once per dashboard process start; **all windows unknown → reason
  `unexpected_response`, never an empty snapshot**; `primary` null /
  duplicate durations → reason `unexpected_response`); `planType`
  rendered verbatim (read from `result.rateLimits.planType`); `accountId`
  hashed (SHA-256 hex digest of its UTF-8 bytes) for the cache
  record; `ordinaryUsageAllowed` false → reason `usage_blocked`;
  `spendControlReached` true → snapshot shown as reported with the
  sub-line "Spend limit reached"; non-null `rateLimitReachedType` →
  no special handling. Exposes `WORDING: dict[str, tuple[str, str]]`
  (§6.1; §6.5 table is its specification).
* `src/jev_agent/core/usage/cache.py` — host-level cache record
  (`~/.jev/usage-cache-<provider>.json`, mode 600, atomic write via
  temp file (created mode 600 before rename) + `os.replace`, reusing
  the approach of `write_secret` but not its refusal behaviour — a
  cache write never refuses, it proceeds and the resulting file is
  chmod 0600), one record per provider. `UsageCacheRecord` dataclass
  with fields `schema_version` (1), `enabled`, `fetched_at`,
  `last_attempt_at`, `last_status`, `last_reason`, `next_retry_at`
  (ISO-8601 UTC, null when none), `source_version`, `account_hash`
  (Codex only), `windows` (serialised as `[{kind, used_percent,
  resets_at}]` with `resets_at` ISO-8601 UTC with Z suffix, null when
  absent; `fetched_at` / `last_attempt_at` / `next_retry_at`
  likewise ISO-8601 UTC with Z suffix). Methods: `read(provider) ->
  UsageCacheRecord | None`, `write(provider, record)` (stale-write
  guard, §6.4), `set_enabled(provider, enabled: bool)` — the opt-in
  write path (creates the record with `enabled: true` when none
  exists, preserving existing windows/`fetched_at` otherwise).
  Storage only — no single-flight (that lives in the fetcher).
  Fingerprint change (`account_hash`) treats the record as empty;
  the card then shows not-checked-yet until the next fetch. Snapshot
  freshness is a read-time label (per-window max-age, §6.4/D20);
  corrupt file treated as empty and logged once, card shows
  "Check usage"; `~/.jev/` created if missing (mode 700).
  Constructor `UsageCache(*, home: Path, clock: Callable[[], datetime]
  = _utcnow)` — home supplied as a parameter (fake home in tests).
* `src/jev_agent/core/usage/fetcher.py` — `UsageFetcher`, the one
  background-fetch owner shared by both providers. Constructor:

  ```python
  UsageFetcher(
      *,
      home: Path,
      env: Mapping[str, str] | None = None,   # defaults to os.environ; tests pass a fake mapping
      adapters: Mapping[str, ProviderUsageSource],
      cache: UsageCache | None = None,        # defaults to UsageCache(home=home, clock=clock)
      clock: Callable[[], datetime] = _utcnow,
  )
  ```

  Config is never stored on the fetcher: every fetch method takes the
  repo config as a parameter (the route handler loads it via
  `open_repo` as today and passes it), and the kill switch is read
  from `config.usage.<provider>` on every call. Methods:
  `maybe_fetch(provider: str, config: JevConfig) -> bool` (the render
  trigger: starts a fetch only when enabled + kill switch on + a
  usage-measurable login is present + floor passed + not auth-stopped
  + not already running + a token is obtainable without a Keychain
  read (file token, saved token, or cached Keychain token — a
  `keychain_login` with no cached token is skipped, never prompted,
  D10)); `refresh(provider: str, config: JevConfig,
  *, manual: bool = False) -> RefreshResult` (the route entry point;
  `manual=True` overrides the auth stop but not the floor); `is_running(provider: str) -> bool`;
  `shutdown(timeout: float = 5.0) -> None` (joins threads with the
  given bound). Single-flight lives here (a second `refresh()` while
  one runs returns `RefreshResult(outcome="already_running")`). The
  floor is measured from `last_attempt_at`; no attempt happens earlier
  than max(`last_attempt_at` + floor, `next_retry_at`) — `next_retry_at`
  is read from and written to the cache record, so it survives a
  restart. The 401/403 stop lives in process memory only:
  `_auth_401_count: dict[str, int]` (consecutive 401/403 results) and
  `_auth_stopped: dict[str, _AuthStop]` where `_AuthStop` is a frozen
  dataclass `(login: LoginSource, account_hash: str | None)` — the
  login in effect when the stop was set. First 401/403 → count 1,
  one retry allowed on the next trigger after the floor; a second
  consecutive 401/403 → stopped (count reset). Every
  `maybe_fetch`/`refresh` call re-arms by comparing the current
  `resolve_credentials(...).effective` (kind + source_path) and, for
  Codex, the cache record's `account_hash` against the stored stop —
  a difference clears the stop and the count. Manual Refresh clears
  the stop; a dashboard restart clears it (nothing persisted).
  A provider with no registered adapter is never fetched — the render
  loops over registered adapters only, and the route 404s for an
  unregistered provider (in Phase A only `codex` is registered, so
  `claude_code` → 404 until Phase B). Held on
  `DashboardCore` (`self.usage_fetcher`).
* `tests/unit/test_usage_codex.py` — with a fake process spawner.
  `tests/unit/test_usage_cache.py` — with a fake clock (the `clock`
  constructor argument) and temp directory.
  `tests/unit/test_usage_fetcher.py` — with a fake clock and fake
  adapters.

**Modified files**

* `src/jev_agent/core/provider_auth.py` — distinct labels for saved
  OAuth token vs CLI login (D1 wording verbatim: "Signed in with your
  Claude subscription (saved by Baton)" / "Detected on this device");
  shared credential-source accessor `get_login_source(provider: str,
  *, env: Mapping[str, str] | None = None, home: Path | None = None)
  -> LoginSource` (provider `"claude_code"` | `"codex"`; defaults
  `os.environ` / `Path.home()`; tests pass fakes); the precedence
  resolution function `resolve_credentials(provider, *, env, home,
  bare=False) -> CredentialResolution` (§6.1; §6.2 tables as its test
  list — one case per row: 7 non-Keychain Claude rows, 5 Codex including the
  both-present Codex row; the 2 Keychain Claude rows are Phase B, §9.2); the shared credential-file reader
  `read_credential_file(path: Path) -> dict | None` (§6.1), used by
  both detection (presence) and the adapter (values); additive
  `ProviderAuth` fields with defaults (`cli_version: str |
  None = None`, `usage: UsageSnapshot | UsageUnavailable | None =
  None`, `present: tuple[str, ...] = ()`, `source_path: str | None =
  None`).
* `src/jev_agent/config/schema.py` + `src/jev_agent/config/defaults.py`
  — `[usage]` section: model `UsageConfig` with `claude_code: bool =
  True`, `codex: bool = True`; `JevConfig.usage: UsageConfig`;
  commented template lines in `DEFAULT_CONFIG_TOML`:

  ```toml
  # [usage]
  # claude_code = true
  # codex = true
  ```

  (surfaced in `jev.toml`, defaults true; documented in
  `docs/config.md`).
* `src/jev_agent/app/settings.py` — register `[usage]` in
  `TOP_SECTIONS` as `("usage", "Usage")` (the label is user-visible
  on the Settings page; unclaimed sections land there automatically).
* `src/jev_agent/dashboard/core.py` — construct `self.usage_fetcher`
  in `DashboardCore.__init__` (`UsageFetcher(home=Path.home(),
  adapters={"codex": CodexUsageSource()})` — only the Codex adapter
  is registered in Phase A; the Claude adapter joins the mapping in
  Phase B); call `self.usage_fetcher.shutdown(5.0)` from
  `shutdown_jobs`.
* `src/jev_agent/dashboard/app.py` — register the refresh and disable
  routes in the route table (`app.py:257–299`, next to the other
  `/connections/*` routes).
* `src/jev_agent/dashboard/views.py` — `cli_providers(config,
  usage_cache: UsageCache | None = None)` gains the cached usage
  snapshot (cache read only, never a fetch) and the detection metadata
  (`source_path`, `present`) on the `ProviderAuth` fields above. The
  default cache is `UsageCache(home=Path.home())`; tests pass a cache
  over a temp home. The read runs on every poll (cheap file read, no
  fetch).
* `src/jev_agent/dashboard/routes_connections.py` — connections page
  and `connections_live` include the cached usage snapshot (additive
  `usage` key nested inside each `providers[]` entry:
  `{state, windows: [{kind, left_percent, resets_at}], fetched_at,
  reason, plan}`; `left_percent` computed by the route serializer
  from `used_percent` as 100 − used, integer, nearest, half up;
  timestamps ISO-8601 UTC with a Z suffix; `state` uses the
  display-state vocabulary `never` / `collecting` / `ready` /
  `failed` / `stale`); new `POST
  /connections/providers/{provider}/usage/refresh` (provider ∈
  {`claude_code`, `codex`}; response via the dashboard's `act()`
  redirect + flash, redirect target `/connections`; first click sets
  `enabled: true` via `cache.set_enabled` AND starts a fetch
  immediately, then redirects with flash "Checking usage — the
  numbers will appear when the check finishes."; no success flash
  after that; rate-limited (inside the floor or before
  `next_retry_at`) → redirect + flash "Already checked recently —
  try again in <relative>." where the wait is max(floor remainder,
  `next_retry_at` − now), rendered relative per D14; unknown provider
  → 404 (in Phase A `claude_code` is unregistered, so it 404s until
  Phase B); kill-switched provider → redirect + flash "Usage is turned
  off for this provider."; CSRF as for other dashboard POSTs); new
  `POST /connections/providers/{provider}/usage/disable` → sets
  `enabled: false` via `cache.set_enabled`, redirects to
  `/connections` with flash "Usage checks turned off for <provider>."
  (D3 off-ramp); the connections page render may start a background
  fetch via `fetcher.maybe_fetch(provider, config)` (D6 trigger
  model, kill-switch exception included).
* `src/jev_agent/dashboard/static/conn.js` — live poll updates the
  `[data-provider-usage]` block in place from the cached `usage` key,
  per state (§6.5 matrix), updating the inner elements of the §6.6
  contract (`[data-usage-line]`, `[data-window-bar]` /
  `[data-window-label]` / `[data-window-fill]` / `[data-window-reset]`,
  `[data-usage-updated]`, `[data-usage-button]`); never triggers a
  fetch; holds no button code and never posts.
* `src/jev_agent/dashboard/templates/connections.html` — usage block
  per provider card: one compact line plus a `<details>` disclosure;
  per-window bars labelled "Session", "Weekly", and "Monthly"; "updated … ago";
  consent note (D16); API-key line "Billed per token — no
  subscription windows apply to an API key." (D12); bare-mode line
  stating the login does not apply to bare-mode workers, with no usage
  bars (D11); "Check usage" / "Refresh usage" / "Checking…" button as
  a `<form method="post">` to the refresh route with `csrf_input()`
  (no `data-usage-refresh` attribute, no `conn.js` button code);
  "Turn off usage checks" control posting to the disable route.
  Reason wording and next steps render from the provider adapter's
  `WORDING` mapping (§6.5); the template holds no reason wording of
  its own. Phase A: the Claude card shows login state only — no usage
  block renders for Claude until Phase B.
* `tests/unit/test_provider_auth.py` — new labels (D1 wording
  verbatim), one case per precedence-table row (7 non-Keychain Claude rows, 5 Codex,
  §6.2; the 2 Keychain rows are Phase B, §9.2), accessor behaviour (`get_login_source` return shape),
  `resolve_credentials` return shape (`CredentialResolution`:
  effective + present + usage_source), `read_credential_file`
  behaviour (missing/corrupt/non-dict → None, never raises);
  `~/.claude.json` via the same reader: missing/corrupt/non-dict → no `keychain_login`, `oauthAccount` null/empty → no `keychain_login`, `oauthAccount` without `uuid` → `keychain_login` with cache key `"keychain"`.
* `tests/unit/test_dashboard_connections.py` and
  `tests/unit/test_dashboard_providers.py` — render serves the cached
  usage snapshot only (fake transport receives zero calls during
  render); live poll JSON gains the additive `usage` key with the
  fields above (`left_percent` integer, timestamps ISO-8601 UTC with
  Z, `state` from the display-state vocabulary); refresh route: first
  click sets `enabled: true` and starts a fetch, redirect +
  flash "Checking usage — the numbers will appear when the check
  finishes."; refresh inside the floor → redirect + flash "Already
  checked recently — try again in <relative>."; disable route sets
  `enabled: false`, redirect + flash; Phase A Claude card renders no
  usage block; template renders every state-matrix row (§6.5) with
  the exact copy; dashboard start creates the fetcher; dashboard
  shutdown calls `shutdown(5.0)` and joins its threads.
* `docs/config.md` — `[usage]` kill switches (restart required).
* `docs/dashboard.md` — Providers section: detected logins, usage
  windows, every state in §6.5 with its UI wording and next step, kill
  switches, the disable off-ramp.

**Test list (required, all fake-based, each case naming its reason code
and observable pass)**

* Codex: happy path (`primary` + `secondary` → two windows, plan
  `plus` rendered verbatim); `secondary` null → one window plus the
  exact copy "Your plan has no separate 5-hour limit — weekly only";
  primary 43200 → monthly window, label "Monthly", `resets_at` from
  unix seconds (the probe's observed shape is the fixture:
  `usedPercent` 48, `resetsAt` 1795687699, plan `free`); monthly-only
  (`secondary` null, primary 43200) → one window plus the exact copy
  "Your plan has no separate 5-hour or weekly limit — monthly only";
  thin launch PATH (no `node` on it) → the adapter still resolves
  the binary and the child still finds `node` (darwin search dirs —
  `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`,
  `~/.npm-global/bin` — §6.3 probe note); auth-required failure (id-2 error result naming
  authentication) → reason `app_server_failed`; handshake failure
  (`initialize` no ack) → reason `app_server_failed`; `initialize`
  ack carrying an error result → reason `app_server_failed`; spawn
  timeout: no response within 15 s → SIGTERM, then SIGKILL after 5 s
  grace, reason `app_server_failed`; output over 4 MiB → process
  killed, reason `app_server_failed`; missing CLI → reason
  `app_server_failed`; unknown `windowDurationMins` → that window
  dropped, known windows shown, duration logged once; **all windows
  unknown duration → reason `unexpected_response`, never an empty
  snapshot**; duplicate `windowDurationMins` → reason
  `unexpected_response`; `primary` null → reason
  `unexpected_response`; known duration with missing/mistyped `usedPercent` → reason `unexpected_response`; known duration with null `resetsAt` → window shown, "no reset reported"; `resetsAt` in the past → renders
  "resetting…"; `ordinaryUsageAllowed` false → reason
  `usage_blocked`; `spendControlReached` true → snapshot shown as
  reported with the sub-line "Spend limit reached";
  `rateLimitReachedType` non-null → snapshot shown as reported, no
  special handling.
* Cache: atomic write — interrupted write leaves the previous file
  intact and readable; mode 600 on the written file (temp file mode
  600 before rename); `account_hash` change treats the record as
  empty (card shows not-checked-yet until the next fetch); a failed
  fetch never overwrites a good cache entry; stale-write guard — a
  write with an older `fetched_at` never replaces fresher windows,
  while attempt metadata still updates; restart preserves the record
  verbatim, including `next_retry_at`; cross-instance: a second
  process that sees a `last_attempt_at` younger than the floor starts
  no fetch (timestamp check; no file lock in this issue); corrupt file
  treated as empty, card shows "Check usage".
* Fetcher: single-flight (second `refresh()` while one runs returns
  `RefreshResult(outcome="already_running")`); minimum intervals
  between attempts (Codex 5 min, measured from `last_attempt_at`);
  manual refresh inside the floor returns
  `RefreshResult(outcome="too_soon", wait_seconds=…)`; `next_retry_at`
  honoured — no attempt before it, including after a restart;
  `shutdown(5.0)` joins threads within 5 s and terminates any live
  app-server; no fetch before opt-in (`enabled` false → zero calls
  on the fake spawner); 401 counting — first 401/403 allows exactly
  one retry on the next trigger after the floor, a second consecutive
  401/403 stops the provider (`outcome="auth_stopped"` on the next
  `maybe_fetch`); re-arm — a detection change (fake home/env change)
  resumes fetching, a Codex `account_hash` change resumes fetching,
  manual Refresh overrides the stop, and a new fetcher (restart) has
  no stop.
* Display derivation (`derive_display_state`, §6.4 — the connections
  route passes the provider's max-ages): monthly window fetched 25 h
  ago → hidden, card stale when no window is fresh; 23 h ago →
  shown; weekly window 25 h ago → hidden (same rule, monthly
  sibling).
* Dashboard: as listed under the dashboard test files above.
* Kill switch: `[usage] codex = false` → `UsageUnavailable` with
  reason `disabled_by_config`, login state unchanged, zero fetches
  attempted. First run → render state `not_checked_yet` with a "Check
  usage" button. API-key auth → render state
  `not_applicable_api_key`, usage section hidden, fake transport
  receives zero calls.
* Token handling: no token value appears in logs, the cache file, or
  the rendered page (asserted on the fake transport's recorded calls,
  the cache file bytes, and the rendered HTML).
* Local-computation guard: no locally computed remaining appears in
  the snapshot (D17) — a snapshot built from session files is
  rejected by the model (no such constructor exists; the test asserts
  the adapter never reads session files).

### 9.2 Phase B — Claude PR (Claude adapter only)

Checkpoint: Phase B merges on its own after Phase A; the Claude Mac
gate (§10) applies to it alone. All shared plumbing, UI, cache,
fetcher, precedence, and docs land in Phase A; Phase B adds only the
Claude adapter, its tests, and its wording.

**New files**

* `src/jev_agent/core/usage/claude.py` — Claude adapter, constructor
  `ClaudeUsageSource(*, home: Path, transport: httpx.Client | None =
  None, clock: Callable[[], datetime] = _utcnow, keychain_reader:
  Callable[[str], KeychainResult] | None = None)`: `GET
  https://api.anthropic.com/api/oauth/usage` with Bearer access token
  and header `anthropic-beta: oauth-2025-04-20`, 10 s timeout; parser
  for `five_hour` / `seven_day` (`utilization`, `resets_at`), plus
  `seven_day_opus` / `seven_day_sonnet` splits and the `limits[]`
  array — structure per the re-probe record (§9.0), which becomes the
  test fixture; flat keys may be null — prefer the structured array
  when they are, `limits[]` wins on conflict, dedupe by window kind;
  splits present and non-null are included as windows; splits-only
  present → show the split windows; `resets_at` null → `resets_at:
  None` ("no reset reported"); no token refresh (D8); `fetch()` takes
  a pre-resolved access token and `expiresAt` and never reads the
  Keychain or the file itself (§6.3 seam) — the manual handler
  resolves the token (file first, else the macOS Keychain via
  `read_keychain_item("Claude Code-credentials")` on darwin, D10)
  and the background fetcher passes only an already-cached Keychain
  token; the token is cached in process memory only (keyed to the
  `oauthAccount` uuid, absent → `"keychain"`, cleared on restart; a
  manual click always re-reads, a background fetch never does, D10);
  `keychain_reader=None` (the default) means the production
  `read_keychain_item`; tests inject a fake. No HTTP call is made
  when no token is obtainable (the outcome is the Keychain reason,
  §6.5); the adapter compares `expiresAt` to the clock BEFORE the
  call — expired → reason `token_expired_refresh_failed` with no
  request sent; a missing, null, or unparsable `expiresAt` is treated
  as non-expired; a 401 after a non-expired token → `unauthorized`;
  setup-token handling per the probe outcome (D9: rejected by
  default — no distinct setup token exists to test, so setup tokens
  are never tried, reason `setup_token_rejected`; a token is
  classified as a setup token by the `sk-ant-oat01-` prefix, §6.3);
  `httpx` transport injected. Exposes
  `WORDING: dict[str, tuple[str, str]]` (§6.1; §6.5 table is its
  specification). The Keychain reader, the `keychain_login` detection
  kind (§6.1/§6.2), the grant UX (§6.6), and the Keychain reason
  codes (§6.5) all land in this PR — the whole Keychain story is one
  unit (D10).
* `tests/unit/test_usage_claude.py` — with a fake HTTP transport.

**Test list (each case naming its reason code and observable pass)**

* Happy path: two windows + splits included as windows when present
  and non-null, per the re-probe record (the re-probe's recorded
  response is the fixture); Weekly line shows the lowest remaining among the
  weekly windows, labelled with the split when it binds.
* Keychain login: file absent + `oauthAccount` present → kind
  `keychain_login`, card names the Keychain source; "Check usage"
  reads the token via the fake Keychain reader (on the request
  thread), caches it in memory, and fetches — a manual click always
  re-reads (fresh token each click, so a rotated token is picked up
  after a 401), while a background fetch reuses the cached token
  with zero Keychain reads; `fetch()` called with no token never
  reads the Keychain; `maybe_fetch` with a `keychain_login` and no
  cached token → no fetch, no prompt, card stays awaiting-grant;
  restart with a persisted snapshot → stale snapshot shown +
  "Check usage", no Keychain read; restart with no snapshot →
  awaiting-grant row; login disappears (detection no longer finds
  it, including sign-out) → cached token dropped; denied grant →
  reason `keychain_denied`, zero HTTP calls; prompt unanswered for
  120 s → reason `keychain_denied`; unreachable Keychain →
  reason `keychain_unavailable`, zero HTTP calls; Keychain item gone
  (reader outcome "missing") → cached token dropped, reason
  `keychain_unavailable`; file present → the Keychain reader
  receives zero calls and no prompt ever appears; non-darwin → the
  Keychain path is never attempted; bare-mode Keychain login →
  detection + bare-mode warning, usage suppressed, effective
  credential named (none).
* Single window: only `five_hour` present → show it alone; only
  `seven_day` present → show it alone.
* `resets_at` in the past → renders "resetting…".
* 429 with `Retry-After` → reason `rate_limited`; next attempt no
  earlier than `Retry-After` seconds; 429 sets `next_retry_at` in the
  cache record (no failure tally exists). 429 with `Retry-After`
  absent or unparsable → wait at least the cadence floor.
* 401 → reason `unauthorized`, exactly one retry on the next trigger
  after the floor, then stop until re-arm (detection change / manual
  Refresh / restart); 403 → same.
* 404 / 410 → reason `endpoint_changed`.
* Setup-token rejected → reason
  `setup_token_rejected`, never retried; the accepted branch is not
  executable on this Mac (no distinct setup token exists) — it is
  covered by a fixture-only unit test using a `sk-ant-oat01-` token
  against the fake transport, asserting the adapter *would* send it
  if the default ever changed.
* Expired token (`expiresAt` in the past) → reason
  `token_expired_refresh_failed`, zero HTTP calls made; nothing is
  written to `~/.claude/.credentials.json`.
* Changed shape (missing `five_hour` and no `limits[]` data, no splits)
  → reason `unexpected_response`.
* Network error → reason `network_error`; fetch over 10 s → reason
  `timeout`.
* Manual refresh inside the 15-minute floor → `RefreshResult(outcome=
  "too_soon", wait_seconds=…)`; route redirects with the flash naming
  the wait.
* API-key auth → fake transport receives zero calls and no usage
  section renders.
* Kill switch: `[usage] claude_code = false` → reason
  `disabled_by_config`, login state unchanged, zero fetches.

### 9.3 What is NOT in the implementation

No worker changes, no `state.db` migration, no usage history, no CLI
command, no Keychain reading in Phase A (the whole Keychain story is
Phase B, D10), no dashboard on/off toggle, no
in-dashboard CLI install, no `unsupported_version` enforcement, no
changes to `.github/workflows/` or any lint/type/test config.

## 10. Verification plan

* **Unit/CI:** all tests in §9 green; the full test suite and the 8
  required checks green on each PR head (lint, build, test
  3.11/3.12/3.13, screenshots, `principles`, windows-latest).
* **Screenshots:** regenerate `docs/images/` with
  `examples/demo/screenshots.py` (the Connections page changes, so
  its shot `docs/images/dashboard-connections.png` at minimum — name
  every changed file in the PR body); commit only
  the images that changed; revert any regenerated image that did not
  change to its pre-regeneration state.
* **Orchestrator verification:** independent review of the PR against
  this design, one verdict per item: precedence table matches §6.2 row
  for row; every reason code in §6.5 maps to its UI wording;
  cadence numbers match D6/D7; no provider-specific response key, URL,
  or method name appears outside its adapter (§6.1); no token value
  appears in logs, cache, or UI; the SITEMAP entry exists in
  `docs/designs/SITEMAP.md`; plus an implementer self-score out of
  100 — merge only at 85 or above.
* **Mac verification (separate step, orchestrator runs — a merge gate
  per phase):**
  * Phase A: (1) Codex detected + usage shows the windows the
    response carries: both windows when `secondary` is present; one
    window (weekly only) plus the exact copy "Your plan has no separate 5-hour
    limit — weekly only"; one window (monthly only) plus the exact copy "Your plan has no
    separate 5-hour or weekly limit — monthly only" (the Mac's observed outcome
    at probe time: only the monthly window present, plan "free" —
    expect the monthly-only copy on the Mac). Either outcome
    passes the gate; record which one was observed. **The Codex
    success shape was first observed by the probe (§9.0 item 1);**
    Mac verification re-observes it on the merged code. (2) CLI
    versions recorded in the PR body (Codex version observed on the
    Mac — 0.167.0 at probe time; the VM's known-good is
    0.164.0-alpha.9). (3) If the Mac's Claude credentials live in
    the Keychain, record that in the PR body (D10) — the Phase A
    card shows file-only detection, so the Keychain login is
    expected to read as Not connected until Phase B, with the
    Phase A file-only sentence on the card itself (§6.5 `no_login`),
    not only in the PR body. (4) Claude card
    shows login state only — no usage block renders for Claude until
    Phase B. (5) Unit-proven only: spawn timeout, 4 MiB cap, and
    corrupt cache file paths are not observed live; record that in
    the PR body.
  * Phase B: (0) The re-probe (§9.0) ran on the Phase B branch and
    every item has a recorded outcome — a pass, or a failure-branch outcome with the corresponding reason wired; an item failing in a way with no failure branch returns the design for resolve and blocks the merge.
    (1) Claude detected + usage shows both windows with
    percentages and reset times that match the numbers shown by
    `/usage` in Claude Code at the same time (same values within 2
    percentage points per window, reset times within 5 minutes —
    state the comparison in the PR body). (2) Keychain path live:
    the Mac's Claude login is detected as Keychain, the first
    "Check usage" shows the system prompt, "Always Allow" avoids
    re-prompting, and the usage numbers match `/usage`. (3) Setup
    token: rejected (reason `setup_token_rejected` recorded) — the
    default stands because no distinct setup token exists to test;
    acceptance is not expected. (4) If no 429 occurs
    naturally, record "429 path not observed live; unit-proven only"
    — the item is untested, not passed. (5) Claude CLI version
    recorded in the PR body (2.1.318 at probe time).
  * No merge before the phase's gates pass live.
* **Honest boundaries:** the Claude endpoint is community-verified, not
  Baton-verified, until the re-probe (§9.0) and Mac verification observe
  a live 200; the Codex success shape was observed by the probe
  (§9.0 item 1) and is re-observed at Mac verification; the 429/backoff paths are
  unit-proven until observed live; the Keychain case is unsupported
  in Phase A and delivered in Phase B (D10); the Keychain item's
  exact JSON shape is unverified until the re-probe records its key
  names.

## 11. Resolve — §9.0 probe (2026-11-06)

Every pick below records what the probe observed, what was decided,
and why. Picks follow the user's standing principles: browser-first,
least surprise, lowest resource use. Nothing here is implementation —
the amendments are design text only; Phase A starts on branch
`design/issue396-cli-autoimport` after this resolve, Phase B after
the re-probe (§9.0).

### R1 — Codex 43200 is the Monthly window (probe item 1)

* **Observed.** Primary `windowDurationMins` 43200 (= 30 days),
  `usedPercent` 48, `resetsAt` 1795687699 (unix seconds — a
  milliseconds value would be ~1.8e12), secondary null, `planType`
  "free" inside `result.rateLimits`. At probe time the window had
  ~20 days left (1795687699 minus the probe time ≈ 1.76M seconds),
  coherent with a 30-day window ~10 days in. The response's own
  rate-limit-reset credit is titled "Full reset (Monthly)" — consistent with the monthly reading, not proof of it (a one-off credit title, not a window field); the deciding evidence is that 43200 minutes is exactly 30 days.
* **Decided.** 43200 → monthly; new window kind `"monthly"`, UI
  label "Monthly"; the compact line for this account reads "Monthly
  52% left · resets in 20 d" (48% used → 52% left, D14 relative-time
  rules unchanged). The Weekly-line rule is per family: the Weekly
  line shows the lowest remaining among the weekly windows; when no
  weekly window exists there is no Weekly line, and the monthly
  window is the long line. Copy: monthly-only → "Your plan has no
  separate 5-hour or weekly limit — monthly only" (§6.3, §9.1 test
  list, §10 Phase A gate). Monthly max-age 24 h, same as weekly
  (D20). The design does **not** claim all free plans report
  monthly — one account is one account; the rule derives the
  window from the duration, never the position or the plan.
* **Why monthly, not "unknown-long".** 43200 minutes is exactly 30
  days, and the provider's own credit title says "Monthly". Any
  other reading would invent a label the provider does not use.

### R2 — Unknown durations: drop, but never an empty snapshot (probe item 1)

* **Observed.** The old rule ("an unknown duration is dropped, not
  fatal") would have dropped this account's only window and shown
  an empty card — on the very device the shape was verified on.
  Item 1's shape check passed; the gap was mapping, not shape.
* **Decided.** Keep per-window drop (a partially-known response
  still shows its known windows, duration logged once per process
  start) and add the all-unknown rule: when no known window
  remains, the outcome is `unexpected_response`, never an empty
  snapshot (§6.3, §9.1 test list). The drop applies to unknown
  *durations* only — a null `primary` or a duplicated duration stays
  `unexpected_response` (§6.3). *Rationale:* an empty card
  looks like "no limits" — a silent lie. "Usage unavailable" is
  honest, carries a next step (maintainer action), and matches the
  design's own principle that a number the source did not report
  must never be shown. The alternative (make every unknown duration
  fatal) was rejected: it would blank the whole card for a provider
  inventing one new duration alongside known ones.

### R3 — `planType` is read from `result.rateLimits` (probe item 1)

* **Observed.** The probe record's top-level `planType` field is
  null, but the response carries `"planType": "free"` inside
  `result.rateLimits` (and duplicated in `rateLimitsByLimitId`).
* **Decided.** No behaviour change: the parser already reads
  `result.rateLimits` only (§6.3). The design now states the field
  location explicitly so a future reader does not mistake the
  record's summary field for the response. *Rationale:* a one-line
  clarification beats a misread.

### R4 — Binary resolution must cover the darwin search dirs (probe item 1, method note)

* **Observed.** The probe's first attempt failed for a PATH
  artifact, not a protocol failure: the launch shell's PATH lacked
  `/opt/homebrew/bin`, so the `codex` wrapper died with `env: node:
  No such file or directory`. Re-running with the dashboard's
  darwin search dirs on the child's PATH worked.
* **Decided.** `resolve_executable` covers the darwin search dirs
  (`src/jev_agent/core/executables.py::_platform_search_dirs()`:
  `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`,
  `~/.npm-global/bin`, plus `~/.nvm` version bin dirs), not just
  the launch PATH, and the `PATH` passed to the spawned child is
  the launch PATH augmented with those dirs (deduped) (§6.3, §9.1).
  *Rationale:* the adapter spawns from whatever process launched
  the dashboard; a thin launch PATH is the normal case for a
  launchd/double-clicked dashboard, not an exception. The fix is
  in the resolver and the child env, not in the user's shell setup.

### R5 — Claude token: macOS Keychain, on explicit user action, Phase B prerequisite (probe item 2)

* **Observed.** No file-stored Claude token exists on the Mac:
  `~/.claude/.credentials.json` absent; `~/.claude.json` holds
  `oauthAccount` profile metadata only (uuid, email, org, tiers —
  Keychain item key names inspected, values never read; the
  `oauthAccount` uuid is the one value read, as the cache key,
  §6.1). The login exists
  (Claude Code works; `oauthAccount` populated). On Claude Code
  2.1.318 the tokens live in the macOS Keychain (service
  `Claude Code-credentials`).
* **Decided.** The Keychain read is a Phase B prerequisite, not a
  follow-up (D10 rewritten): detection is file-first and
  permission-free (`oauthAccount` presence → kind
  `keychain_login`); the token is read only on the "Check usage"
  click (the grant action — one click, not two) via the `security`
  CLI, cached in process memory only (keyed to the `oauthAccount`
  uuid, cleared on restart; a manual click always re-reads, a
  background fetch never does), never on render/poll/background
  fetch; failures are `keychain_denied` / `keychain_unavailable`
  (§6.5); macOS only.
  The whole Keychain story (detection, reader, grant UX, caching,
  reason codes) lands in Phase B as one unit (§9.2); Phase A keeps
  file-only Claude detection with the limitation stated and
  time-boxed. *Rationale:* the probe proved the file path does not
  exist on the device Phase B must verify on — keeping Keychain as
  a follow-up would ship a Phase B that cannot run on the Mac.
  Splitting by permission boundary (detect needs none, read needs
  the user's grant) puts the grant on the click the user already
  makes instead of inventing a second permission step. The
  `security` CLI is chosen over the native Keychain API: stdlib
  subprocess, no new dependency, and the read is rare and
  user-triggered. Alternatives rejected: driving the CLI's `/usage`
  screen (no machine-readable output; PTY rejected in §7 already),
  the paste-token flow (a setup token is a different credential,
  and its acceptance is unresolved — item 4 defaults to rejected),
  and a separate "Allow Keychain access" click (an extra step for
  the common case, with no benefit once the card warns about the
  system prompt before the click).

### R6 — Re-probe runs on the Phase B branch (probe items 2/3/5/6)

* **Observed.** Items 2/3/5/6 could not run (blocked on the token,
  not failed); item 4 unresolved (no distinct setup token).
* **Decided.** The re-probe runs on the Phase B branch using the
  branch's own Keychain reader to obtain the token — it does not
  hand-roll Keychain access — then re-runs items 2 (usage endpoint
  200 + `limits[]` structure), 3 (cadence: 5 attempts over 1 hour),
  5 (account identity), and 6 (splits), plus new item 0 (Keychain
  item key names, never Keychain item values, confirming the `claudeAiOauth`
  shape — the stored password is either the whole stored JSON object
  containing `claudeAiOauth` or the inner `claudeAiOauth` object
  itself, item 0 records which; a different shape returns the design
  for resolve). Item 0 also records the real `security` exit codes
  and stderr per reader case, the prompt's wording and binary, and
  whether a Deny re-prompts; the re-probe needs a human present who
  chooses "Always Allow" on the first prompt, and runs a throwaway
  script on the branch importing the branch's own reader and adapter.
  Item 4
  is not re-probed; the rejected default stands. Phase B merges
  only when the re-probe passes or records failure-branch outcomes
  (§9.0, §10). *Rationale:* the probe depends on the
  implementation here — the token path *is* the implementation —
  so the gate moves from "before implementation" to "on the branch,
  before merge". The alternative (hand-rolling a Keychain read in
  the probe script) would verify a different code path than the
  product uses.

### R7 — Item 4 stays unresolved; Phase B defaults to rejected (probe item 4)

* **Observed.** No distinct setup token exists on the device to
  test; the only `sk-ant-` credential is the stored OAuth token,
  which lives in the Keychain.
* **Decided.** No change: per §9.0 item 4's failure branch, Phase B
  defaults to rejected (`setup_token_rejected`, never tried).
  *Rationale:* the default was designed for exactly this outcome.

### What this resolve unblocks, and what stays blocked

* **Phase A can start** after this resolve (branch
  `design/issue396-cli-autoimport`): item 1 passed and its mapping
  gap is closed (R1–R4). The Mac gate's expected outcome is now the
  monthly-only copy (§10).
* **Phase B stays blocked** on the re-probe (R6): the Keychain path
  is designed (R5) but unimplemented and unverified, the Keychain
  item's exact JSON shape is unverified, and items 3/5/6 are
  unanswered until a 200 body exists. Item 4 stays on the rejected
  default (R7).

### R8 — Goldfish resolve of R5/R6 (2026-11-06)

A fresh zero-context Goldfish review of the R5/R6 amendments found
5 BLOCKING and 15 NITs. All are resolved below; nothing here is
implementation.

* **B1 — §9.1 said "7 Claude" for a 9-row §6.2 table.** The Claude
  precedence table has 9 rows (2 new Keychain rows: Keychain-only
  and bare-mode Keychain). §9.1 now says "7 non-Keychain Claude
  rows" (both places), and the bare-mode Keychain case is added to
  the §9.2 test list (detection + bare-mode warning, usage
  suppressed). *Rationale:* a test count that does not match the
  table is a lie the implementer cannot satisfy either way.
* **B2 — "Background fetch never touches the Keychain" had no
  enforcement seam.** `fetch()` took no arguments and the fetcher
  called it identically for manual and background paths, so an
  implementer would have built an adapter that reads the Keychain
  when uncached — and a page render would pop a system prompt. The
  seam is now named (§6.3, §6.4, §9.2): the adapter's `fetch()` takes
  a pre-resolved token and `expiresAt` and never reads the Keychain
  (or the file); only the manual "Check usage"/Refresh handler calls
  `read_keychain_item`, synchronously on the request thread (up to
  the 120 s bound, page shows a "waiting for the Keychain prompt"
  state); `maybe_fetch` skips a `keychain_login` with no cached
  token. *Rationale:* the permission boundary must be a code
  boundary — a sentence in D10 is not enforceable.
* **B3 — Cache invalidation was self-contradictory, and Refresh was
  not a real way back.** "Re-read on expiry or the next user action"
  contradicted the §9.2 test "reader called once across two
  fetches"; on the reuse reading a CLI-rotated token 401s, D7
  re-arm cannot see it, and Refresh reusing the stale token re-401s.
  Decided: **a manual "Check usage" click always re-reads the
  Keychain** (D10, §6.3, §9.2) — a fresh token every click, so
  Refresh is a real way back from a 401 (D7 updated); background
  fetches reuse the cached token and never re-read; an expired cached
  token is not used by background fetches
  (`token_expired_refresh_failed`, no request). Invalidation adds
  login disappearance (detection no longer finds the login,
  including sign-out while cached) alongside uuid change and
  restart. The §9.2 test is rewritten to manual+background.
  *Rationale:* the manual click is the user explicitly
  re-authorizing, so re-reading there costs at most a re-prompt
  (avoided by "Always Allow") and buys a guaranteed way back.
* **B4 — `read_keychain_item` denied-vs-unavailable was
  unimplementable.** No exit-code/stderr mapping existed. Decided:
  the mapping table is specified by behaviour in §6.1 (exit 0 +
  non-empty stdout → "ok"; exit 0 + empty stdout → "unavailable";
  stderr "could not be found" → new outcome "missing", the item is
  gone — cached token dropped, card shows `keychain_unavailable`
  with the re-sign-in next step; Deny/dismiss/120 s timeout →
  "denied"; `security` absent, non-darwin, headless, spawn failure,
  or unrecognized stderr → "unavailable"), with subprocess details
  (stdin DEVNULL, temp-dir cwd, 4 KiB stderr cap, 120 s timeout)
  and the `keychain_reader=None` default defined as the production
  reader. The exact exit codes/stderr strings for the deny/dismiss
  cases are deferred to re-probe item 0, which runs the real
  `security` CLI through each case (§9.0). *Rationale:* the numbers
  cannot be verified from the VM, so the design specifies the
  decision procedure and lets the re-probe fill the numbers rather
  than inventing them.
* **B5 — Phase A showed a wrong state on the Mac, recorded only in
  the PR body.** The working Keychain-only login would read as "Not
  connected" + sign-in flows — the exact failure §1 promises never
  to show. Decided: the Phase A card carries the limitation itself
  (§6.5 `no_login` Claude wording gains the sentence "This phase
  detects file-stored logins only — a login in the Mac's Keychain is
  not detected yet."), and §10 Phase A gate (3) expects that sentence
  on the card, not only in the PR body. *Rationale:* §1's promise is
  about what the user sees, not what the PR body says.
* **N1 — D16 "does not copy or store" contradicted "keeps in memory
  only".** Base copy now reads "It does not write the sign-in to disk
  or share it." — consistent with the Keychain addendum.
* **N2 — No 120 s timeout→denied test.** Added to §9.2 (prompt
  unanswered for 120 s → `keychain_denied`).
* **N3 — No reader specified for `~/.claude.json`.** Detection reads
  it through the shared `read_credential_file` (missing/corrupt/
  non-dict → None, never raises); `oauthAccount` must be a non-empty
  dict (null/empty → no Keychain login); its `uuid` is the cache key
  (absent → constant `"keychain"`). Tests added to §9.1.
* **N4 — "Values never read" was false (the uuid is read as cache
  key).** All claims qualified to "Keychain item values"; §6.1 states
  the uuid is the one value read outside the token itself.
* **N5 — "(same login)" for file-wins was unproven.** Removed from
  D10: the file wins and the usage shown is the file token's account;
  the two could in principle belong to different accounts.
* **N6 — `no_login` wording overclaimed (the Keychain item is never
  checked).** Meaning and wording now name what detection actually
  checks (credentials file + `~/.claude.json` `oauthAccount`).
* **N7 — "non-macOS" cause in §6.5 was unreachable** (detection is
  darwin-gated). Removed from the `keychain_unavailable` row; D10's
  reader-contract bullet keeps it at the function level.
* **N8 — Re-probe needed a human + "Always Allow", harness unstated,
  gate ambiguous for FAIL outcomes.** §9.0 now states: a human
  present at the Mac choosing "Always Allow" on the first prompt
  (else item 3 alone needs 5 prompt answers); the harness is a
  throwaway script on the Phase B branch importing the branch's own
  reader and adapter; Phase B merges only when every item has a
  recorded outcome (pass or a wired failure branch) — an item failing
  in a way with no failure branch returns the design for resolve and
  blocks the merge (§10 gate 0 updated to match).
* **N9 — Setup-token "accepted" test branch unexecutable; the
  `sk-ant-oat01-` prefix rule unstated.** The accepted branch is now
  a fixture-only unit test (a `sk-ant-oat01-` token against the fake
  transport, asserting the adapter *would* send it if the default
  ever changed); §6.3 states the classification rule (setup token =
  `sk-ant-oat01-` prefix; any other `sk-ant-` token is an OAuth
  token).
* **N10 — Probe record `02-claude-usage.json` wrongly said "no Claude
  login".** Corrected to "no file-stored Claude token on device;
  login exists (`oauthAccount` populated) — token in macOS Keychain,
  not readable by this probe (scheduling block)". (Design text was
  already correct; only the record was wrong.)
* **N11 — Item 0 must name the two candidate Keychain shapes.**
  §9.0 now names them: (a) the whole stored JSON object containing
  `claudeAiOauth`, or (b) the inner `claudeAiOauth` object itself.
* **N12 — "Always Allow" grant identity unstated.** D10 now states
  the prompt names the `security` command-line tool (the helper
  Baton spawns); "Always Allow" grants that helper, and the grant
  persists for future `security` invocations. Item 0 records the
  prompt's actual wording; if it names something else, the card copy
  is corrected.
* **N13 — Deny-then-retry re-prompt assumed, never verified.** Item 0
  now records whether a second invocation after a Deny re-prompts;
  if it does not, the `keychain_denied` card directs the user to
  grant access in Keychain Access (§6.5 conditional).
* **N14 — Adapter token selection by effective credential unstated.**
  §6.3 now states it: a file token, when present, is used and the
  Keychain is never read — a saved/file token never triggers a
  Keychain prompt on any path. (The §9.2 test already asserted it.)
* **N15 — Post-restart stale + no-token rendering unspecified.**
  §6.5 state matrix now states it: after a restart the card shows
  the persisted snapshot as stale with "Check usage" (no prompt, no
  error); with no snapshot it shows the awaiting-grant row. §9.2
  tests added.

--------------------------------------------------------------------------------

*Concepts brief: `concepts_brief.md` (same directory). Brainstorm lens
reports: `goldfish-brainstorm/lens-yagni.md`, `lens-cost.md`,
`lens-ux.md`, `lens-migration.md`, `lens-observability.md`,
`lens-debt.md`. Goldfish review reports: `review/zoom_out_report.md`,
`review/comprehension_report.md`, `review/critic_design.md`,
`review/critic_structural.md`, `review/critic_style.md`,
`review/intermediate_zoom_out_report.md`,
`review/readiness_implementation.md`,
`review/readiness_codebase.md`, `review/readiness_style.md`,
`review/consolidated_review_report.md`, `review/re-audit-zoom-out.md`,
`review/re-audit-comprehension.md`, `review/re-audit-readiness.md`.*
