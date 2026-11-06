# Stage 9 — Implementation Readiness Test, style lens

Scope reviewed: `design.md` §9 (lines 698–779) and §10 (lines 781–807),
checked against the terminology and decisions in lines 1–697 and against
the implementation-readiness standard (zero-decision rule: a fresh
implementer must be able to execute the plan on the first attempt without
guessing or making design choices).

Verdict: not ready as written. §9 reads as a list of nouns where the
standard requires values, and several test and verification items state
no pass condition. Two findings block: one merge gate can be unpassable,
and one design choice is left to the implementer. The rest are Majors
that force the implementer to hunt through §6 for numbers §9 should
carry, plus Nits.

Note: the words "as needed" and "appropriately" do not appear in §9–§10.
The vagueness takes other forms: noun lists without values, unnamed
reason codes, and verification items with no stated outcome.

## Blockers

### B1 — §10 Mac gate (2) can be unpassable
- Section/line: §10, lines 795–802.
- Offending wording: "(2) Codex detected + usage shows both windows …
  No merge before (1) and (2) pass live."
- Problem: §6.3 states `secondary` may be null — "the 5-hour window is
  suspended for many accounts" — and in that case the card shows one
  window. If the Mac account has no secondary window, gate (2) as
  written can never pass, and no fallback is stated.
- Replacement: "(2) Codex detected + usage shows the windows the
  response carries: both windows when `secondary` is present; one
  window plus the exact copy 'Your plan has no separate 5-hour limit —
  weekly only' when `secondary` is null. Either outcome passes the
  gate; record which one was observed."

### B2 — Cross-instance mechanism undecided; implementer must choose
- Section/line: §9 test list, lines 762–765 ("cross-instance
  in-progress marker"); §6.4 (lines ~498–505).
- Offending wording: "cross-instance in-progress marker."
- Problem: §6.4 says "A file lock is the correct mechanism; a timestamp
  check is the minimum." §9 does not pick one. The implementer must
  make a design choice the standard forbids leaving open, and the test
  cannot state a pass condition for a mechanism that is not named.
- Replacement: decide in the design, then write the test to match, e.g.
  "cross-instance: a second dashboard process that sees a
  `fetch_started_at` less than 60 s old serves the cached value and
  starts no fetch (timestamp check; no file lock in this issue)."
  If the file lock is the pick instead, name the lock file path and the
  behaviour when the lock is held.

## Majors

### M1 — Terminology rule broken: "import metadata"
- Section/line: §9 modified files, line 731.
- Offending wording: "`cli_providers` gains the cached usage snapshot
  (cache read only, never a fetch) and import metadata (source,
  detected_at)."
- Problem: §1 (line 49) states "The word 'imported' is not used anywhere
  in the UI or this doc, because importing suggests a copy Baton holds."
  §9 uses "import metadata". §6.9 (line 637) repeats the break with
  "recorded with the imported state". A reader cannot tell whether
  "import" here means the banned copy-semantics or just the detection
  record.
- Replacement: "and detection metadata (`source`, `detected_at`)."
  Also fix line 637 to "recorded with the detected-login state".

### M2 — Claude adapter bullet is nouns, not steps
- Section/line: §9 new files, lines 705–707.
- Offending wording: "Claude adapter: endpoint call, response parser
  (versioned), token refresh (in-memory only), setup-token one-shot
  logic (D9), `httpx` transport injected."
- Problem: no endpoint URL, no timeout value, no refresh trigger are
  stated here; "versioned" is undefined (version of what — adapter,
  response schema? §6.1 says "behind a versioned parser" and never
  defines it either). "One-shot logic" does not restate D9's rule
  ("exactly once per token"). The implementer must reassemble the
  adapter from §6.3 and D8–D9.
- Replacement: "Claude adapter: `GET
  https://api.anthropic.com/api/oauth/usage` with Bearer access token
  and header `anthropic-beta: oauth-2025-04-20`, 10 s timeout; parser
  for `five_hour` / `seven_day` (`utilization`, `resets_at`), ignoring
  `seven_day_opus` / `seven_day_sonnet` and `limits[]`; refresh the
  access token in memory only when expired or expiring within 5 minutes
  via `POST https://console.anthropic.com/v1/oauth/token`; try an
  `sk-ant-oat01-` setup token against the endpoint exactly once per
  token (D9); `httpx` transport injected." Define "versioned" or
  delete the word.

### M3 — Codex adapter bullet omits every value
- Section/line: §9 new files, lines 708–711.
- Offending wording: "app-server spawn contract (cwd, env scrub,
  timeout, output cap, SIGTERM→SIGKILL), JSON-RPC handshake +
  `account/rateLimits/read`, response parser with window derivation
  from `windowDurationMins`, spawner injected."
