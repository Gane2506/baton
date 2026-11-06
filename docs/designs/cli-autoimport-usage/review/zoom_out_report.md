# Zoom Out Review — CLI auto-import + usage (Stage 5, full file, lines 1–814)

* Reviewer: Skeptical Architect (Zoom Out Goldfish), zero prior context
* Design reviewed: `docs/designs/cli-autoimport-usage/design.md` (814 lines, status Draft)
* Standard: `zoom-out-standard.md` — root problem, simplicity/YAGNI, anti-weeds, leverage existing
* Code checked read-only: `src/jev_agent/core/provider_auth.py`, `src/jev_agent/servers/stats.py`, `src/jev_agent/dashboard/routes_connections.py`, `src/jev_agent/dashboard/scheduler_runner.py`, `src/jev_agent/dashboard/core.py`
* Verdict: **not approved in its current shape.** The design is honest about its own weak spots (section 4 is candid), but it then builds permanent machinery on top of them anyway. Two blockers have to be resolved before implementation: verify the sources before building on them, and drop the background refresher in favour of fetching when the user looks.

## What the design gets right (short, because critique comes first)

Detection already exists and the design does not rebuild it. The live-derivation choice (D2) avoids a stale second source of truth. The failure taxonomy is a real improvement over one "unavailable" bucket. Those are the load-bearing good calls. Everything below is about the size and shape of what is built around them.

---

## Blockers

### B1 — The full permanent architecture is built before either usage source has ever been observed working
* **Severity:** Blocker
* **Sections:** 6.3 (Usage sources), 10 (Verification plan), D4
* **Problem:** Neither source is verified. The Codex success shape "has never been observed by Baton — only the failure" on the VM. The Claude endpoint is "verified live by independent community projects, not by Baton", and roughly one request in three succeeds under an active session. Yet the design builds five new modules, a host-level cache file, a background scheduler, a 12-code failure taxonomy, a state machine, a manual-refresh endpoint, kill switches, and a sunset rule — all before a single successful fetch of either source. The Mac verification that would settle both is a *merge gate*, meaning the whole build happens first and the check that could invalidate it happens last. If the Codex `rateLimits/read` shape differs from the assumed one, or the Claude endpoint rejects Baton's token kinds, most of section 6 is scrap.
* **What should happen instead:** a throwaway probe first — a script, not a subsystem — that runs `codex app-server` against a real login and calls the Claude endpoint with a real token, and records the actual responses. Only the sources that answer get permanent architecture. A probe that fails costs an afternoon; this design failing at the merge gate costs the whole PR.

### B2 — A background refresher, a cache file, and a scheduler exist to answer a question the user only asks at view time
* **Severity:** Blocker
* **Sections:** 6.4 (Cache and refresher), D5, D6, D7
* **Problem:** The stated value of the feature is a human deciding before a long run whether allowance remains (section 4: "Usage changes no automated Baton decision"). Nothing in Baton acts on the numbers. That means the numbers are only needed when the user opens the Connections page and looks at them. The design instead builds a always-running background job: per-provider cadence, an idle rule, backoff, a circuit breaker, single-flight, cross-instance coordination, a persisted cache file with retry state, and a manual-refresh endpoint. The idle rule itself concedes the point — no refresh unless the usage UI was viewed in the last 30 minutes, i.e. the background job exists to serve views, and views are the trigger. Fetch-on-view (fetch when the page is opened or Refresh is pressed, serve the last good value otherwise) deletes `refresher.py`, `cache.py`, the idle rule, the circuit breaker, and the entire cross-instance problem in one move. The cache file's only remaining job — surviving a dashboard restart — matters only because the refresher's backoff state lives there; with no refresher, there is no backoff state to preserve.
* **Simpler alternative:** fetch on page view / manual refresh, keep the last good snapshot in memory and in the existing stats-cache pattern if persistence is truly needed, and add a background job later only if real usage shows the view-time fetch is too slow. There is no evidence in the design that a view-time fetch is too slow — the Claude timeout is 10 s and the Codex spawn is 15 s, both acceptable behind a "Checking…" state the design already specifies for the Refresh button.

