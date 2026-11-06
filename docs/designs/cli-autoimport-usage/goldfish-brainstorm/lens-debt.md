# Goldfish Review — Long-Term Tech Debt & Maintenance

- **Lens:** Long-term tech debt & maintainability
- **Concept:** Auto-import Claude Code / Codex CLI logins from the dashboard host and show remaining usage for the 5-hour session window and the weekly window in Connections → Providers.
- **Brief reviewed:** `baton-issue396/concepts_brief.md` (2026-11-06)
- **Code reviewed (read-only):** `src/jev_agent/core/provider_auth.py`, `src/jev_agent/dashboard/routes_connections.py`, `views.cli_providers`, `templates/connections.html` (Providers section), `tests/unit/test_provider_auth.py`
- **Mode:** Brainstorm (upstream concept ideation) — Structural Goldfish persona

## Verdict in one paragraph

This concept, as framed, is a maintenance liability waiting to happen. It bolts two
completely different, vendor-controlled, undocumented mechanisms — an Anthropic
server endpoint Claude Code happens to call, and a hidden local JSON-RPC protocol
inside the Codex CLI — onto a codebase whose provider code is already a single
shared module full of hardcoded file paths, hardcoded JSON keys, hardcoded regexes
for CLI output, and hand-rolled thread state machines. In two years, when Anthropic
renames a field, OpenAI changes a method name, or either CLI moves where it stores
logins, the failure will not be a clean "usage unavailable" — it will be a wrong
number, a blank card, a hung poll, or a crash in the Connections page, and the fix
will touch detection, fetching, caching, routing, and the template at once because
nothing in the current shape separates those concerns. The brief is honest about
the asymmetry of the two sources; the design must now enforce that honesty in
code, or the cheaper source will be dragged down by the fragile one. Nothing
below is a reason to abandon the feature. All of it is a reason to refuse to
build it as two more functions in `provider_auth.py` and two more `if` branches
in the template.

---

## Findings

### 1. Blocker — One shared module already owns both providers; usage will make it a god module

- **Severity:** Blocker
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** `provider_auth.py` already contains, in one file:
  Claude precedence, Codex precedence, two credential-file readers
  (`~/.claude/.credentials.json` key `claudeAiOauth`, `~/.codex/auth.json` keys
  `tokens` / `api_key`), two CLI-output parsers with hardcoded regexes
  (`sk-ant-oat01-…`, `https://auth.openai.com/codex/device`, device-code pattern),
  and two background flow runners (`CodexDeviceFlow`, `ClaudeSetupTokenFlow`)
  with their own locks, watcher threads, and timers. Usage adds, per provider,
  a fetcher, a token-refresh path (Claude), a subprocess lifecycle (Codex
  `app-server`), a cache, a refresh schedule, rate-limit handling, and error
  taxonomy. If all of that lands in the same module, a change to Anthropic's
  endpoint forces a reviewer to read and re-test Codex code, and vice versa.
  The existing `ProviderAuth` dataclass already shows the strain: a single
  stringly-typed `auth` field (`"oauth_token" | "api_key" | "cli_login" | "none"`)
  plus free-text `label` / `detail` strings that the template and the live poll
  both consume. Adding usage fields to that dataclass repeats the pattern:
  every new provider fact becomes another field on a shared struct that both
  providers must fill, even when it does not apply to them (API-key auth has
  no usage windows at all).
- **Maintainability Requirement:** Split provider knowledge by provider before
  usage is added. One adapter module per provider (e.g. `providers/claude.py`,
  `providers/codex.py`), each owning its own file paths, JSON keys, endpoint /
  protocol details, and refresh logic. `provider_auth.py` keeps detection
  only, or becomes a thin registry over the adapters. No provider-specific
  string, path, URL, or response key may appear in shared code, in `views.py`,
  in `routes_connections.py`, or in the template.

### 2. Blocker — No replaceable seam: dropping one source today means rewriting the page

