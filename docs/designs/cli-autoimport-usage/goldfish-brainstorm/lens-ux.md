# Goldfish review — UX & Developer Ergonomics lens
## Auto-import Claude Code / Codex CLIs + show account usage (issue #396)

Reviewer: fresh Goldfish, UX & Developer Ergonomics lens.
Sources read: `concepts_brief.md`, `src/jev_agent/core/provider_auth.py`,
`src/jev_agent/dashboard/routes_connections.py`,
`src/jev_agent/dashboard/views.py` (`cli_providers`), `src/jev_agent/dashboard/templates/connections.html`
(Providers section), `tests/unit/test_provider_auth.py`.
No code was modified. Critique first; nothing here is praise for the concept.

## The one-sentence verdict

The concept adds a second, half-visible system (usage) on top of a first
system (auth) whose own wording is already inconsistent — and it does so in
the most crowded card on the Connections page, without ever telling the user
which account any of it belongs to. Most of the objections below are not
about fetching numbers; they are about what a user will misunderstand when
the numbers, the labels, and the buttons disagree with each other.

---

## Blocker 1 — "Imported" vs "detected" vs "signed in" are three words for one thing, and "imported" is the wrong one

The brief uses "detects those logins automatically", "records them as
imported from the device", and the existing UI already says "The `claude`
CLI is signed in — workers reuse that login" and "Signed in with your Claude
subscription" / "Signed in with ChatGPT". That is at least four phrases for
one underlying fact: a login file already exists on this machine and Baton
did not create it.

"Imported" is actively misleading. In ordinary software, importing means
copying something into the app: the user will reasonably conclude that Baton
now holds a copy of their Claude/Codex login, that it can be removed from
Baton, and that it travels with Baton. None of that is true. The brief is
explicit that detection is read-only, tokens are never copied out, and the
CLI keeps its own credentials file. So "imported" promises a thing the
feature does not do, and sets up the exact support question the feature will
generate: "I removed it from Baton, why am I still signed in?" / "I signed
out of the CLI, why does Baton still say imported?"

