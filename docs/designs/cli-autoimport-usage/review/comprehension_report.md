# Comprehension Test — Stage 6 Goldfish Review

**Document:** `docs/designs/cli-autoimport-usage/design.md` (814 lines)
**Scope:** feature range lines 1–697 and detailed range lines 698–814
**Reviewer:** Comprehension Goldfish (zero prior context; only this document plus
the files it explicitly references, read read-only to ground findings)
**Verdict:** Not re-implementable from this document alone. A competent
implementer would have to stop and ask the author questions on at least a
dozen points, several of which change behaviour the user can see (rate-limit
budget, backoff, what the card shows). The document is strong on intent,
wording, and failure taxonomy; it is weak on the exact contracts an
implementer must code against: wire formats, field names, state transitions,
and several numbers that contradict each other.

Line numbers below are approximate (counted from the document as read).
Every finding carries the exact question an implementer would be forced to ask.

---

## Blockers

### B1 — Wrong file paths for the config change (§9, lines ~728–730)
The plan says to add the `[usage]` section in
`src/jev_agent/core/config/schema.py` + `defaults.py`. Those files do not
exist. The real files are `src/jev_agent/config/schema.py` and
`src/jev_agent/config/defaults.py` (verified in the repo). The plan also never
says what the schema entries are called, their types, or how `jev.toml`
`[usage]` is loaded and validated — only that the two booleans default to true.

**Implementer asks:** "Where does `[usage]` actually go, and what are the
field names, types, and loader behaviour?"

### B2 — The Claude credential file's inner field names are never given (§6.3, §5 D8, lines ~200–215, ~450–465)
The document says the Claude adapter reads the access token, checks
`expiresAt`, and refreshes with the refresh token from
`~/.claude/.credentials.json` under `claudeAiOauth`. It never names the
fields that hold the access token and the refresh token, nor what to do when
`expiresAt` is missing, malformed, or in an unexpected format. The existing
detection code only checks that `claudeAiOauth` is present; it never reads
these fields, so there is no code to copy the names from.

**Implementer asks:** "What are the exact JSON field names for the access
token and refresh token inside `claudeAiOauth`, and what do I do if
`expiresAt` is absent?"

### B3 — The token-refresh HTTP call is underspecified (§5 D8, lines ~255–265)
The design names the refresh URL (`POST https://console.anthropic.com/v1/oauth/token`)
but gives no request body (grant type? client id? which field carries the
refresh token?), no response shape (which fields come back — new access
token, new refresh token, expiry in seconds or an absolute time?), and no
rule for how the new expiry is computed. It also does not say where in memory
the refreshed token lives, what happens if two refreshes race, or whether a
successful refresh replaces the token for the very next usage call only or
for the life of the process.

**Implementer asks:** "What exactly do I POST, what exactly comes back, how
do I compute the new expiry, and where do I keep the token?"

### B4 — The Codex app-server protocol is underspecified (§6.3, lines ~432–450)
The design says: spawn `codex app-server`, send `initialize` (id 1), then
`account/rateLimits/read` (id 2), skip notifications. It does not say:
which binary is spawned (the configured `command`? a PATH lookup?),
what the `initialize` parameters are (protocol version, client info,
capabilities), what parameters `account/rateLimits/read` takes, how responses
are matched to request ids, how the byte stream is framed (newline-delimited
JSON? Content-Length headers?), how notifications are recognised and skipped,
what the success and error envelope shapes look like, or what happens when
the 4 MiB output cap is hit (kill the process? which reason code?).
It also does not say which environment variables survive the "scrub" besides
`HOME` — notably whether `PATH` survives, which decides whether the binary
can even be found.

**Implementer asks:** "What exactly do I spawn, with what arguments and
environment, what exactly do I send, and how do I read the replies back?"

### B5 — Codex window derivation has no rule for unexpected values (§6.3, lines ~440–448)
Windows are derived from `windowDurationMins`: 300 → session, 10080 →
weekly. There is no rule for any other value (a third window? a changed
duration?), for `primary` being null, for `primary` and `secondary` having
the same duration, or for `resetsAt` being missing or in the past. The
document also never states the Claude mapping explicitly (`five_hour` →
session, `seven_day` → weekly is implied, never written), nor what happens
if only one of the two Claude objects is present.

**Implementer asks:** "What do I show when `windowDurationMins` is neither
300 nor 10080, when `primary` is null, or when Claude returns only one
window?"