### B3 — Seven separate throttling mechanisms guard an endpoint that allows four requests an hour
* **Severity:** Blocker (premature optimisation, layered)
* **Sections:** D6, D7, 6.9, 6.4
* **Problem:** Count the brakes: (1) a 15-minute cadence floor, (2) an idle rule, (3) a manual-refresh rate limit, (4) exponential backoff 15→30→60 minutes, (5) a circuit breaker after 3 failures with a 1-hour quiet period, (6) per-provider kill switches in config, (7) a sunset rule for the undocumented endpoint. Each one is individually reasonable; together they are five layers of protection for a failure mode the design has not measured. The Claude bucket is shared with the CLI's own polling, which is a real concern — but the cadence floor alone (4 requests/hour) already bounds Baton's share of it. Backoff plus circuit breaker plus kill switch plus sunset rule is defending against a storm that the cadence floor already prevents. Worse, the layers interact in ways the design never works through: a 401 "retry once, then stop until auth state changes" and a 429 backoff and a 3-failure circuit breaker can all be active at once, and the cache record has to say which one is gating the next attempt. The design never states the precedence between them.
* **What to keep:** the cadence floor and honouring `Retry-After`. Drop the circuit breaker and the sunset rule into follow-ups (a kill switch already covers "turn a broken source off"), and say plainly which single mechanism gates the next attempt.

---

## Majors

### M1 — A second cache is built next to the one already running in the dashboard
* **Severity:** Major
* **Sections:** 6.1, 6.4, D5
* **Problem:** The design says `cache.py` will "mirror `ServerStatsCache`". Code search shows `ServerStatsCache` already exists (`src/jev_agent/servers/stats.py`), is already instantiated in the dashboard (`dashboard/core.py`, `self.server_stats = ServerStatsCache()`), and already implements exactly the pattern the new cache copies: `get()` never blocks, `refresh()` returns False when a collection is already running, results persist as JSON, last-good is kept on failure, states `never` / `collecting` / `ready` / `failed`. Building a sibling module with the same states, the same single-flight rule, and the same JSON persistence is reinventing a wheel that is already turning in the same process. The design never explains why the existing cache cannot be generalised (it is per-server keyed today; a per-provider key is a small change) or reused as-is for the usage snapshots.
* **Ask:** either reuse/generalise `ServerStatsCache`, or write down the concrete reason it cannot hold usage snapshots. "Mirroring" is not a reason.

### M2 — A third background-thread pattern joins two that already exist
* **Severity:** Major
* **Sections:** 6.1, 6.4
* **Problem:** The dashboard already owns background work in two established ways: `SchedulerRunner` (one background thread owned by the dashboard process, graceful stop, lock-based ownership) and the `CodexDeviceFlow` / `ClaudeSetupTokenFlow` watcher-thread pattern in `provider_auth.py` (lock, watcher thread, `cancel()` / `shutdown()`). The design adds a third — `refresher.py`, "one background scheduler shared by both providers" with its own lifecycle, its own `shutdown()`, and its own thread-joining rules. Three lifecycle patterns for background work in one dashboard means three ways for a thread to leak, hang shutdown, or get stuck. If B2 is accepted and the refresher goes away, this finding goes away with it. If a background job survives, it should follow one of the two existing patterns, not invent a third.

