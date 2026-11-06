# Goldfish Lens — COST, QUOTA & TOKEN ECONOMICS

Issue #396: auto-import Claude Code / Codex CLI logins + show remaining usage
(5-hour session window, weekly window) in Connections → Providers.

Reviewer lens: what does a usage refresh cost, what quota does it burn, and
what breaks at scale. Read against the concepts brief dated 2026-11-06 and
the repo at `baton-issue396` (main 61c5000).

## The one fact that decides most of this

`conn.js` is loaded on **every** dashboard page (`templates/base.html:20`)
and polls `/connections/live` every **15 seconds** while the tab is visible.
That is 240 polls per hour per open tab. Today each poll is cheap: it serves
cached server reachability and re-runs file/env detection (`cli_providers`
reads two small JSON files and checks env vars — no network, no subprocess).

The moment usage data touches either that poll or the page render, the cost
profile changes completely. The brief never names a refresh trigger, never
names a cadence number, and never says the usage refresh must be decoupled
from the 15-second poll. That omission is the central problem: the most
natural implementation (add usage to `connections_live`, or fetch it inside
`views.cli_providers`) is also the most expensive one, and nothing in the
brief rules it out.

---

## Blocker 1 — The 15-second live poll will eat the Claude quota bucket if usage rides on it

**Severity: Blocker**

The brief states the Claude usage endpoint rate-limits itself, roughly one
request in three succeeding under an active session, with HTTP 429 and
`Retry-After`. It also says Baton must "cache and poll sparsely (minutes,
not seconds)" — but gives no number, no trigger, and no enforcement point.

Do the arithmetic on the naive wiring:

- Usage refresh piggybacked on `/connections/live`: 240 requests/hour per
  open tab. With two tabs open (common: Connections + Fleet/Chat), 480/hour.
- At a 1-in-3 success rate under an active session, that is ~160 failed
  requests/hour/tab — 320/hour with two tabs — against a bucket the CLI
  itself shares.
- The dashboard is long-running. An 8-hour workday with one tab open is
  ~1,920 Claude usage requests, ~1,280 of them 429s, for a number that
  changes slowly (a 5-hour window and a weekly window).

Worse, the 429s are not free. Each one is a TLS connection, a request, and
a response against Anthropic's API, and each one consumes the same per-token
bucket the `claude` CLI uses for its own `/usage` screen. Baton polling
aggressively while the user is actively working in Claude Code — exactly
when the bucket is hottest — can make the CLI's own usage display fail.
The feature would then degrade the tool it is reporting on.

**What the design must state (non-negotiable):**

1. Usage refresh is a separate background job, decoupled from
   `/connections/live` and from page renders. The live poll serves cached
   usage only; it never triggers a fetch. Same for `connections_page` —
   rendering the page never triggers a fetch.
2. A named minimum interval per provider (see Blocker 3 for numbers).
3. `Retry-After` is honored on every 429, and consecutive 429s back off
   (see Blocker 4).
4. No refresh at all while the provider's auth is `api_key`: API-key auth
   has no subscription windows, so any usage call for it is pure waste —
   and a 401 against an OAuth endpoint with an API key is a pointless
   failure to log and retry.

## Blocker 2 — Baton and the CLI share one per-token bucket, and the CLI loses

**Severity: Blocker**

The brief notes the endpoint is a "shared bucket with the CLI's own
polling" but treats it as a Baton-side inconvenience (Baton gets 429s).
It is a two-sided problem:

- The CLI polls the same endpoint for its own `/usage` screen, and it
  polls while the user is working. Baton's background refresh competes
  directly with those polls.
- If Anthropic ever tightens the bucket (it is undocumented, so it can
  change without notice), the first symptom the user sees may be the CLI's
  own usage screen failing — caused by Baton, blamed on Claude Code.
- There is no way for Baton to know whether the CLI is currently polling,
  so Baton cannot politely step aside. The only safe posture is to poll
  rarely enough that contention is unlikely to matter.

**What the design must state:**

1. A maximum request budget, not just a minimum interval — e.g. Claude:
   no more than ~6 requests/hour under normal operation (10-minute floor),
   fewer under 429 pressure.
2. No polling while the dashboard has no recent viewer: a refresh job that
   runs 24/7 on a long-running dashboard burns quota all night for a page
   nobody is looking at. Refresh only when the Connections page (or any
   page showing usage) was viewed recently, or on explicit user action
   ("Refresh" button), with a TTL on the cached value.