- **Severity:** Blocker
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** There is no seam between "where usage comes from"
  and "what the page shows". `views.cli_providers()` returns a concrete
  `list[ProviderAuth]` built by calling the two detection functions directly;
  `routes_connections.py` passes that list straight to the template and to the
  `connections_live` JSON; the template then branches on
  `p.provider == "claude_code"` with provider-specific HTML, forms, and hints
  inline. To add usage this way, the design would add usage fields to
  `ProviderAuth`, more branches to the template, and more keys to the live
  JSON — so removing the Claude endpoint in two years (because Anthropic
  killed it) would mean editing the dataclass, the view, the route, the live
  poll, and the template, and re-testing Codex at the same time. The same is
  true in reverse for the Codex protocol. A source that cannot be deleted in
  one place is a source that will be left half-dead in the codebase.
- **Maintainability Requirement:** Define the smallest seam and make the
  design doc name it explicitly: a `ProviderUsageSource` protocol with one
  method — roughly, `fetch() -> UsageSnapshot | UsageUnavailable(reason)` —
  returning only a normalised, provider-neutral shape (see finding 4). The
  page, the route, and the live poll consume only that normalised shape and
  never know which source produced it. Each provider registers one adapter
  behind the protocol. Dropping or replacing a source is unregistering one
  adapter and deleting one module; the page, route, and template do not
  change. The design must also state the kill-switch: a per-source flag that
  turns a broken source into `UsageUnavailable` without a code change or a
  redeploy of the page.

### 3. Blocker — The Claude source is undocumented and rate-limited; the design has no failure contract for it

- **Severity:** Blocker
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The brief itself records the facts: the Claude
  endpoint (`GET https://api.anthropic.com/api/oauth/usage`, beta header
  `oauth-2025-04-20`) is undocumented, verified only by community projects,
  not by Baton; it shares a rate-limit bucket with the CLI's own polling
  (HTTP 429, roughly one request in three succeeding under an active session);
  whether it accepts the dashboard's own long-lived `sk-ant-oat01-` token is
  unverified; and Anthropic's terms reserve Pro/Max sign-in tokens for Claude
  Code. That is three independent ways for this source to stop working —
  endpoint moved, token kind rejected, throttled — and none of them is a
  Baton bug. If the fetcher is written as "call the endpoint, parse the
  JSON", every one of those failures becomes either an exception in the
  refresh path, a stale number presented as current, or a silent blank.
  Worse, a naive retry loop would fight the CLI for the same shared bucket
  and make the user's own `/usage` screen fail more often.
- **Maintainability Requirement:** The design must specify a failure contract
  per source, not a happy path: every fetch outcome is one of `ok`,
  `stale (show age honestly)`, `rate-limited (honour Retry-After, back off,
  never retry in a tight loop)`, `unsupported / changed shape (log once,
  show "usage unavailable", keep the login state working)`, or
  `auth rejected (do not retry with the same token)`. The usage feature must
  be separable from the login feature: if the endpoint dies, the imported
  login, the workers, and the rest of the Connections page keep working.
  The open decision in the brief — always-on with honest fallback vs opt-in —
  must be settled in the design doc with a default, because "always-on" means
  Baton owns the ongoing cost of tracking Anthropic's changes, and that cost
  needs an owner and a sunset rule, not just a launch.

### 4. Major — Response-shape coupling: the code will hardcode `five_hour`, `seven_day`, `primary` / `secondary` everywhere

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** Both sources return window shapes that are
  vendor inventions, not a contract: Claude returns `five_hour` /
  `seven_day` objects with `utilization` / `resets_at`, plus `seven_day_opus`
  / `seven_day_sonnet` splits and a `limits[]` array; Codex returns
  `rateLimits.primary` / `.secondary` with `usedPercent` /
  `windowDurationMins` / `resetsAt`, plus `planType` and several flags.
  The brief already warns not to hardcode two windows for Codex — the 5-hour
  window is suspended for many accounts, so `secondary` may be null — but
  the same warning applies to Claude, whose splits and limits array can grow
  or shrink without notice. If parsing code does `data["five_hour"]["utilization"]`
  or `data["rateLimits"]["primary"]["usedPercent"]` at the call site, a
  single renamed or missing key breaks the whole card, and a null window
  becomes a crash or a `0%` that looks like full allowance. The existing
  code already has this habit: `_credentials_login` checks `data.get("claudeAiOauth")`,
  `_codex_auth_file` checks `data.get("tokens")` / `data.get("api_key")` —
  key-presence checks with no schema version, no validation, and no
  "shape changed" signal.
