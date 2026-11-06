# Consolidated Review Report — cli-autoimport-usage (issue #396)

* **Design reviewed:** `docs/designs/cli-autoimport-usage/design.md` (814 lines, draft of 2026-11-06)
* **Review:** EGM five-stage Goldfish review, consolidated (uninterrupted) flow
* **Stages:** 5 zoom-out, 6 comprehension, 7 critic (design / structural / style), 8 intermediate zoom-out, 9 implementation readiness (implementation / codebase-details / style)
* **Reports:** `review/zoom_out_report.md`, `review/comprehension_report.md`, `review/critic_design.md`, `review/critic_structural.md`, `review/critic_style.md`, `review/intermediate_zoom_out_report.md`, `review/readiness_implementation.md`, `review/readiness_codebase.md`, `review/readiness_style.md`
* **Verdict: NOT approved as written.** No stage approved the design. The reviewers converge on five architecture changes (C1–C5 below) plus a data-contract rewrite (§6.1/§9). The resolve pass applies them; the rewritten design then gets one re-audit (max 1 per stage).

## Counts

Raw findings across the nine reports: **27 Blockers, 119 Majors, 64 Nits** (counting each report's own tally; the same defect found by several lenses is counted once per report below and deduplicated into the consolidated items).

| Stage | Blockers | Majors | Nits | Verdict |
|---|---|---|---|---|
| 5 zoom-out | 3 | 11 | 6 | not approved |
| 6 comprehension | 8 | 17 | 7 | not re-implementable from the doc alone |
| 7 design critic | 4 | 17 | 7 | not ready; B1–B4 first |
| 7 structural | 2 | 15 | 8 | load-bearing contradictions |
| 7 style | 3 | 14 | 9 | does not meet plain-English standard |
| 8 intermediate zoom-out | 5 | 15 | 6 | still not approved; case stronger |
| 9 implementation readiness | 6 | 11 | 3 | not implementable as written |
| 9 codebase details | 5 | 6 | 2 | not ready |
| 9 readiness style | 2 | 14 | 9 | not ready as written |

Deduplicated into **30 consolidated items (C1–C30)** below plus **3 contested items (X1–X3)**. Every raw finding maps to exactly one consolidated item or one contest (traceability matrix at the end).

## New evidence gathered during resolve (not from the reviewers)

Two sources were fetched read-only to ground the resolve:

1. **Codex app-server protocol** — `https://github.com/prontsevich/codex-limitbar/blob/HEAD/skills/codex-limitbar/references/rate-limits-protocol.md` (read 2026-11-06; verified against codex-cli 0.147.0, 0.154.0, 0.155.0-alpha.16.4, 0.158.0-alpha.2.1). It gives: spawn `codex app-server --listen stdio://`, cwd temp dir, env copy with `PWD` overridden and `OLDPWD`/`INIT_CWD` deleted; JSONL over stdio; `initialize` id 1 with params carrying **only** `clientInfo` (`name`, `title`, `version`), **no** `protocolVersion`; `initialized` notification; `account/rateLimits/read` id 2 with **no params**; skip anything with `method` and no matching id; live response shape with `result.rateLimits.primary`/`secondary` (`usedPercent`, `windowDurationMins`, `resetsAt` unix seconds), `planType` (`plus`/`pro`/etc.), `rateLimitResetCredits`, **`accountId`**, `ordinaryUsageAllowed`, `spendControlReached`, `rateLimitReachedType`; 15 s timeout, SIGTERM after the answer, 4 MiB cap; 5-hour window suspended for many accounts since ~July 2026 — `secondary` may be null, only a weekly window remains; "derive names from `windowDurationMins`", never from primary/secondary position.
2. **Claude usage endpoint, community sources** — ccusage (PyPI), kamranahmedse/claude-statusline (DeepWiki), tclaude `usageapi` (Go), ssdnodes rate-limit-headers explainer. They confirm: `GET https://api.anthropic.com/api/oauth/usage`, Bearer OAuth token, header `anthropic-beta: oauth-2025-04-20`, called by the `/status` slash command; `five_hour` / `seven_day` / `seven_day_sonnet` (+ `seven_day_opus`) with `utilization` 0–100 and `resets_at` ISO 8601 (nullable); **new fact: "The flat `seven_day_*` keys above are all `null` now — the live quota data lives in a structured `limits` array, which ccusage reads"** — the design's "ignore `limits[]`" was based on stale information; the parser must read `limits[]` too. Also: verified Ultra accounts have no five-hour limit; `resets_at` may be null. **No source states a success rate** — the "one request in three succeeds" figure remains an uncited estimate (see C24). ccusage is "self-caching — refreshes from API when stale, no daemon needed", which supports the fetch-on-view shape (C3).

## Consolidated findings

### Architecture — the five changes that decide the rewrite

#### C1 — Probe first: a throwaway probe is a pre-implementation gate (Blocker)
* **Sources:** zoom-out B1; intermediate B5; design-critic M12/M13; style B2/M12.
* **Decision: PROPOSE FIX.** The design builds permanent architecture on two usage sources never observed working (Codex success shape never seen by Baton; Claude endpoint community-verified only). Add a probe gate **before** implementation starts: a throwaway script (not a subsystem), run by the orchestrator on the Mac where real logins live, that (1) runs `codex app-server` `initialize` + `account/rateLimits/read` against a real login and records the actual success response; (2) calls the Claude endpoint with a real token and records the actual response, including the `limits[]` structure, whether the flat keys are null, and whether the Opus/Sonnet splits are present and ever bind; (3) measures the Claude success rate over a short observation window (so the 15-minute cadence stops being a guess); (4) settles setup-token acceptance as a general fact (C12); (5) records whether the Claude response carries an account-identifying field; (6) records both CLI versions. Records go to `docs/designs/cli-autoimport-usage/probe/`. **Implementation does not start until the probe records exist.** The Codex request contract is now cited (see the protocol doc above), so the Codex wire code is writable from the design; the Claude response shape (`limits[]`) is still unknown and affects the parser, which is why the gate stands.
* **Design change:** new §9.0 "Probe (pre-implementation gate)" with the checklist; §10 references it; the Overview and §4 state the gate.

#### C2 — Drop the Claude token refresh entirely (Blocker)
* **Sources:** design-critic B1; intermediate B1.
* **Decision: PROPOSE FIX.** D8's in-memory-only refresh may destroy the CLI login it claims to protect: if Anthropic rotates refresh tokens, Baton consumes the CLI's refresh token and keeps the replacement only in memory, leaving the CLI's stored refresh token dead; the concurrent case (CLI refreshes at the same moment) yields `invalid_grant`. The design cites no evidence about rotation. **Delete the refresh path.** An expired token is `token_expired_refresh_failed` ("Sign in again in Claude Code"); the CLI, which owns the file, does the refresh. This deletes a whole adapter behaviour and moots comprehension B3 and readiness M4 (refresh call underspecified). The reason code stays, now meaning "expired — the CLI must refresh it".
* **Design change:** rewrite D8; §6.3 Claude adapter has no refresh step; §9 Claude bullet drops refresh; test list drops refresh-success cases, keeps refresh-failure → reason code as "expired token".

#### C3 — Fetch on view replaces the background refresher (Blocker)
* **Sources:** zoom-out B2/B3; intermediate B3; design-critic M1/M9; comprehension M2/M13/M14; structural B1.
* **Decision: PROPOSE FIX.** The numbers are only needed when the user opens the Connections page and looks at them — nothing in Baton acts on them. The background refresher (cadence, idle rule, backoff, circuit breaker, single-flight, cross-instance coordination, persisted retry state) is machinery for a pull question. Worse, the design contradicts itself: D6 says a page view triggers a fetch; §6.4 says render and poll "never trigger or wait on a fetch". **Pick one trigger model and delete the other:**
  * The Connections page render **may start a background fetch** (fire-and-forget, in a daemon thread) when the cached snapshot is absent or older than the cadence floor **and** the provider is enabled (C4). The render **never waits** on it and always serves the cached snapshot immediately.
  * The 15-second live poll **never triggers** a fetch; it serves cached usage only.
  * Manual Refresh starts a fetch subject to the same floor (C5).
  * **Deleted:** `refresher.py`, the idle rule, exponential backoff, the circuit breaker, cross-instance coordination beyond the timestamp check (C25), persisted `next_retry_at` / `consecutive_failures` / `fetch_started_at`. What remains of throttling: the cadence floor, `Retry-After` (C6), the kill switch.
  * The fetch threads follow the existing flow pattern (`CodexDeviceFlow` / `ClaudeSetupTokenFlow` watcher threads): a `UsageFetcher` held on `DashboardCore` (next to `self.codex_flow`, `self.claude_flow`, `self.server_stats`), constructed in `DashboardCore.__init__`, shut down from `shutdown_jobs` with a 5-second join bound (C16).
  * The cache file survives (per-provider, last-good snapshot only) so a restart does not lose the last fetched numbers — its only remaining job, stated plainly.
* **Design change:** rewrite D6/D7 and §6.4; delete `refresher.py` from §9; add `app.py` + `core.py` to modified files; §7 records the background refresher as evaluated and rejected, with reasons.

#### C4 — Per-provider opt-in via "Check usage"; consent note describes the network call (Major)
* **Sources:** zoom-out M9; design-critic M14; style M14.
* **Decision: PROPOSE FIX.** With fetch-on-view there is no background use of the token — the only use is the one the user triggers by opening the page or clicking Refresh. Make that explicit and opt-in: a provider that has never been enabled shows **"Check usage"** and **no fetch happens on view** until the user clicks it. The choice persists in the cache record (`enabled: true`). The consent note is rewritten to describe the actual act: "Opening this page checks usage with the provider. Baton sends your sign-in to <provider> for that check, only when you ask it to. It does not copy or store the sign-in. To stop Baton using it, sign out in the CLI." Plus the dual-credential sentence: "If a token saved by Baton is also present, that token stays in effect until you delete it in the dashboard." This answers the consent objection for the on-view model; see X1 for the contested dashboard toggle.
* **Design change:** new D18; §6.5 `not_checked_yet` row and §6.6 button naming; cache record gains `enabled`.

#### C5 — Manual Refresh floor equals the cadence floor (Blocker)
* **Sources:** structural B2; intermediate M2; comprehension M1; design-critic M16.
* **Decision: PROPOSE FIX.** D6 allowed a manual Claude refresh 5 minutes after the last attempt against a 15-minute floor ("4 requests/hour max") — up to 12/hour. **The manual-refresh floor is the cadence floor:** Claude 15 minutes, Codex 5 minutes between attempts, manual or otherwise. A manual click inside the floor is ignored (redirect + flash naming the wait). Whether a manual refresh resets the background clock is moot — there is no background clock (C3).
* **Design change:** rewrite D6; §9 route contract and tests carry the numbers.

#### C6 — 429 is routine, not a failure (Blocker)
* **Sources:** design-critic B4; intermediate B2; comprehension M3.
* **Decision: PROPOSE FIX.** A 15-minute cadence at the stated (unmeasured) one-in-three success rate means ~45 minutes to one successful refresh, and D7's backoff (15→30→60) is then the normal path, not the exception — while a snapshot goes stale at 60 minutes. **Treat 429 as routine:** honour `Retry-After` exactly when present (seconds or HTTP-date; absent means "wait at least the cadence floor"); a 429 does **not** count toward any failure tally and does **not** trigger backoff. Non-429 failures (network, timeout, unexpected shape) also simply wait for the next view/manual attempt after the floor — there is no backoff ladder and no circuit breaker, because the cadence floor already bounds the attempt rate. Write the expected-freshness arithmetic into the design: with fetch-on-view, a fetch on view succeeds unless the provider rate-limits; on 429 the card shows the reason and the next-retry time, and the last-good value is hidden once stale (C11).
* **Design change:** rewrite D7; §6.4 throttling paragraph; §3 acceptance criteria (C24).

#### C7 — 401/403 stop has a defined re-arm (Blocker)
* **Sources:** design-critic B2; structural M14; comprehension M3.
* **Decision: PROPOSE FIX.** After a 401/403 the adapter retries once, then stops **in process memory only** until (a) the detected login changes (kind or source path, per live detection), or (b) the user clicks Refresh (manual Refresh overrides the stop — the user just re-signed in and wants to check). A dashboard restart clears the stop (nothing about it is persisted). A transient 401 can no longer wedge usage permanently.
* **Design change:** rewrite D7; §6.5 `unauthorized` row; §9 tests.

#### C8 — One precedence function decides worker credential AND measured credential (Blocker)
* **Sources:** intermediate B4; structural M2; zoom-out M10; structural M15.
* **Decision: PROPOSE FIX.** The §6.2 tables are the **test list** for one function in `provider_auth.py` (extend `claude_code_auth` / `codex_auth` to return the resolution, or a new `resolve_provider_auth` — the design names the function and its return type in §6.1/§9). The function returns the effective credential, the set of present credentials, and the credential usage is measured against — **always the effective credential**. The D9 fallback (measure the CLI token when a saved token exists) is deleted with C12. The UI renders whatever the function returns; the adapter receives its token through the shared reader (see C14), not a second file read. The "values are never returned" rule is restated precisely: values are never returned **to the view, template, or logs**; the adapter, inside the trust boundary, reads them through the one shared reader.
* **Design change:** rewrite §6.2 intro + tables; §6.1 accessor contract; §9 `provider_auth.py` bullet.

#### C9 — Fingerprint: defined for Codex (accountId), dropped as a general concept (Blocker)
* **Sources:** design-critic B3; comprehension B8; structural M4; intermediate M4; zoom-out M6.
* **Decision: PROPOSE FIX.** The fingerprint dilemma (token material invalidates on every rotation; path-only misses account switches) resolves per provider from the evidence: the Codex response carries **`accountId`** (protocol doc, cited above). The Codex cache record carries `account_hash` = SHA-256 of `accountId`; a change discards the snapshot. Token rotation does not change `accountId`, so rotation never invalidates. For Claude, **no account-identifying input exists** (response carries no identity, none inferred — D13 stands). The Claude record carries no fingerprint, and the limitation is stated plainly: switching accounts in the CLI through the same file path is not detected; the cached usage belongs to the previous account until the next successful fetch replaces it. The bound: with fetch-on-view, the next page view after the floor refetches, and the snapshot always shows its `fetched_at`. The probe records whether the Claude response carries any account-identifying field (C1); if it does, the Claude record gains the same `account_hash`.
* **Design change:** rewrite D5; §6.7 invalidation rule; §9 cache bullet.

#### C10 — Two PRs: Codex first, Claude second (Major)
* **Sources:** zoom-out M3; design-critic M2; intermediate M9.
* **Decision: PROPOSE FIX.** D4's "independent adapters, one PR, one merge gate" couples the halves at merge time. **Split the delivery:** Phase A PR = Codex adapter + shared plumbing (model, cache, fetcher, UI, precedence, wording) — merge gate: Codex live on the Mac. Phase B PR = Claude adapter only — merge gate: Claude live on the Mac. Each PR is independently verifiable and mergeable; the kill switch stays as a runtime off-switch, not a merge-time one.
* **Design change:** rewrite D4; §9 split into §9.1 (Phase A) and §9.2 (Phase B); §10 gates per phase.

#### C11 — Stale value hidden, not shown with a label (Major)
* **Sources:** design-critic M3; intermediate M5.
* **Decision: PROPOSE FIX.** §4's principle ("a stale '72% left' the user trusts to start a long run is worse than no number") and §6.5's "same line, labelled stale" contradict each other. **Pick the principle's side:** when a snapshot is older than max-age, the value is hidden; the card shows "last seen <relative time> — check again before a long run" + Refresh. With fetch-on-view the user is one click from a fresh attempt, so a hidden stale value is a prompt, not a blank.
* **Design change:** §6.5 stale row; §4 principle now consistent with §6.5.

#### C12 — Setup-token one-shot dropped; probe settles acceptance as a general fact (Major)
* **Sources:** zoom-out M11; design-critic M11.
* **Decision: PROPOSE FIX.** "Tried exactly once per token" has no mechanism (no token identity, no storage field) and builds permanent logic for an unverified guess. **Delete the one-shot.** The probe (C1) settles whether the endpoint accepts `sk-ant-oat01-` setup tokens as a general fact. If accepted, setup tokens are treated as ordinary OAuth tokens. If rejected, the adapter never tries them, and the card shows reason `setup_token_rejected` ("Usage can't be read with a setup token. Use the CLI sign-in instead."). No per-token tracking exists.
* **Design change:** rewrite D9; §6.5 row wording; §9 tests (setup-token test becomes "rejected → reason recorded, no retry" only if the probe says rejected; if the probe says accepted, the test is "accepted → usage fetches succeed").

### Model and contract

#### C13 — Adapter rule restated; wording lives in one mapping per provider (Major)
* **Sources:** zoom-out M5; structural M1; comprehension B6; design-critic M7; intermediate M3.
* **Decision: PROPOSE FIX.** §6.1's absolute ban ("no provider-specific string, path, URL, or response key outside its adapter") is broken by the design's own UI copy. **Restate it:** provider response keys, URLs, and method names live only in the adapter. **Reason codes are provider-neutral.** The **human wording lives in one mapping per provider, inside that provider's adapter module** (reason code → UI wording + next step); the shared layer and template only pass and render reason codes. File paths shown on cards come from the accessor's returned values, not literals. The taxonomy becomes per-provider: each row carries Claude wording and Codex wording where they differ (C27).
* **Design change:** rewrite §6.1 rule; §6.5 table gains per-provider wording columns.

#### C14 — The data contract is written down (Blocker)
* **Sources:** readiness-implementation B2/B3; readiness-codebase B2/B3/B4; comprehension M9/M11/M12/M15; structural M11/M15.
* **Decision: PROPOSE FIX.** Write the contract in §6.1/§9 with exact names and types:
  * `UsageWindow(kind, used_percent, resets_at)` — `kind` derived by the adapter; `used_percent` float 0–100 as reported; `resets_at` datetime.
  * `UsageSnapshot(provider, windows, plan, fetched_at, source_version)`; `UsageUnavailable(provider, reason, fetched_at, last_good)` — the combined shape carrying the last-good windows plus the current failure reason (structural M3). `fetched_at` is always the last **successful** fetch; `last_attempt_at` is the last attempt.
  * `LoginSource(kind, source_path, cli_version)` — frozen dataclass; `kind` ∈ {`env_api_key`, `env_oauth_token`, `saved_oauth_token`, `cli_login`, `api_key_file`, `none`}; `cli_version` from one `<cmd> --version` run per dashboard process start (5 s timeout, cached in memory, `None` on failure).
  * `ProviderAuth` gains additive fields with defaults: `cli_version: str | None = None`, `usage: UsageSnapshot | UsageUnavailable | None = None`, `present: tuple[str, ...] = ()` (all present credential kinds), `source_path: str | None = None`. **`detected_at` is dropped** (zoom-out N1: it always reads "now").
  * Live-poll JSON: `usage` nested **inside each `providers[]` entry** with fields `{state, windows: [{kind, left_percent, resets_at}], fetched_at, reason, plan}`. `conn.js` updates `[data-provider-usage]` blocks per state.
  * Refresh route: `POST /connections/providers/{provider}/usage/refresh`, provider ∈ {`claude_code`, `codex`}; response via the dashboard's `act()` redirect + flash; rate-limited → redirect + flash naming the wait; CSRF as for other dashboard POSTs; registered in `app.py`.
  * Token values: the adapter reads them through **one shared reader** in `provider_auth.py` (the existing credential-file reader, generalised) — detection checks presence through it, the adapter reads values through it. No second file reader anywhere.
* **Design change:** rewrite §6.1 (model + accessor + carrier); §6.4 (poll JSON); §9 file bullets and test list.

#### C15 — Config paths corrected; kill switch fully specified (Blocker)
* **Sources:** readiness-implementation B1; readiness-codebase M1; design-critic M8; intermediate M13; comprehension B1/B7.
* **Decision: PROPOSE FIX.** Real paths: `src/jev_agent/config/schema.py` + `src/jev_agent/config/defaults.py`. Model: `UsageConfig` with `claude_code: bool = True`, `codex: bool = True` (canonical keys are `claude_code`/`codex`, not bare `claude`). `JevConfig.usage: UsageConfig`. Register in `TOP_SECTIONS` (`src/jev_agent/app/settings.py`). Commented template lines in `DEFAULT_CONFIG_TOML`. `JevConfig` is `_Strict` (`extra="forbid"`), so `[usage]` fails validation until the schema change lands — stated. **Flipping the switch needs a dashboard restart** (config is read at start); "no code change, no redeploy" stays, "no restart" is not claimed. Kill switch **stops fetch attempts**, not just rendering. `disabled_by_config` added to the taxonomy with wording + matrix row + tests. Re-enabling resets nothing persisted (nothing about the stop is persisted — C7).
* **Design change:** rewrite §6.9; §9 config bullet with real paths + model + TOP_SECTIONS; `settings.py` added to modified files.

#### C16 — Fetcher lifecycle located (Blocker; refresher part moot by C3)
* **Sources:** readiness-implementation B4; readiness-codebase M2; zoom-out M2; comprehension M13.
* **Decision: PROPOSE FIX.** The `UsageFetcher` is held on `DashboardCore` (`self.usage_fetcher`, next to `self.codex_flow`, `self.claude_flow`, `self.server_stats` at `core.py:111–113`), constructed in `DashboardCore.__init__`, shut down from `shutdown_jobs` (`core.py:218–239`) with a 5-second join bound, after which daemon threads are abandoned. No new thread pattern — it follows the flow pattern. The refresh route registers in `app.py` (route table lines 257–299). `app.py` and `core.py` added to §9 modified files.
* **Design change:** §9 modified files + fetcher bullet; §6.4 lifecycle paragraph.

#### C17 — State machine defined; stale is read-time; timestamps named (Major)
* **Sources:** comprehension M5/M6; structural M9; style M13; intermediate M14.
* **Decision: PROPOSE FIX.** Stored states: `never` / `collecting` / `ready` / `failed` — exactly the `ServerStatsCache` set; **`stale` is not stored**, it is a read-time age label. Display derivation (one rule, in §6.4):
  * no snapshot + no fetch running + never attempted → `never` ("Usage not checked yet" + "Check usage").
  * no snapshot + fetch running → `collecting` ("Checking…" + no value).
  * no snapshot + last attempt failed → `failed` ("Usage unavailable — <reason>" + next step + Refresh).
  * snapshot + fetch running → `collecting` (last value with its age + "Checking…").
  * snapshot + age ≤ max-age → `ready` (value + "updated <relative>").
  * snapshot + age > max-age → `stale` (value hidden per C11; "last seen <relative> — check again before a long run" + Refresh).
  * A failed refetch never overwrites the snapshot; it sets `last_reason` + `last_attempt_at`, shown as a sub-line under a still-young snapshot ("last check failed — <reason>").
  * `last_status` vocabulary: {`ok`, `rate_limited`, `auth_error`, `spawn_error`, `unexpected`, `disabled`}. `fetched_at` = last success; `last_attempt_at` = last attempt. `collecting` row added to the §6.5 matrix.
* **Design change:** rewrite §6.4 state paragraph; §6.5 matrix gains `collecting` row; §6.1 model (`last_good` in `UsageUnavailable`, C14).

#### C18 — Window mapping rules for unexpected values (Major)
* **Sources:** comprehension B5; design-critic M6; structural M10.
* **Decision: PROPOSE FIX.** The mapping is a table **in the adapter** (response keys live in the adapter — C13): Codex `windowDurationMins` 300 → session, 10080 → weekly; Claude `five_hour` → session, `seven_day` → weekly, `seven_day_opus` → weekly_opus, `seven_day_sonnet` → weekly_sonnet (C19). Rules: unknown duration → `unexpected_response` (the provider changed its metering; a partial picture is worse than "couldn't be read"); `primary` null → `unexpected_response`; duplicate durations → `unexpected_response`; Claude with only one window present → show the one present (partial data is fine — the missing window's row is absent, not fabricated); neither present → `unexpected_response`; `resets_at`/`resetsAt` in the past → render "resetting…" and keep the bar at its last value until the next fetch.
* **Design change:** §6.3 per-provider mapping tables; §9 tests.

#### C19 — Opus/Sonnet splits are windows; the binding one is shown (Major)
* **Sources:** design-critic M4; intermediate M6.
* **Decision: PROPOSE FIX.** The design discarded the splits as "no decision value" without evidence. The splits **are** windows: the adapter includes `seven_day_opus` / `seven_day_sonnet` in the snapshot when present and non-null. The card's Weekly line shows the **lowest remaining** among the weekly windows, labelled with which one it is when it is a split ("Weekly (Opus) 45% left"). The `limits[]` array is parsed too (flat keys may be null now — see the ccusage finding above); its structure is recorded by the probe (C1), and the parser prefers the structured array when the flat keys are null. The window list in the model is now justified by the splits (contest X4 below).
* **Design change:** §6.3 Claude parser; §6.1 model comment; §6.6 Weekly line; §9 tests.

#### C20 — Precedence table fixes (Major)
* **Sources:** structural M7/M12/M15/M17; comprehension M8/M17; design-critic M17.
* **Decision: PROPOSE FIX.** (a) Columns renamed: "Env `ANTHROPIC_API_KEY`" / "Saved `CLAUDE_CODE_OAUTH_TOKEN`" (secrets.env) / "CLI login" — env vars are **not** "Saved by Baton" (§1 definition stands). Same for Codex. (b) Within `auth.json`, `tokens` takes precedence over `api_key` (matching existing detection order; pinned by test). (c) Bare-mode rows completed: bare + saved token and bare + API key show usage (those are the working paths); bare + CLI login suppresses usage (D11); **Codex has no bare mode** — stated explicitly. (d) Codex label split dropped: the dashboard's own device-code flow writes tokens into the same `~/.codex/auth.json` detection reads, so a dashboard-created Codex login is indistinguishable by file location — the Codex card uses the detected-login label for any `auth.json` tokens login, and there is no "saved by Baton" Codex label. (e) "Also present" lines get their data source: `ProviderAuth.present` (C14).
* **Design change:** rewrite §6.2 tables; D1 amended for Codex.

#### C21 — Codex protocol contract cited and specified (Major)
* **Sources:** comprehension B4; design-critic M12/M15.
* **Decision: PROPOSE FIX, grounded by the fetched protocol doc.** Cite `https://github.com/prontsevich/codex-limitbar/blob/HEAD/skills/codex-limitbar/references/rate-limits-protocol.md` (verified against codex-cli 0.147.0–0.158.0-alpha.2.1). Specify: spawn `codex app-server --listen stdio://` with temp-dir cwd (created by the adapter, deleted in `finally`), env preserved = `HOME`, `PATH`, `TMPDIR`, `LANG`, `LC_ALL` only (all `*_API_KEY`/`*_TOKEN` vars removed); binary resolved via `resolve_executable` (PATH lookup); JSONL over stdio; `initialize` id 1 with params `{"clientInfo":{"name":"jev","version":"<package version>"}}` only, no `protocolVersion`; wait for `{"id":1,"result":{...}}`, skipping any line with `method` and no matching id; send `{"method":"initialized","params":{}}`; send `{"method":"account/rateLimits/read","id":2}` (no params); read until the line with `"id":2`; parse `result.rateLimits` (ignore `rateLimitsByLimitId` duplicate); 15 s timeout; SIGTERM on completion, SIGKILL after a 5 s grace; 4 MiB output cap — exceeding it kills the process, reason `app_server_failed`; `planType` values (`plus`/`pro`/etc.) rendered verbatim, unknown values verbatim; `accountId` hashed for the cache record (C9); `rateLimitResetCredits` and `rateLimitUpsell` ignored. Do not drive the TUI. The probe (C1) confirms the success shape live.
* **Design change:** rewrite §6.3 Codex subsection with the citation; §9 Codex bullet carries the values.

#### C22 — Claude credential fields and `limits[]` (Blocker, partially probe-gated)
* **Sources:** comprehension B2; ccusage finding (flat keys null now).
* **Decision: PROPOSE FIX.** The access-token and refresh-token field names inside `claudeAiOauth` and the `limits[]` structure are **recorded by the probe** (C1) before implementation; the design states the read path (`claudeAiOauth` object in `~/.claude/.credentials.json`, via the shared reader — C14) and the parser rule (read `five_hour`/`seven_day`/splits when present and non-null; parse `limits[]` when the flat keys are null; the probe records the array's structure). With refresh dropped (C2), only the access token and `expiresAt` are read.
* **Design change:** §6.3 Claude subsection; §9.0 probe checklist; §9 Claude bullet.

### Wording and style

#### C23 — The "imported" ban is fixed, not just claimed (Blocker)
* **Sources:** style B1; readiness-style M1.
* **Decision: PROPOSE FIX.** Title → "Detect Claude Code / Codex CLI logins + show account usage (weekly and session)". §6.2 heading → "Detected-login state and the precedence table". §6.9 "recorded with the imported state" → "recorded with the detected-login state". §7 "imported" uses → the rejected-alternative wording. §9 "import metadata" → "detection metadata". The §1 sentence becomes: "The UI does not use 'imported'. This document uses 'import' only when it names the rejected alternative in §7."
* **Design change:** edits throughout; SITEMAP entry wording updated to match.

#### C24 — Numbers cited or labelled; freshness arithmetic; success metrics (Major)
* **Sources:** style B2/M12; design-critic M13; intermediate B5.
* **Decision: PROPOSE FIX.** (a) "~480 requests/hour" shown with its arithmetic (15 s poll × 2 tabs × 3600 s). (b) "One request in three succeeds" labelled as an **unmeasured estimate from community reports** (ccusage, claude-statusline, tclaude — no measured rate in any cited source); the probe (C1) measures it. (c) "A few percent per 15 minutes" deleted — no source. (d) The 15-minute cadence is labelled a **starting value, to be checked at Mac verification**. (e) Expected-freshness arithmetic written into §6.4 for the fetch-on-view model (C6). (f) **Acceptance criteria added to §3:** a fetch on view returns a fresh snapshot unless the provider rate-limits (then the reason + next-retry time show); a 429 never produces a wrong number; usage failure never changes the login state shown; no token value appears in logs, cache, or UI (test-enforced).
* **Design change:** §4, §5 D6, §6.3, §6.4, §3.

#### C25 — Cross-instance single-flight: timestamp check, per-provider files (Blocker)
* **Sources:** style B3; readiness-implementation B5; readiness-style B2; zoom-out M4; design-critic M5; intermediate M7.
* **Decision: PROPOSE FIX.** **Pick the timestamp check; no file lock in this issue.** One cache file per provider (`~/.jev/usage-cache-claude_code.json`, `~/.jev/usage-cache-codex.json`) — this kills the lost-update race (each process writes only its own provider's file; a concurrent write of the same file is last-writer-wins on the same shape). A process starts a fetch only if the cached snapshot is older than the floor; in-process single-flight follows the `ServerStatsCache` pattern (second `refresh()` returns False while one runs). The "file lock is correct" sentence is deleted, not hedged.
* **Design change:** rewrite D5 + §6.4 cross-instance paragraph; §9 cache bullet + tests.

#### C26 — Cache reuse rationale stated (Major)
* **Sources:** zoom-out M1; readiness-codebase M3; intermediate M7.
* **Decision: PROPOSE FIX.** Write the concrete reason in §6.4: `ServerStatsCache` entries are typed `ServerStats` (mounts, top CPU/memory) keyed by server name under a per-repo `state_dir`; usage snapshots are a different type keyed by provider under the host-level `~/.jev/`. Generalising its type parameter and home would change a working, tested component's contract for no benefit. The new cache **reuses the pattern** (get-never-blocks, single-flight refresh returning False, JSON persistence, last-good on failure, states `never`/`collecting`/`ready`/`failed`) but not the class. Also note the one atomic-write precedent to reuse: `write_secret` in `src/jev_agent/core/secrets.py` (`os.replace` of a temp file, mode 0600).
* **Design change:** §6.4 cache paragraph; §9 cache bullet (reuse `write_secret`'s approach).

#### C27 — UI copy consistency (Majors + Nits)
* **Sources:** style M1–M8, N4–N9; structural N3/N4/N6/N7; comprehension M7, N1–N7.
* **Decision: PROPOSE FIX.** (a) Fixed UI-copy table in §6.2/§6.5: one exact string per state per provider; `login` only for the credential, `sign-in` only for the action. (b) Window labels are **Session** and **Weekly** only in the UI; `five_hour`/`seven_day` named only as response fields; Codex mapping written once in one unit (300 min = 5 hours → Session; 10080 min = 7 days → Weekly). (c) One term for the number: **"left"** ("62% left"); "allowance" and "remaining usage" deleted. (d) One button: **"Check usage"** before the first fetch, **"Refresh usage"** after it; while running it reads **"Checking…"**. (e) One timestamp format: **relative only** ("updated 12 min ago", "last seen 2 h ago"); no `HH:MM` clock times anywhere. Rounding rule: largest unit keeping the number ≥ 1, rounded down ("9 min", "2 h 14 min", "3 d", "just now" under a minute); percent shown as an integer (nearest); the bar fills to **remaining**. (f) "Sign in again" in one wording: "Sign in again in Claude Code. If that does not work, use the dashboard sign-in flow." (g) §1 corrected for the Codex secondary-window exception. (h) "Not connected" used everywhere ("Not logged in" deleted). (i) The "source note" in the disclosure defined: source path + `fetched_at` + provider. (j) Sub-hour resets: "resets in 9 min". (k) Quoted UI strings use one style (sentence case, em-dash separator in "Detected on this device" strings, no trailing period unless a full sentence).
* **Design change:** §1 glossary (new entries: single-flight, kill switch, background fetch, mode 600, atomic write, JSON-RPC, app-server, opt-in); §5 D1/D14/D16; §6.5 table + matrix; §6.6.

#### C28 — Bare mode completed; Keychain scope stated in the goals (Major)
* **Sources:** zoom-out M7/M8; intermediate M12; comprehension M17; design-critic M17; structural M7/M12.
* **Decision: PROPOSE FIX (scope) + CONTEST (worker fix / Keychain read — see X2/X3).** (a) Precedence tables completed for bare mode × saved token and bare mode × API key (C20); Codex has no bare mode — stated. (b) In bare mode the card shows the CLI login as detected **and** names the effective credential workers actually use; usage is shown only for the effective credential. (c) **§3 goals now state plainly: "Mac detection is file-only and best-effort"** — the Keychain limitation is visible at the top of the design, not in a decision footnote. The card names the file checked (`~/.claude/.credentials.json` and `~/.codex/auth.json`).
* **Design change:** §3 goals; §6.2 tables; D10/D11 wording.

#### C29 — Small fixes (Nits and minor Majors)
* **Sources:** design-critic N1–N7; comprehension N1–N7; style N1–N9; structural N1–N8; zoom-out N1–N6; intermediate N1–N6.
* **Decision: PROPOSE FIX.** "Detected at" dropped (C14). "Mostly" deleted. "Correct for free" → "cannot go stale when the user signs out in a terminal". D17's claim softened: the rule is enforced by a test ("no locally computed remaining appears in the snapshot"), not by the model shape. Max-age basis stated: **4 × cadence floor** (Claude 60 min, Codex 20 min — changed from 30). Overview corrected ("the windows the provider reports" — Codex may report one). §1 snapshot definition reconciled with the model (snapshot = windows + fetched_at; outcome lives in `UsageUnavailable`). Codex/Claude card copy matched in form. `no_login` and `not_applicable_api_key` moved out of the fetch taxonomy into the state matrix (render states, not fetch outcomes). `disabled_by_config` added to the taxonomy (C15). `unauthorized` covers 401 **and** 403 (stated). New code `endpoint_changed` for 404/410 (sunset trigger). First-fetch timing stated: dashboard start triggers **nothing**; the first fetch happens on the first enabled view or Refresh. Cache schema fully specified (§6.4): `schema_version: 1`, field types, corrupt file → treated as empty (logged once), `~/.jev/` created if missing (mode 700), temp file in the same directory + rename, mode 600. `unsupported_version` **dropped** from the taxonomy — no version range is enforced in this issue; versions are recorded for diagnosability only (comprehension M10). `planType` rendered verbatim (unknown values verbatim). The `auth.json` `api_key` provenance parenthesis ("stored by `codex login --with-api-key`") deleted as unverified. "Refresh-on-view" for Codex deleted (C3 replaces it). Kill-switch re-enable: nothing persisted, so re-enabling just works on the next view. Shutdown bound: 5 s join, then daemon threads abandoned. Assumption stated: the dashboard and the CLI run as the same user; otherwise detection reads fail and the card shows "not connected". Clock skew: one line — reset times are provider `resets_at` minus the host clock; no correction attempted. PTY and web-scrape rejections in §7 get a full reason each, not one line. The `limits[]` note and the backend-API Codex alternative (`/backend-api/codex/usage`, needs a ChatGPT session token — not adopted) added to §7.
* **Design change:** edits throughout §§1–10.

#### C30 — §9 and §10 rewritten with values, phases, and pass conditions (Blockers)
* **Sources:** readiness-implementation (all findings); readiness-codebase (all findings); readiness-style (all findings).
* **Decision: PROPOSE FIX.** §9 is rewritten as: §9.0 probe gate (C1); §9.1 Phase A (Codex PR) and §9.2 Phase B (Claude PR) with checkpoints (C10); every file bullet carries its values (URLs, timeouts, field names, numbers) instead of noun lists; corrected paths (`src/jev_agent/config/schema.py`, `src/jev_agent/config/defaults.py`, `src/jev_agent/app/settings.py`, `src/jev_agent/dashboard/app.py`, `src/jev_agent/dashboard/core.py`); added files (`tests/unit/test_dashboard_connections.py`, `tests/unit/test_dashboard_providers.py`); SITEMAP line removed (already done — verify only); screenshots script named (`examples/demo/screenshots.py`); test list with a reason code and observable pass condition per case, including kill-switch tests, token-leak tests (no token value in logs/cache/UI), fake-clock seam named, shutdown test, lifecycle test (dashboard start creates the fetcher; shutdown calls `shutdown()`), 403 tests, `Retry-After` absent/unparsable tests, unknown-duration tests. §10 gates fixed: gate (2) passes with one window + the exact copy when `secondary` is null; gate (1) compares against `/usage` in Claude Code at the same time; items (3)–(6) state outcomes and artifacts (PR body); orchestrator verification is a per-item checklist with the ≥85 self-score threshold stated; Mac verification items note what is unit-proven-only when a 429 is not observed live.
* **Design change:** full §9/§10 rewrite.

### Contested items (CONTEST — not fixed, with rationale)

#### X1 — Dashboard on/off toggle for usage in this issue
* **Sources:** zoom-out M9; design-critic M14 (toggle half).
* **Decision: CONTEST.** The toggle demand assumed background use of the token. With fetch-on-view (C3) + per-provider opt-in via "Check usage" (C4), there is **no background use to opt out of**: the only fetch is the one the user triggers by enabling and opening the page or clicking Refresh, and the consent note now describes it. A Settings toggle would be a second control for a use the user already triggered. The kill switch (`[usage]` config, restart to apply) remains the admin off control. **Follow-up (named):** a dashboard usage toggle in Settings, if the user asks for it. The user corrects this pick after the fact if wrong.

#### X2 — Bare-mode worker fix in this issue
* **Sources:** zoom-out M7; intermediate M12 (worker-fix half).
* **Decision: CONTEST.** Fixing bare-mode workers to read the CLI login is worker-environment plumbing in a different subsystem (`workers/` env construction), not a Connections-page change. This issue makes the card honest about it (C28): the card names the effective credential and shows usage only for it. The worker fix is a named follow-up with its own tracking issue to be filed.

#### X3 — macOS Keychain read in this issue
* **Sources:** zoom-out M8; intermediate M12 (Keychain half).
* **Decision: CONTEST.** A Keychain read needs its own permission UX: an existence check may prompt, and reading the token value certainly can. Building that inside this issue couples a platform-permission design to a usage-display feature. The limitation is now stated in §3 (C28) and the card names the file checked. **Follow-up (named):** Keychain detection + usage, with a permission design.

#### X4 — Two-field window model instead of the list
* **Sources:** zoom-out N4; intermediate N2.
* **Decision: CONTEST.** The list is now justified: Claude returns up to four windows (headline + Opus/Sonnet splits, C19) and Codex returns one or two derived from durations (C18). A two-field model cannot carry the splits. The "never hardcoded" rule is restated honestly (C13/C18): response keys live in the adapter; the kind mapping is a table in the adapter with an explicit rule for unknown values.

## Traceability matrix

| Consolidated | Raw findings |
|---|---|
| C1 | Z5-B1; Z8-B5; Dsgn-M12, M13; Sty-B2, M12; RdySty-M12 (probe need) |
| C2 | Dsgn-B1; Z8-B1; Comp-B3 (moot); RdyImp-M4 (moot) |
| C3 | Z5-B2, B3; Z8-B3; Dsgn-M1, M9; Comp-M2, M13, M14; Str-B1; RdyImp-B4, B6, M2; RdyCode-M2, M4 |
| C4 | Z5-M9; Dsgn-M14; Sty-M14 (consent half) |
| C5 | Str-B2; Z8-M2; Comp-M1; Dsgn-M16 |
| C6 | Dsgn-B4; Z8-B2; Comp-M3 (429 half) |
| C7 | Dsgn-B2; Str-M14; Comp-M3 (401 half) |
| C8 | Z8-B4; Str-M2, M15; Z5-M10; Comp-M8, M9 |
| C9 | Dsgn-B3; Comp-B8; Str-M4; Z8-M4; Z5-M6 |
| C10 | Z5-M3; Dsgn-M2; Z8-M9; RdyImp-M10 |
| C11 | Dsgn-M3; Z8-M5 |
| C12 | Z5-M11; Dsgn-M11 |
| C13 | Z5-M5; Str-M1; Comp-B6; Dsgn-M7; Z8-M3 |
| C14 | RdyImp-B2, B3; RdyCode-B2, B3, B4; Comp-M9, M11, M12, M15; Str-M11, M3; Z8-M11 |
| C15 | RdyImp-B1; RdyCode-M1; Dsgn-M8; Z8-M13; Comp-B1, B7 |
| C16 | RdyImp-B4; RdyCode-M2; Z5-M2; Comp-M13 |
| C17 | Comp-M5, M6; Str-M9; Sty-M13; Z8-M14 |
| C18 | Comp-B5; Dsgn-M6; Str-M10 |
| C19 | Dsgn-M4; Z8-M6 |
| C20 | Str-M7, M12, M15, M17; Comp-M8, M17; Dsgn-M17 |
| C21 | Comp-B4; Dsgn-M12, M15 |
| C22 | Comp-B2; ccusage `limits[]` finding (resolve evidence) |
| C23 | Sty-B1; RdySty-M1 |
| C24 | Sty-B2, M12; Dsgn-M13; Z8-B5 |
| C25 | Sty-B3; RdyImp-B5; RdySty-B2; Z5-M4; Dsgn-M5; Z8-M7 |
| C26 | Z5-M1; RdyCode-M3; Z8-M7 |
| C27 | Sty-M1–M8, N4–N9; Str-N3, N4, N6, N7; Comp-M7, N1–N7 |
| C28 | Z5-M7, M8; Z8-M12; Comp-M17; Dsgn-M17; Str-M7, M12 |
| C29 | Dsgn-N1–N7; Comp-N1–N7; Sty-N1–N9; Str-N1–N8; Z5-N1–N6; Z8-N1–N6 |
| C30 | RdyImp (all); RdyCode (all); RdySty (all) |
| X1 | Z5-M9, Dsgn-M14 (toggle half) — contested |
| X2 | Z5-M7, Z8-M12 (worker-fix half) — contested |
| X3 | Z5-M8, Z8-M12 (Keychain half) — contested |
| X4 | Z5-N4, Z8-N2 — contested |

Key: Z5 = zoom-out (Stage 5), Z8 = intermediate zoom-out (Stage 8), Dsgn = design critic (Stage 7), Str = structural (Stage 7), Sty = style (Stage 7), Comp = comprehension (Stage 6), RdyImp = readiness-implementation (Stage 9), RdyCode = readiness-codebase (Stage 9), RdySty = readiness-style (Stage 9).

## What the resolve pass changes in the design

The rewritten design (`design.md`, amended after this report):

1. **Architecture:** fetch-on-view replaces the background refresher (C3); token refresh deleted (C2); 429 is routine (C6); manual floor = cadence floor (C5); 401/403 re-arm defined (C7); per-provider opt-in (C4); two PRs, Codex first (C10); probe gate before implementation (C1).
2. **Model:** combined `UsageUnavailable` with `last_good` (C14/C17); `ProviderAuth` additive fields named and typed, `detected_at` dropped (C14); per-provider wording mapping in the adapters (C13); fingerprint replaced by Codex `account_hash`, Claude limitation documented (C9); cache is one file per provider, timestamp check only (C25).
3. **Contract:** full dataclass/JSON/route/template contract written down (C14); config paths corrected and kill switch specified (C15); Codex protocol cited and specified (C21); Claude fields + `limits[]` probe-gated (C22); window rules for unexpected values (C18); splits shown as the binding window (C19); precedence tables fixed (C20).
4. **Wording:** "imported" ban actually applied (C23); one term per thing, one button, one timestamp format, rounding rules (C27); numbers cited or labelled estimates, acceptance criteria added (C24); stale value hidden (C11).
5. **Plan:** §9/§10 rewritten with values, phases, correct paths, reason-coded tests with pass conditions, and fixed Mac gates (C30).

## Re-audit

One re-audit (max 1 per stage, per the review protocol) follows the rewrite: three fresh zero-context reviewers (zoom-out on the full file, comprehension on the full file, implementation-readiness on §9–§10) check that the consolidated items above are actually in the text. Their reports go to `review/re-audit-zoom-out.md`, `review/re-audit-comprehension.md`, `review/re-audit-readiness.md`.
