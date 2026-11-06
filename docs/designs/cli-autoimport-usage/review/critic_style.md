# Critic Review — Style Lens (Stage 7)

- **Document reviewed:** `docs/designs/cli-autoimport-usage/design.md`, lines 1–697
- **Standard:** `resources/document_standards/feature-spec-standard.md` (plain-English, factual, verifiable claims; a fresh reader can understand the feature without prior context)
- **Persona:** Style Goldfish — Technical Copy Editor
- **Scope:** Style only. No judgement is made here on whether the technical design is correct, except where wording hides a missing decision or asserts a fact without evidence.
- **Result:** 3 Blockers, 14 Majors, 9 Nits. The document does not meet the plain-English standard in its current form. The main defects are: a terminology promise it breaks itself, numbers that drive the refresh cadence with no source, and UI copy in §6.5–§6.6 that uses three different wordings for the same action and three different formats for the same timestamp.

## How to read this report

- **Blocker:** the wording is false, self-contradictory, or hides a decision the implementation needs. Fix before approval.
- **Major:** a fresh reader will misunderstand the feature, the UI, or the decision, or the wording cannot be verified.
- **Nit:** a smaller clarity, consistency, or redundancy defect. Fix in the same edit pass.
- Each finding gives the section and line, the offending wording quoted verbatim, why it fails the standard, and a concrete replacement where one exists. Where no replacement exists, the finding states what must be decided or cited instead.

---

## Blockers

### B1 — The document breaks its own ban on "imported" — §1, §4, §6.2, §6.9, §7, title

- **Section / lines:** Title, line 1; §1 glossary, lines 49–51; §4, lines 143–146 and 151–152; §5 D2, lines 198–199; §6.2 heading, line 397; §6.9, line 637; §7, lines 645–647.
- **Offending wording:**
  - Title: `# Auto-import Claude Code / Codex CLIs + show account usage (weekly and session)`
  - Lines 49–51: `The word "imported" is not used anywhere in the UI or this doc, because importing suggests a copy Baton holds; it does not.`
  - Line 397: `### 6.2 Import state and the precedence table`
  - Line 637: `the CLI version is recorded with the imported state`
  - Lines 143–144: `**The import half is mostly a label.**`
- **Why it fails:** The claim on lines 49–51 is false on its face. "Import" / "imported" appears in the title, in a section heading, in decision titles, and in §6.9 and §7. A reader who checks the claim against the document finds it untrue, which undermines D1, the decision the whole wording section rests on. The title also uses the banned word for the feature itself.
- **Replacement:** Delete the sentence on lines 49–51 and replace it with a statement that is true, for example: `The UI does not use "imported". This document uses "import" only when it names the rejected alternative in §7 and the live-derived state in §6.2.` Then rename §6.2 to `### 6.2 Detected-login state and the precedence table`, rename D2 to `Detected-login state is derived live…`, rename §6.9 line 637 to `recorded with the detected-login state`, and rename the title to `# Detect Claude Code / Codex CLI logins + show account usage (weekly and session)`. If the intent is a total ban, delete every other use instead — but the title and §6.2 heading must change either way.

### B2 — Numbers that set the refresh cadence have no source — §4, §5 D6, §6.3

- **Section / lines:** §4, lines 161–163; §5 D6, lines 232–236; §6.3, lines 461–462.
- **Offending wording:**
  - Lines 161–163: `Riding usage on it would fire ~480 Claude requests/hour with two tabs open against a bucket where roughly one request in three succeeds.`
  - Lines 233–234: `the windows move slowly — a few percent per 15 minutes even in heavy use`
  - Lines 461–462: `roughly one request in three succeeding under an active session`