### B6 — The "no provider-specific string outside its adapter" rule contradicts the reason-code table (§6.1, §6.5, lines ~375–390, ~512–545)
Section 6.1 forbids any provider-specific string, path, URL, or response key
outside the adapter — "not in `provider_auth.py`, `views.py`,
`routes_connections.py`, or the template". But the §6.5 table's UI wording is
provider-specific copy the template must render ("Couldn't reach Anthropic",
"Claude is rate-limiting usage checks right now", "Sign in again in Claude
Code", "This Codex version isn't supported for usage yet"). Either the
adapter must supply the UI wording (then the table's wording lives in the
adapter, which is never stated) or the rule cannot be followed as written.
The table also has no provider column: several rows are Claude-only
(`token_expired_refresh_failed`, `setup_token_rejected`, `rate_limited`) and
one is Codex-only (`app_server_failed`), but nothing says so, and no Codex
equivalents are given for `network_error` or `timeout`.

**Implementer asks:** "Where does the provider-specific UI wording live — in
the adapter or the template — and what is the Codex wording for the shared
rows?"

### B7 — `disabled_by_config` is missing from the failure taxonomy (§6.9 vs §6.5, lines ~512–545, ~630–645)
Section 6.9 says turning a source off renders it as `UsageUnavailable` with
reason `disabled_by_config`. That code does not appear in the §6.5 taxonomy
table, so it has no UI wording and no next step defined. An implementer
following §6.5 alone would not know this state exists.

**Implementer asks:** "What does the card show for a kill-switched provider,
and what is its next step?"

### B8 — The fingerprint is undefined (§5 D5, §6.7, lines ~215–230, ~600–615)
The cache record's `fingerprint` is "login identity hash — source path +
account hint, never a token value". No hash algorithm, no exact input string,
and no comparison rule are given. Worse, "account hint" is unobtainable for
Claude: §5 D13 and §6.7 both say Claude's usage response carries no account
identity and none is inferred. So the fingerprint's second half cannot be
built for Claude as specified, and the invalidation rule ("a fingerprint
change invalidates the cached usage") cannot be coded.

**Implementer asks:** "How exactly do I compute the fingerprint for each
provider, and what is the 'account hint' for Claude when the design says no
account identity exists?"

---

## Major

### M1 — Manual refresh contradicts the stated rate budget (§5 D6, lines ~232–245)
D6 says Claude's floor is 15 minutes between attempts ("4 requests/hour
max"), but the manual Refresh button is only ignored when the last attempt
was under 5 minutes ago. That allows up to 12 manual refreshes an hour,
which breaks the "4 requests/hour max" claim the whole rate-limit argument
rests on. It is also never stated whether a manual refresh counts as an
attempt that resets the 15-minute background clock, or whether it bypasses
the background floor entirely. The same gap exists for Codex (5-minute
cadence vs 60-second manual floor), and "refresh-on-view" for Codex is named
but never defined — what triggers it, and does it bypass the 5-minute floor?

**Implementer asks:** "Does a manual refresh reset the background timer, and
is the real Claude ceiling 4 or 12 requests an hour?"

### M2 — The idle rule cannot be implemented as written (§5 D6, §6.4, lines ~240–245, ~472–490)
"No background refresh while the usage UI has not been viewed in the last
30 minutes." Nothing defines what counts as "viewed": a render of the
Connections page? a hit on the 15-second live poll (which the sidebar slot
fires from the dashboard generally)? a tab open in the background? Nothing
says who records the last-viewed time, where it is stored (process memory?
cache file? per tab?), or how the background refresher thread learns about
it. The related rule — "a page view triggers a fetch only when the cache is
older than the interval" — has the same gap: which page view, and does the
live poll count?

**Implementer asks:** "What event counts as a view, where is the last-viewed
time kept, and does the 15-second poll count as viewing?"

### M3 — Backoff and circuit-breaker interaction is undefined (§5 D7, lines ~247–255)
D7 gives a backoff ladder (15 → 30 → 60 minutes, capped at 60) and a
circuit breaker (after 3 consecutive failures, 1-hour quiet, then one
probe). It never says how the two combine: does the quiet period start
after the third failure's backoff expires, or replace it? If the probe
fails, does the ladder restart, or does another 1-hour quiet start? Does a
success reset `consecutive_failures` to zero (presumably, but unstated)?
For 401/403 — "retry once, then stop until auth state changes" — nothing
says how "auth state changes" is detected (fingerprint change? detection
re-run? a dashboard restart?), what `next_retry_at` is set to, or whether
the 1-hour quiet applies. `Retry-After` handling is also incomplete: honour
it "exactly when present", but no parsing rule (seconds vs HTTP-date), and
no rule for whether a long `Retry-After` (e.g. 3600 s) overrides the
15-minute floor.

**Implementer asks:** "After 3 failures, when exactly is the next attempt,
what resets the failure count, and how do I know auth state changed?"

### M4 — The cross-instance single-flight rule refuses to choose (§6.4, lines ~495–505)
"A file lock is the correct mechanism; a timestamp check is the minimum."
That is two different mechanisms offered as a choice the implementer must
make. The `fetch_started_at` field this relies on appears here but is not
in the D5 cache-record field list, its format is undefined, and nothing says
who clears a stale marker if a process crashes mid-fetch (the "< 60 s old"
expiry is stated, but not whether expiry alone is enough or a marker must be
actively cleared).

**Implementer asks:** "Do I implement a file lock or a timestamp check, what
is the exact marker field, and what happens to a marker left by a crashed
process?"

### M5 — The state machine has no transition rules (§6.4, lines ~505–515)
States are `never` → `collecting` → `ready` → `stale` / `failed`, but no
transition is specified. Is `ready` → `stale` decided at read time by age,
or by a timer? If a refresh fails while the snapshot is still young (fetched
5 minutes ago), is the state `ready` or `failed` — that is, does a failed
attempt demote a fresh number? What moves `failed` back to `collecting`?
`last_status` is named as a stored field but its vocabulary and type are
never defined (state name? HTTP status? something else?).

**Implementer asks:** "When exactly does a provider move between ready,
stale, and failed, and what values can `last_status` hold?"

### M6 — `fetched_at` means two different things (§6.1, §6.4, §6.5, lines ~360–375, ~505–515, ~545–560)
`UsageUnavailable.fetched_at` is "last successful fetch, if any". The D5
cache record's `fetched_at` is not qualified — last attempt or last success?
Section 6.4 staleness and the "updated 12 min ago" label both use
`fetched_at` without saying which one. Section 6.5's "last seen <time>" for a
failed state needs a third timestamp (last success) that may not be the same
as either. An implementer cannot tell which timestamp the UI shows after a
failed refresh.

**Implementer asks:** "After a failed refresh, does 'updated … ago' show the
failed attempt or the last good fetch?"

### M7 — Human time phrasing has no rounding rules (§5 D14, §6.5, lines ~300–310, ~545–560)
The design requires "resets in 2 h 14 min", "resets in 3 d", "updated
12 min ago", "stale — as of HH:MM" — but gives no rounding rule (floor?
nearest?), no unit thresholds (when do minutes become hours, hours become
days — and why does "3 d" carry no hours?), and no timezone for "HH:MM"
(dashboard host time? user's browser time?). Two implementers would render
different strings for the same data. The same gap applies to the remaining
percent: `used_percent` is a float, but the display precision (integer?
one decimal?) and whether the bar fills to remaining or to used are never
stated — "remaining direction ('left')" in §6.6 does not settle it.

**Implementer asks:** "How do I round and choose units for reset times and
'updated … ago', in which timezone, and does the bar show remaining or used?"

### M8 — The precedence table's "—" is undefined (§6.2, lines ~392–430)
Every row uses "—" in columns where a credential may or may not be present,
but the document never says whether "—" means "absent", "don't care", or
"not applicable". This matters: row 1 (saved `ANTHROPIC_API_KEY` = yes) does
not say what happens when a saved OAuth token is also present — which one
governs workers and usage? The table's top-to-bottom order implies API key
> saved token > CLI login, but that rule is never stated in words, and the
row for "saved token, if the endpoint accepts it (D9)" collides with D9's
own rule that a CLI login's token is used instead when one also exists.
The same ambiguity exists in the Codex table (saved `OPENAI_API_KEY` vs
`auth.json` `api_key` vs `auth.json` `tokens` in combination).

**Implementer asks:** "When both a saved API key and a saved OAuth token
exist, which governs — and does '—' mean absent or don't-care?"

### M9 — `get_login_source()` return shape is a sketch, not a contract (§6.1, lines ~385–390)
The shared accessor returns `{kind, path-or-store, detected_at,
cli_version}`. Key names are given in prose, not as a type; the `kind`
vocabulary is undefined; `detected_at`'s type is undefined (datetime? ISO
string?); `cli_version`'s source is undefined (which command and flag
produces it? what value when the CLI is missing?). The related
`UsageSnapshot.source_version` ("CLI version / adapter version") is equally
vague, and "adapter version" is never defined anywhere (a hardcoded string?
a module constant? the package version?).

