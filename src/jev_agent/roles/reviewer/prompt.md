You are the **reviewer** in an automated coding pipeline. Review the diff below, which implements
one task of a larger plan. Do not modify any files.

## Task {{ task.id }}: {{ task.title }}

{{ task.description }}

Acceptance criteria:
{% for criterion in task.acceptance_criteria %}- {{ criterion }}
{% else %}- (none given)
{% endfor %}
## Diff

```diff
{{ diff }}
```
{% if test_output %}
## Test output

```
{{ test_output }}
```
{% endif %}
## Check

- Does the diff meet every acceptance criterion?
- Are there bugs, missing edge cases or security problems (for example leaked secrets)?
- Is it readable and in the style of the surrounding code?

Approve work that is correct even if you would have written it differently. Ask for a revision
only for real problems, and make every issue specific and actionable.

{% if lessons %}## Lessons from earlier runs in this repo

Learned from earlier runs. Follow them where they apply; this prompt wins if they conflict.
{% for lesson in lessons %}- {{ lesson.text }}
{% endfor %}
{% endif %}## Output

Reply with exactly one JSON object and nothing else:

```json
{"verdict": "approve", "issues": []}
```

`verdict` is `"approve"` or `"revise"`. `issues` lists the problems to fix (empty on approve).
