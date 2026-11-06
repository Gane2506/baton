# Re-audit — comprehension (re-implementability)

* **Design reviewed:** `docs/designs/cli-autoimport-usage/design.md` (1,371 lines, amended draft of 2026-11-06)
* **Reviewer:** Comprehension Goldfish, re-audit (single permitted re-audit after the resolve pass)
* **Standard:** re-implementation standard — could an implementer re-implement the design from this document alone, without asking the author anything?
* **Fix-check scope:** consolidated items C14, C17, C18, C21, C22, C25, C27, C30 from `review/consolidated_review_report.md`
* **Verdict: NOT re-implementable.** The rewrite closes several gaps outright (probe gate, refresh deletion, fetch-on-view, config paths, the "imported" ban), but the data contract, the state machine, the Claude parser, and parts of the Codex protocol still leave an implementer guessing at behaviourally significant choices. The findings below are ordered worst first.

## Fix-check summary (the eight items this re-audit was asked to check)

| Item | Closed? | Short reason |
|---|---|---|
| C14 data contract | No | Resolution function unnamed; shared reader unnamed with no signature; poll JSON fields, formats, and state vocabulary undefined; `source_version` contradicts itself |
| C17 state machine | No | Overlapping display states have no precedence; `not_applicable_api_key` is both "not a fetch outcome" (§6.5) and a reason code in a §9 test |
| C18 window rules | Partly | Codex rules are written; Claude splits-only / flat-plus-`limits[]` merge rules are missing; null `resets_at` has no representation |
| C21 Codex protocol | Mostly, with gaps | Spawn sequence, env, timeouts, and cap are specified; `clientInfo` title, `<package version>` source, error-object mapping, and timeout/cap scope are not |
| C22 Claude fields, `limits[]` | No | Access-token field names and the `limits[]` entry shape are probe-gated (as intended), but the expired-vs-rejected 401 distinction is not probe-gated and is still unspecified |
| C25 cross-instance | No | The floor timestamp (`fetched_at` vs `last_attempt_at`) is ambiguous; the "kills the lost-update race" claim is overstated for same-file concurrent writes |
| C27 copy / timestamps | Partly | One term, one button, relative-only times are stated; rounding, button transition, flash texts, and the disabled-vs-ignored Refresh conflict are not settled |
| C30 §9 / §10 values | Partly | Paths, phases, timeouts, and most tests carry values; the fake-clock seam is still unnamed, the "Check usage" click flow is undefined, and the Phase B live-comparison gate has no tolerance |

Items outside the eight that were verified as closed in the text: C1 (probe checklist, §9.0), C2 (refresh deleted, D8), C3 (fetch-on-view, D6/§6.4), C4 (opt-in, D3), C5 (manual floor equals cadence floor, D6), C9 (Codex `account_hash`, Claude limitation stated), C10 (two PRs, D4/§9.1–§9.2), C15 (config paths and kill switch, §6.9), C19 (splits shown, §6.3/D13), C20 (precedence tables, §6.2), C23 ("imported" ban — the only uses of "import"/"imported" left are the §1 definition of the ban and the §7 rejected alternative, lines 62, 212, 271, 967), C26 (cache-reuse rationale, §6.4).

---

## Blockers

### B1 — The precedence resolution function has no name, signature, or return type (C8, C14)
* **Section:** §6.1 (lines ~556–566), §9.1 provider_auth bullet (lines ~1148–1158)
* **Finding:** §6.1 says the §6.2 tables are "the test list for one resolution function in `provider_auth.py`" and §9.1 repeats "the precedence resolution function (one function, §6.2 tables as its test list — one case per row: 5 Claude, 4 Codex)". Neither place names the function, its arguments, or what it returns. The design says it "returns that resolution" without defining the resolution type: does it return a `ProviderAuth`, a `LoginSource`, a new dataclass, or a tuple of (effective credential, present set, measured credential)? An implementer cannot write the function, its tests, or the UI code that "renders whatever it returns" without inventing this.
* **Question the implementer must ask:** "What is the function called, what arguments does it take, and what exactly does it return — field by field?"