- **Why it fails:** Three numbers drive D6 (15-minute Claude cadence, 5-minute Codex cadence) and D7 (backoff): ~480 requests/hour, "one request in three succeeds", and "a few percent per 15 minutes even in heavy use". None cites a measurement, a log, a date, or a source. "Roughly", "a few percent", and "heavy use" are not measurements. A fresh reader cannot check whether 15 minutes is the right interval or whether the 1-in-3 rate was observed once, on one account, or at all. The standard requires claims to be verifiable; a cadence justified by an uncited rate is not.
- **Replacement:** Do not keep the numbers as bare assertions. Either cite the observation (for example: `Observed 2026-11-0X on <account / environment>: N of M requests returned 429 in one hour of active Claude Code use; utilisation rose X% in 15 minutes under <defined workload>`) or replace the rationale with what is actually known, for example: `The Claude endpoint returns 429 with Retry-After; the exact success rate under load was not measured. The 15-minute interval is a starting value, to be checked at Mac verification (§10).` The same applies to the "~480 requests/hour" figure: show the arithmetic (`15 s poll × 2 tabs × 3600 s = 480/hour`) and label the success rate as unmeasured if it is.

### B3 — §6.4 does not decide between a file lock and a timestamp check — §6.4, lines 486–490

- **Section / lines:** §6.4, lines 486–490.
- **Offending wording:** `A file lock is the correct mechanism; a timestamp check is the minimum.`
- **Why it fails:** The sentence names two mechanisms, calls one "correct" and the other "the minimum", and picks neither. An implementer cannot tell whether this issue ships a file lock, a timestamp check, or both. "Correct" and "minimum" are judgements, not a decision. This is the exact defect the task names: vague phrasing that hides a missing decision.
- **Replacement:** Pick one and delete the other, for example: `Cross-instance single-flight uses a file lock on <path>. A second process that cannot take the lock serves the cached value.` If a timestamp check is the shipped mechanism, write: `Cross-instance single-flight uses a timestamp check only: a second process that sees fetch_started_at less than 60 s old serves the cached value. No file lock is used in this issue.` Do not keep both sentences.

---

## Majors

### M1 — One term per thing is not kept: "detected login" has at least six labels — §1, §2, §5 D1, §6.2

- **Section / lines:** §1, lines 47–52; §2, lines 76–80; §5 D1, lines 191–196; §6.2, lines 404–418.
- **Offending wording:**
  - `Detected on this device` (§1, D1)
  - `The claude CLI is signed in — workers reuse that login.` (§2, lines 79–80)
  - `Signed in with your Claude subscription (saved by Baton)` (D1, line 195)
  - `Subscription login (OAuth)` (§2, line 76 — the label D1 says is being split, but the old label is still quoted without saying where it remains)
  - `Detected on this device — signed in with ChatGPT` (§6.2 Codex row, line 418 — no "the `codex` CLI is signed in", unlike the Claude row)
  - `CLI login`, `login`, `sign-in`, `detected login` used interchangeably throughout §§1–6.
- **Why it fails:** The glossary defines one term, then the design uses several. The Codex card wording differs in structure from the Claude card wording for the same state, so a reader cannot tell whether the difference is deliberate. "Login" and "sign-in" are used as nouns for both the credential and the act of signing in.
- **Replacement:** Add a fixed UI-copy table in §1 or §6.2 with one exact string per state per provider, and use only those strings elsewhere. For example, use `Detected on this device — the claude CLI is signed in` and `Detected on this device — the codex CLI is signed in with ChatGPT` in both places, and use `login` only for the credential and `sign-in` only for the action. Delete the standalone quotation of `Subscription login (OAuth)` or label it `Old label, replaced by D1; do not use`.

### M2 — "Session" / "weekly" has four names each — §1, §6.3, §6.5, §6.6

