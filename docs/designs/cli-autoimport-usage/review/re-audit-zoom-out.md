# Re-audit — Zoom Out (Elephant-Goldfish Model, single permitted re-audit)

* **Design reviewed:** `docs/designs/cli-autoimport-usage/design.md` (1371 lines, amended after Goldfish review, 2026-11-06)
* **Checked against:** `review/consolidated_review_report.md` (items C1–C30, contests X1–X4)
* **Reviewer:** Zoom Out (Skeptical Architect)
* **Verdict: NOT approved.** Two Blockers are fix-check failures in the rewritten text, and five Majors are either fixes that did not land as specified or new problems in the rewritten shape. The fetch-on-view shape itself is the right simplification and most of the rewrite did land — but the precedence table now contradicts its own text, and the resolution function the whole design hangs on still has no name or return type.

## Fix-check summary (C1–C30)

| Item | Status | Note |
|---|---|---|
| C1 probe gate | Landed | §9.0, D18, Overview, §10. Checklist covers all six probe questions plus CLI versions. |
| C2 drop token refresh | Landed | D8, §6.3, §9.2 expired-token test. One wording nit (N6). |
| C3 fetch-on-view | Landed | D6, §6.4, §7 rejection, fetcher lifecycle. `refresher.py` is gone from §9. |
| C4 per-provider opt-in | Partial | D3, `enabled` in cache, "Check usage" button land. Consent note misdescribes the trigger (M3). |
| C5 manual floor = cadence floor | Landed | D6, §6.4, §9.1. Claude 15-min manual floor has no Phase B test (N4). |
| C6 429 is routine | Partial | D7, no backoff/circuit breaker. `Retry-After` has nowhere to live (M2). |
| C7 401/403 re-arm | Partial | D7, §6.5, §9.2 tests. Retry timing and re-arm gap (M9). |
| C8 one precedence function | **Missing** | Tables exist, but the function is unnamed with no return type; test count is wrong (B2). |
| C9 fingerprint (Codex `accountId`) | Landed | D5, §6.7, §9.1 cache bullet; Claude limitation stated. |
| C10 two PRs, Codex first | Partial | D4, §9.1/§9.2, §10 gates. Phase A leaves the Claude card half-wired (M4). |
| C11 stale value hidden | Partial | D19, §6.5 stale row. Overlaps the failed row (M1). |
| C12 setup-token probe fact | Landed | D9, §6.5 row, §9.0 item 4, §9.2 conditional tests. |
| C13 adapter rule + wording mapping | Partial | §6.1 restated, §6.5 per-provider columns. Template bullet contradicts it (M5). |
| C14 data contract | Partial | Dataclasses, poll JSON, route, shared reader land. `resets_at` nullability and the `left_percent` conversion do not (M8). |
| C15 config paths + kill switch | Landed | §6.9, §9.1 config bullet; restart required, stops fetches, `disabled_by_config` in taxonomy. |
| C16 fetcher lifecycle | Landed | §6.4, `DashboardCore`, 5 s join. Route-table line numbers not carried over (N3). |
| C17 state machine | Partial | Stored states, read-time stale, display derivation. `last_status` mapping missing (M8). |
| C18 window mapping rules | Landed | §6.3 Codex/Claude rules, unknown → `unexpected_response`. That rule is fragile (N7). |
| C19 splits are windows | Landed | §6.3, D13, §6.6 binding-window label. One test line hedges it (N2). |
| C20 precedence table fixes | **Missing** | Codex table contradicts its own precedence sentence (B1). |
| C21 Codex protocol cited | Landed | §6.3 with citation, full sequence, env allow-list, timeouts, caps. |
| C22 Claude fields + `limits[]` | Landed (probe-gated) | §6.3, §9.0, §9.2; field names and array structure deferred to the probe as specified. |
| C23 "imported" ban | Landed | Title, §6.2 heading, §6.9, §9 wording. Remaining uses name the rejected alternative in §7, as allowed. |
| C24 numbers cited/labelled | Landed | 480/hour arithmetic, one-in-three labelled unmeasured, 15-min cadence a starting value, §3 acceptance criteria. |
| C25 timestamp check, per-provider files | Landed | D5, §6.4, §9.1 cache bullet and tests; no file lock. |
| C26 cache reuse rationale | Landed | §6.4, `write_secret` atomic-write precedent. |
| C27 UI copy consistency | Partial | Glossary, one button, relative times, rounding rule land. "Allowance" still appears three times (N1). |
| C28 bare mode + Keychain in goals | Landed | §3 goals state file-only detection; tables completed for Claude; Codex has no bare mode stated. |
| C29 small fixes | Partial | Most land. The `no_login` / `not_applicable_api_key` move does not (M1). |
| C30 §9/§10 rewritten | Landed (mostly) | Phases, values, paths, reason-coded tests, gates, ≥85 threshold. Gaps are N3/N4. |

