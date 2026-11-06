# Goldfish review — Migration & backward compatibility lens
## Issue #396: Auto-import Claude Code / Codex CLI logins + show account usage

Reviewer lens: rollout safety, version skew, backward compatibility.
Sources read: `concepts_brief.md` (2026-11-06), `src/jev_agent/core/provider_auth.py`,
`src/jev_agent/dashboard/routes_connections.py`, `views.cli_providers`
(`src/jev_agent/dashboard/views.py:1616`), `templates/connections.html` Providers
section (lines 204–322), `tests/unit/test_provider_auth.py`.
Read-only review; nothing modified.

## Verdict

The brief is honest about the two riskiest facts — the Claude endpoint is
undocumented and the Codex protocol is hidden and versioned — but it treats
both as sources to call, not as dependencies that will break. There is no
rollback story, no version gate, no cache-invalidation story, and no answer to
the precedence collision with tokens users already saved through the
dashboard. As written, the first provider-side change, the first CLI upgrade,
or the first login expiry lands directly on the Connections page with no
bulkhead between the new usage feature and the auth state users already rely
on. The brief lists six open decisions; four of them are migration decisions
(caching, recorded-vs-derived import, endpoint posture, setup-token handling)
and none of them can be left open into implementation without a default that
fails safe.

---

## 🔴 Blocker 1 — A saved dashboard token and a CLI login collide, and the brief does not say who wins

`provider_auth.py` precedence is explicit and the brief quotes it correctly:
for Claude, `ANTHROPIC_API_KEY` outranks `CLAUDE_CODE_OAUTH_TOKEN`, which
outranks the `~/.claude/.credentials.json` CLI login (`claude_code_auth`,
env-first checks). For Codex, `OPENAI_API_KEY` outranks `~/.codex/auth.json`.

That means the largest existing-user cohort — anyone who already completed
the dashboard's own `claude setup-token` flow and has `CLAUDE_CODE_OAUTH_TOKEN`
in `~/.jev/secrets.env` — will **never** be detected as `cli_login`. Their
auth kind is `oauth_token`, and the template's Claude branch renders the
`oauth_token` path ("Signed in with your Claude subscription"), not the
CLI-login path. So "auto-import" has three possible behaviours for these
users and the brief picks none of them:

1. **Shadowed:** the CLI login exists but is invisible because the saved
   token outranks it. The user sees no "imported from this device" state,
   even though the device login exists. The feature silently does nothing
   for the people most likely to have both.
2. **Override:** import replaces or ignores the saved token. That deletes a
   working credential the user deliberately saved (a one-year token) in
   favour of a short-lived CLI access token that needs refresh handling.
   That is data loss of a credential, not an upgrade.
3. **Coexist:** both are shown. Then the page must say which one workers
   actually use (the saved token, by precedence) and which one usage is
   measured against — and if they belong to *different accounts* (personal
   CLI login vs. a work setup-token), usage shown under the wrong identity
   is worse than no usage at all.

The same collision exists in reverse on removal: if a user deletes the saved
token from the secrets file, the CLI login surfaces on the next poll and the
page flips from `oauth_token` to `cli_login` with no explanation. And for
Codex, `OPENAI_API_KEY` in the environment shadows a ChatGPT subscription
login the same way — an API-key user gets no usage windows at all, which is
correct, but the page must say *why* there are no bars rather than showing
empty ones.

**Required in the design doc:** a precedence table for every combination of
(saved token, CLI login, API key) × provider, stating for each: which auth
workers use, which auth usage is measured against, what the card shows, and
whether import is recorded at all when a higher-precedence credential already
exists. The default must be "never silently replace or hide a credential the
user saved."

## 🔴 Blocker 2 — There is no rollback path when a provider changes its response

The brief documents the failure modes (Claude 429 with `Retry-After`,
"roughly one request in three succeeds under an active session"; Codex
protocol verified only on 0.164.0-alpha.9) and then stops. It never says what
happens on the *next* failure, which is the whole migration question:

- **Claude shape change.** The response is parsed for `five_hour` /
  `seven_day` objects with `{utilization, resets_at}`, plus
  `seven_day_opus` / `seven_day_sonnet` and a `limits[]` array. There is no
  schema version, no contract test, and no field-level tolerance rule. If
  Anthropic renames `utilization`, nests the windows one level deeper, or
  returns `resets_at` in a new format, a strict parser throws and a lenient
  parser silently shows a wrong number. The brief does not say which, nor
  what "unavailable" means per window (one window bad → that window
  unavailable, or the whole provider card?).
