"""Black-box tests for the stdlib ACP runner; no Grok account or model is used.

Run: python3 -m unittest discover -s grok-cli/tests -v
"""

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
BRIDGE = ROOT / "scripts" / "grok_acp.py"
FIXTURE = Path(__file__).with_name("fake_grok.py")
SECRET_SENTINEL = "FAKE_TOKEN_DO_NOT_LEAK_4f7c9e7a"


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, KeyError) as error:
            last_error = error
        time.sleep(0.025)
    raise AssertionError(f"Condition did not become true within {timeout}s; last error={last_error!r}")


def process_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class BridgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="grok-acp-test-")
        self.base = Path(self.tmp.name)
        self.run_dir = self.base / "run"
        self.trace = self.base / "fake-trace.jsonl"
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
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.tmp.cleanup()

    def start(self, scenario, run_dir=None, extra_args=()):
        target = run_dir or self.run_dir
        prompt = self.base / f"prompt-{len(self.processes)}.txt"
        prompt.write_text(scenario, encoding="utf-8")
        env = dict(os.environ, FAKE_GROK_TRACE=str(self.trace))
        proc = subprocess.Popen(
            [sys.executable, str(BRIDGE), "run", "--cwd", str(self.base),
             "--prompt-file", str(prompt), "--run-dir", str(target),
             "--grok-bin", str(FIXTURE), "--heartbeat-seconds", "0.1",
             "--control-timeout-seconds", "2", *extra_args],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.processes.append(proc)
        return proc

    def command(self, name, *args, expected=0):
        command = [sys.executable, str(BRIDGE), name, "--run-dir", str(self.run_dir), *args]
        response = subprocess.run(command, capture_output=True, text=True, timeout=8)
        self.assertEqual(response.returncode, expected, response.stderr + response.stdout)
        return json.loads(response.stdout) if response.stdout.strip() else None

    def status(self):
        data = self.command("status")
        if data.get("child_pid"):
            self.children.add(data["child_pid"])
        return data

    def await_status(self, predicate):
        def read():
            if not (self.run_dir / "status.json").exists():
                return None
            data = self.status()
            return data if predicate(data) else None
        return wait_until(read)

    def finish(self, proc, expected=0):
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, expected, stderr + stdout)
        result = json.loads((self.run_dir / "result.json").read_text(encoding="utf-8"))
        return result, stdout, stderr

    def messages(self):
        if not self.trace.exists():
            return []
        return [json.loads(line) for line in self.trace.read_text(encoding="utf-8").splitlines()]

    def respond(self, request_id, response, expected=0):
        path = self.base / "response.json"
        path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        return self.command("respond", "--request-id", request_id, "--response-file", str(path), expected=expected)

    @staticmethod
    def tool(status, tool_id="tool-1"):
        tools = status["current_tools"]
        if isinstance(tools, dict):
            return tools.get(tool_id, {})
        return next((tool for tool in tools if tool["tool_call_id"] == tool_id), {})

    def test_success_reconstructs_text_without_claiming_host_capabilities(self):
        proc = self.start("success")
        result, _, _ = self.finish(proc)
        self.assertEqual(result["text"], "你好，ACP。")
        self.assertEqual(result["stopReason"], "end_turn")
        self.assertEqual(result["sessionId"], "fixture-session")
        initialize = next(m for m in self.messages() if m.get("method") == "initialize")
        self.assertEqual(initialize["params"]["clientCapabilities"], {})
        created = next(m for m in self.messages() if m.get("method") == "session/new")
        self.assertIs(created["params"]["_meta"]["askUserQuestion"], True)
        self.assertFalse(any(m.get("method", "").startswith(("fs/", "terminal/")) for m in self.messages()))
        status = self.status()
        self.assertEqual(status["state"], "succeeded")
        self.assertIsInstance(status["event_cursor"], int)

    def test_partial_tool_update_preserves_details_and_pending_is_running(self):
        proc = self.start("pending_tool")
        status = self.await_status(lambda s: bool(self.tool(s).get("locations")))
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["pending_requests"], [])
        tool = self.tool(status)
        self.assertEqual(tool["title"], "Read fixture configuration")
        self.assertEqual(tool["command"], "cat fixture.txt")
        self.assertEqual(tool["locations"], [{"path": "fixture.txt"}])
        self.assertEqual(tool["status"], "pending")
        self.command("cancel")
        self.finish(proc, expected=130)

    def test_permission_waits_for_response_and_forwards_selected_option(self):
        proc = self.start("permission")
        status = self.await_status(lambda s: any(r["kind"] == "permission" for r in s["pending_requests"]))
        self.assertIsNone(proc.poll())
        self.assertEqual(status["state"], "waiting_permission")
        request = status["pending_requests"][0]
        self.respond("req-not-found", {"optionId": "fixture-allow"}, expected=2)
        self.respond(request["request_id"], {"optionId": "not-advertised"}, expected=2)
        self.assertEqual(self.status()["pending_requests"][0]["state"], "pending")
        self.respond(request["request_id"], {"optionId": "fixture-allow"})
        result, _, _ = self.finish(proc)
        self.assertEqual(result["text"], "交互已接收。")
        response = next(m for m in self.messages() if m.get("id") == 701 and "result" in m)
        self.assertEqual(response["result"], {"outcome": {"outcome": "selected", "optionId": "fixture-allow"}})
        self.assertFalse(any(r["state"] == "pending" for r in self.status()["pending_requests"]))
        self.respond(request["request_id"], {"optionId": "fixture-allow"}, expected=2)

    def test_question_response_preserves_answers_and_notes(self):
        proc = self.start("question")
        status = self.await_status(lambda s: any(r["kind"] == "input" for r in s["pending_requests"]))
        self.assertIsNone(proc.poll())
        self.assertEqual(status["state"], "waiting_input")
        answer = {"outcome": "accepted", "answers": {"Choose a route?": ["A"]},
                  "annotations": {"Choose a route?": {"notes": "Use route A."}}}
        self.respond(status["pending_requests"][0]["request_id"], answer)
        result, _, _ = self.finish(proc)
        self.assertEqual(result["text"], "交互已接收。")
        response = next(m for m in self.messages() if m.get("id") == 702 and "result" in m)
        self.assertEqual(response["result"], answer)

    def test_duplicate_response_during_active_turn_is_not_sent_twice(self):
        proc = self.start("permission_hold")
        status = self.await_status(lambda s: bool(s["pending_requests"]))
        request_id = status["pending_requests"][0]["request_id"]
        self.respond(request_id, {"optionId": "fixture-allow"})
        self.await_status(lambda s: s["state"] == "running")
        self.assertIsNone(proc.poll())
        self.respond(request_id, {"optionId": "fixture-allow"}, expected=2)
        self.command("cancel")
        self.finish(proc, expected=130)
        responses = [m for m in self.messages() if m.get("id") == 701 and "result" in m]
        self.assertEqual(len(responses), 1)

    def test_grok_resolved_interaction_cannot_be_answered_again(self):
        proc = self.start("resolved_question")
        status = self.await_status(lambda s: s["state"] == "running" and bool(s.get("interaction_history")))
        self.assertFalse(status["pending_requests"])
        self.assertIsNone(proc.poll())
        request_id = status["interaction_history"][0]["request_id"]
        self.respond(request_id, {"outcome": "accepted", "answers": {"Choose a route?": ["A"]}}, expected=2)
        self.command("cancel")
        self.finish(proc, expected=130)
        self.assertFalse(any(m.get("id") == 702 and "result" in m for m in self.messages()))

    def test_plan_approval_round_trip(self):
        proc = self.start("plan")
        status = self.await_status(lambda s: bool(s["pending_requests"]))
        self.assertIsNone(proc.poll())
        self.respond(status["pending_requests"][0]["request_id"], {"outcome": "approved"})
        self.finish(proc)
        response = next(m for m in self.messages() if m.get("id") == 703 and "result" in m)
        self.assertEqual(response["result"], {"outcome": "approved"})

    def test_resume_history_does_not_pollute_current_answer_or_tools(self):
        proc = self.start("success", extra_args=("--resume", "fixture-session"))
        result, _, _ = self.finish(proc)
        self.assertEqual(result["text"], "你好，ACP。")
        self.assertFalse(self.tool(self.status(), "historical-tool"))
        loaded = next(m for m in self.messages() if m.get("method") == "session/load")
        self.assertIs(loaded["params"]["_meta"]["askUserQuestion"], True)

    def test_model_and_effort_are_applied_before_new_and_resumed_prompts(self):
        for name, extra, expected_model in (
            ("new-model", ("--model", "requested-model", "--reasoning-effort", "high"), "requested-model"),
            ("resume-model", ("--resume", "fixture-session", "--model", "requested-model", "--reasoning-effort", "high"), "requested-model"),
            ("effort-only", ("--reasoning-effort", "high"), "fixture-model"),
        ):
            with self.subTest(name=name):
                self.run_dir = self.base / name
                self.trace = self.base / (name + "-trace.jsonl")
                proc = self.start("success", extra_args=extra)
                self.finish(proc)
                messages = self.messages()
                configured = next(m for m in messages if m.get("method") == "session/set_model")
                self.assertEqual(configured["params"], {
                    "sessionId": "fixture-session", "modelId": expected_model,
                    "_meta": {"reasoningEffort": "high"},
                })
                prompt = next(m for m in messages if m.get("method") == "session/prompt")
                self.assertLess(messages.index(configured), messages.index(prompt))

    def test_subagent_tools_are_visible_without_polluting_root_answer(self):
        proc = self.start("subagent")
        result, _, _ = self.finish(proc)
        self.assertEqual(result["text"], "Root response.")
        events = self.command("events", "--limit", "100")["events"]
        child_tools = [event["tool"] for event in events if event["type"] == "tool"
                       and event["tool"]["session_id"] == "child-session"]
        self.assertTrue(child_tools)
        self.assertEqual(child_tools[-1]["status"], "completed")
        self.assertIn("child-session", self.status()["observed_sessions"])

    def test_unknown_reverse_request_returns_protocol_error_and_fails(self):
        proc = self.start("unknown_request")
        result, _, _ = self.finish(proc, expected=1)
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["error"])
        response = next(m for m in self.messages() if m.get("id") == 711 and "error" in m)
        self.assertEqual(response["error"]["code"], -32601)

    def test_incomplete_or_invalid_runs_never_report_success(self):
        for scenario in ("malformed", "early_eof", "non_end_turn", "empty_result"):
            with self.subTest(scenario=scenario):
                self.run_dir = self.base / scenario
                proc = self.start(scenario)
                result, _, _ = self.finish(proc, expected=1)
                self.assertEqual(result["state"], "failed")
                self.assertTrue(result["error"])
                self.assertEqual(self.status()["state"], "failed")

    def test_cancel_stops_running_child(self):
        proc = self.start("pending_tool")
        status = self.await_status(lambda s: bool(s.get("current_tools")))
        child_pid = status["child_pid"]
        self.command("cancel")
        result, _, _ = self.finish(proc, expected=130)
        self.assertEqual(result["state"], "cancelled")
        wait_until(lambda: not process_exists(child_pid))

    def test_cancel_resolves_pending_interactions_as_cancelled(self):
        for scenario, request_id, expected in (
            ("permission", 701, {"outcome": {"outcome": "cancelled"}}),
            ("question", 702, {"outcome": "cancelled"}),
        ):
            with self.subTest(scenario=scenario):
                self.run_dir = self.base / scenario
                proc = self.start(scenario)
                self.await_status(lambda s: bool(s["pending_requests"]))
                self.command("cancel")
                result, _, _ = self.finish(proc, expected=130)
                self.assertEqual(result["state"], "cancelled")
                response = next(m for m in self.messages() if m.get("id") == request_id and "result" in m)
                self.assertEqual(response["result"], expected)

    def test_sigterm_cleans_up_noncooperative_child(self):
        proc = self.start("wait_forever")
        status = self.await_status(lambda s: bool(s.get("current_tools")))
        child_pid = status["child_pid"]
        proc.send_signal(signal.SIGTERM)
        result, _, _ = self.finish(proc, expected=130)
        self.assertEqual(result["state"], "cancelled")
        wait_until(lambda: not process_exists(child_pid))

    def test_killed_supervisor_is_reported_unreachable_instead_of_running(self):
        proc = self.start("pending_tool")
        status = self.await_status(lambda s: bool(s.get("current_tools")))
        child_pid = status["child_pid"]
        try:
            proc.kill()
            proc.communicate(timeout=5)
            status = self.status()
            self.assertEqual(status["state"], "supervisor_unreachable")
            self.assertEqual(status["recorded_state"], "running")
            self.assertIs(status["supervisor_reachable"], False)
        finally:
            if process_exists(child_pid):
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_existing_run_directory_is_rejected_without_overwriting(self):
        self.run_dir.mkdir()
        sentinel = self.run_dir / "preserve-me.txt"
        sentinel.write_text("existing evidence", encoding="utf-8")
        proc = self.start("success")
        stdout, stderr = proc.communicate(timeout=8)
        self.assertNotEqual(proc.returncode, 0, stdout + stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing evidence")
        self.assertFalse(self.trace.exists(), "Grok should not start before the fresh run directory is accepted")

    def test_events_cursor_paginates_without_loss_or_duplicates(self):
        proc = self.start("success")
        self.finish(proc)
        cursor = 0
        seen = []
        for _ in range(100):
            page = self.command("events", "--after", str(cursor), "--limit", "2")
            self.assertLessEqual(len(page["events"]), 2)
            for event in page["events"]:
                self.assertGreater(event["seq"], cursor)
                self.assertIn("time", event)
                self.assertIn("type", event)
                seen.append(event["seq"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        else:
            self.fail("Event pagination did not terminate")
        self.assertTrue(seen)
        self.assertEqual(seen, sorted(set(seen)))
        self.assertEqual(cursor, self.status()["event_cursor"])
        self.assertEqual(self.command("events", "--after", str(cursor))["events"], [])

    def test_many_chunks_are_retained_without_streaming_raw_text_to_stdout(self):
        proc = self.start("many_chunks")
        result, stdout, stderr = self.finish(proc)
        text = "fragment-never-print-per-chunk;" * 1000
        self.assertEqual(result["text"], text)
        self.assertLess(len(stdout) + len(stderr), len(text), "Terminal output should summarize, not echo every text chunk")

    def test_secrets_are_redacted_from_all_evidence_without_hiding_task_details(self):
        proc = self.start("secret_sentinel")
        status = self.await_status(lambda s: bool(s["pending_requests"]))
        status_text = json.dumps(status, ensure_ascii=False)
        self.assertNotIn(SECRET_SENTINEL, status_text)
        self.assertIn("Choose a route?", status_text)
        self.respond(status["pending_requests"][0]["request_id"], {
            "outcome": "accepted", "answers": {"Choose a route?": ["A"]},
        })
        _, stdout, stderr = self.finish(proc)
        evidence = stdout + stderr
        for name in ("status.json", "events.jsonl", "protocol.jsonl", "result.json", "stderr.log"):
            with self.subTest(artifact=name):
                content = (self.run_dir / name).read_text(encoding="utf-8")
                self.assertNotIn(SECRET_SENTINEL, content)
                evidence += content
        self.assertNotIn(SECRET_SENTINEL, stdout + stderr)
        self.assertIn("Inspect public fixture configuration", evidence)
        self.assertIn("Choose a route?", evidence)


if __name__ == "__main__":
    unittest.main()