- **Maintainability Requirement:** Parse each vendor response exactly once,
  inside its own adapter, behind a versioned parser, into a normalised
  internal model — e.g. a list of `UsageWindow { kind, used_percent,
  resets_at }` derived from `windowDurationMins` / the response's own
  window labels, never from hardcoded window names in shared code. Unknown
  fields are ignored; missing or mistyped expected fields produce
  `UsageUnavailable(reason="source changed shape")`, never an exception and
  never a fabricated zero. The design doc must list the normalised model and
  state that the template renders windows from that list, so adding or
  removing a window in future is a data change, not a template change.

### 5. Major — Credential-file coupling: detection already assumes where CLIs store logins, and that assumption is already half wrong

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** Detection reads `~/.claude/.credentials.json`
  and `~/.codex/auth.json` directly, with no abstraction over "where does
  this CLI keep its login". The brief notes the crack: on macOS the Claude
  credentials may live in the Keychain (`Claude Code-credentials` service)
  instead of the file. That means detection can already report "not logged
  in" for a user who is logged in — and the usage fetcher, which needs the
  actual access token and refresh token from that same file, will fail in
  exactly the same case, while the workers (which shell out to the CLI and
  let it read its own store) keep working. The same risk applies to Codex:
  if a future CLI version moves `auth.json`, renames `tokens`, or encrypts
  the file, both detection and usage break together, silently, because they
  share the same unversioned file read. The current code also has no record
  of which CLI version it was tested against, so when a user reports a
  broken card in two years there is nothing to correlate the failure with.
- **Maintainability Requirement:** Put every credential-location fact behind
  one accessor per provider (`get_login_source() -> {kind, path-or-store,
  detected_at, cli_version if known}`), shared by detection and usage, so a
  storage move is fixed in one place. The accessor must report its source
  explicitly (file path, Keychain, env var) and must distinguish "no login
  found" from "login store unreadable / shape unknown". The design must
  decide the Keychain case now — supported, explicitly unsupported with an
  honest message, or deferred — and must record the CLI version alongside
  the imported state so future breakage is diagnosable. Never let the usage
  fetcher re-implement its own file read alongside detection's.

### 6. Major — A third hand-rolled state machine is coming, and the first two already show the cost

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** `CodexDeviceFlow` and `ClaudeSetupTokenFlow`
  are each a hand-rolled state machine: string states (`idle | starting |
  waiting | done | failed | cancelled`), a `threading.Lock`, a watcher
  thread reading subprocess stdout line by line, a timer in the Claude case,
  and `cancel()` / `shutdown()` methods that must join threads and terminate
  processes without deadlocking. The tests already carry the cost: fake
  processes with `threading.Event`, release gates, polling helpers
  (`_wait_for`, `_wait_for_claude_status`), and comments about thread-timing
  races between a state flip and its field updates. Usage refresh needs
  another lifecycle — `never fetched | fetching | fresh | stale |
  rate-limited | failed | unsupported` — with a cache, a TTL, and a
  background schedule. If it is built as a third copy of this pattern (one
  per provider, or one shared but stringly-typed), Baton will own three
  subtly different concurrency bugs, three cancel/shutdown paths, and three
  test suites full of timing races. The brief leaves the cache location and
  refresh cadence as open decisions (state.db vs a JSON cache file; cadence
  per provider); whichever is chosen, the lifecycle code is where the debt
  will live.