3. The design must name the failure mode honestly in the UI: when the
   cached value is old because of 429s, show "as of HH:MM" and the reason
   ("Claude is rate-limiting usage checks right now"), never a fresh-looking
   number. A stale "72% left" that the user trusts to start a long run is
   worse than no number at all.

## Blocker 3 — No cadence numbers exist; here are the only defensible ones

**Severity: Blocker**

"Minutes, not seconds" is not a cadence. The implementer will pick a
number, and the wrong pick is either quota burn or a permanently stale bar.
The design must pin these down, with reasoning:

- **Claude: 10–15 minute minimum interval between attempts.** Rationale:
  the 5-hour window's utilization moves slowly (a few percent per 15
  minutes even in heavy use); the weekly window barely moves. At 15
  minutes that is 4 requests/hour, 96/day — and with a 1-in-3 success rate
  under an active session, a fresh reading roughly every 15–45 minutes,
  which matches how fast the underlying number actually changes. Anything
  under 10 minutes buys no information and spends quota.
- **Codex: 5-minute minimum interval, or refresh-on-view only.**
  Rationale: the cost here is not server quota (the app-server is local,
  no network call by Baton) but process spawn (below). 5 minutes is 12
  spawns/hour — negligible — and the 5-hour window is the fast-moving one.
  But there is no reason to poll Codex while nobody is looking at the
  page either.
- **Both windows come in one response, so one cadence governs both.**
  The design must not propose separate refresh timers for the 5-hour and
  weekly windows — that would double the requests for no benefit.
- **A manual "Refresh" button must exist and must itself be rate-limited**
  (e.g. ignored if the last attempt was < 60 seconds ago for Codex,
  < 5 minutes for Claude). Without that, a user hammering Refresh during a
  429 storm is a self-inflicted quota burn, and the dashboard will happily
  oblige on every click.

## Blocker 4 — No backoff or circuit-breaker rules are specified

**Severity: Blocker**

The brief says "show staleness honestly" but never defines what Baton does
after a 429. An implementer left to choose will either retry immediately
(the worst option) or retry on the next fixed interval (still bad: a fixed
10-minute retry during a 429 storm is 6 wasted requests/hour indefinitely).

**What the design must state:**

1. Honor `Retry-After` exactly when present; treat its absence as "wait
   at least the base interval."
2. Exponential backoff on consecutive failures: e.g. 10 min → 20 min →
   40 min → 60 min cap. Backoff is per provider, stored in the cache
   record (`next_retry_at`), and survives dashboard restarts (it must live
   in the cache file/DB, not in process memory, or a restart resets the
   backoff to zero — see Blocker 7).
3. A circuit breaker: after N consecutive failures (suggest 3), stop
   attempting entirely for a fixed quiet period (suggest 1 hour), then one
   probe attempt. A dashboard that keeps knocking on a 429ing endpoint for
   a whole weekend is burning quota and log volume for nothing.
4. 401/403 (token rejected) must **not** be retried on the usage interval
   at all — it is not a transient failure, it means the token kind is
   wrong or the login is dead. Retry-once-then-stop, surface "re-check
   your login", and do not resume until auth state changes. This matters
   most for the unverified `sk-ant-oat01-` setup token: if the endpoint
   rejects it, Baton must record that outcome once and never try that
   token kind again per login — not once per refresh cycle forever.

## Major 1 — The Codex spawn is cheap per call, expensive per page load

**Severity: Major**

The brief says the app-server spawn is "cheap but still a subprocess per
refresh" and prescribes `cwd` a temp dir, scrubbed env, 15 s timeout,
SIGTERM after the answer, 4 MiB output cap. That is the right shape, but
the cost analysis stops there:

- **Per-spawn cost:** a process fork+exec, a Node/Rust runtime start, a
  JSON-RPC `initialize` handshake, then the `account/rateLimits/read` call.
  Realistically 100–500 ms of wall time and a few tens of MB of transient
  RSS per spawn. Trivial once. But if the refresh is wired to page loads
  (every Connections render) or to the 15-second poll, that is 240 spawns an
  hour — 240 process starts, 240 handshakes, and 240 chances for something
  to wedge.