## Findings

### Blocker B1 — Codex precedence table contradicts its own precedence sentence (C20, §6.2)

The Codex table is ordered top-to-bottom as precedence. Row 2 is "no env key, `auth.json` `api_key` yes, `tokens` don't-care → workers use API key". Row 3 is "no env key, `api_key` no, `tokens` yes → subscription". A file holding both `api_key` and `tokens` therefore matches row 2 first, and workers use the API key. Two lines below the table, the text says the opposite: "Within `auth.json`, `tokens` takes precedence over `api_key` (matching existing detection order; pinned by test)."

There is also no table row for the both-present combination, so the test list cannot pin the claimed order — the one combination where the precedence matters is the one combination with no row. The fix is one of two: either reorder the table so the `tokens` row comes before the `api_key` row (and say so), or change the sentence so `api_key` wins and say why. As written, an implementer following the table and an implementer following the sentence build different behaviour, and both can point at the design.

### Blocker B2 — The precedence resolution function still has no name and no return type (C8, §6.1, §6.2, §9.1)

C8 required the design to name the function and its return type. The rewrite says "one resolution function in `provider_auth.py`" (§6.1) and "the precedence resolution function (one function, §6.2 tables as its test list — one case per row: 5 Claude, 4 Codex)" (§9.1). No name is given anywhere, and no return type is defined — the design never says what shape carries "effective credential + present credentials + measured credential" back to the caller. §6.1 defines `LoginSource` and the `ProviderAuth` additive fields, but neither is identified as the function's return type.

The count is also wrong: the Claude table has 7 rows, not 5 (§6.2 lists 7 Claude rows; §9.1 claims 5 Claude cases in two places). Two table rows therefore have no test case, and the reader cannot tell which two. Name the function, name its return type, and make the test count match the table row count.

### Major M1 — `not_checked_yet` and `not_applicable_api_key` are still treated as reason codes (C11, C29, §6.5, §9.1)

§6.5 states that `no_login` and `not_applicable_api_key` are "not fetch outcomes — they are render states, listed in the matrix below, not in this table." The same section then puts `not_checked_yet` — also a render state, never a fetch outcome — in the reason-code table, and §9.1 tests assert "First run → reason `not_checked_yet`" and "API-key auth → reason `not_applicable_api_key`". The state matrix rows do not carry the `no_login` / `not_applicable_api_key` code names either, so the move C29 required exists only as a sentence, not as a structure.

The same section has a second overlap: the display derivation (§6.4) sends "snapshot + age > max-age" to `stale` (value hidden), while the matrix's "Detected + failed" row says the "last successful value, if any, [is] shown as 'last seen <relative time>'" without saying whether the value itself is shown when that snapshot is stale. A snapshot that is both stale and the product of a failed refetch has two derivations and no stated winner. State the precedence (stale wins; the failed row shows the time, not the value) and remove the two render states from the reason-code table and the reason-coded test list.

### Major M2 — `Retry-After` has nowhere to live, so the "honour exactly" rule cannot survive a restart (C6, §6.4, D5, §9.2)

D7 requires honouring `Retry-After` exactly when present. D5 and §6.4 delete `next_retry_at` from the cache record (correctly, for the deleted background scheduler) and record nothing else that could hold a retry time. The fetcher can therefore honour `Retry-After` only in process memory. After a dashboard restart the value is forgotten, and the next page view fetches immediately — inside the `Retry-After` window the design promised to respect. §9.2 tests "next attempt no earlier than `Retry-After` seconds" without saying where that deadline is stored, so the test is not implementable as specified across a restart.

Either persist the retry deadline in the cache record (one field, named) or narrow the rule to "in-process only, restart resets it" and say so in D7. Separately, §6.4 and §3 overclaim: "a fetch on view succeeds unless the provider rate-limits." Network errors, timeouts, and auth failures also fail a fetch on view; the sentence should say a fetch succeeds unless the provider refuses it or the call fails, with the reason shown.

### Major M3 — The consent note misdescribes the trigger, and opt-in has no off-ramp (C4, §5 D16, §6.4, §6.6)

D16's note says "Opening this page checks usage with the provider. Baton sends your sign-in to <provider> for that check, only when you ask it to." That is false in both directions. Before the user ever clicks "Check usage", opening the page checks nothing. After the one click, opening the page checks usage whenever the snapshot is older than the floor — without a fresh ask on that visit. "Only when you ask it to" describes a per-visit ask that fetch-on-view does not make. The note must describe the real act: one click authorises later page opens to check usage until the user stops it.

