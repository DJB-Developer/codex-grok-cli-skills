---
name: codex-cli
description: Run a confirmed, bounded non-interactive task through the user's local Codex CLI, including planning, implementation, investigation, testing, tool use, and review. Use for explicit $codex, codex-cli, or local Codex requests. Do not recursively delegate from Codex unless the user explicitly requests a separate CLI worker.
---

# Codex CLI Delegation

Use the locally installed Codex CLI as a headless external worker while the supervising agent retains responsibility for scope, authorization, verification, and the final report. The CLI process cannot see the current conversation unless the handoff includes the relevant context.

This is the shared, host-neutral Codex CLI skill. Host-specific routers may select the worker and mode, but this file owns the direct Codex transport contract.

## Activation

Activate when the user explicitly invokes `$codex`, asks for `codex-cli` or local Codex, or asks another agent to delegate any bounded non-interactive task to Codex CLI.

When the current runtime is already Codex, complete the task directly by default. Use this skill there only when the user explicitly requests a separate local Codex process, independent review, or CLI-specific reproduction.

When activated, tell the user the worker responsibility, mutability, working directory, and whether this is a new headless run or an exact-session resume.

## Headless capability scope

Headless is the transport, not a reduced worker role. Allow every model-facing capability that the installed `codex exec` exposes and that the task authorizes, including planning, code edits, commands, tests, images, web search, MCP tools, structured output, configured plugins or features, local providers, and configured subagents. Do not maintain a task-type or tool allowlist in this skill.

Use `codex exec --help` and the relevant exec subcommand help as the current syntax source of truth. Preserve explicitly requested model, reasoning, image, schema, search, feature, provider, permission, and session options when they are supported and authorized. Do not disable tools, configured integrations, or subagents merely to simplify supervision.

Multi-turn interaction is a sequence of headless calls: return the first result, then send corrections or follow-up questions through `codex exec resume <session-id>`. The Codex TUI, Desktop launcher, and other human-facing interfaces are outside this skill.

## Preconditions

1. Resolve the exact project directory and read its active `AGENTS.md` instructions.
2. Confirm `command -v codex`. If the installation has not been verified in the current task, run `codex --version` and the relevant `codex exec ... --help`; installed CLI help is the syntax source of truth.
3. Inspect branch, HEAD, status, and existing diff. Preserve user changes and keep one writer per worktree.
4. Create a self-contained handoff outside the repository. Use stdin for long prompts.

The handoff must include the objective, worker responsibility, project path, allowed scope, relevant evidence, existing changes to preserve, forbidden side effects, required verification, acceptance criteria, and requested report. Reviews must return `ACCEPT` or `REVISE` with concrete evidence and the smallest necessary correction.

## Permissions

- Use `--sandbox read-only` for investigation and review.
- Use `--sandbox workspace-write` for authorized implementation and tests that create build artifacts or caches.
- Use `--approve-for-me` only when current `codex exec --help` exposes it and the authorized write run must remain unattended.
- Add `--add-dir` only for explicitly in-scope writable directories.

These are defaults, not capability bans. Honor a different supported sandbox or approval option only when the user explicitly requests it and the environment satisfies its documented safety assumptions. Do not substitute `--dangerously-bypass-approvals-and-sandbox` for a missing safe approval path. Use `--skip-git-repo-check` only for an intentionally non-Git directory. Permission automation does not expand user authorization.

## Direct headless transport

Ensure `/tmp/codex-grok-cli/` exists, create one private run directory with `mktemp -d /tmp/codex-grok-cli/<task-id>.XXXXXX`, retain the exact returned path, and write this invocation's handoff and logs inside it. Do not mix that absolute path with `${TMPDIR}`, which commonly resolves elsewhere on macOS.