- **Orphan risk on a long-running dashboard:** the 15 s timeout plus
  SIGTERM is the happy path. If SIGTERM is ignored or the process is stuck
  in a pipe read, the dashboard accumulates zombie/orphaned `codex`
  processes across a days-long run. The design must state a SIGKILL
  escalation (e.g. SIGTERM, then SIGKILL after 5 s) and a reaper: no more
  than one app-server alive per provider at a time, with any previous one
  killed before a new spawn. The existing `CodexDeviceFlow`/`ClaudeSetupTokenFlow`
  code in `provider_auth.py` already shows the pattern (single-flight via
  lock + state, terminate on cancel) — the usage refresher needs the same
  single-flight discipline, and the brief never says so.
- **Concurrency:** two browser tabs do not cause two server-side refreshes
  if (and only if) the refresher is a single server-side job with a
  "collecting" guard. The established precedent is `ServerStatsCache` in
  `servers/stats.py`: `refresh()` returns False when a collection is
  already running, `get()` never blocks, results persist as JSON under the
  state dir. The design should reuse that pattern (or justify not doing
  so) rather than inventing a new one. Without single-flight, every
  concurrent trigger doubles the spawn rate.
- **Free alternative the brief underuses:** the CLI already writes a
  `rate_limits` snapshot into `~/.codex/sessions` files. Reading a file is
  free — no spawn, no handshake. The design should consider: read the
  snapshot file first; spawn `app-server` only when the snapshot is older
  than the TTL or missing. That turns most refreshes into a file read and
  reserves the spawn for when the data is actually stale. (Caveat: the
  snapshot's freshness semantics are unverified — the design must verify
  whether the CLI updates it on its own polling cadence or only when the
  TUI is open. If it only updates while the TUI runs, it is not a usable
  fallback and the design must say so.)

## Major 2 — The cache must be host-level, not repo-level, or costs multiply by repo count

**Severity: Major**

The dashboard is per-repo: `connections_page` opens the repo, and the
existing caches (`ServerStatsCache`) live under the repo's `state_dir`.
Usage, however, is per **account on the host device**, not per repo. The
brief's open decision (1) asks "state.db vs a JSON cache file" but never
asks *whose* state dir.

If the usage cache lives under the repo state dir:

- A user with 5 repos open in 5 dashboard instances gets 5 independent
  caches and 5 independent refreshers — 5× the Claude requests against
  the same per-token bucket, 5× the Codex spawns, all for the same two
  numbers.
- Worse, each instance's backoff state is separate, so a 429 storm that
  should quiet one refresher instead gets five refreshers each backing
  off independently and each probing again.

**What the design must state:**

1. One cache per provider account, keyed by host + account identity (or
   just host + provider, since there is one CLI login per provider per
   device), stored at user level (`~/.jev/`, alongside `secrets.env`),
   not under any repo's state dir.
2. Cross-instance single-flight: if two dashboard processes run, the
   cache file's `next_retry_at` / "refresh in progress" marker must be
   visible to both, so the second instance serves the cached value
   instead of starting its own refresh. A cheap way: the cache file
   records `fetch_started_at`, and a second process that sees a fresh
   `fetch_started_at` (e.g. < 60 s old) does not spawn its own fetch.
   A file lock is the correct way; a timestamp check is the minimum.
3. The cache record must contain, at minimum: `fetched_at`,
   `next_retry_at`, `consecutive_failures`, `last_status` (ok / 429 /
   auth-error / spawn-error), and the usage payload. Without
   `consecutive_failures` and `next_retry_at` persisted, a dashboard
   restart resets all backoff — see Blocker 7.

## Major 3 — The Claude token-refresh path doubles the request count and risks a credentials-file race

**Severity: Major**

The brief notes the access token expires and is renewed against
`console.anthropic.com/v1/oauth/token` with the refresh token from
`~/.claude/.credentials.json`. Two costs follow that the brief does not
price:

1. **Every usage refresh can become two network calls** (token refresh,
   then usage). If the access token is short-lived and Baton refreshes
   lazily on each attempt, the effective Claude request rate doubles —
   and the token endpoint has its own rate limits. The design must state:
   check `expiresAt` before calling; only refresh when the token is
   expired or expiring within a margin (e.g. 5 minutes); cache the
   refreshed access token in memory for the dashboard process lifetime
   (never log it, never render it).
