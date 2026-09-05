# Direct CLI fallback

Use when a requested CLI feature cannot be expressed through the bundled ACP bridge, or a diagnosed ACP incompatibility requires the direct transport. Verify flags using the installed `grok --help`; preserve the user's model, reasoning, tools, schema, plugin, permission, and session choices. Report why this run uses the fallback.

Use the same handoff, private directory, foreground managed session, scope, maximum-capability policy, and verification gate as `SKILL.md`. Direct investigation/review defaults to `--max-turns 25`; implementation defaults to `50`, unless the user requests otherwise. ACP limits must not be assumed to inherit direct-mode flags.

For progress visibility, prefer `--output-format streaming-json`. It is one native ACP update per NDJSON line, not a single final JSON object. Keep stdout and stderr separate. Read incrementally by a retained cursor and reconstruct the assistant message and actual terminal event according to the installed version. A changed output format also requires changed result parsing.

When a feature such as `--json-schema` requires `--output-format json`, the CLI emits a single final object and intermediate progress is unavailable on stdout. State that limitation. The non-streaming receipt pattern remains:

```bash
grok \
  --no-auto-update \
  --cwd "<project-path>" \
  --always-approve \
  --max-turns 25 \
  --output-format json \
  --prompt-file "$TASK_HOME/handoff.md" \
  > "$TASK_HOME/result.json" 2> "$TASK_HOME/stderr.log"
TASK_STATUS=$?
printf '%s\n' "$TASK_STATUS" > "$TASK_HOME/exit-status"
cat "$TASK_HOME/result.json"
exit "$TASK_STATUS"
```

After the managed process exits, inspect the recorded code, stderr, and final result's `text`, `sessionId`, and `stopReason`. Success requires exit `0`, parseable JSON, `stopReason=end_turn`, and substantive text. Keep failed artifacts. A normal process exit or zero-byte output while running is not task acceptance.

For continuation, add `--resume "<exact-session-id>"`, keep the original cwd, and create a new handoff directory. Use `--continue` only when the latest session is unambiguous. Direct output streaming is not a bidirectional interaction channel; use the ACP bridge when answering an in-flight request is required.
