# Goldfish review — Observability & SRE debuggability lens

**Target:** `baton-issue396/concepts_brief.md` — auto-import Claude Code / Codex CLI
logins and show remaining usage (5-hour session window, weekly window) in
Connections → Providers.

**Reviewer stance:** fresh reader, zero prior conversation. Checked against the
referenced code as it exists today: `core/provider_auth.py`,
`dashboard/routes_connections.py`, `dashboard/views.py` (`cli_providers`),
`dashboard/templates/connections.html` (Providers section),
`tests/unit/test_provider_auth.py`, plus the existing precedent
`servers/stats.py` (`ServerStatsCache`).

**Verdict up front:** the brief is honest about the two usage sources and
their asymmetry, and it is honest that the Claude endpoint is undocumented
and rate-limited. But as a specification for something an operator has to
debug, it is not yet shippable. It defines one display state — "usage
unavailable" — for at least eight different failures, it defines no logging
at all, it never says how the dashboard tells "never fetched" from "fetched
and failed" from "fetched but stale", and it puts no numbers on timeouts,
retry budgets, or render blocking. If this is built as written, the first
time usage shows "unavailable" nobody — not the user, not the developer
triaging the report — will be able to say why, and the most likely
explanations will all look identical on screen.

---

## Blocker 1 — "Usage unavailable" conflates at least eight different failures

The brief promises "every state visible and actionable", but it never
enumerates the failure states for usage. Today the auth code has exactly
four auth values (`oauth_token`, `api_key`, `cli_login`, `none`) and the
template renders one badge per provider. Usage adds a whole second axis,
and the brief leaves it as a single bucket: "usage unavailable".

These are different causes, with different fixes, and they must not share
one label:

1. **No login at all** — nothing to fetch from. Fix: sign in.
2. **Expired access token, refresh failed** (Claude) — the refresh-token
   call to `console.anthropic.com` failed. Fix depends on the reason:
   revoked/expired refresh token means re-sign-in; a network blip means
   wait and retry. A single "unavailable" hides which.
3. **Provider rejected the token (401/403)** — e.g. the unverified
   `sk-ant-oat01-` setup token is not accepted by the usage endpoint
   (open decision 6 in the brief). If Baton retries that token on every
   poll, it burns requests forever and the user never learns the token
   kind is the problem. This needs its own reason code, e.g.
   `setup_token_rejected`, and it must stop retrying that token kind
   until something changes.
4. **Rate-limited (429)** — the Claude endpoint throttles its own callers
   and the brief itself notes roughly one request in three succeeds under
   an active session. A 429 is not an error in the user's setup; it is
   expected, transient, and must be shown as "rate-limited, next try at
   …" using the `Retry-After` value, not as a failure.
5. **No network / DNS / TLS failure** — the dashboard host is offline.
   Different fix (check the network), and it should be distinguishable
   from a provider-side problem.
