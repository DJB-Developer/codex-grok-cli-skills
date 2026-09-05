---
name: grok
description: Delegate a bounded task to the local Grok CLI with live tool status, questions, cancellation, and exact-session continuation. Use for explicit $grok or Grok CLI delegation; keep the supervising agent responsible for acceptance.
---

# Grok Delegation

Use Grok as an external headless worker. The supervising agent owns scope, interaction, verification, and the final report. Grok receives only the context supplied in the handoff.

The default transport is the bundled ACP bridge, `scripts/grok_acp.py`. It exposes actual tool events and explicit interaction requests while keeping raw protocol traffic out of the conversation. This is programmatic JSON-RPC, with no Grok TUI or dashboard.

## Prepare the handoff

1. Resolve the project directory and active `AGENTS.md`. Record branch, HEAD, status, and existing diff; preserve user changes and use separate worktrees for concurrent writers.
2. Check `command -v grok`, `grok --version`, `grok --help`, and `grok --no-auto-update --version` once per task. A missing help entry is not an argument rejection. The bridge requires Python 3 and a local Unix socket (macOS/Linux).
3. Tell the user that the grok skill is being used, with worker responsibility, `write` or `review` mode, cwd, and new or resumed ACP transport.
4. Create one private handoff directory outside the repository:

```bash
mkdir -p /tmp/codex-grok-cli
TASK_HOME="$(mktemp -d /tmp/codex-grok-cli/task.XXXXXX)"
```

Retain the exact returned path. Write `handoff.md` there using that same absolute path; do not substitute macOS `TMPDIR`.

Include objective, confirmed plan, cwd, write/review scope, relevant evidence, existing changes, forbidden side effects, tests, and acceptance criteria. Request changed files, commands, test results, unresolved questions, and risks. A review handoff forbids edits and requests `ACCEPT` or `REVISE` with exact evidence. State decisions already settled so Grok need not ask them again.

## Run and supervise

Resolve `BRIDGE` to `scripts/grok_acp.py` beside this installed `SKILL.md`. The run directory must be a **new** path; the bridge creates it with private permissions.

```bash
python3 "$BRIDGE" run \
  --cwd "<project-path>" \
  --prompt-file "$TASK_HOME/handoff.md" \
  --run-dir "$TASK_HOME/run"
```

Start in the foreground through the host's managed command tool, yielding within 30 seconds. Retain its managed `session_id` and poll it until exit. Keep the runner attached while questions are pending; an untracked background launch cannot deliver reliable completion.

The bridge starts an isolated `grok agent --always-approve --no-leader stdio` process with auto-update disabled. Existing Grok sessions and shared leaders are not its cleanup targets. Model and reasoning overrides use `--model` and `--reasoning-effort`; otherwise Grok's configured defaults apply. `--resume <session-id>` resumes the exact session from the same cwd and records only the new turn's answer.

ACP has no implicit model-turn limit. An optional `--timeout-seconds` sets an overall wall-clock limit, including time awaiting input. If a hard model-turn budget is required, use the direct fallback with `--max-turns`; the top-level CLI flag is not forwarded to `agent stdio` in the inspected implementation.

The existing maximum-capability policy remains: `--always-approve`, default sandbox behavior, and no invocation tool allowlist. Review mode is a handoff contract, not an enforced sandbox. Permission automation does not expand authorization; preserve scope for secrets, external messages, deletion, history changes, push, deployment, and production access. Configured deny rules, hooks, and managed requirements may still block calls.

For a deliberately supervised approval workflow or a callback test, `--ask-permissions` opts this invocation out of automatic approval. It does not force Grok to ask about operations its policy already permits; keep the default for ordinary delegation.

Use the bridge's `--help` for supported options. If an explicitly requested CLI option is not represented by ACP, preserve it using the [direct CLI fallback](references/direct-headless.md); report the resulting observability limitation instead of silently dropping the option. Headless capabilities, including plugins, MCP, web tools, and Grok-managed subagents, remain available when exposed by the installed CLI and authorized by the task.

### Read actual progress

Foreground output contains compact tool transitions, explicit questions, final receipt, and factual heartbeats. Query the same run when more detail is needed:

```bash
python3 "$BRIDGE" status --run-dir "$TASK_HOME/run"
python3 "$BRIDGE" events --run-dir "$TASK_HOME/run" --after 0 --limit 20
```

Advance the returned event cursor when fetching the next page. Use status and compact events for supervision; raw `protocol.jsonl` is for targeted diagnosis, not whole-file context ingestion. Give the user a meaningful update at least every 60 seconds, naming the current tool, its result, or the last observed activity.

`pending` tool status alone is not a request for approval. A quiet interval is only time since the last event, not proof of thinking, deadlock, or waiting for input. The bridge reports tool calls and subagent sessions that Grok actually emits; it cannot infer hidden subprocess progress or turn arbitrary stdin prompts into ACP requests.

### Answer an explicit request

When status reports a pending interaction, read its request ID and full parameters. Answer from the confirmed plan or repository evidence when possible. If a user decision is required, present that question and keep supervising the same managed process while awaiting the answer. Elapsed time never constitutes an answer.

Write the response as JSON outside the repository, then submit it to that exact request:

```bash
python3 "$BRIDGE" respond \
  --run-dir "$TASK_HOME/run" \
  --request-id "<request-id-from-status>" \
  --response-file "$TASK_HOME/response.json"
```

Permission responses use `{"optionId":"<an-option-id-from-the-request>"}`. Grok question responses use its question/answer schema. Read [ACP interactions and evidence](references/acp.md) for the exact shapes and protocol limits before answering a question. Unknown requests fail explicitly; they are never silently approved. Expired, duplicate, or cancelled requests must not be answered.

Some Grok toolsets do not expose the native question tool. If Grok asks a question in its final text instead, answer through an exact-session continuation after receipt. A model's statement that it asked is not evidence of a pending RPC; inspect the actual requests.

### Cancel or continue

```bash
python3 "$BRIDGE" cancel --run-dir "$TASK_HOME/run"
```

Cancellation targets only this run. Continue polling for its terminal state. If a cancelled or failed turn needs correction, create a new handoff/run directory and use `run --resume <session-id>` from the original cwd. Start a fresh Grok session if responsibility, repository, or worktree changes. A resumed call is separate from answering a request in a still-running turn.

## Receive and verify

Read `result.json` (`text`, `sessionId`, `stopReason`), `status.json`, and relevant stderr after the managed process exits. Receipt succeeds only with bridge exit `0`, `stopReason=end_turn`, and substantive nonempty text. An ACP server stays alive after a turn; its supervisor-initiated shutdown is distinct from turn completion. Cancellation, protocol errors, premature process exit, and exhausted turns are not success.

Receipt is not task acceptance. Inspect the diff, check scope and confirmed plan, run proportionate tests, and review any failed tools or unsupported claims. Ask Grok to correct concrete defects or make a minimal supervising-agent patch. Report Grok's work, independent verification, and remaining gaps separately. For review tasks, verify that the worktree stayed unchanged.

Retain artifacts through receipt and verification. Preserve failed, cancelled, or audit-requested runs and report their path. After a successful verified run, remove only that invocation's exact `TASK_HOME` child under `/tmp/codex-grok-cli/`, with a path check; never delete the shared root or use an exit trap. Keep any session ID needed for continuation before cleanup.