- **Section / lines:** §1, lines 57–59; §6.3, lines 457–458; §6.5, lines 529–530; §6.6, lines 563–564.
- **Offending wording:** `Session window` / `5-hour session window` / `five_hour` / `Session`; `Weekly window` / `7 days` / `seven_day` / `Weekly`; also `300 minutes` and `10080 minutes` in §1 and §6.3 for the same two windows.
- **Why it fails:** The glossary defines "Session window" and "Weekly window", but the UI copy, the Codex derivation, and the Claude response fields each use a different name. A reader must map four names to one window. The Codex rule ("300 → session, 10080 → weekly") is stated in minutes while the UI states it in hours/days, with no conversion stated.
- **Replacement:** Keep `Session` and `Weekly` as the only UI labels. In §6.3 write the mapping once, in one unit: `Codex windowDurationMins 300 (= 5 hours) is the Session window; 10080 (= 7 days) is the Weekly window.` Refer to `five_hour` / `seven_day` only as `the Claude response fields five_hour and seven_day`, never as window names.

### M3 — "Remaining", "left", "allowance", and "usage" are used for the same number — Overview, §1, §3, §5 D14

- **Section / lines:** Overview, lines 16–18; §1, lines 54–56; §3, lines 109–111; §5 D14, lines 300–304.
- **Offending wording:** `how much of the account's allowance is left` (Overview); `Remaining / left — the share of a window not yet used, shown as "62% left"` (§1); `Show remaining usage for the session window and the weekly window` (§3); `Bars show "left" (remaining)` (D14).
- **Why it fails:** "Allowance", "remaining usage", "remaining", and "left" are four names for one value (100 minus used percent). "Allowance" also suggests a fixed quota, which the document never defines. The glossary entry itself gives two names for one term.
- **Replacement:** Use one term in prose and one label in the UI. For example, define in §1: `Left — the share of a window not yet used, shown as "62% left". The providers report used percent; the UI shows 100 minus that value.` Replace "allowance" in the Overview and §1 with `left`, and replace "remaining usage" in §3 with `left`.

### M4 — The refresh button has three names and the timestamp has three formats — §6.5, §6.6

- **Section / lines:** §6.5, lines 513, 532–537; §6.6, lines 568–571.
- **Offending wording:**
  - Button: `"Check usage" button` (§6.5, lines 513 and 537) vs `"Refresh" button (rate-limited)` (§6.5, line 532) vs `Refresh button: per provider, rate-limited (D6), shows its own state while a refresh is running ("Checking…")` (§6.6).
  - Timestamp: `"updated 12 min ago"` (§6.5, line 530) vs `labelled "stale — as of HH:MM"` (§6.5, line 532) vs `shown as "last seen <time>"` (§6.5, line 535).
- **Why it fails:** A reader cannot tell whether "Check usage", "Refresh", and "Checking…" are one button in three states or three different controls. The three timestamp formats are also different: relative ("12 min ago"), clock time ("HH:MM", with no timezone stated), and an undefined placeholder ("<time>"). D14 says "Never raw ISO timestamps" but does not fix which of these three formats is used where.
- **Replacement:** Fix one button label and its states, for example: `Button label: "Refresh usage". While a refresh runs it reads "Checking…". It is not shown before the first fetch; the first-fetch control reads "Check usage".` Fix one timestamp rule, for example: `All times are relative ("updated 12 min ago", "last seen 2 h ago"). Clock times are not used.` If "as of HH:MM" is kept, state the timezone (for example, `local time on the dashboard host`) and use it in every stale/failed row, not only one.

### M5 — §6.5 failure wording is provider-specific in a per-provider table, and two rows are not actionable — §6.5, lines 512–526

- **Section / lines:** §6.5, lines 512–526.
- **Offending wording:**
  - `network_error` row: UI wording `"Couldn't reach Anthropic"` — the table header says the taxonomy is "per provider", but this wording names Anthropic for a Codex failure too.
  - `rate_limited` row: UI wording `"Claude is rate-limiting usage checks right now"` — same defect for Codex.
  - `unauthorized` row: UI wording `"Claude rejected this sign-in"` — same defect.
  - `setup_token_rejected` row: UI wording `"Usage can't be read with this sign-in method"` — does not name the method, so the user cannot tell which sign-in method failed.
  - `unsupported_version` row: Next step `(upgrade prompt if known)` — "if known" leaves the condition undefined.
  - `unexpected_response` row: Next step `(none — maintainer action; logged once)` — "logged once" is undefined (once per what: per process, per day, per version?).
