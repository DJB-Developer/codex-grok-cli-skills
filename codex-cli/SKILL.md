---
name: codex-cli
description: Delegate a bounded task to the local Codex CLI with live tool status, questions, approvals, cancellation, and exact-session continuation. Use for explicit $codex, codex-cli, or local Codex delegation. From Codex itself, use only for an explicitly requested separate worker, independent review, or CLI-specific reproduction.
---

# Codex CLI Delegation

Use the local Codex CLI as an external worker. The supervising agent supplies context, handles interaction, and verifies the result. This is the shared, host-neutral transport skill; host-specific routers may select it.

The default transport is `scripts/codex_app_server.py`, an attached, private app-server client. It carries native progress and interaction events without launching the TUI or connecting to a shared daemon. `codex exec --json` remains the [direct fallback](references/direct-headless.md) for built-in review, older versions, or requested options outside the bridge.

## Prepare

1. Resolve the project directory and active `AGENTS.md`. Inspect branch, HEAD, status, and existing diff; preserve user changes and keep concurrent writers in separate worktrees.
2. Verify `command -v codex`, `codex --version`, and `codex app-server --help` once per task. The bridge requires Python 3.10+ and Unix sockets (macOS/Linux). For protocol changes, the installed CLI's `app-server generate-json-schema` is the version-specific contract.
3. Tell the user the worker responsibility, read/write scope, cwd, and new or resumed transport. When already running as Codex, delegate only for the explicit worker/review/reproduction purposes in the description.
4. Create a private handoff directory outside the repository:

```bash
mkdir -p /tmp/codex-grok-cli
TASK_HOME="$(mktemp -d /tmp/codex-grok-cli/task.XXXXXX)"
```

Retain the exact returned path. Write `handoff.md` there, including objective, cwd, scope, known decisions, evidence, changes to preserve, forbidden side effects, tests, and acceptance criteria. Reviews request `ACCEPT` or `REVISE` with concrete evidence. The worker cannot see the parent conversation unless its context is in this handoff.

## Run

Resolve `BRIDGE` to the script beside this installed skill. The bridge creates `--run-dir`; that path must not already exist.

```bash
python3 "$BRIDGE" run \
  --cwd "<project-path>" \
  --prompt-file "$TASK_HOME/handoff.md" \
  --run-dir "$TASK_HOME/run" \
  --enable-user-input \
  --sandbox read-only
```

For authorized implementation or tests requiring artifacts, use `--sandbox workspace-write --approval-reviewer auto_review`. For a workflow in which the supervising agent handles native approvals, use `--approval-reviewer user`. Keep reviews and investigations read-only. Permission automation does not expand the handoff's authority; never substitute unrestricted execution for an approval failure.

Use `--model` and `--reasoning-effort` only for requested overrides; otherwise retain configured defaults. `--resume <session-id>` continues the exact session and reapplies the invocation's cwd, sandbox, and approval settings. Model and reasoning overrides also apply to resumed turns. Default collaboration mode permits implementation; select `--collaboration-mode plan` for an actual planning task, not merely to make a question tool appear.

`--enable-user-input` enables native question tools for this child process, including default-mode questions on the verified CLI version. It does not change global configuration. Omit it when the user's task explicitly requires the configured feature set unchanged; use final-text questions and exact-session continuation if the tool is unavailable.

Preserve authorized tools, MCP integrations, features, providers, images, structured output, and other explicitly requested capabilities. Use the bridge's `run --help` to check supported options; preserve unsupported options through the direct fallback and disclose its interaction limit instead of silently dropping them.

Start in the foreground through the host's managed command tool and yield within 30 seconds. Retain the returned managed session ID and poll that same process until exit, including while input is pending. An empty poll is not a reason to launch another worker. Use the bridge's `--timeout-seconds` only when a wall-clock limit is intended; it also counts time awaiting input.

## Supervise actual state

Foreground output contains compact tool transitions, plans, requests, heartbeats, and final receipt. Read more detail from the same run:

```bash
python3 "$BRIDGE" status --run-dir "$TASK_HOME/run"
python3 "$BRIDGE" events --run-dir "$TASK_HOME/run" --after 0 --limit 20
```

Advance the returned cursor for subsequent event pages. Give meaningful user updates at least every 60 seconds, naming observed commands, tool results, pending questions, or elapsed time since activity. Use compact events and status for ordinary supervision; inspect private protocol logs only for targeted diagnosis.

Silence does not prove thinking, deadlock, or waiting for approval. `pending_requests` is the actual reply channel. A nonblocking question may remain answerable while the worker continues; distinguish it from a blocking request. Native child-agent items expose only the state the CLI emits. Arbitrary stdin prompts inside commands are not automatically converted into native questions.

## Respond, steer, cancel

Read the exact pending request and resolve it from the confirmed handoff or repository evidence when possible. If the user must decide, relay the question in the current host conversation and keep supervising the same run while awaiting the answer. Time elapsed and preselected options are not answers or approvals.

Read [native interactions](references/app-server.md) before responding: Codex questions use question IDs, and their response schema differs from Grok ACP.

```bash
python3 "$BRIDGE" respond \
  --run-dir "$TASK_HOME/run" \
  --request-id "<request-id-from-status>" \
  --response-file "$TASK_HOME/response.json"
```

Expired, duplicate, and cancelled requests are rejected. The runner does not choose answers or grant permission. Unknown protocol requests fail explicitly with their method name.

When the user supplies a correction to a still-active turn, send it through native steering:

```bash
python3 "$BRIDGE" steer --run-dir "$TASK_HOME/run" --text-file "$TASK_HOME/correction.md"
python3 "$BRIDGE" cancel --run-dir "$TASK_HOME/run"
```

Steering adds input to the current turn; it does not answer a pending question or start another turn. Cancellation interrupts only this run; continue polling for its terminal receipt. After a turn finishes, use a new handoff/run directory with the exact session ID to continue. Start a fresh session if the repository, worktree, responsibility, or authority changes.

Question availability depends on the installed CLI and collaboration mode/features. If the worker instead asks in its final text, return that question and use exact-session continuation after the answer. A sentence saying it asked is not evidence of a live protocol request.

## Receive and verify

Receipt requires managed process exit `0`, `status.json` state `succeeded`, `result.json` stop reason `completed`, and substantive final assistant text. `turn/start` acknowledges launch; only `turn/completed` establishes turn completion. Supervisor shutdown of the app-server is separate from that result. Failed tools remain evidence to inspect even if the turn completed.

Independently inspect the resulting diff and scope, verify handoff requirements, and run proportionate checks after the worker stops. Report worker output, supervisor verification, and remaining runtime gaps separately. For read-only reviews, verify the worktree stayed unchanged.

Retain run artifacts through receipt and verification. Preserve failed, cancelled, or audit-requested runs and report their exact path. After successful verification, remove only the invocation's recorded `TASK_HOME` child under `/tmp/codex-grok-cli/`, after checking it is not the shared root. Do not use an exit trap; keep any needed session ID before cleanup.