- **Maintainability Requirement:** Do not copy the flow-runner pattern for
  usage. The design must name one refresh/caching mechanism shared by both
  providers (one scheduler, one cache with TTL + staleness metadata, one
  error taxonomy), with per-provider behaviour expressed as data (poll
  interval, timeout, backoff) rather than per-provider threads. Cache entries
  must carry `fetched_at`, `source`, and `source_version` so the page can
  show staleness honestly and a future maintainer can tell a stale number
  from a fresh one. If the cache lives in state.db, the design must address
  schema migration; if it is a JSON file, it must address corruption,
  concurrent writes, and cleanup — the choice cannot stay open past the
  design doc.

### 7. Major — The live poll and the page render are synchronous; usage fetching will block them

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** `connections_page` builds providers by calling
  `views.cli_providers()` synchronously, and `connections_live` re-runs the
  same detection on every poll to refresh the sidebar. Detection today is
  cheap (env checks + two small file reads). Usage fetching is not: a
  network call to Anthropic (with possible 429 + `Retry-After` wait) and a
  subprocess spawn of `codex app-server` (stdio JSONL handshake, 15 s
  timeout, SIGTERM, 4 MiB cap) per refresh. If either fetch is called inline
  from `cli_providers()` or `connections_live`, a slow or hung source makes
  the whole Connections page slow or the live poll time out — and a hung
  Codex subprocess becomes a dashboard hang. The template already renders
  provider cards in a loop over the same list, so a slow provider also
  delays the other provider's card.
- **Maintainability Requirement:** Usage must never be fetched on the render
  or poll path. The design must require a background refresh that writes to
  the cache, with render and poll reading only the cache (plus its staleness).
  Each provider's fetch must have its own timeout and must fail independently
  — Claude being slow or down must not delay or blank the Codex card.
  The live poll's JSON shape must be versioned or extended additively
  (new `usage` key alongside the existing provider fields), so older cached
  clients and tests do not break when usage appears.

### 8. Major — The Codex subprocess protocol is a second CLI-internal coupling, disguised as a clean one

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The brief correctly calls the Codex source
  lower-risk than Claude's because it is local and needs no network call by
  Baton. That is true, and it is also the trap: `codex app-server` is a
  hidden protocol — stdio JSONL, an `initialize` handshake, then
  `account/rateLimits/read` — verified live only on codex-cli
  0.164.0-alpha.9, on this VM, in the failure case (no login, so the
  rate-limits call returned an auth error, not data). The success shape has
  not been observed by Baton at all. The protocol can change method names,
  handshake requirements, or output framing in any release, and there is no
  version negotiation in the design. The existing subprocess discipline in
  the brief (temp-dir cwd, scrubbed env, 15 s timeout, SIGTERM, 4 MiB cap)
  is good, but it is described in prose, not owned by any component — if it
  is re-implemented inline in a usage function, the next caller will forget
  half of it, and a leaked `app-server` process or an uncapped output read
  becomes a resource leak on the dashboard host.
- **Maintainability Requirement:** The Codex adapter must own the whole
  subprocess contract in one place: spawn arguments, handshake, method name,
  timeout, output cap, termination, and env scrubbing — with the protocol
  version / CLI version recorded at fetch time. It must treat any
  unexpected frame, unknown method error, or handshake failure as
  `UsageUnavailable`, never as a crash. The design must require a live
  verification step on the Mac with a real login (as the brief's staging
  already says) before merge, and must state what happens when the protocol
  changes: the adapter is replaced, the page is untouched (finding 2).

