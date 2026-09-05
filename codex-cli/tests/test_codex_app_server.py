"""Black-box Codex app-server adapter tests; no credentials or models are used."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "codex_app_server.py"
FIXTURE = Path(__file__).with_name("fake_codex.py")
SECRET = "FAKE_CODEX_TOKEN_DO_NOT_LEAK_f9845d"
SECRET_REPLY = "FAKE_USER_SECRET_DO_NOT_ECHO_e94fa9"


def process_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class CodexAppServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-adapter-test-", dir="/tmp")
        self.base = Path(self.temp.name)
        self.run_dir = self.base / "run"
        self.trace = self.base / "trace.jsonl"
        self.processes = []
        self.children = set()
        FIXTURE.chmod(0o755)

    def tearDown(self):
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
        for pid in self.children:
            if process_exists(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.temp.cleanup()

    def start(self, scenario, extra=()):
        prompt = self.base / f"prompt-{len(self.processes)}.txt"
        prompt.write_text(scenario, encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(RUNNER), "run", "--cwd", str(self.base),
             "--prompt-file", str(prompt), "--run-dir", str(self.run_dir),
             "--codex-bin", str(FIXTURE), "--heartbeat-seconds", "0.1",
             "--control-timeout-seconds", "5", *extra],
            env=dict(os.environ, FAKE_CODEX_TRACE=str(self.trace)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.processes.append(proc)
        return proc

    def command(self, operation, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(RUNNER), operation, "--run-dir", str(self.run_dir), *args],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def status(self):
        value = self.command("status")
        if value.get("child_pid"):
            self.children.add(value["child_pid"])
        return value

    def await_status(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if (self.run_dir / "status.json").exists():
                last = self.status()
                if predicate(last):
                    return last
                if last["state"] in {"failed", "cancelled", "succeeded"}:
                    self.fail(f"Runner ended before expected state: {last}")
            time.sleep(0.025)
        self.fail(f"Expected state not observed; last status={last}")

    def finish(self, proc, expected=0):
        stdout, stderr = proc.communicate(timeout=12)
        self.assertEqual(proc.returncode, expected, stdout + stderr)
        receipt = json.loads((self.run_dir / "result.json").read_text(encoding="utf-8"))
        return receipt, stdout, stderr

    def messages(self):
        return [json.loads(line) for line in self.trace.read_text(encoding="utf-8").splitlines()]

    def respond(self, request_id, value, expected=0):
        response = self.base / "response.json"
        response.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return self.command("respond", "--request-id", request_id, "--response-file", str(response), expected=expected)

    def steer(self, expected=0):
        text = self.base / "steer.txt"
        text.write_text("Continue this precise active turn.", encoding="utf-8")
        return self.command("steer", "--text-file", str(text), expected=expected)

    def test_wire_initialization_and_final_answer_are_not_duplicated(self):
        proc = self.start("success")
        receipt, _, _ = self.finish(proc)
        self.assertEqual(receipt["text"], "你好，Codex。")
        self.assertEqual(receipt["sessionId"], "fixture-thread")
        self.assertEqual(receipt["threadId"], "fixture-thread")
        self.assertEqual(receipt["turnId"], "fixture-turn")
        self.assertEqual(receipt["stopReason"], "completed")
        messages = self.messages()
        self.assertFalse(any("jsonrpc" in message for message in messages))
        self.assertEqual(messages[0]["method"], "initialize")
        self.assertIs(messages[0]["params"]["capabilities"]["experimentalApi"], True)
        self.assertEqual(messages[1]["method"], "initialized")
        self.assertNotIn("id", messages[1])
        started = next(m for m in messages if m.get("method") == "thread/start")
        self.assertEqual(started["params"]["sandbox"], "read-only")
        self.assertEqual(started["params"]["approvalPolicy"], "on-request")
        self.assertEqual(started["params"]["approvalsReviewer"], "user")

    def test_explicit_sandbox_reviewer_model_and_collaboration_are_forwarded(self):
        proc = self.start("success", extra=("--sandbox", "workspace-write", "--approval-reviewer", "auto_review",
                                           "--model", "requested-model", "--reasoning-effort", "high",
                                           "--collaboration-mode", "plan"))
        self.finish(proc)
        started = next(m for m in self.messages() if m.get("method") == "thread/start")["params"]
        self.assertEqual(started["sandbox"], "workspace-write")
        self.assertEqual(started["approvalsReviewer"], "auto_review")
        self.assertEqual(started["model"], "requested-model")
        turn = next(m for m in self.messages() if m.get("method") == "turn/start")["params"]
        self.assertEqual(turn["collaborationMode"]["mode"], "plan")
        self.assertEqual(turn["collaborationMode"]["settings"]["model"], "requested-model")
        self.assertEqual(turn["collaborationMode"]["settings"]["reasoning_effort"], "high")

    def test_turn_start_ack_is_not_completion_and_steer_targets_active_turn(self):
        proc = self.start("ack_only")
        status = self.await_status(lambda s: s.get("turn_id") == "fixture-turn")
        self.assertEqual(status["state"], "running")
        self.assertIsNone(proc.poll())
        self.assertFalse((self.run_dir / "result.json").exists())
        self.steer()
        receipt, _, _ = self.finish(proc)
        self.assertEqual(receipt["text"], "Steered current turn.")
        steered = next(m for m in self.messages() if m.get("method") == "turn/steer")["params"]
        self.assertEqual(steered["threadId"], "fixture-thread")
        self.assertEqual(steered["expectedTurnId"], "fixture-turn")
        self.assertEqual(steered["input"][0]["text"], "Continue this precise active turn.")
        self.steer(expected=2)

    def test_native_items_and_child_task_state_remain_visible(self):
        proc = self.start("items")
        status = self.await_status(lambda s: len(s.get("current_tools", [])) >= 4 and
                                   "fixture-output-7" in json.dumps(s["current_tools"]) and
                                   any(agent.get("state") == "completed" for agent in s.get("subagents", [])))
        self.assertEqual(status["state"], "running")
        self.assertFalse(status["pending_requests"])
        self.assertFalse(any(tool["thread_id"] == "child-thread" for tool in status["current_tools"]))
        self.assertEqual(status["plan"], [{"step": "Root remains active", "status": "inProgress"}])
        types = {tool["type"] for tool in status["current_tools"]}
        self.assertTrue({"commandExecution", "fileChange", "mcpToolCall", "collabAgentToolCall"}.issubset(types))
        summaries = json.dumps(status["current_tools"])
        for detail in ("printf fixture", "example.txt", "lookup", "child-thread"):
            self.assertIn(detail, summaries)
        command = next(tool for tool in status["current_tools"] if tool["type"] == "commandExecution")
        self.assertTrue(command["last_output_at"])
        self.assertIn("fixture-output-7", command["output_tail"])
        self.steer()
        receipt, _, _ = self.finish(proc)
        self.assertEqual(receipt["text"], "Steered current turn.")
        self.assertFalse(any(m.get("id") == 605 and "result" in m for m in self.messages()))

    def test_blocking_and_optional_questions_use_question_ids_and_answer_once(self):
        for scenario, expected_state in (("question_blocking", "waiting_input"), ("question_nonblocking", "running")):
            with self.subTest(scenario=scenario):
                self.run_dir = self.base / scenario
                self.trace = self.base / (scenario + "-trace.jsonl")
                proc = self.start(scenario)
                status = self.await_status(lambda s: bool(s["pending_requests"]))
                self.assertEqual(status["state"], expected_state)
                self.assertEqual(status["pending_requests"][0]["params"]["questions"][0]["header"], "Route")
                request_id = status["pending_requests"][0]["request_id"]
                self.respond(request_id, {"answers": {"Which visible route?": {"answers": ["A"]}}}, expected=2)
                self.assertTrue(self.status()["pending_requests"])
                answer = {"answers": {"q-route": {"answers": ["A"]}}}
                self.respond(request_id, answer)
                self.await_status(lambda s: not s["pending_requests"])
                self.respond(request_id, answer, expected=2)
                self.steer()
                self.finish(proc)
                responses = [m for m in self.messages() if m.get("id") == 601 and "result" in m]
                self.assertEqual([m["result"] for m in responses], [answer])

    def test_resolved_request_cannot_be_answered_after_expiry(self):
        proc = self.start("question_resolved")
        status = self.await_status(lambda s: bool(s.get("interaction_history")))
        self.assertEqual(status["state"], "running")
        self.assertFalse(status["pending_requests"])
        self.respond(status["interaction_history"][0]["request_id"], {"answers": {"q-route": {"answers": ["A"]}}}, expected=2)
        self.steer()
        self.finish(proc)
        self.assertFalse(any(m.get("id") == 601 and "result" in m for m in self.messages()))

    def test_question_resolved_before_start_ack_does_not_reappear_as_pending(self):
        proc = self.start("question_resolved_before_ack")
        status = self.await_status(lambda s: s.get("turn_id") == "fixture-turn")
        self.assertEqual(status["state"], "running")
        self.assertFalse(status["pending_requests"])
        self.respond("req-1", {"answers": {"q-route": {"answers": ["A"]}}}, expected=2)
        self.steer()
        self.finish(proc)
        self.assertFalse(any(m.get("id") == 601 and "result" in m for m in self.messages()))

    def test_approval_requires_explicit_valid_choice_without_widening_it(self):
        for scenario in ("permission_command", "permission_file"):
            with self.subTest(scenario=scenario):
                self.run_dir = self.base / scenario
                self.trace = self.base / (scenario + "-trace.jsonl")
                proc = self.start(scenario)
                status = self.await_status(lambda s: bool(s["pending_requests"]))
                self.assertEqual(status["state"], "waiting_permission")
                self.assertFalse(any(m.get("id") == 602 and "result" in m for m in self.messages()))
                request_id = status["pending_requests"][0]["request_id"]
                invalid = "acceptForSession" if scenario == "permission_command" else "unknown-decision"
                self.respond(request_id, {"decision": invalid}, expected=2)
                self.assertTrue(self.status()["pending_requests"])
                self.respond(request_id, {"decision": "accept"})
                self.finish(proc)
                response = next(m for m in self.messages() if m.get("id") == 602 and "result" in m)
                self.assertEqual(response["result"], {"decision": "accept"})

    def test_permission_profile_cannot_grant_unrequested_access(self):
        proc = self.start("permission_profile")
        status = self.await_status(lambda s: bool(s["pending_requests"]))
        request_id = status["pending_requests"][0]["request_id"]
        self.respond(request_id, {"permissions": {"network": {"enabled": True}}, "scope": "turn"}, expected=2)
        self.respond(request_id, {"permissions": {"fileSystem": {"write": ["/"]}}, "scope": "turn"}, expected=2)
        grant = {"permissions": {"fileSystem": {"write": ["/fixture/allowed"]}}, "scope": "turn"}
        self.respond(request_id, grant)
        self.finish(proc)
        response = next(m for m in self.messages() if m.get("id") == 602 and "result" in m)
        self.assertEqual(response["result"], grant)

    def test_failed_interrupted_empty_malformed_and_eof_never_succeed(self):
        for scenario in ("failed", "interrupted", "empty", "commentary_only", "malformed", "early_eof"):
            with self.subTest(scenario=scenario):
                self.run_dir = self.base / scenario
                proc = self.start(scenario)
                receipt, _, _ = self.finish(proc, expected=130 if scenario == "interrupted" else 1)
                self.assertEqual(receipt["state"], "cancelled" if scenario == "interrupted" else "failed")
                if scenario != "interrupted":
                    self.assertTrue(receipt["error"])

    def test_mcp_form_accept_and_cancel_preserve_native_response_shape(self):
        for action in ("accept", "cancel"):
            with self.subTest(action=action):
                self.run_dir = self.base / action
                self.trace = self.base / (action + "-trace.jsonl")
                proc = self.start("elicitation")
                status = self.await_status(lambda s: bool(s["pending_requests"]))
                request_id = status["pending_requests"][0]["request_id"]
                self.respond(request_id, {"action": "invalid-action"}, expected=2)
                self.assertTrue(self.status()["pending_requests"])
                answer = {"action": action}
                if action == "accept":
                    answer["content"] = {"name": "fixture"}
                self.respond(request_id, answer)
                self.finish(proc)
                response = next(m for m in self.messages() if m.get("id") == 604 and "result" in m)
                self.assertEqual(response["result"], answer)

    def test_secret_question_reply_reaches_peer_but_echo_is_redacted(self):
        proc = self.start("question_secret")
        status = self.await_status(lambda s: bool(s["pending_requests"]))
        question = status["pending_requests"][0]["params"]["questions"][0]
        self.assertIs(question["isSecret"], True)
        self.assertEqual(question["header"], "Secret")
        answer = {"answers": {"q-secret": {"answers": [SECRET_REPLY]}}}
        self.respond(status["pending_requests"][0]["request_id"], answer)
        _, stdout, stderr = self.finish(proc)
        response = next(m for m in self.messages() if m.get("id") == 601 and "result" in m)
        self.assertEqual(response["result"], answer)
        self.assertNotIn(SECRET_REPLY, stdout + stderr)
        for name in ("status.json", "events.jsonl", "protocol.jsonl", "result.json", "stderr.log"):
            with self.subTest(artifact=name):
                self.assertNotIn(SECRET_REPLY, (self.run_dir / name).read_text(encoding="utf-8"))

    def test_resume_uses_exact_thread_and_excludes_history_and_other_turns(self):
        proc = self.start("resume_history", extra=("--resume", "fixture-thread"))
        receipt, _, _ = self.finish(proc)
        self.assertEqual(receipt["text"], "你好，Codex。")
        resumed = next(m for m in self.messages() if m.get("method") == "thread/resume")
        self.assertEqual(resumed["params"]["threadId"], "fixture-thread")
        self.assertFalse(any(m.get("method") == "thread/start" for m in self.messages()))

    def test_cancel_interrupts_native_turn_without_fabricating_user_answers(self):
        for scenario in ("question_blocking", "permission_command"):
            with self.subTest(scenario=scenario):
                self.run_dir = self.base / scenario
                self.trace = self.base / (scenario + "-trace.jsonl")
                proc = self.start(scenario)
                self.await_status(lambda s: bool(s["pending_requests"]))
                self.command("cancel")
                self.finish(proc, expected=130)
                messages = self.messages()
                interrupted = next(m for m in messages if m.get("method") == "turn/interrupt")
                self.assertEqual(interrupted["params"], {"threadId": "fixture-thread", "turnId": "fixture-turn"})
                self.assertFalse(any(m.get("id") in {601, 602} and "result" in m for m in messages))

    def test_unknown_server_request_fails_with_protocol_error(self):
        proc = self.start("unknown_request")
        receipt, _, _ = self.finish(proc, expected=1)
        self.assertEqual(receipt["state"], "failed")
        response = next(m for m in self.messages() if m.get("id") == 603 and "error" in m)
        self.assertEqual(response["error"]["code"], -32601)

    def test_sigterm_cleans_up_uncooperative_app_server(self):
        proc = self.start("wait_forever")
        status = self.await_status(lambda s: s.get("turn_id") == "fixture-turn")
        child_pid = status["child_pid"]
        proc.send_signal(signal.SIGTERM)
        self.finish(proc, expected=130)
        self.assertFalse(process_exists(child_pid))

    def test_reusing_run_directory_does_not_overwrite_evidence(self):
        self.run_dir.mkdir()
        sentinel = self.run_dir / "existing.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        proc = self.start("success")
        proc.communicate(timeout=5)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
        self.assertFalse(self.trace.exists())

    def test_event_cursor_pagination_and_many_deltas_do_not_flood_terminal(self):
        proc = self.start("many_chunks")
        receipt, stdout, stderr = self.finish(proc)
        text = "retain-this-text-without-printing-every-delta;" * 1000
        self.assertEqual(receipt["text"], text)
        self.assertLess(len(stdout) + len(stderr), len(text))
        cursor, sequences = 0, []
        for _ in range(100):
            page = self.command("events", "--after", str(cursor), "--limit", "2")
            self.assertLessEqual(len(page["events"]), 2)
            sequences.extend(e["seq"] for e in page["events"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        else:
            self.fail("Pagination did not terminate")
        self.assertTrue(sequences)
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertEqual(cursor, self.status()["event_cursor"])

    def test_synthetic_credentials_never_enter_saved_or_terminal_evidence(self):
        proc = self.start("sentinel")
        receipt, stdout, stderr = self.finish(proc)
        self.assertEqual(receipt["text"], "Public result.")
        self.assertNotIn(SECRET, stdout + stderr)
        for name in ("status.json", "events.jsonl", "protocol.jsonl", "stderr.log", "result.json"):
            with self.subTest(artifact=name):
                self.assertNotIn(SECRET, (self.run_dir / name).read_text(encoding="utf-8"))
        self.assertIn("safe_lookup", (self.run_dir / "events.jsonl").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