- **Claude 429 / 401 / 403.** These three are different events and the brief
  conflates them under "unavailable." A 429 means *back off and keep the
  last good value*; a 401 on the CLI access token means *try the refresh
  token once, then prompt re-login*; a 403 on a `sk-ant-oat01-` setup-token
  (explicitly **unverified** against this endpoint) means *this token kind
  is rejected*, not *the login expired*. Treating a token-kind rejection as
  an expired login will send users into a re-login loop for a login that is
  fine. The design must map status codes to distinct UI states, with a
  refresh-attempt cap so a revoked token does not spin the refresh endpoint.
- **Codex version skew.** The protocol was verified on one version,
  0.164.0-alpha.9. There is no version check, no capability probe, and no
  pinned or minimum version. A user who upgrades Codex and gets a renamed
  method, a changed `account/rateLimits/read` shape, or a new required
  `initialize` parameter will see the spawn fail or return unparseable
  output. Worse, a user who *downgrades* (or is on an older release than the
  one verified) is in the same position with no way for the page to tell
  them why. The design needs: run `codex --version`, compare against a
  known-good range, probe the method, and on mismatch show "usage
  unavailable — Codex version X is not supported for usage yet" with the
  manual fallback, not a crash or a blank card.
- **No kill switch.** The Claude endpoint posture is open decision (4)
  ("always-on with honest fallback vs opt-in"), but neither option as
  sketched includes a runtime kill switch. If the endpoint starts 429-ing
  every dashboard or returning garbage, every page load and every live poll
  pays for it until a code release ships. A feature flag or per-provider
  disable that an operator (or the user, in the UI) can flip without a
  deploy is a rollout requirement, not a nice-to-have, for an undocumented
  dependency.

**Required:** a failure taxonomy (per status code and per parse failure),
per-window degradation (never page-level failure), a "last good value +
age" rule, a Codex version gate with a known-good range, and a kill switch
for the Claude endpoint. A bad usage response must never flip auth state:
usage "unavailable" and login "not connected" are different facts and must
never be confused in the UI or in any recorded state.

## 🔴 Blocker 3 — The cache and first-run story is missing, and the render path is synchronous

Open decision (1) asks where usage is cached (state.db vs a JSON file) and
how often it refreshes, but the brief never addresses the properties the
cache must have for this to be safe:

- **First run with no cache.** On the first dashboard load after upgrade
  there is no cached usage, no import record, and no background refresh has
  ever run. The page must render instantly with "usage not checked yet"
  (or equivalent) and kick off a background refresh — it must not block the
  Connections page on a network call to Anthropic or a 15-second Codex
  app-server spawn. This matters because the current render path is
  synchronous: `connections_page` calls `views.cli_providers()` inline, and
  `connections_live` re-runs `cli_providers()` on every poll and returns the
  result as JSON. If usage fetching is bolted onto either call, every poll
  either blocks on the provider or hammers it. Usage refresh must be
  decoupled from both the page render and the live poll, on its own sparse
  cadence (minutes, not poll interval), following the precedent already in
  the codebase: `connections_page` kicks off background server-stats
  collection (`self.server_stats.refresh(...)`) and renders cached values.
- **Cache keying and invalidation.** A cached usage value is only valid for
  the account and credential it was fetched with. The live poll already
  re-runs detection, so a user who signs out of the CLI in a terminal, signs
  in as a different account, or deletes a saved token will flip auth state
  on the next poll — but a cache keyed only by provider would keep showing
  the *previous account's* usage under the new login. The cache must be
  keyed by account identity (or a non-secret fingerprint of the credential)
  and invalidated on any auth change. The brief never mentions this.
- **Poisoning.** A failed fetch must not overwrite a good cached value. The
  rule has to be: errors leave the last good value in place with its
  timestamp (shown as stale), and only a *successful* fetch replaces it.
  A parse failure that writes "unavailable" over good data is a regression
  on every provider hiccup.
- **Storage choice has a migration of its own.** state.db means a schema
  migration and a versioned upgrade path; a JSON cache file means atomic
  writes, file permissions, and cleanup when a login is removed (a stale
  cache file for a deleted login is the same bug as the keying problem
  above, persisted to disk). Either is defensible; "decide later" is not,
  because the choice determines the upgrade path for every existing install.

**Required:** background-only refresh with a stated cadence per provider,
page render that never waits on usage, cache keyed by credential/account
fingerprint with invalidation on auth change, errors-never-overwrite-good-
cache, and a first-run state that is honest about never having checked.

## 🟡 Major 1 — "Imported" as a recorded state will go stale; derived-live loses provenance

Open decision (2) asks whether "imported" is a recorded state or derived
live on each render. Both options have a migration failure the brief does not
name:

- **Recorded** means a row/file saying "imported from this device at time T
  from path P." If the user then signs out of the CLI in a terminal, deletes
  `~/.claude/.credentials.json`, or the file moves to the macOS Keychain
  (which the brief notes as a possibility, with no design for reading it),
  the record claims an import that no longer exists. A stale "imported"
  badge next to a dead login is a trust bug: the user believes workers can
  use a login that is gone.
