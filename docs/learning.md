# Learning

jev can learn from its own runs. When learning is on, each run ends by distilling short
**lessons** ("Run `uv run pytest -q` from the repo root; tests import from `src/`"), and later
runs get the relevant lessons in their prompts. The learner sees the lessons it wrote before,
so it refines, merges and retires them over time: that is the "recursive" part.

Learning is **off by default**. It costs one extra model call at the end of a run (only when
the run had something to teach, see [When jev learns](#when-jev-learns)), and lesson text is
sent to your configured workers as part of their prompts.

```bash
jev learning enable            # turn it on for this repo (profile "default")
jev run "add rate limiting"    # ends with "learning: learned into default: 2 added, ..."
jev learning show              # the lessons, with ids, roles, scores and uses
```

## Profiles

A **profile** is a named store of lessons, for example `default`, `work` or `experiments`.
Each profile has a scope:

| Scope | Where | Use |
| --- | --- | --- |
| `repo` (default) | the repo's `.jev/state.db` | lessons about this repository |
| `shared` | `$XDG_DATA_HOME/jev/learning.db`, else `~/.local/share/jev/learning.db` | lessons you want in several repos |

Profile names are unique across both scopes. Each repo has one **active profile** (default
`default`, created on first use), and each profile can be enabled or disabled.

## Commands

| Command | Does |
| --- | --- |
| `jev learning list` | Whether learning is on for this repo (and why), the active profile, and every profile with its scope, on/off flag and lesson count. |
| `jev learning show [NAME] [--all]` | A profile's lessons (default: the active one): id, roles, tags, score, uses, text. `--all` includes retired lessons. |
| `jev learning create NAME [--shared]` | Create a profile, in the repo or the shared database. |
| `jev learning use NAME` | Make NAME the active profile for this repo. |
| `jev learning enable [NAME]` | Without NAME: turn learning on for this repo (and create/enable the active profile). With NAME: enable that profile. |
| `jev learning disable [NAME]` | Without NAME: turn learning off for this repo (lessons are kept). With NAME: disable that profile; runs only use it when named with `--learning`. |
| `jev learning flush [NAME] --yes` | Delete every lesson of a profile (default: the active one). Asks first without `--yes`. |
| `jev learning delete NAME --yes` | Delete a profile and its lessons. Asks first without `--yes`. |
| `jev learning forget LESSON_ID` | Retire one lesson (e.g. `L12`) so it is never used again. |

Per run: `jev run --learning NAME` uses profile NAME (even a disabled one), and
`jev run --no-learning` runs without lessons in or out. `jev resume` keeps the profile the run
started with; `--learning NAME` / `--no-learning` change it from then on. `jev status <run>`
shows the profile and what the learning step did; `jev log <run>` shows which lessons went into
each attempt (`lessons L3,L7`) and the D7 decision.

## Precedence

Which profile a new run uses, highest first:

1. `--no-learning` or `--learning NAME` on `jev run` (they cannot be combined; NAME must exist).
2. Settings stored by `jev learning enable|disable|use` in `.jev/state.db`.
3. `[learning] enabled` and `profile` in `jev.toml` (or your user config).
4. Defaults: off, profile `default`.

If learning is on but the active profile is disabled, the run does not learn. The profile is
recorded on the run, so `resume` stays consistent even if the settings change in between.

## How lessons are used

Every role prompt gets a `lessons` variable (an empty list when the run does not learn). The
built-in planner, validator, coder and reviewer templates show them in a short "Lessons from
earlier runs in this repo" section, only when there are any.

A lesson has: an id (`L12` in repo scope, `S3` in shared scope; an id is never handed out
again once its lesson was deleted, so an old run's `L12` never names a newer lesson), its text
(at most 300 characters), the roles it applies to (planner, validator, coder, reviewer), tags
(file paths, directories or keywords), the run and task it came from, when it was created, how
often it was used, a score, and a retired flag.

Selection is simple and deterministic, no embeddings:

1. Lessons for the role that are not retired.
2. Ranked by tag overlap with the context: the task card's `files_hint` (the path, its
   directories, file names and stems) and title, plus the idea's words. A multi-word tag such
   as `rate-limiting` matches when each word does. Untagged lessons apply everywhere.
3. Then by score (highest first), then newest first.
4. Capped at `[learning] max_lessons` (default 8) and `max_chars` (default 2000 characters of
   lesson text); a lesson that does not fit is skipped and smaller ones may still fit.
5. After ranking, the contradiction rule: the lower-scored side of a `contradicts` pair is
   dropped, but only when the pair has actually been injected together before — never on
   assertion alone (see "The lesson graph"). Suppressions are logged to the run's
   `learning.log`.

The planner's context is the idea; the validator's adds the plan's task titles and files; the
coder's and reviewer's add their task card.

## The lesson graph

Beyond the flat list, lessons form a queryable graph: full-text search over text and tags
(FTS5, no embeddings), edges between lessons, and module nodes. The storage lives in the
learning database (shared migration 4; repo databases gain it with their own migration):
`lesson_modules`, `lesson_edges`, the `lessons_fts` index (kept in sync by triggers), and a
`lesson_run_edges` view. Edge types:

- `derived-from` — the lesson came from this run. Not stored: it is the view over
  `lessons.source_run`.
- `applies-to` — the lesson is about this module (`src/auth/login.py`, `src/auth/`).
  Derived automatically from path-looking tags on `add_lesson`, or declared by the learner
  (`applies_to`), or linked by hand. For shared profiles the path is qualified with a repo
  slug derived from the repo directory name (`baton:src/auth/`); queries match with
  directory-prefix semantics (`--module src/auth` also finds `src/auth/login.py`).
- `supersedes` — a newer lesson replaces an older one. **Advisory only**: recording it
  never retires the old lesson; the old lesson stays active until a human retires it, and
  `graph show` displays the chain.
- `contradicts` — two lessons disagree. **Advisory only**: both stay active. Selection
  suppresses the lower-scored side of a pair, but only when the pair has actually been
  injected together before (evidence in `attempts.lesson_ids`) — never on assertion alone.
  Suppressions are logged to the run's `learning.log`. Ties keep the older lesson.
- `co-occurs-with` — lessons injected into the same prompt often. Computed on demand from
  `attempts.lesson_ids`, never stored.

The learner declares relations on each added draft (`supersedes`, `contradicts`,
`applies_to`); at most 8 edges are recorded per run. The learner prompt shows the existing
lessons' ids so it can name them.

```
jev learning graph query "flaky tests" --module src/api --from-runs failed --since 2026-09-15
jev learning graph show L12            # the lesson + its 1-hop neighborhood
jev learning graph link L31 supersedes L12
jev learning graph link L12 applies-to src/auth/
jev learning graph unlink L31 contradicts L12
jev learning graph stats
```

The dashboard's learning page has a Graph tab: a faceted browser (module and relation
facets, full-text search) over the same table, and a radial SVG neighborhood when a lesson
is focused. Editing stays in the CLI; the dashboard is read-only here.

## When jev learns

At the end of a run that is DONE, FAILED, NEEDS_HUMAN or BUDGET_EXCEEDED (never CANCELLED,
and not while waiting for approval), when the run has a profile:

1. **Scoring** (no model call). For every finished task, each lesson injected into its coder
   or reviewer attempts gets `uses + 1`, and `score + 1` if the task was DONE on its first
   attempt, or `score - 0.5` if it needed a retry, escalation or reassignment, or failed. A
   lesson whose score reaches -2 is retired automatically. Lessons that only went to the
   planner or validator are not scored. Only lessons of the run's own profile are touched (a
   lesson flushed or deleted during the run is simply skipped). Each task is scored once: the
   database that holds the lessons records the (run, task) pair in its `scored_tasks` table in
   the same transaction as the scores, so a crash before the run records it does not count a
   task twice on `jev resume`.