### 9. Major — Token refresh is a write path hiding inside a read feature

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The Claude usage call needs a live access token.
  The file holds `accessToken` + `expiresAt` + a refresh token, and an
  expired token "is renewed against `https://console.anthropic.com/v1/oauth/token`".
  That turns a read-only usage display into a credential-write path: who
  writes the refreshed token back, to the file or the Keychain, and what
  happens when the CLI itself refreshes the same file at the same time?
  What happens when the refresh fails — is the stored login now considered
  broken? The current codebase is careful about this boundary: detection
  never returns values, the setup-token flow saves through one `write_secret`
  path, and the Codex flow stores nothing itself ("the CLI keeps its own
  `auth.json`"). A usage fetcher that quietly refreshes and rewrites the
  CLI's credentials breaks that boundary and creates a race the dashboard
  cannot win, plus a new place where a token value can leak into logs or
  into the usage cache.
- **Maintainability Requirement:** The design must state the refresh
  ownership rule explicitly: either Baton never refreshes (usage is
  unavailable when the access token is expired, and the user re-opens the
  CLI to refresh it — honest, low-debt), or refresh is a single, named,
  tested component with one write-back path, file locking or atomic replace,
  and a rule that a failed refresh never corrupts the stored login. The
  usage cache must never contain a token value. The `sk-ant-oat01-`
  setup-token question in the brief (open decision 6) is part of this: if
  that token kind is tried against the endpoint, the design must say so and
  must handle its rejection as `UsageUnavailable`, not as an auth failure
  of the imported login.

### 10. Major — "Imported" as a recorded state creates a second source of truth that will drift

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The brief lists as open decision (2) whether
  "imported" is a recorded state or derived live on each render, and the
  goals say to "record + show the login as imported from the device with its
  source and detection time". A recorded state (a row in state.db, a flag in
  a JSON file) is a snapshot: the user can sign out of the CLI in a terminal,
  delete `~/.claude/.credentials.json`, or switch accounts, and the recorded
  "imported" row will still say imported until something notices. The live
  poll already re-runs detection, so a derived state stays correct for free;
  a recorded state needs invalidation, reconciliation, and a story for
  "recorded but no longer true" — and for "recorded, then the CLI was
  reinstalled with a different account". The same drift applies to cached
  usage: a weekly number fetched yesterday belongs to yesterday's account,
  and if the account changes, the stale number is now attached to the wrong
  person. The bare-mode gap (open decision 5) makes it worse: a login can
  be "imported" for display while workers in bare mode cannot use it.
- **Maintainability Requirement:** Prefer deriving "imported" live from
  detection on each render/poll, with source path + detection time as
  display metadata, not as a persisted identity. If the design chooses a
  recorded state, it must specify the invalidation rule (re-derived on every
  poll, cleared when detection disagrees, keyed by a login fingerprint such
  as source path + account hint — never by token value) and the conflict
  behaviour. Cached usage must be keyed to the login it was fetched for and
  discarded when the login changes. The bare-mode warning must stay attached
  to the imported state so "imported" never implies "workers can use it"
  when they cannot.

### 11. Major — Testability: the current seams are good, and usage will break them if it is not injected the same way

- **Severity:** Major
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The existing code is unusually testable for
  this kind of feature: `claude_code_auth` / `codex_auth` take `env`, `home`,
  and `which` as injectable parameters; the flows take `popen` and
  `write_secret` as constructor arguments; the tests fake homes, fake
  processes, and fake tokens, and assert names-only. Usage fetching needs
  the same treatment — an injectable HTTP client (or transport), an
  injectable subprocess spawner, and an injectable clock for TTL/staleness
  — or its tests become live network calls, real `codex app-server` spawns,
  and wall-clock sleeps. Without those seams, the first maintainer to touch
  the Claude endpoint will have no fast test proving the 429, expired-token,
  and changed-shape paths, and will ship blind. The existing tests also
  show the cost of getting this wrong: the setup-token flow tests already
  need polling helpers and carry comments about thread-timing races.