### B2 — The "one shared reader" has no name, signature, or behaviour (C14)
* **Section:** §6.1 (lines ~567–572), §6.7 (line ~879), §9.1 provider_auth bullet
* **Finding:** The design says the adapter reads token values "through one shared reader in `provider_auth.py` (the existing credential-file reader, generalised)" and that "no second file reader exists anywhere". There is no such reader in the code the design points at: the existing code has only private presence checks (`_credentials_login` returning a bool, `_codex_auth_file` returning a string kind) — verified by reading `src/jev_agent/core/provider_auth.py`. No function name, return type, or error behaviour is given anywhere: what does it return for a missing file, a corrupt file, a file with the wrong shape, or a permission error — and does it return values, paths, or parsed objects?
* **Question the implementer must ask:** "What is the reader function called, what does it return, and what does it do when the file is missing, corrupt, or unreadable?"

### B3 — Claude token field names are missing, and expired vs rejected cannot be told apart (C22, C14)
* **Section:** §6.3 Claude (lines ~680–705), D8 (lines ~361–372), §6.5 taxonomy (lines ~792–812)
* **Finding:** The design says the adapter "reads the access token and `expiresAt` from the `claudeAiOauth` object" and that field names are "per the probe record" (§9.2). The access-token field name itself is never stated, so the read path cannot be written from the document alone — that part is probe-gated by design and is acceptable only if the probe actually records it. What is not probe-gated and still missing: how the adapter decides between `token_expired_refresh_failed` and `unauthorized`. Both are 401s from the same endpoint. D8 says "if the token is expired, the fetch fails with reason `token_expired_refresh_failed`", but no rule says how expiry is detected — by comparing `expiresAt` to the clock before the call, by a 401 after the call, or by something else — and §6.3 gives no `expiresAt` parsing rule (missing field? null? clock skew?).
* **Question the implementer must ask:** "What is the exact JSON key for the access token, and what test decides that a 401 means 'expired' rather than 'rejected'?"

### B4 — The `limits[]` parser cannot be written from this document (C22, C18, C19)
* **Section:** §6.3 Claude (lines ~680–705), §9.0 probe step 2, §9.2 claude.py bullet
* **Finding:** The design says the parser "reads the flat keys when present and non-null, and parses `limits[]` when they are null" and to "prefer the structured array when the flat keys are null". No entry shape is given: no field names inside a `limits[]` entry, no mapping from an entry to a window kind (`session` / `weekly` / `weekly_opus` / `weekly_sonnet`), and no rule for when both flat keys and `limits[]` carry data (use both? prefer one? merge? what if they disagree?). The probe is supposed to record the structure, but the design never states what the parser does with a `limits[]` entry once the probe has recorded it — the mapping rule itself is absent, not just the field names.
* **Question the implementer must ask:** "For one `limits[]` entry, which fields carry the window kind, the used percent, and the reset time — and if flat keys and `limits[]` both have data, which one wins?"

### B5 — `resets_at` is required by the model but may be null in the source (C14, C18)
* **Section:** §6.1 model (lines ~503–516), §6.3 Claude (lines ~694–696), §6.3 Codex (lines ~650–656)
* **Finding:** `UsageWindow.resets_at` is typed `datetime` with no `None` allowed, yet §6.3 says Claude `resets_at` "may be null". No representation is given for a window with no reset time: is the window dropped, is `resets_at` made optional, or is a sentinel used — and what does the card show ("resets …" with what text)? Separately, Codex `resetsAt` is unix seconds and Claude `resets_at` is ISO-8601; the design never says whether the resulting `datetime` is timezone-aware or naive, or in which timezone, so the "provider `resets_at` minus the host clock" arithmetic in §6.7 cannot be written without guessing.
* **Question the implementer must ask:** "What goes in `resets_at` when the provider sends null, and are these datetimes timezone-aware UTC or naive local time?"

