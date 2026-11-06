# Design Critic Review — cli-autoimport-usage (Stage 7)

* **Scope reviewed:** `docs/designs/cli-autoimport-usage/design.md`, lines 1–697
  (Overview through §8; §9 implementation plan and §10 verification plan are
  outside this range and were not reviewed, except for their headings).
* **Standard:** `feature-spec-standard.md`
* **Lens:** design critic — decisions, trade-offs, what breaks first in production.
* **Grounding:** claims about existing code were checked read-only against the
  checkout (`provider_auth.py` detection precedence, `ServerStatsCache` in
  `servers/stats.py`, `conn.js` `POLL_MS = 15000`, `views.cli_providers`,
  `docs/config.md`). No project file was edited.

## Verdict

The document is well structured and honest about its two hardest facts — the
Claude endpoint is undocumented and rate-limited, and the Codex success shape
has never been observed. That honesty does not save it. Four blockers sit
underneath the design, and the feature's headline promise (a fresh remaining
allowance before a long run) fails on the design's own numbers for Claude.
The standard is met in form — problem, goals, architecture, alternatives,
risks are all present — but not in substance: one decisive alternative is
never evaluated, several risks are named but not actually mitigated, and
there are no success metrics inside the reviewed range. Not ready to
implement as written; resolve B1–B4 first.

## What breaks first in production (in order)

1. **The Claude card is stale or unavailable most of the time the user is
   actually working.** 15-minute cadence × ~1-in-3 success × backoff that
   stretches after routine 429s means a fresh number is the exception, not
   the rule (B4).
2. **Mac verification of the Codex success shape fails**, and the single PR
   plus shared merge gate blocks the Claude half too (M2, M12).
3. **A token refresh damages the user's CLI login** — the exact outcome D8
   claims to prevent (B1).
4. **Two dashboard processes overwrite each other's cache entry** in the
   shared JSON file (M5).
5. **A transient 401 wedges usage permanently** with no defined way back
   (B2).

## Blockers

### B1 — Token refresh can destroy the CLI login it claims to protect
* **Severity:** Blocker — **Section:** §5 D8, §6.3, §6.7
* **Finding:** D8 refreshes the CLI's OAuth token in memory and never
  writes the new token back, on the stated grounds that two writers could
  corrupt the credentials file. The design never addresses refresh-token
  rotation: when an OAuth provider rotates the refresh token on use, the
  old refresh token is invalidated and a new one is issued. If Baton
  consumes the CLI's refresh token and keeps the replacement only in
  process memory, the CLI's stored refresh token is now dead — the next
  time the CLI tries to refresh, it fails, and the user's login is broken
  until they sign in again. The design also never addresses the concurrent
  case: the CLI refreshes at the same moment Baton does, one of them wins,
  the other gets `invalid_grant`. The central safety claim of D8
  ("destroying the login is a far worse outcome than a stale usage bar")
  is therefore unproven and possibly backwards — the refresh path itself
  may be what destroys the login. The design cites no evidence about
  whether Anthropic rotates refresh tokens, what `expiresAt` is called in
  the credentials file, or what happens on a failed refresh beyond the
  reason code.
* **Recommendation:** Do not ship a refresh path until the rotation
  behaviour is established from evidence (observed token response, or a
  cited source). The safe default is the opposite of D8: never refresh at
  all — treat an expired token as `token_expired_refresh_failed` and let
  the CLI, which owns the file, do the refresh. If refresh is kept, the
  design must specify the rotation semantics, the concurrent-refresh
  outcome, and a test that proves the CLI login still works after Baton
  refreshes.

### B2 — The 401/403 stop has no way back
* **Severity:** Blocker — **Section:** §5 D7, §6.4, §6.5
* **Finding:** After a 401/403 the adapter retries once, "then stops until
  auth state changes". Nothing in the design defines what counts as an
  auth-state change, how it is detected, or what re-arms the fetcher.
  Detection re-runs every 15 seconds, so if "change" means "detection
  output changed", a re-sign-in with the same account may not register as
  a change at all. Worse, D5 persists `next_retry_at` and
  `consecutive_failures` in the cache file precisely so a dashboard
  restart does not reset backoff — so a restart does not re-arm it either.
  A transient 401 (for example a token mid-rotation, which B1 makes
  likely) can wedge usage off permanently, with the UI showing a stale
  reason and no path back except, presumably, hand-deleting the cache
  file — which the design calls disposable but never presents as a user
  action.
