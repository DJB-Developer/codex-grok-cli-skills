---
name: grok
description: Run a confirmed, bounded non-interactive task through the user's local Grok CLI, including planning, implementation, investigation, testing, tool use, and review. Use for explicit $grok or Grok CLI requests. Do not use for the interactive TUI or ordinary Grok product questions.
---

# Grok Delegation

Use the locally installed Grok CLI as a headless external worker while Codex remains responsible for scope, supervision, verification, and the final report. The CLI process is not a native subagent and cannot see the current conversation unless the handoff includes it.

## Activation

Activate when the user:

- invokes `$grok`;
- explicitly asks Grok to plan, complete, investigate, implement, test, use its available tools, or review a task;
- asks Codex to hand an already confirmed plan to Grok.

Do not activate merely because a task could benefit from another model.

When activated, tell the user that the `$grok` skill is being used and state the worker responsibility, mutability, working directory, and whether the transport is a new direct headless run, an exact-session resume, or ACP.

## Headless capability scope

Headless is the transport, not a reduced worker role. Allow every model-facing capability that the installed Grok CLI exposes in headless mode and that the task authorizes, including code edits, commands, tests, web and MCP tools, structured output, configured plugins or memory, and Grok-managed subagents. Do not maintain a task-type or tool allowlist in this skill.

Use `grok --help` and the relevant option help as the current syntax source of truth. Preserve explicitly requested model, reasoning, schema, tool, permission, and session options when they are supported and authorized. Do not disable web search, tools, plugins, memory, or subagents merely to simplify supervision.

Default multi-turn interaction in Codex App is a sequence of direct headless calls: return the first result, then send corrections or follow-up questions through `--resume <session-id>`. ACP is an optional headless protocol transport, not a human-facing UI. The Grok TUI, dashboard, and other human-facing interfaces are outside this skill.

## Preconditions

1. Resolve the exact project working directory and read the active `AGENTS.md` instructions.
2. Confirm the executable with `command -v grok`. When this installation has not been verified in the current task, run `grok --version` and `grok --help`; flags can change between CLI versions. Help output is not exhaustive: Grok 1.0.5 accepts the documented hidden `--no-auto-update` flag without listing it. Probe it with `grok --no-auto-update --version`. Keep the flag when that command exits `0`; omit it only after an explicit unknown/unrecognized-argument error, and record the incompatibility.
3. Inspect the target worktree's branch, HEAD, status, and existing diff. Preserve user changes. Multiple writers require separate worktrees when the active project rules require isolation.
4. Convert the relevant context into a self-contained handoff file outside the repository.

The handoff packet must contain:

- objective and worker responsibility;
- final confirmed plan, if one exists;
- project path, `write` or `review` mode, and allowed scope;
- relevant files, symptoms, evidence, and dependencies;
- constraints, existing user changes to preserve, and forbidden side effects;
- required tests and acceptance criteria;
- requested report: changed files, commands, test results, unresolved questions, and remaining risks.

For a review handoff, require `ACCEPT` or `REVISE` with exact evidence and the smallest necessary correction. Review mode must not edit business code.

## Default permission policy: maximum capability

The user has selected maximum capability as the default for local Grok headless delegation. For every direct new or resumed run:

- pass `--always-approve`;
- do not pass `--permission-mode dontAsk`, `--allow`, or `--deny`;
- do not pass `--sandbox`; Grok's default sandbox-off behavior is intentional here.

This avoids whole-command allowlist mismatches such as an allowed `git status*` rule silently rejecting `git status && git diff`. `write` and `review` remain responsibility contracts in the handoff, but review is not technically enforced as read-only by the CLI under this policy.

`--always-approve` is the documented `--yolo`/bypass path. Configured deny rules, trusted hooks, or managed requirements can still block calls. If a call is rejected, inspect the effective Grok configuration instead of falling back to `dontAsk` or widening an invocation allowlist.

Maximum capability does not expand the user's authorization. The handoff must still state the exact repository, intended scope, and forbidden external side effects. Under this policy Grok can technically read secrets, write outside the workspace, use the network, delete files, rewrite Git history, push, or deploy, so prompt constraints and the supervising Codex verification gate are advisory governance rather than a security boundary.

