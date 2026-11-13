"""Exercise repeated derivation through Bridge with real persistent delegation state."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexd_bridge.delegation import DelegationGuard, OperatorGuard, SessionRef, request_meta
from codexd_bridge.sessions import Bridge
from codexd_bridge.transport import BridgeError


class TreeServer:
    def __init__(self):
        self.threads = {"root": self.thread("root")}
        self.calls = []
        self.inputs = {}
        self.fail_turn = False

    @staticmethod
    def thread(thread_id, **extra):
        return {"id": thread_id, "sessionId": "root", "modelProvider": "demo",
                "model": "model", "reasoningEffort": "low", "status": {"type": "idle"},
                **extra}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            return {"thread": self.threads[params["threadId"]]}
        if method == "model/list":
            return {"data": [{"id": "model", "model": "model", "supportedReasoningEfforts": [
                {"reasoningEffort": "low"}]}]}
        if method in ("thread/start", "thread/fork", "thread/resume"):
            if method == "thread/resume":
                thread = self.threads[params["threadId"]]
            else:
                thread_id = f"child-{len(self.threads)}"
                thread = self.thread(thread_id)
                if method == "thread/fork":
                    thread["forkedFromId"] = params["threadId"]
                else:
                    thread["sessionId"] = thread_id
                self.threads[thread_id] = thread
            return {"thread": thread, "modelProvider": "demo", "model": "model",
                    "reasoningEffort": "low"}
        if method == "turn/start":
            if self.fail_turn:
                raise BridgeError("outcome unknown")
            self.inputs[params["threadId"]] = params["input"]
            return {"turn": {"id": "turn", "status": "inProgress"}}
        if method == "thread/list":
            return {"data": list(self.threads.values())}
        if method == "thread/turns/list":
            return {"data": []}
        if method in ("thread/name/set", "turn/steer"):
            return {}
        if method == "thread/delete":
            del self.threads[params["threadId"]]
            return {}
        raise AssertionError(method)


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / "bridge.toml"
        self.config.write_text(f'[backends.demo]\nhome={json.dumps(str(self.home))}\n'
                               'model_provider="demo"\ndefault_model="model"\n')
        self.server = TreeServer()
        self.bridge = Bridge(self.config, connector=lambda _home: self.server)
        env = patch.dict(os.environ, {"CODEXD_BRIDGE_CALLER_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        token = request_meta.set({"threadId": "root", "sessionId": "root"})
        self.addCleanup(request_meta.reset, token)

    def caller(self, thread_id):
        request_meta.set({"threadId": thread_id,
                          "sessionId": self.server.threads[thread_id]["sessionId"]})

    def fork(self, source, task="Inspect parser", **kwargs):
        return self.bridge.fork_session("demo", source, task, "Branch", **kwargs)

    def test_repeated_self_forks_keep_tree_identity_and_fresh_assignments(self):
        child = self.fork("root", session_type="reviewer", acceptance_criteria="Report defects")
        self.caller(child["thread_id"])
        leaf = self.fork(child["thread_id"], "Check parser tests", session_type="tester",
                         acceptance_criteria="Run the parser tests and report failures")
        record = leaf["delegation"]
        self.assertEqual(record["parent"]["thread_id"], child["thread_id"])
        self.assertEqual(record["fork_source"], record["parent"])
        self.assertEqual(record["root_sessions"], [SessionRef(self.home, "root").as_dict()])
        self.assertEqual(record["depth"], 2)
        self.assertEqual(record["session_type"], "tester")
        self.assertEqual(record["parent_delegation_id"], child["delegation"]["delegation_id"])
        self.assertNotEqual(record["delegation_id"], child["delegation"]["delegation_id"])
        text = self.server.inputs[leaf["thread_id"]][0]["text"]
        self.assertIn("Check parser tests", text)
        self.assertIn("Run the parser tests and report failures", text)
        self.assertNotIn("Inspect parser", text)
        self.assertIn("Earlier bridge assignments", text)
        reloaded = Bridge(self.config, connector=lambda _home: self.server)
        self.assertEqual(reloaded.read_session("demo", leaf["thread_id"])["session"]["delegation"],
                         record)
        listed = reloaded.list_sessions("demo")["sessions"]
        self.assertEqual(listed[-1]["delegation"]["delegation_id"], record["delegation_id"])
        self.assertNotIn("task_requirements", listed[-1]["delegation"])
        self.caller(leaf["thread_id"])
        for target in (leaf["thread_id"], child["thread_id"], "root"):
            with self.assertRaisesRegex(BridgeError, "upstream"):
                self.bridge.send_message("demo", target, "Do work")
        with self.assertRaisesRegex(BridgeError, "upstream"):
            self.fork("root")

    def test_create_then_fork_and_continue_preserves_original_assignment(self):
        child = self.bridge.create_session("demo", "Implement parser", "Task", str(self.home),
                                           acceptance_criteria="Parser tests pass")
        self.assertEqual(child["delegation"]["origin"], "create")
        self.caller(child["thread_id"])
        leaf = self.fork(child["thread_id"])
        self.assertEqual(leaf["delegation"]["depth"], 2)
        self.assertEqual(leaf["delegation"]["acceptance_criteria"], "Not specified by the caller.")
        self.caller("root")
        self.bridge.send_message("demo", leaf["thread_id"], "Also check empty input")
        record = self.bridge.read_session("demo", leaf["thread_id"])["session"]["delegation"]
        self.assertEqual(record, leaf["delegation"])

    def test_fork_of_a_child_distinguishes_parent_from_requesting_session(self):
        child = self.fork("root")
        leaf = self.fork(child["thread_id"], "Inspect the child's result")
        record = leaf["delegation"]
        self.assertEqual(record["parent"]["thread_id"], child["thread_id"])
        self.assertEqual(record["requested_by"]["thread_id"], "root")
        self.assertEqual(record["parent_delegation_id"], child["delegation"]["delegation_id"])
        self.assertEqual(record["depth"], 2)

    def test_malformed_fork_response_cannot_delete_or_start_the_source(self):
        original = self.server.call

        def reuse_source(method, params):
            if method == "thread/fork":
                return {"thread": self.server.threads["root"], "modelProvider": "demo",
                        "model": "model", "reasoningEffort": "low"}
            return original(method, params)

        with patch.object(self.server, "call", side_effect=reuse_source):
            with self.assertRaisesRegex(BridgeError, "source session"):
                self.fork("root")
        self.assertIn("root", self.server.threads)
        self.assertFalse(self.server.inputs)

    def test_depth_limit_is_enforced_before_creating_another_child(self):
        self.config.write_text('[delegation]\nmax_depth=2\n' + self.config.read_text())
        child = self.fork("root")
        self.caller(child["thread_id"])
        leaf = self.fork(child["thread_id"])
        self.caller(leaf["thread_id"])
        before = len(self.server.threads)
        with self.assertRaisesRegex(BridgeError, "depth"):
            self.fork(leaf["thread_id"])
        self.assertEqual(len(self.server.threads), before)

    def test_native_source_ancestry_is_checked_even_for_an_unrelated_caller(self):
        self.config.write_text('[delegation]\nmax_depth=1\n' + self.config.read_text())
        self.server.threads["native"] = self.server.thread("native", forkedFromId="root")
        with self.assertRaisesRegex(BridgeError, "depth"):
            self.fork("native")
        self.assertFalse(any(method == "thread/fork" for method, _ in self.server.calls))

    def test_human_fork_is_persisted_for_later_agent_delegation(self):
        operator = Bridge(self.config, connector=lambda _home: self.server,
                          guard=OperatorGuard(self.config))
        child = operator.fork_session("demo", "root", "Review", "Human fork")
        self.assertIsNone(child["delegation"]["requested_by"])
        self.assertEqual(child["delegation"]["parent"]["thread_id"], "root")
        self.caller(child["thread_id"])
        leaf = self.fork(child["thread_id"])
        self.assertEqual(leaf["delegation"]["depth"], 2)

    def test_ambiguous_turn_failure_keeps_assignment_and_identified_session(self):
        self.server.fail_turn = True
        with self.assertRaisesRegex(BridgeError, "child-1.*Inspect this session"):
            self.fork("root")
        record = self.bridge.read_session("demo", "child-1")["session"]["delegation"]
        self.assertEqual(record["task_requirements"], "Inspect parser")
        self.assertIn("child-1", self.server.threads)

    def test_mismatched_tree_and_conflicting_thread_metadata_are_rejected(self):
        request_meta.set({"threadId": "root", "sessionId": "wrong-tree"})
        with self.assertRaisesRegex(BridgeError, "session tree"):
            self.fork("root")
        request_meta.set({"threadId": "root", "x-codex-turn-metadata": {"thread_id": "other"}})
        with self.assertRaisesRegex(BridgeError, "conflicting"):
            self.fork("root")
        self.assertFalse(any(method == "thread/fork" for method, _ in self.server.calls))

    def test_invalid_assignment_fields_are_rejected_before_any_daemon_call(self):
        for options in ({"session_type": ""}, {"session_type": "review\nworker"},
                        {"acceptance_criteria": " "}, {"acceptance_criteria": "x" * 4097}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.fork("root", **options)
        self.assertEqual(self.server.calls, [])

    def test_v1_state_upgrade_retains_edges_and_assignments_across_native_import(self):
        guard = self.bridge.guard
        parent, child = SessionRef(self.home, "root"), SessionRef(self.home, "native")
        guard.path.write_text(json.dumps({"version": 1, "links": {parent.key: [child.key]}}))
        self.server.threads["native"] = self.server.thread("native", parentThreadId="root")
        self.caller("native")
        leaf = self.fork("native")
        self.caller(leaf["thread_id"])
        self.bridge.guard.caller()
        state = json.loads(guard.path.read_text())
        self.assertEqual(state["version"], 2)
        self.assertIn(child.key, state["links"][parent.key])
        self.assertEqual(DelegationGuard(self.config).describe([
            SessionRef(self.home, leaf["thread_id"])])[0], leaf["delegation"])