* **Recommendation:** Define the re-arm event concretely (fingerprint
  change, detection transition, manual Refresh click, or a bounded quiet
  period after which probing resumes — pick one and state it), and state
  whether the manual Refresh button overrides the stopped state. The
  "stop until" state needs an expiry, or it is a permanent off-switch
  wearing a temporary name.

### B3 — The login fingerprint is undefined, and both possible definitions break something
* **Severity:** Blocker — **Section:** §5 D5, §6.7; conflicts with §5 D8, §5 D13
* **Finding:** The cache record carries a `fingerprint` ("source path +
  account hint, never a token value") and "a fingerprint change
  invalidates the cached usage". No section defines the fingerprint's
  inputs, and the two candidate definitions each break a stated
  requirement. If the fingerprint includes token material (even a hash),
  then every CLI-side token refresh changes the fingerprint and wipes the
  usage cache — the cache is invalidated on exactly the routine event
  that should not invalidate it. If the fingerprint is only the file
  path, then switching accounts in the CLI (same path, different token)
  does not change the fingerprint, and the previous account's usage is
  shown as the current account's — a wrong number presented as fresh.
  D13 makes this worse: it states Claude's response carries no account
  identity and none is inferred — so for Claude there is no "account
  hint" available from any source the design admits. The fingerprint is
  doing load-bearing work (cache invalidation, re-arm for B2, provenance
  for D2) with no specified contents.
* **Recommendation:** Specify the exact fingerprint inputs per provider,
  state where each input comes from, and handle both events explicitly:
  token rotation must not invalidate, account switch must invalidate.
  If no account-identifying input exists for Claude, say so and accept
  the consequence (stale-account risk) as a documented limitation instead
  of implying the fingerprint covers it.

### B4 — The design's own numbers say the Claude card will usually be stale
* **Severity:** Blocker — **Section:** §5 D6, §5 D7, §6.4
* **Finding:** D6 sets a 15-minute Claude cadence. The design states
  roughly one request in three succeeds under an active session. Expected
  time to one successful refresh is therefore ~45 minutes. D7 then
  applies exponential backoff (15 → 30 → 60 minutes, capped at 60) on
  consecutive failures — but under the design's own success rate, two
  consecutive failures happen about 4 times in 9, so backoff is the
  normal path, not the exception. After two failures the next attempt is
  an hour away, while §6.4 declares a snapshot stale after 60 minutes.
  Put together: during exactly the heavy-use sessions when the user most
  wants the number, the card will spend most of its time stale or
  unavailable, and the backoff schedule guarantees it stays that way.
  D6 and D7 contradict each other — D6 assumes failures are rare enough
  that a 15-minute cadence yields fresh numbers, D7 assumes failures are
  exceptional enough to punish with an hour-long wait, and the stated
  1-in-3 success rate says failures are routine. The design never
  computes expected freshness, so this contradiction is never noticed.
* **Recommendation:** Compute expected freshness from the stated success
  rate and the backoff schedule, and write the result into the design.
  Treat 429 as a routine outcome with its own schedule (short retry,
  honour `Retry-After`, no consecutive-failure counting for 429s) instead
  of feeding it into the failure backoff. If the computed freshness is
  still poor, say plainly that the Claude half is best-effort and usually
  stale — or drop the background refresher for Claude and fetch on demand
  (see M1).

## Major findings

### M1 — The decisive alternative is never evaluated: fetch on demand only, no background refresher
* **Severity:** Major — **Section:** §7 (missing); §5 D6, §6.4
* **Finding:** The design builds a background refresher with per-provider
  cadence, idle rule, manual refresh, exponential backoff, circuit
  breaker, single-flight, and cross-process coordination. None of that
  machinery is justified against the simplest design: fetch only when the
  user opens the Connections page or clicks Refresh, and show nothing
  otherwise. The user's stated need is a number before a long run — a
  pull, not a push. Windows move slowly by the design's own claim, so
  there is nothing a background timer learns that an on-view fetch does
  not. On-demand fetching deletes the 429-budget problem (no polling
  while the user works in the CLI), the idle rule, the cross-process
  race, and most of D7. §7 lists ten rejected alternatives; this one is
  not among them. The standard requires rejected options to be documented
  with reasons; the option that would delete a whole subsystem is absent.
* **Recommendation:** Add the on-demand-only alternative to §7 with an
  honest evaluation. If background refresh is kept, the rationale must
  name a concrete user need it serves that on-view fetching does not —
  "the number is already there when the page opens" is a latency saving
  of one fetch, not a reason for a scheduler.

### M2 — "Two independent adapters" is contradicted by one PR and one merge gate
* **Severity:** Major — **Section:** §5 D4, §6.9, §4
* **Finding:** D4 claims the adapters are independent and each is
  "verifiable on its own". §6.9 then ships both in one PR, and §4 makes
  Mac verification of the Codex success shape a merge gate. A failed
  Codex verification therefore blocks the Claude half, and a broken
  Claude adapter ships enabled (kill switches default true) in the same
  PR as a working Codex one. The independence exists only at the unit-test
  level; at the review, merge, and rollout level the two are coupled.
  The kill switch is presented as the decoupling mechanism, but it is a
  runtime off-switch, not a merge-time one — it cannot unblock a PR.
* **Recommendation:** Either split into two PRs (Codex first, Claude
  second) so the merge gate applies per adapter, or state plainly that
  the two ship together and the Codex gate blocks both, and drop the
  independence claim.

### M3 — The design shows the stale number it says is worse than no number
* **Severity:** Major — **Section:** §4, §6.5
* **Finding:** §4 states the principle clearly: "A stale '72% left' the
  user trusts to start a long run is worse than no number". §6.5 then
  specifies the "Detected + stale" state as "same line, labelled
  'stale — as of HH:MM'". The design violates its own principle: it shows
  the number anyway and relies on a small label to undo the trust the
  number creates. A label does not change the fact that the user reads
  "72% left" first and the qualifier second, if at all.
* **Recommendation:** Pick one side. Either hide the value when stale
  (show only "last seen at HH:MM — check again before a long run") or
  delete the §4 principle and defend showing stale numbers. The current
  text claims the principle and breaks it in the same document.

### M4 — Dropping the Opus/Sonnet splits can show the wrong binding limit
* **Severity:** Major — **Section:** §6.3, §7
* **Finding:** Claude's response carries `seven_day_opus` and
  `seven_day_sonnet` splits plus a `limits[]` array. The design discards
  all of them as "extra numbers with no decision value". That is asserted,
  not shown. For a user whose work is mostly Opus, the Opus split may be
  the binding limit: the headline weekly number can read "81% left"
  while the user is actually blocked on Opus. The design's headline
  number would then be the wrong answer to the exact question the feature
  exists to answer ("do I have enough left for this run?"). D13's rule —
  show only what a source returns — is not a defence here, since the
  source does return the splits.
* **Recommendation:** Either show the binding split (the lowest remaining
  of the windows that apply) or establish from evidence that the splits
  never bind before the headline windows do. "No decision value" needs a
  reason, not a sentence.

### M5 — Two dashboard processes can lose each other's snapshot in the shared cache file
* **Severity:** Major — **Section:** §6.4, §5 D5
* **Finding:** The cache is one JSON file holding both providers'
  snapshots, written atomically (temp + rename). Atomic rename prevents a
  torn file; it does not prevent a lost update. Two dashboard processes
  that both read, update different providers, and write will silently
  discard one process's update — the classic read-modify-write race.
  §6.4 admits "a file lock is the correct mechanism; a timestamp check
  is the minimum", then ships the minimum: a `fetch_started_at`
  timestamp that only prevents duplicate fetches, not concurrent writes.
  The design also never states how many dashboard processes can run on
  one host, or whether the dashboard is single-process in practice — the
  cross-instance paragraph assumes the problem exists, then does not solve
  it.
* **Recommendation:** Either use the file lock (the design already names
  it as correct), split the cache into one file per provider so writes do
  not overlap, or establish from evidence that only one dashboard process
  ever runs and delete the cross-instance paragraph. Shipping the admitted
  minimum for a known race is a decision that needs a rationale, not a
  sentence.

### M6 — The "never hardcoded" window mapping is hardcoded, and unmapped durations have no behaviour
* **Severity:** Major — **Section:** §6.1, §6.3
* **Finding:** §6.1 promises windows are "derived from the response,
  never hardcoded". §6.3 then maps Codex windows by exact minutes —
  300 → session, 10080 → weekly. That is a hardcoded mapping with magic
  numbers, and it has no specified behaviour for anything else: a third
  window, a changed duration, a 60-minute window. Does an unmapped
  window produce `unexpected_response`, get dropped silently, or render
  with a raw minute count? The same applies on the Claude side, where
  `five_hour` / `seven_day` are hardcoded response keys by necessity.
  The "never hardcoded" rule is stated absolutely and broken by the
  design's own mapping.
* **Recommendation:** Restate the rule honestly (provider response keys
  live in the adapter; the window-kind mapping is a table in the adapter
  with an explicit default for unknown durations), and specify what
  happens to an unmapped window.

### M7 — The failure taxonomy is Claude-shaped, and it breaks the design's own no-leak rule
* **Severity:** Major — **Section:** §6.5, §6.1, §6.9
* **Finding:** Three defects. First, the table gives one wording per
  reason code, but `network_error` is worded "Couldn't reach Anthropic"
  — meaningless for Codex, whose fetch is a local spawn with no network.
  Second, the sunset rule (§6.9) triggers on 404/410, but the taxonomy
  has no 404/410 code — it is unclear whether that maps to
  `unexpected_response`, `unauthorized`, or nothing, so the sunset rule
  cannot be executed as written. Third, §6.1 forbids any
  provider-specific string outside the adapter, yet §6.5's own table
  contains "Claude rejected this sign-in", "Couldn't reach Anthropic",
  and "Sign in again in Claude Code" — the design violates its own
  architectural rule in the section that states the wordings.
* **Recommendation:** Make the taxonomy per-provider (reason code +
  provider → wording), add a code for 404/410, and either drop the
  absolute no-leak rule or move the wording table into the adapters where
  the rule says it belongs.

### M8 — The kill switch invents config with no config design
* **Severity:** Major — **Section:** §6.1, §6.9
* **Finding:** The kill switch is `[usage] claude = false` / `[usage]
  codex = false` in `jev.toml`, "documented in `docs/config.md`". The
  design never says where `JevConfig` parses a new top-level section,
  whether unknown sections fail validation today (many config loaders
  reject them), what the default is when the section is absent, or
  whether flipping the switch needs a dashboard restart. "No code change,
  no redeploy" is claimed; "no restart" is not claimed and not addressed.
  If the switch only takes effect on restart, the rollback story is
  weaker than stated, and the operator has no way to know.
* **Recommendation:** Specify the config-schema change, the behaviour for
  a missing section, and whether the switch is picked up live or on
  restart. Verify that an unknown `[usage]` section does not break config
  loading today.

### M9 — The idle rule cannot be evaluated because "viewed" is undefined
* **Severity:** Major — **Section:** §5 D6, §6.4
* **Finding:** "No background refresh while the usage UI has not been
  viewed in the last 30 minutes." The live poll hits `connections_live`
  every 15 seconds from every open tab — is a poll a view? If yes, the
  idle rule never triggers while any tab is open, and the rule is dead
  text. If no, the design describes no mechanism that records views
  (page render? tab focus? a separate endpoint?) and no store for the
  last-viewed timestamp. Either the rule does nothing or it depends on
  machinery that is not designed.
* **Recommendation:** Define "viewed" operationally (for example, a page
  render or an explicit poll parameter) and state where the timestamp
  lives. If the poll counts as a view, delete the idle rule.

### M10 — The most important failure is undiagnosable by design
* **Severity:** Major — **Section:** §6.5, §6.8
* **Finding:** `unexpected_response` — the provider changed its response
  shape — is the failure the whole Claude half is built around, and the
  sunset rule depends on detecting it. §6.8 forbids logging verbatim
  exception text and §6.5 says it is "logged once". Nothing specifies
  what is logged instead: no redacted field list, no response-shape
  digest, no sample of which expected field was missing. The maintainer
  is told the shape changed and given no information about how. "Logged
  once" is also undefined — once per process lifetime, per day, per
  failure? Combined with backoff, the maintainer may see a single line
  and then silence.
* **Recommendation:** Specify a redacted diagnostic payload for
  `unexpected_response` (which fields were present, which expected fields
  were missing or mistyped, response top-level keys only, no values), and
  define "once" precisely.

### M11 — "Exactly once per token" has no mechanism
* **Severity:** Major — **Section:** §5 D9
* **Finding:** The `sk-ant-oat01-` setup token is tried "exactly once per
  token", with a rejection recorded so it is never retried. The design
  never says how one token is distinguished from another without storing
  token material — a hash is implied but never specified, and the cache
  record in D5 has no field for it. It also never says where the
  rejection is recorded (cache record? separate state?), or what happens
  if a 401 has a different cause (expired token, revoked token) and is
  misattributed to the token kind. A user who pastes a new setup token
  after the first was rejected may find usage permanently unavailable
  with no path back.
* **Recommendation:** Specify the tracking (hash algorithm, storage
  field, what resets it when the token changes) and the re-arm path for a
  new token. If the tracking is a token hash, reconcile it with the
  "never a token value" cache rule explicitly.

### M12 — The entire Codex half rests on an unverified method name with no fallback
* **Severity:** Major — **Section:** §6.3, §4
* **Finding:** The design is candid that the Codex success shape has
  never been observed — only the auth-required failure, on this VM, with
  codex 0.164.0-alpha.9. What it does not say plainly is that the
  request itself is unverified: the method name
  `account/rateLimits/read`, the initialize-then-read sequence, the stdio
  framing, and the field names all come from no cited source in the
  reviewed range. A wrong method name means every fetch fails with
  `app_server_failed` forever, and Mac verification as a merge gate only
  discovers this — it does not fix it. The only fallback, reading the
  `rate_limits` snapshot from `~/.codex/sessions`, is explicitly deferred
  to a later issue with its freshness unverified. If the method name is
  wrong, this issue delivers no Codex usage at all.
* **Recommendation:** Cite the source for the method name and framing
  before implementation, or make the sessions-snapshot read the fallback
  inside this issue (with its freshness verified first), so a wrong guess
  does not zero out the whole Codex half.

### M13 — No success metrics inside the reviewed range; key rates are unbacked
* **Severity:** Major — **Section:** §5 D6; standard compliance
* **Finding:** Nothing in lines 1–697 states what "done" means in
  measurable terms: no target for fetch success rate, freshness, or
  staleness frequency, and no acceptance threshold for either provider.
  (Success criteria may exist in §10, outside this review's range — if
  so, the design should reference them from §3 or §6.) Separately, two
  numbers load-bearing for the design are asserted without evidence:
  "roughly one request in three succeeding under an active session" and
  "a few percent per 15 minutes even in heavy use". The first comes from
  the brainstorm lenses with no citation; the second has no source at
  all. The cadence, the staleness threshold, and the freshness argument
  all rest on these two numbers.
* **Recommendation:** Add measurable acceptance criteria in the reviewed
  range (or a pointer to them), and cite or qualify the 1-in-3 and
  "few percent" figures. If the 1-in-3 figure is a guess, label it one —
  B4 shows the design's conclusions change materially if it is wrong.

### M14 — The consent note does not describe the new act
* **Severity:** Major — **Section:** §5 D16, §6.7
* **Finding:** D16's consent line says "Baton reads the sign-in your CLI
  already has. It never copies or stores it." That describes reading a
  file. The new act in this issue is different: Baton takes that sign-in
  and sends it over the network to Anthropic's usage endpoint, on a
  15-minute background schedule, with no opt-out (a dashboard opt-out is
  a named follow-up). The note does not say that. The rationale — "the
  login is the user's own on their own machine" — addresses file reading,
  not network use of someone else's credential against an undocumented
  endpoint. A user who is comfortable with Baton reading a file has not
  necessarily agreed to Baton spending their CLI's rate-limit budget on
  background calls.
* **Recommendation:** Make the note describe the network call, or build
  the opt-out in this issue rather than deferring it. At minimum, the
  always-on default (D3) needs a consent argument that covers background
  use of the token, not just reading it.

### M15 — The Codex spawn environment contradicts itself
* **Severity:** Major — **Section:** §6.3, §6.7
* **Finding:** The spawn uses a "scrubbed environment (preserve `HOME`)".
  The `codex` binary is resolved from `PATH` — if `PATH` is scrubbed, the
  spawn fails; if `PATH` is preserved, "scrubbed" is undefined, and the
  design never lists which variables are kept or dropped. The same
  paragraph caps output at 4 MiB and sets a 15-second timeout, but never
  quantifies what an app-server spawn costs the dashboard host in CPU or
  memory, or whether a 5-minute spawn cadence is cheap — the app-server
  is a long-lived CLI process, not a trivial binary. "Scrubbed" and the
  resource cost are both doing security and performance work with no
  specification.
* **Recommendation:** List the exact preserved variables, state how the
  binary is resolved, and quantify or bound the spawn cost (or measure it
  at Mac verification and record the number).

### M16 — Manual refresh breaks the budget the cadence was built to protect
* **Severity:** Major — **Section:** §5 D6
* **Finding:** D6 sets a Claude minimum of 15 minutes between attempts,
  then allows a manual Refresh when the last attempt was under 5 minutes
  ago — that is, a manual click can fire a request 5 minutes after the
  last attempt, inside the 15-minute floor. Codex is the same shape:
  5-minute minimum, manual allowed after 60 seconds. The manual path
  exists to let an impatient user hammer exactly the rate-limited bucket
  the cadence exists to protect, and with two tabs open the button exists
  twice. "Rate-limited" is doing work the numbers contradict.
* **Recommendation:** Make the manual-refresh floor equal to the cadence
  floor (or state explicitly that manual refresh is exempt and bound it
  separately, for example one manual refresh per interval).

### M17 — The bare-mode table is missing a combination
* **Severity:** Major — **Section:** §6.2, §5 D11
* **Finding:** The Claude precedence table has a row for "bare mode +
  CLI login" but no row for bare mode plus a saved token or API key. D11
  says saved tokens and API keys are unaffected because they are the
  bare-mode paths — so is usage shown against a saved token in bare mode?
  The table does not say, and the Codex table has no bare-mode rows at
  all. The section claims a precedence table "for every combination";
  that claim is false for the bare-mode combinations.
* **Recommendation:** Complete the table for bare mode × saved token and
  bare mode × API key, for both providers.

## Nits

### N1 — "As of HH:MM" has no timezone
* **Severity:** Nit — **Section:** §5 D14, §6.5
* The design bans raw ISO timestamps but never says which timezone
  "HH:MM" is in, or whether it follows the dashboard host or the user.
  State it.

### N2 — The Keychain card names the Claude path only
* **Severity:** Nit — **Section:** §5 D10
* D10 specifies the card names `~/.claude/.credentials.json` when nothing
  is found. The Codex equivalent (`~/.codex/auth.json`) is not specified.
  State both.

### N3 — Kill-switch re-enable behaviour is unspecified
* **Severity:** Nit — **Section:** §6.9
* Disabling then re-enabling a source: does the persisted backoff and
  `consecutive_failures` reset, or does the source come back still in its
  quiet period? State it.

### N4 — Shutdown can stall on the SIGKILL grace
* **Severity:** Nit — **Section:** §6.4
* `shutdown()` joins threads and terminates a live app-server with a
  5-second SIGKILL grace. The design never bounds dashboard shutdown
  time if a fetch is in flight. State the bound or accept the delay.

### N5 — Two alternatives are rejected in a single sentence each
* **Severity:** Nit — **Section:** §7
* Driving the CLI's `/usage` / `/status` screen with a PTY and scraping
  the provider web dashboard are each rejected in one line marked
  "(brief)". The standard asks for documented reasons; a sentence is the
  minimum, and for the PTY option — which is the only alternative that
  uses the providers' own supported surfaces — it is thin.

### N6 — File reads assume the dashboard and the CLI run as the same user
* **Severity:** Nit — **Section:** §6.7
* Detection reads `~/.claude/.credentials.json` and `~/.codex/auth.json`
  (mode 600, owned by the CLI's user). The design never addresses a host
  where the dashboard process runs as a different user, or under Docker,
  where those reads fail with permission errors. Name the assumption or
  the failure mode.

### N7 — Reset-time arithmetic assumes a correct host clock
* **Severity:** Nit — **Section:** §5 D14
* "Resets in 2 h 14 min" is computed from the provider's `resets_at`
  minus the host clock. Clock skew is never mentioned. One line would do.

## Counter-arguments to each major decision

* **D1 (wording "detected on this device"):** the words are honest, but
  they understate the relationship — Baton does not merely observe the
  login, it spends it (background usage calls). The wording fix solves a
  labelling problem and leaves the consent problem (M14) untouched.
* **D2 (derive live, record nothing):** live derivation avoids a stale
  login record, but "detected at" re-stamped every 15-second poll is
  noise, not provenance (§M-findings, N-level: it always reads "just
  now"), and it cannot distinguish a login that changed from one that
  did not (B3).
* **D3 (always-on, no opt-in):** the user asked for visible numbers, but
  always-on background use of the CLI's token against an undocumented
  endpoint is the most aggressive default available; an opt-in would cost
  one click and would remove the entire consent objection (M14).
* **D4 (both providers, one issue):** saves a review cycle, but couples
  the two halves at merge time (M2) and doubles the unverified surface in
  a single PR.
* **D5 (host-level JSON cache):** correct location, but one file for two
  providers plus read-modify-write is a lost-update race (M5), and the
  fingerprint doing the invalidation is undefined (B3).
* **D6 (named cadence):** named numbers are good; the numbers themselves
  fail on the stated success rate (B4), the idle rule is undefined (M9),
  and manual refresh undercuts the floors (M16).
* **D7 (backoff + circuit breaker):** a sound pattern applied to the
  wrong failure class — routine 429s are treated as exceptional failures
  (B4) — and the 401/403 stop has no re-arm (B2). `Retry-After` versus
  backoff precedence is also undefined when both apply.
* **D8 (in-memory refresh only):** avoids a two-writer file race by
  introducing a worse one — refresh-token rotation and concurrent refresh
  may destroy the login (B1). Not refreshing at all is safer and simpler.
* **D9 (setup token tried once):** one attempt is the right instinct, but
  "once per token" cannot be enforced without a specified token identity
  (M11), and a misattributed 401 can permanently disable usage for a
  token kind.
* **D10 (file-only, Keychain deferred):** honest labelling is good, but
  the result is a card that tells a Keychain-only user "no login found"
  while they are in fact signed in — the design's own least-surprise
  principle is satisfied by a footnote, not by detection.
* **D11 (bare mode: suppress usage):** correct not to show a false
  number, but the table is incomplete for bare mode plus a saved
  credential (M17), and suppressing the number while showing the login
  still invites the question the design does not answer.
* **D12 (API key: hide usage):** correct — an API key has no windows —
  but "hidden" also hides the fact that a subscription login exists
  underneath, which the card only notes in passing; the dual-auth trap
  (§4) is half-closed here.
* **D13 (show only what a source returns):** sound against fabrication,
  but it is used to justify discarding data the source does return (the
  Opus/Sonnet splits, M4) and it leaves Claude with no account identity
  at all, which breaks the fingerprint (B3).
* **D14 (bars show "left", human reset times):** the right presentation,
  but "resets in …" depends on an unexamined host clock (N7) and the
  timezone of "as of HH:MM" is unspecified (N1).
* **D15 (CLI missing stays terminal):** a reasonable scope cut, but it
  means the zero-terminal promise in the goals is already false for the
  first-run user who most needs the feature — the goal should say so
  instead of the exceptions section carrying it.
* **D16 (consent note, no opt-out):** the note describes file reading,
  not the background network use of the token (M14). A note is not
  consent to spend a rate-limited credential.
* **D17 (never compute remaining locally):** the strongest decision in
  the document and the right one — but it is undermined by M3, which
  shows a stale server number the user can equally mistake for fresh.

## Standard compliance (feature-spec-standard.md)

* **Problem statement:** present and clear (§1).
* **Goals & non-goals:** present (§3); the zero-terminal goal is
  narrowed by exceptions carried in §6.6 rather than stated in §3.
* **Proposed architecture:** present (§6.1) and unusually concrete.
* **Alternatives considered:** present (§7) but incomplete — the
  on-demand-only alternative is missing (M1) and two rejections are
  one-liners (N5). This is the standard's mandatory rule, and it is only
  partly satisfied.
* **Risks & mitigations:** present (§4, §6.5, §6.7) but several named
  risks are not actually mitigated: refresh-token rotation (B1),
  permanent 401 wedge (B2), fingerprint ambiguity (B3), routine-429
  freshness (B4), cross-process cache race (M5). Naming a risk is not
  mitigating it.
* **Success metrics:** none inside the reviewed range (§3, §6); if they
  exist in §10, they are outside this review and should be referenced
  from §3 (M13).
