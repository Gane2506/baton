# Critic Review — Structural Lens (Stage 7)

* **Document:** `docs/designs/cli-autoimport-usage/design.md` (lines 1–697; the §9–§10 tail, lines 698–815, was also read for cross-checks)
* **Standard:** `resources/document_standards/feature-spec-standard.md`
* **Lens:** structural — contradictions between sections, gaps between §6.1, §6.2, §6.4, §6.5 and §6.6, terms used but never defined, numbers that disagree.
* **Method:** two claims in the design were checked against the code it references: `src/jev_agent/core/provider_auth.py` (`CodexDeviceFlow` spawns `codex login --device-auth`; the CLI writes `~/.codex/auth.json`) and `src/jev_agent/servers/stats.py` (`ServerStatsCache` states are `never | collecting | ready | failed` — there is no `stale` state).

## What passes structurally

The heading hierarchy is clean: one H1, H2 sections, H3 subsections, no skipped levels, no out-of-order sections. Every section the feature-spec standard requires is present: problem statement (§1), goals and non-goals (§3), proposed architecture (§6.1), alternatives considered (§7), risks and mitigations (§4, §6.7). All seventeen decisions D1–D17 are referenced downstream. The tables in §6.2 and §6.5 are well-formed. None of that saves the findings below — the document is well laid out and still contradicts itself.

## Blockers

### B1 — A page view both triggers a fetch and never triggers a fetch

* **Severity:** Blocker
* **Sections:** D6 vs §6.4
* **Contradiction:** D6 says "a page view triggers a fetch only when the cache is older than the interval." §6.4 says the opposite twice: "`views.cli_providers` and `connections_live` serve cached usage only; they never trigger or wait on a fetch" and "The live poll never triggers a fetch." An implementer cannot satisfy both. Either the render path can start a fetch (and §6.4's "never trigger" and the §8 test "render serves cached usage only (no fetch on render)" are wrong), or it cannot (and D6's page-view trigger and the idle rule's "viewed" mechanism have no trigger at all). The whole refresh architecture hangs on which sentence is true, and the design picks both.

### B2 — The manual Refresh button breaks the cadence the design just set

* **Severity:** Blocker
* **Sections:** D6 vs D6
* **Contradiction:** D6 sets a minimum of 15 minutes between Claude attempts ("4 requests/hour max") and 5 minutes between Codex attempts. The same decision sets the manual Refresh button to be "ignored when the last attempt was < 5 minutes ago (Claude) or < 60 seconds ago (Codex)." A Claude manual refresh 6 minutes after the last attempt is allowed by the button rule and forbidden by the cadence rule — it would push Claude to 10 requests/hour against a bucket the design says fails one request in three. The Codex button allows a refresh every 61 seconds against a 5-minute floor. Either the cadence floors do not apply to manual refreshes (then the "4 requests/hour max" claim is false and the rate-limit reasoning in §4 collapses), or the button floors are wrong numbers. The design states both as named numbers with no precedence between them.

## Majors

### M1 — The "no provider-specific string outside its adapter" rule forbids copy other sections require

* **Severity:** Major
* **Sections:** §6.1 vs §6.7, D10, §6.5
* **Contradiction:** §6.1 states, in bold, "**No provider-specific string, path, URL, or response key may appear outside its adapter** — not in `provider_auth.py`, `views.py`, `routes_connections.py`, or the template." But §6.7 requires the card to say "Detected on this device, from `~/.claude/.credentials.json`, at 14:32" — a provider-specific path in the template. D10 requires the card to name "`~/.claude/.credentials.json`". The §6.5 taxonomy, which lives in the shared layer, requires provider-named wording: "Claude is rate-limiting usage checks right now", "Couldn't reach Anthropic", "Sign in again in Claude Code". The rule as written cannot be implemented alongside the UI copy the design also specifies. The rule needs scoping (response keys and URLs stay in the adapter; display names and file paths do not), but as written it is a direct contradiction.

### M2 — Usage is measured against a credential the workers do not use

* **Severity:** Major
* **Sections:** §6.2 (Claude table, row 2) vs D9 vs §6.2 default rule
* **Contradiction:** Row 2 of the Claude table says that with a saved OAuth token present, workers use the saved token — but "Usage measured against: Saved token, if the endpoint accepts it (D9)". D9 then says: "If a CLI login also exists, the CLI access token is used instead." So when both a saved token and a CLI login exist, workers spend the saved token's allowance while the card shows the CLI login's remaining allowance. The numbers on the card would not be the numbers that govern the run the user is deciding about — the exact decision the Overview says the feature exists to support. This also breaks the section's own default rule ("Baton never silently replaces or hides a saved credential") in the usage dimension: the saved credential is the one in effect, and its usage is the one hidden. Separately, D9's "tried exactly once per token" has nowhere to live: the D5 cache-record field list has no flag for "setup token already tried and rejected", so either a restart re-tries it (violating "exactly once") or the memory exists in a field the design never defines.

### M3 — The normalized model cannot render the "Detected + failed" matrix row

* **Severity:** Major
* **Sections:** §6.1 vs §6.5
* **Gap:** The §6.5 state matrix says a failed card shows "Usage unavailable — \<plain-words reason\>" **and** "last successful value, if any, shown as 'last seen \<time\>'." But the §6.1 model offers only two shapes: `UsageSnapshot` (windows, no reason) or `UsageUnavailable` (reason, and `fetched_at` — a time, not the windows). There is no shape carrying both the last-good windows and the current failure reason. §8's reliability rule ("A failed refresh never overwrites a good cache entry (errors keep last-good)") means the data exists in the cache, but the model the page, route, poll, and template ever see has no way to express it. Either the model needs a combined type or the matrix row cannot be built as specified.

### M4 — The fingerprint is built from an "account hint" Claude does not provide

* **Severity:** Major
* **Sections:** D5 vs §6.7; D2 vs §6.9
* **Contradiction/gap:** D5 defines the cache fingerprint as "source path + account hint, never a token value", and §6.7 says "A fingerprint change invalidates the cached usage for that provider." But §6.7 also says "Claude's response carries no account identity, and none is inferred" — so for Claude there is no account hint to hash. The fingerprint's composition is undefined for one of the two providers, and the invalidation behaviour is undefined for both: does invalidation delete the snapshot, mark it `never`, or mark it `stale`? Second contradiction in the same area: D2 says "nothing is recorded about the login", yet §6.9 says "the CLI version is recorded with the imported state" — there is no "imported state" to record it with, per D2. (§6.1 says `ProviderAuth` gains a `cli_version` field, which is per-render memory, not a record; §6.9's wording says "recorded".)