Worse, the existing code already collapses distinct things into one label.
`provider_auth.py` gives both `oauth_token` (a token Baton itself saved to
`~/.jev/secrets.env` via the setup-token flow) and `cli_login` (a login the
CLI created, living in `~/.claude/.credentials.json`) the identical label
"Subscription login (OAuth)". The template then renders them differently
anyway ("Signed in with your Claude subscription" vs "The `claude` CLI is
signed in — workers reuse that login"), so label and prose already
contradict each other. Adding a third vocabulary word ("imported") on top of
that split guarantees the user cannot tell, from the card, who owns the
login, where it lives, or what happens if they delete it.

Recommendation: pick exactly one word and use it everywhere, including the
badge, the detail line, and the brief. "Detected on this device" is honest;
"imported" is not, unless the design genuinely copies and owns the
credential — which it must not. The card must also name the source in plain
language ("found in the Claude CLI's own sign-in, not saved by Baton"), not
a file path alone, and the two OAuth cases must stop sharing a label.

## Blocker 2 — The card never says *whose* account this is, so usage numbers are unauditable

Nothing in the brief, the detection code, or the template shows account
identity: no email, no account name, no plan for Claude (the brief admits
the Claude usage response carries no plan), and only `planType` for Codex.
A user with one Claude account will not notice. A user with two — personal
and work, or a Max account and a Pro account — cannot tell which account's
remaining usage they are looking at, and cannot tell whether the 5-hour bar
belongs to the login Baton found or to some other login.

This stops being cosmetic the moment usage is shown. A usage bar without an
account is a number the user cannot verify and cannot act on: "42% left" of
*what*, for *whom*? If the number looks wrong, the user's only recourse is
to open the CLI and run `/usage` — which is exactly the terminal trip this
feature exists to eliminate.

Recommendation: the design must state, per provider, what identity can
honestly be shown and what cannot, and must show it in the same visual
group as the usage bars. If Claude genuinely exposes no identity through
the usage endpoint, the design must say so on the card ("account name not
available from Claude") rather than silently showing a bare bar — and the
asymmetry with Codex's `planType` must be a deliberate, visible difference,
not an accident the user discovers by comparing cards.

## Blocker 3 — Dual auth: a CLI login *and* a dashboard-saved token, and the card shows only one of them

Precedence in `provider_auth.py` is silent and total: an `ANTHROPIC_API_KEY`
outranks everything; a dashboard-saved `CLAUDE_CODE_OAUTH_TOKEN` outranks
the CLI login; the CLI login is only reached when nothing else is set. The
card renders a single badge and a single detail line for whichever source
won. The losing source is invisible.

That creates three concrete confusions the design does not address:

1. **Wrong-account usage.** A user signs in to the CLI as account A, then
   pastes a setup-token for account B through the dashboard (or vice versa).
   The usage bars will show B's allowance while the user believes they are
   looking at A — or the CLI's `/usage` will disagree with the dashboard
   with no explanation.
2. **Surprising fallback on removal.** Removing the dashboard-saved token
   (a button that exists today under Keys) silently promotes the CLI login
   underneath it. The badge flips from one "Subscription login (OAuth)" to
   the identical "Subscription login (OAuth)" — the user sees no change at
   all, while the account in use has changed.
3. **API key masking a subscription.** With `ANTHROPIC_API_KEY` set, the
   subscription login is entirely hidden, yet the user is still paying per
   token. The card says "API key" and nothing about the dormant
   subscription login that would be free (metered) instead.

Recommendation: when more than one auth source is present, the card must
say so ("Using the API key; a Claude subscription login is also present but
not in use") and name which source the usage numbers belong to. A single
badge cannot carry this; the design needs a small "active source + other
sources present" treatment, and removal of the active source must warn
that a different login will take over.

## Blocker 4 — "Usage unavailable" and "usage stale" will read as "broken", and the Claude source makes them common

The brief is honest that the Claude endpoint is undocumented, rate-limits
itself (roughly one request in three succeeds under an active session), and
may reject token kinds; Codex's source is local and cheap. The design
therefore has to show, routinely, a state that is neither "usage" nor "no
usage": the last fetch failed, or the number is minutes/hours old.

Nothing in the brief specifies what that looks like, and the existing page
gives no good pattern to copy. The live poll (`connections_live`) returns
only `provider`, `auth`, `label`, `installed` — no usage, no timestamp — so
as specified, usage cannot even update without a full page reload, and a
user staring at a stale bar has no way to know it is stale. A cached number
presented without a timestamp is worse than no number: the user will plan a
long run against a "38% remaining" that was true two hours ago, hit the
limit mid-run, and blame Baton for lying.

The failure modes also differ per provider in ways the user will not
understand: Claude 429s ("try again later"), an expired CLI access token
that needs a refresh, a setup-token the endpoint may or may not accept, a
Codex `secondary` window that is null because the 5-hour window is suspended
for that account (brief: "do not hardcode two windows"). A null second
window will look like a missing feature or a bug unless the card explains
it in words ("Your plan has no separate 5-hour limit — weekly only").

Recommendation: every usage display must carry its own freshness label
("updated 4 min ago", "couldn't refresh — showing 2 h old data", "couldn't
load usage — [Retry]"), with a manual refresh control, and the poll must
carry usage + timestamp or the design must admit usage only updates on
reload. "Unavailable" must distinguish "couldn't reach the provider" from
"this auth type has no usage to show" (see Major 2) — those are different
states with different next steps, and conflating them teaches the user that
retrying fixes a structural absence.

## Major 1 — The state matrix is larger than the brief admits, and several states have no next step

Enumerating what the user can actually see, per provider:

| State | What the card should say | Next step in the dashboard? |
|---|---|---|
| CLI installed + CLI login present | detected login + usage | none needed (the happy path) |
| CLI installed, not logged in | not signed in | sign-in button exists today — good |
| CLI not installed | not on PATH | today: a bare `npm install -g …` command to type in a terminal — **not** a dashboard action, and it contradicts the brief's "nothing needs the terminal" |
| CLI login present, usage fetch fails / 429 | usage unavailable, auth fine | retry button — not specified |
| Usage fetched, now stale | last-updated time | refresh button — not specified |
| API-key auth only | no subscription windows exist | none — but must be explained, not blank (Major 2) |
| Bare mode (`claude.bare`) | CLI login exists but workers will not read it | today: a warning telling the user to "set `ANTHROPIC_API_KEY`" — a terminal/env-var action with no in-dashboard path; the card simultaneously says "signed in" and "workers won't use it" |
| macOS Keychain credentials | login lives in Keychain, not the file | detection reads only the file today — a signed-in Mac user may see "not connected" with no explanation |

Two of those rows are the sharpest. **CLI missing** is currently a dead
end disguised as help: the template prints an npm command and stops. If
"zero terminal use" is a real goal, the design must either provide an
in-dashboard install path or explicitly scope CLI-missing out of that
promise — it cannot keep the terminal command and claim the goal. **Bare
mode** is worse: it is a state where the card's headline ("signed in")
and its warning ("bare mode does not read the CLI subscription login")
contradict each other, and the usage bars — if shown — would describe an
allowance the workers cannot even spend. Showing usage for an auth source
that is not in effect is misleading; hiding it while still saying "signed
in" is confusing. The design must decide: in bare mode, does the card show
the CLI login at all, and does it show its usage?

Recommendation: the design doc must contain this matrix, one row per
state, with the exact card copy and the exact next-step control for each.
Any row whose next step is "open a terminal" must be labelled as a
documented exception to the zero-terminal goal, not silently left in.

## Major 2 — API-key auth has no windows, and a blank or "N/A" usage area will read as an error

For both providers, API-key auth has no 5-hour or weekly window: Claude
has only per-minute rate-limit headers, Codex's local rate-limits call is
tied to the subscription login. The brief lists "API-key auth with no
windows" as a state to show, but does not say what the user sees.

The obvious implementations are all bad: two empty bars (reads as 0%
remaining — alarming, and wrong, since an API key is pay-per-token and
effectively unmetered from the user's point of view); a greyed-out "N/A"
(reads as broken, and invites a retry that can never succeed); or nothing
at all (reads as a rendering bug, and the user will compare with the other
provider's bars and conclude something failed). The user also needs the
cost framing: an API key is not "unlimited", it is "billed per token, no
allowance window applies" — a different concept from "100% remaining", and
conflating them will produce a nasty billing surprise for someone who
assumes the empty bar means free.

Recommendation: the API-key state needs its own explicit, non-bar copy
("API key — billed per token. Subscription usage windows don't apply to
API keys."), visually distinct from a failed fetch, with no progress bars
at all. The design must also decide whether usage is hidden or replaced in
that state, and must not reuse the "unavailable" wording that means
"fetch failed" elsewhere.

## Major 3 — Two windows per provider, plus reset times, plus plan, does not fit the card as built

Count what the Providers section already holds per card: a title, two
badges ("CLI installed" + auth label), a detail line, an optional warning
flash, a sign-in flow (URL + code + two forms) or a "Set" line, and a hint
paragraph about `~/.jev/secrets.env` and the API-key fallback. It is
already the densest card on the page.

The concept adds, per provider: two usage bars (5-hour + weekly), two reset
times, a freshness timestamp, possibly a plan label, possibly Opus/Sonnet
splits (the Claude response carries `seven_day_opus` / `seven_day_sonnet`
— the brief mentions them without deciding whether they are shown), and a
refresh/retry control. Doubled across two providers, that is roughly four
bars, four timestamps, and two freshness labels in one section, plus the
existing sign-in machinery. Rendered naively, the section becomes a wall of
meters the user scans past without reading — the exact opposite of "show
what is left".

There are also unit confusions to settle before layout: is the bar
"remaining" or "used"? The APIs return `utilization` / `usedPercent`
(used), while the concept promises "remaining" — the design must pick one
direction and label it unambiguously ("62% left" vs "38% used" are the same
number and opposite readings; a user who misreads the direction will either
panic or over-commit). Reset times need relative, human phrasing ("resets
in 2 h 14 min", "resets Fri 09:00") — a raw ISO `resets_at` timestamp is
not a usable display. And Codex's `primary` / `secondary` naming maps to
5-hour / weekly only by `windowDurationMins`; showing the words "primary"
and "secondary" to a user would be jargon with no referent.

Recommendation: usage must be collapsible or progressively disclosed —
e.g. one compact line per provider ("Session 62% left · resets in 2 h ·
Weekly 81% left") with the second window, splits, and exact reset times
behind a disclosure — and the design must include a concrete mock or
layout sketch proving the section still scans at a glance. Decide the
bar direction ("left" vs "used") once, in words, and apply it to both
providers; never show a raw ISO timestamp.

## Major 4 — "This device" is ambiguous when the dashboard host is not the machine in front of the user

"Imported from the device" / "detected on this device" assumes the user
knows which device the dashboard runs on. Baton's dashboard runs on a host
(the user's Mac, per the program's standing setup) while the user may view
it from another machine or browser. "This device" to the user means the
laptop in their hands; to Baton it means the dashboard host. A user who
signed in to Claude Code on their laptop but not on the Mac will see "not
logged in" and conclude Baton is wrong — or, worse, will sign in through
the dashboard and then wonder why their laptop CLI still asks them to log
in.

The brief's "source path + detection time" helps only if the source path
is recognisable: `~/.claude/.credentials.json` is recognisable to a
developer on the host, meaningless to anyone else, and on macOS the
credentials may live in the Keychain instead of the file at all. Detection
time ("detected 3 min ago") is useful only if the user understands that
detection re-runs on poll and that signing in on another machine changes
nothing here.

Recommendation: the card must name the host in plain language ("on the
Mac running this dashboard" / the dashboard host's name), and the design
must state what happens on multi-machine setups — including the fleet case,
where different machines may hold different logins. If the dashboard host
cannot be named plainly, "this device" must not be used.

## Major 5 — There is no way to remove, disconnect, or stop using an imported login from the dashboard

Import is one-way in the concept: detection finds a login, the card shows
it, workers reuse it. There is no "Forget this login" / "Stop using the CLI
login" control, and there cannot be one that is honest — Baton does not own
the credential, so "removing" it from Baton would be a lie; the real action
is signing out of the CLI (`claude logout` / `codex logout`), which is a
terminal action.

That leaves the user with no dashboard-native way to undo the feature's
central act. If the wrong account was detected, if a shared machine holds
someone else's login, or if the user simply does not want Baton to see
their subscription, their only options are the terminal or ignoring the
card. On a shared or borrowed dashboard host, silently reusing whatever
login happens to be present is also a privacy surprise: the user never
consented to Baton reading their CLI login, they merely had the CLI
installed.

Recommendation: the design must address consent and removal explicitly —
at minimum a plain-language note ("Baton reads the CLI's own sign-in on
this machine; it never copies or stores it. To stop Baton using it, sign
out of the CLI.") and a decision on whether an explicit "don't use the CLI
login" opt-out (a dashboard setting that suppresses reuse) is in scope. If
it is out of scope, the design must say so and accept the terminal-only
removal as a documented gap, not leave the user to discover it.

## Major 6 — The two providers are asymmetric, and the UI will either lie about that or look broken

Codex's source is the CLI's own local protocol: no network call by Baton,
no undocumented endpoint, cheap to poll. Claude's is an undocumented server
endpoint that 429s, may reject token kinds, and depends on a refresh-token
flow. The brief correctly says the design must treat them differently —
but the UI, as sketched, treats them identically: two cards, two sets of
bars.

Identical presentation of asymmetric reliability means the user will hold
Claude's bars to Codex's standard. When Claude's usage is stale or missing
and Codex's is fresh, the user will conclude Claude is broken (or Baton
is), not that the two sources have different failure modes. The reverse
risk is also real: if the design papers over the difference with the same
"updated just now" copy for both, it is misrepresenting the Claude number's
provenance.

The asymmetry extends to what is shown: Codex gives `planType`,
`rateLimitResetCredits`, `spendControlReached`; Claude gives no plan and
gives Opus/Sonnet splits. A design that shows "Plan: Plus" on one card and
nothing on the other, or that shows model splits for Claude and not Codex,
will read as incomplete on whichever side is thinner — unless the design
explains the difference in one line.

Recommendation: the design must make the asymmetry visible and honest —
different freshness expectations per provider, and a one-line provenance
note per card ("from the Codex CLI on this machine" vs "from Claude's
usage service — may be a few minutes old"). Do not force the two cards
into identical layouts when their data and reliability differ.

## Nit 1 — "5-hour session window" is provider jargon, not a user concept

Users think in "how much can I use before it stops" and "when does it come
back". "Five-hour session window", "weekly window", "utilization",
"windowDurationMins" are the providers' metering vocabulary. The card copy
must translate: "Session limit" / "Weekly limit" with "resets in …" is
close; "5-hour" can stay as a qualifier ("Session (5 h)") but must never be
the only label. The brief's own goal wording ("the 5-hour session window
and the weekly window") should not be copied verbatim onto the card.

## Nit 2 — The live poll already re-runs detection, so a login made in a terminal appears on the next poll — but nothing tells the user that

This is actually good behaviour, and the brief notes it. The UX gap is
that it is invisible: a user who signs in to the CLI in a terminal and
returns to the dashboard will see the card change with no explanation of
why, and a user who signs *out* will see the card change and may think
Baton broke something. One line of copy ("updates automatically when the
CLI sign-in changes") would close this. The same applies in reverse: the
poll interval is unstated, so the user cannot know whether "still showing
the old login" means "poll hasn't run yet" or "detection failed".

## Nit 3 — Existing copy the new feature inherits is already terminal-flavoured

The template's fallback text tells the user to "run `claude setup-token`
and paste the token", to run `codex login --device-auth`, and to type an
npm install command. The detection detail lines name env vars
(`ANTHROPIC_API_KEY is set (outranks the subscription login)`) and file
paths. None of that is wrong for a developer tool, but the concept's
headline promise is "nothing needs the terminal". The new usage feature
will be judged against that promise, and every inherited terminal-flavoured
line will read as a broken promise. The design should either rewrite those
lines into dashboard-native actions or narrow the promise to "viewing usage
needs no terminal" — the current wording claims both.

## Nit 4 — Refresh cadence is a UX decision, not just a caching decision

The brief defers cadence to the design doc (state.db vs JSON cache,
"minutes, not seconds"). From the user's side this is the difference
between "the bar moves as I work" and "the bar is a snapshot from lunch".
If the Claude endpoint only tolerates sparse polling, the honest cadence
may be 10–15 minutes — at which point the bar is a lagging indicator and
must be labelled as one. The design must pick a cadence per provider and
write the user-facing consequence of that cadence into the card copy,
rather than letting an engineering cache choice silently decide what the
user believes.

---

## What would change my mind

- One term for the login state, used in the badge, the detail line, and the
  brief — and it is not "imported" unless Baton genuinely owns a copy.
- A state matrix with exact card copy and an in-dashboard next step for
  every row, including CLI-missing, bare mode, API-key-only, and
  usage-unavailable/stale — with terminal-only steps labelled as exceptions.
- Account identity shown next to the usage bars, or an explicit statement
  that it cannot be shown and what the user should do about that.
- A layout sketch proving two windows × two providers + reset times +
  plan + freshness fits the Providers section without turning it into a
  wall of meters — with the bar direction ("left" vs "used") decided in
  words.
- An explicit answer on dual auth (which source's usage is shown, what the
  user sees when a second source is present) and on removal/consent (how a
  user stops Baton using a CLI login without a terminal, or an honest
  statement that they cannot).

## Top 3 objections (summary)

1. **"Imported" is the wrong word and the third synonym for one state.**
   Detection is read-only; nothing is copied or owned by Baton. The code
   already gives two different OAuth sources the same "Subscription login
   (OAuth)" label while rendering them differently. One honest term —
   "detected on this device" — everywhere, or the user will believe Baton
   holds, and can remove, a login it does not own.
2. **Usage without account identity is unverifiable, and dual auth makes it
   actively misleading.** No email or account name is shown for either
   provider; a dashboard-saved token silently outranks a CLI login, and
   removing it silently promotes the login underneath — while the badge
   text does not change. The user cannot tell whose allowance the bars
   describe.
3. **The failure states are the common case for Claude, and the design has
   no face for them.** The undocumented endpoint 429s routinely, the poll
   carries no usage or timestamp, and API-key auth has no windows at all.
   A stale or missing bar presented without freshness, retry, and a
   distinction between "fetch failed" and "no windows exist" will read as
   broken — and the CLI-missing and bare-mode rows already have
   terminal-only next steps that contradict the "nothing needs the
   terminal" promise.
