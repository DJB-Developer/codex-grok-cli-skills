#!/usr/bin/env python3
"""Deterministic ACP peer for bridge integration tests. Never calls a model."""

import json
import os
from pathlib import Path
import sys


TRACE = Path(os.environ["FAKE_GROK_TRACE"])
prompt_id = None
scenario = None
pending_id = None
cancel_received = False
pending_cancelled = False
SECRET_SENTINEL = "FAKE_TOKEN_DO_NOT_LEAK_4f7c9e7a"


def send(message):
    print(json.dumps(message, ensure_ascii=False), flush=True)


def result(request_id, value):
    send({"jsonrpc": "2.0", "id": request_id, "result": value})


def update(value, session_id="fixture-session"):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": session_id, "update": value,
    }})


def text_chunk(text, session_id="fixture-session"):
    update({"sessionUpdate": "agent_message_chunk", "content": {
        "type": "text", "text": text,
    }}, session_id)


def finish(text="已完成。", reason="end_turn"):
    if text:
        text_chunk(text)
    result(prompt_id, {"stopReason": reason})


def start_prompt(message):
    global prompt_id, scenario, pending_id
    prompt_id = message["id"]
    scenario = message["params"]["prompt"][0]["text"].strip()
    if scenario == "malformed":
        print("{not-valid-json", flush=True)
        return
    if scenario == "early_eof":
        sys.exit(0)
    if scenario == "unknown_request":
        pending_id = 711
        send({"jsonrpc": "2.0", "id": pending_id,
              "method": "fixture/unsupported_callback", "params": {}})
        return
    if scenario in {"permission", "permission_hold"}:
        pending_id = 701
        send({"jsonrpc": "2.0", "id": pending_id,
              "method": "session/request_permission", "params": {
                  "sessionId": "fixture-session",
                  "toolCall": {"toolCallId": "permission-tool", "title": "Run fixture command"},
                  "options": [
                      {"optionId": "fixture-allow", "kind": "allow_once", "name": "Allow once"},
                      {"optionId": "fixture-reject", "kind": "reject_once", "name": "Reject"},
                  ],
              }})
        return
    if scenario == "secret_sentinel":
        print("SERVICE_TOKEN=" + SECRET_SENTINEL, file=sys.stderr, flush=True)
        secret_data = {
            "env": [{"name": "GITHUB_PERSONAL_ACCESS_TOKEN", "value": SECRET_SENTINEL},
                    {"name": "LANG", "value": "en_US.UTF-8"}],
            "headers": {"Authorization": "Bearer " + SECRET_SENTINEL,
                        "X-Api-Key": SECRET_SENTINEL},
            "nested": {"token": SECRET_SENTINEL, "api_key": SECRET_SENTINEL,
                       "accessToken": SECRET_SENTINEL,
                       "env": {"SERVICE_TOKEN": SECRET_SENTINEL, "LANG": "en_US.UTF-8"}},
        }
        send({"jsonrpc": "2.0", "method": "_x.ai/mcp/servers_updated", "params": secret_data})
        update({"sessionUpdate": "tool_call", "toolCallId": "safe-summary-tool",
                "title": "Inspect public fixture configuration", "kind": "read", "status": "in_progress",
                "rawInput": secret_data, "rawOutput": secret_data})
    if scenario in {"question", "secret_sentinel", "resolved_question"}:
        pending_id = 702
        send({"jsonrpc": "2.0", "id": pending_id,
              "method": "_x.ai/ask_user_question", "params": {
                  "method": "x.ai/ask_user_question", "params": {
                      "sessionId": "fixture-session", "toolCallId": "question-tool", "mode": "default",
                      "questions": [{"question": "Choose a route?", "multiSelect": False,
                                     "options": [{"label": "A", "description": "Route A"},
                                                 {"label": "B", "description": "Route B"}]}],
                  },
              }})
        if scenario == "resolved_question":
            send({"jsonrpc": "2.0", "method": "_x.ai/session_notification", "params": {
                "method": "x.ai/session_notification", "params": {
                    "sessionId": "fixture-session", "update": {
                        "sessionUpdate": "interaction_resolved", "tool_call_id": "question-tool",
                    },
                },
            }})
            pending_id = None
        return
    if scenario == "plan":
        pending_id = 703
        send({"jsonrpc": "2.0", "id": pending_id,
              "method": "_x.ai/exit_plan_mode", "params": {
                  "method": "x.ai/exit_plan_mode", "params": {
                      "sessionId": "fixture-session", "toolCallId": "plan-tool",
                      "planContent": "1. Read fixture.\n2. Verify fixture.",
                  },
              }})
        return
    if scenario == "empty_result":
        finish(text="")
        return
    if scenario == "non_end_turn":
        finish(reason="max_tokens")
        return
    if scenario == "many_chunks":
        for _ in range(1000):
            text_chunk("fragment-never-print-per-chunk;")
        result(prompt_id, {"stopReason": "end_turn"})
        return
    if scenario == "subagent":
        update({"sessionUpdate": "tool_call", "toolCallId": "child-tool", "kind": "read",
                "title": "Child reads its fixture", "status": "in_progress"}, "child-session")
        text_chunk("Child response must not enter the root answer.", "child-session")
        update({"sessionUpdate": "tool_call_update", "toolCallId": "child-tool",
                "status": "completed"}, "child-session")
        finish("Root response.")
        return
    update({"sessionUpdate": "tool_call", "toolCallId": "tool-1",
            "title": "Read fixture configuration", "kind": "read", "status": "pending",
            "rawInput": {"command": "cat fixture.txt"}})
    update({"sessionUpdate": "tool_call_update", "toolCallId": "tool-1",
            "locations": [{"path": "fixture.txt"}],
            "content": [{"type": "content", "content": {"type": "text", "text": "partial result"}}]})
    if scenario in {"pending_tool", "wait_forever"}:
        return
    update({"sessionUpdate": "tool_call_update", "toolCallId": "tool-1", "status": "in_progress"})
    update({"sessionUpdate": "tool_call_update", "toolCallId": "tool-1", "status": "completed"})
    text_chunk("你好")
    text_chunk("，ACP。")
    result(prompt_id, {"stopReason": "end_turn"})