### M5 — `disabled_by_config` is missing from the taxonomy that claims to be exhaustive

* **Severity:** Major
* **Sections:** §6.5 vs §6.9
* **Gap:** §6.5 introduces the taxonomy with "Every fetch outcome is one of these reason codes" and lists twelve. §6.9 then says a kill-switched source renders as `UsageUnavailable` with reason `disabled_by_config` — a thirteenth code with no row, no plain-words UI wording, and no next step. The state matrix has no "disabled" row either, so what the card shows when a provider is kill-switched is unspecified. Also unspecified: does the kill switch stop the refresher from attempting fetches for that provider, or only change the rendering? If the refresher keeps attempting, the kill switch does not stop the load it exists to stop.

### M6 — HTTP statuses the design relies on have no reason code

* **Severity:** Major
* **Sections:** D7, §6.9 vs §6.5
* **Gap:** D7's rule covers "401/403", but the §6.5 taxonomy maps only 401 (`unauthorized` — "401 — token rejected"). There is no code for 403. §6.9's sunset rule triggers when the Claude endpoint "returns `unexpected_response` or 404/410 on three consecutive days of attempts" — 404 and 410 have no code either, so it is undefined which reason a 404 produces, and undefined what tracks "three consecutive days": no cache-record field counts days or consecutive sunset-qualifying failures, and "three consecutive days of attempts" is ambiguous against the backoff/circuit-breaker rules, under which attempts can be more than a day apart. A maintainer cannot implement the sunset rule from this design.

