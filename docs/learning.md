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

The planner's context is the idea; the validator's adds the plan's task titles and files; the
coder's and reviewer's add their task card.

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

## What is stored, and privacy

- Lessons, profiles and the scores live only on your machine: in `.jev/state.db` (repo scope,
  ignored by git through `.jev/`) or the shared database file. Nothing is uploaded by jev.
- Upgrading: state.db schema version 9 and shared-database version 2 rebuild the `lessons`
  table so ids are never reused (ids, scores and text are kept). Ids deleted before the
  upgrade cannot be known, except repo-scope ids that recorded attempts still name; numbering
  continues after the highest of those.
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