### M3 — Both providers ship in one PR, but one merge gate couples them — D4's independence claim does not hold
* **Severity:** Major
* **Sections:** D4, 9, 10
* **Problem:** D4 says the two adapters are independent and "each merges in the same PR but is verifiable on its own", with the Claude adapter kill-switchable while Codex ships. Section 10 then makes Mac verification of *both* providers a merge gate: "No merge before (1) and (2) pass live" — (1) is Claude usage, (2) is Codex usage. A Codex failure on the Mac blocks the Claude half, the cache, the refresher, and the UI, even though the kill switch supposedly lets them ship separately. The independence exists in the code shape but not in the delivery plan. The Codex adapter is also the lower-risk, locally-verifiable half; it should be its own shippable step, with Claude following once its endpoint is proven against a real token.
* **Ask:** split the delivery so the merge gate matches the independence claim — Codex first as its own mergeable unit, Claude second — or drop the "independently verifiable" claim and admit it is one atomic delivery.

### M4 — The cross-instance mechanism is one the design itself admits is inadequate
* **Severity:** Major
* **Sections:** 6.4
* **Problem:** "A file lock is the correct mechanism; a timestamp check is the minimum." The design then ships the minimum: a second dashboard process that sees a fresh `fetch_started_at` (< 60 s old) serves the cached value instead of fetching. A timestamp check is not mutual exclusion — two processes starting within the same window both fetch, and a crashed process leaves a `fetch_started_at` that blocks everyone for 60 seconds with no owner to clear it. Shipping a mechanism the author already knows is wrong, for a problem (two dashboard processes sharing one host cache) that only exists because of the host-level cache plus background refresher in B2, is the wrong trade. Either use the file lock, or remove the shared-state design that needs one.

### M5 — "No provider-specific string outside its adapter" cannot hold, because the UI copy is provider-specific by design
* **Severity:** Major
* **Sections:** 6.1, 6.5, 6.6
* **Problem:** Section 6.1 states a hard rule: no provider-specific string, path, URL, or response key may appear outside its adapter — "not in `provider_auth.py`, `views.py`, `routes_connections.py`, or the template". Section 6.5 then specifies UI wording that is inherently provider-specific and must live in the template or view: "Couldn't reach Anthropic", "Sign in again in Claude Code", "This Codex version isn't supported for usage yet", "Use the CLI sign-in instead". Either the rule means the reason *code* stays generic and the *rendering* maps codes to words per provider — in which case the rule as written is wrong and needs restating — or the rule is literal, in which case the section 6.5 table violates it. An architectural rule the design's own UI section breaks on first contact will be the first rule ignored in implementation.
* **Ask:** restate the rule as it is actually meant (reason codes are provider-neutral; the human wording lives in one mapping per provider, in one place) and name that place.

### M6 — The cache fingerprint depends on an "account hint" the design elsewhere says does not exist
* **Severity:** Major
* **Sections:** D5, D13, 6.7
* **Problem:** The cache record's `fingerprint` is defined as "source path + account hint, never a token value", and a fingerprint change invalidates cached usage. But D13 states Claude's usage response carries no plan and no account identity, and "no plan is inferred from the token, the login file, or the usage numbers". The design never says where the Claude account hint comes from, or what it is when the source is a setup token versus a CLI login. If the hint is unobtainable, the fingerprint is just the source path — and then two different accounts signing in through the same file path (sign out, sign in as someone else) would not invalidate the cache, which is exactly the staleness the fingerprint exists to prevent. If the hint is obtainable, D13's "nothing is inferred" needs an exception. One of the two sections is wrong.
* **Ask:** define the fingerprint per provider with its actual source, or drop the account-hint half and say what invalidation actually rests on.

### M7 — Bare mode ships a known-misleading card and defers the real fix
* **Severity:** Major
* **Sections:** D11, 6.2, 6.5
* **Problem:** The design knows the card's claim is false today — bare-mode workers do not read the CLI login — and its answer is to qualify the label ("the login does not apply to bare-mode workers") and suppress the usage bars, while "fixing bare-mode workers to read the CLI login is a separate issue". That is a workaround pasted over a structural gap: the Connections page's job is to say which login workers use, and for bare-mode users the answer is "not this one". Showing a detected login with a footnote that it does not apply, on the page whose purpose is to show what applies, preserves the confusion in a smaller font. The honest alternatives are to fix the worker path in this issue (it is the same login the usage bars are measured against) or to not show the CLI login as the effective credential in bare mode at all.
* **Ask:** do not ship a card that names a login as detected while the workers beside it cannot use it. Either scope the bare-mode worker fix in, or show the effective credential only.

