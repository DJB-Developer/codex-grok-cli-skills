# Direct noninteractive fallback

Use for built-in review, older CLI versions, or requested options outside the app-server bridge. Check `codex exec --help` and the relevant subcommand help first. `exec --json` emits progress but has no native bidirectional question/approval channel in the inspected version. Use exact-session follow-ups and disclose this limit before launch.

Preserve explicitly requested models, features, providers, images, schemas, tools, and integrations. Use `--add-dir` only for authorized writable directories and `--skip-git-repo-check` only for an intentional non-Git directory. Use automatic review only when supported and appropriate to the authorized write scope; never substitute unrestricted execution for an approval failure.

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
2. If the command tool returns a live session ID, retain it and poll that same session with empty input until the process exits. Incrementally read complete new JSONL records using a saved line cursor while it runs. Report observed tool starts, updates, and completions rather than waiting until exit to discover progress. Keep the user updated at least every 60 seconds; a quiet interval is not proof of blocking.
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

Resume an exact session only when responsibility, permissions, repository, and working directory remain unchanged. Use new task-scoped log, stderr, exit, and last-message files. Launch from the original working directory and reapply permissions with supported configuration overrides; the inspected 0.152.0 resume help lacks `-C`, `--sandbox`, and `--approve-for-me`:

```bash
codex exec resume \
  -c 'sandbox_mode="read-only"' \
  -c 'approval_policy="on-request"' \
  -c 'approvals_reviewer="user"' \
  --json \
  --output-last-message "$LAST_MSG" \
  "<session-id>" \
  - < "<follow-up-file>" \
  > "$TASK_LOG" 2> "$TASK_ERR"
```

For the same authorized implementation scope, use `sandbox_mode="workspace-write"` and `approvals_reviewer="auto_review"`. Verify configuration keys against the installed version before relying on them. Use exact IDs when other sessions may exist. Start a new session when scope or authority changes. Run continuations through the same managed result-receipt and artifact-lifecycle loop.

## Fixed-point review

Choose one transport before launch:

- For built-in review instructions, use `codex exec review` with exactly one of `--uncommitted`, `--base <branch>`, or `--commit <sha>`, and no custom prompt.
- For a custom checklist or required `ACCEPT`/`REVISE`, use ordinary `codex exec --sandbox read-only`; put the fixed point and comparison command in the handoff.

This routing avoids CLI-version-dependent conflicts between review target flags and a custom prompt. A parser exit is a dispatch failure, not a review attempt.

## Verification gate

The supervising agent must inspect the resulting status and diff, verify every handoff requirement and authorization boundary, run proportionate checks after the worker stops, identify unrelated changes or unsupported claims, and provide one final report that distinguishes worker output from supervisor verification.
