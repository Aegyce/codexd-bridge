"""Exercise installed MCP session operations against native daemons and a loopback model fixture."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from codexd_bridge.config import Backend, load_backends
from codexd_bridge.delegation import OperatorGuard
from codexd_bridge.probe import PROBE_MARKER, check_model
from codexd_bridge.providers import _minimal_catalog
from codexd_bridge.provisioning import _toml_dumps, daemon_action
from codexd_bridge.sessions import Bridge
from codexd_bridge.transport import AppServer, BridgeError


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class ModelFixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        self.server.received.set()
        if not self.server.release.wait(15):
            self.send_error(504)
            return
        item = {"type": "message", "id": "msg_" + uuid4().hex, "role": "assistant",
                "status": "completed", "content": [
                    {"type": "output_text", "text": PROBE_MARKER, "annotations": []}]}
        response = {"id": "resp_" + uuid4().hex, "object": "response", "model": body["model"],
                    "status": "completed", "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
        events = [
            {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {**item, "status": "in_progress", "content": []}},
            {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "delta": PROBE_MARKER},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        data = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                       for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Native steering may replace a pending model stream.


@contextmanager
def native_fixture(root):
    root.mkdir(mode=0o700)
    http = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
    http.requests, http.received, http.release = [], threading.Event(), threading.Event()
    http.release.set()
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    processes, logs, routes = [], [], {}
    try:
        for name in ("a", "b"):
            home = root / name
            home.mkdir(mode=0o700)
            catalog = _minimal_catalog("test-model", None, 32768)
            catalog["models"] += _minimal_catalog("second-model", "none", 32768)["models"]
            (home / "models.json").write_text(json.dumps(catalog))
            (home / "config.toml").write_text(_toml_dumps({
                "model_provider": name, "model": "test-model",
                "model_catalog_json": str(home / "models.json"),
                "model_providers": {name: {"name": name,
                    "base_url": f"http://127.0.0.1:{http.server_port}/v1", "wire_api": "responses",
                    "requires_openai_auth": False, "request_max_retries": 0, "stream_max_retries": 0}},
            }))
            socket = home / "app-server-control/app-server-control.sock"
            socket.parent.mkdir()
            log = (root / (name + ".log")).open("w")
            logs.append(log)
            process = subprocess.Popen([shutil.which("codex"), "app-server", "--listen", "unix://" + str(socket)],
                env={"PATH": os.environ["PATH"], "HOME": str(root), "CODEX_HOME": str(home)}, stdout=log, stderr=log)
            processes.append(process)
            for _ in range(100):
                try:
                    with AppServer(home):
                        break
                except BridgeError:
                    require(process.poll() is None, "Disposable native process exited; inspect its log")
                    time.sleep(.1)
            else:
                raise RuntimeError("Disposable native process did not become ready")
            routes[name] = {"home": str(home), "model_provider": name, "default_model": "test-model"}
        registry = root / "routes.toml"
        registry.write_text(_toml_dumps({"backends": routes}))
        yield registry, http
    finally:
        http.release.set()
        for process in processes:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()
        http.shutdown()
        http.server_close()
        worker.join(timeout=5)


def provider_setup(registry, http, command):
    """Exercise the installed onboarding command against the same local model fixture."""
    root = registry.parent
    home = root / ("onboarded-" + "long-home-" * 8)
    env = {"PATH": os.environ["PATH"], "HOME": str(root),
           "BRIDGE_FIXTURE_KEY": "local-fixture-only"}
    argv = [str(command), "--config", str(registry)]
    try:
        result = subprocess.run([*argv, "provider", "add", "onboarded", "--home", str(home),
            "--base-url", f"http://127.0.0.1:{http.server_port}/v1", "--env-key", "BRIDGE_FIXTURE_KEY",
            "--model", "test-model", "--context-window", "32768", "--launcher-dir", str(root / "bin")],
            env=env, cwd="/", capture_output=True, text=True, timeout=90)
        require(result.returncode == 0, "Installed provider setup failed: " + result.stderr[-2000:])
        result = json.loads(result.stdout)
        require(result["enabled"] and result["verification"]["deleted"]
                and result["registration"]["new_sessions_ready"], "Provider setup did not complete")
        require(load_backends(registry)["onboarded"].enabled, "New provider route was not enabled")
        status = subprocess.run([result["launcher"], "server", "status"], env=env, cwd="/",
                                capture_output=True, text=True, check=True, timeout=15)
        require(json.loads(status.stdout)["status"] == "running", "Generated launcher cannot find its daemon")
        removed = subprocess.run([*argv, "daemon", "remove", "onboarded", "--purge"], env=env, cwd="/",
                                 capture_output=True, text=True, timeout=60)
        require(removed.returncode == 0, "Installed provider removal failed: " + removed.stderr[-2000:])
        require(not home.exists() and not Path(result["launcher"]).exists(), "Provider removal left owned files")
        require(set(load_backends(registry)) == {"a", "b"}, "Provider removal affected other routes")
    finally:
        if home.exists():
            daemon_action(Backend("onboarded", home, "onboarded"), "stop")


async def exercise(registry, http, command):
    bridge = Bridge(registry, guard=OperatorGuard(registry))
    root = registry.parent

    async def completed(backend, thread_id, turn_id):
        for _ in range(100):
            await asyncio.sleep(.1)
            result = await asyncio.to_thread(bridge.read_session, backend, thread_id)
            turn = next((turn for turn in result["turns"] if turn["id"] == turn_id), None)
            if turn and turn["status"] == "completed":
                return result
            require(not turn or not turn["error"], "Native session failed: " + str(turn))
        raise RuntimeError("Native session did not complete")

    probe = await asyncio.to_thread(check_model, load_backends(registry)["a"])
    require(probe["available"] and probe["deleted"], "Probe did not verify and clean up")
    parent = bridge.create_session("a", "Return the marker.", "Parent", str(root), sandbox="read-only")
    parent_state = await completed("a", parent["thread_id"], parent["turn_id"])
    params = StdioServerParameters(command=str(command), args=["--config", str(registry), "serve"],
        env={"PATH": os.environ["PATH"], "HOME": str(root), "CODEXD_BRIDGE_CALLER_HOME": str(root / "a")})
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()

        async def call(name, args, caller, *, error=None):
            state = bridge.read_session("a", caller)["session"]
            result = await client.call_tool(name, args, meta={"threadId": caller, "sessionId": state["sessionId"]})
            text = result.content[0].text
            if error:
                require(result.is_error and error in text, "Expected rejection: " + text)
                return None
            require(not result.is_error, "MCP operation failed: " + text)
            return json.loads(text)

        source = parent
        for depth, role in ((1, "reviewer"), (2, "tester")):
            criteria = json.dumps({"evidence": role})
            child = await call("fork_session", {"backend": "a", "thread_id": source["thread_id"],
                "prompt": "Task for " + role, "title": role, "session_type": role,
                "acceptance_criteria": criteria}, source["thread_id"])
            state = await completed("a", child["thread_id"], child["turn_id"])
            record = child["delegation"]
            require(record["depth"] == depth and record["parent"]["thread_id"] == source["thread_id"]
                and record["parent_delegation_id"] == source["delegation"]["delegation_id"]
                and record["session_type"] == role and record["task_requirements"] == "Task for " + role
                and record["acceptance_criteria"] == criteria and state["session"]["delegation"] == record,
                "Nested assignment lost its identity or task details")
            source = child
        leaf = source
        await call("send_message", {"backend": "a", "thread_id": parent["thread_id"], "prompt": "Cycle"},
                   leaf["thread_id"], error="upstream")
        changed = await call("send_message", {"backend": "a", "thread_id": leaf["thread_id"],
            "prompt": "Use the second model", "model": "second-model", "effort": "none"}, parent["thread_id"])
        state = await completed("a", changed["thread_id"], changed["turn_id"])
        require(state["session"]["model"] == "second-model" and state["session"]["reasoningEffort"] == "none",
                "Explicit model and effort did not persist")
        http.release.clear()
        http.received.clear()
        try:
            active = await call("send_message", {"backend": "a", "thread_id": leaf["thread_id"],
                "prompt": "Wait for additional instructions"}, parent["thread_id"])
            require(await asyncio.to_thread(http.received.wait, 5), "Native model request did not start")
            require(active["model"] == "second-model" and active["effort"] == "none", "Continuation changed selection")
            await call("send_message", {"backend": "a", "thread_id": leaf["thread_id"], "prompt": "Duplicate"},
                       parent["thread_id"], error="not idle")
            steered = await call("steer_session", {"backend": "a", "thread_id": leaf["thread_id"],
                "expected_turn_id": active["turn_id"], "prompt": "Additional acceptance detail"}, parent["thread_id"])
            require(steered["accepted"], "Active turn did not accept steering")
        finally:
            http.release.set()
        await completed("a", active["thread_id"], active["turn_id"])
        cross = await call("create_session", {"backend": "b", "prompt": "Independent provider task",
            "title": "Cross provider", "cwd": str(root), "sandbox": "read-only"}, parent["thread_id"])
        await completed("b", cross["thread_id"], cross["turn_id"])
        require(cross["model_provider"] == "b" and cross["delegation"]["parent"]["home"] == str(root / "a"),
                "Cross-provider routing lost its destination or parent")

        # Exercise both opaque native cursors and partial-message continuations through MCP.
        messages, cursor = {}, None
        for _ in range(50):
            page = await call("read_messages", {"backend": "a", "thread_id": parent["thread_id"],
                "turn_id": parent["turn_id"], "limit": 1, "text_limit": 256, "cursor": cursor}, parent["thread_id"])
            for message in page["messages"]:
                previous = messages.get(message["id"], "")
                require(len(previous) == message["text_offset"], "Pagination skipped or repeated text")
                messages[message["id"]] = previous + message["text"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        require(cursor is None and messages == {m["id"]: m["text"] for m in parent_state["turns"][0]["messages"]},
                "Message pagination did not preserve the complete native turn")
    return {"native_mcp_sessions": True, "nested_forks": True, "cross_provider": True,
            "active_steering": True, "selection_overrides": True, "message_pagination": True,
            "probe_cleanup": True, "model_response_source": "loopback fixture", "external_inference": False}


def validate(root, command):
    with native_fixture(root) as (registry, http):
        provider_setup(registry, http, command)
        result = asyncio.run(exercise(registry, http, command))
        result["provider_onboarding"] = True
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--command", type=Path, required=True)
    args = parser.parse_args()
    validate(args.root.resolve(), args.command.absolute())
