# ACP interactions and evidence

Read this reference when replying to a Grok request, investigating protocol failure, or changing the bridge. The runner's `--help` is the command-line interface. Response examples below are the contents of `--response-file`, not full JSON-RPC envelopes: the bridge supplies the original RPC ID.

The installed CLI is the protocol source. Official docs, local user-guide pages, and this file can lag. For each run, `initialize` and the session payload win over any remembered method list.

## Discovery

The bridge advertises no client filesystem or terminal capabilities. It reads what Grok advertises and selects among supported paths.

- `initialize` returns `protocolVersion`, `authMethods`, and `agentCapabilities`. Negotiate ACP v1. Authenticate with an advertised noninteractive method: `xai.api_key` when `XAI_API_KEY` or `GROK_CODE_XAI_API_KEY` is set and advertised, else `cached_token`. Interactive methods such as `grok.com` need an explicit `--auth-method`.
- Continue a session with `session/resume` when `agentCapabilities.sessionCapabilities.resume` is present. Otherwise use `session/load` when `loadSession` is true. Resume restores context without replaying history; load may replay `session/update` frames that must not enter this turn's answer.
- `session/new`, `session/load`, and `session/resume` may include `configOptions` and/or a legacy `models` object. When `configOptions` is present, apply `--model` and `--reasoning-effort` with `session/set_config_option` and a **string** `value` (`"high"`, not `{"value":"high"}`). Grok's local agent-mode example of a nested object is invalid against the live CLI and the ACP spec. Fall back to `session/set_model` only when `configOptions` is absent.
- `configOptions` currently used by Grok: `model` (category `model`) and `reasoning_effort` (category `thought_level`). Treat the list as open: use advertised ids, and fail if a requested override has no matching option.
- Unknown `sessionUpdate` kinds (`config_option_update`, `usage_update`, `available_commands_update`, and later additions) are recorded as `session_event` and are not fatal. Tool, plan, and interaction frames keep their dedicated handlers.
- Successful JSON-RPC replies whose `id` is not an integer waiter ID are internal receipts. Log them and keep waiting for `session/prompt`. Integer unknown or duplicate IDs still fail.

Grok also emits `_x.ai/session/setup`, `_x.ai/mcp/servers_updated`, `_x.ai/session_notification`, and other `x.ai/` extensions. Those are notifications; they are not turn completion. Discover further methods from `initialize` `_meta` rather than copying a versioned catalog into this skill.

## Permission

`session/request_permission` supplies a tool call and real `options[].optionId` values. Return one received option, rather than inventing `allow` or approving a different action:

```json
{"optionId":"actual-option-id"}
```

The bridge converts this to ACP's nested `outcome`. `{"outcome":"cancelled"}` declines to complete the request. The CLI's existing `--always-approve` policy does not authorize new work outside the handoff, nor does it eliminate model questions or configured policy blocks.

## Grok questions

Supported request: `x.ai/ask_user_question`; Grok can send the wire method `_x.ai/ask_user_question` with an `ExtRequest` wrapper containing `method` and `params`. The unwrapped payload includes `sessionId`, `toolCallId`, `questions`, and optionally `mode`.

Question responses are keyed by the **exact question text**, not a made-up ID or the header. Selected answers use the actual option labels:

```json
{
  "outcome": "accepted",
  "answers": {"Which test fixture should be used?": ["Fixture A"]}
}
```

For a freeform response, Grok's schema uses `Other` and per-question notes:

```json
{
  "outcome": "accepted",
  "answers": {"Which test fixture should be used?": ["Other"]},
  "annotations": {
    "Which test fixture should be used?": {"notes": "Use the temporary fixture created for this run."}
  }
}
```

Only supply the actual answer authorized by the user or already established by the handoff. The bridge carries replies; it does not decide them. `{"outcome":"cancelled"}` dismisses a question. Cancelling the whole task uses the separate `cancel` command.

## Plan approval

`x.ai/exit_plan_mode` (including its underscored wrapper) supplies `planContent`. Read the plan and compare it to the existing authorization before replying. The response is `{"outcome":"approved"}`, `{"outcome":"abandoned"}`, or `{"outcome":"cancelled","feedback":"The required correction."}`. An approved plan may authorize the next agent action; automatic receipt is not automatic approval.

## State and failure semantics