### M7 — The precedence table mislabels environment variables as "Saved by Baton"

* **Severity:** Major
* **Sections:** §6.2 vs §1 ("Words used in this design"), §2
* **Contradiction:** §1 defines "Saved by Baton" precisely: "a token the user pasted or the dashboard's own flow produced, stored in `~/.jev/secrets.env`." §6.2's Claude table then labels its first column "Saved `ANTHROPIC_API_KEY`" and its second "Saved `CLAUDE_CODE_OAUTH_TOKEN`" — but §2 says both of those are environment variables, not values in `secrets.env`. An env-var API key is not "saved by Baton" under the design's own definition, so the card copy for env-var auth is undefined: is it shown as "API key", as "saved by Baton", or as something else? The same mislabelling hits the Codex table ("Saved `OPENAI_API_KEY`"). Three further gaps in the same tables: (a) no precedence is defined between `auth.json` `tokens` and `auth.json` `api_key` when both are present in the file (§2 lists them as alternatives with no order); (b) there is no row for bare mode + saved token, even though D11 says saved tokens are the bare-mode paths and usage should still show — the implementer must guess whether the bare-mode row or the saved-token row governs; (c) Claude row 2's "Card shows" column never names the CLI login that may also be present, while row 1 does name the also-present subscription login — the "name which others exist" rule is applied inconsistently between rows.

### M8 — The Claude adapter has no defined way to obtain the token it must send

* **Severity:** Major
* **Sections:** §6.1 vs §6.3, D8
* **Gap:** §6.3 and D8 require the Claude adapter to call the usage endpoint with a Bearer access token and to refresh via the refresh token. But §6.1's shared accessor — the single place a storage move is meant to be fixed — returns only `{kind, path-or-store, detected_at, cli_version}`: metadata, no token values. Nothing in the design says where the adapter gets the access token or the refresh token from. If the adapter reads `~/.claude/.credentials.json` itself, the accessor's centralisation promise is broken and there are two readers of the credentials file. If the accessor is meant to hand over values, §6.1 contradicts §2's "Values are never returned" rule for detection. Either way, a load-bearing data flow is missing.

### M9 — Stale vs failed has no precedence, and the state machine gains a state with no definition

* **Severity:** Major
* **Sections:** §6.4 vs §6.5; §2
* **Gap:** §6.4 defines staleness by age ("`ready` while younger than its max-age … `stale` after that, `failed` when the last attempt failed") and a state machine `never → collecting → ready → stale / failed`. A snapshot can satisfy both `stale` and `failed` at once (old *and* last attempt failed), but §6.5 has separate "Detected + stale" and "Detected + failed" rows and no rule for which wins. The `stale` state is also new: the `ServerStatsCache` pattern the design reuses has states `never | collecting | ready | failed` (verified in `servers/stats.py`), and the design never defines `stale`'s transitions — in particular, whether a `stale` snapshot with a *successful* last attempt exists (yes, under the idle rule: nothing failed, the cache just aged), and what reason is shown then, since the stale matrix row assumes a failure reason ("labelled 'stale — as of HH:MM' with the reason ('rate-limited')"). There is no matrix row for `collecting` at all, so what the card shows during a first fetch is undefined. Three smaller undefined pieces in the same section: the `last_status` field's value set is never enumerated; `fetch_started_at`, used by the cross-instance single-flight rule ("a second process that sees a fresh `fetch_started_at` (< 60 s old)"), does not appear in D5's cache-record field list; and the cross-instance mechanism is left undecided ("A file lock is the correct mechanism; a timestamp check is the minimum") — the design names two options without picking one.

### M10 — The Codex single-window case may label the wrong window

* **Severity:** Major
* **Sections:** §6.3 vs §6.5; §6.1
* **Gap:** §6.3 says `secondary` may be null and the card then shows one window with the copy "Your plan has no separate 5-hour limit — weekly only." That copy assumes the surviving window is the weekly one, but the design never states which of `primary`/`secondary` is the 5-hour window and which is the weekly window — window kinds are derived from `windowDurationMins`, not from primary/secondary position. If `primary` were the 300-minute window, the single-window copy would be wrong. Related gaps: no mapping is defined for a `windowDurationMins` that is neither 300 nor 10080 (is it `unexpected_response`, ignored, or shown with an unknown label?), and the Claude-side mapping (`five_hour` → session, `seven_day` → weekly) is never stated at all, unlike the Codex mapping which is explicit.

