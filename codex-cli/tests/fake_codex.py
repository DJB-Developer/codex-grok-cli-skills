#!/usr/bin/env python3
"""A deterministic Codex app-server peer. Uses no account, network, or model."""

import json
import os
from pathlib import Path
import sys


TRACE = Path(os.environ["FAKE_CODEX_TRACE"])
SECRET = "FAKE_CODEX_TOKEN_DO_NOT_LEAK_f9845d"
THREAD = "fixture-thread"
TURN = "fixture-turn"
initialized = False
scenario = None
pending_id = None
active_items = []


def send(message):
    print(json.dumps(message, ensure_ascii=False), flush=True)


def result(request_id, value):
    send({"id": request_id, "result": value})


def notify(method, params):
    send({"method": method, "params": params})


def item_event(item, completed=False, thread=THREAD, turn=TURN):
    method = "item/completed" if completed else "item/started"
    timestamp = "completedAtMs" if completed else "startedAtMs"
    notify(method, {"threadId": thread, "turnId": turn, "item": item, timestamp: 1000})


def agent_message(text, phase="final_answer", item_id="answer-1"):
    return {"type": "agentMessage", "id": item_id, "text": text, "phase": phase}


def delta(text, item_id="answer-1", thread=THREAD, turn=TURN):
    notify("item/agentMessage/delta", {"threadId": thread, "turnId": turn, "itemId": item_id, "delta": text})


def finish(text="完成。", status="completed", turn=TURN, thread=THREAD):
    items = [agent_message(text)] if text else []
    for item in items:
        item_event(item, completed=True, thread=thread, turn=turn)
    value = {"id": turn, "status": status, "items": items}
    if status == "failed":
        value["error"] = {"message": "Fixture turn failure", "codexErrorInfo": None, "additionalDetails": None}
    notify("turn/completed", {"threadId": thread, "turn": value})


def tools():
    return [
        {"type": "commandExecution", "id": "command-1", "command": "printf fixture", "cwd": os.getcwd(),
         "commandActions": [], "status": "inProgress", "processId": "fixture-process"},
        {"type": "fileChange", "id": "file-1", "status": "inProgress",
         "changes": [{"path": "/fixture/example.txt", "kind": {"type": "update"}, "diff": "+fixture"}]},
        {"type": "mcpToolCall", "id": "mcp-1", "server": "fixture", "tool": "lookup", "arguments": {"query": "public"},
         "status": "inProgress"},
        {"type": "collabAgentToolCall", "id": "collab-1", "tool": "spawnAgent", "senderThreadId": THREAD,
         "receiverThreadIds": ["child-thread"], "agentsStates": {"child-thread": {"status": "running"}}, "status": "inProgress"},
    ]


def thread_value(history=False):
    return {"id": THREAD, "sessionId": "fixture-session", "cliVersion": "fixture", "createdAt": 1,
            "updatedAt": 1, "cwd": os.getcwd(), "ephemeral": False, "modelProvider": "openai", "preview": "fixture",
            "projectId": None, "source": "exec", "status": {"type": "idle"},
            "turns": [{"id": "historical-turn", "status": "completed", "items": [agent_message("Historical answer.")]}] if history else []}


