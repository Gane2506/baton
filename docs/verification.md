# Verification of external interfaces (PLAN.md section 16)

Checked on 2026-10-12 against current official docs and the locally installed CLIs
(Claude Code 2.1.114, codex-cli 0.162.1). Each claim has a source. Anything marked
**UNVERIFIED** could not be confirmed and should not be relied on without a live test.
No model calls, logins, or paid requests were made.

---

## 1. Jev (TypeSafe)

### Findings

**Availability on OpenRouter and slugs**
- Jev is on OpenRouter. Slugs that exist:
  - `typesafe/jev-1.13`, the versioned model. Served endpoint `typesafe/jev-1.13-20260917`, provider TypeSafe, modality `text->decisions`.
    Source: `GET https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints`
  - `~typesafe/jev-latest`, an alias that "always redirects to the latest model in the Jev family", modality `text->decisions`.
    Source: `GET https://openrouter.ai/api/v1/models/~typesafe/jev-latest/endpoints`
  - `typesafe/jev-router`, a separate product: a chat-completions model router. It is not the decision model.
    Source: `GET https://openrouter.ai/api/v1/models` and https://openrouter.ai/docs/guides/routing/routers/jev-router.md
- `typesafe/jev-1.13` and `~typesafe/jev-latest` do **not** appear in the chat `GET /api/v1/models` listing. Only `typesafe/jev-router` does. They are decision models, not chat models.
- Pricing on OpenRouter: prompt `0.000000042` USD/token ($0.042 per Mtok). Completion is `0`, so output tokens are free.
  Source: same endpoints call; https://openrouter.ai/docs/guides/community/jev.md
- Context on OpenRouter: `context_length: 64000` and `max_prompt_tokens: 32000` (endpoints call). The OpenRouter Jev hub says "Context length is 32,000 tokens".
- Native pricing and limits for Jev 1.13 (`jev-1.13.0`):
  - $0.042 per Mtok, charged on input only.
  - 64k tokens per request; 32k for `state` plus the longest question.
  - Rate limits of 100K tokens/s and 80 req/s, which the docs say are "adjusting dynamically".
  - Text-only input.
  Source: https://docs.typesafe.ai/models.md
- Native aliases are `jev-latest` and `jev-preview`, which both currently point to `jev-1.13.0`. The response `model` field reports the versioned ID that answered. Source: https://docs.typesafe.ai/models.md