### M11 — The live-poll `usage` payload is defined in three places with three different field lists

* **Severity:** Major
* **Sections:** §6.4 vs §6.6 vs §6.8
* **Gap:** §6.4 says the poll gains "an additive `usage` key per provider". §6.6 says the poll "carries cached usage (`fetched_at` + windows)". §6.8 says "Per-provider health is visible in the live poll JSON (`usage.state`)". No single place defines the `usage` object's schema: is it `{fetched_at, windows}`, or does it also carry `state`, `last_status`, `last_reason`, `plan`, `source_version`? The template and `conn.js` cannot be written against a payload whose fields are scattered across three sections that do not agree.

### M12 — The D1 label split is unimplementable for Codex

* **Severity:** Major
* **Sections:** D1 vs §2, verified against `provider_auth.py`
* **Gap:** D1 splits the OAuth labels: a dashboard-saved token is "Signed in with your Claude subscription (saved by Baton)" and a CLI login is "Detected on this device". That split works for Claude, where the dashboard flow stores into `secrets.env`. For Codex it does not: the dashboard's own device-code flow (`CodexDeviceFlow`) spawns `codex login --device-auth`, and the CLI writes the resulting tokens into `~/.codex/auth.json` — the exact file detection reads. A Codex login created through the dashboard is therefore indistinguishable by file location from a CLI-created login, and the design never says how the card tells them apart, or whether the dashboard-flow Codex login should be labelled "saved by Baton" at all. The §6.2 Codex table has no "saved by Baton" row for this case.

### M13 — "Viewed" in the idle rule is undefined, and the 15-second poll may defeat it

* **Severity:** Major
* **Sections:** D6 vs §2, §6.4
* **Gap:** D6's idle rule stops background refresh "while the usage UI has not been viewed in the last 30 minutes", but nothing defines what counts as a view. The live poll fires every 15 seconds whenever the Connections page is open (§2) — if a poll counts as a view, the idle rule never engages while the page sits open in any tab, including a background tab, and the rule is dead text. If only a foreground render counts, the design needs to say so and say how the refresher learns about it — which leads back to Blocker B1, since the only channel from a page view to the refresher is the render path that §6.4 says never triggers anything.

### M14 — "Stop until auth state changes" has no detection mechanism

* **Severity:** Major
* **Sections:** D7 vs D5, §6.4, §6.7
* **Gap:** D7 says a 401/403 retries once, "then stop until auth state changes." Nothing defines how an auth state change is detected: is it the fingerprint change (§6.7), which is itself undefined for Claude (M4)? Is `consecutive_failures` reset when auth changes, or does the circuit breaker still count the 401s toward its 3-failure threshold? And after the 1-hour quiet period, "then one probe attempt" — if the probe fails, is there another hour of quiet, or does the breaker re-arm immediately? The backoff, circuit breaker, and auth-stop rules are three overlapping stop conditions with no stated interaction.

### M15 — The card must name credentials the model never carries

* **Severity:** Major
* **Sections:** §6.1 vs §6.2
* **Gap:** §6.2 requires the card to name "which credential is in effect and which others exist" (e.g. row 1: "a subscription login is also present"). But §6.1 says `ProviderAuth` gains "additive fields only: `cli_version` and the usage snapshot". Neither field carries the set of present credentials. Detection today returns the first present credential (§2); the design never specifies a model field listing *all* present credentials, so the "also present" lines in the precedence table have no data source.

## Nits

### N1 — The Overview promises two windows for Codex; §6.3 says often one

* **Severity:** Nit
* **Sections:** Overview vs §6.3
* **Discrepancy:** The Overview says each provider shows "the two windows the providers meter: the 5-hour session window and the weekly window". §6.3 says the 5-hour window "is suspended for many accounts" and `secondary` may be null. The Overview overstates the Codex case.

