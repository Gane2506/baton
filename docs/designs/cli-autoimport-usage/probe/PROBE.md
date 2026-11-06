# Probe record — Baton #396, design §9.0

Run by the orchestrator on the Mac (Ganesh's MacBook Pro) 2026-11-05 ~21:23 EST.
Throwaway script: `probe.py` (VM copy at `~/workspace/baton-program/probe-396/probe.py`;
Mac copy at `/tmp/probe-396/probe.py`). Stdlib only. No tokens were refreshed
(design: Baton never refreshes — a refresh could kill the CLI's login).
Redaction verified after fetch: secret scan clean (one literal `sk-ant-`
prefix mention in a note only); `accountId` recorded as SHA-256 hex only.

Raw redacted records sit next to this file. CLI versions (item 7):
`claude` 2.1.318 (Claude Code) at `~/.local/bin/claude`;
`codex` codex-cli 0.167.0 at `~/.npm-global/bin/codex`.
Note: design §6.3 was verified against codex-cli 0.147–0.158; 0.167.0
speaks the same app-server protocol (item 1 proves it).

## 1. Codex — PASS (shape confirmed), with a design gap found

`codex app-server --listen stdio://`, initialize id 1
(`clientInfo` name `jev`, title `Baton`, version `0.5.0`), `initialized`,
`account/rateLimits/read` id 2. Full redacted success response in
`01-codex-ratelimits.json`. Pass conditions met: `result.rateLimits.primary`
present, `usedPercent` present (48).

Measured: primary `windowDurationMins` **43200** (= 30 days),
`resetsAt` **1795687699** (unix seconds — confirmed: a milliseconds value
would be ~1.8e12); secondary **null**; `planType` null.

**Finding for resolve:** 43200 is not in the design's duration table
(300 → session, 10080 → weekly). Per the design's own rule an unknown
duration is dropped, not fatal — which would leave this account's card
showing NO windows (primary dropped, secondary null). The design must
decide what 43200 means (monthly window?) and what the card shows when
only a monthly window exists, including the Weekly-line logic. Phase A
is go only after this resolve; implementing the current rule would ship
a Codex card that is empty on the very device it was verified on.

Method note: the first attempt failed for a PATH artifact, not a
protocol failure — the SSH shell's PATH lacks `/opt/homebrew/bin`, so
the `codex` wrapper died with `env: node: No such file or directory`.
Re-ran with the dashboard's darwin search dirs on the child's PATH;
the design's `resolve_executable` must cover this or the same failure
will recur in any thin-PATH launch.

## 2. Claude usage endpoint — BLOCKED (no token obtainable as specified)

No call was made. There is no file-stored Claude OAuth token on this
device: `~/.claude/.credentials.json` (the path design D10 names) does
not exist, and `~/.claude.json` holds `oauthAccount` profile metadata
only (uuid, email, org, tiers) with no token fields — verified by key
name inspection (values never read). On Claude Code 2.1.318 the tokens
live in the macOS Keychain (service `Claude Code-credentials` per D10),
which this probe does not read: reading it is the D10 follow-up (needs
a permission UX and must not pop an access prompt on every check), and
attempting it unattended could pop a Keychain prompt on the user's Mac.

This is not an endpoint failure and not a "no login" case — the login
exists (Claude Code works on the Mac; `oauthAccount` is populated).
Phase B cannot start until the design resolves HOW Baton obtains the
token: Keychain permission UX as a prerequisite (not a follow-up), or
another path. Design returns for resolve on this question.

## 3. Claude success rate — NOT RUN

Blocked on item 2: the cadence measurement needs a working token.
No attempts made; the 15-minute floor stands regardless. Re-run after
item 2 is unblocked. Cadence decision: pending.

## 4. Setup token — UNRESOLVED

No distinct setup token exists on the device to test (no separate
setup-token storage; the only sk-ant- credential is the stored OAuth
token, which is in the Keychain — see item 2). Per §9.0, Phase B
defaults to rejected (`setup_token_rejected`, never tried).

## 5. Account identity — UNANSWERED

Needs a 200 body from item 2. No Claude fingerprint question can be
settled until then. D5 stays open.

## 6. Splits — UNANSWERED

Needs a 200 body from item 2. Whether the Opus/Sonnet splits are
present, and whether either binds below the headline weekly window,
is unknown. The Phase B test fixture cannot be written yet.

## 7. CLI versions — PASS

Recorded above (claude 2.1.318, codex-cli 0.167.0).

## Go / no-go per phase

- **Phase A (Codex): CONDITIONAL GO** — item 1 passed, but the 43200
  duration must be resolved first (see item 1). After the resolve,
  Phase A can start on branch `design/issue396-cli-autoimport`.
- **Phase B (Claude): NO-GO** — items 2/3/5/6 blocked on token
  acquisition; item 4 unresolved (safe default stands). The design
  returns for resolve on the Keychain question before Phase B starts.
