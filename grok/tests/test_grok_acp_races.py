"""Deterministic regressions for cancellation around a blocked ACP stdin drain."""

import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


SPEC = importlib.util.spec_from_file_location(
    "grok_acp_race_subject", Path(__file__).resolve().parents[1] / "scripts" / "grok_acp.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class InputPipe:
    def __init__(self, pause_drain):
        self.frames = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not pause_drain:
            self.release.set()

    def is_closing(self):
        return False

    def write(self, data):
        self.frames.append(json.loads(data))

    async def drain(self):
        self.entered.set()
        await self.release.wait()


class ControlReader:
    def __init__(self, value):
        self.value = value

    async def readline(self):
        return (json.dumps(self.value) + "\n").encode()


class ControlReply:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None

    def result(self):
        return json.loads(self.data)


class ResponseCancellationRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="grok-acp-race-", dir="/tmp")
        self.path = Path(self.temp.name)
        self.args = SimpleNamespace(
            grok_bin="unused-fake-grok", cwd=str(self.path), model=None,
            reasoning_effort=None, timeout_seconds=5, heartbeat_seconds=0.1,
            control_timeout_seconds=2, ask_permissions=False,
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def run_race(self, response_wins):
        bridge = MODULE.Bridge(self.args, self.path)
        pipe = InputPipe(pause_drain=response_wins)
        ready = asyncio.Event()

        async def idle():
            await asyncio.Event().wait()

        async def conversation():
            bridge.state.update(state="running", session_id="race-session")
            await bridge.incoming_request({
                "jsonrpc": "2.0", "id": 701, "method": "session/request_permission",
                "params": {"sessionId": "race-session", "toolCall": {"toolCallId": "race-tool"},
                           "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}]},
            })
            ready.set()
            await idle()

        bridge.conversation = conversation
        bridge.reader = idle
        bridge.heartbeat = idle
        bridge.stderr_reader = idle
        bridge.stop_child = AsyncMock()
        process = SimpleNamespace(pid=999999, stdin=pipe)
        answer = ControlReply()
        cancel = ControlReply()
        run_task = response_task = None
        with patch.object(MODULE.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                run_task = asyncio.create_task(bridge.run())
                try:
                    await asyncio.wait_for(ready.wait(), 1)
                except TimeoutError:
                    self.fail(f"Bridge fixture failed to start: {bridge.state.get('error')}")
                if not response_wins:
                    await bridge.write_lock.acquire()
                response_task = asyncio.create_task(bridge.control(ControlReader({
                    "operation": "respond", "request_id": "req-1", "response": {"optionId": "allow"},
                }), answer))
                if response_wins:
                    await asyncio.wait_for(pipe.entered.wait(), 1)
                else:
                    async def reserved():
                        while bridge.requests["req-1"]["state"] != "responding":
                            await asyncio.sleep(0)
                    await asyncio.wait_for(reserved(), 1)
                await bridge.control(ControlReader({"operation": "cancel"}), cancel)
                if response_wins:
                    pipe.release.set()
                else:
                    bridge.write_lock.release()
                await asyncio.wait_for(response_task, 2)
                self.assertEqual(await asyncio.wait_for(run_task, 2), 130)
            finally:
                pipe.release.set()
                if bridge.write_lock.locked() and not response_wins:
                    bridge.write_lock.release()
                for task in (response_task, run_task):
                    if task and not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
        self.assertTrue(cancel.result()["ok"])
        responses = [frame for frame in pipe.frames if frame.get("id") == 701]
        self.assertEqual(len(responses), 1, "Exactly one party may answer each ACP reverse request")
        return answer.result(), responses[0], bridge.requests["req-1"]

    async def test_answer_written_before_drain_cannot_also_be_cancelled(self):
        reply, response, request = await self.run_race(response_wins=True)
        self.assertTrue(reply["ok"])
        self.assertEqual(response["result"], {"outcome": {"outcome": "selected", "optionId": "allow"}})
        self.assertEqual(request["state"], "answered")

    async def test_cancel_before_write_lock_prevents_late_answer(self):
        reply, response, request = await self.run_race(response_wins=False)
        self.assertFalse(reply["ok"])
        self.assertEqual(response["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(request["state"], "invalidated")


if __name__ == "__main__":
    unittest.main()
