# ACP interactions and evidence

Read this reference when replying to a Grok request, investigating protocol failure, or changing the bridge. The runner's `--help` is the command-line interface. Response examples below are the contents of `--response-file`, not full JSON-RPC envelopes: the bridge supplies the original RPC ID.

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
- `status` reports the last event's age. Silence does not reveal whether a model, subprocess, network service, or human is responsible. Unexpected protocol requests fail with a concrete method name and retained evidence.
- Arbitrary command-line stdin prompts and unimplemented MCP elicitation extensions are not automatically brokered. Diagnose the recorded tool/error, use its supported noninteractive option or an appropriate client, and retain the failure evidence.

The run directory keeps `protocol.jsonl` (inbound events with credential redaction), `events.jsonl` (compact cursor-indexed activity), `stderr.log`, `status.json`, and `result.json`. Configuration credentials and known sensitive values are redacted; this is not a guarantee that arbitrary project content is free of secrets. Retain logs only through acceptance unless needed for diagnosis or an audit. Configuration notifications are not echoed with their payloads.

The shared supervisor also records `cleanupComplete` and `cleanupWarnings` in the result. If the owned process group cannot be confirmed stopped, the final receipt fails while preserving the turn's text and native stop reason for diagnosis.

## Sources and compatibility

The installed CLI was Grok `1.0.13` (`5e9a58528b76`) during development. Its `initialize` response negotiated ACP v1, `loadSession`, and `cached_token`. Public source commit `72a61251fcffb464bcc687aeb5a998e5a98ec0c9` established extension shapes; it is **not** the installed binary's source revision. Live smoke tests and fake-peer protocol tests are separate evidence tiers.

Validation on 2026-09-05:

- 22 standard-library tests passed: fake ACP subprocesses, Unix control socket, partial tool updates, permission/question/plan replies, expired and duplicate requests, cancellation races, abrupt supervisor loss, child sessions, resume history isolation, model configuration, and credential redaction.
- Live Grok exposed file-read transitions and completion. With `--ask-permissions`, a new fixture write stayed pending and the file did not exist; submitting the actual `allow-once` option released the operation, created the expected file, and returned `end_turn`.
- Live exact-session resume returned only the new answer, recalled the prior fixture marker, and reported effective `yolo=true` and `reasoningEffort=low`, confirming invocation settings were reapplied.
- Native question smoke returned `TOOL_UNAVAILABLE` with no question RPC. Question and plan response formats are covered by fake-peer tests; native question availability in this installed toolset was not accepted as working. Use final-text questions plus exact-session resume when needed.

Run the portable regression suite from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s grok-cli/tests -v
```

The complete Grok implementation lives in `scripts/grok_acp.py`. Keep this skill self-contained: it must not import from the repository root or from the Codex skill. Run the tests from `grok-cli/tests` after changes.

- [xAI headless and ACP documentation](https://docs.x.ai/build/cli/headless-scripting)
- [ACP tool events and permission responses](https://agentclientprotocol.com/protocol/v1/tool-calls)
- [Grok question request and response types](https://github.com/xai-org/grok-build/blob/72a61251fcffb464bcc687aeb5a998e5a98ec0c9/crates/codegen/xai-grok-tools/src/implementations/grok_build/ask_user_question/types.rs)
- [Grok plan approval types](https://github.com/xai-org/grok-build/blob/72a61251fcffb464bcc687aeb5a998e5a98ec0c9/crates/codegen/xai-grok-tools/src/implementations/grok_build/exit_plan_mode/types.rs)
- [Grok transient interaction notifications](https://github.com/xai-org/grok-build/blob/72a61251fcffb464bcc687aeb5a998e5a98ec0c9/crates/codegen/xai-grok-shell/src/session/pending_interaction.rs)