def start_turn(message):
    global scenario, pending_id, active_items
    scenario = message["params"]["input"][0]["text"].strip()
    if scenario == "question_resolved_before_ack":
        send({"id": 601, "method": "item/tool/requestUserInput", "params": {
            "threadId": THREAD, "turnId": TURN, "itemId": "early-question", "isBlocking": True,
            "questions": [{"id": "q-route", "header": "Route", "question": "Which visible route?",
                           "options": [{"label": "A", "description": "Route A"}]}],
        }})
        notify("serverRequest/resolved", {"threadId": THREAD, "requestId": 601})
    result(message["id"], {"turn": {"id": TURN, "status": "inProgress", "items": []}})
    notify("turn/started", {"threadId": THREAD, "turn": {"id": TURN, "status": "inProgress", "items": []}})
    if scenario in {"ack_only", "wait_forever", "question_resolved_before_ack"}:
        return
    if scenario == "malformed":
        print("{bad-json", flush=True)
        return
    if scenario == "early_eof":
        sys.exit(0)
    if scenario == "unknown_request":
        send({"id": 603, "method": "fixture/unsupported_callback", "params": {}})
        return
    if scenario in {"failed", "interrupted", "empty"}:
        finish(text="" if scenario == "empty" else "Incomplete answer.", status="completed" if scenario == "empty" else scenario)
        return
    if scenario == "commentary_only":
        comment = agent_message("Only progress, no final answer.", phase="commentary", item_id="progress-only")
        item_event(comment, completed=True)
        notify("turn/completed", {"threadId": THREAD, "turn": {"id": TURN, "status": "completed", "items": [comment]}})
        return
    if scenario == "items":
        active_items = tools()
        for item in active_items:
            item_event(item)
        for index in range(8):
            notify("item/commandExecution/outputDelta", {"threadId": THREAD, "turnId": TURN,
                   "itemId": "command-1", "delta": f"fixture-output-{index}\n"})
        notify("turn/plan/updated", {"threadId": THREAD, "turnId": TURN,
               "plan": [{"step": "Root remains active", "status": "inProgress"}]})
        child_tool = {"type": "commandExecution", "id": "child-command-running", "command": "printf child",
                      "cwd": os.getcwd(), "commandActions": [], "status": "inProgress"}
        item_event(child_tool, thread="child-thread", turn="child-turn")
        send({"id": 605, "method": "item/tool/requestUserInput", "params": {
            "threadId": "child-thread", "turnId": "child-turn", "itemId": "child-question", "isBlocking": True,
            "questions": [{"id": "child-question-id", "header": "Child", "question": "A now-expired child question?"}],
        }})
        delta("Child answer must not enter the root result.", thread="child-thread", turn="child-turn")
        item_event(agent_message("Child final answer."), completed=True, thread="child-thread", turn="child-turn")
        notify("turn/completed", {"threadId": "child-thread", "turn": {
            "id": "child-turn", "status": "completed", "items": [
                {**child_tool, "id": "child-tool-terminal-item", "status": "completed"},
                {"type": "plan", "id": "child-plan", "text": "Child plan must not replace root plan."},
                agent_message("Child terminal answer."),
            ],
        }})
        return
    if scenario in {"question_blocking", "question_nonblocking", "question_resolved", "question_secret"}:
        pending_id = 601
        questions = ([{"id": "q-secret", "header": "Secret", "question": "Enter a fixture secret.",
                       "isSecret": True, "options": None}] if scenario == "question_secret" else
                     [{"id": "q-route", "header": "Route", "question": "Which visible route?",
                       "options": [{"label": "A", "description": "Route A"}, {"label": "B", "description": "Route B"}]}])
        send({"id": pending_id, "method": "item/tool/requestUserInput", "params": {
            "threadId": THREAD, "turnId": TURN, "itemId": "question-1", "isBlocking": scenario != "question_nonblocking",
            "questions": questions,
        }})
        if scenario == "question_resolved":
            notify("serverRequest/resolved", {"threadId": THREAD, "requestId": pending_id})
            pending_id = None
        return
    if scenario == "elicitation":
        pending_id = 604
        send({"id": pending_id, "method": "mcpServer/elicitation/request", "params": {
            "threadId": THREAD, "turnId": TURN, "serverName": "fixture-mcp", "mode": "form",
            "message": "Choose a fixture name.", "requestedSchema": {
                "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
            },
        }})
        return
    if scenario in {"permission_command", "permission_file", "permission_profile"}:
        pending_id = 602
        params = {"threadId": THREAD, "turnId": TURN, "itemId": "approval-1", "startedAtMs": 1000}
        if scenario == "permission_command":
            params.update(command="printf fixture", cwd=os.getcwd(), availableDecisions=["accept", "decline", "cancel"])
        if scenario == "permission_profile":
            params.update(cwd=os.getcwd(), permissions={"fileSystem": {"write": ["/fixture/allowed"]}, "network": {"enabled": False}})
        method = {"permission_command": "item/commandExecution/requestApproval",
                  "permission_file": "item/fileChange/requestApproval",
                  "permission_profile": "item/permissions/requestApproval"}[scenario]
        send({"id": pending_id, "method": method, "params": params})
        return
    if scenario == "sentinel":
        print("SERVICE_TOKEN=" + SECRET, file=sys.stderr, flush=True)
        secrets = {"env": [{"name": "API_TOKEN", "value": SECRET}], "headers": {"Authorization": "Bearer " + SECRET},
                   "nested": {"accessToken": SECRET, "api_key": SECRET}}
        notify("fixture/unknown_notification", secrets)
        item = {"type": "mcpToolCall", "id": "mcp-secret", "server": "fixture", "tool": "safe_lookup",
                "arguments": secrets, "status": "completed"}
        item_event(item, completed=True)
        finish("Public result.")
        return
    if scenario == "many_chunks":
        text = "retain-this-text-without-printing-every-delta;" * 1000
        for _ in range(1000):
            delta("retain-this-text-without-printing-every-delta;")
        finish(text)
        return
    if scenario == "resume_history":
        item_event(agent_message("Late historical final answer."), completed=True, turn="historical-turn")
        finish("Wrong historical turn.", turn="historical-turn")
    item_event(agent_message("Progress commentary.", phase="commentary", item_id="progress-1"), completed=True)
    delta("你好")
    delta("，Codex。")
    finish("你好，Codex。")


