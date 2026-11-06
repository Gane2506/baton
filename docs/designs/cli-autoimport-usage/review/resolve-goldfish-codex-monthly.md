# Goldfish review — Codex 43200/monthly amendment (§9.0 probe resolve, §11 R1–R4)

* **Scope:** ONLY the Codex 43200/monthly amendment — the §9.0 probe resolve, §11 R1–R4, and every section it touched (§6.3, §6.4, §9.0, §9.1, §10, D14, D20, §1 word list).
* **Files read (only):** `design.md`, `probe/PROBE.md`, `probe/01-codex-ratelimits.json`, `probe/02-claude-usage.json`, `probe/04-setup-token.json`, `probe/05-06-identity-splits.json`, `probe/07-versions.json`, `probe/claude-auth-info.json`.
* **Reviewer:** fresh Goldfish reviewer, zero prior context. Design phase only; no implementation files written.
* **Verdict: 3 BLOCKING, 5 NIT.** The monthly mapping itself (43200 → monthly, duration-derived, plan-independent) is sound and the all-unknown rule does fix the observed empty-card failure. But the resolve records wrong arithmetic in two places, never defines the directory set its PATH fix depends on, and leaves stale window-set statements that contradict the new three-kind model.

## Method note on the arithmetic check

Probe time per `probe/PROBE.md`: 2026-11-05 ~21:23 EST = 2026-11-06 02:23 UTC.
`resetsAt` 1795687699 = 2026-11-26 10:08:19 UTC (Thursday 2026-11-26 05:08:19 EST).
Delta = 1,755,919 s = **20.32 days**, not ~27 days. Implied window start
(`resetsAt` − 30 days) = 2026-10-27 10:08 UTC, so the probe ran **~9.7 days**
into the window, not ~3 days in. Verified with `date -d @1795687699` and
direct epoch subtraction. 100 − 48 = **52% left is correct**; only the
time-remaining figures are wrong.

---

## BLOCKING

### B1 — Wrong time-remaining arithmetic in D14 and R1: "resets in 27 d" should be "resets in 20 d"

* **Sections/lines:** D14 (~line 525–526); §11 R1 "Observed" (~lines 2039–2040) and R1 "Decided" (~line 2044).
* **What the design says:**
  * D14: the compact line for this account reads "Monthly 52% left · resets in 27 d" — "(the Mac's observed values at probe time, §6.3)".
  * R1: "At probe time the window had ~27 days left (1795687699 minus the probe time ≈ 2.35M seconds), coherent with a 30-day window ~3 days in."
  * R1 "Decided" repeats the "Monthly 52% left · resets in 27 d" compact line.
* **What is true:** 1795687699 − probe epoch (2026-11-06 02:23 UTC) = **1,755,919 s ≈ 20.3 days**, not ≈ 2.35M s / ~27 days. Under D14's own rounding rule (≥ 1 day → days only, rounded down) the correct rendering is **"resets in 20 d"**. The window started 2026-10-27, so the probe ran ~10 days in, not ~3 days in. (A 27-day remainder would require a probe time around 2026-10-30, a week before the probe ran; no reading of the probe time — EST or UTC — yields 27 days.)
* **Why blocking:** this is the resolve's own recorded observation of the probe, and the D14 example is the exact user-visible string the implementer copies into the template/tests. Shipping it means the design's flagship example is wrong on the very device it was verified on, and any test asserting "27 d" from the probe fixture (`usedPercent` 48, `resetsAt` 1795687699) would fail against a correct implementation — or worse, pass against an implementation that reproduces the wrong arithmetic.
* **Fix:** in D14 and in both R1 places, replace "resets in 27 d" with **"resets in 20 d"**, replace "≈ 2.35M seconds" with **"≈ 1.76M seconds"**, and replace "~3 days in" with **"~10 days in"**. §6.3 itself is fine (it records only the raw `usedPercent` 48 / `resetsAt` 1795687699 and derives nothing from them) and needs no change. The "52% left" half is correct and stays.

### B2 — The darwin-search-dirs set is never defined: the PATH fix is unimplementable as written