**Implementer asks:** "What is the exact return type of `get_login_source`,
what values can `kind` take, and how is each version string obtained?"

### M10 — There is no known-good version range (§6.5, §6.9, §9, lines ~540–545, ~640–650, ~745–750)
The `unsupported_version` reason code requires a "known-good range", but
the design records exactly one data point: Codex 0.164.0-alpha.9 "verified
on the VM". No range for Codex, no version at all for the Claude CLI (its
version is "to be recorded at Mac verification"), and no rule for what
counts as unsupported. The §9 test "wrong version" therefore cannot be
written, and the adapter cannot decide when to emit the code.

**Implementer asks:** "Which CLI versions are supported, and what makes a
version unsupported?"

### M11 — The refresh route contract is missing (§6.4, §9, lines ~500–505, ~735–740)
The new endpoint `POST /connections/providers/<provider>/usage/refresh` has
no specified response (JSON shape? redirect? status codes?), no stated
behaviour when the refresh is ignored by the rate limit (200 with a state?
429? silent no-op?), and no defined `<provider>` spelling — `ProviderAuth.provider`
uses `"claude_code"` / `"codex"` (verified in the repo), so "claude" in the
route is ambiguous. It also never says how the route fits the dashboard's
existing POST pattern (the `act()` helper other POST routes use) or whether
any CSRF/auth applies.