### B6 — `source_version` contradicts itself (C14, C29)
* **Section:** §6.1 (lines ~515–528), D5 (lines ~314–318), §6.9 (lines ~957–960)
* **Finding:** The `UsageSnapshot` field comment says `source_version: str  # CLI version / adapter version`. Two paragraphs later §6.1 says the parser version is a module constant, e.g. `PARSER_VERSION = 1`, "recorded in `source_version`". Those are different values: a CLI version string, an adapter version, and a parser integer cannot all be the one string field, and no format is given for combining them.
* **Question the implementer must ask:** "What exact string goes in `source_version` — the CLI version, the adapter version, the parser version, or some combination, and in what format?"

### B7 — Overlapping display states have no precedence (C17, C11)
* **Section:** §6.4 display derivation (lines ~758–772), §6.5 state matrix (lines ~814–850), D19 (lines ~460–468)
* **Finding:** The derivation lists six cases, but real states overlap and no case wins: a snapshot older than max-age with a fetch running is both `collecting` ("last value with its age + Checking…") and `stale` (value hidden, D19) — does the running fetch show the old value or hide it? A snapshot older than max-age whose last refetch failed is both `stale` and `failed` — and the `failed` matrix row says the "last successful value, if any" is shown as "last seen <relative time>", which reads as showing the value that D19 says to hide. A snapshot younger than max-age with a failed refetch shows a sub-line; a snapshot older than max-age with a failed refetch shows… the design never says.
* **Question the implementer must ask:** "When a snapshot is stale and a fetch is running or has just failed, which row of the matrix renders — and is the number shown or hidden?"

### B8 — `not_applicable_api_key` is both "not a fetch outcome" and a §9 test expectation (C17, C29)
* **Section:** §6.5 (lines ~792–794), §9.1 test list (line ~1248), §9.2 test list (lines ~1300–1302)
* **Finding:** §6.5 states `no_login` and `not_applicable_api_key` are "not fetch outcomes — they are render states, listed in the matrix below". But the §9.1 test list says "API-key auth → reason `not_applicable_api_key`, usage section hidden, fake transport receives zero calls", and §9.2 repeats it for Claude. If it is a reason code, it belongs in `UsageUnavailable.reason` and the fetcher must return it; if it is only a render state, the fetcher returns nothing and the test is misworded. `not_checked_yet` has the same problem in the other direction: it sits in the reason-code table (§6.5) while describing a state in which no fetch has happened at all.
* **Question the implementer must ask:** "For API-key auth, does the fetcher return a `UsageUnavailable` with reason `not_applicable_api_key`, or does it return nothing and the template renders the state — and which one does the test assert?"

### B9 — Codex `initialize`: title missing, version source undefined, error objects unmapped (C21)
* **Section:** §6.3 Codex (lines ~636–650), §9.1 codex.py bullet (lines ~1104–1120)
* **Finding:** The design specifies `clientInfo` as `{"name":"jev","version":"<package version>"}` and says params carry "only `clientInfo`, no `protocolVersion`". The consolidated report's cited protocol source lists `clientInfo` as carrying `name`, `title`, and `version` — `title` is dropped here with no note saying whether it is optional or omitted on purpose. `<package version>` has no source: which package, read how (`importlib.metadata`, a `__version__` constant, something else)? The 15-second timeout's scope is undefined (whole spawn-to-answer, or per read?), as is the 4 MiB cap (total output, or per line?). Error responses are unmapped: a line `{"id":2,"error":{...}}` has no stated mapping to `app_server_failed` vs `unexpected_response`, and a missing or malformed `initialize` result has no stated outcome.
* **Question the implementer must ask:** "Does `clientInfo` need a `title`, where does `<package version>` come from, what does the 15-second timeout cover, and what reason code does an `error` object on id 2 produce?"