### M8 — Detection is incomplete on the Mac, the dashboard's primary host
* **Severity:** Major
* **Sections:** D10, 6.5
* **Problem:** Detection checks the credentials file only. On the Mac — where the dashboard actually runs for the user — Claude credentials can live in the Keychain (`Claude Code-credentials` service) instead of the file. In that case the card says "No CLI login found in `~/.claude/.credentials.json`", which a user with a working CLI login will read as "Baton thinks I am not signed in". The design's answer is wording plus a named follow-up. Wording does not fix a false negative on the main platform; it annotates it. A feature whose headline is "detect the login on this device" should detect the login on the device it runs on before it builds usage bars on top of detection.
* **Ask:** either include the Keychain read in this issue (it is the detection half, not a separate feature), or state plainly in the goals that Mac detection is best-effort and file-only, so the limitation is visible at the top of the design instead of in a decision footnote.

### M9 — Always-on background use of the user's OAuth token, with no off switch the user can reach
* **Severity:** Major
* **Sections:** D3, D16, 6.9
* **Problem:** D3 makes usage always-on: Baton sends the user's OAuth token to Anthropic's endpoint on a schedule, and spawns `codex app-server`, without the user asking for either and without an opt-in. The only off switch in the issue is `[usage] claude = false` in `jev.toml` — a config-file edit, which needs a terminal, which contradicts the browser-first principle the design cites for everything else. D16's answer is a one-line consent note ("to stop Baton using it, sign out in the CLI") — i.e. the way to stop Baton using the login is to destroy the login itself, which also stops the user's own CLI. A background job that spends the user's credentials needs a dashboard-visible off switch, not a note telling the user to sign out of their own tools.
* **Ask:** a per-provider on/off control in the dashboard in this issue, or make usage opt-in until that control exists. A kill switch only the author can flip by editing TOML is not a user control.

### M10 — The precedence table is specified three times instead of once in code
* **Severity:** Major
* **Sections:** 6.2, 6.1, 6.5
* **Problem:** Which credential governs workers and usage is defined in three places: the precedence tables in 6.2 (nine rows across two providers, plus bare-mode carve-outs), the card copy that names the effective credential, and the adapters' source selection (which token the Claude adapter sends, which Codex file the adapter reads). The design adds a "shared credential-source accessor" (`get_login_source()`) in 6.1, which is the right instinct — but the tables are still written out as UI copy, and nothing in the design says the table *is* the accessor's behaviour (one function, tested against the table) rather than a description the implementation must reproduce by hand in the view, the route, and the adapter. The dual-auth trap the design is worried about (section 4: removing a saved token silently promotes the login underneath) is a single-precedence-function problem; specifying it as prose tables invites the three copies to drift.
* **Ask:** make the precedence table a test list for one function in `provider_auth.py`, and say the UI renders whatever that function returns — no second copy of the logic in the view or adapter.