**Implementer asks:** "What does the refresh endpoint return when it accepts,
ignores, or rejects a refresh, and is the path segment `claude` or
`claude_code`?"

### M12 — The live-poll `usage` JSON shape is missing (§6.4, §6.8, lines ~485–495, ~617–628)
The poll "gains an additive `usage` key per provider" and §6.8 says
"per-provider health is visible in the live poll JSON (`usage.state`)".
No complete JSON schema is given: is `usage` nested inside each provider
object or a separate top-level key? Which fields does it carry (windows?
`fetched_at`? state? reason? plan?)? The existing poll shape (verified:
`provider`, `auth`, `label`, `installed` per provider) is not extended in
writing, and `conn.js` behaviour per state is not described beyond "updates
the usage block in place".

**Implementer asks:** "What is the exact JSON of the poll's `usage` key, and
what does `conn.js` do for each state?"

### M13 — Refresher lifecycle wiring is not located (§6.4, §9, lines ~490–500, ~735–740)
"The refresher starts with the dashboard and stops on shutdown" and
`routes_connections.py` owns "refresher lifecycle (start on dashboard start,
stop on shutdown)" — but no function, module, or hook is named. Nothing says
where dashboard start lives, who constructs the refresher, what happens with
multiple repos or dashboard processes, how many threads exist ("one
background scheduler shared by both providers" — one thread or one per
provider?), or what `shutdown()` does if a thread or a live app-server does
not stop (join timeout? force-kill?).

**Implementer asks:** "Which function starts the refresher, where is it
called from, and what does shutdown do if a thread hangs?"

### M14 — First-fetch timing is undefined (§6.4, lines ~472–490)
Nothing says whether the refresher fetches immediately when the dashboard
starts, or waits for the first view / the first interval. This decides
whether starting the dashboard fires Claude usage calls before any user has
looked at the page — directly relevant to the rate-limit budget the design
is careful about elsewhere. The "Check usage" button's behaviour for the
`not_checked_yet` state is likewise implied (it triggers a manual refresh)
but never stated.

**Implementer asks:** "Does dashboard start trigger an immediate usage fetch,
or only a page view?"

### M15 — `ProviderAuth` gains fields that are never typed (§6.1, §9, lines ~385–390, ~725–730)
`ProviderAuth` gains "additive fields only: `cli_version` and the usage
snapshot (cache read, never a fetch)". Neither field's name nor type is
given (is the usage field `UsageSnapshot | UsageUnavailable | None`? what is
it called?), and `detected_at` — which the card must display per D2 and §6.7
("from `~/.claude/.credentials.json`, at 14:32") — is not listed as a field
at all. The existing `ProviderAuth` (verified) has no `detected_at`, so the
template has no defined source for that time.

**Implementer asks:** "What are the exact new `ProviderAuth` field names and
types, and where does 'detected at' come from?"

### M16 — Cache record schema is incomplete (§5 D5, §6.4, lines ~215–230, ~472–480)
The D5 record lists `fingerprint`, `fetched_at`, `next_retry_at`,
`consecutive_failures`, `last_status`, `last_reason`, `source_version`,
`windows` — but the `windows` payload's serialised form is undefined (dict
keys? datetime format?), there is no schema-version field for future
changes, no stated behaviour for a corrupt or unparseable cache file
(treat as empty? which reason code surfaces?), no rule for the temp file's
name/location/mode during atomic write, and no stated behaviour when
`~/.jev/` does not exist (create it? fail?). The `fetch_started_at`
in-progress marker from §6.4 is absent from this list (see M4).