### B10 — Codex response fields are listed but their outcomes are not mapped (C21)
* **Section:** §6.3 Codex (lines ~650–668), §6.5 taxonomy (lines ~792–812)
* **Finding:** §6.3 names `ordinaryUsageAllowed` (`false` = usage blocked for the account), `spendControlReached`, and `rateLimitReachedType` (names the exhausted window), then says only that "credit ids, descriptions, and `rateLimitUpsell` are ignored". It never says what happens when `ordinaryUsageAllowed` is `false` (which reason code, which card wording, is the snapshot still shown?), what `spendControlReached` being true means, or whether `rateLimitReachedType` changes anything. The taxonomy also gives Codex wording for `unauthorized` ("Codex rejected this sign-in") but §6.3 never says which Codex outcome produces `unauthorized` — the only Codex failure named there is the auth-required failure, which §9 maps to `app_server_failed`.
* **Question the implementer must ask:** "When `ordinaryUsageAllowed` is false, what reason code and card text result — and which Codex outcome ever produces `unauthorized` instead of `app_server_failed`?"

### B11 — The cross-instance floor timestamp is ambiguous, and the race claim is overstated (C25)
* **Section:** §6.4 single-flight / cross-instance (lines ~744–756), D5 (lines ~308–325), D6 (lines ~326–345)
* **Finding:** D6 defines the floor as "minimum time between attempts, manual or otherwise", which is measured from the last attempt (`last_attempt_at`). But §6.4 says a second process "starts a fetch only if the cached snapshot is older than the floor" and §9.1's cross-instance test says a second process "that sees a `fetched_at` younger than the floor starts no fetch" — measured from the last success. Those differ exactly when it matters: a failed attempt 2 minutes ago with a 3-hour-old snapshot would start a fetch under the `fetched_at` rule and be blocked under the `last_attempt_at` rule. Also, D5 claims one file per provider "kills the lost-update race (each process writes only its own provider's file)" — but two dashboard processes write the *same* provider's file, and §6.4 concedes the concurrent write is "last-writer-wins on the same shape" with no guard against a stale write overwriting a fresher one.
* **Question the implementer must ask:** "Is the cross-instance check against `fetched_at` or `last_attempt_at` — and is there any guard against an older snapshot overwriting a newer one in the same file?"

### B12 — The "Check usage" click flow is undefined, and Phase A leaves the Claude card unspecified (C4, C30, C10)
* **Section:** D3 (lines ~291–300), D6 (lines ~326–345), §9.1 routes_connections bullet (lines ~1170–1190), §9.1 Phase A checkpoint (lines ~1092–1096)
* **Finding:** Clicking "Check usage" is the opt-in moment: the choice "persists in the cache record (`enabled: true`)". The design never says what the POST refresh route does on that first click — does it start a fetch immediately and then redirect, or only set `enabled` and redirect, leaving the fetch to the next page render? The route's stated response is "via the dashboard's `act()` redirect + flash" with no flash text for success, and no statement of where the redirect goes. Separately, Phase A ships the shared plumbing, UI, precedence, and wording, but the Claude adapter lands in Phase B — the design never says what the Claude card shows in Phase A (a "Check usage" button that does nothing? a hidden usage block? the full block with no adapter behind it?).
* **Question the implementer must ask:** "On the first 'Check usage' click, does the route start a fetch or only set `enabled` — and what does the Claude card render in Phase A before its adapter exists?"

### B13 — The live-poll JSON is underspecified (C14, C27)
* **Section:** §6.4 live poll (lines ~780–786), §9.1 routes_connections bullet (lines ~1176–1184)
* **Finding:** The poll shape is `{state, windows: [{kind, left_percent, resets_at}], fetched_at, reason, plan}`. Undefined: the vocabulary of `state` (the display states `never`/`collecting`/`ready`/`failed`/`stale`, or the `last_status` vocabulary `ok`/`rate_limited`/`auth_error`/`spawn_error`/`unexpected`/`disabled`, or something else); who computes `left_percent` (adapter, fetcher, or poll serializer) and whether it is an integer or a float; and the serialisation format of `resets_at` and `fetched_at` (ISO-8601 strings? unix seconds?). The in-memory model carries `used_percent` while the poll carries `left_percent`, and the conversion point is never named.
* **Question the implementer must ask:** "What values can `state` take, who computes `left_percent` from `used_percent`, and are the timestamps ISO strings or unix seconds?"

