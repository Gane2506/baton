# Goldfish review — Claude Keychain amendment (resolve pass)

* **Scope:** ONLY the Claude Keychain amendment — the §9.0 probe resolve,
  §11 R5–R7, and every section they touched (D10, D16 Keychain addendum,
  §6.1 `LoginSource`/`read_keychain_item`, §6.2 Claude table Keychain rows,
  §6.3 Claude token source, §6.5 `keychain_*` rows, §6.6 grant UX,
  §9.0 re-probe, §9.2 Claude/Keychain tests, §10 Phase B gate).
* **Files read:** `design.md` (full, all ~2190 lines);
  `probe/PROBE.md`; `probe/01-codex-ratelimits.json`,
  `probe/02-claude-usage.json`, `probe/04-setup-token.json`,
  `probe/05-06-identity-splits.json`, `probe/07-versions.json`,
  `probe/claude-auth-info.json`. No other context used.
* **Verdict: NOT READY — 5 BLOCKING findings, 12 NITs.** The split itself
  (whole Keychain story in Phase B, file-only Phase A) is correctly drawn
  in most places, and the re-probe design is sound in outline. But the
  amendment has one stale test-count contradiction, one unimplementable
  contract (the `security` exit-code mapping), and — most seriously — the
  "background fetch never touches the Keychain" guarantee has no specified
  enforcement mechanism, and the stated way back for a CLI-refreshed
  Keychain token may not work at all. Those are design-level, not wording,
  and they sit on the exact path Phase B must verify live on the Mac.

## BLOCKING

### B1 — §9.1 still says "7 Claude" rows; the amended §6.2 table has 9 — the two Keychain rows belong to no phase's test list

