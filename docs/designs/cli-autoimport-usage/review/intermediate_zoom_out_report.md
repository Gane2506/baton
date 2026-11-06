# Intermediate Zoom Out Review — CLI auto-import + usage (Stage 8, full file)

* Reviewer: Skeptical Architect (Zoom Out Goldfish), zero prior context
* Design reviewed: `docs/designs/cli-autoimport-usage/design.md` (814 lines, status Draft)
* Standard: `zoom-out-standard.md` — root problem, simplicity/YAGNI, anti-weeds, leverage existing
* Builds on: Stage 5 `zoom_out_report.md`, Stage 6 `comprehension_report.md`, Stage 7 `critic_design.md`, `critic_structural.md`, `critic_style.md`
* Verdict: **still not approved, and the case against the current shape is now stronger, not weaker.** The design document is unchanged since Stage 5 — none of the Stage 5 findings have been mitigated in the text — and the Comprehension and Critic stages add three new architecture-level problems Stage 5 missed (refresh-token rotation, usage measured against a credential the workers do not use, and the Claude card being stale by the design's own numbers). They also show the background-refresher architecture is not just heavy but self-contradictory: the design now gives opposite instructions on whether a page view can start a fetch.

## What changed since Stage 5

Nothing in the design changed. The 814-line document is the same one Stage 5 reviewed, so every Stage 5 blocker and major is still open. What changed is the evidence:

* Three independent lenses now agree on the same structural defects — the adapter rule, the fingerprint, the throttling layers, the idle rule, the one-PR/two-provider coupling. Agreement across lenses means these are defects in the design, not one reviewer's reading of it.
* The Critic stages found new problems at architecture altitude that Stage 5 did not see. Those are the findings in this report's Blocker section.
* The Comprehension stage found mostly detail-level gaps (field names, wire formats, route contracts). Those do not change the architecture, but they confirm the Stage 5 diagnosis: the design was written before either source was observed, so the contracts between its parts are guesses. Detail gaps are listed separately below and not re-litigated.

## Blockers

### B1 — The token-refresh path can destroy the CLI login it claims to protect (new since Stage 5)
* **Severity:** Blocker
* **Sections:** §5 D8, §6.3, §6.7
* **Source:** Critic (design) B1
* **Problem:** D8 refreshes the CLI's OAuth token in memory and never writes it back, on the grounds that two writers could corrupt the credentials file. The design never addresses refresh-token rotation: if Anthropic rotates the refresh token on use, Baton consumes the CLI's refresh token and keeps the replacement only in process memory, leaving the CLI's stored refresh token dead. The next CLI refresh then fails and the user's login is broken until they sign in again. The concurrent case is also unaddressed: the CLI refreshes at the same moment Baton does, one wins, the other gets `invalid_grant`. The central safety claim of D8 is therefore unproven and possibly backwards — the refresh path itself may be what destroys the login. The design cites no evidence about whether Anthropic rotates refresh tokens.
* **Architecture change required:** drop the refresh path from this issue. Treat an expired token as expired (`token_expired_refresh_failed`) and let the CLI, which owns the file, do the refresh. If refresh is kept, the design must specify rotation semantics, the concurrent-refresh outcome, and a test proving the CLI login still works after Baton refreshes. This is not a detail fix: it removes a whole adapter behaviour and the reason code built around it.

### B2 — The design's own numbers say the Claude card will usually be stale (new since Stage 5)
* **Severity:** Blocker
* **Sections:** §5 D6, §5 D7, §6.4
* **Source:** Critic (design) B4
* **Problem:** A 15-minute cadence with a stated one-in-three success rate means the expected time to one successful refresh is about 45 minutes. Two consecutive failures happen about four times in nine under that rate, so D7's exponential backoff (15 → 30 → 60 minutes) is the normal path, not the exception — and after two failures the next attempt is an hour away, while §6.4 declares a snapshot stale after 60 minutes. During exactly the heavy-use sessions when the user most wants the number, the card will spend most of its time stale or unavailable, and the backoff schedule guarantees it stays that way. D6 assumes failures are rare; D7 assumes they are exceptional enough to punish with an hour-long wait; the stated success rate says they are routine. The design never computes expected freshness, so the contradiction is never noticed.
* **Architecture change required:** compute expected freshness from the stated success rate and write the result into the design. Treat 429 as a routine outcome with its own short schedule (honour `Retry-After`, do not count it toward consecutive failures) instead of feeding it into the failure backoff. If the computed freshness is still poor, say plainly that the Claude half is best-effort and usually stale — or drop the background refresher for Claude and fetch on demand. This also strengthens Stage 5 B2: a background job whose output is stale most of the time is worse than no background job.

### B3 — The background refresher is now self-contradictory, not just heavy (Stage 5 B2, confirmed and worsened)
* **Severity:** Blocker
* **Sections:** §5 D6 vs §6.4
* **Source:** Stage 5 B2; Critic (structural) B1; Critic (design) M1, M9; Comprehension M2, M14
* **Problem:** Stage 5 said the refresher was unnecessary because the numbers are only needed when the user looks at them. The later stages show it is also incoherent as specified. D6 says "a page view triggers a fetch only when the cache is older than the interval." §6.4 says twice that the render path and the live poll "never trigger or wait on a fetch" and "serve cached usage only". An implementer cannot satisfy both sentences, and the §8 test ("render serves cached usage only") sides with §6.4, which leaves D6's page-view trigger and the idle rule with no trigger at all. The idle rule is separately undefined in three reports: nothing defines what counts as "viewed", who records it, or where it lives — and if the 15-second live poll counts as a view, the rule never engages while any tab is open and is dead text. Critic (design) M1 adds that the on-demand-only alternative — fetch when the page opens or Refresh is pressed, show nothing otherwise — is missing from §7 entirely, although it would delete the refresher, the idle rule, the cross-process race, and most of D7.
* **Architecture change required:** pick one trigger model and delete the other. The simpler shape remains fetch-on-view: delete `refresher.py`, the idle rule, the circuit breaker, and the cross-instance handling; keep the cadence floor, `Retry-After`, and the failure taxonomy. If a background job survives, §7 must evaluate the on-demand alternative with an honest reason for rejecting it, and "viewed" must be defined operationally.

### B4 — Usage is measured against a credential the workers do not use (new since Stage 5)
* **Severity:** Blocker
* **Sections:** §6.2 (Claude table, row 2) vs §5 D9
* **Source:** Critic (structural) M2
* **Problem:** With a saved OAuth token present, workers use the saved token — but the table says usage is measured against the saved token "if the endpoint accepts it (D9)", and D9 says if a CLI login also exists, the CLI access token is used instead. So when both exist, workers spend the saved token's allowance while the card shows the CLI login's remaining allowance. The numbers on the card would not be the numbers that govern the run the user is deciding about — the exact decision the Overview says the feature exists to support. This also breaks the section's own default rule ("Baton never silently replaces or hides a saved credential") in the usage dimension: the saved credential is the one in effect, and its usage is the one hidden.
* **Architecture change required:** one precedence function, stated once, that decides both which credential workers use and which credential usage is measured against, for every combination of credentials. The precedence table must be the test list for that function, not prose the view, route, and adapter each reproduce by hand (Stage 5 M10, still open). The D9 setup-token branch must also say where its "tried once" flag lives — the D5 cache-record field list has no such field, so a restart either re-tries it or the memory exists in a field the design never defines.

### B5 — The sources are still unverified, and the Codex half now rests on an unverified method name (Stage 5 B1, confirmed and extended)
* **Severity:** Blocker
* **Sections:** §6.3, §10, §5 D4
* **Source:** Stage 5 B1; Critic (design) M12; Critic (design) M13; Critic (style) B2, M12
* **Problem:** Nothing has changed since Stage 5: the Codex success shape has never been observed by Baton, the Claude endpoint is verified only by unnamed "independent community projects" with no link, project name, date, or response sample, and the full permanent architecture (five modules, cache file, scheduler, 12-code taxonomy, state machine, kill switches, sunset rule) is still built before a single successful fetch. The Critic stages add two points. First, the Codex request itself is unverified: the method name `account/rateLimits/read`, the initialize-then-read sequence, the stdio framing, and the field names come from no cited source, and the only fallback (reading the `rate_limits` snapshot from `~/.codex/sessions`) is deferred to a later issue with its freshness unverified — so a wrong method name zeroes out the whole Codex half. Second, the numbers that justify the cadence are uncited: "roughly one request in three succeeds" and "a few percent per 15 minutes even in heavy use" have no measurement, log, or date behind them, and the cadence, staleness threshold, and freshness argument all rest on them.
* **Architecture change required:** probe first, as Stage 5 asked — a throwaway script, not a subsystem, that runs `codex app-server` against a real login and calls the Claude endpoint with a real token, and records the actual responses, including the method name's source and the success rate actually observed. Only the sources that answer get permanent architecture. The 15-minute cadence should be labelled a starting value to be checked at Mac verification until a measured rate exists.

---

## Majors

### M1 — Seven throttling mechanisms, still with no defined interaction (Stage 5 B3, confirmed)
* **Severity:** Major
* **Sections:** §5 D6, §5 D7, §6.4, §6.9
* **Source:** Stage 5 B3; Comprehension M3; Critic (structural) M14
* **Problem:** Stage 5 counted seven brakes (cadence floor, idle rule, manual-refresh limit, backoff, circuit breaker, kill switches, sunset rule) and noted the design never states their precedence. The later stages confirm the interaction is genuinely undefined, not just under-documented: does the 1-hour quiet period start after the third failure's backoff expires or replace it? If the probe fails, does the ladder restart or does another quiet period start? Does a success reset `consecutive_failures`? For 401/403 — "retry once, then stop until auth state changes" — nothing defines how an auth-state change is detected, what `next_retry_at` is set to, or whether the quiet period applies. Comprehension M3 and Structural M14 both conclude an implementer cannot determine when the next attempt is after three failures.
* **What changes at altitude:** this is no longer just over-engineering; the throttling design cannot be implemented as written. Keep the cadence floor and `Retry-After`; move the circuit breaker and sunset rule to follow-ups (the kill switch already covers "turn a broken source off"); and state which single mechanism gates the next attempt. Note B2 above: 429 must not feed the failure backoff at all.

### M2 — The manual Refresh button still breaks the cadence (Stage 5 B3, confirmed as a direct contradiction)
* **Severity:** Major
* **Sections:** §5 D6
* **Source:** Stage 5 B3; Critic (structural) B2; Comprehension M1; Critic (design) M16
* **Problem:** Four reports now state the same contradiction: a 15-minute Claude floor ("4 requests/hour max") alongside a manual Refresh allowed 5 minutes after the last attempt (up to 12 requests/hour); a 5-minute Codex floor alongside a manual refresh after 60 seconds. It is also never stated whether a manual refresh resets the background clock or bypasses it. The manual path exists to let an impatient user hammer exactly the rate-limited bucket the cadence exists to protect, and with two tabs open the button exists twice.
* **What changes at altitude:** make the manual-refresh floor equal to the cadence floor, or state explicitly that manual refresh is exempt and bound it separately (for example, one manual refresh per interval). The "4 requests/hour max" claim is false until one of these is chosen.

### M3 — The "no provider-specific string outside its adapter" rule is broken by the design's own UI copy (Stage 5 M5, confirmed by all three later lenses)
* **Severity:** Major
* **Sections:** §6.1 vs §6.5, §6.6, §6.7, §5 D10
* **Source:** Stage 5 M5; Comprehension B6; Critic (structural) M1; Critic (design) M7; Critic (style) M5
* **Problem:** Every later report independently found the same contradiction: §6.1 forbids any provider-specific string, path, URL, or response key outside the adapter, while §6.5's taxonomy, §6.7's card copy ("from `~/.claude/.credentials.json`, at 14:32"), and D10's location naming all require provider-specific strings in the template or view. The taxonomy is also Claude-shaped: `network_error` is worded "Couldn't reach Anthropic", which is meaningless for a Codex local spawn, and there is no Codex wording for the shared rows. The sunset rule's 404/410 trigger has no reason code at all, so the rule cannot be executed as written.
* **What changes at altitude:** restate the rule as it is actually meant — reason codes are provider-neutral; the human wording lives in one mapping per provider, in one named place — and make the taxonomy per-provider (reason code + provider → wording), with a code for 404/410. An architectural rule the design's own UI section breaks on first contact will be the first rule ignored in implementation.

### M4 — The fingerprint is still undefined, and both candidate definitions break something (Stage 5 M6, confirmed and worsened)
* **Severity:** Major
* **Sections:** §5 D5, §5 D13, §6.7
* **Source:** Stage 5 M6; Comprehension B8; Critic (design) B3; Critic (structural) M4
* **Problem:** Three reports now agree: the fingerprint ("source path + account hint") has no hash algorithm, no exact input, and no comparison rule — and for Claude there is no account hint to hash, because D13 states Claude's response carries no account identity and none is inferred. Critic (design) B3 adds the dilemma Stage 5 only implied: if the fingerprint includes token material, every routine CLI token refresh invalidates the cache; if it is only the file path, switching accounts through the same path does not invalidate it and the previous account's usage is shown as the current account's. The fingerprint also does load-bearing work beyond invalidation — it is the re-arm event for the 401 stop (see M1 above) and the provenance for D2 — with no specified contents. Structural M4 adds a second contradiction in the same area: D2 says "nothing is recorded about the login", yet §6.9 says "the CLI version is recorded with the imported state".
* **What changes at altitude:** specify the exact fingerprint inputs per provider and where each comes from, and handle both events explicitly — token rotation must not invalidate, account switch must invalidate. If no account-identifying input exists for Claude, say so and accept the stale-account risk as a documented limitation instead of implying the fingerprint covers it.

### M5 — The design shows the stale number it says is worse than no number (new since Stage 5)
* **Severity:** Major
* **Sections:** §4 vs §6.5
* **Source:** Critic (design) M3
* **Problem:** §4 states the principle clearly: "A stale '72% left' the user trusts to start a long run is worse than no number". §6.5 then specifies the "Detected + stale" state as "same line, labelled 'stale — as of HH:MM'". The design violates its own principle and relies on a small label to undo the trust the number creates. A label does not change the fact that the user reads "72% left" first and the qualifier second, if at all.
* **What changes at altitude:** pick one side. Either hide the value when stale (show only "last seen at HH:MM — check again before a long run") or delete the §4 principle and defend showing stale numbers. This is a rendering decision with direct consequences for the feature's headline promise — a fresh remaining allowance before a long run.

### M6 — Dropping the Opus/Sonnet splits can show the wrong binding limit (new since Stage 5)
* **Severity:** Major
* **Sections:** §6.3, §7
* **Source:** Critic (design) M4
* **Problem:** Claude's response carries `seven_day_opus` and `seven_day_sonnet` splits plus a `limits[]` array. The design discards all of them as "extra numbers with no decision value", asserted, not shown. For a user whose work is mostly Opus, the Opus split may be the binding limit: the headline weekly number can read "81% left" while the user is actually blocked on Opus. The headline number would then be the wrong answer to the exact question the feature exists to answer. D13's rule — show only what a source returns — is not a defence here, since the source does return the splits.
* **What changes at altitude:** either show the binding split (the lowest remaining of the windows that apply) or establish from evidence that the splits never bind before the headline windows do. This changes what the normalized snapshot model carries, so it is a model decision, not a UI detail. It also interacts with B5: the probe should record whether the splits ever bind.

### M7 — A second cache is still built next to the one already running (Stage 5 M1, unchanged)
* **Severity:** Major
* **Sections:** §6.1, §6.4, §5 D5
* **Source:** Stage 5 M1; Comprehension (grounding confirms `ServerStatsCache` states); Critic (design) M5
* **Problem:** `cache.py` will "mirror `ServerStatsCache`", which already exists in the same process, already implements the same pattern (`get()` never blocks, single-flight `refresh()`, JSON persistence, last-good on failure, `never` / `collecting` / `ready` / `failed`), and is already instantiated in the dashboard. No later report found a reason the existing cache cannot hold usage snapshots. Critic (design) M5 adds a concrete defect in the new cache's own terms: one JSON file holding both providers, written atomically, still loses updates — two processes that read, update different providers, and write will silently discard one process's update. The timestamp check §6.4 ships only prevents duplicate fetches, not concurrent writes.
* **What changes at altitude:** either reuse or generalise `ServerStatsCache`, or write down the concrete reason it cannot hold usage snapshots. If any cache survives the fetch-on-view simplification (B3), use the file lock the design already names as correct, split the cache into one file per provider, or establish from evidence that only one dashboard process ever runs and delete the cross-instance paragraph. Note that if B3 is accepted and the refresher goes away, most of this finding goes away with it — the cache's only remaining job is surviving a restart, which matters only because the refresher's backoff state lives there.

### M8 — A third background-thread pattern still joins two that already exist (Stage 5 M2, unchanged)
* **Severity:** Major
* **Sections:** §6.1, §6.4
* **Source:** Stage 5 M2; Comprehension M13
* **Problem:** The dashboard already owns background work in two established ways (`SchedulerRunner`; the `CodexDeviceFlow` / `ClaudeSetupTokenFlow` watcher-thread pattern). The design adds a third — `refresher.py` with its own lifecycle and `shutdown()` — and Comprehension M13 adds that the lifecycle wiring is not even located: no function, module, or hook is named for where the refresher starts, who constructs it, how many threads exist, or what `shutdown()` does if a thread or a live app-server does not stop.
* **What changes at altitude:** if B3 is accepted, this finding disappears with the refresher. If a background job survives, it must follow one of the two existing patterns, not invent a third, and its start/stop wiring must be named.

### M9 — Both providers still ship in one PR behind one merge gate (Stage 5 M3, confirmed)
* **Severity:** Major
* **Sections:** §5 D4, §6.9, §10
* **Source:** Stage 5 M3; Critic (design) M2
* **Problem:** D4 claims the adapters are independent and each is "verifiable on its own", with the Claude adapter kill-switchable while Codex ships. §10 then makes Mac verification of both providers a merge gate: "No merge before (1) and (2) pass live". A Codex failure on the Mac blocks the Claude half, the cache, the refresher, and the UI. The kill switch is a runtime off-switch, not a merge-time one — it cannot unblock a PR. Critic (design) M2 adds that a broken Claude adapter ships enabled (kill switches default true) in the same PR as a working Codex one.
* **What changes at altitude:** split the delivery so the merge gate matches the independence claim — Codex first as its own mergeable unit, Claude second once its endpoint is proven — or drop the "independently verifiable" claim and admit it is one atomic delivery. The Codex adapter is the lower-risk, locally-verifiable half and should be its own shippable step.

### M10 — Always-on background use of the user's OAuth token, still with no off switch the user can reach (Stage 5 M9, confirmed)
* **Severity:** Major
* **Sections:** §5 D3, §5 D16, §6.9
* **Source:** Stage 5 M9; Critic (design) M14
* **Problem:** D3 makes usage always-on: Baton sends the user's OAuth token to Anthropic's endpoint on a schedule and spawns `codex app-server`, without the user asking for either. The only off switch is `[usage] claude = false` in `jev.toml` — a config-file edit, which needs a terminal, which contradicts the browser-first principle the design cites everywhere else. Critic (design) M14 adds that D16's consent note does not describe the new act at all: it says "Baton reads the sign-in your CLI already has. It never copies or stores it", which describes reading a file, not sending the sign-in over the network to an undocumented endpoint on a 15-minute background schedule. A user comfortable with Baton reading a file has not necessarily agreed to Baton spending their CLI's rate-limit budget on background calls.
* **What changes at altitude:** a per-provider on/off control in the dashboard in this issue, or make usage opt-in until that control exists. At minimum the consent note must describe the network call. This also interacts with B3: fetch-on-view removes the background use of the token entirely, which dissolves most of the consent objection.

### M11 — The normalized model cannot render the "Detected + failed" matrix row (new since Stage 5)
* **Severity:** Major
* **Sections:** §6.1 vs §6.5
* **Source:** Critic (structural) M3; Comprehension M6
* **Problem:** §6.5 says a failed card shows "Usage unavailable — <plain-words reason>" and "last successful value, if any, shown as 'last seen <time>'." But §6.1 offers only two shapes: `UsageSnapshot` (windows, no reason) or `UsageUnavailable` (reason, and a `fetched_at` that is defined as the last successful fetch). There is no shape carrying both the last-good windows and the current failure reason. §8's rule ("a failed refresh never overwrites a good cache entry") means the data exists in the cache, but the model the page, route, poll, and template ever see has no way to express it. Comprehension M6 adds that `fetched_at` means two different things in different sections (last attempt vs last success), so after a failed refresh the UI cannot tell which timestamp "updated … ago" shows.
* **What changes at altitude:** the model needs a combined type (last-good windows plus current failure reason) or the matrix row cannot be built as specified — and the two timestamps need distinct names. This is a model decision, not a template detail.

### M12 — Bare mode and Keychain gaps are unchanged (Stage 5 M7, M8)
* **Severity:** Major
* **Sections:** §5 D10, §5 D11, §6.2, §6.5
* **Source:** Stage 5 M7, M8; Comprehension M17; Critic (design) M17; Critic (structural) M7, M12
* **Problem:** Both gaps stand exactly as Stage 5 left them, and the later stages add detail that makes them worse rather than better. Bare mode: the design knows the card's claim is false today (bare-mode workers do not read the CLI login) and answers with a qualifying label while deferring the worker fix; the precedence table is also missing the bare-mode × saved-token and bare-mode × API-key combinations, and D11 is specified for Claude only although the design never says Codex has no bare mode. Keychain: detection checks the credentials file only, so on the Mac — where the dashboard actually runs — a Keychain-only Claude login reads as "no login found"; the answer is still wording plus a named follow-up. Structural M12 adds a related defect Stage 5 did not see: D1's label split is unimplementable for Codex, because the dashboard's own device-code flow writes tokens into the same `~/.codex/auth.json` detection reads, so a dashboard-created Codex login is indistinguishable by file location from a CLI-created one.
* **What changes at altitude:** do not ship a card that names a login as detected while the workers beside it cannot use it — either scope the bare-mode worker fix in, or show the effective credential only. For Keychain, either include the read in this issue (it is the detection half, not a separate feature) or state plainly in the goals that Mac detection is best-effort and file-only, so the limitation is visible at the top of the design instead of in a decision footnote.

### M13 — The kill switch invents config with no config design (new since Stage 5)
* **Severity:** Major
* **Sections:** §6.1, §6.9, §9
* **Source:** Critic (design) M8; Comprehension B1, B7
* **Problem:** The kill switch (`[usage] claude = false` / `[usage] codex = false` in `jev.toml`) is load-bearing for the whole rollout and rollback story — it is the escape hatch for a broken undocumented endpoint — but the design never says where the config parses a new top-level section, whether unknown sections fail validation today, what the default is when the section is absent, or whether flipping the switch needs a dashboard restart. Comprehension B1 adds that §9 names files that do not exist (`src/jev_agent/core/config/schema.py` + `defaults.py`; the real files are `src/jev_agent/config/schema.py` and `src/jev_agent/config/defaults.py`) and never gives the schema entries' names, types, or loader behaviour. Comprehension B7 adds that the kill-switched state renders as `disabled_by_config`, a thirteenth reason code missing from the §6.5 taxonomy, with no UI wording, no next step, and no matrix row — and it is unspecified whether the kill switch stops the refresher attempting fetches or only changes the rendering.
* **What changes at altitude:** specify the config-schema change, the missing-section behaviour, and whether the switch is picked up live or on restart — and verify that an unknown `[usage]` section does not break config loading today. Add `disabled_by_config` to the taxonomy with its wording and matrix row. "No code change, no redeploy" is claimed; "no restart" is not claimed and not addressed, so the rollback story is weaker than stated.

### M14 — The state machine gains a state with no definition, and stale vs failed has no precedence (new since Stage 5)
* **Severity:** Major
* **Sections:** §6.4 vs §6.5; §2
* **Source:** Comprehension M5; Critic (structural) M9; Critic (style) M13
* **Problem:** States are `never → collecting → ready → stale / failed`, but no transition is specified: is `ready → stale` decided at read time by age or by a timer? If a refresh fails while the snapshot is still young, is the state `ready` or `failed`? A snapshot can satisfy both `stale` and `failed` at once (old and last attempt failed) and §6.5 has separate rows for each with no rule for which wins. The `stale` state is new — the `ServerStatsCache` pattern the design reuses has no `stale` state — and the stale matrix row assumes a failure reason ("labelled 'stale — as of HH:MM' with the reason ('rate-limited')"), but under the idle rule a snapshot can be stale with a successful last attempt and no reason at all. There is no matrix row for `collecting`, so what the card shows during a first fetch is undefined. `last_status`'s value set is never enumerated, and `fetch_started_at` appears in §6.4 but not in the D5 cache-record field list.
* **What changes at altitude:** define the transitions, the stale/failed precedence, and the `collecting` rendering — or drop the `stale` state and let age be a read-time label on `ready`. The state machine is the shared vocabulary of the cache, the poll, and the template; if it is undefined, all three are built on guesses.

### M15 — The precedence table is specified three times instead of once in code, and it mislabels env vars (Stage 5 M10, extended)
* **Severity:** Major
* **Sections:** §6.2, §6.1, §6.5
* **Source:** Stage 5 M10; Critic (structural) M7, M15; Comprehension M8, M9, M15
* **Problem:** Which credential governs workers and usage is defined in the §6.2 tables, the card copy, and the adapters' source selection, with nothing saying the table *is* one function's behaviour rather than a description each layer reproduces by hand. Structural M7 adds that the tables mislabel environment variables as "Saved by Baton" — §1 defines "Saved by Baton" as a token stored in `~/.jev/secrets.env`, but §2 says `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` are env vars — so the card copy for env-var auth is undefined. Structural M7 also notes there is no precedence defined between `auth.json` `tokens` and `auth.json` `api_key` when both are present, and the "name which others exist" rule is applied inconsistently between rows. Structural M15 adds that §6.1's `ProviderAuth` gains no field carrying the set of present credentials, so the "also present" lines have no data source. Comprehension M9/M15 add that `get_login_source()`'s return shape is a sketch (undefined `kind` vocabulary, undefined `detected_at` type, undefined `cli_version` source) and the new `ProviderAuth` fields are never named or typed — while Structural M8 adds the load-bearing gap: the accessor returns metadata only, no token values, so nothing in the design says where the Claude adapter gets the access token and refresh token it must send.
* **What changes at altitude:** make the precedence table the test list for one function in `provider_auth.py`, with the UI rendering whatever that function returns and the adapter receiving its token through a defined path — not a second reader of the credentials file, and not a contradiction of §2's "values are never returned" rule. One of those two rules has to give.

---

## Nits (altitude-relevant only)

### N1 — The design drops to implementation altitude throughout (Stage 5 N2, unchanged)
* **Severity:** Nit
* **Sections:** §6.1, §6.3, §6.4
* **Problem:** JSON-RPC message ids (`initialize` id 1, `rateLimits/read` id 2), exact header values, a 4 MiB output cap, a 5-second SIGKILL grace, and frozen dataclass field lists are code-review detail in a design document. Some named numbers are genuinely design decisions (cadence, backoff); message ids and byte caps are not. The 814-line length is itself a symptom: a read-only display of two numbers per provider does not need a document longer than the feature.

### N2 — The windows list is generic in the model and hardcoded everywhere else (Stage 5 N4, confirmed)
* **Severity:** Nit
* **Sections:** §6.1, §5 D14, §6.5
* **Problem:** `UsageSnapshot.windows` is a list so that "adding or removing a window is a data change, not a template change" — but the template, the copy, the state matrix, and the tests all assume exactly two windows named Session and Weekly. Neither provider has ever shipped a third window. Structural M6/M10 add that the "never hardcoded" promise is broken by the design's own mapping (300 → session, 10080 → weekly are hardcoded magic numbers) with no specified behaviour for any other duration, for `primary` being null, or for the Claude-side mapping, which is never stated at all. A two-field model (`session`, `weekly`) would be simpler and honest; the list buys flexibility nobody has asked for.

### N3 — D17 claims a data model can prevent a future feature (Stage 5 N5, unchanged)
* **Severity:** Nit
* **Sections:** §5 D17
* **Problem:** "The normalized model has no field for locally computed remaining, so no future change can fill the gap that way." A missing dataclass field prevents nothing — a future change adds the field. If the rule matters, it belongs in a test or a documented policy, not in a claim about what a model shape makes impossible.

### N4 — The two stale max-ages have no stated basis (Stage 5 N6, unchanged)
* **Severity:** Nit
* **Sections:** §6.4
* **Problem:** A snapshot is stale after 60 minutes for Claude and 30 minutes for Codex, with no reason given for the difference. If the Codex spawn is cheap and local (it is), the shorter max-age is the opposite of what the cost argument would suggest. Critic (style) M8 notes the same gap: the max-ages appear with no rationale and no link to the cadence values in D6.

### N5 — "Detected at" always reads "now" (Stage 5 N1, unchanged)
* **Severity:** Nit
* **Sections:** §5 D2
* **Problem:** "Detected at" is the time of the current render/poll, and detection re-runs on every render and every 15-second poll. A timestamp that always says "just now" carries no information and teaches the user to ignore timestamps on the card — including `fetched_at`, which does matter. Either show the time the underlying login was created (if the file carries it) or drop the field.

### N6 — The Overview overstates the Codex case (new, structural)
* **Severity:** Nit
* **Sections:** Overview vs §6.3
* **Problem:** The Overview says each provider shows "the two windows the providers meter: the 5-hour session window and the weekly window". §6.3 says the 5-hour window "is suspended for many accounts" and `secondary` may be null. The Overview should state the exception, and "for many accounts" should be cited or deleted. Critic (style) M7 makes the same point from the wording side.

---

## What the Comprehension stage found that does NOT change the architecture

These gaps mean the design is not re-implementable from the document alone, but they are contracts to fill in, not architecture to change. They are recorded here so the resolve pass does not mistake them for design decisions — and because their sheer number confirms the Stage 5 diagnosis that the design was written before the sources were observed.

* **Config paths are wrong** (Comprehension B1): §9 names `src/jev_agent/core/config/schema.py` + `defaults.py`, which do not exist; the real files are `src/jev_agent/config/schema.py` and `src/jev_agent/config/defaults.py`.
* **Claude credential field names are never given** (Comprehension B2): the access-token and refresh-token field names inside `claudeAiOauth`, and what to do when `expiresAt` is missing or malformed.
* **Token-refresh HTTP call is underspecified** (Comprehension B3): no request body, no response shape, no expiry computation, no statement of where the refreshed token lives or what happens if two refreshes race. (Moot if B1 above is accepted and refresh is dropped.)
* **Codex app-server protocol is underspecified** (Comprehension B4): binary resolution, `initialize` parameters, `account/rateLimits/read` parameters, stream framing, notification recognition, success/error envelopes, output-cap behaviour, and which environment variables survive the "scrub" besides `HOME` — notably whether `PATH` survives, which decides whether the binary can even be found. Critic (design) M15 adds the same gap from the other side: "scrubbed" is undefined and the spawn cost is unquantified.
* **Window derivation has no rule for unexpected values** (Comprehension B5): any `windowDurationMins` other than 300 or 10080, `primary` null, duplicate durations, missing or past `resetsAt`, Claude returning only one window.
* **Human time phrasing has no rounding rules** (Comprehension M7): floor vs nearest, unit thresholds, timezone for "HH:MM", display precision for the percent, whether the bar fills to remaining or used.
* **Live-poll `usage` JSON shape is missing** (Comprehension M12; Structural M11): three sections give three different field lists (`fetched_at` + windows; `usage.state`; an additive `usage` key) and no single place defines the schema.
* **Refresh route contract is missing** (Comprehension M11): response shape, status codes, behaviour when rate-limited, and the `<provider>` spelling (`claude` vs `claude_code`).
* **First-fetch timing is undefined** (Comprehension M14): whether dashboard start fires usage calls before any user has looked at the page — directly relevant to the rate-limit budget.
* **Cache record schema is incomplete** (Comprehension M16): serialised form of `windows`, no schema-version field, no corrupt-file behaviour, no temp-file name/mode, no rule for when `~/.jev/` does not exist.
* **No known-good version range** (Comprehension M10): `unsupported_version` requires a range; the design records one data point (Codex 0.164.0-alpha.9) and no Claude version at all.
* **No success metrics in the reviewed range** (Critic design M13): no target for fetch success rate, freshness, or staleness frequency, and no acceptance threshold for either provider.

## What the design already covers (and the later stages do not overturn)

* **Live derivation of the login state (D2).** No recorded import state; detection re-runs on render and poll. All three later reports accept this; the only open points are the meaningless "detected at" timestamp (N5) and the fingerprint that is supposed to give it provenance (M4).
* **"Detected on this device", not "imported" (D1).** The wording decision itself survives — but Critic (style) B1 shows the document breaks its own ban in the title, a section heading, and §6.9, so the claim "the word 'imported' is not used anywhere in this doc" is false on its face and must be fixed in the resolve pass. The decision is right; the document does not follow it.
* **No locally computed "remaining" (D17).** The strongest decision in the document; no later report disputes it. What the later reports do dispute is the adjacent behaviour — showing a stale server number the user can equally mistake for fresh (M5 above).
* **Failure taxonomy instead of one "unavailable" bucket.** All reports accept the taxonomy as a real improvement. What they show is that the taxonomy as written is incomplete (missing `disabled_by_config`, missing 404/410, Claude-shaped wording) and contradicts the adapter rule — the taxonomy needs repair, not removal.
* **Host-level cache location (D5).** Usage is per account on the host, not per repo; no report disputes the `~/.jev/` location or mode 600. What is disputed is the fingerprint in the record, the lost-update race on the shared file, and whether a cache file is needed at all once the refresher goes.
* **API-key auth hides usage (D12) and plan is shown only when a source returns it (D13).** No later report disputes either. D13 is, however, the same rule that leaves Claude with no account identity, which is what breaks the fingerprint (M4) — the two decisions interact and the design never reconciles them.
* **Usage failure never breaks the page.** Stated in §3, §6.5, and §8; no report disputes the goal. Critic (style) M9 notes only that repeating the sentence three times does not make it more true, and that it is a testable claim with no cited test.

## What would make this design pass a zoom-out review

1. **Probe first (resolves B5, B1, M6).** A throwaway script observes both success shapes against real logins, cites the source for the Codex method name, records whether the Opus/Sonnet splits ever bind, and measures the Claude success rate. The design is then rewritten around what the sources actually return — including the cadence numbers, which are currently uncited guesses.
2. **Drop the token refresh (resolves B1).** Treat an expired token as expired; let the CLI own refresh. This deletes a whole adapter behaviour and its reason code.
3. **Fetch on view, not in the background (resolves B3, M1, M2, M8, M14, and most of M10).** Delete the refresher, the idle rule, the circuit breaker, and the cross-instance handling. Keep the cadence floor, `Retry-After`, and the failure taxonomy. Define the manual-refresh floor as equal to the cadence floor.
4. **Compute the freshness the design actually delivers (resolves B2, M5).** Write the expected-freshness arithmetic into the design, treat 429 as routine, and decide whether a stale number is shown or hidden — the design cannot claim the principle and break it in the same document.
5. **One precedence function, one model, one adapter rule (resolves B4, M3, M11, M15).** The precedence table becomes the test list for a single function that decides both worker credential and measured credential; the normalized model gains a shape carrying last-good windows plus the current failure reason; the adapter rule is restated so the per-provider wording mapping has a named home.
6. **Define or drop the fingerprint (resolves M4).** Exact inputs per provider, rotation must not invalidate, account switch must invalidate — or accept the stale-account risk as a documented limitation.
7. **Ship Codex first (resolves M9).** One provider, one merge gate, then Claude once its endpoint is proven.
8. **Put the off switch in the dashboard and describe the network call in the consent note (resolves M10).** Or make usage opt-in until the control exists. Fetch-on-view (step 3) removes the background use of the token and dissolves most of this objection.
9. **Resolve the bare-mode and Keychain gaps before implementation (resolves M12).** Each is a paragraph of design, not a follow-up — and the Codex label split needs a mechanism, since a dashboard-created Codex login is indistinguishable by file location from a CLI-created one.
10. **Specify the kill switch as config (resolves M13).** Real file paths, schema entries, missing-section behaviour, live vs restart pickup, and the `disabled_by_config` taxonomy row.

## Findings count

* Blockers: 5 (B1–B5) — 3 new since Stage 5 (B1, B2, B4), 2 carried and worsened (B3, B5)
* Majors: 15 (M1–M15) — 5 new since Stage 5 (M5, M6, M11, M13, M14), 10 carried (M1–M4, M7–M10, M12, M15)
* Nits: 6 (N1–N6)
* Comprehension-only detail gaps (no architecture change): 12, listed above