for line in sys.stdin:
    message = json.loads(line)
    with TRACE.open("a", encoding="utf-8") as trace:
        trace.write(json.dumps(message, ensure_ascii=False) + "\n")
    if "jsonrpc" in message:
        print("Fixture requires Codex's wire envelope without a jsonrpc member", file=sys.stderr, flush=True)
        sys.exit(3)
    method = message.get("method")
    if method == "initialize":
        result(message["id"], {"userAgent": "fake-codex/1.0", "platformFamily": "unix", "platformOs": "macos"})
    elif method == "initialized":
        if "id" in message:
            sys.exit(4)
        initialized = True
    elif method in {"thread/start", "thread/resume"}:
        if not initialized:
            sys.exit(5)
        params = message["params"]
        if method == "thread/resume":
            finish("Historical replay before resume ACK.", turn="historical-turn")
        result(message["id"], {"thread": thread_value(history=method == "thread/resume"), "model": params.get("model") or "fixture-model",
                              "modelProvider": "openai", "cwd": os.getcwd(), "approvalPolicy": "on-request",
                              "approvalsReviewer": params.get("approvalsReviewer", "user"),
                              "sandbox": {"type": "workspaceWrite" if params.get("sandbox") == "workspace-write" else "readOnly"}})
    elif method == "turn/start":
        start_turn(message)
    elif method == "turn/steer":
        result(message["id"], {"turnId": TURN})
        for item in active_items:
            item_event({**item, "status": "completed"}, completed=True)
        active_items = []
        finish("Steered current turn.")
    elif method == "turn/interrupt":
        result(message["id"], {})
        if scenario != "wait_forever":
            finish(text="", status="interrupted")
    elif message.get("id") == pending_id and "result" in message:
        if scenario and scenario.startswith("permission_"):
            finish("Approval recorded.")
        elif scenario == "question_secret":
            finish("Echo from peer: " + message["result"]["answers"]["q-secret"]["answers"][0])
        elif scenario == "elicitation":
            finish("Elicitation recorded.")
        else:
            notify("serverRequest/resolved", {"threadId": THREAD, "requestId": pending_id})
        pending_id = None