- Problem: the parenthetical names five parameters and gives zero
  values. §6.3 holds them (temp-dir cwd, preserve `HOME`, 15 s
  timeout, 4 MiB cap, SIGTERM then SIGKILL after 5 s) but §9 is the
  execution checklist and does not carry them. "Handshake" does not
  say which messages (`initialize` id 1, then
  `account/rateLimits/read` id 2) or that notifications are skipped.
- Replacement: repeat the §6.3 values in the bullet, e.g. "spawn `codex
  app-server` with temp-dir cwd, scrubbed environment preserving
  `HOME`, 15 s timeout, 4 MiB output cap, SIGTERM on completion then
  SIGKILL after a 5 s grace; send `initialize` (id 1), then
  `account/rateLimits/read` (id 2); skip notifications; derive windows
  from `windowDurationMins` (300 → session, 10080 → weekly)."

### M4 — Cache bullet omits the record shape
- Section/line: §9 new files, lines 712–714.
- Offending wording: "host-level cache record
  (`~/.jev/usage-cache.json`, mode 600, atomic write), fingerprint
  invalidation, single-flight state, staleness rules."
- Problem: the record fields are a design decision (D5 lists
  `fingerprint`, `fetched_at`, `next_retry_at`,
  `consecutive_failures`, `last_status`, `last_reason`,
  `source_version`, `windows`) and §9 does not list them. "Staleness
  rules" hides the numbers (§6.4: ready while younger than max-age —
  Claude 60 min, Codex 30 min — then stale). "Single-flight" is
  jargon with no definition in §9.
- Replacement: "host-level cache record
  (`~/.jev/usage-cache.json`, mode 600, atomic write via temp file +
  rename), one record per provider with fields `fingerprint`,
  `fetched_at`, `next_retry_at`, `consecutive_failures`,
  `last_status`, `last_reason`, `source_version`, `windows`;
  fingerprint change invalidates the record; one refresh per provider
  at a time (a second `refresh()` call returns False while one runs);
  snapshot is `ready` while younger than 60 min (Claude) / 30 min
  (Codex), `stale` after that."

### M5 — Refresher bullet is a noun list with no numbers
- Section/line: §9 new files, lines 715–717.
- Offending wording: "background scheduler: cadence, idle rule,
  backoff, circuit breaker, manual refresh, `shutdown()`."
- Problem: six nouns, zero numbers. The numbers exist (D6/D7, §6.4)
  but §9, the file the implementer works from, does not carry them.
  `shutdown()` does not state its contract (§6.4: joins threads and
  terminates any live app-server).
- Replacement: "background scheduler: Claude minimum 15 min between
  attempts, Codex minimum 5 min; no background refresh while the usage
  UI has not been viewed in 30 min; backoff 15 → 30 → 60 min capped at
  60; after 3 consecutive failures, 1 h quiet then one probe; manual
  refresh ignored when the last attempt was < 5 min ago (Claude) or
  < 60 s ago (Codex); `shutdown()` joins threads and terminates any
  live app-server."

### M6 — Claude test cases omit the expected reason codes
- Section/line: §9 test list, lines 752–757.
- Offending wording: "429 with `Retry-After` (honoured, backoff), 401
  (retry once, then stop), setup-token rejected once (recorded, never
  retried), expired token (refresh succeeds in memory; refresh fails →
  reason code), … network error, timeout, API-key auth (no fetch
  attempted)."
- Problem: every case except the changed-shape case (which correctly
  names `unexpected_response`) fails to state what "pass" looks like:
  - 429: "honoured" does not say the pass condition (next attempt no
    earlier than `Retry-After` seconds; result reason
    `rate_limited`). Backoff numbers not restated.
  - 401: "then stop" drops D7's qualifier "until auth state changes",
    names no reason code (`unauthorized`), and does not say what
    "stop" means observably (no further attempts recorded). 403 is
    covered by D7/§6.5 but absent from the test list entirely.
  - Setup-token: "recorded" does not say recorded as what (reason
    `setup_token_rejected`) or where.
  - Expired token: "→ reason code" does not name the code
    (`token_expired_refresh_failed`); "succeeds in memory" does not
    state the observable pass (refreshed token used for the fetch,
    nothing written to `~/.claude/.credentials.json`).
  - Network error / timeout: no reason codes named (`network_error`,
    `timeout`), no timeout value (10 s).
  - API-key auth: "no fetch attempted" is observable only via the
    fake transport receiving zero calls — not stated — and the UI
    outcome (usage section hidden, D12) is not mentioned.
- Replacement: name the reason code and the observable outcome for
  each case, e.g. "401 → reason `unauthorized`, exactly one retry,
  then no further attempts until auth state changes; 403 → same;
  setup-token rejected → reason `setup_token_rejected` recorded in the
  cache record and never retried for that token; refresh fails →
  reason `token_expired_refresh_failed`; network error → reason
  `network_error`; fetch over 10 s → reason `timeout`; API-key auth →
  fake transport receives zero calls and no usage section renders."

### M7 — Codex test cases omit values and reason codes
- Section/line: §9 test list, lines 758–761.
- Offending wording: "happy path (primary + secondary), `secondary`
  null (one window + plain copy), auth-required failure, handshake
  failure, spawn timeout (SIGTERM→SIGKILL), output cap, missing CLI,
  wrong version."
- Problem:
  - "`secondary` null (one window + plain copy)" does not quote the
    copy §6.3 fixes ("Your plan has no separate 5-hour limit — weekly
    only"), although the Dashboard test two lines below demands "the
    exact copy" — the standard is applied inconsistently.
  - "auth-required failure": no reason code named (§6.5 has no
    Codex-auth code; `unauthorized` reads as Claude-only and
    `app_server_failed` is the spawn/handshake bucket — the mapping
    is undecided).
  - "handshake failure": does not say which step (`initialize` vs
    `account/rateLimits/read`) or the expected reason.
  - "spawn timeout": no timeout value (15 s), no grace value (5 s),
    no expected reason code.
  - "output cap": no cap value (4 MiB), no expected behaviour when
    exceeded (truncate? fail? which reason?).
  - "wrong version": no version range named. §6.9 promises "a
    known-good range is recorded in the design's verification
    section" but §10 records only one Codex version
    (0.164.0-alpha.9) and no range, so the test has no pass
    condition. Expected reason `unsupported_version` is not named.
- Replacement: restate the §6.3 values and name one reason code per
  case, e.g. "spawn timeout: no response within 15 s → SIGTERM, then
  SIGKILL after 5 s grace, reason `app_server_failed`; output over
  4 MiB → [state the behaviour]; version outside [state the range] →
  reason `unsupported_version`; `secondary` null → one window plus
  the exact copy 'Your plan has no separate 5-hour limit — weekly
  only'."

### M8 — Cache tests: "atomic write" and "error" have no pass condition
- Section/line: §9 test list, lines 762–765.
- Offending wording: "atomic write, mode 600, fingerprint invalidation
  on auth change, error never overwrites good cache, restart preserves
  `next_retry_at` / `consecutive_failures`, cross-instance in-progress
  marker."
- Problem: "atomic write" does not state the observable pass (write
  temp file + rename; a crash mid-write leaves the previous good file
  intact). "Error" is unqualified — which errors (fetch failure?
  write failure? corrupt cache file?). "Fingerprint invalidation on
  auth change" does not say which change triggers it (source path
  change? account-hint change? per D5 the fingerprint is "source path
  + account hint"). "Cross-instance in-progress marker" is the B2
  undecided mechanism restated as a test.
- Replacement: "atomic write: interrupted write leaves the previous
  file intact and readable; mode 600 on the written file; fingerprint
  invalidation: changing the source path or account hint discards the
  cached windows; a failed fetch never overwrites a good cache entry;
  restart preserves `next_retry_at` and `consecutive_failures`
  verbatim."

### M9 — Refresher and dashboard tests omit the rate-limit numbers and status
- Section/line: §9 test list, lines 766–773.
- Offending wording: "manual refresh rate limits" (line 768–769);
  "refresh route rate limit" (line 771); "render serves cached usage
  only (no fetch on render)" (line 770).
- Problem:
  - "Manual refresh rate limits" does not repeat D6's numbers
    (ignored when the last attempt was < 5 min ago for Claude,
    < 60 s for Codex) or the observable outcome (ignored call returns
    what — cached value? nothing?).
  - "Refresh route rate limit" does not state the HTTP outcome when
    limited (status code, body) — an implementer cannot write the
    test's assertion.
  - "No fetch on render" does not state how it is observed (fake
    transport receives zero calls during render).
  - The live-poll test ("live poll JSON gains additive `usage` key")
    does not name the key's contents (§6.6: `fetched_at` + windows).
- Replacement: "manual refresh ignored when the last attempt was
  < 5 min ago (Claude) / < 60 s ago (Codex), returning the cached
  value; refresh route returns [status code] with [body] when
  rate-limited; render triggers zero calls on the fake transport;
  live poll JSON gains an additive `usage` key carrying `fetched_at`
  and `windows` per provider."

### M10 — Test list omits the kill-switch and first-run states
- Section/line: §9 test list, lines 750–773, against §6.5 and §6.9.
- Offending wording: (omission) no test names `disabled_by_config`,
  `not_checked_yet`, `no_login`, or `not_applicable_api_key`.
- Problem: §6.5 defines 12 reason codes and §6.9 adds a 13th,
  `disabled_by_config`, which never appears in the §6.5 table. The
  test list covers roughly half the taxonomy. The kill switch is the
  rollback mechanism (§6.9: "Rollback is the kill switch plus a normal
  revert") and has no test proving it renders `UsageUnavailable`
  with reason `disabled_by_config` while leaving login state
  unaffected. The first-run state (`not_checked_yet`, "Check usage"
  button) is likewise untested.
- Replacement: add "kill switch off (`[usage] claude = false`) →
  `UsageUnavailable` with reason `disabled_by_config`, login state
  unchanged; first run → reason `not_checked_yet` with a 'Check
  usage' button; API-key auth → reason `not_applicable_api_key`,
  usage section hidden." Also add `disabled_by_config` to the §6.5
  table so the taxonomy has one home.

### M11 — File changes with no test: refresher lifecycle and route wiring
- Section/line: §9 modified files, lines 733–737; test list,
  lines 750–773.
- Offending wording: "refresher lifecycle (start on dashboard start,
  stop on shutdown)."
- Problem: the lifecycle change has no corresponding test. The
  refresher tests cover `shutdown()` joining threads but not that the
  dashboard starts the refresher on start and stops it on shutdown.
  "Start on dashboard start, stop on shutdown" also names no hook or
  function, so the implementer must decide where the calls go.
- Replacement: name the hook ("refresher starts in [function/module]
  on dashboard start and `shutdown()` is called from [function/module]
  on dashboard shutdown") and add a test: "dashboard start creates
  the refresher; dashboard shutdown calls `shutdown()` and joins its
  threads."

### M12 — Mac verification item (1): "real numbers" is not a pass condition
- Section/line: §10, lines 795–796.
- Offending wording: "(1) Claude detected + usage shows both windows
  with real numbers and reset times".
- Problem: "real numbers" does not say what counts as real. Any
  non-zero percent? Numbers matching the CLI's own `/usage` output?
  A fabricated zero is already forbidden by §6.1, so the check needs
  a comparison method, not an adjective.
- Replacement: "(1) Claude detected + usage shows both windows with
  percentages and reset times that match the numbers shown by
  `/usage` in Claude Code at the same time (within the same fetch —
  state the tolerance or state 'same values')."

### M13 — Mac verification items (3)–(6) state no outcome or artifact
- Section/line: §10, lines 797–801.
- Offending wording: "(3) the setup-token acceptance question settled
  live; (4) a 429 path observed if it occurs naturally (not forced);
  (5) the Keychain case noted if the Mac's Claude credentials live in
  the Keychain; (6) CLI versions recorded (Codex known-good
  0.164.0-alpha.9 from the VM)."
- Problem:
  - (3) "settled" names no outcome: accepted (usage works with the
    setup token) or rejected (reason `setup_token_rejected`
    recorded)? Either could be called "settled".
  - (4) is conditional with no defined outcome when the condition
    never occurs: is a run with no 429 a pass, or an untested item?
  - (5) "noted" names no artifact: noted where — PR body, design
    doc, issue comment?
  - (6) "recorded" names no artifact either, and records only the
    Codex version although §6.9 requires the Claude CLI version to
    be recorded at Mac verification too.
- Replacement: "(3) setup token accepted (usage fetches succeed) or
  rejected (reason `setup_token_rejected` recorded) — record which;
  (4) if no 429 occurs naturally, record '429 path not observed live;
  unit-proven only' — the item is untested, not passed; (5) if the
  Mac's Claude credentials live in the Keychain, record that in the
  PR body; (6) record both CLI versions in the PR body (Codex
  0.164.0-alpha.9 known-good from the VM; Claude version observed on
  the Mac)."

### M14 — Orchestrator verification has no pass condition; "self-score" has no scale
- Section/line: §10, lines 789–792.
- Offending wording: "independent review of the PR against this design
  (precedence table, reason codes, cadence numbers, adapter seam, no
  token leakage in logs/cache/UI), plus a self-score from the
  implementer."
- Problem: the parenthetical is a topic list, not a checklist with
  verdicts — it does not say what the reviewer must confirm for each
  item, or what happens on a mismatch. "Self-score" states no scale
  and no minimum (the Baton push protocol's ≥85 threshold is not
  stated here, so a fresh reader cannot know it). "Adapter seam" is
  jargon for the protocol boundary and is undefined in the doc.
- Replacement: "independent review of the PR against this design, one
  verdict per item: precedence table matches §6.2 row for row;
  every reason code in §6.5 maps to its UI wording; cadence numbers
  match D6/D7; no provider-specific string, path, URL, or response
  key appears outside its adapter (§6.1); no token value appears in
  logs, cache, or UI; plus an implementer self-score out of 100, merge
  only at 85 or above."

## Nits

### N1 — "all with fakes (section 8 testability)"
- Section/line: §9 new files, lines 718–720.
- Offending wording: "`tests/unit/test_usage_claude.py`,
  `test_usage_codex.py`, `test_usage_cache.py`,
  `test_usage_refresh.py` — all with fakes (section 8 testability)."
- Problem: cross-reference instead of content; does not say which fake
  each file uses (§8 names four: HTTP client, process spawner, clock,
  cache).
- Replacement: "— `test_usage_claude.py` with a fake HTTP transport,
  `test_usage_codex.py` with a fake process spawner,
  `test_usage_cache.py` with a fake clock and temp directory,
  `test_usage_refresh.py` with fake clock and fake adapters."

### N2 — "distinct labels" without the labels
- Section/line: §9 modified files, lines 724–727.
- Offending wording: "distinct labels for saved OAuth token vs CLI
  login (D1); shared credential-source accessor (`get_login_source`)
  used by detection and adapters".
- Problem: D1 fixes the exact labels ("Signed in with your Claude
  subscription (saved by Baton)" / "Detected on this device") and §6.1
  fixes the accessor's return shape (`{kind, path-or-store,
  detected_at, cli_version}`); §9 quotes neither, so the implementer
  must cross-reference to write the change.
- Replacement: quote both labels and the return shape in the bullet.

### N3 — Template bullet: "bare-mode qualification" is vague
- Section/line: §9 modified files, lines 740–742.
- Offending wording: "usage block per provider card (progressive
  disclosure, bars, 'updated … ago', consent note, API-key
  explanation, bare-mode qualification)."
- Problem: "bare-mode qualification" does not state the wording (D11:
  the login does not apply to bare-mode workers, no usage bars for
  that source) and "progressive disclosure" is jargon unexplained in
  §9 (§6.6 defines it as one compact line plus a `<details>`
  disclosure). "Consent note" and "API-key explanation" likewise
  quote no copy, though D16/D12 fix both.
- Replacement: "usage block per provider card: one compact line plus
  a `<details>` disclosure; per-window bars labelled 'Session' and
  'Weekly'; 'updated … ago'; consent note (D16); API-key line 'Billed
  per token — no subscription windows apply to an API key.' (D12);
  bare-mode line stating the login does not apply to bare-mode
  workers, with no usage bars (D11)."

### N4 — Precedence-table test does not count the rows
- Section/line: §9 modified files, lines 743–744.
- Offending wording: "`tests/unit/test_provider_auth.py` — new labels,
  precedence table cases, accessor behaviour."
- Problem: §6.2 has 5 Claude rows and 4 Codex rows; "precedence table
  cases" does not say all 9 rows are tested, so a partial table
  passes the wording.
- Replacement: "— new labels (D1 wording verbatim), one case per
  precedence-table row (5 Claude, 4 Codex, §6.2), accessor behaviour
  (`get_login_source` return shape)."

### N5 — Screenshots: "at least its shots", "revert the rest"
- Section/line: §10, lines 786–788.
- Offending wording: "regenerate `docs/images/` if any dashboard page
  changes (the Connections page changes, so at least its shots),
  commit only the affected images, revert the rest."
- Problem: "at least its shots" names no files, and the "if" is dead
  wording — the condition is already known true (the Connections page
  changes). "Revert the rest" does not say revert to what (the
  pre-regeneration state).
- Replacement: "regenerate `docs/images/` (the Connections page
  changes, so its shots at minimum — name the files); commit only
  the images that changed; revert any regenerated image that did not
  change to its pre-regeneration state."

### N6 — "cadence floors" is unexplained jargon
- Section/line: §9 test list, line 766.
- Offending wording: "cadence floors (Claude 15 min, Codex 5 min)".
- Problem: "floors" means minimum intervals here, but the word is
  never defined; D6/§6.4 say "minimum 15 minutes between attempts".
  Use the doc's own wording.
- Replacement: "minimum intervals between attempts (Claude 15 min,
  Codex 5 min)."

### N7 — Inconsistent names for the same thing
- Section/line: §9, lines 730–731, 734, 741.
- Offending wording: "cached usage snapshot" (line 730), "cached
  usage" (line 734), "usage block" (line 741).
- Problem: three phrases for one object (§1 defines "usage snapshot").
  Minor, but the doc is otherwise careful about naming (see §1's
  glossary) and §9 drifts from it.
- Replacement: use "cached usage snapshot" in all three places.

### N8 — Config bullet omits the user-facing file
- Section/line: §9 modified files, lines 728–729.
- Offending wording: "`src/jev_agent/core/config/schema.py` +
  `defaults.py` — `[usage]` section: `claude` / `codex` booleans,
  default true (kill switches)."
- Problem: §6.9 places the switches in `jev.toml`; §9 names only the
  code files, so the implementer must infer where the user sets them.
- Replacement: append "(surfaced as `[usage] claude` / `[usage] codex`
  in `jev.toml`, defaults true; documented in `docs/config.md`)."

### N9 — Docs bullets state topics, not content
- Section/line: §9 modified files, lines 745–747.
- Offending wording: "`docs/dashboard.md` — Providers section:
  detected logins, usage windows, states, kill switches."
- Problem: a topic list, not a content spec; it does not say the docs
  must carry the exact UI copy, the reason-code table, or the
  cadence numbers.
- Replacement: "`docs/dashboard.md` — Providers section: detected
  logins, usage windows, every state in §6.5 with its UI wording and
  next step, kill switches."

## What passes (for the record)

- The changed-shape test (line 755–756) is the model for the test
  list: it names the trigger (missing `five_hour`) and the expected
  reason code (`unexpected_response`).
- The refresher tests (lines 766–769) mostly carry their numbers
  (15→30→60 backoff, 3 failures → 1 h quiet → one probe, 30 min
  idle rule).
- The dashboard test (lines 772–773) demands "the exact copy" for
  every state-matrix row.
- §10 states the merge gate plainly: "No merge before (1) and (2)
  pass live" (line 801–802).
- §10's honest-boundaries bullet (lines 803–807) states plainly what
  is unit-proven versus live-observed.