### N2 — The §1 snapshot definition includes "the outcome"; the §6.1 model does not

* **Severity:** Nit
* **Sections:** §1 vs §6.1
* **Discrepancy:** "Usage snapshot — one provider's last fetched usage: its windows, the time it was fetched, and the outcome." `UsageSnapshot` has windows and `fetched_at` but no outcome field; the outcome lives in the separate `UsageUnavailable` type. The vocabulary definition and the model disagree about what a snapshot is.

### N3 — Two different formats for the same timestamp

* **Severity:** Nit
* **Sections:** §6.4 vs §6.5
* **Discrepancy:** §6.4 says every usage number carries its `fetched_at` ("updated 12 min ago"). The §6.5 stale row shows "as of HH:MM" instead. One datum, two renderings, no rule for which the template uses where.

### N4 — Codex and Claude card copy do not match each other

* **Severity:** Nit
* **Sections:** D1 vs §6.2 (Codex table, row 3)
* **Discrepancy:** Claude's card reads "Detected on this device — the `claude` CLI is signed in"; Codex's equivalent reads "Detected on this device — signed in with ChatGPT" — no CLI name, different sentence shape. D1 says the Codex wording is "the Codex equivalent" of the Claude wording; the table's wording is not equivalent in form.

### N5 — Two taxonomy codes are not fetch outcomes

* **Severity:** Nit
* **Sections:** §6.5 vs D12
* **Discrepancy:** The taxonomy claims "Every fetch outcome is one of these reason codes", but `no_login` (no fetch is attempted — there is nothing to fetch with) and `not_applicable_api_key` (D12: "No usage calls are attempted for `api_key` auth") are not fetch outcomes at all. They are rendering states filed under a fetch-outcome taxonomy. The claim should be narrowed, or the codes moved out of the fetch taxonomy.

### N6 — Two taxonomy rows leave the UI behaviour half-specified

* **Severity:** Nit
* **Sections:** §6.5
* **Discrepancy:** The `no_login` row's "UI wording" cell contains "— (no usage section)" — a dash, not copy — while every other row gives exact wording. The `rate_limited` row's next step says "no button spam" without saying whether the Refresh button is hidden, disabled, or present-but-ignored, which is the behaviour an implementer needs.

### N7 — Three small undefined terms

* **Severity:** Nit
* **Sections:** §6.5, §6.1, D14
* **Discrepancy:** (a) The "Detected + ready" matrix row says the disclosure opens "the per-window bars, plan (Codex), and source note" — the content of the "source note" is never defined. (b) §6.1 requires each adapter to parse "behind a versioned parser" without saying what the parser version is or where it is recorded (`source_version` is defined as "CLI version / adapter version", not parser version). (c) D14's reset phrasing gives examples for hours and days ("resets in 2 h 14 min", "resets in 3 d") but no rule for sub-hour resets.

### N8 — The Codex response nesting is ambiguous

* **Severity:** Nit
* **Sections:** §6.3
* **Discrepancy:** "Response: `rateLimits.primary` / `.secondary`, each with `usedPercent` …, plus `planType`, `rateLimitResetCredits`, …" — it is not stated whether `planType` and the other fields are siblings of `rateLimits` or nested inside it, nor whether `rateLimits` sits in the JSON-RPC `result` or at the response top level. The parser contract needs the nesting; the design leaves the implementer to guess.

## Summary count

* Blockers: 2 (B1 page-view trigger contradiction; B2 manual-refresh floors vs cadence floors)
* Majors: 15 (M1–M15)
* Nits: 8 (N1–N8)

The two blockers and M1–M3 are the load-bearing problems: the design gives opposite instructions on whether a page view can start a fetch, lets the Refresh button violate its own rate limit, bans provider-specific strings the UI is required to show, measures usage against a credential the workers do not use, and defines a display model that cannot render one of its own state-matrix rows. The remaining majors are gaps an implementer would have to invent answers for — most of them in exactly the places the design claims to have "named numbers" (fingerprint composition, poll payload schema, stale/failed precedence, auth-change detection, sunset tracking).
