#!/usr/bin/env python3
"""Standalone foreground supervision for the Codex app-server protocol.

Python 3.10+; standard library only. Keep `run` attached to a managed terminal.
This bridge reports observed events; silence is never classified as a hang.
"""

import argparse
import asyncio
import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import tempfile
import time


FINAL = {"succeeded", "failed", "cancelled"}
TERMINAL_TOOLS = {"completed", "failed"}
MAX_FRAME = 16 * 1024 * 1024


class Redactor:
    """Remove configuration credentials before either terminal or evidence output."""
    def __init__(self):
        self.secrets = set()
        for key, value in os.environ.items():
            if self.sensitive(key) and len(value) >= 4:
                self.secrets.add(value)

    @staticmethod
    def sensitive(key):
        key = re.sub(r"[^a-z0-9]", "", str(key).lower())
        return (key in {"env", "environment", "environmentvariables", "envvars", "headers", "header",
                       "authorization", "token", "cookie", "cookies"} or key.endswith("token") or
                any(word in key for word in ("apikey", "accesstoken", "refreshtoken", "authtoken",
                                              "password", "passwd", "secret", "credential", "privatekey")))

    @classmethod
    def sensitive_field(cls, key, value, container):
        if key == "isSecret" and type(value) is bool:
            return False  # Native question metadata, not the user's secret answer.
        is_question = (isinstance(container.get("question"), str) and
                       (isinstance(container.get("id"), str) or isinstance(container.get("options"), list)))
        if key == "header" and isinstance(value, str) and is_question:
            return False  # Question headers are short UI labels; HTTP headers remain protected.
        return cls.sensitive(key)

    def remember(self, value):
        if isinstance(value, str) and len(value) >= 4:
            self.secrets.add(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key not in {"name", "key"}:
                    self.remember(item)
        elif isinstance(value, list):
            for item in value:
                self.remember(item)

    def collect(self, value):
        if isinstance(value, dict):
            if "name" in value and "value" in value and self.sensitive(value["name"]):
                self.remember(value["value"])
            for key, item in value.items():
                if self.sensitive_field(key, item, value):
                    self.remember(item)
                self.collect(item)
        elif isinstance(value, list):
            for item in value:
                self.collect(item)

    def clean(self, value):
        self.collect(value)
        def replace(item):
            if isinstance(item, dict):
                pair = "name" in item and "value" in item and self.sensitive(item["name"])
                return {key: "[REDACTED]" if self.sensitive_field(key, child, item) or (pair and key == "value")
                        else replace(child) for key, child in item.items()}
            if isinstance(item, list):
                return [replace(child) for child in item]
            if isinstance(item, str):
                for secret in sorted(self.secrets, key=len, reverse=True):
                    item = item.replace(secret, "[REDACTED]")
                item = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", item)
                item = re.sub(r"(?i)(\b(?:[A-Za-z0-9_]+_)?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization|token)"
                              r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+", r"\1[REDACTED]", item)
            return item
        return replace(value)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def compact(value, limit=1200):
    text = value if isinstance(value, str) else encode(value)
    return text if len(text) <= limit else text[:limit] + "… [full value in protocol.jsonl]"


def atomic_json(path, value):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        os.chmod(temp, 0o600)
        handle.write(encode(value) + "\n")
    os.replace(temp, path)


def private_directory(path):
    path = Path(path).absolute()
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("run directory must be an owned, real directory")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("run directory must be private (mode 700)")
    return path


def create_directory(value):
    if value:
        path = Path(value).absolute()
        path.mkdir(mode=0o700)  # Deliberately refuses an existing directory.
    else:
        parent = Path("/tmp/codex-grok-cli")
        parent.mkdir(mode=0o700, exist_ok=True)
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o022):
            raise ValueError("shared run root must be an owned directory not writable by others")
        path = Path(tempfile.mkdtemp(prefix="acp-", dir=parent))
    os.chmod(path, 0o700)
    return private_directory(path)


def read_status(path):
    data = json.loads((path / "status.json").read_text(encoding="utf-8"))
    last = data.get("last_event_at")
    data["last_event_age_seconds"] = (max(0, time.time() -
        datetime.datetime.fromisoformat(last).timestamp()) if last else None)
    end = datetime.datetime.fromisoformat(data["finished_at"]).timestamp() if data.get("finished_at") else time.time()
    data["elapsed_seconds"] = max(0, end - datetime.datetime.fromisoformat(data["started_at"]).timestamp())
    return data


class BaseBridge:
    agent_name = "Worker"

    def __init__(self, args, path):
        self.args, self.path = args, path
        self.process = None
        self.server = None
        self.background = []
        self.waiters = {}
        self.requests = {}
        self.tools = {}
        self.parts = []
        self.rpc_sequence = 0
        self.request_sequence = 0
        self.fatal = asyncio.get_running_loop().create_future()
        self.cancel_event = asyncio.Event()
        self.write_lock = asyncio.Lock()
        self.turn_completed = False
        self.prompt_active = False
        self.redactor = Redactor()
        self.finishing = False
        self.state = {"version": 1, "run_dir": str(path), "state": "starting",
                      "pid": os.getpid(), "child_pid": None, "session_id": None,
                      "started_at": now(), "updated_at": now(), "last_event_at": None,
                      "event_cursor": 0, "current_tools": [], "pending_requests": [],
                      "observed_sessions": [],
                      "stop_reason": None, "error": None,
                      "result_path": str(path / "result.json")}
        self.protocol = (path / "protocol.jsonl").open("a", encoding="utf-8", buffering=1)
        self.events = (path / "events.jsonl").open("a", encoding="utf-8", buffering=1)
        self.stderr = (path / "stderr.log").open("ab", buffering=0)
        for name in ("protocol.jsonl", "events.jsonl", "stderr.log"):
            os.chmod(path / name, 0o600)


    def save(self):
        self.state["updated_at"] = now()
        self.state["current_tools"] = [t for t in self.tools.values()
                                       if t.get("status") not in TERMINAL_TOOLS]
        self.state["pending_requests"] = [r for r in self.requests.values()
                                          if r["state"] in {"pending", "responding"}]
        self.state["interaction_history"] = [r for r in self.requests.values()
                                             if r["state"] not in {"pending", "responding"}]
        atomic_json(self.path / "status.json", self.redactor.clean(self.state))


    def event(self, event_type, **fields):
        self.state["event_cursor"] += 1
        entry = self.redactor.clean({"seq": self.state["event_cursor"], "time": now(),
                                     "type": event_type, **fields})
        self.events.write(encode(entry) + "\n")
        self.save()
        try:
            print(encode(entry), flush=True)
        except BrokenPipeError:
            pass  # Durable evidence remains available if the terminal disconnects.


    def fail(self, message):
        if not self.finishing and not self.fatal.done():
            self.fatal.set_result(str(message))


    def interaction_state(self):
        if self.state["state"] in FINAL or self.state["state"] == "cancelling":
            return
        pending = [r for r in self.requests.values() if r["state"] == "pending"]
        self.state["state"] = ("waiting_permission" if any(r["kind"] == "permission"
                              for r in pending) else "waiting_input" if pending else "running")


    async def write(self, message, guard=None, on_written=None):
        async with self.write_lock:
            if guard and not guard():
                raise RuntimeError("interaction is no longer active")
            if not self.process or self.process.stdin.is_closing():
                raise RuntimeError(self.agent_name + " protocol input is closed")
            self.process.stdin.write((encode(self.envelope(message)) + "\n").encode())
            if on_written:
                on_written()
            await self.process.stdin.drain()


    async def rpc(self, method, params, timeout=None):
        self.rpc_sequence += 1
        request_id = self.rpc_sequence
        future = asyncio.get_running_loop().create_future()
        self.waiters[request_id] = (method, future)
        async def exchange():
            await self.write({"jsonrpc": "2.0", "id": request_id,
                              "method": method, "params": params})
            finished, _ = await asyncio.wait([future, self.fatal],
                                              return_when=asyncio.FIRST_COMPLETED)
            if self.fatal in finished:
                raise RuntimeError(self.fatal.result())
            return future.result()
        try:
            if timeout is None:
                return await exchange()
            return await asyncio.wait_for(exchange(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(method + " timed out") from None
        finally:
            self.waiters.pop(request_id, None)
            if not future.done():
                future.cancel()


    async def reader(self):
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    if not self.turn_completed and not self.finishing:
                        self.fail(self.agent_name + " protocol EOF before the turn completed")
                    return
                self.state["last_event_at"] = now()
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    self.protocol.write(encode({"time": now(), "invalid_frame_bytes": len(line),
                                               "error": "malformed_json"}) + "\n")
                    raise RuntimeError(self.agent_name + " sent malformed JSON; see protocol.jsonl")
                self.protocol.write(encode(self.redactor.clean({"time": now(), "message": message})) + "\n")
                if not self.valid_envelope(message):
                    raise RuntimeError(self.agent_name + " sent an invalid JSON-RPC envelope")
                if "method" in message:
                    if "id" in message:
                        await self.incoming_request(message)
                    else:
                        self.notification(message)
                elif "id" in message:
                    waiter = self.waiters.get(message["id"])
                    if not waiter:
                        raise RuntimeError(self.agent_name + " returned an unknown or duplicate response ID")
                    method, future = waiter
                    if future.done():
                        raise RuntimeError(self.agent_name + " returned a duplicate response ID")
                    if "error" in message:
                        future.set_exception(RuntimeError(method + ": " + encode(message["error"])))
                    elif "result" in message:
                        self.response_received(method, message["result"])
                        future.set_result(message["result"])
                    else:
                        raise RuntimeError(self.agent_name + " response has neither result nor error")
                else:
                    raise RuntimeError(self.agent_name + " sent an unrecognized protocol message")
                self.save()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fail(error)


    async def stderr_reader(self):
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    return
                value = line.decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(value)
                except ValueError:
                    cleaned = self.redactor.clean(value)
                else:
                    cleaned = encode(self.redactor.clean(parsed)) + "\n"
                self.stderr.write(cleaned.encode("utf-8"))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fail("failed to capture " + self.agent_name + " stderr: " + str(error))


    async def control(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), self.args.control_timeout_seconds)
            message = json.loads(line)
            operation = message.get("operation")
            if operation == "ping":
                result = {"state": self.state["state"], "pid": os.getpid()}
            elif operation == "cancel":
                if self.state["state"] in FINAL or self.finishing:
                    raise ValueError("task has already finished")
                if not self.cancel_event.is_set():
                    self.state["state"] = "cancelling"
                    self.invalidate_requests("cancelled")
                    self.cancel_event.set()
                    self.event("cancel_requested")
                result = {"state": "cancelling"}
            elif operation == "respond":
                request = self.requests.get(message.get("request_id"))
                if (not request or request["state"] != "pending" or self.finishing or
                        self.state["state"] in FINAL or self.cancel_event.is_set()):
                    raise ValueError("request is missing, already answered, or no longer active")
                response = self.validate_response(request, message.get("response"))
                request["state"] = "responding"  # Reserve before any await: only one responder wins.
                self.save()
                def written():
                    request.update(state="answered", answered_at=now(), response_claimed=True)
                try:
                    await asyncio.wait_for(self.write({"jsonrpc": "2.0", "id": request["rpc_id"],
                                                        "result": response},
                        guard=lambda: not self.finishing and not self.cancel_event.is_set()
                                      and request["state"] == "responding",
                        on_written=written),
                                           self.args.control_timeout_seconds)
                except Exception as error:
                    if request["state"] == "responding":
                        request["state"] = "invalidated"
                    if not self.cancel_event.is_set() and request.get("invalidated_reason") != "interaction_resolved":
                        self.fail("failed to deliver interaction response: " + str(error))
                    raise
                self.interaction_state()
                if not self.finishing:
                    self.event("input_resolved", request_id=request["request_id"], kind=request["kind"])
                result = {"request_id": request["request_id"], "state": "answered"}
            else:
                result = await self.extra_control(message)
            reply = {"ok": True, **result}
        except Exception as error:
            reply = {"ok": False, "error": str(error)}
        try:
            writer.write((encode(self.redactor.clean(reply)) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


    def invalidate_requests(self, reason):
        for request in self.requests.values():
            if request["state"] in {"pending", "responding"}:
                request["state"] = "invalidated"
                request["invalidated_reason"] = reason


    async def heartbeat(self):
        while True:
            await asyncio.sleep(self.args.heartbeat_seconds)
            last = self.state["last_event_at"]
            age = max(0, time.time() - datetime.datetime.fromisoformat(last).timestamp()) if last else None
            self.event("heartbeat", state=self.state["state"],
                       last_event_age_seconds=age, current_tools=self.state["current_tools"],
                       pending_request_ids=[r["request_id"] for r in self.requests.values()
                                            if r["state"] == "pending"])


    async def stop_child(self):
        if not self.process:
            self.state["cleanup_complete"] = True
            return
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except Exception as error:
                self.cleanup_warning("close_child_stdin", error)
        for action, timeout, sig in (("graceful_exit", 0.3, None),
                                     ("terminate_process_group", 3, signal.SIGTERM),
                                     ("kill_process_group", 3, signal.SIGKILL)):
            if sig is not None:
                try:
                    os.killpg(self.process.pid, sig)
                except ProcessLookupError:
                    pass
                except Exception as error:
                    self.cleanup_warning(action, error)
            if self.process.returncode is None:
                try:
                    await asyncio.wait_for(self.process.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
                except Exception as error:
                    self.cleanup_warning("wait_after_" + action, error)
        self.state["child_exit_code"] = self.process.returncode
        group_absent = False
        probe_permission_warning = False
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                group_absent = True
                break
            except PermissionError as error:
                if not probe_permission_warning:
                    self.cleanup_warning("verify_process_group_exit", error)
                    probe_permission_warning = True
                # macOS can return EPERM while a signalled group is being reaped.
                # Retry within the same deadline; only ESRCH confirms its exit.
            except Exception as error:
                self.cleanup_warning("verify_process_group_exit", error)
                break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.05)  # Signals and descendant reaping complete asynchronously.
        self.state["cleanup_complete"] = self.process.returncode is not None and group_absent
        if not self.state["cleanup_complete"]:
            self.cleanup_warning("verify_child_cleanup", RuntimeError("child or process group exit could not be confirmed"))

    def cleanup_warning(self, action, error):
        self.state.setdefault("cleanup_warnings", []).append({
            "action": action, "error": str(error), "error_type": type(error).__name__,
            "errno": getattr(error, "errno", None)})


    async def run(self):
        code = 1
        task = cancelled = None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.cancel_event.set)
        try:
            socket_path = str(self.path / "control.sock")
            if len(os.fsencode(socket_path)) > 100:
                raise ValueError("run directory path is too long for a portable Unix socket")
            self.server = await asyncio.start_unix_server(self.control, path=socket_path, limit=MAX_FRAME)
            os.chmod(socket_path, 0o600)
            self.process = await asyncio.create_subprocess_exec(*self.command(),
                cwd=self.args.cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=MAX_FRAME)
            self.state["child_pid"] = self.process.pid
            self.event("started", child_pid=self.process.pid, run_dir=str(self.path))
            self.background = [asyncio.create_task(self.reader()), asyncio.create_task(self.heartbeat()),
                               asyncio.create_task(self.stderr_reader())]
            task = asyncio.create_task(self.conversation())
            cancelled = asyncio.create_task(self.cancel_event.wait())
            done, _ = await asyncio.wait([task, cancelled, self.fatal],
                timeout=self.args.timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                self.state["state"] = "cancelling"
                self.invalidate_requests("cancelled")
                await self.cancel_protocol()
                self.state["state"] = "cancelled"
                self.state["stop_reason"] = "cancelled"
                code = 130
            elif self.fatal in done:
                raise RuntimeError(self.fatal.result())
            elif task in done:
                await task
                self.state["state"] = "succeeded"
                code = 0
            else:
                raise TimeoutError("configured overall task timeout expired")
        except Exception as error:
            self.state["state"] = "failed"
            self.state["error"] = str(error)
        finally:
            self.finishing = True
            self.invalidate_requests(self.state["state"])
            for pending in [task, cancelled, *self.background]:
                if pending:
                    pending.cancel()
            for pending in [task, cancelled, *self.background]:
                if pending:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pending
            try:
                await self.stop_child()
            except Exception as error:
                self.cleanup_warning("stop_child", error)
                self.state["cleanup_complete"] = False
            if self.server:
                try:
                    self.server.close()
                    await self.server.wait_closed()
                except Exception as error:
                    self.cleanup_warning("close_control_server", error)
            try:
                (self.path / "control.sock").unlink()
            except FileNotFoundError:
                pass
            except Exception as error:
                self.cleanup_warning("unlink_control_socket", error)
            if self.state.get("cleanup_complete") is False:
                code = 1
                self.state["state"] = "failed"
                self.state["error"] = ((self.state.get("error") + "; ") if self.state.get("error") else "") + "process cleanup incomplete"
            self.state["finished_at"] = now()
            self.state["exit_code"] = code
            result = self.result_receipt()
            atomic_json(self.path / "result.json", self.redactor.clean(result))
            self.event("finished", state=self.state["state"], stop_reason=self.state["stop_reason"],
                       error=self.state["error"], exit_code=code, result_path=str(self.path / "result.json"),
                       cleanup_complete=self.state.get("cleanup_complete"),
                       cleanup_warnings=self.state.get("cleanup_warnings", []))
            for handle in (self.protocol, self.events, self.stderr):
                with contextlib.suppress(OSError):
                    handle.close()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
        return code


    def command(self):
        raise NotImplementedError

    def envelope(self, message):
        return message

    def valid_envelope(self, message):
        return isinstance(message, dict) and message.get("jsonrpc") == "2.0"

    def response_received(self, method, result):
        pass

    async def conversation(self):
        raise NotImplementedError

    def notification(self, message):
        raise NotImplementedError

    async def incoming_request(self, message):
        raise NotImplementedError

    def validate_response(self, request, value):
        raise NotImplementedError

    async def cancel_protocol(self):
        raise NotImplementedError

    async def extra_control(self, message):
        raise ValueError("unknown control operation")

    def result_receipt(self):
        return {"text": "".join(self.parts), "stopReason": self.state["stop_reason"],
                  "sessionId": self.state["session_id"], "state": self.state["state"],
                  "error": self.state["error"], "exitCode": self.state["exit_code"],
                  "cleanupComplete": self.state.get("cleanup_complete"),
                  "cleanupWarnings": self.state.get("cleanup_warnings", [])}


async def control_request(path, message, print_result=True):
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(
        str(path / "control.sock"), limit=MAX_FRAME), 5)
    try:
        writer.write((encode(message) + "\n").encode())
        await asyncio.wait_for(writer.drain(), 5)
        line = await asyncio.wait_for(reader.readline(), 20)
        if not line:
            raise RuntimeError("task control socket closed without an acknowledgement")
        result = json.loads(line)
        if print_result:
            print(encode(result))
            return 0 if result.get("ok") else 2
        return result
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def positive(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def base_main(bridge_class, configure_run, configure_controls=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run and supervise one worker turn in the foreground")
    run.add_argument("--cwd", required=True)
    run.add_argument("--prompt-file", required=True)
    run.add_argument("--run-dir", help="new private directory; must not already exist")
    run.add_argument("--resume")
    run.add_argument("--model")
    run.add_argument("--reasoning-effort")
    run.add_argument("--timeout-seconds", type=positive)
    run.add_argument("--heartbeat-seconds", type=positive, default=30)
    run.add_argument("--control-timeout-seconds", type=positive, default=15)
    configure_run(run)
    for name in ("status", "events", "respond", "cancel"):
        sub = commands.add_parser(name)
        sub.add_argument("--run-dir", required=True)
        if name == "events":
            sub.add_argument("--after", type=int, default=0)
            sub.add_argument("--limit", type=int, default=100)
        elif name == "respond":
            sub.add_argument("--request-id", required=True)
            sub.add_argument("--response-file", required=True)
    if configure_controls:
        configure_controls(commands)
    args = parser.parse_args()
    try:
        if args.command == "run":
            if args.heartbeat_seconds > 30:
                raise ValueError("heartbeat interval must be at most 30 seconds")
            if not Path(args.cwd).is_dir():
                raise ValueError("cwd must be an existing directory")
            if not Path(args.prompt_file).is_file():
                raise ValueError("prompt file must exist")
            path = create_directory(args.run_dir)
            async def start():
                return await bridge_class(args, path).run()
            return asyncio.run(start())
        path = private_directory(args.run_dir)
        if args.command == "status":
            data = read_status(path)
            if data["state"] not in FINAL:
                try:
                    response = asyncio.run(control_request(path, {"operation": "ping"}, False))
                    if not response.get("ok") or response.get("pid") != data["pid"]:
                        raise RuntimeError("control socket did not identify the recorded supervisor")
                    data["supervisor_reachable"] = True
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    data.update(recorded_state=data["state"], state="supervisor_unreachable",
                                supervisor_reachable=False, supervisor_error=str(error))
            print(encode(data))
            return 0
        if args.command == "events":
            if args.after < 0 or not 1 <= args.limit <= 10000:
                raise ValueError("after must be nonnegative; limit must be between 1 and 10000")
            entries = []
            with (path / "events.jsonl").open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.endswith("\n"):
                        break  # A concurrent append may be incomplete.
                    entry = json.loads(line)
                    if entry["seq"] > args.after:
                        entries.append(entry)
                        if len(entries) > args.limit:
                            break
            shown = entries[:args.limit]
            print(encode({"events": shown, "next_cursor": shown[-1]["seq"] if shown else args.after,
                          "has_more": len(entries) > args.limit}))
            return 0
        message = {"operation": args.command}
        if args.command == "respond":
            message.update(request_id=args.request_id,
                response=json.loads(Path(args.response_file).read_text(encoding="utf-8")))
        elif hasattr(args, "control_payload"):
            message.update(args.control_payload(args))
        return asyncio.run(control_request(path, message))
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(encode({"ok": False, "error": str(error)}), file=sys.stderr)
        return 2


"""Codex app-server protocol adapter.

Wire contracts are checked against codex-cli 0.152.0's generated JSON schemas.
The app-server uses newline JSON-RPC messages without a jsonrpc member.
"""

import math
from urllib.parse import urlsplit


CODEX_REQUESTS = {
    "item/tool/requestUserInput": "input",
    "item/commandExecution/requestApproval": "permission",
    "item/fileChange/requestApproval": "permission",
    "item/permissions/requestApproval": "permission",
    "mcpServer/elicitation/request": "elicitation",
}
CODEX_TOOL_TYPES = {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
                    "collabAgentToolCall", "subAgentActivity", "webSearch", "imageView",
                    "imageGeneration", "sleep", "contextCompaction"}


def validate_elicitation(schema, value, location="content"):
    """Validate the supported MCP form subset; never ignore unknown constraints."""
    if not isinstance(schema, dict):
        raise ValueError(location + ": unsupported form schema")
    supported = {"$schema", "title", "description", "default", "enumNames", "type", "properties",
                 "required", "additionalProperties", "items", "enum", "const", "oneOf", "anyOf",
                 "minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems", "format"}
    if set(schema) - supported:
        raise ValueError(location + ": unsupported form constraints: " + ", ".join(sorted(set(schema) - supported)))
    kind = schema.get("type")
    matches = {"string": isinstance(value, str), "boolean": isinstance(value, bool),
               "number": type(value) in (int, float) and math.isfinite(value),
               "integer": type(value) is int, "object": isinstance(value, dict),
               "array": isinstance(value, list), "null": value is None}
    if kind is not None and (not isinstance(kind, str) or not matches.get(kind, False)):
        raise ValueError(location + ": value does not match the requested type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(location + ": value is not an advertised enum option")
    if "const" in schema and value != schema["const"]:
        raise ValueError(location + ": value does not match the advertised constant")
    for union in ("oneOf", "anyOf"):
        if union in schema:
            count = 0
            for option in schema[union]:
                try:
                    validate_elicitation(option, value, location)
                    count += 1
                except ValueError:
                    pass
            if count == 0 or (union == "oneOf" and count != 1):
                raise ValueError(location + ": value does not match the form alternatives")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict) or set(value) - set(properties):
            raise ValueError(location + ": only explicitly requested form fields are supported")
        if not set(schema.get("required") or []) <= set(value):
            raise ValueError(location + ": required form fields are missing")
        for key, child in value.items():
            validate_elicitation(properties[key], child, location + "." + key)
    if isinstance(value, list):
        if "items" not in schema:
            raise ValueError(location + ": array schema must specify items")
        for child in value:
            validate_elicitation(schema["items"], child, location + "[]")
    for key, bound, valid in (("minLength", len(value) if isinstance(value, str) else None, lambda a, b: a >= b),
                              ("maxLength", len(value) if isinstance(value, str) else None, lambda a, b: a <= b),
                              ("minItems", len(value) if isinstance(value, list) else None, lambda a, b: a >= b),
                              ("maxItems", len(value) if isinstance(value, list) else None, lambda a, b: a <= b),
                              ("minimum", value if type(value) in (int, float) else None, lambda a, b: a >= b),
                              ("maximum", value if type(value) in (int, float) else None, lambda a, b: a <= b)):
        if schema.get(key) is not None and (bound is None or not valid(bound, schema[key])):
            raise ValueError(location + ": value violates " + key)
    fmt = schema.get("format")
    if fmt:
        valid = False
        if isinstance(value, str):
            if fmt == "email":
                valid = bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value))
            elif fmt == "uri":
                valid = bool(urlsplit(value).scheme) and not any(c.isspace() for c in value)
            elif fmt in {"date", "date-time"}:
                try:
                    if fmt == "date":
                        datetime.date.fromisoformat(value)
                    else:
                        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            raise ValueError("date-time needs timezone")
                    valid = True
                except ValueError:
                    pass
        if not valid:
            raise ValueError(location + ": invalid or unsupported format " + str(fmt))


def validate_granted_permissions(requested, granted):
    if not isinstance(granted, dict) or set(granted) - {"fileSystem", "network"}:
        raise ValueError("permissions must contain only fileSystem and network")
    for key, value in granted.items():
        if value is None:
            continue
        source = requested.get(key) or {}
        if not isinstance(value, dict) or not isinstance(source, dict):
            raise ValueError("granted permissions must be a subset of the request")
        if key == "network":
            if set(value) - {"enabled"} or (value.get("enabled") is not None and type(value["enabled"]) is not bool):
                raise ValueError("network permission requires an enabled boolean")
            if value.get("enabled") is True and source.get("enabled") is not True:
                raise ValueError("network access was not requested")
            continue
        if set(value) - {"entries", "read", "write", "globScanMaxDepth"}:
            raise ValueError("unsupported fileSystem permission fields")
        for field, child in value.items():
            if child is None:
                continue
            if field == "globScanMaxDepth":
                if type(child) is not int or child < 1 or child != source.get(field):
                    raise ValueError("globScanMaxDepth must match the requested limit")
            elif not isinstance(child, list) or any(item not in (source.get(field) or []) for item in child):
                raise ValueError("fileSystem grants must use an exact subset of the requested paths/entries")
        denies = [entry for entry in source.get("entries") or [] if isinstance(entry, dict) and entry.get("access") == "deny"]
        if value and any(entry not in (value.get("entries") or []) for entry in denies):
            raise ValueError("grants must preserve requested fileSystem deny entries")


class Bridge(BaseBridge):
    agent_name = "Codex"

    def __init__(self, args, path):
        super().__init__(args, path)
        self.state.update(thread_id=None, turn_id=None, subagents=[], plan=None, diff=None)
        self.completion = asyncio.get_running_loop().create_future()
        self.early_messages = []
        self.answer_items = {}
        self.subagents = {}
        self.history_turn_ids = set()
        self.output_event_times = {}

    def command(self):
        command = [self.args.codex_bin]
        for override in self.args.config:
            command += ["-c", override]
        if self.args.enable_user_input:
            command += ["-c", "features.default_mode_request_user_input=true",
                        "-c", "tools.experimental_request_user_input.enabled=true"]
        return command + ["app-server", "--listen", "stdio://"]

    def envelope(self, message):
        return {key: value for key, value in message.items() if key != "jsonrpc"}

    def valid_envelope(self, message):
        return isinstance(message, dict) and "jsonrpc" not in message

    def response_received(self, method, result):
        if not isinstance(result, dict):
            raise RuntimeError("Codex " + method + " returned a non-object result")
        if method in {"thread/start", "thread/resume"}:
            thread = result.get("thread", {})
            thread_id = thread.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise RuntimeError("Codex returned no thread.id")
            if self.args.resume and thread_id != self.args.resume:
                raise RuntimeError("Codex resumed a different thread than requested")
            turns = thread.get("turns") or []
            # A saved transcript may contain an abandoned inProgress turn after a crash.
            # Only the live thread status establishes that another turn is still active.
            if isinstance(thread.get("status"), dict) and thread["status"].get("type") == "active":
                raise RuntimeError("the resumed Codex thread already has an active turn")
            self.history_turn_ids = {turn["id"] for turn in turns if isinstance(turn, dict) and isinstance(turn.get("id"), str)}
            self.state.update(thread_id=thread_id, session_id=thread_id)
        elif method == "turn/start":
            turn = result.get("turn", {})
            turn_id = turn.get("id")
            if not isinstance(turn_id, str) or not turn_id or turn_id in self.history_turn_ids:
                raise RuntimeError("Codex returned no new turn.id")
            self.state["turn_id"] = turn_id
            messages, self.early_messages = self.early_messages, []
            for message in messages:
                if "id" in message:
                    self.register_request(message)
                else:
                    self.notification(message)

    def interaction_state(self):
        if self.state["state"] in FINAL or self.state["state"] == "cancelling" or self.turn_completed:
            return
        pending = [r for r in self.requests.values() if r["state"] in {"pending", "responding"} and r.get("blocking", True)]
        self.state["state"] = ("waiting_permission" if any(r["kind"] == "permission" for r in pending)
                               else "waiting_input" if pending else "running")

    def correlated(self, params):
        thread_id, turn_id = params.get("threadId"), params.get("turnId")
        if thread_id == self.state["thread_id"]:
            return turn_id == self.state["turn_id"]
        return thread_id in self.subagents and isinstance(turn_id, str)

    def buffer_early(self, message):
        if len(self.early_messages) >= 10000:
            raise RuntimeError("Codex sent too many events before acknowledging turn/start")
        self.early_messages.append(message)

    def note_subagent(self, thread_id, **fields):
        if not isinstance(thread_id, str) or thread_id == self.state["thread_id"]:
            return
        self.subagents[thread_id] = {**self.subagents.get(thread_id, {}), "thread_id": thread_id, **fields}
        self.state["subagents"] = list(self.subagents.values())
        if thread_id not in self.state["observed_sessions"]:
            self.state["observed_sessions"].append(thread_id)

    def record_item(self, params, completed):
        item = params.get("item", {})
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise RuntimeError("Codex item notification is missing item.id")
        thread_id, turn_id = params["threadId"], params["turnId"]
        kind = item.get("type")
        if kind == "agentMessage":
            if completed and thread_id == self.state["thread_id"] and item.get("phase") in (None, "final_answer"):
                if not isinstance(item.get("text"), str):
                    raise RuntimeError("Codex agentMessage text is not a string")
                self.answer_items[item["id"]] = item["text"]
                self.parts[:] = ["\n\n".join(self.answer_items.values())]
            return
        if kind == "plan" and completed:
            if thread_id == self.state["thread_id"]:
                self.state["plan"] = item.get("text")
            self.event("plan", thread_id=thread_id, turn_id=turn_id, text=item.get("text"))
            return
        if kind not in CODEX_TOOL_TYPES:
            return
        key = (thread_id, turn_id, item["id"])
        previous = self.tools.get(key, {})
        native_status = item.get("status", "completed" if completed else "inProgress")
        terminal = native_status in {"completed", "failed", "declined", "interrupted"}
        status = ("failed" if native_status in {"failed", "declined", "interrupted"}
                  else "completed" if native_status == "completed" else "running")
        if completed and not terminal:
            status = "completed"
        tool = {**previous, "thread_id": thread_id, "session_id": thread_id, "turn_id": turn_id,
                "item_id": item["id"], "type": kind, "status": status, "native_status": native_status,
                "first_observed_at": previous.get("first_observed_at", now())}
        names = {"command": "command", "cwd": "cwd", "processId": "process_id", "exitCode": "exit_code",
                 "durationMs": "duration_ms", "server": "server", "tool": "tool", "query": "query",
                 "path": "path", "senderThreadId": "sender_thread_id", "receiverThreadIds": "receiver_thread_ids",
                 "agentsStates": "agents_states", "agentPath": "agent_path", "agentThreadId": "agent_thread_id",
                 "kind": "activity_kind"}
        for source, target in names.items():
            if source in item:
                tool[target] = item[source]
        for source in ("arguments", "aggregatedOutput", "result", "error"):
            if item.get(source) is not None:
                tool[source + "_summary"] = compact(item[source])
        if kind == "fileChange":
            tool["files"] = [{"path": change.get("path"), "kind": change.get("kind")} for change in item.get("changes", [])]
        if kind == "collabAgentToolCall":
            for child in item.get("receiverThreadIds", []):
                self.note_subagent(child, parent_thread_id=item.get("senderThreadId"),
                                   state=(item.get("agentsStates") or {}).get(child), tool=item.get("tool"))
        elif kind == "subAgentActivity":
            self.note_subagent(item.get("agentThreadId"), agent_path=item.get("agentPath"),
                               state=item.get("kind"), parent_thread_id=thread_id)
        self.tools[key] = tool
        if tool != previous:
            self.event("tool", tool=tool)

    def notification(self, message):
        method, params = message["method"], message.get("params") or {}
        if not isinstance(params, dict):
            raise RuntimeError("Codex notification params must be an object")
        if self.prompt_active and not self.state["turn_id"]:
            self.buffer_early(message)
            return
        if method == "serverRequest/resolved":
            for request in self.requests.values():
                if (request["rpc_id"] == params.get("requestId") and
                        request["params"].get("threadId") == params.get("threadId") and
                        request["state"] in {"pending", "responding"}):
                    request.update(state="invalidated", invalidated_reason="interaction_resolved")
                    self.event("input_invalidated", request_id=request["request_id"], reason="serverRequest/resolved")
            self.interaction_state()
            return
        if not self.prompt_active or self.turn_completed:
            return
        if not self.state["turn_id"]:
            self.buffer_early(message)
            return
        if method == "thread/started":
            thread = params.get("thread") or {}
            source = thread.get("source") or {}
            subagent = source.get("subAgent", {}) if isinstance(source, dict) else {}
            spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}
            parent = spawn.get("parent_thread_id")
            if parent == self.state["thread_id"] or parent in self.subagents:
                self.note_subagent(thread.get("id"), parent_thread_id=parent, agent_path=spawn.get("agent_path"),
                                   state=thread.get("status"))
                self.event("subagent", thread_id=thread.get("id"), parent_thread_id=parent)
            return
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn") or {}
            params = {**params, "turnId": turn.get("id")}
        if not self.correlated(params):
            return
        if method in {"item/started", "item/completed"}:
            self.record_item(params, method == "item/completed")
        elif method in {"turn/plan/updated", "turn/diff/updated"}:
            field = "plan" if method == "turn/plan/updated" else "diff"
            if params["threadId"] == self.state["thread_id"]:
                self.state[field] = params.get(field)
            self.event(field, thread_id=params["threadId"], turn_id=params["turnId"],
                       **{field: params.get(field)}, explanation=params.get("explanation"))
        elif method == "turn/completed":
            turn = params["turn"]
            if turn.get("status") not in {"completed", "failed", "interrupted"}:
                raise RuntimeError("Codex turn/completed has a nonterminal status")
            for item in turn.get("items") or []:
                self.record_item({**params, "item": item}, True)
            if params["threadId"] != self.state["thread_id"]:
                for request in self.requests.values():
                    if (request["params"].get("threadId") == params["threadId"] and
                            request["params"].get("turnId") == params["turnId"] and
                            request["state"] in {"pending", "responding"}):
                        request.update(state="invalidated", invalidated_reason="interaction_resolved")
                for tool in self.tools.values():
                    if (tool.get("thread_id") == params["threadId"] and tool.get("turn_id") == params["turnId"] and
                            tool.get("status") not in TERMINAL_TOOLS):
                        tool.update(status="failed", incomplete_at_turn_end=True)
                self.note_subagent(params["threadId"], state=turn.get("status"), turn_id=params["turnId"])
                self.interaction_state()
                self.event("subagent_turn_completed", thread_id=params["threadId"], status=turn.get("status"))
                return
            self.turn_completed = True
            self.prompt_active = False
            self.state["stop_reason"] = turn["status"]
            self.state["native_turn_status"] = turn["status"]
            self.invalidate_requests("interaction_resolved")
            for tool in self.tools.values():
                if tool.get("status") not in TERMINAL_TOOLS:
                    tool.update(status="failed", incomplete_at_turn_end=True)
            self.event("turn_completed", thread_id=params["threadId"], turn_id=params["turnId"],
                       status=turn["status"], error=turn.get("error"))
            if turn["status"] == "interrupted":
                self.cancel_event.set()
            if not self.completion.done():
                self.completion.set_result(turn)
        elif method == "item/commandExecution/outputDelta":
            item_id, delta = params.get("itemId"), params.get("delta")
            if not isinstance(item_id, str) or not isinstance(delta, str):
                raise RuntimeError("Codex command output delta is missing itemId or delta")
            key = (params["threadId"], params["turnId"], item_id)
            tool = self.tools.setdefault(key, {"thread_id": params["threadId"], "session_id": params["threadId"],
                "turn_id": params["turnId"], "item_id": item_id, "type": "commandExecution",
                "status": "running", "first_observed_at": now()})
            tool["output_tail"] = (tool.get("output_tail", "") + delta)[-2000:]
            tool["last_output_at"] = now()
            observed = time.monotonic()
            if observed - self.output_event_times.get(key, float("-inf")) >= 1:
                self.output_event_times[key] = observed
                self.event("tool_output", thread_id=params["threadId"], turn_id=params["turnId"],
                           item_id=item_id, output_tail=tool["output_tail"])
        elif method == "error":
            self.event("agent_error", thread_id=params["threadId"], turn_id=params["turnId"],
                       error=params.get("error"), will_retry=params.get("willRetry"))
        # Per-token deltas and legacy codex/event notifications stay in protocol.jsonl.

    def register_request(self, message):
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("threadId"), str):
            raise RuntimeError("Codex client request is missing threadId")
        if not self.prompt_active or self.turn_completed:
            raise RuntimeError("Codex requested interaction outside this turn")
        if not self.correlated(params) and not (message["method"] == "mcpServer/elicitation/request" and
                params.get("turnId") is None and params["threadId"] == self.state["thread_id"]):
            raise RuntimeError("Codex requested interaction for a different thread or turn")
        if any(request["rpc_id"] == message["id"] for request in self.requests.values()):
            raise RuntimeError("Codex reused a server request ID")
        kind = CODEX_REQUESTS[message["method"]]
        if kind == "input":
            questions = params.get("questions")
            if not isinstance(params.get("isBlocking"), bool) or not isinstance(questions, list) or not questions:
                raise RuntimeError("Codex question is missing isBlocking or questions")
            ids = [q.get("id") if isinstance(q, dict) else None for q in questions]
            if not all(isinstance(qid, str) and qid for qid in ids) or len(set(ids)) != len(ids):
                raise RuntimeError("Codex question IDs must be unique nonempty strings")
        self.request_sequence += 1
        request_id = "req-" + str(self.request_sequence)
        request = {"request_id": request_id, "rpc_id": message["id"], "kind": kind,
                   "method": message["method"], "params": params, "state": "pending", "created_at": now(),
                   "blocking": params["isBlocking"] if kind == "input" else True}
        self.requests[request_id] = request
        self.interaction_state()
        self.event("permission_requested" if kind == "permission" else "input_requested", request=request)

    async def incoming_request(self, message):
        if message["method"] not in CODEX_REQUESTS:
            await self.write({"id": message["id"], "error": {"code": -32601,
                              "message": "Unsupported client method: " + message["method"]}})
            self.event("unsupported_request", method=message["method"])
            self.fail("unsupported Codex client request: " + message["method"])
            return
        if self.prompt_active and not self.state["turn_id"]:
            self.buffer_early(message)
            return
        self.register_request(message)

    def validate_response(self, request, value):
        if not isinstance(value, dict):
            raise ValueError("response must be a JSON object")
        method, params = request["method"], request["params"]
        if method == "item/tool/requestUserInput":
            if set(value) != {"answers"} or not isinstance(value["answers"], dict):
                raise ValueError("question response requires an answers object")
            questions = params["questions"]
            if set(value["answers"]) != {question["id"] for question in questions}:
                raise ValueError("answers must use every exact question ID")
            for question in questions:
                answer = value["answers"][question["id"]]
                if (not isinstance(answer, dict) or set(answer) != {"answers"} or
                        not isinstance(answer["answers"], list) or not answer["answers"] or
                        not all(isinstance(text, str) and text.strip() for text in answer["answers"])):
                    raise ValueError("each question response must contain a nonempty answers array of strings")
            for question in questions:
                if question.get("isSecret"):
                    self.redactor.remember(value["answers"][question["id"]]["answers"])
            return value
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            decision = value.get("decision")
            if set(value) != {"decision"} or not isinstance(decision, str) or decision not in {"accept", "acceptForSession", "decline", "cancel"}:
                raise ValueError("approval decision must be accept, acceptForSession, decline, or cancel")
            available = params.get("availableDecisions")
            if available is not None and decision not in available:
                raise ValueError("approval decision was not advertised for this request")
            return value
        if method == "item/permissions/requestApproval":
            if (set(value) - {"permissions", "scope", "strictAutoReview"} or "permissions" not in value or
                    value.get("scope", "turn") not in {"turn", "session"} or
                    ("strictAutoReview" in value and type(value["strictAutoReview"]) is not bool)):
                raise ValueError("permissions response requires permissions and optional turn/session scope")
            validate_granted_permissions(params.get("permissions") or {}, value["permissions"])
            return value
        if method == "mcpServer/elicitation/request":
            if set(value) - {"action", "content"} or value.get("action") not in {"accept", "decline", "cancel"}:
                raise ValueError("elicitation action must be accept, decline, or cancel")
            if value["action"] != "accept" or params.get("mode") == "url":
                if value.get("content") is not None:
                    raise ValueError("URL, declined, and cancelled elicitations must not contain form data")
            else:
                validate_elicitation(params.get("requestedSchema"), value.get("content"))
            return value
        raise ValueError("unsupported interaction response")

    async def conversation(self):
        timeout = self.args.control_timeout_seconds
        self.state["state"] = "initializing"
        self.event("initializing")
        initialized = await self.rpc("initialize", {"clientInfo": {"name": "codex-cli-skill-supervisor", "version": "1.0.0"},
                                     "capabilities": {"experimentalApi": True}}, timeout)
        await self.write({"method": "initialized"})
        self.event("initialized", server_info=initialized)
        self.state["state"] = "creating_session"
        self.save()
        params = {"cwd": str(Path(self.args.cwd).resolve()), "sandbox": self.args.sandbox,
                  "approvalPolicy": "on-request", "approvalsReviewer": self.args.approval_reviewer}
        if self.args.model:
            params["model"] = self.args.model
        if self.args.resume:
            params["threadId"] = self.args.resume
        created = await self.rpc("thread/resume" if self.args.resume else "thread/start", params, timeout)
        expected_sandbox = "workspaceWrite" if self.args.sandbox == "workspace-write" else "readOnly"
        if (not isinstance(created.get("sandbox"), dict) or created["sandbox"].get("type") != expected_sandbox or
                created.get("approvalPolicy") != "on-request" or
                created.get("approvalsReviewer") != self.args.approval_reviewer):
            raise RuntimeError("Codex did not apply the requested sandbox and approval settings")
        model = created.get("model")
        if not isinstance(model, str) or not model:
            raise RuntimeError("Codex thread response has no model; cannot apply collaboration settings")
        effort = self.args.reasoning_effort or created.get("reasoningEffort")
        self.state.update(model=model, reasoning_effort=effort, sandbox=created.get("sandbox"),
                          approval_policy=created.get("approvalPolicy"), approval_reviewer=created.get("approvalsReviewer"),
                          collaboration_mode=self.args.collaboration_mode)
        self.parts.clear()
        self.tools.clear()
        self.state["observed_sessions"] = [self.state["thread_id"]]
        self.state["state"] = "running"
        self.event("session_ready", session_id=self.state["thread_id"], thread_id=self.state["thread_id"],
                   resumed=bool(self.args.resume), model=model, reasoning_effort=effort,
                   collaboration_mode=self.args.collaboration_mode)
        prompt = Path(self.args.prompt_file).read_text(encoding="utf-8")
        if not prompt.strip():
            raise ValueError("prompt file must not be empty")
        self.prompt_active = True
        await self.rpc("turn/start", {"threadId": self.state["thread_id"], "input": [{"type": "text", "text": prompt}],
                       "collaborationMode": {"mode": self.args.collaboration_mode,
                           "settings": {"model": model, "reasoning_effort": effort, "developer_instructions": None}}}, timeout)
        finished, _ = await asyncio.wait([self.completion, self.fatal], return_when=asyncio.FIRST_COMPLETED)
        if self.fatal in finished:
            raise RuntimeError(self.fatal.result())
        turn = self.completion.result()
        if turn["status"] != "completed":
            raise RuntimeError("Codex turn stopped with status " + turn["status"] + ": " + compact(turn.get("error")))
        if not "".join(self.parts).strip():
            raise RuntimeError("Codex completed without a nonempty final assistant answer")

    async def cancel_protocol(self):
        if self.state["thread_id"] and self.state["turn_id"] and not self.turn_completed:
            # Interrupt owns cancellation of pending server requests. Do not send fabricated answers.
            with contextlib.suppress(Exception):
                await self.rpc("turn/interrupt", {"threadId": self.state["thread_id"], "turnId": self.state["turn_id"]}, 2)

    async def extra_control(self, message):
        if message.get("operation") != "steer":
            raise ValueError("unknown control operation")
        if (not self.prompt_active or not self.state["turn_id"] or self.turn_completed or
                self.finishing or self.cancel_event.is_set()):
            raise ValueError("steer requires an active turn; use exact-thread resume after completion")
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("steering text must not be empty")
        turn_id = self.state["turn_id"]
        result = await self.rpc("turn/steer", {"threadId": self.state["thread_id"], "expectedTurnId": turn_id,
                                 "input": [{"type": "text", "text": text}]}, self.args.control_timeout_seconds)
        if not self.finishing:
            self.event("steered", thread_id=self.state["thread_id"], turn_id=turn_id)
        return {"thread_id": self.state["thread_id"], "turn_id": turn_id, "result": result}

    def result_receipt(self):
        if self.state["state"] == "cancelled" and self.state.get("native_turn_status") == "interrupted":
            self.state["stop_reason"] = "interrupted"
        return {**super().result_receipt(), "threadId": self.state["thread_id"], "turnId": self.state["turn_id"],
                "nativeTurnStatus": self.state.get("native_turn_status")}


def configure_run(parser):
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--sandbox", choices=("read-only", "workspace-write"), default="read-only")
    parser.add_argument("--approval-reviewer", choices=("user", "auto_review"), default="user")
    parser.add_argument("--collaboration-mode", choices=("default", "plan"), default="default")
    parser.add_argument("--config", action="append", default=[], metavar="KEY=TOML_VALUE",
                        help="repeatable Codex configuration override, scoped to this child process")
    parser.add_argument("--enable-user-input", action="store_true",
                        help="enable experimental request_user_input and default-mode questions for this process")


def configure_controls(commands):
    steer = commands.add_parser("steer", help="send additional user input to this run's active Codex turn")
    steer.add_argument("--run-dir", required=True)
    steer.add_argument("--text-file", required=True)
    steer.set_defaults(control_payload=lambda args: {"text": Path(args.text_file).read_text(encoding="utf-8")})


def main():
    return base_main(Bridge, configure_run, configure_controls)


if __name__ == "__main__":
    sys.exit(main())