- **Maintainability Requirement:** Every usage adapter must accept its
  transport (HTTP client, process spawner, clock, cache) by injection, with
  no direct `httpx.get`, `subprocess.Popen`, `time.time()`, or file read at
  the call site. The design must require tests for: changed response shape,
  null/absent window, 429 with `Retry-After`, expired token, missing login
  store, Keychain-only login, CLI not installed, and subprocess timeout —
  all with fakes, no network, no real CLI. If a path cannot be tested with
  a fake, that is a design smell, not a test gap.

### 12. Nit — Presentation debt: "remaining" must never be computed from local consumption

- **Severity:** Nit
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** The brief is clear that local session files
  (`~/.claude/projects/*.jsonl`, `~/.codex/sessions`) show consumption only
  and remaining is never written to disk — but the codebase will, in two
  years, contain both a "usage" fetcher and a pile of local session files,
  and a future contributor under pressure will be tempted to fill a gap
  ("Claude endpoint down, show something from session files") with a
  computed number labelled as remaining. That is a correctness lie that
  compounds: once users have seen a number, removing it looks like a
  regression even when the number was wrong.
- **Maintainability Requirement:** Encode the rule in the normalised model:
  `UsageSnapshot` carries only server-reported windows; there is no field
  for locally computed remaining, and no fallback path from session files
  to the usage display. If a source is down, the card shows "usage
  unavailable" with the reason and the last-fetched time — never a
  locally derived substitute.

### 13. Nit — Account identity is asymmetric and will tempt a fake

- **Severity:** Nit
- **Dimension:** Long-Term Tech Debt & Maintainability
- **Tech Debt Description:** Codex's rate-limits response carries
  `planType`; Claude's usage response carries no plan. The goals want
  account identity shown ("which account / which plan"). The gap will
  tempt a future change to infer a Claude plan from the token, the login
  file, or the usage numbers — none of which is a plan — or to show the
  same "plan" label for both providers with different meanings.
- **Maintainability Requirement:** The normalised model must make identity
  optional per provider (`account_hint` / `plan` may be absent), and the
  template must render absence as absence ("plan not reported by this
  provider"), not as a guessed value. Do not add a Claude plan field that
  no source fills.

---

## What the design doc must contain before implementation

1. The `ProviderUsageSource` protocol and the normalised `UsageSnapshot` /
   `UsageUnavailable` model, with window derivation rules (no hardcoded
   window names in shared code).
2. One adapter module per provider, owning all provider-specific paths,
   keys, URLs, method names, and parsers; no provider-specific literal
   outside its adapter.
3. A per-source failure contract (ok / stale / rate-limited / unsupported /
   auth-rejected) and a per-source kill-switch, with the rule that usage
   failure never breaks login detection, workers, or the rest of the page.
4. The cache + refresh design settled, not left open: one shared scheduler
   and cache, TTL and staleness metadata (`fetched_at`, `source`,
   `source_version`), render/poll reading cache only, per-provider
   timeouts and independent failure.
5. The token-refresh ownership rule (never refresh, or one named component
   with one write-back path), the `sk-ant-oat01-` decision, and the
   Keychain-only decision.
6. The "imported" state rule: derived live preferred; if recorded, the
   invalidation and account-change rules, and the bare-mode caveat.
7. Injection seams (HTTP transport, process spawner, clock, cache) and the
   required fake-based test list in finding 11.
8. A versioning / drift story: CLI version recorded with the imported
   state, source version recorded with each cache entry, and a stated
   sunset rule for the undocumented Claude endpoint (what signal triggers
   dropping it, and who owns watching for that signal).

## What is already good (brief, for the record)

- Detection is read-only and names-only; tokens are never returned, logged,
  or rendered. That boundary must survive the usage work unchanged.
- The brief verifies both sources instead of inventing them, records the
  Codex protocol's own rules (derive windows, do not drive the TUI), and
  names the asymmetry between the two sources instead of pretending they
  are the same kind of source.
- `env` / `home` / `which` / `popen` injection in the existing code is the
  right pattern; usage should extend it, not abandon it.
- The live poll re-running detection is the right model for "imported" —
  derived, not recorded — and the design should keep it.