### B14 — The fake-clock seam is still unnamed, and the adapter protocol takes no arguments (C30)
* **Section:** §8 testability (lines ~1010–1016), §9.1 test files (lines ~1130–1136), §6.1 protocol (lines ~495–500)
* **Finding:** C30 required the fake-clock seam to be named. §8 says "fake clock named in §9", but §9 only says the tests use "a fake clock" — no class, module, or injection point is named, and no existing clock seam in the codebase is cited. Separately, `ProviderUsageSource.fetch()` takes no arguments, so the token, the provider identity, and the cache must reach the adapter through its constructor — but no constructor signature is given for either adapter. The Codex bullet names the spawner injection `PopenFn` "matching the existing `popen` injection in the flows" without giving its signature (the existing flows use `Callable[..., Any]`, verified in `provider_auth.py`).
* **Question the implementer must ask:** "What is the fake clock called and where is it injected — and what arguments do the adapter constructors take?"

---

## Majors

### M1 — Claude window edge cases: splits-only, and flat keys plus `limits[]` together (C18, C19)
* **Section:** §6.3 Claude (lines ~690–700)
* **Finding:** The rule "only one of the two headline windows present → show the one present; neither present → `unexpected_response`" covers the headline keys only. It never says what happens when both headline keys are null/absent but a split (`seven_day_opus`) is present — is that "one window present" or "neither present"? And when flat keys and `limits[]` both carry data, there is no merge or dedupe rule: could the same weekly window appear twice with different numbers?
* **Question the implementer must ask:** "If only a split window is present, do I show it — and if flat keys and `limits[]` both report the weekly window, which number wins?"

### M2 — `resets_at` in the past: "keep the bar at its last value" has no first-fetch case (C18)
* **Section:** §6.3 Codex (lines ~662–664), §6.3 Claude (implied), §6.5 matrix
* **Finding:** "ResetsAt in the past renders as 'resetting…' and keep the bar at its last value until the next fetch." On a first fetch there is no last value. The design never says where "resetting…" appears (in place of the reset time? in place of the whole line?) or what the bar shows on a first fetch with a past reset time.
* **Question the implementer must ask:** "On the first fetch, when there is no last value, what does the bar show when the reset time is already past?"

### M3 — The 401/403 "retry once, then stop" rule leaves its scope and its blind spot undefined (C7)
* **Section:** D7 (lines ~346–360), §6.5 `unauthorized` row, §9.2 test list (lines ~1289–1291)
* **Finding:** "Retry once" is not scoped: once per fetch attempt, once per login state, or once per dashboard process? The re-arm condition (a) — "the detected login changes (kind or source path, per live detection)" — cannot see the common case: the user signs in again in the CLI, which rewrites the same file with a new token under the same kind and path. Only the manual-Refresh re-arm (b) covers that, which the design never says outright. It is also never stated whether the stop applies to the Codex adapter at all.
* **Question the implementer must ask:** "Once per what does 'retry once' mean, and does re-signing in with the same file path re-arm the stop or does only a manual Refresh?"

### M4 — `Retry-After`: date parsing, next-retry display, and disabled-vs-ignored Refresh conflict (C6, C27)
* **Section:** D7 (lines ~346–352), §6.5 `rate_limited` row (line ~802), §6.6 buttons (lines ~856–862), D6 (lines ~340–344)
* **Finding:** `Retry-After` may be seconds or an HTTP-date, but no parsing rule is given (which parser, what counts as unparsable, how a date is converted to a wait given host clock skew — §6.7 says no clock correction is attempted). The card "shows the next-retry time" with no format: a clock time would break the relative-only rule (C27), a relative time is never stated. And §6.5 says the Refresh button is "disabled until then" while D6/§6.6 say a click inside the floor is "ignored (redirect + flash naming the wait)" — a disabled button and an ignored click with a flash are different behaviours.
* **Question the implementer must ask:** "Is the next-retry time shown as a clock time or a relative time, and is the Refresh button actually disabled or clickable-but-ignored with a flash?"

