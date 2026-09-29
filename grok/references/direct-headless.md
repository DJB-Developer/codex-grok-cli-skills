# Direct CLI fallback

Use when a requested CLI feature cannot be expressed through the bundled ACP bridge, or a diagnosed ACP incompatibility requires the direct transport. Verify flags using the installed `grok --help`; preserve the user's model, reasoning, tools, schema, plugin, permission, and session choices. Report why this run uses the fallback.

Use the same handoff, private directory, foreground managed session, scope, maximum-capability policy, and verification gate as `SKILL.md`. Direct investigation/review defaults to `--max-turns 25`; implementation defaults to `50`, unless the user requests otherwise. `--max-turns`, `--tools`, and `--disallowed-tools` apply to this headless transport only. ACP limits must not be assumed to inherit direct-mode flags.

Checked against Grok `1.0.41` help text and the [headless](https://docs.x.ai/build/cli/headless-scripting), [CLI reference](https://docs.x.ai/build/cli/reference), [sessions](https://docs.x.ai/build/features/sessions), and [enterprise](https://docs.x.ai/build/enterprise) pages. Where the headless table says `-s/--session-id` creates or resumes, follow `grok --help` and the Sessions page: `-s` only names a new UUID.

## Output

`-p/--single`, `--prompt-file`, and `--prompt-json` each start headless mode. Piped stdin is not the prompt. `--no-auto-update` still belongs on automated runs.

For progress visibility, prefer `--output-format streaming-json`. It is one native ACP update per NDJSON line, not a single final JSON object. Keep stdout and stderr separate. Read incrementally by a retained cursor and reconstruct the assistant message and actual terminal event according to the installed version. A changed output format also requires changed result parsing.

`streaming-messages-json` is Anthropic Messages API NDJSON, not ACP updates. `--include-partial-messages` affects only that format. `plain` is human-readable text with no structured receipt.

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

Apply the exact-continuation rule in `SKILL.md`: `--resume "<uuid-from-result.json>"`, the original cwd, and a new handoff directory. `--continue` is only for an unambiguous latest session in the directory. A non-UUID `--resume` value matches a session title and can select the wrong session. Sessions are stored under `~/.grok/sessions/`.

`--permission-mode` accepts `default`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`, and `plan`. Keep `SKILL.md`'s `--always-approve` policy unless the user requested another mode. `dontAsk` denies tool calls that lack an allow rule; `acceptEdits` auto-approves file edits. Pass `--sandbox <profile>` only when the user requested a profile. The managed always-approve lock is handled in `SKILL.md`.

Direct output streaming is not a bidirectional interaction channel; use the ACP bridge when answering an in-flight request is required.