- **Derived live** avoids staleness — detection already re-runs on every
  poll — but then "imported" is just a re-label of `cli_login`, and there is
  nothing to record a detection time or source path against, which the brief
  says the design should show ("source path + detection time, names only").

The safe shape is a hybrid the brief does not propose: derive auth live on
every render/poll (the existing `cli_providers` behaviour), and treat any
recorded import as a *hint* that is revalidated against live detection on
every read and cleared or flagged the moment the source disappears. The
design must also say what happens to the record when the CLI is uninstalled
(`installed: false` already appends "; `command` is not on PATH" to the
detail — an imported record must not survive that) and when the user has
never had either CLI (no record, no import UI, manual flows only — the brief
states this as a goal but the record lifecycle is what enforces it).

## 🟡 Major 2 — Login expiry mid-run has no defined transition

Both providers' logins expire, and the brief's refresh story is one sentence
(Claude: renew against `console.anthropic.com/v1/oauth/token` with the
refresh token; Codex: nothing stated). Missing transitions:

- **Claude access token expiry.** `_credentials_login()` in
  `provider_auth.py` only checks that `claudeAiOauth` is present — it never
  reads `accessToken`, `expiresAt`, or the refresh token, and there is no
  refresh code anywhere in the repo. The usage fetcher is therefore the
  first code that must read token *values* (today nothing returns values;
  the tests assert values never appear in labels/details). That expands the
  secret-handling surface: the refresh flow needs the refresh token, must
  never log it, and must decide who owns writing a renewed token back —
  writing into `~/.claude/.credentials.json` risks corrupting the CLI's own
  file (format, permissions, Keychain sync on macOS). If renewal fails
  because the refresh token is itself expired or revoked, the state must
  become "login expired — sign in again," not "usage unavailable," and it
  must not retry the refresh endpoint in a loop.
- **Codex auth expiry.** `~/.codex/auth.json` has a `tokens` blob whose
  expiry the current `_codex_auth_file()` never inspects (presence only).
  The app-server call will fail with an auth error; the design must map
  that to "login expired" with the device-code flow as the next step,
  rather than generic "unavailable."
- **Expiry between fetch and render.** A token can expire after a successful
  usage fetch but before the user reads the page. Showing a fresh-looking
  number from a now-dead login is acceptable only if the number carries its
  fetch time — staleness must always be visible, never implied fresh.

## 🟡 Major 3 — Bare mode makes "imported" misleading