- **Why it fails:** The table promises one taxonomy per provider but gives Claude wording for rows that also apply to Codex. Two next steps are conditionals with no condition. A user who sees "Couldn't reach Anthropic" on a Codex card will report the wrong provider.
- **Replacement:** Give each row a Claude wording and a Codex wording where they differ, for example: `network_error — Claude: "Couldn't reach Anthropic"; Codex: "Couldn't start the Codex CLI for usage".` Replace `"Usage can't be read with this sign-in method"` with `"Usage can't be read with a setup token. Use the CLI sign-in instead."` Replace `(upgrade prompt if known)` with a decision: either `Show "Update the Codex CLI, then refresh."` or `No next step; log the version.` Replace `logged once` with `logged once per dashboard process start` or delete the qualifier.

### M6 — "Sign in again" has three wordings for one action — §6.5, lines 516–517; §6.6, lines 573–575

- **Section / lines:** §6.5, lines 516–517; §6.6, lines 573–575.
- **Offending wording:**
  - `"Sign in again in Claude Code"` (token_expired_refresh_failed next step)
  - `"Sign in again" (Claude: re-run the CLI sign-in or the dashboard flow)` (unauthorized next step)
  - `signing in again inside the CLI when refresh fails (the dashboard sign-in flow is the alternative)` (§6.6)
- **Why it fails:** The same user action — sign in again — is written three ways, and the second adds a parenthetical choice ("re-run the CLI sign-in or the dashboard flow") that the first does not offer. The user cannot tell whether the two failure rows need different actions, and the 401 row does not say which of its two options to try first.
- **Replacement:** Use one next-step string for both rows, and state the order if there is one, for example: `Next step (both rows): "Sign in again in Claude Code. If that does not work, use the dashboard sign-in flow."` If the two rows genuinely need different steps, state the difference in one sentence each instead of hiding it in a parenthetical.

### M7 — §6.3 and §6.5 contradict the §1 definition of the session window — §6.3, lines 441–444

- **Section / lines:** §1, lines 57–59; §6.3, lines 441–444.
- **Offending wording:**
  - §1: `Session window — the provider's short meter (5 hours for both providers; Codex's is 300 minutes by windowDurationMins).`
  - §6.3: `secondary may be null (the 5-hour window is suspended for many accounts), in which case the card shows one window with plain copy: "Your plan has no separate 5-hour limit — weekly only".`
- **Why it fails:** §1 states the 5-hour window exists for both providers. §6.3 states it may not exist for Codex ("suspended for many accounts") and gives UI copy that says the plan has no separate 5-hour limit. "Many accounts" is unquantified and uncited, and the two sections cannot both be true as written. A reader cannot tell whether "Session" is always present or sometimes absent.
- **Replacement:** Correct §1 to state the exception, for example: `Session window — the provider's short meter (5 hours when the provider reports one; Codex may report no secondary window).` Cite or delete "for many accounts" — for example, replace it with `secondary may be null; when it is, the card shows the Weekly window only.` Keep the UI copy, but only after §1 no longer promises a session window for every account.

### M8 — Undecided or undefined phrases hide missing decisions — §5 D6/D7, §5 D10, §6.3, §6.4

- **Section / lines:** §5 D6, lines 234–237; §5 D7, lines 246–251; §5 D10, lines 275–278; §6.3, lines 432–436; §6.4, lines 493–496.
- **Offending wording:**
  - D6: `minimum 5 minutes between attempts, or refresh-on-view` — "refresh-on-view" is not defined: which view, and does viewing reset the 5-minute minimum or bypass it?
  - D7: `401/403 is not a transient failure: retry once, then stop until auth state changes.` — "until auth state changes" names no trigger: what event restarts attempts (a new detection, a manual Refresh click, a dashboard restart)?
  - D10: `The card names the location checked ("No CLI login found in ~/.claude/.credentials.json")` — only the Claude path is given; the Codex path is not stated.
  - §6.3: `scrubbed environment (preserve HOME — the CLI needs it to find auth.json)` — "scrubbed" does not say which variables are removed and which are kept besides HOME.
  - §6.4: `a snapshot is ready while younger than its max-age (Claude 60 min, Codex 30 min)` — the max-age values (60/30) appear here with no rationale and no link to the cadence values (15/5) in D6.