2. **D7** (a dispatcher decision): `learn` or `skip`, and the learner's worker, model and
   effort from `[roles.learner]`. The rules dispatcher learns when the run had any plan
   revision, retry, escalation, reassignment, human hand-off, fix task, failure or
   NEEDS_HUMAN stop, or when it was a clean DONE but the profile has fewer than 3 lessons;
   otherwise it skips. The step is skipped without a decision when the learner role has no
   usable worker or the run's `per_run_usd` budget is spent.
3. **The learner** (a read-only role, `roles/learner/prompt.md`) gets a compact run card (the
   idea, plan summary and rounds, the validator's issues, each task's outcome, attempts and
   D5 actions, short redacted failure excerpts, never transcripts) and up to 30 of the
   profile's most relevant lessons with their ids. It answers
   `{"add": [{"text", "roles", "tags"}], "update": [{"id", "text"}], "retire": [ids]}`.
4. **Applying** the answer: at most 5 adds and 10 updates; text is redacted, put on one line
   and clipped to 300 characters; unknown roles are dropped (none left means every role);
   repeats of an existing lesson are ignored. Updates and retirements only apply to lessons
   that were shown to the learner.

Problems in this step (no worker, budget spent, an invalid reply, an error) never change the
run's state; they are recorded as `skipped: ...` in the learning outcome. If a NEEDS_HUMAN run
is resumed, it learns again when it ends.

## Laya weights