`claude_code_auth(..., bare=True)` already warns that bare mode does not
read the OAuth token *or* the CLI subscription login
(`provider_auth.py`, the `bare` branches). The brief lists the bare-mode gap
as open decision (5) but does not connect it to this feature: in bare mode,
an "imported" CLI login with a usage bar says "workers reuse that login"
when workers in fact cannot use it. Either usage/import must be suppressed
or visibly qualified in bare mode ("imported, but bare-mode workers can't
use this login — set `ANTHROPIC_API_KEY`"), or the bare-mode gap must be
closed first. Showing usage for a login the worker cannot spend is the
exact confusion the "remaining" number is supposed to prevent.

## 🟡 Major 4 — The two windows are not the fixed pair the UI assumes

The brief's goal says "the 5-hour session window and the weekly window," and
the Codex section correctly warns not to hardcode two windows: derive them
from `windowDurationMins`, because the 5-hour window is suspended for many
accounts and `secondary` may be null. That warning needs to become a UI
rule, not just a parsing rule: if `secondary` is null, the page must show
*one* window, not a phantom 5-hour bar at 0% or "unavailable." The same
applies to Claude if Anthropic ever drops or renames a window. A fixed
two-bar layout that renders a missing window as empty will be read as
"you have nothing left," which is the most alarming misreading this feature
can produce. Also note the windows are not the same thing across providers:
Claude's `seven_day` and Codex's 10080-minute window are both weekly, but
their reset semantics and the `seven_day_opus` / `seven_day_sonnet` splits
have no Codex equivalent — the design must not present them as directly
comparable bars.

## 🟡 Major 5 — Codex spawn discipline is specified but its failure modes are not

The brief gives good spawn discipline (temp-dir `cwd`, scrubbed env, 15 s
timeout, SIGTERM after the answer, 4 MiB cap). Three gaps:

- **Scrubbed env vs. auth discovery.** If scrubbing removes `HOME`, the
  app-server cannot find `~/.codex/auth.json` and reports "not logged in"
  for a logged-in user. The design must state exactly which variables
  survive scrubbing (`HOME` at minimum) and test that a scrubbed spawn
  still authenticates.
- **Concurrency.** The live poll and the page render can both trigger a
  refresh; without a per-provider lock or single-flight guard, a slow
  app-server spawn (up to 15 s) can pile up concurrent `codex app-server`
  processes on every poll. The existing device-flow code already uses a
  lock and a "busy" error (`CodexDeviceFlow`, `DeviceFlowBusy`) — the
  usage refresher needs the same treatment.
- **Orphaned processes.** SIGTERM "after the answer" assumes an answer
  arrives. On a hung spawn the timeout must kill the whole process group,
  and the design should state the zombie/orphan policy (the dashboard must
  not accumulate app-server processes across refreshes).

## 🟡 Major 6 — macOS Keychain is named and then abandoned

The brief notes credentials "may live in the Keychain
(`Claude Code-credentials` service) instead of the file" on macOS. That is
a migration fork, not a footnote: on a Mac where the CLI stores credentials
in the Keychain, `_credentials_login()` returns False today — the dashboard
already reports "not connected" for a signed-in user. Auto-import built on
file reading inherits that false negative, and any design that reads the
Keychain from the dashboard raises a permission question (Keychain access
prompts, `security` CLI, user approval) that the brief does not address.
The design must state: file-only with a documented Keychain gap, or a
Keychain read path with its permission UX — and the false-negative case
must render as "couldn't check" rather than "not logged in."

## 🟡 Major 7 — Account identity is asymmetric and the brief lets it stay vague

Open decision (3) notes Codex returns `planType` in the rate-limits
response while Claude's usage response carries no plan. That asymmetry has
a migration consequence: after upgrade, Codex cards can show a plan and
Claude cards cannot, and any attempt to infer a Claude plan (from the
token, the account, or the presence of Opus/Sonnet splits) would be
guessing. The design must state explicitly what identity is shown per
provider (Codex: `planType` when present; Claude: none, or only what a
verified endpoint returns) and must not backfill Claude identity from local
files. It must also say whether identity is part of the cache key (see
Blocker 3): if two accounts share a machine, usage keyed by provider alone
is wrong.

## 🟢 Nits / smaller gaps

- **API-key users and usage.** An `ANTHROPIC_API_KEY` user has no
  subscription windows (only per-minute `anthropic-ratelimit-*` headers,
  per the brief). The design should state that the usage section is hidden
  — not shown as unavailable — for API-key auth, with one line explaining
  why, so it is not read as a broken feature.
- **The `limits[]` array and Opus/Sonnet splits** are mentioned as present
  in the Claude response but never assigned a UI fate. If they are parsed
  and ignored, say so; if shown, say where. Unowned response fields are how
  a shape change first goes unnoticed.
- **Reset-time parsing.** `resets_at` / `resetsAt` are ISO-8601, but the
  brief never states timezone handling or what renders when a reset time is
  missing or unparseable (a window with a percentage but no reset time must
  not render a broken date).
- **Tests.** `tests/unit/test_provider_auth.py` is thorough for detection
  and the device/setup-token flows, and it asserts values never leak into
  labels/details. The usage feature needs the same class of tests the brief
  does not name: malformed/renamed-field responses, 429 with `Retry-After`,
  401-then-refresh-then-401, Codex version mismatch, cache poisoning by an
  error response, auth-change invalidation, and first-run-no-cache rendering.
  No test may use a real token or a real endpoint — the existing fake-token
  discipline must extend to usage fixtures.
- **"Imported" wording.** The word suggests the dashboard copied the login
  into its own storage. It does not (and must not — the CLI keeps its own
  files). The UI copy should say "detected on this device" or similar, so a
  user never concludes Baton duplicated their credential, and never expects
  the import to survive uninstalling the CLI.

## What the design doc must decide before implementation

1. Precedence table for (saved token × CLI login × API key) per provider,
   including which credential usage is measured against.
2. Failure taxonomy: per-status-code and per-parse-failure UI states;
   refresh-attempt cap; token-kind rejection distinguished from expiry.
3. Codex version gate (known-good range, probe, unsupported-version copy).
4. Kill switch / opt-in posture for the undocumented Claude endpoint.
5. Cache: storage, key (credential/account fingerprint), invalidation on
   auth change, errors-never-overwrite-good-cache, first-run state,
   background-only refresh decoupled from page render and live poll.
6. Import record lifecycle: revalidated-live hybrid, cleared when the
   source disappears, never surviving CLI uninstall.
7. Bare-mode treatment of import + usage.
8. Keychain: file-only with documented gap, or a permissioned read path.
9. Per-provider identity shown (Codex `planType`, Claude none) and whether
   identity is part of the cache key.
10. Single-flight guard and process-group cleanup for Codex spawns; exact
    env scrub list (`HOME` preserved).