* **Sections/lines:** §6.3 (~lines 822–834); §9.1 Codex adapter (~lines 1551–1552) and test list (~line 1799); §11 R4 (~lines 2087–2095).
* **What the design says:** `resolve_executable` covers "the dashboard's darwin search dirs, not just the launch PATH", and "the `PATH` passed to the child is the launch PATH augmented with those dirs". Both sides of the fix (binary lookup and child env `PATH`) are specified — but the set itself is never enumerated anywhere in the design, and no single source of truth (module, function, or config key) is cited for it.
* **Why blocking:** the one directory the probe proves necessary is `/opt/homebrew/bin` — the launch shell's PATH lacked it and the `codex` wrapper died with `env: node: No such file or directory` (PROBE.md item 1, method note). An implementer reading only this design cannot tell whether `/opt/homebrew/bin` is in "the darwin search dirs", nor whether the set also includes the dirs the probe shows the CLIs actually live in (`~/.npm-global/bin/codex`, `~/.local/bin/claude`, per `probe/07-versions.json`). If the implementer's set omits `/opt/homebrew/bin`, the exact observed failure recurs on any thin-PATH (launchd/double-clicked) dashboard launch — the fix silently fails to fix what R4 exists to fix. A phrase that points at a set defined nowhere is not a specification.
* **Fix:** enumerate the set in §6.3 (and repeat it in R4), e.g. `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, `~/.npm-global/bin` — or, if the set already lives in code, cite the exact defining location (module + symbol) and state that §6.3/R4 defer to it, with `/opt/homebrew/bin` explicitly confirmed in the set. Either way, the §9.1 thin-PATH test should name the dirs: launch PATH without `node`, binary resolved via the search dirs, child `PATH` containing `/opt/homebrew/bin` so the wrapper's `env node` resolves.

### B3 — Window-set statements in the Overview, §3 Goals, and §9.1 template instruction still describe a two-window world, contradicting §6.3's three-kind model

* **Sections/lines:** Overview (~line 17); §3 Goals (~lines 164, 167); §9.1 modified files, `templates/connections.html` bullet (~line 1750).
* **What the design says:**
  * Overview: "the 5-hour session window and the weekly window, each with its reset time. Codex may report only the weekly window (its 5-hour window is suspended for many accounts)."
  * §3 Goals: "Show what is left for the session window and the weekly window per provider… Codex may report only the weekly window."
  * §9.1: template "per-window bars labelled "Session" and "Weekly"" — no "Monthly".
* **Contradiction:** §6.3 now derives three kinds (session / weekly / monthly), the §1 word list defines the Monthly window and label, §6.6 (~line 1177) says bars are labelled "Session", "Weekly", and "Monthly", and the probe's observed account is monthly-only — the exact case the Overview/Goals sentences say cannot happen ("only the weekly window"). The Overview and Goals are the design's statement of what the product does; as written they exclude the monthly window the resolve just added. The §9.1 template line directly contradicts §6.6: an implementer following §9.1 literally omits the Monthly bar label.
* **Why blocking:** the resolve's job was to make the monthly window first-class; three of the sections it should have touched still assert the old window set. This is a straight internal contradiction, not a wording preference.
* **Fix:** Overview → "the session, weekly, and monthly windows the provider reports — Codex may report only one window (its 5-hour window is suspended for many accounts; the Mac's free-plan account reports only a 30-day window)". §3 Goals → "Show what is left for the windows the provider reports (session, weekly, and — for Codex — monthly), with reset times… Codex may report only one window." §9.1 template bullet → 'per-window bars labelled "Session", "Weekly", and "Monthly"' to match §6.6.

---

## NIT

### N1 — "Full reset (Monthly)" is used as corroboration without saying what it is: a credit title, not a window

* **Sections/lines:** §6.3 (~lines 899–900); §11 R1 (~lines 2040–2041, 2054–2055).
* **What the design says:** "The 43200 reading as monthly is corroborated by the response's own rate-limit-reset credit titled 'Full reset (Monthly)'" / "the provider's own credit title says 'Monthly'."
* **Check against the probe record:** `probe/01-codex-ratelimits.json` confirms the credit exists with `title: "Full reset (Monthly)"`, `resetType: "codexRateLimits"`, `grantedAt` 1791414652, `expiresAt` 1794006652, `status: "available"`. But it is a one-off free reset credit, not a window field — and its `expiresAt` (2026-11-06 23:10 UTC) falls **~19.5 days before** the window's `resetsAt` (2026-11-26), so the credit's lifecycle does not track the window's cycle at all. The title is consistent with a monthly concept on the provider's side (the credit's own grant→expiry span is also exactly 30 days), but it cannot corroborate the window's duration or reset.
* **Why only a nit:** the mapping does not need the credit — 43200 minutes is exactly 30 days, which alone justifies "monthly". The design never claims the credit *is* a window, so this is not a false claim, just an overstated corroboration.
* **Fix:** qualify both places, e.g. "consistent with the response's own rate-limit-reset credit titled 'Full reset (Monthly)' (a credit title, not a window field; the credit expires 2026-11-06, before the window reset)". Keep 43200 = exactly 30 days as the deciding evidence.

### N2 — Blanket fatal rules (`primary` null, duplicate durations) vs R2's per-window-drop rationale: precedence for mixed inputs is unstated

* **Sections/lines:** §6.3 (~lines 883–885); §9.1 (~lines 1575–1576, 1810–1812); §11 R2.
* **What the design says:** unknown duration → drop that window, show the known ones; all unknown → `unexpected_response`. Separately: "`primary` null and duplicate durations remain `unexpected_response`" — stated unconditionally, and the test list pins "`primary` null → `unexpected_response`" and "duplicate `windowDurationMins` → `unexpected_response`" with no qualification.
* **Hole:** the two rules point opposite ways for mixed inputs the design never addresses:
  * `primary` null + `secondary` carrying a known duration (e.g. 10080): R2's principle ("a partially-known response still shows its known windows") says show the weekly window; the blanket rule says the whole response is `unexpected_response`.
  * `primary` 300 + `secondary` 300 (known but duplicated): same conflict — known windows exist, yet the outcome is fatal.
  * (The one-known-one-unknown-duration case the task asked about *is* unambiguous: drop the unknown, show the known — stated identically in §6.3 and §9.1. This hole is only about the null/duplicate carve-outs.)
* **Why only a nit:** the test list does pin an outcome for both inputs (fatal), so an implementer following §9.1 literally will not guess — the behaviour is determinable, just unreconciled with R2's stated rationale. It cannot arise on the Mac's observed shape (secondary null).
* **Fix:** add one sentence to §6.3 stating the precedence explicitly, e.g. "`primary` null and duplicate durations are fatal to the whole response even when another window is known — the per-window drop applies to unknown *durations* only." Or, if the intent is the opposite (a known secondary still renders), say that instead and adjust the §9.1 tests. Either resolution closes the hole; the current text leaves the reader to infer it from the test list.

### N3 — Per-window field failure (missing/mistyped `usedPercent`/`resetsAt` on a known duration) is ambiguous between drop-the-window and fail-the-response

* **Sections/lines:** §6.1 (missing/mistyped expected fields → `unexpected_response`); §6.3 unknown-duration rule; §9.1 test list.
* **What the design says:** §6.1 states globally that "missing or mistyped expected fields produce `UsageUnavailable(reason="unexpected_response")`". §6.3's drop rule covers only unknown *durations*. Neither says what happens when a window has a known duration but a missing/mistyped `usedPercent` (or a non-numeric `resetsAt`): drop that window and show the rest (mirroring the duration rule), or fail the whole response (per §6.1)?
* **Fix:** state the rule in §6.3 and add a §9.1 test, e.g. "a window whose duration is known but whose `usedPercent`/`resetsAt` is missing or mistyped is dropped like an unknown duration; if no window remains, `unexpected_response`." If the intent is whole-response fatal, state that and note it as an explicit exception to the drop philosophy.

### N4 — Positional language survives alongside "never from primary/secondary position"

* **Sections/lines:** §6.3 (~lines 866–867); §10 Phase A gate (~lines 1977–1984).
* **What the design says:** windows are derived from `windowDurationMins`, "**never from primary/secondary position**" — but the same paragraph glosses a null `secondary` as "(the 5-hour window is suspended for many accounts since ~July 2026)", i.e. it assigns a positional meaning to `secondary` after forbidding positional derivation. §10's gate likewise keys the expected copy to "`secondary` is null and the primary is monthly" rather than to "only the monthly window is present" (§6.3's actual trigger). On the Mac's observed shape the two phrasings coincide, so no gate outcome changes — but a future response with `primary` monthly + `secondary` weekly (two windows, no copy) vs `primary` weekly + `secondary` monthly (two windows, no copy) is decided by the §6.3 rule, while the §10 phrasing invites a position-based reading.
* **Fix:** in §6.3, rephrase the parenthetical to "(`secondary` may be null; a null `secondary` says nothing about which window is absent — the surviving window is identified by its duration)". In §10, key the gate to the window set ("one window, and it is monthly") rather than to `primary`/`secondary` positions.

### N5 — Monthly max-age is consistent, but its wiring and its test are missing

* **Sections/lines:** D20 (~lines 589, 595); §6.4 (~lines 1029, 1043–1051); §9.1 test list.
* **What checked out:** D20 (monthly 24 h, same as weekly) and §6.4 agree — `derive_display_state` takes a `monthly_max_age: timedelta` parameter, the staleness bullet says "the monthly window while younger than 24 h", and the kind mapping ("monthly kinds use `monthly_max_age`") covers the new kind. No contradiction.
* **What is missing:**
  1. **No call-site wiring.** No §9.1 bullet (views/routes/fetcher) says who passes `monthly_max_age=timedelta(hours=24)` — or the per-provider `session_max_age` (Codex 20 min vs Claude 60 min, D20) — to `derive_display_state`, nor whether the function has defaults. An implementer must invent the wiring.
  2. **No monthly-staleness test.** The §9.1 test list covers the monthly happy path, monthly-only copy, and unknown durations, but no case asserts a monthly window older than 24 h is hidden / the card goes `stale`, or that a 23-hour-old monthly window still shows.
* **Fix:** add one sentence to §6.4 (or the §9.1 views/routes bullets) naming the caller and the values passed (monthly 24 h; session per provider), and add a §9.1 test: "monthly window with `fetched_at` 25 h ago → hidden, state `stale`; 23 h ago → shown".

---

## Checked and clean (no finding)

1. **Monthly-only copy string identical everywhere it appears.** "Your plan has no separate 5-hour or weekly limit — monthly only" (em-dash, same words) in §6.3, §9.1 test list, §10 Phase A gate, and §11 R1 — verified character-for-character across the line-wrapped occurrences. The weekly-only copy is likewise identical in §6.3, §9.1, and §10.
2. **Unknown-duration rule stated identically in §6.3 and §9.1** (drop the window, show known windows, log once per process start; all unknown → `unexpected_response`, never an empty snapshot), and it fixes the observed failure two ways: 43200 is now a known duration so the Mac account's only window renders, and any future all-unknown response yields honest "usage unavailable" instead of an empty card. One-known-one-unknown outcome is unambiguous.
3. **No overclaim about free plans.** R1 explicitly disclaims it ("The design does not claim all free plans report monthly — one account is one account; the rule derives the window from the duration, never the position or the plan"), and no other amended section implies otherwise; §10's expectation is scoped to the Mac's observed account.
4. **`planType` location consistent.** §6.3 says read from `result.rateLimits.planType`; the probe JSON confirms `"planType": "free"` inside `result.rateLimits` (and duplicated in `rateLimitsByLimitId`, which §6.3 correctly says to ignore), while the record's top-level `planType: null` is the probe script's own summary field, exactly as §6.3 explains. Consistent with §6.3's "Parse `result.rateLimits` only".
5. **§9.0 / §10 gates agree with the resolve.** Phase A conditional-go → go after R1–R4; the §10 Mac gate expects the monthly-only copy for the Mac's observed shape and records either outcome. No contradiction between §9.0, §10, and §11 on the Codex half.
6. **Monthly max-age value consistent** (see N5 — value agrees; only wiring/test missing).
7. **"52% left" correct** (100 − 48), and 43200 minutes = exactly 30 days, so the monthly reading itself is well-founded independent of the credit-title corroboration (N1).
