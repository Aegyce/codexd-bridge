import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexd_bridge.config import Backend
from codexd_bridge.sessions import Bridge
from codexd_bridge.transport import BridgeError


class FakeServer:
    def __init__(self, provider="qwen", model="custom-model", effort="low"):
        self.provider = provider
        self.model = model
        self.effort = effort
        self.status = "idle"
        self.calls = []
        self.fail_at = None
        self.active_on_resume = False
        self.wrong_created_provider = False
        self.turns = []
        self.items = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def thread(self):
        return {
            "id": "owned-thread",
            "modelProvider": self.provider,
            "model": self.model,
            "reasoningEffort": self.effort,
            "status": {"type": self.status},
        }

    def call(self, method, params):
        self.calls.append((method, params))
        if method == self.fail_at:
            raise BridgeError("outcome unknown")
        if method == "model/list":
            return {
                "data": [
                    {
                        "id": "custom-model",
                        "model": "custom-model",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": effort} for effort in ("low", "high", "max")
                        ],
                    }
                ]
            }
        if method == "thread/read":
            return {"thread": self.thread()}
        if method in ("thread/start", "thread/resume", "thread/fork"):
            self.model = params.get("model", self.model)
            self.effort = params.get("config", {}).get("model_reasoning_effort", self.effort)
            if self.active_on_resume and method == "thread/resume":
                self.status = "active"
            thread = self.thread()
            if self.wrong_created_provider:
                thread["modelProvider"] = "wrong-provider"
            return {
                "thread": thread,
                "modelProvider": self.provider,
                "model": self.model,
                "reasoningEffort": self.effort,
            }
        if method == "turn/start":
            return {"turn": {"id": "owned-turn", "status": "inProgress"}}
        if method == "thread/turns/list":
            return {"data": self.turns, "nextCursor": "older-turns"}
        if method == "thread/items/list":
            return {"data": [{"item": i} for i in self.items], "nextCursor": "older-items"}
        if method == "thread/list":
            return {"data": [self.thread()]}
        return {}


class SessionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.targets = {name: Backend(name, self.home / name, name) for name in ("qwen", "openai")}
        self.servers = {
            target.home: FakeServer(target.model_provider) for target in self.targets.values()
        }
        self.route = patch("codexd_bridge.sessions.load_backends", return_value=self.targets)
        self.route.start()
        self.addCleanup(self.route.stop)
        self.bridge = Bridge(connector=self.servers.__getitem__)
        self.server = self.servers[self.targets["qwen"].home]

    def create(self, **kwargs):
        return self.bridge.create_session(
            "qwen", "Inspect this module", "Inspection", str(self.home), **kwargs
        )

    def test_two_backends_are_routed_to_independent_servers(self):
        for backend in ("openai", "qwen"):
            result = self.bridge.create_session(
                backend, "Check", "Task", str(self.home), model="custom-model", effort="high"
            )
            self.assertEqual(result["model_provider"], backend)
            server = self.servers[self.targets[backend].home]
            params = next(p for m, p in server.calls if m == "thread/start")
            self.assertEqual(params["modelProvider"], backend)
            self.assertEqual(params["config"]["model_provider"], backend)

    def test_creation_uses_model_and_provider_specific_effort(self):
        result = self.create(model="custom-model", effort="max", sandbox="read-only")
        self.assertEqual((result["model"], result["effort"]), ("custom-model", "max"))
        params = next(p for m, p in self.server.calls if m == "thread/start")
        self.assertEqual(params["sandbox"], "read-only")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(self.server.calls[-1][1]["effort"], "max")

    def test_unverified_backend_can_be_inspected_but_cannot_receive_tasks(self):
        self.targets["qwen"] = Backend("qwen", self.home / "qwen", "qwen", enabled=False)
        with self.assertRaisesRegex(BridgeError, "disabled"):
            self.create()
        self.assertEqual(self.server.calls, [])
        catalog = self.bridge.list_models("qwen")
        self.assertFalse(catalog["enabled"])
        self.assertFalse(catalog["availability_checked"])

    def test_dynamic_model_discovery_preserves_other_providers_when_one_fails(self):
        self.server.fail_at = "model/list"
        result = self.bridge.list_provider_models()
        records = {entry["backend"]: entry for entry in result["providers"]}
        self.assertFalse(records["qwen"]["reachable"])
        self.assertTrue(records["openai"]["reachable"])
        self.assertEqual(records["openai"]["models"][0]["model"], "custom-model")
        self.assertFalse(records["openai"]["availability_checked"])

    def test_resume_and_fork_inherit_thread_not_backend_defaults(self):
        self.targets["qwen"] = Backend("qwen", self.home / "qwen", "qwen", "other", "high")
        self.bridge.send_message("qwen", "owned-thread", "Continue")
        self.bridge.fork_session("qwen", "owned-thread", "Continue independently", "Fork")
        for method, params in self.server.calls:
            if method in ("thread/resume", "thread/fork"):
                self.assertEqual(params["model"], "custom-model")
                self.assertEqual(params["config"]["model_reasoning_effort"], "low")
                self.assertNotIn("approvalPolicy", params)
                self.assertNotIn("sandbox", params)

    def test_explicit_resume_and_fork_effort_overrides(self):
        resumed = self.bridge.send_message("qwen", "owned-thread", "Continue", effort="high")
        forked = self.bridge.fork_session("qwen", "owned-thread", "Inspect", "Fork", effort="low")
        self.assertEqual(resumed["effort"], "high")
        self.assertEqual(forked["effort"], "low")

    def test_missing_inherited_selection_requires_explicit_choice(self):
        self.server.model = None
        with self.assertRaisesRegex(BridgeError, "model is unavailable"):
            self.bridge.send_message("qwen", "owned-thread", "Continue")
        self.assertEqual([m for m, _ in self.server.calls], ["thread/read"])
        result = self.bridge.send_message("qwen", "owned-thread", "Continue", model="custom-model")
        self.assertEqual(result["model"], "custom-model")

    def test_wrong_provider_is_never_mutated(self):
        self.server.provider = "openai"
        actions = (
            lambda: self.bridge.send_message("qwen", "foreign", "Continue"),
            lambda: self.bridge.fork_session("qwen", "foreign", "Inspect", "Fork"),
            lambda: self.bridge.steer_session("qwen", "foreign", "turn", "Continue"),
        )
        for action in actions:
            with self.assertRaises(BridgeError):
                action()
        self.assertEqual([m for m, _ in self.server.calls], ["thread/read"] * 3)

    def test_wrong_provider_creation_returns_id_without_starting_turn(self):
        self.server.wrong_created_provider = True
        with self.assertRaisesRegex(BridgeError, "owned-thread"):
            self.create()
        self.assertNotIn("turn/start", [m for m, _ in self.server.calls])

    def test_unknown_model_or_effort_rejected_before_creation(self):
        for kwargs in ({"model": "missing"}, {"model": "custom-model", "effort": "unsupported"}):
            self.server.calls.clear()
            with self.assertRaises(BridgeError):
                self.create(**kwargs)
            self.assertEqual([m for m, _ in self.server.calls], ["model/list"])

    def test_effort_only_is_checked_against_resolved_model_before_inference(self):
        with self.assertRaisesRegex(BridgeError, "owned-thread"):
            self.create(effort="unsupported")
        self.assertNotIn("turn/start", [m for m, _ in self.server.calls])

    def test_active_or_unknown_state_never_receives_followup(self):
        for status in ("active", "systemError", None):
            self.server.status = status
            self.server.calls.clear()
            with self.assertRaises(BridgeError):
                self.bridge.send_message("qwen", "owned-thread", "Continue")
            self.assertEqual([m for m, _ in self.server.calls], ["thread/read"])

    def test_becoming_active_during_resume_does_not_receive_input(self):
        self.server.active_on_resume = True
        with self.assertRaises(BridgeError):
            self.bridge.send_message("qwen", "owned-thread", "Continue")
        self.assertNotIn("turn/start", [m for m, _ in self.server.calls])

    def test_steer_carries_exact_turn_precondition(self):
        self.bridge.steer_session("qwen", "owned-thread", "exact-turn", "Finish this check")
        self.assertEqual(self.server.calls[-1][1]["expectedTurnId"], "exact-turn")

    def test_failure_after_creation_keeps_id_and_never_retries(self):
        for method in ("thread/name/set", "turn/start"):
            self.server.calls.clear()
            self.server.fail_at = method
            with self.assertRaisesRegex(BridgeError, "owned-thread.*Inspect this session"):
                self.create()
            self.assertEqual(sum(m == "thread/start" for m, _ in self.server.calls), 1)
            self.assertEqual(sum(m == method for m, _ in self.server.calls), 1)

    def test_malformed_turn_result_keeps_known_thread_id(self):
        original = self.server.call

        def malformed(method, params):
            return {"turn": {}} if method == "turn/start" else original(method, params)

        with patch.object(self.server, "call", side_effect=malformed):
            with self.assertRaisesRegex(BridgeError, "owned-thread"):
                self.create()

    def test_messages_prioritize_latest_and_do_not_expose_private_items(self):
        self.server.turns = [
            {"id": "one", "status": "inProgress"},
            {"id": "two", "status": "completed"},
        ]
        self.server.items = [
            {"type": "agentMessage", "text": "Latest result"},
            {"type": "reasoning", "content": ["private reasoning"]},
            {"type": "commandExecution", "output": "private command output"},
            {"type": "userMessage", "content": [{"type": "text", "text": "x" * 1000}]},
        ]
        result = self.bridge.read_session("qwen", "owned-thread", text_limit=256)
        self.assertEqual(result["turns"][0]["messages"][-1]["text"], "Latest result")
        self.assertTrue(result["text_limit_reached"])
        self.assertEqual(result["turns"][1]["messages"], [])
        self.assertEqual(sum(m == "thread/items/list" for m, _ in self.server.calls), 1)
        self.assertNotIn("private", str(result))

    def test_tool_only_page_falls_back_to_turn_summary(self):
        self.server.turns = [
            {
                "id": "one",
                "status": "inProgress",
                "items": [{"type": "agentMessage", "text": "Last visible update"}],
            }
        ]
        self.server.items = [{"type": "commandExecution"}]
        result = self.bridge.read_session("qwen", "owned-thread")
        self.assertEqual(result["turns"][0]["messages"][0]["text"], "Last visible update")

    def test_messages_support_native_cursor(self):
        self.bridge.read_messages("qwen", "owned-thread", "turn", cursor="older")
        self.assertEqual(self.server.calls[-1][1]["cursor"], "older")

    def test_invalid_input_does_not_connect(self):
        with patch.object(self.bridge, "connector", side_effect=AssertionError("must not connect")):
            for prompt in ("", " ", "中" * 6000):
                with self.assertRaises(ValueError):
                    self.bridge.send_message("qwen", "thread", prompt)
            with self.assertRaises(ValueError):
                self.create(sandbox="danger-full-access")
            with self.assertRaises(ValueError):
                self.bridge.read_session("qwen", "thread", turn_limit=100)


if __name__ == "__main__":
    unittest.main()