- **Why it fails:** Each phrase leaves the implementer to guess a behaviour the design should fix. "Or", "until", and "if known" in these places are not style defects alone; they are undecided requirements.
- **Replacement:** Decide each one in one sentence, for example: D6 — `Codex: minimum 5 minutes between attempts; opening the Providers section does not bypass the minimum.` D7 — `After a 401/403, attempts stop until the next manual Refresh click or a change in the detected login.` D10 — add the Codex sentence: `Codex: "No CLI login found in ~/.codex/auth.json".` §6.3 — list the kept variables: `Environment: HOME only, plus <list>. All other variables are removed.` §6.4 — add the rationale or delete the separate max-age: `Max-age is four times the cadence (Claude 60 min, Codex 30 min) because …` — or set max-age equal to cadence and say so.

### M9 — Absolute "never" / "always" claims are repeated without evidence — Overview, §1, §3, §6.4, §6.5, §8

- **Section / lines:** Lines 61–62, 116–120, 495–499, 546–547, 677, 684–686 (representative; "never" appears about 30 times in lines 1–697).
- **Offending wording:**
  - Lines 61–62: `remaining is only ever server-reported.`
  - Lines 119–120: `Usage failure never breaks login detection, workers, or the rest of the Connections page.` (repeated verbatim at line 546 and in §8, line 684)
  - Lines 495–496: `a stale number is always labelled stale.`
  - Line 677: `render and live poll never block on network or subprocess`
- **Why it fails:** "Never" and "always" are testable claims, but the document cites no test for them. "Only ever server-reported" is also inaccurate as written for Codex, whose source is a local CLI process, not a server. Repeating the same "never breaks" sentence three times does not make it more true; it makes the document longer.
- **Replacement:** State each claim once, in the section that owns it, with its scope and its check, for example: `Usage failure must not change the login state shown on the card; §9 tests this by <named test>.` Replace `only ever server-reported` with `remaining comes only from the provider response (Claude endpoint or Codex app-server); it is never computed locally (D17).` Delete the duplicate sentences at lines 546 and 684 and cross-refer to §3.

### M10 — Colloquial and emotive language in a design document — §4, §5, §7

- **Section / lines:** §4, lines 149–150, 161–168; §5 D3, line 205; §5 D8, lines 263–264; §5 D9, lines 266–267; §5 D17, lines 325–326; §7, lines 653, 663–665.
- **Offending wording:**
  - `The design states that plainly instead of dressing it up as automation.` (lines 149–150)
  - `The 15-second live poll is the trap.` (line 161)
  - `Two providers, two mechanisms, one shared module is a god module.` (line 168)
  - `Usage is always-on with honest fallback, not opt-in.` (D3, line 205)
  - `destroying the login is a far worse outcome than a stale usage bar.` (D8, lines 263–264)
  - `it is the cheapest path` (D9, line 267)
  - `a computed number labelled as remaining is a lie.` / `inferring a plan from a number is a fabrication.` (D17, line 326; D13, line 299)
  - `riding it would burn the shared Claude bucket` (§7, line 653); `the CLIs' own polling would fight Baton for the same bucket` (§7, line 665)