for line in sys.stdin:
    message = json.loads(line)
    with TRACE.open("a", encoding="utf-8") as trace:
        trace.write(json.dumps(message, ensure_ascii=False) + "\n")
    method = message.get("method")
    if method == "initialize":
        if message["params"].get("clientCapabilities") != {}:
            send({"jsonrpc": "2.0", "id": message["id"], "error": {
                "code": -32602, "message": "Fixture rejects unimplemented client capabilities",
            }})
        else:
            result(message["id"], {"protocolVersion": 1,
                                   "agentCapabilities": {"loadSession": True},
                                   "authMethods": [{"id": "cached_token", "name": "Cached token"}]})
    elif method == "authenticate":
        result(message["id"], {})
    elif method == "session/load":
        text_chunk("历史消息，不能出现在本轮答案。")
        update({"sessionUpdate": "tool_call", "toolCallId": "historical-tool",
                "title": "Historical operation", "kind": "read", "status": "pending"})
        result(message["id"], {"sessionId": "fixture-session", "models": {"currentModelId": "fixture-model"}})
    elif method == "session/new":
        result(message["id"], {"sessionId": "fixture-session", "models": {"currentModelId": "fixture-model"}})
    elif method == "session/set_model":
        result(message["id"], {})
    elif method == "session/prompt":
        start_prompt(message)
    elif method == "session/cancel":
        cancel_received = True
        if scenario != "wait_forever" and (pending_id is None or pending_cancelled):
            finish(text="", reason="cancelled")
    elif message.get("id") == pending_id:
        if scenario == "unknown_request":
            continue
        response = message.get("result", {})
        outcome = response.get("outcome", {})
        pending_cancelled = outcome == "cancelled" or (
            isinstance(outcome, dict) and outcome.get("outcome") == "cancelled")
        if pending_cancelled:
            if cancel_received:
                finish(text="", reason="cancelled")
        elif "error" not in message:
            if scenario == "permission_hold":
                pending_id = None
                text_chunk("Permission accepted; continuing the same turn.")
            else:
                if scenario == "secret_sentinel":
                    update({"sessionUpdate": "tool_call_update", "toolCallId": "safe-summary-tool", "status": "completed"})
                finish("交互已接收。")
