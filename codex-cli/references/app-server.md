# Native interactions and evidence

Read this reference before replying to a native request or modifying the runner. Responses below are the JSON body in `--response-file`; the bridge supplies the original RPC ID. Keep the user decision and the protocol transport separate.

## Questions

`item/tool/requestUserInput` carries `threadId`, `turnId`, `itemId`, `isBlocking`, and `questions`. Each question has an `id`, `header`, `question`, optional options, and potentially `isOther`/`isSecret`. Answer using every actual **question ID**, not question text or a Grok-style outcome:

```json
{"answers":{"fixture":{"answers":["Fixture A"]}}}
```

Use the selected label, or the user's freeform response where supported. Inspect `isBlocking` to distinguish waiting from a question the agent can continue working around. The deprecated `autoResolutionMs` is not a permission timer. `serverRequest/resolved` invalidates the request even when the user has not answered, for example when the turn is interrupted or completes.

Default-mode question availability depends on the installed CLI's features. `--enable-user-input` supplies `features.default_mode_request_user_input=true` and `tools.experimental_request_user_input.enabled=true` to this app-server invocation; it does not edit the user's configuration. Plan mode can issue blocking questions; default mode can issue nonblocking questions. Use the mode the task requires. The parent host presents questions in its own conversation and replies through the runner; the runner does not create a native Codex App question card on its own.

## Command and file approvals

`item/commandExecution/requestApproval` and `item/fileChange/requestApproval` carry the actual action and context. An individual approval is:

```json
{"decision":"accept"}
```

`decline` denies the action while allowing the turn to continue; `cancel` also interrupts the turn. `acceptForSession` has wider duration and must match the established authority. For command requests, honor any advertised `availableDecisions`. The runner supports the four string decisions above. Persistent command/network rule amendment objects are not implemented; handle those through an appropriate client if explicitly required. Do not invent or enlarge a rule to get past a blocked tool.

The default read-only sandbox remains enforced by Codex. For authorized write work the skill can use workspace-write with `auto_review`; a rejection is not authority to retry unrestricted. Report the concrete rejected action and reason when user action is needed.

## Additional permissions and MCP elicitation

`item/permissions/requestApproval` requests a specific permission profile. A response uses the received profile or an authorized subset and a duration:

```json
{"permissions":{},"scope":"turn"}
```

An empty profile grants nothing. The runner validates that returned permission entries do not expand the request. A session-wide grant requires corresponding authority; no grant is inferred from the request itself.

`mcpServer/elicitation/request` is an MCP server's form or URL interaction. Read its message and schema. Respond with `action` (`accept`, `decline`, or `cancel`) and accepted form `content` when applicable. A URL flow may require actual work in the browser; acknowledging the RPC is not evidence that external authentication or consent completed. The bridge does not implement arbitrary client-executed dynamic tools. Unsupported reverse requests fail explicitly and preserve evidence.

## Steering and completion

`steer --text-file` submits the user's correction with the exact active `threadId` and `expectedTurnId`. It fails for a completed or replaced turn. Use `respond` for an actual pending request; steering is additional input, not a substitute answer.

`cancel` invokes `turn/interrupt` and closes only the child process group owned by this run. An exact-session continuation creates a new run directory, reuses the thread ID, and records only its new turn. Historical transcript items and child-thread final messages do not become the root turn's result.

App-server messages are newline-delimited JSON-RPC objects **without** the `jsonrpc` field. `initialize` and `initialized` precede thread/turn requests. `turn/start` returns an acknowledgement; only `turn/completed` with status `completed` plus substantive final text can satisfy the result gate. `failed`, `interrupted`, premature EOF, and unknown client requests cannot become success because the server process was alive or exited cleanly.

`result.json` also records `cleanupComplete` and `cleanupWarnings`. A native completed turn whose owned process group cannot be confirmed stopped retains its text and native status, but the supervisor reports failure. This distinguishes a model result from a cleanly closed invocation.

## Artifacts and maintenance

The run directory contains atomic `status.json`, cursor-indexed `events.jsonl`, redacted inbound `protocol.jsonl`, `stderr.log`, and `result.json`. Use status and compact events for routine progress; protocol logs are targeted diagnostic evidence. Known credentials are redacted, but arbitrary project content can still be sensitive. Keep artifacts according to the main skill's lifecycle.

The complete Codex implementation lives in `scripts/codex_app_server.py`. Keep this skill self-contained: it must not import from the repository root or from the Grok skill. Run the tests from `codex-cli/tests` after changes.

The development contract was generated from installed Codex CLI `0.152.0`. Native app-server fields are version-dependent. Generate schemas with the installed CLI when adapting the bridge, rather than assuming current web examples match the binary.

Validation on 2026-09-05:

- 49 standard-library tests passed: 8 shared supervisor tests, 22 Grok ACP tests, and 19 Codex app-server tests. These use synthetic peers and credentials, not model calls.
- Live Codex exposed a fixture-read command, a blocking Plan question, and a nonblocking Default question. Replies used actual question IDs and produced the selected fixture labels.
- Installed Codex `0.152.0` was rejected by the configured `gpt-6-astra` model with an upgrade-required error. Version `0.153.4` successfully used that same configured model; the local CLI was upgraded without changing the default model or global feature settings.
- An exact-session continuation returned only `CODEX_RESUME: Alpha`. Final receipt was successful and the owned process group was confirmed absent. macOS can briefly return EPERM during group teardown; the supervisor retries within two seconds and preserves the observed warning.
- Command/file approvals, additional permissions, MCP forms, steering, and cancellation are covered by synthetic-peer tests. They were not all separately exercised against a live Codex model.

- [Official app-server documentation](https://developers.openai.com/codex/app-server/)
- [Official noninteractive mode documentation](https://developers.openai.com/codex/noninteractive/)
- [Codex 0.152.0 question implementation](https://github.com/openai/codex/blob/rust-v0.152.0/codex-rs/core/src/tools/handlers/request_user_input.rs)