And there is no way to stop it per provider in the UI. `enabled: true` persists in the cache record with no button, toggle, or card action to clear it; the only off-ramps are the config kill switch (edit a file, restart the dashboard) or signing out in the CLI. A one-click opt-in with no one-click opt-out is a one-way door, and it undercuts the consent story C4 was meant to fix. Add a per-provider stop action on the card, or state plainly that there is none and the kill switch is the only off control. A related fragility: a corrupt cache file is treated as empty (§6.4), which silently clears `enabled` and forces the user to opt in again with no explanation.

### Major M4 — Phase A ships the Claude card with no Claude adapter behind it (C10, §9.1, §10)

Phase A is "Codex adapter + shared plumbing (model, cache, fetcher, UI, precedence, wording)". The UI, precedence tables, kill switch, and wording it ships cover both providers — including Claude's precedence rows, Claude's wording, and the Claude usage block in `connections.html`. Phase B adds "only the Claude adapter". The design never states what the Claude card shows after Phase A merges: a usage block with no source, a permanently "not checked yet" card, or a hidden block. The Phase A Mac gate (§10) checks only Codex, so the Claude interim state is unreviewed and unverified at the merge that creates it.

State the interim state in §9.1 (for example: the Claude usage block is hidden until Phase B, or it renders `not_checked_yet` with no fetch possible) and add it to the Phase A gate. A split that leaves one provider's card in an unspecified state has not finished the split.

### Major M5 — Wording is claimed for the adapter mapping but specified for the template (C13, §6.1, §9.1)

§6.1 requires human wording to live "in one mapping per provider, inside that provider's adapter module" with the shared layer and template only passing and rendering reason codes. §9.1's template bullet then directs literal copy into `connections.html`: "Billed per token — no subscription windows apply to an API key.", the bare-mode line, and the "Check usage" / "Refresh usage" / "Checking…" button. The consent note (D16) is likewise specified as template text. An implementer following §9.1 hardcodes wording in the template; an implementer following §6.1 puts it in the adapter mapping and renders codes. Pick one owner for each string and say which file holds it. The §6.5 table is fine as a specification of the mapping's contents; the §9.1 template bullet is not fine as a second copy of the same strings.

### Major M6 — One max-age hides the slow weekly window with the fast session window (new, D19, D20, §6.4)

Staleness is per snapshot, not per window: Claude hides everything after 60 minutes, Codex after 20 minutes. The weekly window measures seven days and barely moves in an hour; the session window is the fast-moving one. Hiding a weekly "81% left" that is still decision-useful because the session half of the same response is old throws away good data to protect the user from the stale half. The design's own principle — a stale number trusted for a long run is worse than no number — applies to the session window, not obviously to a weekly window that changes by a fraction of a percent per hour. Either give each window its own max-age (session short, weekly long) or justify in D20 why the snapshot-level rule is the right trade despite hiding still-good weekly data.

### Major M7 — `cache.py` and `fetcher.py` both own single-flight, and neither owns `refresh()` (new, §6.1, §6.4, §9.1)

§6.1 gives `cache.py` "the single-flight / staleness rules" and `fetcher.py` the cadence floor, threads, and `shutdown()`. §6.4's single-flight paragraph says "a second `refresh()` returns False while one runs" without saying whose `refresh()` that is — the cache's or the fetcher's. §9.1 repeats the same sentence under both the cache bullet ("one fetch per provider at a time (a second `refresh()` call returns False…)") and the fetcher test list ("single-flight (second `refresh()` returns False…)"). Two new modules with an overlapping responsibility and an unowned method name is the kind of split that produces two half-implementations. Name the single owner of `refresh()` and single-flight, and say what the other module calls to ask for a fetch.

### Major M8 — The model cannot hold a null reset, and `last_status` has no mapping to display states (C14, C17, §6.1, §6.3, §6.4)

`UsageWindow.resets_at` is typed `datetime` (§6.1), but §6.3 says Claude's `resets_at` "may be null". A window with no reset time cannot be built as specified. Make it `datetime | None` and say what the card renders then ("no reset reported" or the bar without a reset line).

Separately, §6.4 defines a `last_status` vocabulary (`ok`, `rate_limited`, `auth_error`, `spawn_error`, `unexpected`, `disabled`) that never maps onto the display states (`never` / `collecting` / `ready` / `failed` / `stale`). A young snapshot plus a failed refetch with `last_status: rate_limited` is `ready` with a sub-line per one sentence and `failed` per the matrix's failed row — the design does not say which. Write the one mapping. A smaller instance of the same gap: the model carries `used_percent` while the poll JSON carries `left_percent` (§6.4, §9.1), and no layer is named as doing the 100-minus conversion; the poll's `resets_at` serialisation format is also unstated (the cache specifies ISO-8601 for its own `resets_at`, the poll does not).