- **Why it fails:** "Trap", "god module", "dressing it up", "burn", "fight", "lie", "fabrication", "honest", and "cheapest" are judgements or metaphors, not measurements. "Honest fallback" praises the design instead of describing it. "Cheapest" is undefined (cheapest in requests, in time, in code?). A fresh reader cannot verify any of them, and they set the tone for the rest of the document.
- **Replacement:** Replace each with a factual statement, for example:
  - `instead of dressing it up as automation` → `Usage does not change any automated Baton decision.`
  - `is the trap` → `is the risk: riding usage on the poll would send a usage request on every poll.`
  - `is a god module` → `would put both providers' parsing, transport, and error handling in one module.`
  - `with honest fallback` → `with fallback to "usage unavailable" and a reason ( §6.5).`
  - `is a far worse outcome` → `Corrupting the login removes the user's CLI sign-in; a stale usage bar does not.`
  - `is the cheapest path` → `is the path with fewest requests (one endpoint call, no refresh round-trip).`
  - `is a lie` / `is a fabrication` → `is a number the source did not report.` / `is a plan the source did not report.`
  - `burn the shared Claude bucket` / `would fight Baton` → `would send <N> requests per hour against the same rate-limit bucket the CLI uses.`

### M11 — Jargon is used without definition on first use — §2, §6.1, §6.3, §6.4

- **Section / lines:** §2, lines 89–95; §6.1, lines 376–395; §6.3, lines 432–436; §6.4, lines 483–490.
- **Offending wording:** `single-flight`, `circuit breaker`, `kill switch`, `daemon threads`, `JSON-RPC`, `app-server`, `stdio`, `PTY` (§7, line 665), `mode 600`, `atomic write`, `fingerprint`, `YAGNI lens` (§7, line 647), `god module` (see M10).
- **Why it fails:** The standard requires a fresh reader to understand the design without prior context. "Single-flight", "circuit breaker", "kill switch", and "fingerprint" are used as if defined, but the glossary in §1 defines only six terms and none of these. "Mode 600" and "atomic write" assume Unix file-permission knowledge. "YAGNI lens" assumes the reader knows the brainstorm lens names.
- **Replacement:** Add each term to the §1 glossary in one plain sentence, for example: `Single-flight — at most one refresh per provider runs at a time; a second request waits or returns the cached value.` `Kill switch — a config setting ([usage] claude = false) that turns a usage source off without a code change.` `Fingerprint — a hash of the login source path and account hint, used only to tell whether the login changed; it contains no token value.` `Mode 600 — the file can be read and written only by its owner.` Do the same for `circuit breaker`, `daemon threads`, `JSON-RPC`, and `app-server`, or replace the term with the plain description where it appears once.

### M12 — "Verified, not invented" is asserted without a citation — §6.3, lines 455–463

- **Section / lines:** §6.3, lines 455–463.
- **Offending wording:** `Verified live (HTTP 200) by independent community projects, not by Baton.` and `Rate-limits itself (429 + Retry-After; roughly one request in three succeeding under an active session).`
- **Why it fails:** The section heading promises sources that are "verified, not invented", but the Claude endpoint verification is attributed to unnamed "independent community projects" with no link, project name, date, or response sample. The reader cannot check the claim. The 1-in-3 rate (see B2) is repeated here without a source.
- **Replacement:** Name the source, for example: `Verified live (HTTP 200) by <project name, link, date observed>. Baton has not called this endpoint.` If no public source can be named, change the heading and the sentence to state the limit plainly: `Not verified by Baton. Community reports describe <…>; Mac verification (§10) must confirm the response shape before merge.`

### M13 — The state machine in §6.4 has a state the UI matrix in §6.5 omits — §6.4, §6.5

- **Section / lines:** §6.4, lines 501–503; §6.5, lines 529–544.
- **Offending wording:**
  - §6.4: `State machine per provider: never → collecting → ready → stale / failed`
  - §6.5 state matrix rows: `Detected + ready`, `Detected + stale`, `Detected + failed`, `Detected + never` — no `Detected + collecting` row.
- **Why it fails:** The matrix claims to be "the full matrix; each row is one rendered state", but it omits the `collecting` state. A reader cannot tell what the card shows while a fetch is running, except for the passing mention of "Checking…" in §6.6. The omission is a consistency defect between two sections that describe the same states.
- **Replacement:** Add the missing row to §6.5, for example: `Detected + collecting: compact line hidden; card shows "Checking…" and the last fetched value, if any, labelled with its age.` Or delete `collecting` from the §6.4 state machine if the UI never renders it — but do not leave the two sections disagreeing.