```bash
TASK_RUN_DIR="<exact-run-directory-returned-by-mktemp>"
case "$TASK_RUN_DIR" in
  /tmp/codex-grok-cli/*) ;;
  *) echo "invalid task run directory: $TASK_RUN_DIR" >&2; exit 2 ;;
esac
HANDOFF="$TASK_RUN_DIR/handoff.md"
TASK_LOG="$TASK_RUN_DIR/events.jsonl"
LAST_MSG="$TASK_RUN_DIR/last-message.md"
TASK_ERR="$TASK_RUN_DIR/stderr.log"
TASK_EXIT="$TASK_RUN_DIR/exit-status"
test -f "$HANDOFF" || { echo "handoff missing: $HANDOFF" >&2; exit 2; }
codex exec \
  -C "<project-path>" \
  --sandbox workspace-write \
  --approve-for-me \
  --json \
  --output-last-message "$LAST_MSG" \
  - < "$HANDOFF" \
  > "$TASK_LOG" 2> "$TASK_ERR"
TASK_STATUS=$?
printf '%s\n' "$TASK_STATUS" > "$TASK_EXIT"

# Return the final worker message to the supervising process and retain files.
test ! -s "$LAST_MSG" || sed -n '1,$p' "$LAST_MSG"
test ! -s "$TASK_ERR" || sed -n '1,160p' "$TASK_ERR" >&2
exit "$TASK_STATUS"
```

For `review`, replace `workspace-write` with `read-only` and omit `--approve-for-me`.

### Result receipt contract

1. Run Codex CLI in the foreground through the host's managed command tool, yielding for at most 30 seconds at a time.
2. If the command tool returns a live session ID, retain it and poll that same session with empty input until the process exits. Keep the user updated at least every 60 seconds.
3. Do not use `&`, `nohup`, `disown`, an untracked background shell, or “launch then end the turn.” Do not start a duplicate run after an empty poll.
4. After exit, inspect `$TASK_EXIT`, `$TASK_ERR`, `$LAST_MSG`, and relevant JSONL failure events. If tool output is truncated, read the saved files in chunks.

Treat the worker response as returned only when the managed process has exited, the recorded exit is `0`, `$LAST_MSG` is non-empty and substantive, and the JSONL has no unresolved `turn.failed` or `error` event. CLI completion is evidence, not final acceptance.

### Artifact lifecycle

Keep the run directory until the managed process has exited and the result-receipt and supervisor verification gates have finished. Then remove that exact run directory when the invocation succeeded and the user did not request retained evidence. Do not use an exit trap: it can delete the handoff or diagnostics before they are inspected.

Before cleanup, verify that the recorded path is a child of `/tmp/codex-grok-cli/` and is not the shared root. Remove only that exact `mktemp` directory:

```bash
case "$TASK_RUN_DIR" in
  /tmp/codex-grok-cli/*) rm -R -- "$TASK_RUN_DIR" ;;
  *) echo "refusing unsafe cleanup: $TASK_RUN_DIR" >&2; exit 2 ;;
esac
```

Preserve it when execution, JSONL, final-message, substantive-output, or verification checks fail, or when the user requests an audit trail; report the preserved path and reason. A resumed call gets a new run directory even though it reuses the Codex session.

## Continuation

Resume an exact session only when responsibility, permissions, repository, and working directory remain unchanged. Use new task-scoped log, stderr, exit, and last-message files:

```bash
codex exec resume \
  --json \
  --output-last-message "$LAST_MSG" \
  "<session-id>" \
  - < "<follow-up-file>" \
  > "$TASK_LOG" 2> "$TASK_ERR"
```

Use `--last` only when the most recent session is unambiguous. Start a new session when scope or authority changes. Run continuations through the same managed result-receipt and artifact-lifecycle loop.

## Fixed-point review

Choose one transport before launch:

- For built-in review instructions, use `codex exec review` with exactly one of `--uncommitted`, `--base <branch>`, or `--commit <sha>`, and no custom prompt.
- For a custom checklist or required `ACCEPT`/`REVISE`, use ordinary `codex exec --sandbox read-only`; put the fixed point and comparison command in the handoff.

This routing avoids CLI-version-dependent conflicts between review target flags and a custom prompt. A parser exit is a dispatch failure, not a review attempt.

## Verification gate

The supervising agent must inspect the resulting status and diff, verify every handoff requirement and authorization boundary, run proportionate checks after the worker stops, identify unrelated changes or unsupported claims, and provide one final report that distinguishes worker output from supervisor verification.
