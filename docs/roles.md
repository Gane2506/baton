# Roles

A role is a job in the pipeline: **planner**, **validator**, **coder** or **reviewer**, plus
the **learner**, which distills lessons at the end of a run when learning is on
([learning.md](learning.md)), and the **code reviewer**, the AI review of the quality gate
([quality.md](quality.md)). Each role is two things:

- a prompt template, `src/jev_agent/roles/<role>/prompt.md` (Jinja2), and
- an output format, defined in Python (`src/jev_agent/roles/schemas.py` and `src/jev_agent/core/models.py`).

You can replace any prompt with your own file without touching Python. The output format is
fixed: jev validates every reply against it.

Which worker, model and effort play a role is set per role in `[roles.<role>]` (`workers`,
`models`, `efforts`, `selection`) and chosen at run time by the dispatcher. See
[config.md](config.md#rolesrole).

## Workers and roles

| Worker | Planner | Validator | Coder | Reviewer | Code reviewer | Learner |
| --- | --- | --- | --- | --- | --- | --- |
| `claude_code` | yes (read-only) | yes (read-only) | yes | yes (read-only) | yes (read-only) | yes (read-only) |
| `codex` | yes (read-only) | yes (read-only) | yes | yes (read-only) | yes (read-only) | yes (read-only) |
| `api` | yes | yes | no | yes | yes | yes |

Read-only means the agent can read the repository but its write tools are disabled
([security.md](security.md)). The `api` worker sees only the prompt, not the repository, so its
plans rely on what the idea and prompt tell it. The coder must be a CLI worker: it edits files
in its own git worktree.

The default role tables put a different vendor on the validator than on the planner
(`prefer_different_vendor_than = "planner"`), so a plan is checked by a second opinion.

## Planner

**Purpose.** Turn the idea into a plan of small tasks, each done on its own branch.

**Template variables.**

| Variable | Type | Value |
| --- | --- | --- |
| `idea` | string | The idea text from `jev run`. |
| `repo_path` | string | Absolute path of the repository root. |
| `context_files` | list of strings | Files the user pointed at. Currently always empty: `jev run` has no option for it yet. |
| `has_tests` | bool | True if the repo has a `tests/` or `test/` directory. |
| `round` | int | Plan round being written, starting at 1. |
| `previous_plan` | string or none | The rejected plan as indented JSON when revising, else none. |
| `validation_issues` | list of strings | The validator's issues when revising, else empty. |
| `lessons` | list | Lessons from earlier runs for this role (see [below](#lessons-in-every-prompt)); empty when the run does not learn. |

**Output.** One JSON object (`Plan` in `core/models.py`):

| Field | Type | Rules |
| --- | --- | --- |
| `summary` | string | One paragraph on the overall approach. |
| `tasks` | list | At least one task. |
| `tasks[].id` | string | `T1`, `T2`, ... (must match `T` plus digits); unique. |
| `tasks[].title` | string | Short, imperative. |
| `tasks[].description` | string | Enough detail to work alone. |
| `tasks[].files_hint` | list of strings | Files or directories likely touched. |
| `tasks[].depends_on` | list of strings | Ids of tasks that must finish first. Must exist; no self-dependency. |
| `tasks[].acceptance_criteria` | list of strings | Testable statements. |
| `tasks[].test_command` | string or null | One shell command run from the repo root after the coder finishes. |
| `risks` | list of strings | Optional. |

A plan with a dependency cycle stops the run in `FAILED`. No valid plan at all stops it in
`NEEDS_HUMAN`.

## Validator

**Purpose.** Decide whether the plan is good enough to hand to coders.

**Template variables.**

| Variable | Type | Value |
| --- | --- | --- |
| `idea` | string | The idea text. |
| `plan_json` | string | The plan as indented JSON. |
| `has_tests` | bool | As for the planner. |
| `lessons` | list | As for the planner. |

**Output.** One JSON object (`ValidationResult`):

| Field | Type | Rules |
| --- | --- | --- |
| `verdict` | string | `"approve"` or `"revise"`. |
| `issues` | list of strings | Problems to fix; empty on approve. |

The dispatcher (D3) reads the verdict and decides to approve, revise or escalate to a human.
`revise` sends the plan and the issues back to the planner, up to `[budget] max_plan_rounds`.

## Coder

**Purpose.** Implement one task in its own git worktree. The coder must not commit or push;
jev commits its changes to the task branch.

**Template variables.**

| Variable | Type | Value |
| --- | --- | --- |
| `idea` | string | The idea text. |
| `plan_summary` | string | The plan's `summary`. |
| `task` | object | The task card: `task.id`, `task.title`, `task.description`, `task.files_hint`, `task.depends_on`, `task.acceptance_criteria`, `task.test_command`. |
| `attempt` | int | Attempt number for this task, starting at 1. |
| `feedback` | string or none | On attempt 2 and later: what went wrong last time (worker status and summary, the tail of failing test output, reviewer issues, quality gate findings). None on attempt 1. |
| `quality_findings` | list of strings | When the quality gate blocked the previous attempt: up to 10 compact findings, most severe first (`major security src/a.py:12: ... (bandit); fix: ...`). Always defined; empty otherwise. |
| `lessons` | list | As for the planner, chosen for this task. |

**Output.** Free-form work, then a final fenced block tagged `jev-summary` holding one JSON
object (`CoderSummary` in `roles/schemas.py`):

````
```jev-summary
{"summary": "Added word_frequencies() and tests.", "files_changed": ["textstats/core.py"]}
```
````

| Field | Type | Rules |
| --- | --- | --- |
| `summary` | string | Two or three sentences on what changed. |
| `files_changed` | list of strings | Optional. jev takes the real list from `git status`; this is only a fallback. |

If there are several blocks, the last one counts. A missing or invalid block makes the attempt
an `error`, even if the code is fine. Templates get the tag as `{{ summary_marker }}`; use it
rather than typing `jev-summary`.

After the coder, jev runs `task.test_command` in the worktree. A failure there counts as a
failed attempt and the dispatcher (D5) decides what happens next.

## Reviewer

**Purpose.** Check one task's diff against its acceptance criteria. On by default with the
consensus dispatcher, off with `jev` and `rules` (`[review] enabled` sets it either way). It
runs after the task's tests pass and the quality gate, if on, lets the attempt through, with
the worker, model and effort the dispatcher picks at D8 (see
[config.md](config.md#review)). The rules dispatcher picks the role's first worker and model
and its middle effort level.

**Template variables.**

| Variable | Type | Value |
| --- | --- | --- |
| `task` | object | The task card, as for the coder. |
| `diff` | string | The task's diff against its base, clipped to 20,000 characters. |
| `test_output` | string or none | Output of the task's test command, clipped to 4,000 characters; none if there was no test command. |
| `lessons` | list | As for the planner, chosen for this task. |

**Output.** One JSON object (`ReviewResult`), the same shape as the validator's:
`{"verdict": "approve" | "revise", "issues": [...]}`. A `revise` verdict counts as a failed
attempt, and the issues reach the next coder attempt through `feedback`.

## Code reviewer

**Purpose.** The AI review of the quality gate ([quality.md](quality.md)): a CodeRabbit-style
review of one task's diff, or of the whole run's diff at integration, when `[quality] level` is
not `none`. It reports findings; the gate's policy decides what blocks. It uses the role's
first worker and model and its middle effort, like the reviewer.

**Template variables.**

| Variable | Type | Value |
| --- | --- | --- |
| `level` | string | The run's quality level: `basic`, `medium`, `production` or `scalable`. |
| `task` | object or none | The task card (as for the coder); none at integration. |
| `idea` | string | The idea text. |
| `plan_summary` | string | The plan's `summary` (empty if there is none). |
| `diff` | string | The diff, clipped to 40,000 characters. |
| `checks` | list | The tool checks that already ran: `{name, status, detail}`. |
| `security` | bool | True from `production`: include the security checklist (injection, authn/authz, secrets, input validation, SSRF, path traversal, deserialization, error handling and logging of sensitive data, API/migration compatibility). |
| `scalability` | bool | True at `scalable`: include the scalability checklist and a threat model. |
| `lessons` | list | Always empty (lessons are not selected for this role). |

**Output.** One JSON object (`QualityReview` in `roles/schemas.py`):

| Field | Type | Rules |
| --- | --- | --- |
| `summary` | string | What the change does and its overall quality. |
| `walkthrough` | list | `{"file", "change"}` per changed file. |
| `findings` | list | `{"severity", "category", "title", "file", "line_start", "line_end", "suggestion"}`; severity `info`, `minor`, `major` or `critical`; category `bug`, `security`, `performance`, `maintainability`, `tests` or `docs`. |
| `security_checklist`, `scalability_checklist` | lists | `{"item", "status", "note"}`, status `ok`, `concern` or `n/a`. Empty below their level. |
| `threat_model` | list of strings | `scalable` only: "threat -> mitigation". |

An invalid reply is reported as an AI review `error` in the gate's report; it never blocks.

## Learner

**Purpose.** At the end of a run that learns, turn what happened into short lessons for future
runs, and keep the existing lessons accurate: add, update (merge) or retire them. It only reads;
it never edits the repository. It runs when D7 says `learn` (see
[learning.md](learning.md#when-jev-learns)), with the worker, model and effort D7 picked from
`[roles.learner]`.

**Template variables.**

| Variable | Type | Value |
| --- | --- | --- |
| `run_card` | string | Indented JSON: idea, outcome and reason, plan summary and rounds, plan issues, `signals` (revision, retry, escalate, reassign, human, fix_task, failed, needs_human, budget_exceeded), and per task its title, files, state, attempts, D5 actions and a short redacted failure excerpt. Never transcripts. |
| `profile` | string | The profile the lessons go to. |
| `max_adds` | int | Most new lessons kept (5). |
| `lessons` | list | Up to 30 of the profile's lessons most relevant to the run, oldest first, so it can refer to them by id. |

**Output.** One JSON object (`LearnerOutput` in `roles/schemas.py`):

| Field | Type | Rules |
| --- | --- | --- |
| `add` | list | New lessons: `{"text", "roles", "tags"}`. At most 5 are kept; text is redacted and clipped to 300 characters; unknown roles are dropped (none left: every role). |
| `update` | list | `{"id", "text"}`: new text for a lesson that was shown. At most 10. |
| `retire` | list of strings | Ids of shown lessons to retire. |

An invalid reply only skips the learning step; the run's outcome does not change.

## Lessons in every prompt

Every template, built-in or your own, gets `lessons`: a list (empty when the run does not
learn) of lessons with `id`, `text`, `roles`, `tags`, `score` and `uses`. The built-in planner,
validator, coder and reviewer templates show them only when there are any:

```jinja
{% if lessons %}## Lessons from earlier runs in this repo
{% for lesson in lessons %}- {{ lesson.text }}
{% endfor %}{% endif %}
```

A custom template does not have to use `lessons`; if it does not, lessons are simply not shown
to that role.

## How replies are checked

Planner, validator, reviewer and learner replies are validated against their schema. Where the worker
supports it, jev also passes the schema to the agent: `--json-schema` for Claude Code,
`--output-schema` for Codex, and a strict `json_schema` response format for the API worker. If
the reply does not validate, the attempt is an `error`. So a custom prompt must still ask for
exactly the JSON shape above.

## Overriding a prompt

Point `prompt_file` at your own template:

```toml
[roles.planner]
prompt_file = "prompts/planner.md"   # relative to the repo root; absolute paths work too
```

Only the prompt changes; the role's workers, models and output schema stay the same.

Start from a copy of the built-in template, `src/jev_agent/roles/<role>/prompt.md` in the jev
repository, and edit it. Commit it with your repo so every run uses the same prompt.

**Strict variables.** Templates are rendered with Jinja2's `StrictUndefined`. Using a variable
that jev does not pass for that role (a typo, or a variable from another role) is an error, not
empty text. You do not have to use every variable. Variables that do not apply are passed as
none or an empty list, so `{% if previous_plan %}` and `{% for issue in validation_issues %}`
are safe. To guard a variable that might not exist, use `{% if name is defined %}` or
`{{ name | default("") }}`.

**When a template is wrong.** `jev doctor` checks every `prompt_file`: a missing file or a
Jinja2 syntax error is a `fail` row naming the role and the file. It does not render templates,
so an undefined variable is only found when the role runs.

During a run, a missing or unreadable file, a syntax error, or an error while rendering (such
as `UndefinedError: 'nope' is undefined`) stops the run in `NEEDS_HUMAN`, with a reason that
names the role and the file, for example:

```
bad prompt template: coder prompt (prompt_file /repo/prompts/coder.md): UndefinedError:
'nope' is undefined; fix it, then run `jev resume <run_id>`
```

Missing files and syntax errors are caught for every role before each step starts, so no work
is spent. An undefined variable is caught when that role's prompt is rendered, before its worker
starts; a coder attempt is not counted. (A reviewer template fails after the coder's attempt
has run, so that attempt is run again on resume.) Fix the template, then `jev resume <run_id>`
continues from the step where the run stopped. This is the one `NEEDS_HUMAN` stop that
`resume` accepts; `jev status <run_id>` shows the reason.

## Tips for writing prompts

- **Keep the output section.** Copy the "Output" part of the built-in template verbatim. The
  schema does not change when the prompt does.
- **One role, one job.** The planner plans, the validator judges, the coder codes. Say what the
  agent must not do ("Do not modify any files", "commit nothing").
- **Make criteria testable.** The planner's acceptance criteria and test commands drive
  everything after it. Ask for criteria a command or reviewer can check.
- **Name your conventions.** Put repo-specific rules in the prompt: test layout, style, the
  test runner (for example "test commands use `uv run pytest`").
- **Split by files.** Ask the planner to split along file or module boundaries. Tasks run on
  separate branches and are merged at the end, so two tasks editing one file risk a conflict.
- **Keep it short.** Every word is sent on every call. Long context belongs in the repo (the
  agent can read it), not in the prompt.
- **Ask for specific issues.** Validators and reviewers should only ask for revisions on real
  problems, each one actionable. Vague issues produce vague revisions.
- **Use the feedback.** A custom coder prompt should include `{{ feedback }}` when it is set;
  otherwise retries repeat the same mistake.
- **Test with fakes first.** `JEV_FAKE_WORKERS=1` does not render your prompt through a real
  agent, but it does render the template, so it catches undefined variables and syntax errors
  without spending anything.