Under the consensus dispatcher, Laya scores every allowed answer 1-5 on six weighted criteria
([`[dispatcher.laya]`](config.md#dispatcherlaya)). The framework ends with a feedback loop, and
`jev laya tune` closes it: it looks at how the work chosen by past consensus decisions turned
out and suggests new weights. It never changes them on its own.

| Command | Does |
| --- | --- |
| `jev laya tune [--profile NAME] [--min-outcomes N]` | Show the suggestion: per criterion, the chosen answers' average score in passed and in failed work, the current weight and the suggested one. |
| `jev laya tune --apply` | Save the suggestion on the profile (default: the active one). |
| `jev laya reset [--profile NAME]` | Remove the profile's tuned weights. |

**Outcomes** come from what `.jev/state.db` already holds; nothing extra is recorded during a
run. Each consensus decision stores Laya's final scores (`laya.<question>.scores` in the
decision, see `jev log`), and its outcome is:

- D4 (the coder's assignment): passed when the task's next D5 answer is `done`, failed when it
  is anything else. Known as soon as that D5 is made.
- Every other decision: known once the run ends in `DONE`, `FAILED` or `BUDGET_EXCEEDED`. A
  task's decision passed when the task is `DONE`; a run-level one when the run is `DONE`.
- D7 (whether to learn) comes after the outcome, so it is never counted. Cancelled runs, runs
  waiting for a person and runs still going are left out, and so are decisions made by `jev`
  or `rules` and decisions from before this feature (they have no stored scores).

Every question Laya scored is one sample: the chosen answer's six scores and whether its work
passed.

**The suggestion.** Per criterion, the gap is the average score of the chosen answers in passed
work minus the average in failed work. A criterion whose high scores went with passing work
gains weight: the weight is multiplied by e^(0.25 x gap) (one level of gap is about +28%),
kept within half of itself up or down per tune, and all six are rescaled to add up to 1 and
rounded to 3 decimals. A weight of 0 stays 0. With fewer than 5 passed or 5 failed samples
(`--min-outcomes`), there is no suggestion. Samples come from every run in this repo, whatever
profile they used.

**Which weights a run uses**, highest first:

1. `[dispatcher.laya] weights` in `jev.toml` or your user config. While they are set,
   `--apply` refuses, so a saved suggestion is never silently ignored.
2. The tuned weights on the run's learning profile. A run without a learning profile
   (`--no-learning`, or learning off) does not use them.
3. The defaults (the Laya CDA matrix).

Each decision records the weights it was made with (`weights` in the decision). Weights saved
on a profile that no longer validate (for example after a criterion is renamed) stop the run
for a person; `jev laya reset` clears them.

## What is stored, and privacy

- Lessons, profiles and the scores live only on your machine: in `.jev/state.db` (repo scope,
  ignored by git through `.jev/`) or the shared database file. Nothing is uploaded by jev.
- Upgrading: state.db schema version 9 and shared-database version 2 rebuild the `lessons`
  table so ids are never reused (ids, scores and text are kept). Ids deleted before the
  upgrade cannot be known, except repo-scope ids that recorded attempts still name; numbering
  continues after the highest of those. Schema version 10 stores what each run's learning
  steps changed as counts (`runs.learning_counts_json`: added, updated, retired), which the
  KPIs read; older runs get them once from their outcome line. Schema version 13 and
  shared-database version 3 add the tuned Laya weights to profiles
  (`learning_profiles.laya_weights_json`, empty until `jev laya tune --apply`).
- Lesson text and tags are redacted before they are stored (the same redaction as run logs:
  known key formats and the values of secret-looking environment variables).
- Lessons are sent to the workers you configured, as part of their prompts. So is the run card,
  to the learner. Use `jev learning show` to see exactly what will be sent, `forget` to drop a
  lesson and `flush` to start over.

## Cost

When learning is on, a run that has something to teach makes one extra model call at its end:
the learner, with a prompt of a few kilobytes. Its cost counts toward the run's budget like any
other attempt, and it is skipped when the run's budget is already spent. D7 itself costs a
Jev call (a fraction of a cent) or nothing with the rules dispatcher. Injected lessons add at
most `max_chars` characters to each prompt.

## For integrators

The functions a front end (such as a chat command) needs:

- `jev_agent.core.learning.resolve_learning(store, learning, config, flag_profile, no_learning)`:
  the profile a new run uses, or None.
- `jev_agent.cli.run_cmds.start_run(ctx, idea, plan_only=..., learning=..., no_learning=...)`: the
  body of `jev run` (resolve, then start), with `ctx` from `jev_agent.cli.wiring.cli_context(repo)`.
- `jev_agent.core.learning.Learning`: profiles and lessons (`profiles`, `create`, `lessons`,
  `flush`, `forget`, ...); `learning_status`, `set_repo_learning`, `set_active_profile` for the
  repo settings.