### M14 — UI copy contradicts the "no terminal" goal in wording, not just in fact — §5 D15/D16, §6.6

- **Section / lines:** §5 D15, lines 306–310; §5 D16, lines 312–316; §6.6, lines 572–575.
- **Offending wording:**
  - D15: `The zero-terminal promise narrows to "viewing usage and signing in needs no terminal".`
  - D16: `"Baton reads the sign-in your CLI already has. It never copies or stores it — to stop Baton using it, sign out in the CLI."`
  - §6.6: `signing in again inside the CLI when refresh fails (the dashboard sign-in flow is the alternative).`
- **Why it fails:** D15 quietly redefines the goal from §3 ("zero terminal use for viewing usage and signing in") to a narrower promise, but §3 is not corrected to match. D16 tells the user to "sign out in the CLI" to stop Baton using the login, while §6.2 states that removing a saved credential promotes the CLI login underneath — the consent note does not say what happens if both a saved token and a CLI login exist. "Reads the sign-in" is also the wrong verb: Baton reads a credential file, not a sign-in.
- **Replacement:** Correct §3 to the narrowed promise in the same words as D15, and use those words in §6.6. Rewrite the consent note, for example: `"Baton reads the login file your CLI already uses. It does not copy or store the login. To stop Baton using it, sign out in the CLI."` Add one sentence for the dual-credential case, for example: `If a token saved by Baton is also present, that token stays in effect until you delete it in the dashboard.`

---

## Nits

### N1 — "Mostly" quantifies nothing — §1, lines 35–38; §4, lines 143–144

- **Offending wording:** `the credential half is mostly built` (line 35); `The import half is mostly a label.` (line 143).
- **Why it fails:** "Mostly" does not say what is built and what is not. The rest of the paragraph does say it (wording, precedence, edge cases), so the word adds nothing.
- **Replacement:** Delete "mostly" in both places: `the credential half is built: detection already exists…` and `The import half is a label change.`

### N2 — "Correct for free" is a slogan, not a reason — §5 D2, lines 201–203; §7, line 647

- **Offending wording:** `live derivation is correct for free.`
- **Why it fails:** "For free" suggests no cost, but live derivation costs a detection run on every render and poll. The real reason is that it cannot go stale.
- **Replacement:** `live derivation cannot go stale when the user signs out in a terminal.`

### N3 — "As a priority" and "ships the feature off" are vague — §5 D3, lines 205–211

- **Offending wording:** `the user asked for the numbers visible as a priority; an opt-in would ship the feature off and hide exactly what was asked for.`
- **Why it fails:** "As a priority" does not name the request or its date. "Ship the feature off" is colloquial for "ship it disabled".
- **Replacement:** `the user asked for the numbers to be visible without an enable step (issue #396); an opt-in would ship the feature disabled.`

### N4 — "Human phrasing" is undefined — §5 D14, lines 300–304

- **Offending wording:** `reset times in human phrasing.` and the example `"Session 62% left · resets in 2 h 14 min · Weekly 81% left · resets in 3 d"`.
- **Why it fails:** "Human phrasing" is not a format. The example mixes units (`2 h 14 min` vs `3 d`) with no rule for when to use days, hours, or minutes, and no rule for rounding.
- **Replacement:** State the format, for example: `Reset times use the largest unit that keeps the number above 1, rounded down: "resets in 3 d", "resets in 2 h 14 min", "resets in 9 min".` Use that rule in every example.

### N5 — "Plain words" / "plain copy" / "plain-language" are used for at least five different things — §1, §5, §6.3, §6.5

- **Offending wording:** `say so plainly` (line 32); `one plain-language line` (D16, line 313); `plain copy: "Your plan has no separate 5-hour limit — weekly only"` (§6.3, line 443); `the UI renders the plain-words column` (§6.5, line 510); `the reason in plain words` (§6.4, line 495).
- **Why it fails:** "Plain" is used as praise in some places and as a label for a table column in others. It does not tell the writer what to write.
- **Replacement:** Use `plain words` only for the §6.5 UI-wording column, and delete it elsewhere: `say so` (line 32), `one line` (D16), `with this copy:` (§6.3), `the reason` (§6.4).