6. **Changed response shape** — the undocumented Claude endpoint returns
   a new field layout, a missing `five_hour`/`seven_day` object, or a
   value outside 0–100. This is a code problem (Baton's parser is stale),
   not a user problem, and it must be diagnosable as such.
7. **Hung or crashed Codex app-server subprocess** — the local
   `codex app-server` spawn hangs, dies, or answers a handshake but never
   answers `account/rateLimits/read`. This is a local process problem,
   not an auth problem.
8. **Not applicable, not failed** — API-key auth (`ANTHROPIC_API_KEY` /
   `OPENAI_API_KEY`) has no subscription windows at all. The brief notes
   this for Claude but never says what the UI shows. "Unavailable" for an
   API-key login is wrong and will generate false bug reports; it must be
   a distinct state such as "no subscription windows for API-key auth".

**Requirement:** the design doc must define an enumerated failure taxonomy
— one reason code per provider per cause, e.g. `no_login`,
`token_expired_refresh_failed`, `unauthorized`, `rate_limited`,
`network_error`, `timeout`, `unexpected_response`, `app_server_failed`,
`not_applicable_api_key` — carried in the usage record, rendered in the UI
in plain words with the matching next step, and used as the log field.
No new failure may be added later by reusing the generic bucket.

## Blocker 2 — No logging specification at all

`provider_auth.py` today contains zero logging (no `logging` import, no
logger). The brief says tokens must never be logged, which is correct and
already enforced for rendered values by `test_claude_never_returns_values`,
but it never says what **must** be logged. A feature whose only failure
signal is a UI badge is undebuggable.

**Requirement:** every usage fetch must emit a structured log event with,
at minimum: provider, source (`claude_oauth_endpoint` /
`codex_app_server`), reason code from Blocker 1, HTTP status or process
exit signal where applicable, duration in milliseconds, `Retry-After`
when present, attempt number, and fetched/stale status. What must never
appear: any token value, any refresh token, any account identifier (no
email, no account id, no user id — the brief says "no account identity is
shown", and the same rule must bind logs), and no response body. Two
specific traps to name in the design:

- Exception messages from HTTP and JSON-parsing libraries can embed
  URLs, headers, or response fragments. Log the exception *type* and a
  curated message, never `str(exc)` verbatim from a network call.
- The cached usage record (whichever store is chosen, open decision 1)
  must contain only numbers and timestamps (utilization, `resets_at`,
  `fetched_at`, reason code). A cache file that ever holds a token is a
  leak, and the existing test pattern (assert the fake token appears in
  no rendered text) must be extended to log lines and cache files.

## Blocker 3 — "Never fetched" vs "fetched and failed" vs "fetched but stale" are not distinguished

The brief says usage is "refreshed in the background" and "show staleness
honestly", but it defines no state machine and no timestamps. The codebase
already has the right precedent and the brief does not say to use it:
`ServerStatsCache` in `servers/stats.py` has explicit states
(`never` / `collecting` / `ready` / `failed`), keeps the last good result
on failure, records the error, and its `get()` never blocks. The usage
feature needs the same contract, stated explicitly:

- **never** — no fetch has ever succeeded or been attempted (e.g. just
  imported, or dashboard restarted with an empty cache). UI: "Usage not
  checked yet", not a number and not an error.
- **collecting** — a fetch is in flight. UI keeps showing the previous
  value (if any) with a "checking…" indicator; it must never blank out
  good data while a refresh runs.
- **ready** — fresh data with a `fetched_at` timestamp shown in the UI
  ("Updated 4 min ago"). Freshness needs a number: the design must set a
  max-age after which `ready` becomes `stale`.
- **stale** — last good data older than the max-age, fetch failing or not
  yet retried. UI shows the old numbers clearly marked stale, with the
  age ("last updated 52 min ago") and the reason the refresh failed.
- **failed** — no good data at all and the latest fetch failed. UI shows
  the reason code in plain words, not a number.

Without `fetched_at` and a max-age in the record, "stale" is a feeling,
not a state. The design must also say whether this state survives a
dashboard restart: if the cache lives only in memory, a restart silently
resets everything to "never" and loses the last-known-good numbers. The
server-stats precedent persists JSON to disk under the state dir; usage
should do the same (JSON is also inspectable by whoever triages a report,
whereas `state.db` is opaque — that is an observability argument for the
JSON side of open decision 1).

## Blocker 4 — No timeouts, retry budget, or render-blocking guarantee

The brief gives exactly one timeout: 15 s for the Codex app-server spawn.
Everything else is unbudgeted:

- No HTTP timeout for the Claude usage endpoint (connect timeout,
  read timeout). A hung HTTPS request with no timeout is a hung
  background thread; enough of them and refreshes pile up.
- No retry policy for the Claude endpoint. The brief admits the endpoint
  429s aggressively and shares a bucket with the CLI's own polling. If
  Baton retries eagerly, it competes with the CLI, makes the 429 worse,
  and burns the user's allowance of endpoint calls. The design needs a
  failure budget: sparse cadence (minutes, not seconds — the brief says
  this but never gives a number), jitter, honour `Retry-After`, a
  consecutive-failure counter, and a circuit breaker (e.g. after N
  consecutive failures, pause fetching and say so in the UI with the
  next scheduled attempt) instead of retrying forever.
- No guarantee that page render never waits on usage. Today
  `connections_page` calls `views.cli_providers()` synchronously and
  `connections_live` re-runs detection on every poll. If usage fetching
  is folded into either path, a slow provider call blocks the page or
  the poll. The design must state the rule the server-stats code already
  follows: render reads the cache only and never blocks; refresh runs in
  a background thread with single-flight protection (a second refresh
  while one is `collecting` is refused, as `ServerStatsCache.refresh()`
  does); and the live poll either carries cached usage or carries
  nothing — it must not trigger a fetch. The brief never says whether
  `connections_live` will include usage at all, so the poll frequency
  and the fetch cadence are currently coupled by accident. Decouple
  them explicitly.

## Major 1 — The Codex subprocess has no lifecycle observability

The brief's spawn discipline (temp `cwd`, scrubbed env, 15 s timeout,
SIGTERM after the answer, 4 MiB cap) is good, but it stops at the happy
path. Missing:

- **Timeout kill escalation:** SIGTERM after 15 s, then SIGKILL after a
  short grace period, and a log line recording which signal killed it.
  A process that ignores SIGTERM and is never reaped becomes a zombie
  that holds the single-flight `collecting` state — the exact failure
  mode `ServerStatsCache` documents avoiding (a stuck `collecting`
  entry that refuses all future refreshes for the process lifetime).
- **Per-stage reason codes:** `spawn_failed` (binary missing/crashed on
  start — distinct from `auth: none`), `handshake_failed` (the
  `initialize` call failed — a protocol-version problem, e.g. the CLI
  was upgraded and the method changed), `rate_limits_failed` (handshake
  OK, method failed — including the explicit `authentication required`
  failure the brief verified live), `timeout`, `parse_failed` (malformed
  JSONL). Lumping these as "app-server failed" repeats Blocker 1
  locally.
- **Version logging:** log the Codex CLI version (from `codex --version`
  or the handshake) on each fetch. When the protocol changes, the first
  triage question is "which CLI version", and without it the report is
  guesswork.

## Major 2 — Response-shape drift has no detection or versioning plan

The Claude source is an undocumented endpoint. The brief lists the
response fields (`five_hour`, `seven_day`, `seven_day_opus`,
`seven_day_sonnet`, `limits[]`) but defines no validation: what happens
when a field is missing, when `utilization` arrives as a string, when it
is outside 0–100, when `resets_at` is unparseable or already in the past,
when a new window appears? The Codex side has the matching rule stated
("do not hardcode two windows — derive them from `windowDurationMins`"),
but the Claude side has no equivalent, and the 5-hour window being
suspended for many accounts (noted for Codex) can presumably happen for
Claude too.

**Requirement:** strict parse-and-validate with a named
`unexpected_response` reason that logs *which fields were missing or
invalid* (field names and expected types only — never values that could
identify the account), plus a rule that windows are derived from the
response, never hardcoded to two. A silently wrong number (e.g. showing
"100% remaining" because a missing field defaulted to zero utilization)
is worse than showing "unavailable": the design must say which invalid
inputs produce `failed` and never a fabricated number.

## Major 3 — No operational surface: no "last fetch", no manual retry, no aggregate state

The user has no way to answer "is this broken right now, and since when?"
from the dashboard:

- No per-provider, per-window "last fetched at" / "next refresh at"
  timestamps in the UI. The live poll JSON (`connections_live`) today
  returns only `provider`, `auth`, `label`, `installed` — usage state is
  not in it, so it is unclear whether usage updates without a full page
  reload.
- No manual "refresh usage" action. When a fetch fails transiently, the
  user's only recourse is to wait for an undisclosed schedule. A manual
  retry button (rate-limited itself, e.g. one press per minute) is the
  cheapest triage tool and the brief never mentions it.
- No aggregate health signal. Server health has an aggregate
  (`ok` / `degraded`) on the Connections page; providers have none. One
  provider's usage failing while the other works should be visible as a
  per-provider state, and a sensible aggregate ("usage degraded") should
  exist so the failure is not only discoverable by reading a small badge.
- No counters: consecutive failures, total fetches, success rate per
  provider. Even in-memory counters exposed in the usage record would
  let a bug report say "Claude failed 14 times in a row since 09:12"
  instead of "it says unavailable".

## Major 4 — The token-refresh path is a black box

Claude usage needs the access token from `~/.claude/.credentials.json`,
renewed via the refresh token when expired. The brief mentions the
renewal in one sentence and never treats it as a failure domain. It is
one: refresh can fail because the refresh token is revoked (re-sign-in
needed), because the token endpoint is unreachable (retry later), or
because the credentials file is unreadable — and on macOS the credentials
may live in the Keychain instead of the file, which today's detection
(`_credentials_login`, file only) does not check at all. That last case
is an observability trap: the CLI works, Baton reports "not logged in",
and the usage fetch is never even attempted. The design must log which
credential source was attempted (file vs Keychain vs env token, names
only), give refresh failure its own reason code, and record whether the
token in use is the CLI-login access token or the `sk-ant-oat01-` setup
token — because which token was rejected determines the fix, and open
decision 6 is exactly this question.

## Nit 1 — "Imported" needs a detection timestamp, or its staleness is invisible

Open decision 2 asks whether "imported" is a recorded state or derived
live on each render. From an observability view, derived-live has a cost:
every render re-reads credential files and re-runs `which()`, so there is
no record of *when* the login was first seen, and a login that disappears
(a logout in the terminal, a deleted credentials file) looks identical to
one that was never there. Record the import (source path, detection time,
auth kind) and show it; the live poll already re-runs detection, so a
recorded state with a "last seen" time is strictly more informative than
re-deriving it silently each render.

## Nit 2 — Stale reset times can lie

If `resets_at` is in the past and no fresh fetch has succeeded, the UI
must not present the old window as current. The design should state the
rule: a window whose reset time has passed and whose data is older than
the max-age is shown as stale/unknown, never as a confidently rendered
percentage with an expired reset clock.

## Nit 3 — Tests cover detection, not debuggability

`test_provider_auth.py` is thorough on detection precedence, flow
state machines, and never returning token values. There is no test for
anything this lens cares about: no test that each failure maps to a
distinct reason code, no test that a failed fetch preserves the last
good value, no test that a hung subprocess is killed and the state
recovers, no test that log output contains no token or account
identifier, no test that `get()` never blocks on a slow provider. The
design doc should carry an observability test list alongside the feature
tests, reusing the existing fake-process and fake-home patterns.

---

## What the design doc must contain before implementation

1. The failure taxonomy (Blocker 1) as a table: reason code, provider(s),
   UI wording, next step, log fields.
2. The logging contract (Blocker 2): event fields, redaction rules, and
   the two traps (exception text, cache contents).
3. The usage state machine (Blocker 3): `never` / `collecting` /
   `ready` / `stale` / `failed`, with `fetched_at`, max-age, and
   restart persistence — reusing the `ServerStatsCache` pattern rather
   than inventing a new one.
4. The budgets (Blocker 4): per-provider timeouts, poll cadence in
   minutes, `Retry-After` handling, consecutive-failure circuit
   breaker, and the explicit rule that render and the live poll never
   trigger or wait on a fetch.
5. The Codex subprocess lifecycle (Major 1) and response validation
   rules (Major 2), including the per-stage reason codes.
6. The operational surface (Major 3): timestamps, manual retry, and
   per-provider health in the live poll payload.