### M11 — The setup-token one-shot builds permanent logic for an unverified guess
* **Severity:** Major
* **Sections:** D9, 6.3, 6.5
* **Problem:** Whether the `sk-ant-oat01-` setup token is accepted by the usage endpoint is unknown — the design says so ("If it is accepted, it is the cheapest path"). D9 still builds permanent behaviour around it: try once per token, record `setup_token_rejected`, never retry that token kind, fall back to the CLI token if one exists, plus a dedicated reason code and UI wording. That is a whole code path, a cache field, and a user-facing state for a hypothesis. If the endpoint rejects setup tokens (the likely outcome, since the token was minted for the CLI's own use), the one-shot path is dead code that ran exactly once. This is B1 in miniature: verify first (part of the probe in B1), then build the branch only if the answer is yes.

---

## Nits

### N1 — "Detected at" always reads "now"
* **Severity:** Nit
* **Sections:** D2
* **Problem:** "Detected at" is the time of the current render/poll, and detection re-runs on every render and every 15-second poll. A timestamp that always says "just now" carries no information and teaches the user to ignore timestamps on the card — including `fetched_at`, which does matter. Either show the time the underlying login was created (if the file carries it) or drop the field.

### N2 — The design drops to implementation altitude throughout
* **Severity:** Nit (but pervasive)
* **Sections:** 6.1, 6.3, 6.4
* **Problem:** A design document should settle interfaces and data flow; this one also settles JSON-RPC message ids (`initialize` id 1, `rateLimits/read` id 2), exact header values, exact timeouts (10 s vs 15 s), a 4 MiB output cap, a 5-second SIGKILL grace, and frozen dataclass field lists. Some named numbers are genuinely design decisions (cadence, backoff). Message ids and byte caps are code-review detail. The 814-line length is itself a symptom: a read-only display of two numbers per provider does not need a document longer than the feature.

### N3 — Codex "refresh-on-view" is mentioned once and never defined
* **Severity:** Nit
* **Sections:** D6
* **Problem:** D6 says Codex refreshes on a "minimum 5 minutes between attempts, or refresh-on-view" — but refresh-on-view has no debounce, no trigger definition, and no interaction with the 5-minute floor stated anywhere else. If refresh-on-view means "a page view fetches immediately", the 5-minute floor is the actual rule and the phrase adds nothing. If it means something else, it needs a paragraph.

### N4 — The windows list is generic in the model and hardcoded everywhere else
* **Severity:** Nit
* **Sections:** 6.1, D14, 6.5
* **Problem:** `UsageSnapshot.windows` is a list so that "adding or removing a window is a data change, not a template change" — but the template, the copy, the state matrix, and the tests all assume exactly two windows named Session and Weekly. The generality is speculative: neither provider has ever shipped a third window. A two-field model (`session`, `weekly`) would be simpler and honest; the list buys flexibility nobody has asked for.

### N5 — D17 claims a data model can prevent a future feature
* **Severity:** Nit
* **Sections:** D17
* **Problem:** "The normalized model has no field for locally computed remaining, so no future change can fill the gap that way." A missing dataclass field prevents nothing — a future change adds the field. If the rule matters, it belongs in a test or a documented policy, not in a claim about what a model shape makes impossible.

### N6 — The two stale max-ages have no stated basis
* **Severity:** Nit
* **Sections:** 6.4
* **Problem:** A snapshot is stale after 60 minutes for Claude and 30 minutes for Codex, with no reason given for the difference. If the numbers are arbitrary, say so; if the Codex spawn is cheap and local (it is), the shorter max-age is the opposite of what the cost argument would suggest.

---

## What would make this design pass a zoom-out review

1. **Probe first (resolves B1, M11).** A throwaway script observes both success shapes against real logins. The design is then rewritten around what the sources actually return.
2. **Fetch on view, not in the background (resolves B2, B3, M2, M4).** Delete the refresher, the cache file, the idle rule, the circuit breaker, and the cross-instance handling. Keep the cadence floor, `Retry-After`, and the failure taxonomy — those are the parts that survive contact with a simpler shape.
3. **Reuse the cache that exists (resolves M1).** If any persistence is still needed, generalise `ServerStatsCache` instead of mirroring it.
4. **Ship Codex first (resolves M3).** One provider, one merge gate, then Claude once its endpoint is proven.
5. **Put the off switch in the dashboard (resolves M9)** and resolve the fingerprint definition (M6), the adapter-rule wording (M5), and the bare-mode and Keychain gaps (M7, M8) before implementation — each is a paragraph of design, not a follow-up.

## Findings count

* Blockers: 3 (B1–B3)
* Majors: 11 (M1–M11)
* Nits: 6 (N1–N6)