## Default transport: direct headless CLI

For a bounded task or implementation of an already confirmed plan, prefer direct headless CLI:

Ensure `/tmp/codex-grok-cli/` exists, create one private run directory with `mktemp -d /tmp/codex-grok-cli/<task-id>.XXXXXX`, retain the exact returned path, and write this invocation's handoff and logs inside it. Use that same absolute path in shell commands and file-writing tools; do not mix it with `${TMPDIR}`, which commonly resolves elsewhere on macOS.

```bash
TASK_RUN_DIR="<exact-run-directory-returned-by-mktemp>"
case "$TASK_RUN_DIR" in
  /tmp/codex-grok-cli/*) ;;
  *) echo "invalid task run directory: $TASK_RUN_DIR" >&2; exit 2 ;;
esac
HANDOFF="$TASK_RUN_DIR/handoff.md"
TASK_OUT="$TASK_RUN_DIR/result.json"
TASK_ERR="$TASK_RUN_DIR/stderr.log"
TASK_EXIT="$TASK_RUN_DIR/exit-status"
test -f "$HANDOFF" || { echo "handoff missing: $HANDOFF" >&2; exit 2; }
grok \
  --no-auto-update \
  --cwd "<project-path>" \
  --always-approve \
  --max-turns 25 \
  --output-format json \
  --prompt-file "$HANDOFF" \
  > "$TASK_OUT" 2> "$TASK_ERR"
TASK_STATUS=$?
printf '%s\n' "$TASK_STATUS" > "$TASK_EXIT"

# Return Grok's captured result to the supervising Codex process as well as
# retaining it on disk. If tool output is truncated, Codex can reread TASK_OUT.
test ! -s "$TASK_OUT" || sed -n '1,$p' "$TASK_OUT"
test ! -s "$TASK_ERR" || sed -n '1,160p' "$TASK_ERR" >&2
exit "$TASK_STATUS"
```

Default `--output-format json`: current Grok emits one final object containing at least `text`, `stopReason`, and `sessionId`. Do not default to `streaming-json`; it can produce a large ACP event log and requires event reconstruction. Investigation/review default `--max-turns 25`; implementation default `50`.

### Result receipt contract

The supervising Codex must keep the Grok process attached to a managed terminal session until it exits. This is what makes the final assistant text observable.

1. Start the command in the foreground with the runtime's managed command tool. Use a yield no longer than 30 seconds.
2. If the command tool returns a live `session_id`, retain that exact ID and poll it with empty-input `write_stdin` calls, again waiting at most 30 seconds per call. Send the user a brief progress update at least every 60 seconds while continuing to poll.
3. Do not use `&`, `nohup`, `disown`, an untracked background shell, or “launch then end the turn.” Those processes may be cleaned up, and their eventual file output is not automatically delivered back to Codex.
4. Do not start a duplicate Grok run merely because a poll has no new output. An empty poll means the final JSON has not been emitted yet.
5. When the managed process exits, inspect `$TASK_EXIT`, `$TASK_ERR`, and `$TASK_OUT`. The wrapper above echoes captured stdout into the managed tool result; if that result is truncated, read `$TASK_OUT` directly in chunks.
6. Parse the final object and explicitly extract the worker response:

```bash
jq -e . "$TASK_OUT" >/dev/null
jq -r '.text // empty' "$TASK_OUT"
jq -r '[.sessionId // "", .stopReason // ""] | @tsv' "$TASK_OUT"
```

Treat the delegation as successfully returned only when the managed terminal has finished, the recorded exit is `0`, the JSON parses, `.stopReason == "end_turn"`, and `.text` is non-empty and substantive. The `.text` field is Grok's answer; use it to supervise the task and report to the user rather than merely saying that Grok was started. Preserve the files when any check fails, diagnose the concrete failure, and resume or retry only with a specific correction.

Investigation and review use the same maximum-capability command. Mark them `review` in the handoff, explicitly forbid edits and side effects, and verify the worktree remained unchanged afterward.