* **Where:** §9.1, `provider_auth.py` changes ("§6.2 tables as its test
  list — one case per row: 7 Claude, 5 Codex", ~line 1673) and §9.1 test
  files ("one case per precedence-table row (7 Claude, 5 Codex, §6.2)",
  ~line 1763). Against §6.2 Claude table (~lines 777–785): 9 data rows
  (API-key ×2, saved-token ×2, file CLI-login ×2, Keychain ×2, not
  connected ×1). Codex is still 5 — only the Claude count is stale.
* **Why it blocks:** §9.2 states "the `keychain_login` detection kind
  (§6.1/§6.2) … [lands] in this PR [Phase B]" (~lines 1897–1900) and §9.3
  states "no Keychain reading in Phase A (the whole Keychain story is
  Phase B, D10)" (~line 1950). So either (a) Phase A implements the two
  Keychain rows because §9.1 says "one case per row" of the amended
  table — contradicting §9.2/§9.3; or (b) the count is stale and Phase A
  tests 7 rows — in which case the bare-mode Keychain row (§6.2, ~line
  786: Keychain + bare mode → detection + warning, usage suppressed) has
  **no test case in either phase**: §9.2's Keychain tests (~lines
  1909–1916) name only the non-bare detection case ("file absent +
  `oauthAccount` present → kind `keychain_login`").
* **Fix:** in §9.1 change both counts to "7 non-Keychain Claude rows, 5
  Codex" and state explicitly that the two Keychain rows are Phase B
  only; in §9.2 add the missing case (Keychain + bare mode →
  `keychain_login` detected, usage suppressed per D11, effective
  credential named none).

### B2 — "Background fetch never touches the Keychain" has no specified enforcement mechanism; as written, the fetch path can prompt on page open

* **Where:** D10 (~lines 441–450: "The token is read only on explicit
  user action, never on render, poll, or background fetch… A background
  fetch never touches the Keychain: it uses the cached token only, and
  if no token is cached it does not run"); §6.6 (~lines 1190–1194: "no
  Keychain read happens on render, poll, or background fetch"); §9.1
  fetcher spec (~lines 1627–1632) and refresh route (~lines 1724+); §9.1
  protocol ("`fetch()` … no arguments — adapters take what they need in
  their constructors").
* **Why it blocks:** the fetcher calls `adapter.fetch()` identically for
  a manual refresh and for a background `maybe_fetch`. Nothing in the
  design tells the adapter which call it is:
  * `maybe_fetch`'s start conditions ("enabled + kill switch on + a
    usage-measurable login is present + floor passed + not auth-stopped
    + not already running") never mention a cached-token precondition,
    and "usage-measurable login" is never defined for the Keychain case.
  * `resolve_credentials().usage_source` cannot express grant state: it
    is a pure function of env/home/bare, and §6.2 says usage is measured
    against the "Keychain token, after the grant" — before the grant,
    `usage_source` for a `keychain_login` is unspecified.
  * The refresh route ("first click sets `enabled: true` … AND starts a
    fetch immediately") names no Keychain acquisition step and never
    says whether the read runs on the request thread or the fetch thread.
  * So an implementer following §9.1 literally builds an adapter whose
    `fetch()` reads the Keychain whenever no token is cached (the only
    way `fetch()` can succeed for a `keychain_login`) — and `maybe_fetch`
    will call it from a page render. The D10/§6.6 guarantee is then
    false in the shipped code, and a system prompt appears with no user
    action. This is the exact failure the probe warned about ("attempting
    it unattended could pop a Keychain prompt on the user's Mac",
    `probe/PROBE.md` item 2).
  * Secondary gap inside the same finding: if the read runs on the
    request thread, the refresh POST hangs for up to the 120 s bound
    before redirecting; if it runs on the fetch thread, the redirect is
    immediate. The design never says which.
* **Fix:** specify the seam. E.g. (a) the manual route path performs an
  explicit token-acquisition step (read Keychain iff `keychain_login`
  and no valid cached token) *before* starting the fetch, and
  `adapter.fetch()` never calls the Keychain reader — it uses the cached
  token, or returns a non-failure "not run / awaiting grant" outcome
  (not `keychain_denied`) when none is cached; and `maybe_fetch` skips
  `keychain_login` providers with no valid cached token; or (b) add an
  explicit parameter/flag (`fetch(*, may_prompt: bool)`) or an
  `adapter.has_valid_cached_token()` gate consulted by `maybe_fetch`.
  Also state which thread the read runs on, and that a background fetch
  with no cached token records nothing (the card stays in the
  awaiting-grant state, per D10 — not `keychain_denied`, which means
  "access was not granted" and was never requested).

### B3 — Cache invalidation is self-contradictory, and the stated way back for a CLI-refreshed Keychain token probably does not work

* **Where:** D10 caching bullet (~lines 461–470); §9.2 adapter spec
  (~lines 1885–1886: "cached in process memory only (keyed to the
  `oauthAccount` uuid, cleared on restart, re-read on expiry or the
  next user action)"); §9.2 test list (~lines 1911–1913: "the fake
  reader is called once across two fetches (cache reused)"); D7
  (~lines 390–408: 401/403 re-arm; "if the CLI refreshes its own token
  (same file, same kind/path), the fetcher cannot see the change — the
  way back is the Refresh click").
* **Why it blocks — three linked holes:**
  1. **Re-read rule contradicts the cache-reuse test.** "Re-read on
     expiry or the next user action" reads as: every user action
     re-reads. Then two user-action fetches would call the reader
     twice, contradicting "called once across two fetches". If instead
     the intended reading is "re-read only when expired / uuid changed /
     restarted, otherwise reuse", then the test's "two fetches" must be
     one manual + one background fetch — unstated. The design must pick
     one semantics; an implementer cannot satisfy both sentences as
     written.
  2. **The Refresh-click way back fails on the reuse reading.** The CLI
     rotates its Keychain token (same uuid, same kind, no
     `account_hash` for Claude, so D7's re-arm cannot see it). The next
     fetch sends the stale cached token → 401 → D7 counts one 401, the
     retry (background, cached token only) 401s again → auth-stopped.
     If Refresh reuses the non-expired cached token, Refresh 401s again
     and re-stops: the user is wedged until a dashboard restart (which
     clears both the token cache and the D7 stop). The design claims
     Refresh is the way back; on the reuse reading the actual way back
     is restart. On the re-read-every-user-action reading, Refresh
     works — but then finding (1)'s test contradicts it.
  3. **Sign-out is missing from the invalidation rules.** The rules
     list expiry, uuid change, restart. If the user signs out in the
     CLI while a token is cached, `oauthAccount` disappears (or the
     uuid goes away) and no rule clears the cached token: a signed-out
     session's token lingers in process memory until restart, and if
     the user signs back into the *same* account (same uuid) the stale
     token is reused. Related: a missing/null/unparsable `expiresAt`
     is treated as non-expired (§6.3), so expiry invalidation never
     fires for such tokens — combined with CLI rotation, the cached
     token can be stale indefinitely. Also unspecified: what a
     *background* fetch does when the cached token is expired (D10 says
     re-read happens "on the next user action, never in a background
     fetch" — does the background fetch skip silently, or record
     `token_expired_refresh_failed` and tell the user to "Sign in again
     in Claude Code" when the CLI already holds a fresh token?).
* **Fix:** state one rule, e.g.: "A manual Check/Refresh click ALWAYS
  re-reads the Keychain (fresh grant check, prompt may appear);
  background fetches never read and use only a non-expired cached
  token." Then fix the §9.2 test to "one manual + one background fetch
  → reader called once", add invalidation on login disappearance
  (detection no longer finds `keychain_login` → drop the cached token),
  and specify the background-fetch-with-expired-token behaviour (skip,
  record nothing, card stays awaiting-grant/stale — not
  `token_expired_refresh_failed`).

### B4 — `read_keychain_item`'s denied-vs-unavailable distinction is unimplementable as specified: no subprocess-outcome mapping exists anywhere

* **Where:** §6.1 (~lines 733–748: `KeychainResult(outcome: "ok" |
  "denied" | "unavailable")`; docstring: `"denied" = access was not
  granted (denied, dismissed, or not answered within the bound);
  "unavailable" = the Keychain could not be reached (no `security`
  command, non-darwin platform, headless session, unexpected error)`).
  D10 (~lines 473–477) and §6.5 (~lines 1110–1111) repeat the English
  definitions but add no mapping.
* **Why it blocks:** the design never says *which subprocess outcome*
  maps to which result — no `security` exit code, no stderr text, for
  any case. An implementer cannot distinguish denied from unavailable
  from the spec: user-clicked-Deny, user-dismissed, 120 s timeout,
  item-not-found (user deleted the Keychain item while `oauthAccount`
  metadata remains), `security` binary missing, non-darwin, headless
  session, stdout that is not JSON / not an object. There is also no
  "malformed" outcome: if the item exists but its content is not the
  expected JSON shape, `KeychainResult` has nowhere to put it (the
  re-probe item 0 settles the shape on the Mac, but a runtime shape
  mismatch on another machine is unmapped). Likewise unspecified: how
  the 120 s bound is enforced (`communicate(timeout=120)`? kill on
  expiry? what happens to a lingering prompt process?), whether stdout
  is stripped before parsing, stdin/cwd for the subprocess, and what
  the §9.2 constructor's `keychain_reader: … | None = None` defaults to
  (presumably `read_keychain_item` — say so).
* **Fix:** specify the mapping table (exit code / stderr signal →
  outcome) for at least deny, dismiss, timeout, item-not-found,
  no-`security`-binary, and malformed stdout — or explicitly defer the
  mapping to re-probe item 0 (record exit codes + stderr for each of
  those cases on the Mac) and state that the mapping lands before Phase
  B merges. Item-not-found deserves its own handling: it is neither
  "access not granted" nor "Keychain unreachable"; the §6.5
  `keychain_unavailable` next step ("Sign in again in Claude Code")
  happens to be the right advice, but the reason recorded would be
  wrong.

### B5 — Phase A on the Mac shows a wrong state the design's own §1 promises never to show, and the design records the deviation only in the PR body, not on the card

* **Where:** §10 Phase A gate (3) (~lines 1991–1993): "the Phase A card
  shows file-only detection, so the Keychain login is expected to read
  as Not connected until Phase B." §6.2 not-connected row (~line 785):
  "Not connected; sign-in flows." Against §1 (~lines 60–72): "when a
  working login already exists on the device, Baton should treat it as
  the login in use and say so plainly, instead of offering a sign-in
  flow for a problem that does not exist."
* **Why it blocks (narrowly):** the split itself is honest — §10
  discloses the expected wrong state and §9.3 time-boxes it to Phase B.
  But the *user-facing* consequence is undisclosed: on the Mac (the
  only Phase A verification device, where the login exists and is
  Keychain-only per `probe/claude-auth-info.json`), the Phase A card
  will say "Not connected" and offer sign-in flows for a login that
  works. No Phase A section specifies any card copy naming the known
  limitation (e.g. "Claude logins stored in the Mac Keychain aren't
  detected yet"). The only record is gate text + PR body, which the
  user never sees.
* **Fix:** either specify one line of Phase A card copy for the
  Keychain case (a note that Keychain-stored Claude logins are not
  detected until Phase B), or record an explicit acceptance in §9.1/§10
  that Phase A knowingly ships the wrong state with no user-facing note.
  Do not leave it implicit.

## NITs

### N1 — D16's "It does not copy or store the sign-in" contradicts its own Keychain addendum (and §1's "never copies")

* **Where:** D16 (~lines 550, 558): main note "It does not copy or
  store the sign-in." vs Keychain addendum "Baton keeps the sign-in in
  memory only, never on disk." §1 (~line 66): "Baton never copies,
  stores, or owns it."
* **Fix:** qualify the main sentence — "It does not copy it to disk or
  store it." — so the consent note does not contradict the addendum in
  the same paragraph. (The addendum itself is correct and covers the
  Keychain read; this is a wording fix, not a missing disclosure.)

### N2 — The 120-second bound is stated in D10 / §6.5 / §6.1 but absent from §9.2 tests and from the route behaviour

* **Where:** stated at D10 (~line 474), §6.5 (~line 1110), §6.1
  (`timeout_s: float = 120.0`, ~line 739). Missing from §9.2 test list
  (~lines 1909–1916): no case for "prompt not answered within 120 s →
  `keychain_denied`, zero HTTP calls". Also missing from §6.6/§9.1: what
  the user sees during the wait (see B2's thread question).
* **Fix:** add the timeout→denied test case to §9.2 (fake reader that
  blocks past the bound, or a fake `security` that times out).

### N3 — `~/.claude.json` missing / malformed is not covered, and no reader for it is specified anywhere

* **Where:** D10 detection rule (~lines 433–436) and §6.2 Keychain rows
  assume `~/.claude.json` can be read for `oauthAccount` presence, but
  no function is specified to read it. `read_credential_file` (§6.1)
  is specified only for CLI credential files; reuse for
  `~/.claude.json` is never stated.
* **Fix:** name the reader (reuse `read_credential_file`, whose
  None-on-missing/invalid/non-object contract then covers
  missing/malformed `~/.claude.json` → treated as no `oauthAccount`,
  logged once), and specify presence semantics: `oauthAccount` present
  means a non-null object; null / empty object / non-object counts as
  absent. Also name the uuid field path used for cache keying
  (presumably `oauthAccount.uuid` — the probe verified the key name
  `uuid` exists, `probe/PROBE.md` item 2, but the design never writes
  the path).

### N4 — "Presence only, values never read" is false: the uuid value is read as the cache key

* **Where:** §3 Goals (~lines 159–161): "`oauthAccount` metadata …
  (presence only, values never read)"; D10 (~lines 433–436): "presence
  only". Against D10 caching (~line 462): "keyed to the `oauthAccount`
  uuid".
* **Fix:** qualify — "presence only for detection; the uuid value is
  read solely as the in-memory cache key, never displayed or logged."
  (The uuid is metadata, not a secret; the claim just needs to be true.)

### N5 — "Same login" for file-wins is an unproven assertion

* **Where:** §6.2 (~lines 794–796): "When both a credentials file and a
  Keychain login exist, the file wins (same login, same rank…)".
* **Fix:** the file-wins rule stands regardless; drop "(same login)"
  or mark it an assumption. It cannot be verified on the Mac (no file
  exists there) and the two stores could hold different accounts.

### N6 — §6.5 `no_login` Claude wording overclaims what was checked

* **Where:** §6.5 (~line 1108): "No CLI login found in
  `~/.claude/.credentials.json` or the Mac's Keychain".
* **Fix:** detection checks the metadata file `~/.claude.json`, not the
  Keychain item itself — and in Phase A it checks neither. Scope the
  wording to Phase B and to what was actually checked, e.g. "No Claude
  login found on this Mac (checked the CLI credentials file and the
  Keychain login metadata)".

### N7 — `keychain_unavailable` lists "non-macOS" as a cause, but D10 gates detection to darwin, so that cause is unreachable

* **Where:** §6.5 (~line 1111) and §6.1 docstring (~line 744–745) list
  "non-macOS" / "non-darwin platform" under `keychain_unavailable`,
  while D10 scope (~lines 479–481) says "On Linux and Windows …
  detection and token reading stay file-only … The Keychain code path
  is gated on darwin."
* **Fix:** remove "non-macOS" from the user-visible §6.5 cause list (keep
  it only as a reader-level guard in §6.1, which is legitimate
  defence-in-depth), or state explicitly that it is a dead cause in the
  UI. Credit: the darwin-gating question itself *is* answered by D10's
  scope bullet — this is only the dead-cause inconsistency.

### N8 — Re-probe procedure gaps (three small ones)

* **Where:** §9.0 re-probe paragraph (~lines 1449–1461) and §10 Phase B
  gate (0) (~lines 1998–2000).
* **(a) The re-probe needs a human and an "Always Allow".** Item 3 is 5
  attempts over an hour through the branch's reader. If the user chose
  "Allow once" on the first prompt, each attempt re-prompts (up to 5
  prompts, or 5 × 120 s waits). The design never states the precondition
  (user present; choose "Always Allow" on the first prompt so items
  2/3/5/6 can run).
  **(b) The harness for items 2/3/5/6 is unstated.** The re-probe uses
  "the Phase B branch's own reader to obtain the token" — but do items
  2/3/5/6 then run through the branch's adapter/`fetch()` path, or via
  a hand-rolled HTTP call with that token? R6's rationale ("would verify
  a different code path than the product uses") demands the product path
  for the token; the HTTP half's harness should be named the same way.
  **(c) Gate wording is ambiguous for FAIL outcomes.** "Phase B merges
  only when the re-probe passes or records failure-branch outcomes"
  (§9.0) / "passed or recorded failure-branch outcomes" (§10 gate 0).
  Item 2 failing twice is a FAIL (blocks Phase B); item 3 with 0/5 is a
  FAIL (blocks); item 0 shape-differs returns the design for resolve.
  Only items 5/6 "recorded either way" outcomes unblock. State
  explicitly that FAIL outcomes do not satisfy the gate.
* **Fix:** add the three sentences to §9.0.

### N9 — The §9.2 setup-token test has an unexecutable branch, and the adapter's setup-token detection rule is never stated

* **Where:** §9.2 (~lines 1924–1926): "Setup-token rejected (if the
  probe says rejected) → reason `setup_token_rejected`, never retried;
  if the probe says accepted → usage fetches succeed with the setup
  token." But no setup token exists to test in either probe run
  (`probe/04-setup-token.json`: `separate_setup_token_found: false`;
  re-probe item 4 is explicitly not re-probed, §9.0/R7). The "accepted"
  branch can never execute.
* **Fix:** assert rejected-by-default only; reword §10 Phase B gate (3)
  (~line 2005: "rejected (reason `setup_token_rejected` recorded)") to
  "default stands; no setup-token fetch attempted". Also state the
  adapter rule that identifies a setup token (prefix `sk-ant-oat01-`)
  — currently implied by §6.5 but never specified as an adapter check.
  Note: the Keychain token itself is not a setup token
  (`probe/04-setup-token.json`: `stored_token_is_sk_ant_oat01: false`),
  so this cannot arise on the Keychain path.

### N10 — Probe record `02-claude-usage.json` misstates its own outcome (record hygiene; design text is correct)

* **Where:** `probe/02-claude-usage.json`: `"outcome": "no Claude login
  on device (scheduling block)"`. The login exists; only the token was
  unobtainable (`probe/claude-auth-info.json`:
  `claude_json_has_oauthAccount: true`; `probe/PROBE.md` item 2
  corrects this explicitly: "This is not … a 'no login' case — the
  login exists").
* **Fix:** correct the JSON's outcome string to match PROBE.md
  ("blocked: no file-stored token; token lives in Keychain, not read by
  this probe"). No design change needed — §9.0's outcome text is
  already right.

### N11 — Re-probe item 0 should name the two candidate Keychain shapes

* **Where:** §9.0 item 0 (~lines 1455–1458): "confirm the
  `claudeAiOauth` shape the adapter parses"; §9.2 (~lines 1883–1884):
  "the file and Keychain paths parse through the same `claudeAiOauth`
  parser".
* **Fix:** name the candidates item 0 must distinguish: (a) the stored
  item is the whole credentials object (`{"claudeAiOauth": {...}}`,
  same as the file) vs (b) the stored item IS the inner `claudeAiOauth`
  object (`{"accessToken": …, "expiresAt": …}`). "Same parser" presumes
  (a); if (b) is what the Mac holds, the file parser needs a branch and
  the design returns for resolve (which item 0 already provides for —
  it just needs the distinction written down).

### N12 — "Always Allow" grant identity is unstated (security-relevant)

* **Where:** D10 (~lines 445–447) and D16 (~lines 557–559): "choose
  'Always Allow' so you are not asked again".
* **Why it matters:** the read runs via the `security` CLI subprocess,
  so the Keychain prompt's requesting application is plausibly the
  `security` tool (or the dashboard process), not "Baton" as a named
  app — and an "Always Allow" grant attached to `security` would be
  usable by any local process that can invoke it, which is broader than
  the consent note implies.
* **Fix:** have re-probe item 0 record which application the prompt
  names, and qualify the D16 note with what the grant attaches to (or
  state it as unverified until the re-probe records it).

### N13 — Deny-then-retry behaviour is assumed, never verified

* **Where:** §6.5 `keychain_denied` next step (~line 1110): "Click
  'Check usage' and choose Allow in the system prompt".
* **Fix:** the design assumes the prompt reappears on the next click
  after a Deny. If macOS instead fails fast on repeat attempts (or the
  denial sticks until the user changes the ACL in Keychain Access), the
  next step is wrong and the user is wedged. Add a deny-then-retry
  observation to the re-probe (or to Phase B Mac verification §10(2)):
  deny once, click again, confirm the prompt reappears.

### N14 — Token-source selection by effective credential is unspecified for the adapter (saved token vs Keychain)

* **Where:** §6.3 token source (~lines 949–958) covers only file vs
  Keychain for the CLI login; §9.2 adapter constructor
  (`home, transport, clock, keychain_reader`) takes no token/secrets
  input.
* **Fix:** state the selection rule: when the effective credential is
  the saved token (§6.2 precedence), the adapter uses it and never
  touches the Keychain (no grant, no prompt); the Keychain read happens
  only when the effective credential is a `keychain_login`. This is
  adjacent to the amendment (the prompt risk only exists on the
  Keychain branch) and the §6.2 table already implies it — it just
  needs one sentence in §6.3/§9.2.

### N15 — `derive_display_state` has no awaiting-grant input; the post-restart stale + no-token rendering is unspecified

* **Where:** §6.4 `derive_display_state` vocabulary (`never` /
  `collecting` / `ready` / `failed` / `stale`); §6.5 matrix Keychain
  row (~lines 1146–1152) covers only the never-granted case.
* **Fix:** specify that after a restart, a `keychain_login` with a
  stale snapshot and no cached token renders as `stale` + Refresh, and
  that Refresh click is the re-grant action (ties to the B3 fix). No
  new display state is needed — just the sentence.

## What checked out (no finding)

* **Phase split, as drawn:** D10, §9.2 (~lines 1897–1900), §9.3, §10
  honest boundaries, and §11 R5 agree the whole Keychain story
  (detection kind, reader, grant UX, caching, reason codes) lands in
  Phase B as one unit, and no Phase A section claims a Keychain piece
  — except for the B1 test-count contradiction, which is the single
  place the split leaks.
* **`keychain_login` vocabulary:** used consistently in §6.1 kind list
  (~line 682), §6.2 table rows, §9.2 tests. `KeychainResult`
  (ok/denied/unavailable) is the same contract in §6.1, D10, §6.5, §9.2
  at the type level — the gap is the subprocess mapping (B4), not the
  contract shape.
* **120 s bound:** present in D10, §6.5, and the §6.1 signature
  (`timeout_s: float = 120.0`); only the tests/route behaviour miss it
  (N2).
* **Grant → fetch / denied → `keychain_denied` / unreachable →
  `keychain_unavailable`:** each path's outcome is stated (D10, §6.5,
  §9.2 tests with zero HTTP calls on both failure paths). The hole is
  enforcement (B2), not the mapping.
* **Re-probe can unblock Phase B:** items 0/2/3/5/6 each have a stated
  pass/fail criterion in §9.0 (item 0: shape matches else resolve; item
  2: 200 + ≥1 window + `limits[]` recorded, with 404/410 →
  `endpoint_changed` and other non-200 → retry once then FAIL; item 3:
  0/5 FAIL, ≥1 pass with the rate recorded; items 5/6: recorded either
  way), and R6 correctly puts the re-probe on the Phase B branch using
  the branch's own reader.
* **Item 4 / D9 / §10 consistency:** "not re-probed, rejected default
  stands" is stated identically in §9.0, R7, D9, and §10 Phase B gate
  (3). Consistent — the only issue is the unexecutable test branch (N9).
* **Honesty on storage:** D10, D16's addendum, §6.7, and §9.2 all say
  memory-only, never on disk, never in logs, never shown; no section
  promises the token is stored, copied to disk, or displayed. The
  "never copies" wording needs the N1 qualification, but the substance
  is honest. The Keychain shape deferral (re-probe item 0) is the right
  call, not a gap.
* **Probe-record agreement:** the design's account of the probe matches
  the records — `~/.claude/.credentials.json` absent,
  `~/.claude.json` carries `oauthAccount` only
  (`probe/claude-auth-info.json`), service `Claude Code-credentials`
  per D10/PROBE.md item 2. (Only `02-claude-usage.json`'s outcome
  string disagrees, N10.)

## Summary table

| # | Severity | Section / line | Finding | Fix (one line) |
|---|---|---|---|---|
| B1 | BLOCKING | §9.1 ~1673, ~1763 vs §6.2 ~777–785 | "7 Claude" rows vs 9-row table; bare-mode Keychain row untested in either phase | "7 non-Keychain Claude rows" in §9.1; add bare-mode Keychain case to §9.2 |
| B2 | BLOCKING | D10 ~441–450; §6.6 ~1190–1194; §9.1 fetcher/route | No mechanism stops a background fetch from prompting; `fetch()` can't tell manual from background | Name the seam: manual-path token acquisition before fetch; `fetch()` never reads Keychain; `maybe_fetch` skips uncached `keychain_login`; state the thread |
| B3 | BLOCKING | D10 ~461–470; §9.2 ~1885–1886, ~1911–1913; D7 ~390–408 | "Re-read on next user action" contradicts "reader called once across two fetches"; Refresh way back fails on reuse reading; sign-out invalidation missing | Pick one rule (manual click always re-reads); fix the test to manual+background; add login-disappearance invalidation; specify expired-token background behaviour |
| B4 | BLOCKING | §6.1 ~733–748; D10 ~473–477; §6.5 ~1110–1111 | No exit-code/stderr → outcome mapping; timeout enforcement, item-not-found, malformed stdout unmapped | Specify the mapping table, or defer it to re-probe item 0 with an explicit list of cases |
| B5 | BLOCKING | §10 ~1991–1993; §6.2 ~785 | Phase A shows Not connected + sign-in flows for a working Mac login; deviation recorded only in PR body | Specify Phase A card copy naming the Keychain limitation, or record explicit acceptance |
| N1 | NIT | D16 ~550, ~558; §1 ~66 | "Does not copy or store" contradicts "keeps in memory only" | "Does not copy it to disk or store it" |
| N2 | NIT | §9.2 ~1909–1916 | No 120 s timeout→denied test | Add the test case |
| N3 | NIT | D10 ~433–436 | No reader specified for `~/.claude.json`; missing/malformed/presence semantics unstated | Name the reader; define presence (non-null object); name the uuid path |
| N4 | NIT | §3 ~159–161; D10 ~433–436 | "Values never read" false — uuid is read as cache key | Qualify: presence-only for detection; uuid read solely as cache key |
| N5 | NIT | §6.2 ~794–796 | "(same login)" unproven | Drop or mark as assumption |
| N6 | NIT | §6.5 ~1108 | `no_login` wording overclaims (Keychain never directly checked) | Scope wording to what was checked / Phase B |
| N7 | NIT | §6.5 ~1111; §6.1 ~744–745 | "non-macOS" cause unreachable (detection darwin-gated per D10) | Remove from §6.5 or mark reader-level only |
| N8 | NIT | §9.0 ~1449–1461; §10 ~1998–2000 | Re-probe needs human + Always Allow; items 2/3/5/6 harness unstated; gate wording ambiguous for FAILs | Add the three sentences |
| N9 | NIT | §9.2 ~1924–1926; §10 ~2005 | Setup-token "accepted" test branch unexecutable; prefix rule unstated | Assert rejected-by-default only; state `sk-ant-oat01-` prefix check |
| N10 | NIT | `probe/02-claude-usage.json` | Outcome string says "no Claude login" — login exists | Correct the string to match PROBE.md |
| N11 | NIT | §9.0 item 0; §9.2 ~1883–1884 | "Same parser" presumes one of two candidate Keychain shapes | Name both candidate shapes in item 0 |
| N12 | NIT | D10 ~445–447; D16 ~557–559 | "Always Allow" grant identity unstated (`security` vs Baton) | Re-probe item 0 records the prompt's named application; qualify D16 |
| N13 | NIT | §6.5 ~1110 | Deny-then-retry re-prompt assumed, never verified | Add deny-then-retry observation to re-probe / §10(2) |
| N14 | NIT | §6.3 ~949–958; §9.2 ~1870 | Adapter token selection by effective credential unstated (saved token must never prompt) | One sentence: Keychain read only when effective credential is `keychain_login` |
| N15 | NIT | §6.4; §6.5 ~1146–1152 | Post-restart stale + no-token rendering unspecified | State: `stale` + Refresh, Refresh is the re-grant |