2. **Two writers to one credentials file.** The `claude` CLI refreshes its
   own token when it runs. If Baton also writes a refreshed token back to
   `~/.claude/.credentials.json`, the two writers can interleave and
   corrupt or clobber the file — destroying the user's CLI login is a
   far worse outcome than a stale usage bar. The design must state
   explicitly: **Baton never writes to `~/.claude/.credentials.json`**.
   A refreshed token lives in dashboard process memory only (and is
   re-read from the file the CLI maintains on the next refresh). If the
   design wants persistence, it must use a separate Baton-owned file and
   justify why the CLI-maintained file is not sufficient.
3. **macOS Keychain path:** on macOS the credentials may live in the
   Keychain (`Claude Code-credentials` service) instead of the file.
   Reading the Keychain from a background refresher can trigger a macOS
   access prompt or fail silently when the dashboard runs without a GUI
   session. The design must state what happens then (fail the refresh,
   show "usage unavailable — Keychain access needed", do not block) and
   must not propose a `security` CLI call that pops a prompt on every
   refresh cycle.

## Major 4 — Dashboard latency: usage fetching must never be on the render path

**Severity: Major**

Today the Connections page render is fast because everything it reads is
local and cached: `connections_page` calls `views.cli_providers`, which
reads env vars and two small files. The live endpoint likewise serves
cached state ("a GET never opens an SSH connection").

A usage fetch on either path breaks that contract:

- Claude: a network round-trip to `api.anthropic.com` plus a possible
  token refresh — hundreds of milliseconds to seconds, and unbounded if
  the endpoint hangs (a timeout must be named; suggest 10 s, not the
  15 s used for the local Codex spawn — a network call deserves a
  shorter leash than a local process).
- Codex: a subprocess spawn plus handshake — 100–500 ms even when healthy,
  15 s when wedged.

Both would hold a dashboard worker thread and delay the page (and, via
`/connections/live`, the sidebar slot on *every* page). The brief says
"refreshed in the background" but never states the render-path rule.

**What the design must state:**

1. `views.cli_providers` and `connections_live` serve cached usage only
   and never start a fetch. Rendering never blocks on network or
   subprocess. This is the same rule the server-stats feature already
   follows (`ServerStatsCache.get` never blocks; `refresh` is explicit
   and backgrounded) — the design should cite it as the pattern.
2. The background refresher runs on daemon threads with a named timeout
   per provider, and a failure (timeout, 429, spawn error) records a
   failed state and never raises into the dashboard process.
3. The template renders a staleness label from `fetched_at` on every
   usage number ("updated 12 min ago"), so a slow or failed background
   refresh is visible rather than silently presenting old data as current.

## Major 5 — Long-running dashboard + no idle stop = quota burned while nobody watches

**Severity: Major**

