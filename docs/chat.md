# Chat

`jev chat` lets you talk an idea through with a model before you spend a run on it. The model
acts as a product and tech lead: it asks clarifying questions, points out risks, can read the
repository (read-only), and writes a short, testable **brief** for jev's planner. From the chat
you then plan, run or queue that brief.

```text
$ jev chat
Learning for this chat and the runs it starts:
  1) With learning: default (4 lessons)
  2) Other learning profile...
  3) Without learning
Choose [1/2/3] (1): 1
chat c-20261012-153012-ab12 started: claude_code (model: default), budget $1.00
learning: profile default; 4 lesson(s) from default in the model's instructions
Describe your idea. /help lists the commands; /quit leaves.
you> add rate limiting to the public API
...
you> /brief
you> /plan
run 20261012-153412-9f3c: AWAITING_APPROVAL
  details: `jev status 20261012-153412-9f3c`
```

## Options

```bash
jev chat [--learning NAME | --no-learning] [--profile NAME] [--worker api|claude_code]
         [--model MODEL] [--budget-usd USD] [--resume CHAT_ID] [--list]
```

| Option | Does |
| --- | --- |
| `--learning NAME` / `--no-learning` | The learning choice (see below). Skips the start screen. |
| `--profile NAME` | Workspace profile for the runs the chat starts (see [Workspace profile](#workspace-profile)). |
| `--worker api\|claude_code` | Chat backend. Default: `api` when `[workers.api]` is enabled and its key (`OPENROUTER_API_KEY` by default) is set, else Claude Code. |
| `--model MODEL` | Model for the backend (see [Backends](#backends)). |
| `--budget-usd USD` | Stop model calls once the chat has spent this much. Default `1.00`; a resumed chat keeps its own unless you pass it again. |
| `--resume CHAT_ID` | Continue a saved chat. Flags given again override what it stored. |
| `--list` | List saved chats (newest first) and exit. |

There are no config keys for chat: everything is a flag, plus the existing `[workers.*]` and
`[roles.planner] models` settings.

## Learning

On a terminal, without `--learning` or `--no-learning`, the start screen asks:

1. **With learning: ACTIVE (N lessons)**: the repo's active profile (`jev learning use`),
   created if it does not exist yet.
2. **Other learning profile...**: pick any other profile (repo or shared).
3. **Without learning**.

Without a terminal (scripts, pipes) and without flags, the chat uses the **repo default**: the
same profile `jev run` without flags would use (see [learning.md](learning.md#precedence)).

The choice does two things:

- The chat model's instructions include the profile's lessons for the **planner** role,
  selected the same way a run's planner gets them (`[learning] max_lessons` / `max_chars`). The
  start line says how many.
- `/plan`, `/run` and `/queue` pass it on: a profile becomes `--learning NAME`, "without"
  becomes `--no-learning`, the repo default passes neither flag.

`/learning` shows the choice; `/learning on|off|default|NAME` changes it for the rest of the
chat (and is saved). `on` is the repo's active profile. With Claude Code, which keeps its own
session, the new lessons are sent along with your next message.

## Commands

| Command | Does |
| --- | --- |
| *(plain text)* | Goes to the model. Start a line with `//` to send text that begins with `/`. |
| `/brief` | The model writes the current idea brief (title, goal, scope, acceptance criteria, constraints, assumptions); it is shown and saved. |
| `/plan` | Starts a plan-only run from the brief, as `jev run --plan-only` would. Prints the run id and the `jev status` hint. |
| `/run` | Starts a full run from the brief. It still stops at plan approval when `[approval] require_plan_approval` is on (the default). |
| `/queue [high\|normal\|low]` | Adds the brief to the idea queue (default `normal`) with the chat's learning choice and workspace profile, instead of running it now. `jev scheduler start` runs the queue. |
| `/learning [on\|off\|default\|NAME]` | Show or change the learning choice. |
| `/quality [LEVEL\|default]` | Show or change the quality gate level of runs started (or queued) from this chat; `default` uses `[quality] level` ([quality.md](quality.md)). |
| `/status` | Chat id, backend and model, learning choice and lesson count, workspace profile, brief title, the runs launched (with their current state) and queue items. |
| `/cost` | What the chat has spent and its budget. |
| `/help` | The commands. |
| `/quit` | Leave (also `/exit`, Ctrl-D or Ctrl-C). The chat is saved. |

`/plan`, `/run` and `/queue` use the latest brief. If there is none, or you said more since it
was written, the model writes a fresh one first. The brief's text is the run's idea, so it is
what the planner sees and what `jev status` shows.

## Backends

| Backend | How | Model |
| --- | --- | --- |
| `api` | `POST {base_url}/chat/completions` from `[workers.api]` (OpenRouter by default), with the whole conversation each turn. No file access. | `--model`, else `[workers.api] default_model`, else the first `vendor/model` id in `[roles.planner] models`. |
| `claude_code` | `claude -p --output-format json`, continued with `--resume <session_id>`. Read-only: `--permission-mode dontAsk`, only `Read`, `Glob` and `Grep` allowed, file-writing tools and `Bash` disallowed; cwd is the repo. Each call gets `--max-budget-usd` set to what is left of the chat budget. | `--model`, else the first planner model without a `/`, else Claude Code's default. `[workers.claude_code] command`, `bare` and `timeout_s` apply. |

The instructions (`src/jev_agent/roles/chat/prompt.md`, Jinja with strict undefined variables)
include a short repo summary: the top-level tree with one level of children (at most 60
entries) and the first 2000 characters of the README. With Claude Code they are sent with the
first message of a session; if a resumed session is gone, a new one starts with the earlier
turns included. Switching backends on `--resume` works the same way.

With `JEV_FAKE_WORKERS=1` the chat uses a fake model that echoes your messages and writes a
fixed brief, so you can try the flow without any model.

## Workspace profile

`--profile NAME` is stored with the chat (`--profile none`: no profile). `/plan` and `/run`
start their runs with it, as `jev run --profile NAME` would: the run gets that profile's
servers (SSH at the profile's access level), browser, learning and quality
([servers.md](servers.md#workspace-profiles)). Without one, launched runs use the repo
default (`jev profile use`). `/queue` stores the profile on the queue item and the scheduler
starts the run with it. An unknown profile is refused (the run is not started, the item is
marked `failed`) instead of running with other access. The chat itself gets no server tools.

## Saved chats and cost

Each chat lives in `.jev/chats/<chat_id>/` (ids look like `c-20261012-153012-ab12`):

- `transcript.jsonl`: one JSON object per line (`at`, `kind`, `text`): your messages, the
  model's answers, briefs, errors and events (learning changes, runs started, queue items).
  Text is redacted (key patterns and the values of secret-looking environment variables)
  before it is written.
- `meta.json`: learning choice, workspace profile, backend, model, Claude Code session id,
  budget and spend, the latest brief, runs launched and queue items.
- `backend.log`: the raw requests and responses (redacted), for debugging.

`jev chat --resume ID` restores all of it; `jev chat --list` lists chats.

The chat's spend (reported by the backend; Claude Code's figure is its own estimate) is shown
by `/cost` and `/status` and is checked before every model call: once it reaches the budget,
the chat stops calling the model (commands still work). The last call can take the total
slightly over the budget. Chat spend counts toward no run's budget; the runs a chat starts have
their usual caps.