### Major M9 — "Retry once" on 401/403 has no timing, and the re-arm misses token rotation (C7, §5 D7, §6.5)

"Retry once, then stop" does not say when the retry happens. An immediate retry of a rejected token will almost certainly fail again, burning a second request against the same rate-limit bucket for no information. If the retry is meant for the next view or manual attempt, say so; if it is immediate, say why a second identical call is expected to behave differently.

The re-arm conditions — login change (kind or source path) or manual Refresh — also miss the common rotation case: the CLI refreshes its token in the same file, so kind and path are unchanged and the in-process stop persists until the user happens to click Refresh. The user has no signal telling them to click it; the card shows the auth failure, not "click Refresh to retry". Either include a content/identity change in the re-arm (for Codex, `accountId` is available; for Claude, none is — say so) or make the auth-failure next step name the Refresh click as the way back.

A smaller error in the same table: the `unauthorized` row gives Codex wording ("Codex rejected this sign-in") although a Codex fetch is local and never returns 401/403 — a Codex auth failure surfaces as `app_server_failed`. The Codex column for `unauthorized` should be empty, not worded.

## Nits

* **N1 — "Allowance" is still used three times despite the §1 ban (C27, Overview, §5 D11, §6.2).** §1 says "'Allowance' and 'remaining usage' are not used." The Overview says "how much of the account's allowance is left", D11's rationale says "showing an allowance next to…", and §6.2 says "workers spend the saved token's allowance". Delete all three uses or delete the ban; a ban the document breaks on its first page is not a ban.
* **N2 — Phase B test hedges the splits decision (C19, §9.2).** "Happy path: two windows + splits ignored-or-included per the probe record" contradicts §6.3 and D13, which commit to including the splits as windows. The probe records the array structure; it does not re-decide whether splits are shown. Write the test to match the committed decision.
* **N3 — Route-table line numbers not carried over (C16, §9.1).** C16 located the refresh route in the route table at `app.py` lines 257–299; the §9.1 `app.py` bullet says only "register the refresh route in the route table". Carry the lines over.
* **N4 — No Phase B test for the Claude 15-minute manual floor (C5, §9.2).** Phase A fetcher tests assert the Codex 5-minute floor; nothing asserts the Claude 15-minute floor or the redirect-plus-flash naming the wait for Claude.
* **N5 — Cache field types and the source-note display form are incomplete (C29, §6.4, §6.5).** §9.1 serialises windows `resets_at` as ISO-8601 but gives no serialisation for `fetched_at` / `last_attempt_at`. The §6.5 source note carries `fetched_at` with no display form stated, so the relative-only timestamp rule (D14) may or may not apply to it.
* **N6 — Expired-token wording implies a renewal was attempted (C2, §6.5).** "Your sign-in expired and couldn't be renewed" describes a renewal attempt that D8 deletes. "Your sign-in expired" plus the existing next step is the accurate wording.
* **N7 — An unknown Codex duration blanks the whole card (C18, §6.3).** Unknown duration → `unexpected_response` discards every window, including valid ones. That is what C18 required, but it means the provider adding one new window kind takes down the two windows the user already had. Record the trade-off in §6.3 or soften to "ignore the unknown window, show the known ones, log the unknown duration".

## What did land (verified in the text)

The probe gate (§9.0, D18), the deleted refresh (D8), fetch-on-view with the background refresher rejected in §7 (C3), the Codex `accountId` fingerprint with the Claude limitation stated (C9), the setup-token probe fact (C12), the corrected config paths with restart-required kill switch (C15), the cited Codex protocol with the full handshake sequence (C21), the probe-gated Claude `limits[]` handling (C22), the "imported" ban applied through the text (C23), the labelled numbers and §3 acceptance criteria (C24), per-provider cache files with the timestamp check (C25), the `ServerStatsCache` reuse rationale (C26), file-only detection stated in the §3 goals (C28), and the two-PR plan with per-phase Mac gates (C10/C30, subject to M4). The architecture change at the centre of the rewrite — fetch-on-view replacing the background refresher — is coherent and consistently applied across D6, §6.4, §7, and §9.

## What must change before approval

1. Resolve B1: make the Codex table and the `tokens`-precedence sentence agree, and add the both-present row.
2. Resolve B2: name the precedence function and its return type, and fix the Claude test count from 5 to 7.
3. Resolve M1–M5: remove the render states from the reason-code table and settle stale-vs-failed precedence; give `Retry-After` a stored home or narrow the rule; rewrite the consent note for the real trigger and add a per-provider off-ramp; state the Phase A Claude card state; give each UI string one owner.
4. Answer M6–M9 or record the trade-off in the design: per-window staleness, `refresh()` ownership, nullable `resets_at` plus the `last_status` mapping, and 401 retry timing with a rotation-aware re-arm.