**Implementer asks:** "What is the exact JSON schema of the cache file, how
is a corrupt file handled, and who creates `~/.jev/`?"

### M17 — Bare mode is specified for Claude only, silently (§5 D11, §6.2, lines ~275–285, ~410–420)
D11 and the precedence table cover bare mode for Claude. Codex has no bare
mode in the config (verified: only `claude_code.bare` exists), but the design
never says that, and never says whether the Codex card needs any equivalent
qualification. It also says "saved tokens and API keys are unaffected (they
are the bare-mode paths)" without saying which Codex credential, if any, is
the bare-mode path. An implementer could reasonably add a Codex bare-mode
branch that does not exist.

**Implementer asks:** "Does Codex have a bare mode, and if not, does the card
need to say anything about it?"

---

## Nits

### N1 — "—" in the §6.5 table's "Next step" column
Several rows say "(none — maintainer action; logged once)" or "(none)" without
distinguishing "no action exists" from "action exists but is not shown in the
UI". Minor, but the template needs to know whether to render a next-step area
at all.

### N2 — Numbers that need a source (§4, lines ~150–160)
"~480 Claude requests/hour with two tabs open" and "roughly one request in
three succeeding under an active session" are presented as facts with no
source or measurement method. The 480 follows from the 15-second poll, but
the one-in-three figure is unverified — it drives the cadence argument, so
its basis should be cited or labelled as an estimate. (Section 7's "240
times/hour" for the Codex spawn is likewise per-tab, not total; the document
mixes per-tab and total figures without saying which.)

### N3 — `planType` values are undefined (§6.1, §6.3, lines ~360–375, ~440–448)
Codex's `planType` is shown on the card, but its possible values are never
listed, so the template cannot know whether to render it verbatim or map it
to a friendlier label — and what to do with an unknown value.

### N4 — The Codex `auth.json` `api_key` provenance claim (§6.2, lines ~415–425)
The table annotates the `api_key` row "stored by `codex login
--with-api-key`". The detection section only establishes that the file holds
an `api_key`; the claim about which command stored it is unverified and
irrelevant to the implementation. Either verify it or drop the parenthesis.

### N5 — `UsageWindow.kind` wording (§6.1, lines ~355–365)
`kind` is described as "from the response", but neither vendor response
carries a kind name — both kinds are derived by the adapter (duration for
Codex, object name for Claude). The comment should say "derived by the
adapter", matching the `windows` comment one line below.

### N6 — `not_checked_yet` vs `no_login` emission (§6.5, lines ~512–525)
`no_login` is listed as a fetch outcome, but no fetch is ever attempted with
no login, so nothing in the design emits it. Say which component produces
each code, or drop `no_login` from the fetch taxonomy and treat it as a
render-time state.

### N7 — Stale `resets_at` in the past (§6.1, §6.3, lines ~355–365, ~440–448)
No rule covers a `resets_at` / `resetsAt` that is already in the past when
read (window reset between fetch and render). "Resets in −12 min" is the
literal outcome; the design should state the intended rendering (e.g. clamp
to "resetting…" or re-fetch).

---

## What is clear (for the record)

The wording decisions (D1, D14, D16, D17), the precedence intent, the
reason-code taxonomy's plain-words column, the cache location and mode, the
cadence numbers themselves, the kill-switch mechanism, the sunset rule, and
the "usage failure never breaks the page" rule are all stated plainly enough
to implement. The gaps above are not about intent — they are about the
contracts between the parts: wire formats, field names, state transitions,
and which of two stated numbers governs when they disagree.

## Grounding notes

Verified read-only against the repo at `~/workspace/baton-issue396`:
`src/jev_agent/core/provider_auth.py` (`ProviderAuth` fields, detection
precedence, `CodexDeviceFlow` / `ClaudeSetupTokenFlow` exist,
`get_login_source` does not exist, no `accessToken`/`refreshToken`/
`expiresAt` field reads); `src/jev_agent/servers/stats.py`
(`ServerStatsCache` states `never`/`collecting`/`ready`/`failed` confirmed);
`src/jev_agent/dashboard/routes_connections.py` and `views.py`
(`cli_providers` signature, live-poll JSON shape confirmed);
`src/jev_agent/config/schema.py` (real config path — the `core/config`
path in §9 does not exist); `src/jev_agent/config/schema.py` line ~896
(only Claude has a `bare` flag).
