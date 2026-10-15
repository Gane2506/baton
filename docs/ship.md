# Shipping PRs and tracking KPIs

With shipping on, a run ships itself once both votes approve it at D6: it pushes its branch,
opens the PR, waits for the checks and merges it, or first waits for you to approve, modify or
stop it ([Shipping after the votes](#shipping-after-the-votes)). `jev ship RUN` turns a finished
run into a GitHub pull request by hand. `jev ship sync` reads the PR
back from GitHub, and `jev kpi` and the dashboard's **KPIs** page show what each shipped PR
cost, how it went, and how that trends over time. Everything here is opt-in and runs with your
own `gh` login. Safety details: [security.md](security.md#shipping-prs).

![The dashboard KPIs page: summary cards, regressions, shipped PRs and trend charts](images/dashboard-kpis.png)

## Setup

1. Install the [GitHub CLI](https://cli.github.com) and log in: `gh auth login`.
2. Turn shipping on in `jev.toml` ([config.md](config.md#ship)):

   ```toml
   [ship]
   enabled = true
   remote = "origin"            # where the branch is pushed
   base = ""                    # "" = the repo's default branch
   branch_prefix = "jev/"
   draft = true
   labels = []                  # labels must already exist in the repo
   require_quality_pass = true
   # after_approval = "merge"   # or "ask"; unset: "merge" under consensus, else "ask"
   # merge_method = "squash"    # or "merge", "rebase"
   ```

## `jev ship RUN`

```bash
jev ship 20261012-153012-ab12 --dry-run   # show the PR body and the commands; run nothing
jev ship 20261012-153012-ab12             # asks before pushing
jev ship 20261012-153012-ab12 --yes       # no question
jev ship 20261012-153012-ab12 --force-new # another PR for an already shipped run
```

It refuses, with the reason, unless all of these hold:

- `[ship] enabled = true`;
- the run is `DONE` and its result branch `jev/<run>/result` exists;
- with `require_quality_pass` (the default), no task's or the integration's latest quality
  gate blocked;
- the remote exists, `gh` is installed and `gh auth status` succeeds;
- the run has not been shipped yet (`--force-new` opens a second PR from a new branch,
  `<prefix><run>-2`); a branch of that name on the remote with other commits is never
  overwritten.

Then it shows the remote, repository, branch, base, title and a summary of the body, asks for
confirmation, and runs:

```bash
git push <remote> jev/<run>/result:refs/heads/<branch_prefix><run>     # never --force
gh pr create --repo <owner/repo> --head <branch> --base <base> \
  --title "<first line of the idea, at most 72 characters>" --body-file <tmp> [--draft] [--label ...]
```

Your checkout is never touched: the result branch already exists locally and is pushed as is.
The repository comes from the remote URL (else `gh repo view`); the base is `[ship] base`, else
the remote's default branch (`refs/remotes/<remote>/HEAD`, else
`gh repo view --json defaultBranchRef`), else the branch checked out in the repo. If the push
worked but `gh pr create` failed, run `jev ship RUN` again: the push is skipped when the remote
branch already holds the same commit. `jev ship RUN` never merges a PR; only the pipeline does,
after the votes ([below](#shipping-after-the-votes)). jev never enables auto-merge.

GitHub may open the PR even when `gh pr create` reports an error or times out (the answer was
lost). So after such a failure, and before creating a PR on a branch an earlier `jev ship`
already pushed, jev asks
`gh pr list --repo <repo> --head <branch> --state all --json number,url,state,headRefName`;
a PR found there is recorded (with its state) instead of opening a second one. A timeout with
no PR found is reported as an unknown outcome; run `jev ship RUN` again once GitHub answers.
The shape of `gh pr list`'s answer is not verified against a live repository, so anything
unexpected reads as "no PR found" (docs/verification.md section 8).

Nothing waits for input: git runs without a terminal, with `GIT_TERMINAL_PROMPT=0` and an empty
`GIT_ASKPASS` (no password prompt, no askpass popup; credential helpers such as
`gh auth setup-git` or the OS keychain still work), and `gh` with `GH_PROMPT_DISABLED=1`. Every
call has a timeout: 10 minutes for `git push`, 1 minute for the other git calls, 2 minutes for
`gh`. A push that needs a password you have not stored fails with git's error instead of hanging
(set up a credential helper or use an SSH remote with a loaded key); a push that timed out may
still have landed, and running `jev ship RUN` again skips it if it did.

The PR body (Markdown, redacted like every log) has the idea, the plan summary, a task table
(worker, model, effort, attempts, cost), the quality summary (level, gates, blocking problems
and non-blocking findings by severity; the full review stays local, `jev quality show RUN`), a
KPI card and the decision statistics (Jev answers, rules fallbacks, mean Jev confidence),
ending with "Opened by Baton Agent Orchestrator (`jev ship`)".

The dashboard's run page has a **Ship as PR** button for a DONE, not yet shipped run when
shipping is enabled. It shows the same summary and the rendered body on a confirmation page;
confirming runs `jev ship RUN --yes` in the background, and the run page then links the PR.

## Shipping after the votes

With `[ship] enabled = true`, a D6 "ship" no longer ends the run at `DONE`. What happens next is
`[ship] after_approval` ([config.md](config.md#ship)), or the run's own choice:

| Setting | After both votes approve |
| --- | --- |
| `"merge"` (default under `primary = "consensus"`) | The run goes to `SHIPPING`: push, open the PR ready for review (never a draft), wait for its checks, merge it, then `DONE`. Nobody is asked. |
| `"ask"` (default with `jev` and `rules`) | The run waits in `AWAITING_SHIP`. Nothing is pushed yet. |

Per run: `jev run --auto-ship` ("merge") or `jev run --ask-before-ship` ("ask"); the same flags
on `jev queue add`, and the "After both votes approve" choice in the dashboard's queue form.
Both need shipping on. Shipping off: D6 ends the run `DONE` as before.

### Waiting for you (`AWAITING_SHIP`)

`jev run`, `jev approve` and `jev status` show the PR title, the result branch, the diff summary
and the D6 vote (Jev's confidence and Laya's margin). The dashboard's run page shows the same,
with three buttons. You choose:

```bash
jev ship approve RUN                          # ship and merge it (same as `jev approve RUN`)
jev ship modify RUN --notes "Rename foo to bar and add a test"
jev ship stop RUN                             # end it DONE, not shipped
```

- **approve**: the run goes to `SHIPPING`, as with "merge".
- **modify**: your notes become a new task, "Changes requested before shipping (round N)",
  after all the others; the run executes it, integrates again, and D6 votes on the new result.
  If both approve again, the run waits for you again (or ships, under "merge"). These tasks do
  not count against `max_fix_rounds`.
- **stop**: the run ends `DONE` with "not shipped"; its result branch stays, and `jev ship RUN`
  can still open a PR for it by hand.

Nothing is pushed before you approve: integration rebuilds the result branch from the start
commit, so a PR opened before a "modify" could only be updated by force-pushing.

### Shipping (`SHIPPING`)

1. Push and open the PR exactly as `jev ship RUN --yes` does (all its checks apply), except that
   the PR is ready, not a draft.
2. Poll `gh pr view <n> --json state,isDraft,headRefOid,mergeable,statusCheckRollup` every
   `checks_poll_s` (30 s) until the checks finish, at most `checks_timeout_s` (1 h).
3. Merge: `gh pr merge <n> --repo <repo> --<merge_method> --match-head-commit <voted commit>`.
   GitHub refuses the merge if the PR's head is not the commit D6 voted on. Never `--admin`,
   never `--auto`; the PR's branch is not deleted.
4. Read the PR back (`jev ship sync`'s fields); only a PR GitHub reports as merged ends the run
   `DONE` with "merged PR #N". A PR GitHub merged although `gh` reported an error or timed out
   counts as merged.

There is no second vote after the checks: the merge is pinned to the commit both votes
approved, and the checks are a yes/no gate.

### When it stops for you

Everything that is not clearly fine stops the run in `NEEDS_HUMAN` with the reason, and
`jev resume RUN` tries the step again. The step is safe to repeat: a pushed branch, an open PR
and a finished merge are found, not redone.

- a check failed (the reason names it), or the checks did not finish within `checks_timeout_s`;
- no checks appeared within `checks_grace_s` (120 s) and `require_checks = true` (the default;
  `false` merges PRs without CI);
- the PR's head moved (someone pushed to it), it is a draft, it conflicts with its base, or it
  was closed;
- `gh pr merge` failed and GitHub does not report the PR merged (branch protection, required
  reviews), or a merge queue still holds it;
- the push or `gh pr create` failed, as for `jev ship`;
- under consensus, Jev and Laya do not agree at D6: that stops before anything ships.

The shapes of `gh pr view --json` and `gh pr merge` are verified against a live repository
([verification.md](verification.md) section 8, 2026-10-15).

## KPIs

When a run is shipped, its KPIs are computed and stored as a snapshot (`run_kpis` in
`.jev/state.db`), so later changes (cleaned branches, deleted logs) do not move them:

| KPI | Meaning |
| --- | --- |
| total cost | the run's reported spend (Codex reports none) |
| wall-clock time | from the run's creation to its last recorded activity |
| tasks, attempts per task | coder attempts over all tasks, and their mean |
| first-pass rate | tasks DONE on their first attempt / tasks |
| retries, escalations, reassigns | D5 answers |
| fix rounds | fix tasks added after integration |
| quality findings | by severity, from the latest gate of each task and the integration; plus how many gates blocked along the way |
| tests | the integration tests at the last D6 (passed, failed, not run) |
| diff | `git diff --shortstat` start..result: files, additions, deletions |
| decisions | total, by Jev, rules fallbacks, fallback rate (fallbacks / decisions Jev was asked for), mean Jev confidence |
| free model share | attempts on free models / attempts on the api worker |
| lessons | distinct lessons injected, lessons added by the learner (summed over the run's learning steps, stored as data on the run) |

`jev ship sync RUN` (or `--all`) asks GitHub for each PR with
`gh pr view <number> --repo <repo> --json state,createdAt,mergedAt,closedAt,reviews,comments,commits,additions,deletions,changedFiles,statusCheckRollup,isDraft`
and stores:

- **state** (open, closed, merged) and **time to merge** (merged - opened);
- **review comments**: conversation comments plus reviews with a non-empty body (inline review
  comments are not in this JSON), and the number of reviews;
- **CI status**: `failure` if any check failed (or errored, timed out, was cancelled),
  `pending` if any is unfinished, `success` otherwise, `none` without checks;
- **commits after open**: the PR's commits minus the commits jev pushed (pushing to the branch
  or GitHub's "Update branch" both count);
- additions, deletions, changed files as GitHub counts them;
- **reverted** (merged PRs only): a merged PR whose body says `Reverts owner/repo#N` or whose
  title is `Revert "<title>"`, which is what GitHub's Revert button writes. It is found with
  `gh pr list --state merged --search '"owner/repo#N"'`; a revert written by hand without
  either marker is not detected.

Review and comment bodies are not stored, only their counts.

## `jev kpi`

```bash
jev ship sync --all     # refresh GitHub data first
jev kpi                 # the last 20 shipped PRs
jev kpi --last 10
jev kpi --json          # every PR with its run KPIs, medians, trends and regressions
```

![jev kpi --last 10](images/cli-kpi.svg)

One row per PR (cost, run time, attempts per task, first-pass rate, findings, fallback rate,
time to merge, CI), a median row, and the regression flags below.

## Benchmarks: trends and regressions

For each KPI (cost per PR, run time, attempts per task, first-pass rate, rules fallback rate,
mean Jev confidence, quality findings, time to merge, review comments) over the last N shipped
PRs (default 20, oldest first; PRs without a value are skipped):

- the **rolling median** of up to 5 values ending at each point;
- a point is a **regression** when it is worse than the median of the up to 5 values before
  it (at least 3 needed):
  - costs, times and counts: more than 1.5x that median when higher is worse (never against a
    median of 0);
  - rates and the confidence (0 to 1): more than 15 percentage points past it in the bad
    direction (first-pass rate and confidence: lower is worse; fallback rate: higher is worse).

`jev kpi` prints a line per KPI whose latest PR is a regression. The KPIs page draws one SVG
chart per KPI: the values as a solid line, the rolling median dashed, and regressions as red
points (hover for the numbers); summary cards show the median cost per PR, median time to
merge, first-pass rate, fallback rate and the share of PRs merged. Without shipped PRs it
explains how to start.
