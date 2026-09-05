#!/usr/bin/env python3
"""Standalone foreground supervision for the Grok ACP protocol.

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


"""Grok ACP protocol adapter."""

QUESTION_METHODS = {"x.ai/ask_user_question", "_x.ai/ask_user_question"}
PLAN_METHODS = {"x.ai/exit_plan_mode", "_x.ai/exit_plan_mode"}


class Bridge(BaseBridge):
    agent_name = "Grok"

    def command(self):
        command = [self.args.grok_bin, "--no-auto-update", "agent", "--no-leader"]
        if not self.args.ask_permissions:
            command.append("--always-approve")
        if self.args.model:
            command += ["--model", self.args.model]
        if self.args.reasoning_effort:
            command += ["--reasoning-effort", self.args.reasoning_effort]
        command += ["stdio"]
        return command

    def response_received(self, method, result):
        if method == "session/prompt":
            self.turn_completed = True

    async def cancel_protocol(self):
        for request in self.requests.values():
            if request.get("invalidated_reason") == "cancelled" and not request.get("response_claimed"):
                request["response_claimed"] = True
                outcome = {"outcome": "cancelled"}
                if request["kind"] == "permission":
                    outcome = {"outcome": outcome}
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.write({"jsonrpc": "2.0", "id": request["rpc_id"],
                                                       "result": outcome}), 1)
        if self.state["session_id"] and not self.turn_completed:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.write({"jsonrpc": "2.0", "method": "session/cancel",
                    "params": {"sessionId": self.state["session_id"]}}), 1)


    def notification(self, message):
        params = message.get("params", {})
        if message["method"].startswith("_") and isinstance(params, dict) and "method" in params:
            params = params.get("params", {})
        update = params.get("update", {})
        if not isinstance(update, dict) or not update.get("sessionUpdate"):
            self.event("notification", method=message["method"])
            return
        session_id = params.get("sessionId")
        expected = self.state["session_id"]
        if not self.prompt_active:
            return  # session/load replays history; it is not work performed by this turn.
        if session_id and session_id not in self.state["observed_sessions"]:
            self.state["observed_sessions"].append(session_id)
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk" and session_id == expected:
            content = update.get("content", {})
            if content.get("type") == "text" and isinstance(content.get("text"), str):
                self.parts.append(content["text"])
        elif kind in {"tool_call", "tool_call_update"}:
            tool_id = update.get("toolCallId")
            if not isinstance(tool_id, str) or not tool_id:
                raise RuntimeError("tool event is missing toolCallId")
            key = (session_id, tool_id)
            previous = self.tools.get(key, {})
            tool = {**previous, "session_id": session_id, "tool_call_id": tool_id}
            if not previous:
                tool["first_observed_at"] = now()
            for field in ("title", "kind", "status", "locations"):
                if field in update:
                    tool[field] = update[field]
            raw = update.get("rawInput")
            meta = update.get("_meta")
            metadata = meta.get("x.ai/tool", {}) if isinstance(meta, dict) else {}
            if isinstance(metadata, dict):
                if metadata.get("name"):
                    tool["name"] = metadata["name"]
                raw = metadata.get("input", raw)
            if isinstance(raw, dict):
                command = raw.get("command", raw.get("cmd"))
                if isinstance(command, str):
                    tool["command"] = command[:500]
            for field in ("rawInput", "rawOutput", "content"):
                if field in update:
                    tool[field + "_summary"] = compact(update[field])
            self.tools[key] = tool
            if tool != previous:
                self.event("tool", tool=tool)
        elif kind == "plan":
            self.event("plan", entries=update.get("entries", []))
        elif kind in {"pending_interaction", "interaction_resolved", "turn_started", "turn_completed"}:
            if kind == "interaction_resolved":
                for request in self.requests.values():
                    request_params = request["params"]
                    tool_id = request_params.get("toolCallId", request_params.get("toolCall", {}).get("toolCallId"))
                    if (request_params.get("sessionId") == session_id and tool_id == update.get("tool_call_id")
                            and tool_id and request["state"] in {"pending", "responding"}):
                        request.update(state="invalidated", invalidated_reason="interaction_resolved")
                self.interaction_state()
            fields = {key: update[key] for key in ("tool_call_id", "kind", "prompt_id", "stop_reason",
                      "error_kind", "usage", "elapsed_ms") if key in update}
            self.event("session_event", session_id=session_id, session_update=kind, **fields)


    async def incoming_request(self, message):
        method = message["method"]
        params = message.get("params", {})
        if method == "session/request_permission":
            kind = "permission"
        elif method in QUESTION_METHODS:
            kind = "input"
        elif method in PLAN_METHODS:
            kind = "plan_approval"
        else:
            await self.write({"jsonrpc": "2.0", "id": message["id"], "error":
                              {"code": -32601, "message": "Unsupported client method: " + method}})
            self.event("unsupported_request", method=method)
            self.fail("unsupported Grok client request: " + method)
            return
        if method.startswith("_"):
            if params.get("method") != method[1:]:
                raise RuntimeError("unsupported wrapped interaction method")
            params = params.get("params", {})
        if not isinstance(params, dict):
            raise RuntimeError("client request params must be an object")
        if not isinstance(params.get("sessionId"), str):
            raise RuntimeError("client request is missing sessionId")
        if any(r["rpc_id"] == message["id"] for r in self.requests.values()):
            raise RuntimeError("Grok reused a client request ID")
        self.request_sequence += 1
        request_id = "req-" + str(self.request_sequence)
        request = {"request_id": request_id, "rpc_id": message["id"], "kind": kind,
                   "method": method, "params": params, "state": "pending", "created_at": now()}
        self.requests[request_id] = request
        self.interaction_state()
        self.event("permission_requested" if kind == "permission" else "input_requested",
                   request=request)


    def validate_response(self, request, value):
        if not isinstance(value, dict):
            raise ValueError("response must be a JSON object")
        if request["kind"] == "plan_approval":
            if value.get("outcome") not in {"approved", "cancelled", "abandoned"}:
                raise ValueError("plan response outcome must be approved, cancelled, or abandoned")
            if set(value) - {"outcome", "feedback"} or ("feedback" in value and
                    (value["outcome"] != "cancelled" or not isinstance(value["feedback"], str))):
                raise ValueError("only cancelled plans may carry string feedback")
            return value
        if request["kind"] == "permission":
            if value == {"outcome": "cancelled"}:
                return {"outcome": {"outcome": "cancelled"}}
            option = value.get("optionId")
            options = request["params"].get("options", [])
            if set(value) != {"optionId"} or option not in [o.get("optionId") for o in options]:
                raise ValueError("permission response requires an advertised optionId or cancelled")
            return {"outcome": {"outcome": "selected", "optionId": option}}
        if value == {"outcome": "cancelled"}:
            return value
        if value.get("outcome") != "accepted" or not isinstance(value.get("answers"), dict):
            raise ValueError("question response requires outcome accepted and an answers object")
        questions = request["params"].get("questions", [])
        if not isinstance(questions, list):
            raise ValueError("Grok question payload has no questions array")
        texts = {q.get("question") for q in questions if isinstance(q, dict)}
        if None in texts or set(value["answers"]) != texts:
            raise ValueError("answers must use every exact question text as their keys")
        annotations = value.get("annotations", {})
        if not isinstance(annotations, dict) or set(annotations) - texts:
            raise ValueError("annotations must be keyed by an exact question text")
        for question in questions:
            answer = value["answers"][question["question"]]
            if not isinstance(answer, list) or not answer or not all(isinstance(a, str) for a in answer):
                raise ValueError("each answer must be a nonempty array of strings")
            options = {o.get("label") for o in question.get("options", []) if isinstance(o, dict)}
            if options and any(a not in options | {"Other"} for a in answer):
                raise ValueError("answer must use an advertised option label or Other")
            if not question.get("multiSelect", False) and len(answer) != 1:
                raise ValueError("single-selection questions require exactly one answer")
            annotation = annotations.get(question["question"], {})
            if not isinstance(annotation, dict):
                raise ValueError("question annotation must be an object")
            if "Other" in answer and not isinstance(annotation.get("notes"), str):
                raise ValueError("Other answers require string annotations.notes with the custom answer")
        return value


    async def conversation(self):
        timeout = self.args.control_timeout_seconds
        self.state["state"] = "initializing"
        self.event("initializing")
        initialized = await self.rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout)
        if not isinstance(initialized, dict) or initialized.get("protocolVersion") != 1:
            raise RuntimeError("Grok did not negotiate ACP protocol version 1")
        self.event("initialized", agent_info=initialized.get("agentInfo"),
                   agent_capabilities=initialized.get("agentCapabilities", {}))
        methods = {m.get("id") for m in initialized.get("authMethods", [])}
        auth = self.args.auth_method or ("xai.api_key" if os.environ.get("XAI_API_KEY") and
                                          "xai.api_key" in methods else "cached_token")
        if auth not in methods:
            raise RuntimeError("Grok does not advertise the selected authentication method: " + auth)
        self.state["state"] = "authenticating"
        self.save()
        await self.rpc("authenticate", {"methodId": auth, "_meta": {"headless": True}}, timeout)
        params = {"cwd": str(Path(self.args.cwd).resolve()), "mcpServers": [],
                  "_meta": {"askUserQuestion": True, "yoloMode": not self.args.ask_permissions}}
        if self.args.reasoning_effort:
            params["_meta"]["reasoningEffort"] = self.args.reasoning_effort
        self.state["state"] = "creating_session"
        self.save()
        if self.args.resume:
            if not initialized.get("agentCapabilities", {}).get("loadSession"):
                raise RuntimeError("Grok does not advertise session/load support")
            params["sessionId"] = self.args.resume
            self.state["session_id"] = self.args.resume
            created = await self.rpc("session/load", params, timeout)
            session_id = self.args.resume
        else:
            if self.args.model:
                params["_meta"]["modelId"] = self.args.model
            created = await self.rpc("session/new", params, timeout)
            session_id = created.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise RuntimeError("Grok session/new returned no sessionId")
            self.state["session_id"] = session_id
        if self.args.model or self.args.reasoning_effort:
            models = created.get("models", {}) if isinstance(created, dict) else {}
            model_id = self.args.model or models.get("currentModelId")
            if not model_id:
                raise RuntimeError("session response has no currentModelId; cannot safely apply reasoning effort")
            settings = {"sessionId": session_id, "modelId": model_id}
            if self.args.reasoning_effort:
                settings["_meta"] = {"reasoningEffort": self.args.reasoning_effort}
            await self.rpc("session/set_model", settings, timeout)
            self.event("session_configured", model_id=model_id, reasoning_effort=self.args.reasoning_effort)
        self.parts.clear()  # A loaded transcript is history, not this turn's answer.
        self.tools.clear()
        self.state["state"] = "running"
        self.event("session_ready", session_id=session_id, resumed=bool(self.args.resume))
        prompt = Path(self.args.prompt_file).read_text(encoding="utf-8")
        if not prompt.strip():
            raise ValueError("prompt file must not be empty")
        self.prompt_active = True
        result = await self.rpc("session/prompt", {"sessionId": session_id,
            "prompt": [{"type": "text", "text": prompt}]})
        if not isinstance(result, dict):
            raise RuntimeError("Grok session/prompt returned a non-object result")
        self.state["stop_reason"] = result.get("stopReason")
        if result.get("stopReason") != "end_turn":
            raise RuntimeError("Grok turn stopped with reason: " + str(result.get("stopReason")))
        if not "".join(self.parts).strip():
            raise RuntimeError("Grok returned end_turn without a nonempty assistant answer")



def configure_run(parser):
    parser.add_argument("--auth-method", choices=("cached_token", "xai.api_key"))
    parser.add_argument("--ask-permissions", action="store_true",
                        help="request Grok permission callbacks instead of default always-approve")
    parser.add_argument("--grok-bin", default="grok")


def main():
    return base_main(Bridge, configure_run)


if __name__ == "__main__":
    sys.exit(main())