### M5 — The rounding rule contradicts its own example (C27)
* **Section:** D14 (lines ~440–452), §1 glossary (lines ~64–80)
* **Finding:** The rule is "largest unit keeping the number at 1 or above, rounded down" with examples "9 min", "2 h 14 min", "3 d". "2 h 14 min" uses two units, which the largest-unit rule does not produce (it would give "2 h"). Unspecified: 90 minutes ("1 h" or "1 h 30 min"?), a duration of 3 days and 4 hours ("3 d" or "3 d 4 h"?), and percent rounding at .5 (62.5% → 62 or 63, and by which rounding rule?).
* **Question the implementer must ask:** "When do I show two units instead of one, and does 62.5% round to 62 or 63?"

### M6 — The button label transition is undefined after a failed first fetch (C27, C4)
* **Section:** §6.6 buttons (lines ~856–862), §6.5 matrix (lines ~814–830)
* **Finding:** The button reads "Check usage" before the first fetch and "Refresh usage" after it. "After it" is never defined: after the first attempt (even a failed one), or after the first successful fetch? A provider whose first fetch fails and whose record now has `enabled: true` and no snapshot could show either label, and the §6.5 `failed` row and `never` row give no guidance.
* **Question the implementer must ask:** "After a first fetch that fails, does the button say 'Check usage' or 'Refresh usage'?"

### M7 — Flash texts and the consent-note provider name are not given (C27, C5, C16)
* **Section:** D6 (lines ~340–344), D16 (lines ~462–476), §9.1 routes_connections bullet (lines ~1176–1190)
* **Finding:** Three flashes are required but none is written out: the manual-refresh-inside-the-floor flash "naming the wait" (wait in what format — "try again in 9 min"?), the kill-switched flash ("usage is turned off" — the exact string is given only as a paraphrase), and the success flash, if any. The consent note says "Baton sends your sign-in to <provider>" — the design never says what replaces `<provider>` ("Claude", "Anthropic", "Codex", "OpenAI"?). The route's `act()` redirect target is also unstated (back to `/connections`, presumably, but never said).
* **Question the implementer must ask:** "What are the exact flash strings, what word replaces `<provider>` in the consent note, and where does the refresh route redirect?"

### M8 — `account_hash`: format and invalidation mechanics are missing; a cross-reference points at the wrong place (C9)
* **Section:** D5 (lines ~314–318), §6.3 Codex (lines ~664–666), §6.7 (lines ~890–898), §9.1 cache bullet (lines ~1122–1130)
* **Finding:** `account_hash` is "SHA-256 of `accountId`" with no encoding or format: hex digest string, and of the UTF-8 bytes of the id — the design never says. "A change discards the snapshot" is never made concrete: is the cache file deleted, treated as empty, or overwritten on the next fetch — and what does the card show in between? Separately, §6.7 says the Claude no-fingerprint limitation "is stated in §6.4/D5" — it is not in §6.4 or D5; it is stated in §6.7 itself.
* **Question the implementer must ask:** "What exact string is stored as `account_hash`, what happens to the cache file when it changes, and what does the card show until the next fetch?"

### M9 — The per-provider wording mapping has no name or shape (C13)
* **Section:** §6.1 (lines ~529–540), §6.5 taxonomy (lines ~792–812)
* **Finding:** The design requires the human wording to live "in one mapping per provider, inside that provider's adapter module (reason code → UI wording + next step)". No mapping name, constant name, function name, or return shape is given, and the §6.5 table in the design is a single central table — the implementer must decide how to split it per provider and what the shared layer receives (a string? a pair of wording + next step? a dict?). The mapping's keys are the reason codes, but the table also carries per-provider wording in the same row, so the split point is a judgement call.
* **Question the implementer must ask:** "What is the mapping called, what does a lookup return, and which rows belong in which adapter's mapping?"