### N6 — Redundant repetition of the same sentence — §3, §6.5, §8

- **Offending wording:** `Usage failure never breaks login detection, workers, or the rest of the Connections page.` (§3, lines 119–120; §6.5, line 546; §8, lines 684–685, in near-identical words).
- **Why it fails:** The same guarantee is stated three times. A reader who finds a difference between the three versions will not know which one governs.
- **Replacement:** State it once, in §3 (the goals section), and cross-refer from §6.5 and §8: `See §3: usage failure does not change login detection, workers, or the rest of the page.`

### N7 — "Every state" overstates what the matrix covers — Overview, lines 18–20

- **Offending wording:** `Every state — detected login, CLI missing, not logged in, usage unavailable, usage stale — is visible in the dashboard with an actionable next step`
- **Why it fails:** §6.5 gives several states with next step "(none)" (`not_applicable_api_key`, `unexpected_response`, and the `unsupported_version` conditional). "Every state … with an actionable next step" is therefore false as written.
- **Replacement:** `Each state is visible in the dashboard; states with a user action name the next step, and states with none say so.` Or add a next step to the "(none)" rows.

### N8 — Inconsistent capitalisation and punctuation in quoted UI copy — §5 D1, §6.2, §6.5

- **Offending wording:** `Detected on this device — the claude CLI is signed in` (D1) vs `detected on this device` (§1 glossary, lower case) vs `Detected on this device, from ~/.claude/.credentials.json, at 14:32` (§6.7, line 593, with a comma instead of an em dash); `"Billed per token — no subscription windows apply to an API key."` (§6.5, period inside the quotation) vs `"Usage not checked yet"` (no period).
- **Why it fails:** Quoted UI copy is a specification. Inconsistent capitalisation, punctuation, and separators mean the implementer must guess which version is the string to render.
- **Replacement:** Fix one style for quoted UI strings (for example: sentence case, no trailing period inside the quotation unless the string is a full sentence shown as such) and apply it to every quoted string in §§5–6. Use the same separator (em dash) in every "Detected on this device" string.

### N9 — "Not connected" vs "Not logged in" for the same state — §6.2, §6.5

- **Offending wording:** §6.2 table: `Not connected; sign-in flows` (lines 409, 419); §6.5 matrix: `Not logged in: no usage section; existing sign-in flows.` (line 542).
- **Why it fails:** Two labels for one state (no credential of any kind). A reader cannot tell whether "not connected" and "not logged in" are different states.
- **Replacement:** Use one label everywhere. For example, use `Not connected` in both places, and delete `Not logged in` from §6.5 — or the reverse, but only one.

---

## What is not a finding

- The document is, in most places, specific about numbers, paths, timeouts, and file modes (D5–D7, §6.3–§6.4). That specificity is what the standard asks for; the findings above are about the places where it lapses, not a general lack of detail.
- The §1 glossary is the right structure. It needs more entries (see M11) and consistent use (see M1–M3), not removal.
- No finding is made on lines 698–815. They were outside the assigned range (lines 1–697) and were not reviewed.

## Checklist for the resolve pass

1. Fix B1 first: either delete the "imported is not used anywhere" claim or delete every use of "import(ed)"; rename the title and §6.2 either way.
2. Cite or remove the three uncited numbers in B2, and state the 15-minute cadence as a starting value if it is unmeasured.
3. Decide the file-lock question in B3 in one sentence.
4. Add a fixed UI-copy table (M1, M4–M6) so §6.5 and §6.6 quote the same strings.
5. Add the missing glossary entries (M11) and use the defined terms only (M1–M3).
6. Apply one timestamp format and one button label (M4), and correct §1 for the Codex secondary-window exception (M7).