- Tools merge `tool_call` and `tool_call_update` by session and tool-call ID; partial updates do not erase earlier details.
- `pending` means the tool has not started; it can also reflect streaming arguments. Only an explicit interaction request or Grok interaction notification proves that input is pending.
- Grok broadcasts `pending_interaction` and `interaction_resolved` through `x.ai/session_notification` extensions. A notification records state; replying requires the corresponding live RPC request ID. A resolved or expired request is no longer answerable.
- ACP `session/prompt` completion and its `stopReason` are authoritative for the turn. Grok's service process can remain alive after completion. Historical messages from `session/load` must not enter this turn's answer.
- `status` reports the last event's age. Silence does not reveal whether a model, subprocess, network service, or human is responsible. Unexpected protocol *requests* fail with a concrete method name and retained evidence. Unexpected *notifications* and unknown `sessionUpdate` kinds do not.
- Arbitrary command-line stdin prompts and unimplemented MCP elicitation extensions are not automatically brokered. Diagnose the recorded tool/error, use its supported noninteractive option or an appropriate client, and retain the failure evidence.

The run directory keeps `protocol.jsonl` (inbound events with credential redaction), `events.jsonl` (compact cursor-indexed activity), `stderr.log`, `status.json`, and `result.json`. Configuration credentials and known sensitive values are redacted; this is not a guarantee that arbitrary project content is free of secrets. Retain logs only through acceptance unless needed for diagnosis or an audit. Configuration notifications are not echoed with their payloads.

The shared supervisor also records `cleanupComplete` and `cleanupWarnings` in the result. If the owned process group cannot be confirmed stopped, the final receipt fails while preserving the turn's text and native stop reason for diagnosis.

ACP `text` concatenates this turn's `agent_message_chunk` messages, including any model progress prose before its answer. Consumers must not assume a final-answer-only field or exact structured output. Use the direct JSON-schema path when structured content is required.

## Internal receipts

Grok can leak internal JSON-RPC replies onto ACP stdout with non-integer IDs while a prompt is active. One observed shape is `{"jsonrpc":"2.0","id":"skills-reload","result":{"result":{"reloaded":1}}}`. Older bridges aborted with `Grok returned an unknown or duplicate response ID`.

The bridge records any successful non-integer reply as `internal_response` and continues waiting for the real `session/prompt` result. Integer unknown IDs, duplicate waiter IDs, and error replies still fail with retained evidence. These frames are not ACP standard messages and are not completion signals.

If a run still fails on an unknown response ID, inspect the last response ID in `protocol.jsonl` and confirm the installed `scripts/grok_acp.py` includes this handler. Preserve the failed run. Before continuing a failed write task, inspect its diff and side effects; resume its exact session with a new run directory only after establishing what already happened. A read-only review can be rerun with its original scope. The failed review remains unaccepted until the replacement finishes and is independently verified.

## Tests and sources

Run the portable regression suite from the repository root. It uses a fake ACP peer; it does not call a model.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s grok/tests -v
```

The complete Grok implementation lives in `scripts/grok_acp.py`. Keep this skill self-contained: it must not import from the repository root or from the Codex skill. After bridge changes, run the tests from `grok/tests`. Live CLI smoke is optional extra evidence, not a reason to pin a version number into `SKILL.md`.

- [Grok Build overview](https://docs.x.ai/build/overview)
- [xAI headless and ACP documentation](https://docs.x.ai/build/cli/headless-scripting)
- [CLI reference](https://docs.x.ai/build/cli/reference)
- [Sessions](https://docs.x.ai/build/features/sessions)
- [Enterprise permissions and API-key auth](https://docs.x.ai/build/enterprise)
- [ACP initialization and capabilities](https://agentclientprotocol.com/protocol/v1/initialization)
- [ACP session setup, load, and resume](https://agentclientprotocol.com/protocol/v1/session-setup)
- [ACP session config options](https://agentclientprotocol.com/protocol/v1/session-config-options)
- [ACP prompt turn and session updates](https://agentclientprotocol.com/protocol/v1/prompt-turn)
- [ACP tool events and permission responses](https://agentclientprotocol.com/protocol/v1/tool-calls)
- [Grok question request and response types](https://github.com/xai-org/grok-build/blob/72a61251fcffb464bcc687aeb5a998e5a98ec0c9/crates/codegen/xai-grok-tools/src/implementations/grok_build/ask_user_question/types.rs)
- [Grok plan approval types](https://github.com/xai-org/grok-build/blob/72a61251fcffb464bcc687aeb5a998e5a98ec0c9/crates/codegen/xai-grok-tools/src/implementations/grok_build/exit_plan_mode/types.rs)
- [Grok transient interaction notifications](https://github.com/xai-org/grok-build/blob/72a61251fcffb464bcc687aeb5a998e5a98ec0c9/crates/codegen/xai-grok-shell/src/session/pending_interaction.rs)