### M10 — The "expected freshness" arithmetic is a sentence, not arithmetic (C24, C6)
* **Section:** §6.4 throttling (lines ~742–752), §9.0 probe step 3 (lines ~1076–1080)
* **Finding:** C24 required the expected-freshness arithmetic to be written into the design. §6.4 says only "a fetch on view succeeds unless the provider rate-limits; on a 429 the card shows the reason and the next-retry time". No numbers follow: no expected wait to a fresh value at the stated one-in-three success estimate, no statement of how often a view finds a fresh snapshot. Probe step 3 says to repeat the call over "one hour of normal Claude Code use" without saying how many attempts that means or at what spacing, so two runs of the probe would measure different things.
* **Question the implementer must ask:** "How many probe attempts, spaced how far apart, count as the success-rate measurement — and what wait to a fresh value should the design's arithmetic predict?"

### M11 — The Phase B live-comparison gate has no tolerance, and screenshot files are unnamed (C30)
* **Section:** §10 Mac verification Phase B (lines ~1352–1362), §10 screenshots (lines ~1322–1328)
* **Finding:** Phase B gate (1) requires Claude usage to show "percentages and reset times that match the numbers shown by `/usage` in Claude Code at the same time (same values — state the comparison in the PR body)". Usage percentages move as the CLI is used, and the two readings are never simultaneous, so "same values" with no tolerance (exact? within a point? within a few percent?) makes the gate unjudgeable. §10 also says to regenerate screenshots and "name the files in the PR body" without naming which files the Connections page shots are.
* **Question the implementer must ask:** "How close must the dashboard numbers be to `/usage` to pass — exact, or within what tolerance?"

### M12 — The `[usage]` config example is never shown (C15)
* **Section:** §6.9 (lines ~930–946), §9.1 config bullet (lines ~1160–1168)
* **Finding:** The schema, defaults file, `TOP_SECTIONS` registration, and restart requirement are all stated, but the actual TOML the user writes is never shown — not even the two commented template lines. An implementer must guess the section header and key spelling from the model field names (`[usage]` with `claude_code = true` / `codex = true` is implied, never written).
* **Question the implementer must ask:** "What are the exact commented template lines in `DEFAULT_CONFIG_TOML`?"

### M13 — `LoginSource` lives in two modules at once (C14)
* **Section:** §6.1 (lines ~542–556), §9.1 new files (lines ~1098–1102)
* **Finding:** §6.1 defines `LoginSource` under "`provider_auth.py` keeps detection, plus one shared credential-source accessor". §9.1's `usage/__init__.py` bullet lists `LoginSource(kind, source_path, cli_version)` as a dataclass defined in the new usage package. Both cannot be the definition; the design never says which module owns it and which imports it. The `get_login_source(provider)` argument type is also unstated — the string `"claude_code"` / `"codex"`, or something else?
* **Question the implementer must ask:** "Which module defines `LoginSource`, and what type is the `provider` argument to `get_login_source`?"

### M14 — `last_status` has a vocabulary but no mapping from reason codes (C17)
* **Section:** §6.4 display derivation (lines ~768–772), §6.8 (lines ~916–926)
* **Finding:** `last_status` vocabulary is `{ok, rate_limited, auth_error, spawn_error, unexpected, disabled}`. No mapping says which reason code produces which status: `token_expired_refresh_failed` → `auth_error`? `network_error` → which of these? `timeout`? `setup_token_rejected`? `endpoint_changed`? An implementer choosing differently in the fetcher and in the tests would produce inconsistent records.
* **Question the implementer must ask:** "For each reason code in §6.5, what `last_status` value is stored?"