**Native System One API** (source: https://docs.typesafe.ai/api.md)
- Endpoint and headers: `POST https://api.typesafe.ai/v1/systemone`, with `Authorization: Bearer <API_KEY>` and `Content-Type: application/json`.
- Request body:
  - `state` (required): string, object, or array.
  - `model` (required): for example `"jev-latest"`.
  - `questions` (required): a map from an id you choose to a Question. The key is not sent to the model.
- Question types. All take `type` and `instructions` (string, object, or array):
  - `noul`: yes/no. Optional `criteria: {"true": ..., "false": ...}`. Answer: `{type, noul: 0..1}`.
  - `choice`: `criteria` is required. It is a **map of option -> description** (description may be `null`), with a maximum of 255 options. Answer: `{type, choice, probabilities: {option: p}, confidence}`.
  - `score`: `criteria` is an **ordered array** of level descriptions (2 to 10 levels). Answer: `{type, score (float, can fall between levels), legend: {"0": desc}, probabilities: {"0": p}, confidence}`.
- How allowed answers are constrained: the keys of a Choice's `criteria` map *are* the allowed answers. `choice` is always one of those keys.
- Response: `{model, answers: {id: Answer}, usage: {input_tokens, output_tokens}}`. The native response has **no cost field**; compute cost as `input_tokens * price`.
- Errors are standard HTTP statuses with a JSON body:
  - `401`: bad or missing key.
  - `422`: validation failure; the body names the field.
  - `429`: rate limited.
  - `529`: overloaded.
  The docs say to retry 429 and 529 with exponential backoff, and that the SDKs honor `retry-after`. The exact JSON error body schema is **UNVERIFIED**; the docs only say it "details the offending field".
- A local reference client script (`jev_route.py`) matches the docs:
  - It sends POST `/v1/systemone` with a Bearer key from `TYPESAFE_API_KEY`, `model: "jev-latest"`, one `choice` whose `criteria` is a dict of agent name to description string (9 entries), and one `noul` without criteria.
  - It reads `answers[x].choice`, `.confidence`, `.probabilities` and `answers[y].noul`, plus `usage.input_tokens` and `usage.output_tokens`.
  - Its 60 s timeout is generous. The SDK default is 10 s (https://docs.typesafe.ai/sdk/python/api/constants.md).

**SDKs and environment variables**
- Python: `pip install typesafe-sdk`, imported as `typesafe_sdk`. The latest version on PyPI is 0.7.4 (from `https://pypi.org/pypi/typesafe-sdk/json`).
  - Provides `TypeSafeClient` and `AsyncTypeSafeClient`, plus the `Choice`, `Noul`, and `Score` types.
  - Typed accessors: `response.choices[...]`, `.nouls[...]`, `.scores[...]`.
  - Source: https://docs.typesafe.ai/sdk/python.md
- Environment variables: `TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL` (default `https://api.typesafe.ai`), `TYPESAFE_DEFAULT_MODEL` (default `jev-latest`), and `TYPESAFE_LOG_LEVEL`. Source: https://docs.typesafe.ai/sdk/python/api/constants.md
- Python exceptions: `TypeSafeAPIError` carries `.status`, `.body`, `.headers`, and `.request_id` (from the `x-typesafe-request-id` header). Subclasses are BadRequest (400), Authentication (401), PermissionDenied, RateLimit, and others. Source: https://docs.typesafe.ai/sdk/python/api/exceptions.md
- JavaScript: `npm install @typesafe-ai/sdk` (Node 20+), `new TypeSafeClient().systemOne({...})`. Source: https://docs.typesafe.ai/sdk/javascript.md
- Listing models: `GET /v1/models` lists the aliases. Source: https://docs.typesafe.ai/models.md

**Jev through OpenRouter.** It does *not* use chat completions. There are two surfaces (source: https://openrouter.ai/docs/guides/community/jev.md):
- **System One API**: `POST https://openrouter.ai/api/v1/systemone`.
  - Implements TypeSafe's request and response shape exactly, using an OpenRouter key as the Bearer token.
  - Bare IDs are mapped: `jev-1.13` becomes `typesafe/jev-1.13` and `jev-latest` becomes `~typesafe/jev-latest`. Prefixed IDs are used as-is.
  - The response adds `id`, `provider`, and **`usage.cost`**. Example: `{"id":"gen-dec-...","model":"typesafe/jev-1.13-20260917","provider":"TypeSafe","answers":{...},"usage":{"input_tokens":275,"output_tokens":20,"cost":0.00003}}`.
  - The TypeSafe SDK works by setting `base_url="https://openrouter.ai/api"`.
  - `client.models.list()` does **not** work against OpenRouter, because the response shape differs.
  - Source: https://openrouter.ai/docs/guides/community/typesafe-sdk.md (also documented on the TypeSafe side: https://docs.typesafe.ai/sdk/python/usage.md, "Configuring the base URL")
- **Decisions API (alpha)**: `POST https://openrouter.ai/api/alpha/decisions`.
  - Same `model`, `state`, `questions` body, plus OpenRouter extras: `provider`, `session_id`, `trace`, `user`.
  - Errors: 400/401/402/403/404/413/429/500/502/503/524/529.
  - Source: https://openrouter.ai/docs/client-sdks/python/sdks/decisions/README.md
- **Jev Router** (`typesafe/jev-router`) is a different thing.
  - It works on chat completions, Responses, and Messages, and picks the model and reasoning effort itself.
  - Optional plugin `{"id":"jev-router","cost_tier":"low|medium|high","models":[...],"excluded_models":[...]}`.
  - The response `model` field reports the model that actually served the request.
  - Pricing is listed as `-1`, meaning it varies with the routed model.
  - Source: https://openrouter.ai/docs/guides/routing/routers/jev-router.md

### Differences from PLAN.md
- **Section 5 assumed "calls Jev through OpenRouter" without naming the surface.** It is *not* chat completions. Use `/api/v1/systemone` (or `/api/alpha/decisions`) with typed `questions`.
- **Slug `typesafe/jev-latest` (sections 8 and 16) is wrong.** The alias is `~typesafe/jev-latest` (tilde) on OpenRouter, or bare `jev-latest`, which OpenRouter's System One API maps itself. The pinned slug is `typesafe/jev-1.13`.
- **Allowed-answer lists in section 5 are bare lists** (for example `complexity: [trivial, standard, hard]`). Jev Choice `criteria` must be a **map**, `{option: description|null}`. An ordered scale like complexity or effort could instead be a `score` question, with `criteria` as an array.
- **Model pools must fit within 255 Choice options.** A Score is limited to 10 levels.
- **`typesafe/jev-router` cannot be a dispatcher.** It is a black-box router that doesn't return typed decisions. It could be offered as an *ApiWorker model* (`model="typesafe/jev-router"`) where you don't care which model answers.
- **Context limit**: the budget is 32k for `state` plus the longest question, not something larger. `state` must stay compact (task card, quota, spend), not whole plans or diffs.

### Recommendation for implementation
- **Use the System One request shape for `JevDispatcher`.** Make the base URL configurable and default to OpenRouter:
  - `base_url = "https://openrouter.ai/api"`, endpoint `POST {base_url}/v1/systemone`, `model = "jev-latest"` (or pin `"jev-1.13"`), key from `OPENROUTER_API_KEY`.
  - Allow `base_url = "https://api.typesafe.ai"` with `TYPESAFE_API_KEY` as an alternative. The same code path works because the shapes are identical. This lets the existing local TypeSafe key work with no OpenRouter account.
  - Why OpenRouter as the default: one key and one bill shared with `ApiWorker`, plus `usage.cost` in each response for budget tracking. Native TypeSafe requires computing cost from tokens.
- **Call it with raw `httpx` rather than `typesafe-sdk`.**
  - Reasons: one fewer dependency, easy to mock in tests, and OpenRouter's extra fields are harmless.
  - Adding `typesafe-sdk` is also fine; it gives retries with `retry-after` support for free. Pin it if you use it (0.7.4).
- **Question mapping for D4:**
  - `worker`: `choice` with `{claude_code: "...", codex: "...", api: "..."}`.
  - `model`: `choice` with `{slug: description-from-config}`.
  - `complexity` and `effort`: `score` with ordered levels, then round `score` in code. A `choice` with descriptions also works.
  - Add a `noul` for "needs human" or "should escalate".
- **Gate on `confidence`.** If confidence is below a configured threshold (for example 0.55, as in the local script), fall back to `RulesDispatcher` for that decision. Validate that `choice` is one of the keys you sent before trusting it.
- **Errors:**
  - Retry 429, 529, and 5xx with backoff, honoring `Retry-After`.
  - On 401, 402, 403, or 422, fail over to `RulesDispatcher` immediately and log the body.
- **Logging:** record `model` (the versioned ID), `usage`, and `cost` (OpenRouter) per decision in the audit log.
- Recommended config keys:
  ```toml
  [dispatcher]
  jev_base_url = "https://openrouter.ai/api"
  jev_model = "jev-latest"
  jev_api_key_env = "OPENROUTER_API_KEY"
  min_confidence = 0.55
  ```

---

## 2. Claude Code headless

### Findings
- **Non-interactive mode:** `-p/--print`.
  - The workspace trust dialog is skipped in `-p`. Source: `claude --help` (2.1.114)
  - Without `--bare`, `-p` loads project hooks, `.mcp.json`, CLAUDE.md, and similar configuration. Source: https://code.claude.com/docs/en/headless.md
- **`--bare`** skips hooks, skills, plugins, MCP, auto-memory, and CLAUDE.md discovery.
  - It authenticates **only** with `ANTHROPIC_API_KEY` or an `apiKeyHelper`; it never reads OAuth or keychain.
  - The docs call it "the recommended mode for scripted and SDK calls" and say it "will become the default for `-p` in a future release".
  - Sources: `claude --help`; https://code.claude.com/docs/en/headless.md
- **Output formats:** `--output-format text|json|stream-json`. Source: `claude --help`
  - `json` emits a single result object.
  - `stream-json` emits JSONL events. The docs' examples pair it with `--verbose`.
  - `--include-partial-messages` requires `stream-json`. Source: https://code.claude.com/docs/en/cli-reference.md
- **Result fields** (`type: "result"`; source: https://code.claude.com/docs/en/agent-sdk/typescript.md, `SDKResultMessage`):
  - `subtype`: one of `success`, `error_max_turns`, `error_during_execution`, `error_max_budget_usd`, `error_max_structured_output_retries`.
  - Common fields: `is_error`, `session_id`, `duration_ms`, `duration_api_ms`, `num_turns`, `stop_reason`, `total_cost_usd`, `usage`, `modelUsage` (per-model breakdown), and `permission_denials`.
  - Present on `success` only: `result` (text), `structured_output`, and `api_error_status`.
  - `total_cost_usd` is a **client-side estimate**. Source: headless.md
- **Model and effort:** `--model <alias|full name>` (for example `sonnet`, `opus`, `claude-sonnet-4-6`) and `--effort low|medium|high|xhigh|max`. `--fallback-model` works only with `--print`. Source: `claude --help`
- **Permissions:** `--permission-mode` accepts `acceptEdits`, `auto`, `bypassPermissions`, `default`, `dontAsk`, and `plan`.
  - `--allowedTools` / `--disallowedTools` take patterns such as `"Bash(git *) Edit"`.
  - `--dangerously-skip-permissions` is the same as `--permission-mode bypassPermissions`.
  - `--tools` restricts the built-in tool set.
  - Sources: `claude --help`; https://code.claude.com/docs/en/cli-reference.md
  - The docs warn that if no mode is set, the session may start in `auto`, "so pass the one you want".
  - `dontAsk` denies anything not pre-allowed, which suits locked-down CI.
  - Newer docs add `--permission-prompts none` for unattended runs. That flag is **not in local 2.1.114 help**.
  - Source: https://code.claude.com/docs/en/headless.md
- **Working directory:** there is no `--cd` flag. Claude Code uses the process cwd, and `--add-dir` grants extra directories.
  - Source: `claude --help`
  - If the cwd is deleted mid-run, shell commands fail, and a warning is emitted in stream-json. Source: headless.md
  - Also available: `-w/--worktree [name]`, which creates a git worktree.
- **Limits:** `--max-turns N` (print mode only; exits with an error at the limit) and `--max-budget-usd <amount>` (print only).
  - `--max-turns` is documented in https://code.claude.com/docs/en/cli-reference.md but **not shown in local `claude --help`** (2.1.114). It may be hidden or newer. **UNVERIFIED locally.**
  - `--max-budget-usd` appears in local help.
- **Structured output:** `--json-schema '<schema>'` with `--output-format json`. The parsed value goes in `structured_output`.
  - An invalid schema exits with `Error: --json-schema is not a valid JSON Schema` (as of v2.1.205 or later; earlier versions silently ignored it).
  - If retries fail, `subtype` is `error_max_structured_output_retries`.
  - Sources: headless.md; `claude --help`
- **Rate and usage limits:**
  - stream-json emits a `system/api_retry` event with `error` set to one of: `rate_limit`, `overloaded`, `billing_error`, `authentication_failed`, `server_error`, and others. Source: headless.md
  - `rate_limit_event` carries `rate_limit_info.status` (`allowed`, `allowed_warning`, or `rejected`) and `resetsAt`. `errorCode: "credits_required"` means the subscription's included usage is exhausted. Source: agent-sdk/typescript.md
  - `rate_limit` means a 429 against your quota; `overloaded` means a 529 and does not count against quota. Source: agent-sdk/typescript.md
  - User-facing strings include `You've hit your session limit`, `You've hit your weekly limit`, `Request rejected (429)`, and `Credit balance is too low`. Source: https://code.claude.com/docs/en/errors.md
  - `CLAUDE_CODE_RETRY_WATCHDOG=1` retries 429 and 529 indefinitely. Source: errors.md
- **Version note:** the online docs reference behavior up to v2.1.286; the local CLI is 2.1.114. Feature-detect with `claude --help`, or require a minimum version in `baton doctor`.

Sample `--output-format json` result (shape from `SDKResultMessage`; values illustrative):
```json
{"type":"result","subtype":"success","is_error":false,"session_id":"3f1c...","num_turns":6,
 "duration_ms":48210,"duration_api_ms":40122,"stop_reason":"end_turn",
 "result":"Implemented rate limiting ...\n\nSUMMARY: ...","structured_output":null,
 "total_cost_usd":0.4123,"usage":{"input_tokens":1234,"output_tokens":2345,
 "cache_read_input_tokens":45678,"cache_creation_input_tokens":3456},
 "modelUsage":{"claude-sonnet-4-6":{"...":"..."}},"permission_denials":[]}
```

### Differences from PLAN.md
- The plan says to "parse structured output where the role requires JSON". Claude Code has native `--json-schema` that returns `structured_output`, so prompt-and-extract is not needed for planner, validator, and reviewer.
- Cost is reported directly as `total_cost_usd`, but it is an estimate. On subscription auth it is not real spend.
- Quota detection can be precise, not just "obvious error messages": use `api_retry.error`, `rate_limit_event`, and result `subtype`/`is_error`.

### Recommendation for implementation
- **Coder command:** `claude -p <prompt> --output-format json --model M --effort E --permission-mode acceptEdits --allowedTools "Bash(<test cmd>*) Read Edit Write" --max-turns N --max-budget-usd X`.
  - Run it with `cwd=worktree`.
  - Consider `--bare` plus `ANTHROPIC_API_KEY` for reproducibility.
  - Use `--permission-mode dontAsk` for read-only roles.
- **For roles with schemas:** pass `--json-schema` built from the pydantic model (`Model.model_json_schema()`) and read `structured_output`.
- **Status mapping:**
  - `subtype=="success"` and `!is_error`: success.
  - `error_max_turns` or `error_max_budget_usd`: budget.
  - Rate-limit or credits signals: `quota_exhausted`.
  - Otherwise: error.
- **Streaming:** use `stream-json --verbose` if you need live logs and quota events. Parse the final `type:"result"` line.
- **Unattended runs:** set an explicit permission mode always. Never rely on the default.

---

## 3. Codex CLI headless

### Findings (`codex exec --help`, codex-cli 0.162.1, unless noted)
- **Prompt input:** `codex exec [PROMPT]`. Use `-` or piped stdin for the prompt; when both are given, stdin is appended as a `<stdin>` block.
- **Output:**
  - Progress streams to stderr; only the final message goes to stdout. Source: https://developers.openai.com/codex/noninteractive.md
  - `--json`: JSONL events on stdout.
  - `-o/--output-last-message <FILE>`: writes the final message to a file and also prints it.
  - `--output-schema <FILE>`: a JSON Schema for the final response.
- **Model and effort:** `-m/--model <MODEL>`. Reasoning effort is a config key, `-c model_reasoning_effort="high"`, with values `low`, `medium`, `high`, `xhigh`, `max`, or `ultra`, depending on the model. Source: https://developers.openai.com/codex/config-reference.md
- **Sandbox:** `-s/--sandbox read-only|workspace-write|danger-full-access`. The default for `exec` is read-only. Source: noninteractive.md
- **Approvals:**
  - `--ask-for-approval` is **not an `exec` flag** in 0.162.1. It exists only on top-level `codex` (`-a on-request|never`).
  - For exec, use `-c approval_policy="never"`. Per config-reference.md, `untrusted` is unsupported and `on-failure` is deprecated.
  - `--full-auto` is **absent from 0.162.1 `exec --help`**. The docs call it a "deprecated compatibility flag" that prints a warning, and say to use `--sandbox workspace-write` instead (noninteractive.md).
  - New flags: `--approve-for-me` (automatic review with the workspace-write sandbox) and `--dangerously-bypass-approvals-and-sandbox`.
- **Directories and git:** `-C/--cd <DIR>`, `--add-dir <DIR>`, `--worktree`, and `--skip-git-repo-check` (git is required by default; noninteractive.md).
- **Other flags:** `--ephemeral` (no session files), `--ignore-user-config`, `--ignore-rules`, `--strict-config`, `-p/--profile`, and `--oss`/`--local-provider`.
- **Auth:**
  - Uses saved CLI auth by default.
  - For automation, set `CODEX_API_KEY` inline for the invocation. The docs say "API keys are the right default for automation". Source: noninteractive.md
- **JSONL events** (sources: noninteractive.md; https://raw.githubusercontent.com/openai/codex/main/sdk/typescript/src/events.ts):
  - Event types: `thread.started {thread_id}`, `turn.started`, `item.started`, `item.updated`, `item.completed {item}`, `turn.completed {usage}`, `turn.failed {error:{message}}`, and `error {message}`.
  - Item types: `agent_message` (with `text`), `reasoning`, `command_execution`, `file_change`, `mcp_tool_call`, `web_search`, and plan updates.
  - The `usage` fields are `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, and `reasoning_output_tokens`.
  - **No cost field** is emitted; compute cost from tokens if needed.
- **Custom providers:** `[model_providers.<id>]` with `name`, `base_url`, `env_key`, `wire_api`, and `query_params`, selected via `model_provider`.
  - `wire_api` supports **only `"responses"`** (the default), so a custom provider must expose an OpenAI Responses-compatible API.
  - Source: config-reference.md
- **Usage-limit errors:** source strings in `codex-rs/protocol/src/error.rs` (`UsageLimitReachedError`) include:
  - `You’ve hit your usage limit. …` (note the curly apostrophe)
  - `You’ve hit your usage limit for {limit_name}. …`
  - `Your workspace is out of credits…`
  - `You hit your spend cap…`
  - Source: https://raw.githubusercontent.com/openai/codex/main/codex-rs/protocol/src/error.rs
  - These reach `turn.failed.error.message` or the `error` event. That mapping is inferred, not documented: **UNVERIFIED**.
  - Exit codes are **UNVERIFIED**.

Sample event sequence (from noninteractive.md, plus a failure variant per events.ts):
```jsonl
{"type":"thread.started","thread_id":"0199a213-81c0-7800-8aa1-bbab2a035a53"}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"bash -lc ls","status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"Repo contains docs, sdk, and examples directories."}}
{"type":"turn.completed","usage":{"input_tokens":24763,"cached_input_tokens":24448,"output_tokens":122,"reasoning_output_tokens":0}}
// failure: {"type":"turn.failed","error":{"message":"You’ve hit your usage limit. ..."}}
```

### Differences from PLAN.md
- `codex exec` is confirmed as the right command. However:
  - `--full-auto` and `--ask-for-approval` should **not** be used with `exec` in 0.162.1.
  - Use `--sandbox workspace-write` plus `-c approval_policy="never"`.
- Effort is set through `-c model_reasoning_effort=...`, not a dedicated flag.
- Custom providers must use the Responses wire API. OpenRouter does offer a Responses API (https://openrouter.ai/docs/api_reference/responses/overview.md), but whether Codex works end-to-end via OpenRouter is **UNVERIFIED**.
- Codex reports no cost, only tokens.

### Recommendation for implementation
- **Coder command:** `codex exec --json -C <worktree> -s workspace-write -c approval_policy="never" -m <model> -c model_reasoning_effort="<effort>" --ephemeral -o <logdir>/last.txt -- "<prompt>"`.
  - For read-only roles, use `-s read-only`.
  - For roles with schemas, add `--output-schema schema.json` and parse the `-o` file as JSON.
- **Parsing:** read JSONL.
  - Final text is the last `item.completed` with `item.type=="agent_message"`, or the `-o` file.
  - Sum `turn.completed.usage`.
  - Map `turn.failed` or `error` to status. Classify as `quota_exhausted` when the message matches `/hit your usage limit|out of credits|spend cap/i`, normalizing curly quotes first.
- **Version checks:** have `baton doctor` check `codex --version`. Flags have churned (`--full-auto` was removed from help), so feature-detect via `codex exec --help`.

---

## 4. OpenRouter chat completions

### Findings
- **Structured output:**
  - Request with `response_format: {type: "json_schema", json_schema: {name, strict: true, schema}}`.
  - Support is **per endpoint (provider)**, not per model. Find supported models via the models page filter `supported_parameters=structured_outputs`, or the `supported_parameters` array in `GET /api/v1/models`.
  - To force routing only to supporting providers, set `provider: {require_parameters: true}`.
  - The optional Response Healing plugin repairs malformed JSON (non-streaming only).
  - Source: https://openrouter.ai/docs/guides/features/structured-outputs.md
- **Usage and cost:** every response includes `usage`, with no opt-in needed. `usage: {include: true}` and `stream_options.include_usage` are **deprecated and have no effect**. Source: https://openrouter.ai/docs/cookbook/administration/usage-accounting.md
  - Fields: `prompt_tokens`, `completion_tokens`, `total_tokens`, details, `cost` (credits ≈ USD), `is_byok`, and `cost_details.upstream_inference_cost`. Source: https://openrouter.ai/docs/api_reference/overview.md
  - When streaming, usage arrives in the final chunk before `[DONE]`. Source: overview.md
- **Generation stats:** `GET https://openrouter.ai/api/v1/generation?id=<id>` returns token counts and cost after completion. Source: overview.md
- **Errors:**
  - The body shape is `{"error": {"code": <int>, "message": "...", "metadata": {...}}}`.
  - `402`: insufficient credits. This is not a wait-and-retry case, unless `error.metadata.limit_source == "openrouter_in_flight_budget"` and a `Retry-After` header is present.
  - `429`: rate limited, possibly with `Retry-After`.
  - `503`: no provider meets the routing requirements.
  - `403`: guardrail or budget block, where `metadata.limit_source` is one of `openrouter_guardrail_budget`, `openrouter_workspace_budget`, or `openrouter_key_limit`.
  - Also: 400, 401, 408, and 502.
  - Source: https://openrouter.ai/docs/api_reference/errors-and-debugging.md
- **Headers:**
  - Required: `Authorization: Bearer <key>`.
  - Optional: `HTTP-Referer` (app URL), `X-OpenRouter-Title` (`X-Title` is still accepted), and `X-OpenRouter-Categories`.
  - Source: overview.md

### Differences from PLAN.md
- No plan changes are needed. One addition: PLAN.md should not add `usage: {include: true}`, since it is deprecated and a no-op.
- `structured_outputs` support must be checked per model or provider at config time, not assumed.

### Recommendation for implementation
- **ApiWorker:**
  - Send `response_format` json_schema built from the pydantic model, with `provider.require_parameters=true` when a schema is used.
  - On a 400 for an unsupported parameter, fall back to the prompt-and-extract path.
  - Read `usage.cost` for budget tracking.
  - Send `HTTP-Referer` and `X-OpenRouter-Title: baton`.
- **Error mapping:**
  - 402 or 403 with a `limit_source`: `quota_exhausted` / budget.
  - 429 or 503: retry honoring `Retry-After`, then `quota_exhausted`.
- **Doctor check:** `baton doctor` can call `GET /api/v1/models` (free) to confirm each configured model slug exists and lists `structured_outputs`.

---

## 5. Terms of use (automated use of the CLIs)

### Findings
- **Claude Code.** Source: https://code.claude.com/docs/en/legal-and-compliance.md ("Authentication and credential use")
  - OAuth (subscription) authentication "is intended exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise subscription plans and is designed to support ordinary use of Claude Code".
  - Developers building products, including with the Agent SDK, "should use API key authentication".
  - Third parties may not "route requests through Free, Pro, or Max plan credentials on behalf of their users". An end user signing in to the unmodified binary with their own subscription is not prohibited.
  - `--bare` (recommended for scripts) accepts API keys only (headless.md).
- **Codex CLI.** Source: https://developers.openai.com/codex/noninteractive.md
  - "API keys are the right default for automation."
  - ChatGPT-managed auth in CI/CD is documented as an "advanced" path for running "as your Codex account". The docs say not to use it for public or open-source repositories.
  - No explicit prohibition on scripted subscription use was found. A specific OpenAI ToS clause on this is **UNVERIFIED**.

### Differences from PLAN.md
- These findings are consistent with section 16.5. They strengthen it: for Claude Code, the documented terms specifically reserve subscription OAuth for "ordinary use".

### Recommendation for implementation
- `docs/security.md` should state that API keys (`ANTHROPIC_API_KEY`, `CODEX_API_KEY`/`OPENAI_API_KEY`) are the supported path, and that baton never handles or stores subscription credentials. Users who run the CLIs on their own subscription login must check their plan terms.
- Consider a `[workers.claude_code] bare = true` option. With it, Claude Code uses `--bare`, which forces API-key auth.

---

## 6. Agent tools (MCP, skills, plugins)

Checked 2026-10-12 against Claude Code 2.1.296 and codex-cli 0.162.1 (`--help` output only:
nothing was run that calls a model, logs in, installs or edits `~/.claude` / `~/.codex`), plus
the online docs cited below.

### Findings: Claude Code
- **Per-run MCP servers:** `--mcp-config <configs...>` loads servers "from JSON files or
  strings"; `--strict-mcp-config` makes it "Only use MCP servers from --mcp-config, ignoring all
  other MCP configurations". With `-p`, startup waits for pending servers up to `MCP_TIMEOUT`
  (30 s default); invalid entries are skipped and reported only in `system/init`
  `mcp_server_errors` when stderr is captured. Sources: `claude --help`;
  https://code.claude.com/docs/en/cli-reference.md; https://code.claude.com/docs/en/headless.md
- **File shape** (documented for `.mcp.json`, used for `--mcp-config` too):
  `{"mcpServers": {"<name>": {"type": "stdio", "command", "args", "env"} | {"type": "http"|"sse"|"ws", "url", "headers"}}}`.
  "A JSON entry that has a `url` but no `type` is a configuration error" (read as stdio), so jev
  always writes `type`. Source: https://code.claude.com/docs/en/mcp.md
- **`${VAR}` / `${VAR:-default}` expansion** is documented for `command`, `args`, `env`, `url`
  and `headers` "in `.mcp.json`". Whether it also applies to `--mcp-config` files is not stated:
  **UNVERIFIED**. Also, credential variables such as `ANTHROPIC_API_KEY` "read as empty" in a
  remote server's `url`/`headers`. Source: mcp.md
- **Permission names:** `mcp__<server>` matches every tool of a server, as does
  `mcp__<server>__*`; `mcp__<server>__<tool>` matches one. Allow globs need a literal
  `mcp__<server>__` prefix (`mcp__*` in an allow rule is ignored). Plugin servers are named
  `mcp__plugin_<plugin>_<server>__<tool>`. In `dontAsk`, anything not pre-allowed is denied, and
  tools marked `requiresUserInteraction` are denied even when allowed. Sources:
  https://code.claude.com/docs/en/permissions.md; mcp.md; headless.md
- **Project MCP in `-p`:** a `-p` run "loads project-scoped servers without asking" (no approval
  prompt); `--strict-mcp-config` keeps them out. Source: mcp.md
- **Skills discovery:** `~/.claude/skills/`, `.claude/skills/` from the cwd up to the repo root,
  nested `.claude/skills/` (lazily), plugin `skills/`, and `.claude/skills/` inside a directory
  passed with `--add-dir`. There is no flag that adds a single skill folder. Permission rules:
  `Skill(name)` and `Skill(name *)`. Source: https://code.claude.com/docs/en/skills.md
- **Plugins per run:** `--plugin-dir <path>` (repeatable; directory or `.zip`) and
  `--plugin-url <url>` load a plugin "for this session only". `enabledPlugins` is a settings key
  for installed plugins. Plugin skills are namespaced `<plugin>:<skill>`. Sources:
  `claude --help`; cli-reference.md; https://code.claude.com/docs/en/plugins-reference.md
- **`--bare`** skips auto-discovery of hooks, skills, commands, subagents, installed plugins,
  MCP servers, auto memory and CLAUDE.md, but still loads what flags name: `--mcp-config`,
  `--plugin-dir`, `--settings`, `--agents`, and the skills of an `--add-dir` folder. In bare
  mode Claude is not given the list of available skills in system reminders. Sources:
  headless.md; cli-reference.md
- **`--add-dir`** also grants file read/edit access to that directory.
- **Permission modes:** 2.1.296 `--help` lists `manual` instead of `default`; the docs say
  `manual` is an alias and "both values work". Source: cli-reference.md

### Findings: Codex
- **MCP config keys** under `[mcp_servers.<id>]`: stdio `command`, `args`, `env` (literal
  values), `env_vars` (names of variables to forward; strings or `{ name, source }`), `cwd`;
  streamable HTTP `url`, `bearer_token_env_var`, `http_headers` (static), `env_http_headers`
  (header -> env var name); plus `enabled`, `required`, `enabled_tools`, `disabled_tools`,
  `startup_timeout_sec`, `tool_timeout_sec`, `default_tools_approval_mode`
  (`auto|prompt|writes|approve`). Sources:
  https://developers.openai.com/codex/config-reference.md and
  https://developers.openai.com/codex/mcp.md (both now redirect to learn.chatgpt.com).
- **Per run:** `codex exec -c <dotted.key>=<TOML value>` overrides any `config.toml` value
  ("If it fails to parse as TOML, the raw string is used as a literal"), so
  `-c mcp_servers.github.command="npx"` etc. express a server without a config file. That
  `exec` connects servers given only through `-c` is **UNVERIFIED** end to end (expected, since
  `-c` is the general config override). There is no `--strict-mcp-config` equivalent: servers in
  `~/.codex/config.toml` still load. Source: `codex exec --help`, `codex mcp add --help`.
- **Only streamable HTTP** remote servers are documented (no SSE). Source: config-reference.md
- **Approvals:** how MCP tool calls behave under `approval_policy="never"` (which jev sets) is
  not documented: **UNVERIFIED**; `default_tools_approval_mode` values are listed but not
  explained beyond `writes` ("prompts for tools that aren't marked read-only").
- **Whether the sandbox (`-s read-only`) confines MCP servers** is not documented:
  **UNVERIFIED**. Assume a server can do whatever its credentials allow.
- **Skills:** discovered from `.agents/skills` (cwd up to the repo root), `$HOME/.agents/skills`,
  `/etc/codex/skills` and bundled ones. `[[skills.config]]` (`path`, `enabled`) is documented
  only for disabling. No CLI flag adds skills; nothing documents skills in `codex exec`.
  Source: https://developers.openai.com/codex/skills.md (redirects to learn.chatgpt.com)
- **Plugins:** installed from marketplaces (`codex plugin add`, `plugins.<name>@<market>.enabled`);
  no per-run plugin directory. Source: `codex plugin --help`; config-reference.md

### Differences from what we assumed
- Claude Code has **no per-run skills flag**: skills come in through `--add-dir` (its
  `.claude/skills/`) or a plugin. jev copies each configured skill into a temporary
  `<tmp>/skills/.claude/skills/<name>/` and passes `--add-dir <tmp>/skills`.
- `${VAR}` expansion is documented for `.mcp.json` only, so jev **writes resolved values** into
  the per-attempt `--mcp-config` file (mode 0600, in a private temp dir outside the worktree,
  deleted after the attempt) rather than relying on expansion. Switch to `${VAR}` once verified.
- `--strict-mcp-config` drops the repo's `.mcp.json` and user servers for that run: a role with
  `mcp = [...]` sees only those servers. Whether it also drops servers bundled in a
  `--plugin-dir` plugin is **UNVERIFIED**.
- Headless runs need explicit allow rules for MCP tools and skills (`dontAsk` denies the rest),
  so jev adds `mcp__<server>` and `Skill(<name>)` to `--allowedTools`. Plugin tool names depend on
  the plugin's own server names, so plugins take an explicit `allowed_tools` list in config.
- Codex can pass secrets **by name** (`env_vars`, `bearer_token_env_var`, `env_http_headers`), so
  nothing secret is written anywhere for Codex. Codex cannot take skills or plugins per run, nor
  SSE servers; jev leaves those out with a log note and `jev doctor` says so.

### Recommendation for implementation (done)
- Config: `[tools.mcp.<name>]` (`command`/`args`/`env_from` or `url`/`transport`/
  `bearer_token_env`/`headers_from_env`), `[tools.skills]`, `[tools.plugins.<name>]`; roles name
  them with `mcp`, `skills`, `plugins` (docs/config.md).
- API worker: no tools yet (future work: MCP client in the API worker).

---

## 7. Free models (OpenRouter)

Checked on 2026-10-12 against the OpenRouter docs and one unauthenticated `GET /api/v1/models`
(free, no key). No completions were requested.

### Findings
- **How free models are identified.**
  - The `:free` suffix is a *catalog variant*: "the free version appears as its own entry in
    the models API with its own pricing, context length, and endpoints"; only models that list
    one support it. Source: https://openrouter.ai/docs/guides/routing/model-variants/free.md
  - Pricing values are strings in USD per token; "A value of `"0"` indicates the feature is
    free". Source: https://openrouter.ai/docs/guides/overview/models.md ("Pricing Object")
  - Observed on 2026-10-12: 459 text models, 19 with prompt and completion `"0"`; 15 of those
    end in `:free`, the rest are price-0 models without the suffix (for example
    `inclusionai/ling-3.1-flash`) and the `openrouter/free` router. jev treats both as free.
  - Prices can vary by time window (`pricing.overrides`); the top-level price is the one that
    applies under default conditions now. jev uses the top-level price. Source: models.md.
- **`GET /api/v1/models`.**
  - Returns `{data: [...], total_count, links: {next}}`. Pagination is opt-in: without
    `offset`/`limit` the full list is returned. Fields used by jev: `id`, `name`,
    `context_length`, `pricing.prompt`, `pricing.completion`, `supported_parameters`. Query
    filters exist (`supported_parameters=...`, `output_modalities`, `sort`). Source: models.md.
  - **Auth:** the OpenAPI spec declares the bearer `apiKey` scheme globally
    (https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties.md),
    but an unauthenticated request returned HTTP 200 with the full list on 2026-10-12. jev
    sends the key when it is set and works without one. Whether unauthenticated access will
    stay supported is **UNVERIFIED**.
- **Routers and fallback.**
  - `openrouter/free` picks a free model at random per request, filtered to those supporting
    the request's needs ("tool calling, and structured outputs"); the response `model` names
    the model used. Source: https://openrouter.ai/docs/guides/routing/routers/free-router.md
  - Chat completions accept a top-level `models` array: "If the first model returns an error,
    OpenRouter will automatically try the next model in the list", including on rate limits;
    billing is "at the rate of the model that actually handled the request"; the response
    `model` says which one. No maximum length is documented for chat completions. Source:
    https://openrouter.ai/docs/guides/routing/model-fallbacks.md
  - `route: "fallback"` does not appear in the current docs: **UNVERIFIED** (likely retired).
- **Rate limits.** Source: https://openrouter.ai/docs/api_reference/limits.md ("Rate limits")
  - Free variants (ids ending in `:free`): 20 requests per minute; 50 requests per day for
    accounts that bought fewer than 10 credits (all time), 1000 per day for at least 10
    (granted from 9 credits to absorb fees). `GET /api/v1/key` reports
    `free_model_daily_requests: {used, limit, remaining}`; the per-minute limit is not reported.
  - Whether the limits are per model or shared across all free models is not stated: the
    daily counter is account-wide ("Free-model requests recorded so far in the current UTC
    day"); the per-minute scope is **UNVERIFIED**. A tip says rate limits differ per model "so
    you can share the load".
  - Whether failed requests count against the daily counter: **UNVERIFIED**.
  - A negative credit balance can return 402 even for free models (limits.md, "Credit limits").
- **Error shape when exceeded.** HTTP 429 with
  `{"error": {"code": 429, "message": "Rate limit exceeded", "metadata": {"error_type":
  "rate_limit_exceeded"}}}`, and `X-RateLimit-Limit`, `X-RateLimit-Remaining`,
  `X-RateLimit-Reset` headers when OpenRouter's own limit was hit; `Retry-After` only when every
  attempted provider sent a hint. Provider-side 429s carry `metadata.provider_code` instead.
  There is **no `limit_source`** on free-model 429s (that field belongs to 402/403 credit and
  budget limits). Mid-stream limits arrive as an SSE chunk with `finish_reason: "error"`.
  Telling the per-minute from the per-day limit apart (for example by `X-RateLimit-Limit`
  20 versus 50/1000) is not documented: **UNVERIFIED**. Source: limits.md.
- **`response_format` / `reasoning` on free models.** Support is per model, from
  `supported_parameters`: on 2026-10-12 every free text model listed `reasoning`; 5 of 19
  listed `structured_outputs` (json_schema), 4 more listed only `response_format`. Free
  variants "may have different rate limits or availability compared to paid versions" (free.md).
- **Privacy.** "There are separate settings for paid and free models" for allowing providers
  that may train on prompts; with training opted out, OpenRouter "will not route to providers
  that train". Source: https://openrouter.ai/docs/guides/privacy/provider-logging.md. Which
  free endpoints log or train is per provider and changes over time; check the account's
  privacy settings (https://openrouter.ai/settings/privacy).

### Recommendation for implementation (done)
- Free = prompt and completion price 0, or an id ending in `:free`. Capability for structured
  roles = `structured_outputs` in `supported_parameters`; no list at all = unknown (kept,
  marked). `openrouter/free` is not added by `"free"` expansion (random, not reproducible).
- Cache `GET /models` for 24h; the key is optional.
- Classify any 429 for a free model as `quota_exhausted` (no `limit_source` to rely on, and
  the remedy is another model, not a wait) and fall back: next free model, first paid model,
  another worker. A 404 on a free model whose body signals a data-policy/guardrail block
  ("no endpoints … guardrail … training") is `quota_exhausted` too (issue #327): the
  2026-10-25 parked run failed with HTTP 404 "0 endpoints available matching your guardrail
  restrictions … Free model training violation" and parked at NEEDS_HUMAN because 404 mapped
  to plain `error`, so the ladder never engaged.
- Under the `mixed_no_training` planner policy (issue #327), free-model calls send
  `provider: { data_collection: "deny" }` so OpenRouter routes only to providers that do not
  collect user data; the paid fallback call is sent without it. This is a per-request
  narrowing only: the OpenRouter account privacy setting (separate for free and paid models)
  is the authoritative control and Baton cannot change it.
- jev does **not** send the `models` fallback array: one attempt would then be served and
  billed by a model jev did not choose (possibly a paid one), which breaks per-attempt model
  records and budget reasoning. The fallback chain is explicit, one attempt per model, instead.

---

## 8. GitHub CLI (`jev ship`)

Checked on 2026-10-13 with the locally installed `gh` 2.86.0, by reading its help only
(`gh --help`, `gh pr create --help`, `gh pr view --help`, `gh repo view --help`,
`gh pr list --help`). No `gh` command was run against any repository, and no login,
push or API call was made; the tests use a fake `gh` and a local bare repo.

### Findings
- **`gh pr create`** takes `--repo/-R OWNER/REPO` (inherited), `--head/-H branch` ("explicitly
  skip any forking or pushing behavior"), `--base/-B`, `--title/-t`, `--body-file/-F`,
  `--draft/-d` and `--label/-l` (repeatable). "Upon success, the URL of the created pull
  request will be printed." It has no merge or auto-merge flags.
- **`gh pr view [<number> | <url> | <branch>] --json fields`**: the JSON field list printed by
  the help includes every field jev asks for: `state`, `createdAt`, `mergedAt`, `closedAt`,
  `reviews`, `comments`, `commits`, `additions`, `deletions`, `changedFiles`,
  `statusCheckRollup`, `isDraft` (also `number`, `url`).
- **`gh repo view --json`** lists `nameWithOwner` and `defaultBranchRef`.
- **`gh pr list`** has `--state {open|closed|merged|all}`, `--search query` (GitHub search
  syntax) and `--json` with `number`, `title`, `body`.
- **Value shapes** (not exercised against a live PR: **UNVERIFIED** beyond GitHub's GraphQL
  schema): `state` is `OPEN`, `CLOSED` or `MERGED`; times are ISO 8601 with `Z`; `reviews`,
  `comments` and `commits` are arrays (reviews and comments carry a `body`);
  `statusCheckRollup` mixes check runs (`status` such as `COMPLETED`, `IN_PROGRESS`, and
  `conclusion` such as `SUCCESS`, `FAILURE`, `NEUTRAL`, `SKIPPED`, `CANCELLED`, `TIMED_OUT`,
  `ACTION_REQUIRED`) and commit statuses (`state`: `SUCCESS`, `FAILURE`, `ERROR`, `PENDING`,
  `EXPECTED`). jev treats unknown values as pending and missing fields as unknown.
- **`gh auth status`** exits non-zero when not logged in. It may contact the GitHub host to
  check the token.

### Recommendation for implementation (done)
- Push with plain `git push <remote> <src>:refs/heads/<dst>` (never `--force`), then
  `gh pr create --head <dst>` so gh neither pushes nor forks.
- Take the PR number from the printed URL; fall back to `gh pr view <branch> --json number,url`.
- Set `GH_PROMPT_DISABLED=1` so gh never waits for input.
- Inline review comments are not part of `gh pr view --json`; "review comments" counts
  conversation comments plus reviews with a body. Reverts are detected heuristically (the body
  `Reverts owner/repo#N` or title `Revert "<title>"` that GitHub's revert button writes).

### Shipping after the votes (issue #68)

Checked on 2026-10-13 with `gh` 2.89.0 in the cloud environment, again by reading help only
(`gh pr merge --help`, `gh pr view --help`, `gh help exit-codes`). No command was run against a
repository.

- **`gh pr merge [<number> | <url> | <branch>]`** has `--squash/-s`, `--merge/-m`,
  `--rebase/-r`, `--match-head-commit SHA` ("Commit SHA that the pull request head must match
  to allow merge"), `--delete-branch/-d`, `--auto`, `--disable-auto`, `--admin` and the
  inherited `--repo/-R`. jev passes `--repo`, one method flag and `--match-head-commit` only.
- **Merge queues.** The help says that on a branch that requires a merge queue, "If required
  checks have not yet passed, auto-merge will be enabled. If required checks have passed, the
  pull request will be added to the merge queue." jev merges only after the checks it sees are
  green, so on such a branch the PR should be queued; jev then finds it not merged and stops for
  a person instead of waiting. **UNVERIFIED** live: whether gh can still enable auto-merge when
  a required check is missing from what `statusCheckRollup` reports.
- **`gh pr view --json`** also lists `headRefOid` and `mergeable` (and `mergeStateStatus`,
  `reviewDecision`). Value shapes are verified live since 2026-10-15 (below): `mergeable`
  observed as `MERGEABLE` on open PRs and `UNKNOWN` after merging (GitHub's GraphQL enum);
  only `CONFLICTING` stops a merge.
- **Exit codes** (`gh help exit-codes`): 0 success, 1 any failure, 2 cancelled, 4 needs
  authentication. What `gh pr merge` prints when not on a terminal is verified live since
  2026-10-15 (below): exit 0 with no output. jev does not read it: after the command it reads
  the PR back and trusts only `state: MERGED`. The fake `gh` in the tests answers `pr merge`
  with exit 0 and no output.

Needs one live check on a scratch repository (with the maintainer's OK): a PR with green
checks, one with a failing check, one without CI, and one on a branch with required reviews,
to confirm the `gh pr view` value shapes and that `gh pr merge` refuses (exit 1) rather than
enabling auto-merge.

#### Live check on 2026-10-15 (issue #68's remaining boxes)

Run with `gh` 2.102.0 against a disposable scratch repo, `Gane2506/jev-merge-test-disposable`
(private; made public briefly to set branch protection, then private again; the repo could not
be deleted afterwards — the VM's token lacks the `delete_repo` scope, so it is left private
with a disposable name). Four scratch PRs were opened and merged/refused; no other
repository was touched.

- **`gh pr view --json state,isDraft,headRefOid,mergeable,statusCheckRollup` on an open PR.**
  `state` is `OPEN` (uppercase), `isDraft` a boolean, `headRefOid` the full 40-character SHA,
  `mergeable` was `MERGEABLE`. `statusCheckRollup` entries carry `__typename` (`CheckRun` or
  `StatusContext`): check runs have `status` (`COMPLETED`) and `conclusion` (`SUCCESS` /
  `FAILURE`) with `name`; status contexts have `context` and `state` (`PENDING`), and no
  `status`/`conclusion`. This matches what `ci_status` and `merge_check` assume. A
  pending non-required status (a third-party `CodeRabbit` context) makes `ci_status` return
  `pending`, so jev waits on it, exactly as designed.
- **`gh pr merge <n> --repo <repo> --squash --match-head-commit <sha>` (jev's exact form).**
  Exit 0 with **no output** when not on a terminal — the fake `gh`'s "exit 0 and no output"
  assumption was right. Read back afterwards: `state` `MERGED`, `mergedAt`/`closedAt` set to
  ISO-8601 `Z` times, `mergeable` now `UNKNOWN` (jev only ever stops on `CONFLICTING`, and it
  checks `state` first, so this is harmless).
- **Merge refusal.** With a wrong `--match-head-commit`: exit 1,
  `GraphQL: Head branch was modified. Review and try the merge again. (mergePullRequest)`.
  The pin works: only the voted commit merges. On a branch with required approving reviews
  (set via the API): exit 1,
  `X Pull request ... is not mergeable: the base branch policy prohibits the merge.`
  followed by suggestions to add `--auto` or `--admin` — flags jev deliberately never passes,
  so a held PR stops for a person instead of queueing silently.
- **Merging an already-merged PR**: exit **0** with the warning
  `! Pull request ... was already merged` on stderr. jev reads the PR back and finds
  `state: MERGED`, so this is idempotent, as the SHIPPING step's "found, not redone" design
  requires.
- **`gh pr view` for `jev ship sync`'s fields** (`state,createdAt,mergedAt,closedAt,reviews,
  comments,commits,additions,deletions,changedFiles,statusCheckRollup,isDraft`): `state`
  `MERGED`, ISO `Z` times, `reviews`/`comments`/`commits` are arrays (reviews and comments
  carry `body`), matching `github_kpis`'s expectations.
- jev's own `merge_check` and `ci_status` were run against the captured live payloads: a
  failing check run maps to "stop: checks failed: test", a pending status context to "wait",
  and `pr_state("MERGED")` to "merged".

No live check was done for the checks timeout (1 h) or a merge queue; those paths are
covered by the fake-`gh` tests. With this, both of issue #68's remaining boxes are done:
the merge path is verified live and recorded here, and offline tests cover merge success,
merge failure, the checks timeout and `gh pr view --json` parsing
(`tests/unit/test_ship.py`).

---

## 9. Servers, SSH and browsers (issue #3)

Checked 2026-10-13 against the local OpenSSH (`ssh -V`: OpenSSH_9.9p2, LibreSSL 3.3.6) and its
man pages (`man ssh_config`, `man ssh`), PyPI and npm registry metadata (read-only JSON), the
MCP specification and the Playwright, Playwright MCP and noVNC docs cited below. No SSH
connection was opened, nothing was installed and no browser was started.

### Findings: OpenSSH, non-interactive use
- **`BatchMode=yes`**: "user interaction such as password prompts and host key confirmation
  requests will be disabled", meant for "scripts and other batch jobs". Source: ssh_config(5).
- **`StrictHostKeyChecking`**: `yes` means ssh "will never automatically add host keys to the
  ~/.ssh/known_hosts file, and refuses to connect to hosts whose host key has changed";
  `accept-new` adds unknown keys but refuses changed ones; `no`/`off` adds new keys and still
  connects to hosts with changed keys "subject to some restrictions"; `ask` is the default.
  jev uses `yes`: the user connects once by hand to check and accept a new host's key.
  Source: ssh_config(5).
- **`ConnectTimeout`**: seconds, applied to the TCP connect and "the initial SSH protocol
  handshake and key exchange". Source: ssh_config(5).
- **Connection reuse**: `ControlMaster=auto` uses a master if one is listening on
  `ControlPath`, else becomes one ("opportunistic multiplexing"); `ControlPersist=<seconds>`
  keeps the master in the background until idle that long; the docs recommend a `ControlPath`
  with `%C` ("Hash of %l%h%p%r%j") "placed in a directory that is not writable by other users".
  `ControlPath=none` disables sharing. `ssh -O check|exit` controls a master. Source:
  ssh_config(5) (ControlMaster, ControlPath, ControlPersist, TOKENS); ssh(1) (`-O`).
- **Socket path length**: a Unix socket path is limited (104 bytes on macOS, 108 on Linux) and
  ssh creates the master under a temporary name with a random suffix first; jev turns reuse off
  when `.jev/ssh/<%C>` would not fit. The exact suffix length is from OpenSSH's source
  (mux.c), not the man page: **UNVERIFIED** in docs; jev keeps an 18-byte margin.
- **`IdentitiesOnly=yes`**: use only the configured identity files "even if ssh-agent ... offers
  more identities". jev sets it together with `-i` when `identity_file` is set. Source:
  ssh_config(5).
- **`~/.ssh/config` aliases**: the destination is matched against `Host` patterns, so an alias
  works as `host`. `ssh -G HOST` prints the evaluated configuration (including `hostname`)
  without connecting; `jev doctor` uses it, then a DNS lookup, and connects only with
  `--connect` (`ssh -o BatchMode=yes ... HOST true`). Source: ssh(1) (`-G`).
- **`-T`** disables pseudo-terminal allocation (no tty for commands). `--` ends options before
  the destination. Source: ssh(1).
- **Remote commands** are a single string run by the remote user's login shell, so jev builds
  it only from `shlex.quote`d words (servers/policy.py), never from raw agent text.

### Findings: MCP server (Python)
- **SDK**: `mcp` 2.3.0 on PyPI (Python >= 3.10) depends on anyio, httpx2, jsonschema, mcp-types,
  opentelemetry-api, pydantic, pyjwt[crypto], python-multipart, sse-starlette, starlette,
  typing-extensions, typing-inspection and uvicorn. Source: https://pypi.org/pypi/mcp/json
- **stdio transport**: the client starts the server as a subprocess; "Messages are delimited by
  newlines, and MUST NOT contain embedded newlines"; the server "MUST NOT write anything to its
  stdout that is not a valid MCP message"; stderr is for logs. Source:
  https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- **Versions**: the current revision is **2026-07-28**: no handshake; each request carries
  `_meta["io.modelcontextprotocol/protocolVersion"]`; servers MUST implement `server/discover`
  (returns `supportedVersions`, `capabilities`, `_meta[...serverInfo]`) and answer unknown
  versions with error `-32022` ("Unsupported protocol version", `data.supported`). Revisions
  2025-11-25 and earlier are "legacy" (the `initialize` handshake); a "dual-era" server answers
  `initialize` with legacy semantics and modern requests statelessly. Sources:
  https://modelcontextprotocol.io/specification/versioning,
  https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning,
  https://modelcontextprotocol.io/specification/2026-07-28/server/discover
- Whether Claude Code and Codex (the versions jev drives) send `initialize` (legacy) or
  modern requests to stdio servers today is **UNVERIFIED**; jev's server answers both.
- MCP clients start stdio servers with a reduced environment (the TypeScript SDK's default
  passes only HOME, LOGNAME, PATH, SHELL, TERM, USER plus the configured `env`): **UNVERIFIED**
  for Claude Code specifically. jev therefore lists `SSH_AUTH_SOCK` in the synthesized entry's
  `env_from`, so ssh keeps access to the agent's keys.

### Findings: Playwright MCP and the browser
- **`@playwright/mcp`**: latest `0.0.83`, published 2026-09-28 (npm `dist-tags`); it depends on
  `playwright` and `playwright-core` `1.64.0-alpha-1790635538000`; bin `playwright-mcp`; Node
  >= 18. Source: https://registry.npmjs.org/@playwright/mcp (and `/0.0.83`).
- **Run command**: the README's standard config is `"command": "npx"`, `"args":
  ["@playwright/mcp@latest"]`. Options used by jev: `--headless` ("run browser in headless
  mode, headed by default"), `--browser` ("browser or chrome channel to use, possible values:
  chrome, firefox, webkit, msedge"; the Docker example uses `--browser chromium`), `--isolated`
  ("keep the browser profile in memory, do not save it to disk"), `--output-dir`. Other
  relevant options: `--allowed-origins`, `--no-sandbox`, `--user-data-dir`. Source:
  https://github.com/microsoft/playwright-mcp/blob/v0.0.83/README.md
- **Browser install**: `npx playwright install --with-deps chromium` installs the browser and
  its OS dependencies "with a single command" ("useful for CI environments"). Headless runs use
  a separate "chromium headless shell" unless the `chromium` channel is chosen (then
  `--no-shell` skips it). Whether `--with-deps` calls `sudo` itself on Linux is not stated in
  the docs: **UNVERIFIED** (it needs root to install packages). Source:
  https://playwright.dev/docs/browsers
- The browser build must match the Playwright version the MCP server uses, so jev pins the
  install to `playwright@1.64.0-alpha-1790635538000` (the MCP server's dependency), not
  `playwright@latest` (1.64.0 at the time). That this alpha's Chromium revision differs from
  1.64.0's is **UNVERIFIED**; pinning the same version avoids the question.
- **Xvfb + noVNC** (documented only, not automated): run a virtual display (`Xvfb :99`), a VNC
  server on it (`x11vnc -display :99 -localhost`), and noVNC's proxy (`./utils/novnc_proxy
  --vnc localhost:5900 --listen localhost:6080`, which "can automatically download and start
  websockify"); then start the browser headed with `DISPLAY=:99` and open noVNC through an SSH
  tunnel. Source: https://github.com/novnc/noVNC/blob/master/README.md (Quick start); Xvfb(1)
  and x11vnc(1) usage from their man pages, not checked on a server here: **UNVERIFIED**.

### Recommendation for implementation (done)
- `ssh` with `-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10`, connection
  reuse under `.jev/ssh/` (`%C`, mode 0700, `ControlPersist=60`), `-T`, `--`; files through
  `ssh ... 'head -c N -- PATH'` and `cat > TMP && mv -f -- TMP PATH` (no scp/sftp needed).
- A minimal dual-era stdio MCP server in jev (about 200 lines, no new dependency) instead of the
  `mcp` SDK, whose HTTP stack (starlette, uvicorn, httpx2, pyjwt, opentelemetry) jev would never
  use. Supported: `initialize` (2024-11-05 to 2025-11-25), `server/discover`, per-request
  `_meta` versions, `ping`, `tools/list`, `tools/call`.
- Playwright MCP pinned to `@playwright/mcp@0.0.83 --browser chromium --isolated [--headless]`;
  install with `npx -y playwright@1.64.0-alpha-1790635538000 install --with-deps chromium`.