The dashboard is a long-running process (it "does not survive a Mac
reboot" precisely because it is expected to run for days). A background
refresher with a fixed interval and no idle detection will:

- Poll Claude 4–6 times/hour, 24 hours/day, ~100–144 requests/day, even
  when the user is asleep, on holiday, or simply not looking at
  Connections. At a 1-in-3 success rate that is ~70–96 wasted 429s a day.
- Spawn `codex app-server` 12 times/hour around the clock — ~288 spawns
  a day — for a page nobody has open.

**What the design must state:**

1. An idle rule: the refresher only runs while the usage UI was viewed
   recently (e.g. Connections page loaded in the last 30 minutes, or the
   live poll is active — the poll itself proves a tab is open), plus one
   fetch on explicit page view if the cache is older than the TTL.
2. A quiet-hours behaviour or at minimum a statement that the dashboard
   does not need usage data while idle. "Refresh on view + TTL" is
   cheaper than "refresh on a timer" and is the right default for data
   that changes this slowly.
3. Dashboard shutdown must stop the refresher threads (the existing flows
   already have `shutdown()`; the usage refresher needs one too, and the
   brief never mentions lifecycle).

## Minor 1 — "Remaining" for API-key auth is a category error, not just a missing feature

**Severity: Nit (cost-relevant)**

API-key auth (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) is billed per token
with no subscription windows. There is nothing to show, and any attempt to
call the usage endpoints with an API key is a wasted request that returns
an error. The design must state that the usage section is hidden (not
"unavailable") for `api_key` auth, with a one-line explanation. Showing
"usage unavailable" for API-key users invites support questions and retry
clicks; hiding it costs nothing.

## Minor 2 — The setup-token question has a cost answer, not just a correctness answer

**Severity: Nit**

Open decision (6) asks whether the `sk-ant-oat01-` setup token is tried
against the usage endpoint. From a cost view the answer is: try it **once**
per token, record the outcome, and never retry a rejected token kind on
the usage interval. A design that tries it on every refresh cycle turns
an unverified guess into a permanent stream of 401s. If it is accepted,
it is the cheapest path (a long-lived token needs no refresh round-trip);
if rejected, the cost of discovering that must be paid exactly once.

## Minor 3 — No usage history is out of scope, and that bounds the storage cost — keep it that way

**Severity: Nit**

The non-goals correctly exclude usage history. That matters for cost: a
latest-snapshot cache is one small JSON file per provider, written a few
times an hour. A history would be an ever-growing series in `state.db`
with retention and pruning to design. The design must not let a "show the
trend" idea creep back in through the cache schema — store one snapshot,
overwrite in place.

## Minor 4 — Token economics of the usage calls themselves: free, but not free

**Severity: Nit**

For the record: neither usage call bills tokens. The Claude endpoint is a
metadata read, and the Codex call is local stdio. The real currency is
rate-limit budget (Claude) and process-spawn budget (Codex), plus the
indirect cost of degrading the CLI's own usage display. The design doc
should say this in one sentence so nobody later "optimises" by assuming
the calls are free and cranking the cadence up.

---

## Required design statements (checklist for the design doc)

The design doc must contain explicit, numbered answers to all of these.
Any one left open hands the implementer a decision that directly sets the
quota burn rate:

1. Refresh trigger: background job only, decoupled from `/connections/live`
   (15 s) and from page renders; plus an explicit manual Refresh with its
   own minimum gap.
2. Cadence numbers: Claude 10–15 min minimum interval; Codex 5 min
   minimum or refresh-on-view; one timer for both windows (they arrive in
   a single response).
3. Idle rule: no refresh while the usage UI has not been viewed recently;
   refresher lifecycle tied to dashboard start/stop.
4. Backoff: honor `Retry-After`; exponential backoff to a 60 min cap;
   circuit breaker after 3 consecutive failures (1-hour quiet period,
   then one probe).
5. 401/403 handling: retry once, then stop until auth state changes;
   record a rejected token kind once, never retry per cycle.
6. Cache location: host/user level (`~/.jev/`), not per-repo state dir;
   cross-instance visibility of `next_retry_at` / in-progress state.
7. Cache record contents: `fetched_at`, `next_retry_at`,
   `consecutive_failures`, `last_status`, payload. All persisted, so a
   dashboard restart does not reset backoff or re-fetch immediately.
8. Single-flight: one refresh per provider at a time, reusing the
   `ServerStatsCache` "collecting" pattern; Codex SIGTERM → SIGKILL
   escalation and a one-app-server-at-a-time rule.
9. Render-path rule: `cli_providers` and `connections_live` serve cached
   usage only, never fetch, never block; named timeouts (Claude 10 s,
   Codex 15 s spawn).
10. Claude token handling: check `expiresAt`, refresh only when needed,
    never write to `~/.claude/.credentials.json`, Keychain failure is a
    failed refresh, not a prompt loop.
11. API-key auth: usage section hidden, no usage calls attempted.
12. Staleness UI: every usage number carries its `fetched_at` ("updated
    N min ago") and a failure reason when the last attempt failed.

## What the brief gets right (briefly)

- It correctly identifies the asymmetry: Codex's source is local and safe,
  Claude's is an undocumented server endpoint that can 429 or change. The
  design must keep treating them differently — the cadence, backoff, and
  failure rules above are different per provider for exactly that reason.
- "Remaining is a server-side number" is the right line; nothing computed
  from local session files may be presented as remaining.
- Ruling out PTY-driving `/usage` and the web dashboard as sources avoids
  two expensive dead ends.
- The open decisions list is honest about what is undecided. The problem
  is only that the undecided items include the numbers that determine the
  entire cost profile — cadence, trigger, backoff, and cache location —
  and the brief's analysis section gestures at them ("minutes, not
  seconds") without pinning any of them down.