### M15 — The trigger rule omits the kill-switch exception (C3, C15)
* **Section:** §6.4 trigger model (lines ~736–742), §6.5 kill-switched row (lines ~848–850), §6.9 (lines ~942–946)
* **Finding:** The trigger rule says a render "may start a background fetch when the cached snapshot is absent or older than the cadence floor and the provider is enabled". It never mentions the kill switch, while §6.5 says the fetcher "does not attempt fetches for that provider" when kill-switched and §6.9 says the switch "stops fetch attempts". The implementer must combine three sections to conclude the trigger has a third condition, and the same gap applies to the manual Refresh route (does a kill-switched provider's Refresh click fetch, flash, or do nothing?).
* **Question the implementer must ask:** "Does the render trigger check the kill switch, and what does a Refresh click do for a kill-switched provider?"

---

## Nits

### N1 — The §1 snapshot definition omits fields the model has (C29)
* **Section:** §1 glossary (lines ~78–82) vs §6.1 model (lines ~510–517)
* **Finding:** §1 defines a usage snapshot as "its windows and the time it was fetched". The `UsageSnapshot` dataclass also carries `provider`, `plan`, and `source_version`. A reader reconciling the two must guess whether the glossary is a simplification or the model has extra fields the glossary should name.
* **Question the implementer must ask:** "Is the §1 definition complete, or does a snapshot also carry plan and source version?"

### N2 — "not connected" casing differs between §6.7 and the rest (C27)
* **Section:** §6.7 (lines ~908–910) vs §6.2 tables, §6.5 matrix
* **Finding:** §6.7 says the card shows "not connected" (lowercase, in quotes) where the tables and matrix use "Not connected". Trivial, but the design claims one wording per state.
* **Question the implementer must ask:** "Is the card text 'Not connected' or 'not connected'?"

### N3 — The expired-token wording implies a renewal that never happens (C2, C27)
* **Section:** §6.5 taxonomy, `token_expired_refresh_failed` row
* **Finding:** The Claude wording is "Your sign-in expired and couldn't be renewed". D8 says Baton never refreshes and never renews anything — the CLI does. "Couldn't be renewed" reads as if Baton tried and failed.
* **Question the implementer must ask:** "Should this say the sign-in expired and the CLI must renew it, rather than that it 'couldn't be renewed'?"

### N4 — Codex gets a `network_error` wording for a fetch that has no network (C27)
* **Section:** §6.5 taxonomy, `network_error` row
* **Finding:** The Codex wording is "Couldn't reach OpenAI", but §6.3 and §1 both state the Codex fetch is local stdio with no network call. No Codex outcome is mapped to `network_error`, so the wording is dead text — or a real case exists that the design never names.
* **Question the implementer must ask:** "When, if ever, does the Codex adapter produce `network_error`?"

### N5 — The cache temp-file mode and the `write_secret` refusal rule are unspecified (C26, C29)
* **Section:** D5 (lines ~308–318), §6.4 (lines ~722–728)
* **Finding:** The design reuses "the approach of `write_secret`" (temp file + rename, mode 0600). The existing `write_secret` (verified in `src/jev_agent/core/secrets.py`) refuses to write when the existing file has looser-than-0600 permissions. The design never says whether that refusal applies to the cache write, what mode the temp file itself gets before the rename, or what happens on refusal.
* **Question the implementer must ask:** "If the existing cache file has loose permissions, does the write refuse like `write_secret` does, or overwrite it?"

### N6 — The probe has no script location or run command (C1, C30)
* **Section:** §9.0 (lines ~1056–1090)
* **Finding:** The probe is a "throwaway script (not a subsystem), run by the orchestrator on the Mac". No file path, language, or command is given, and the redaction rule for the recorded raw responses covers "token values" without saying whether `accountId` is recorded raw, hashed, or redacted.
* **Question the implementer must ask:** "Where does the probe script live, how is it run, and is `accountId` recorded raw or hashed in the probe records?"

---

## What was checked and found sound

* The Codex spawn sequence, environment allow-list, timeouts, cap, SIGTERM/SIGKILL grace, notification skipping, and duration-derived window mapping are specified tightly enough to implement (§6.3, §9.1).
* The config correction is complete: real paths, model, `TOP_SECTIONS`, restart requirement, and `disabled_by_config` are all present (§6.9).
* The precedence tables name the effective credential, the measured credential, and the card text per row, including bare-mode rows and the "Codex has no bare mode" statement (§6.2, D11).
* The stale-value rule (D19/D20), the 429-as-routine rule (D7), and the 401/403 re-arm shape (D7) are stated as decisions; only their boundary cases (B7, M3, M4) are missing.
* The "imported" ban, the per-provider opt-in, the two-PR split, and the probe gate are all actually present in the text, not merely claimed in the consolidated report.