For short prompts, `-p "<prompt>"` may replace `--prompt-file`. Prefer a prompt file for long plans so quoting and truncation do not corrupt the handoff.

Use `--no-auto-update` for scripted headless runs so background update checks cannot interfere. Capture stdout JSON and stderr in separate task-scoped files. Exit code `0` only means the CLI process exited normally: also inspect `stopReason` (`end_turn` is success). A missing `stopReason`, `cancelled`, or an unhandled failed tool call means the delegated task failed even when the process returned `0`.

### Artifact lifecycle

Keep the run directory until the managed process has exited and the result-receipt and Codex verification gates have finished. Then remove that exact run directory when the invocation succeeded and the user did not request retained evidence. Do not use an exit trap: it can delete stdout, stderr, or the handoff before the supervisor diagnoses the result.

Before cleanup, verify that the recorded path is a child of `/tmp/codex-grok-cli/` and is not the shared root. Remove only that exact `mktemp` directory:

```bash
case "$TASK_RUN_DIR" in
  /tmp/codex-grok-cli/*) rm -R -- "$TASK_RUN_DIR" ;;
  *) echo "refusing unsafe cleanup: $TASK_RUN_DIR" >&2; exit 2 ;;
esac
```

Preserve it when execution, parsing, `stopReason`, substantive-output, or verification checks fail, or when the user requests an audit trail; report the preserved path and reason. A resumed call gets a new run directory even though it reuses the Grok session.

## Continuation

Direct headless sessions support ordinary multi-turn correction. Resume the recorded session from the same working directory and repeat the maximum-capability flags:

```bash
grok \
  --no-auto-update \
  --cwd "<project-path>" \
  --resume "<session-id>" \
  --always-approve \
  --max-turns 25 \
  --output-format json \
  --prompt-file "<follow-up-file>"
```

Run continuations through the same managed-session, result-receipt, and artifact-lifecycle loop. Use `--continue` only when selecting the most recent session for that directory is unambiguous. Start a new session when responsibility, permissions, repository, or worktree changes.

## Optional headless protocol: ACP

ACP is programmatic JSON-RPC over stdin/stdout, not a terminal UI. Keep direct headless JSON as the default. Use ACP only when the user explicitly requests it or an integration needs a long-lived session, streamed agent events, or protocol-level control and the current runtime has a working ACP client.

```bash
grok --no-auto-update --always-approve agent stdio
```

The client must initialize and authenticate, create or load the session, send `session/prompt`, consume `session/update`, concatenate `content.text` from `agent_message_chunk` updates, check the returned `stopReason`, and own process termination. The `session/prompt` response is completion metadata; it is not the assistant body.

When no working ACP client is available, use direct headless JSON plus exact-session `--resume`. Do not substitute the Grok TUI or hand-written terminal interaction for a missing protocol client.

## Execution rules

- Set the intended project with `--cwd` and keep the operational scope explicit in the handoff. Under the selected maximum-capability policy, `--cwd` is context rather than a filesystem boundary.
- Permission automation does not expand authorization. Keep unrelated deletion, history rewriting, secrets, external messages, push, deploy, and production changes outside the handoff unless the user explicitly authorized them.
- Use direct headless, exact-session resume, or ACP with a real client; keep human-facing Grok interfaces out of the Codex App delegation path.
- Keep one writer per worktree. Stop the writer before an independent test or review process operates on that worktree when project rules require it.
- If Grok asks a question that can be answered from the confirmed plan or repository evidence, answer it without interrupting the user. Ask the user only when the missing choice would materially change scope or outcome.
- If Grok fails, preserve its output, diagnose the failure, and retry only with a concrete correction.

## Codex verification gate

Grok completing the process is not the final acceptance signal. Codex must:

1. inspect the resulting diff and changed files;
2. check that every confirmed plan item was addressed;
3. run focused tests or other proportionate verification;
4. identify unrelated changes, regressions, skipped tests, or unsupported claims;
5. ask Grok to correct issues or make a minimal Codex patch when appropriate;
6. provide one consolidated final report distinguishing Grok's work, Codex verification, test results, and remaining risks.
