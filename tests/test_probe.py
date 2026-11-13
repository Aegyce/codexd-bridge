"""Offline tests for probe.check_model: fake daemon, fake clock, no real inference."""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from codexd_bridge.config import Backend
from codexd_bridge.probe import (
    PROBE_MARKER,
    PROBE_PROMPT,
    PROBE_THREAD_NAME,
    ProbeError,
    check_model,
)
from codexd_bridge.transport import BridgeError


def marker_turn(
    status="completed", text=PROBE_MARKER, error=None, turn_id="probe-turn", items=None
):
    if items is None:
        items = [] if text is None else [{"type": "agentMessage", "text": text}]
    turn = {"id": turn_id, "status": status, "items": items}
    if error is not None:
        turn["error"] = error
    return turn


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeProbeServer:
    """One fake daemon: records every call and scripts turn outcomes."""

    def __init__(self, home, provider="qwen"):
        self.home = home
        self.provider = provider
        self.catalog = [
            {
                "id": "custom-model",
                "model": "custom-model",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": effort} for effort in ("low", "high", "max")
                ],
            },
            {
                "id": "model-b",
                "model": "model-b",
                "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
            },
        ]
        self.resolved_model = "custom-model"
        self.resolved_effort = "low"
        self.ignore_requested = False
        self.calls = []
        self.created = 0
        self.deleted = []
        self.interrupted = []
        self.closed = False
        self.fail_start = None
        self.start_without_id = False
        self.wrong_provider = False
        self.fail_turn_start = None
        self.fail_delete = None
        self.fail_interrupt = None
        self.fail_name = False
        self.turn_pages = []
        self.extra_turns = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def methods(self):
        return [method for method, _ in self.calls]

    def params(self, method):
        return next(params for name, params in self.calls if name == method)

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "model/list":
            return {"data": self.catalog}
        if method == "thread/start":
            if self.fail_start is not None:
                raise self.fail_start
            self.created += 1
            if not self.ignore_requested:
                self.resolved_model = params.get("model", self.resolved_model)
                self.resolved_effort = params.get("config", {}).get(
                    "model_reasoning_effort", self.resolved_effort
                )
            thread = {
                "id": "probe-thread",
                "modelProvider": "other-provider" if self.wrong_provider else self.provider,
            }
            if self.start_without_id:
                thread.pop("id")
            return {
                "thread": thread,
                "modelProvider": self.provider,
                "model": self.resolved_model,
                "reasoningEffort": self.resolved_effort,
            }
        if method == "thread/name/set":
            if self.fail_name:
                raise BridgeError("thread/name/set: unsupported")
            return {}
        if method == "turn/start":
            if self.fail_turn_start is not None:
                raise self.fail_turn_start
            return {"turn": {"id": "probe-turn", "status": "inProgress"}}
        if method == "thread/turns/list":
            # Historical summaries may report interruption before a live turn ends.
            return {"data": [marker_turn(status="interrupted", text=None)]}
        if method == "thread/read":
            if len(self.turn_pages) > 1:
                page = self.turn_pages.pop(0)
            elif self.turn_pages:
                page = self.turn_pages[0]
            else:
                page = marker_turn()
            return {"thread": {"turns": self.extra_turns + [page]}}
        if method == "turn/interrupt":
            self.interrupted.append(params)
            if self.fail_interrupt is not None:
                raise self.fail_interrupt
            return {}
        if method == "thread/delete":
            self.deleted.append(params)
            if self.fail_delete is not None:
                raise self.fail_delete
            return {}
        raise AssertionError(f"unexpected method {method}")


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        # Disabled on purpose: verification must bypass the enabled gate.
        self.backend = Backend(
            "qwen",
            self.home,
            "qwen",
            default_model="custom-model",
            default_effort="low",
            enabled=False,
        )
        self.server = FakeProbeServer(self.home)
        self.homes = []
        self.clock = FakeClock()
        for name, value in (("_clock", self.clock.clock), ("_sleep", self.clock.sleep)):
            route = patch(f"codexd_bridge.probe.{name}", value)
            route.start()
            self.addCleanup(route.stop)

    def factory(self, home):
        self.homes.append(home)
        return self.server

    def check(self, **kwargs):
        kwargs.setdefault("timeout", 5)
        return check_model(self.backend, client_factory=self.factory, **kwargs)

    def test_probe_error_is_bridge_error(self):
        self.assertTrue(issubclass(ProbeError, BridgeError))

    def test_success_binds_provider_defaults_and_cleans_up(self):
        result = self.check()
        self.assertTrue(result["available"])
        self.assertEqual(result["backend"], "qwen")
        self.assertEqual(result["provider"], "qwen")
        self.assertEqual(result["model"], "custom-model")
        self.assertEqual(result["effort"], "low")
        self.assertEqual(result["thread_id"], "probe-thread")
        self.assertEqual(result["turn_id"], "probe-turn")
        self.assertTrue(result["deleted"])
        checked = datetime.fromisoformat(result["checked_at"])
        self.assertEqual(checked.tzinfo, UTC)
        self.assertEqual(self.homes, [self.home])
        start = self.server.params("thread/start")
        self.assertEqual(start["modelProvider"], "qwen")
        self.assertEqual(
            start["config"], {"model_provider": "qwen", "model_reasoning_effort": "low"}
        )
        self.assertEqual(start["approvalPolicy"], "never")
        self.assertEqual(start["sandbox"], "read-only")
        self.assertEqual(start["model"], "custom-model")
        self.assertEqual(start["cwd"], str(self.home))
        self.assertEqual(
            self.server.params("turn/start"),
            {
                "threadId": "probe-thread",
                "input": [{"type": "text", "text": PROBE_PROMPT}],
                "model": "custom-model",
                "effort": "low",
            },
        )
        self.assertEqual(
            self.server.params("thread/read"),
            {"threadId": "probe-thread", "includeTurns": True},
        )
        naming = self.server.params("thread/name/set")
        self.assertEqual(naming["threadId"], "probe-thread")
        self.assertEqual(naming["name"], PROBE_THREAD_NAME)
        self.assertIn("temporary", naming["name"])
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])
        self.assertEqual(self.server.interrupted, [])
        self.assertEqual(self.server.created, 1)
        self.assertEqual(self.server.methods().count("thread/start"), 1)
        self.assertEqual(self.server.methods().count("turn/start"), 1)
        self.assertTrue(self.server.closed)

    def test_explicit_overrides_and_prompt_forbids_tools(self):
        result = self.check(model="custom-model", effort="max")
        self.assertEqual(result["effort"], "max")
        self.assertEqual(self.server.params("turn/start")["effort"], "max")
        self.assertIn(PROBE_MARKER, PROBE_PROMPT)
        self.assertIn("Do not call any tools", PROBE_PROMPT)
        self.assertIn("files", PROBE_PROMPT)

    def test_live_completion_takes_precedence_over_stale_history(self):
        self.server.turn_pages = [marker_turn(status="inProgress", text=None), marker_turn()]
        result = self.check()
        self.assertTrue(result["available"])
        self.assertNotIn("thread/turns/list", self.server.methods())
        self.assertEqual(self.server.interrupted, [])
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])

    def test_absent_model_or_unsupported_effort_blocks_before_creation(self):
        for kwargs in ({"model": "missing"}, {"model": "custom-model", "effort": "ultra"}):
            self.server.calls.clear()
            with self.assertRaises(ProbeError):
                self.check(**kwargs)
            self.assertEqual(self.server.methods(), ["model/list"])
            self.assertEqual(self.server.created, 0)
            self.assertEqual(self.server.deleted, [])

    def test_wrong_provider_keeps_id_and_deletes_without_turn(self):
        self.server.wrong_provider = True
        with self.assertRaisesRegex(ProbeError, "probe-thread"):
            self.check()
        self.assertNotIn("turn/start", self.server.methods())
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])

    def test_requested_selection_must_be_applied_by_daemon(self):
        self.server.ignore_requested = True
        with self.assertRaisesRegex(ProbeError, "probe-thread"):
            self.check(model="model-b")
        self.assertNotIn("turn/start", self.server.methods())
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])

    def test_provider_blocked_turn_keeps_id_and_deletes(self):
        self.server.fail_turn_start = BridgeError("turn/start: provider refused the request")
        with self.assertRaisesRegex(ProbeError, "probe-thread"):
            self.check()
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])
        self.assertEqual(self.server.interrupted, [])

    def test_failed_turn_fails_and_deletes_without_interrupt(self):
        self.server.turn_pages = [
            marker_turn(status="failed", text=None, error={"message": "model exploded"})
        ]
        with self.assertRaisesRegex(ProbeError, "probe-thread.*failed"):
            self.check()
        self.assertEqual(self.server.methods().count("turn/start"), 1)
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])
        self.assertEqual(self.server.interrupted, [])

    def test_error_on_running_turn_interrupts_then_deletes(self):
        self.server.turn_pages = [
            marker_turn(status="inProgress", text=None, error={"message": "stream broke"})
        ]
        with self.assertRaisesRegex(ProbeError, "probe-thread"):
            self.check()
        self.assertEqual(
            self.server.interrupted, [{"threadId": "probe-thread", "turnId": "probe-turn"}]
        )
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])
        methods = self.server.methods()
        self.assertLess(methods.index("turn/interrupt"), methods.index("thread/delete"))

    def test_refusal_empty_or_echo_reply_fails(self):
        cases = [
            marker_turn(text="I cannot comply with that request."),
            marker_turn(text="I refuse " + PROBE_MARKER),
            marker_turn(text=PROBE_MARKER + " but I will not comply"),
            marker_turn(text=None),
            marker_turn(items=[{"type": "userMessage", "text": PROBE_PROMPT}]),
            marker_turn(items=[{"type": "reasoning", "text": PROBE_MARKER}]),
        ]
        for page in cases:
            self.server.calls.clear()
            self.server.deleted.clear()
            self.server.turn_pages = [page]
            with self.assertRaisesRegex(ProbeError, "marker"):
                self.check()
            self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])

    def test_exact_marker_with_surrounding_whitespace_passes(self):
        self.server.turn_pages = [marker_turn(text="\n  " + PROBE_MARKER + "  \n")]
        result = self.check()
        self.assertTrue(result["available"])
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])

    def test_timeout_interrupts_then_deletes_with_mocked_clock(self):
        self.server.turn_pages = [marker_turn(status="inProgress", text=None)]
        with self.assertRaisesRegex(ProbeError, "timed out"):
            self.check(timeout=2)
        self.assertEqual(
            self.server.interrupted, [{"threadId": "probe-thread", "turnId": "probe-turn"}]
        )
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])
        methods = self.server.methods()
        self.assertLess(methods.index("turn/interrupt"), methods.index("thread/delete"))
        self.assertTrue(self.clock.sleeps)
        self.assertLessEqual(max(self.clock.sleeps), 0.5)
        self.assertAlmostEqual(sum(self.clock.sleeps), 2.0, delta=0.5)
        self.assertEqual(self.server.methods().count("turn/start"), 1)

    def test_delete_failure_after_success_is_not_reported_available(self):
        self.server.fail_delete = BridgeError("thread/delete: outcome unknown")
        with self.assertRaisesRegex(ProbeError, "probe-thread.*cleaned up"):
            self.check()
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])
        self.assertEqual(self.server.interrupted, [])

    def test_timeout_with_delete_failure_reports_both(self):
        self.server.turn_pages = [marker_turn(status="inProgress", text=None)]
        self.server.fail_delete = BridgeError("thread/delete: outcome unknown")
        with self.assertRaises(ProbeError) as caught:
            self.check(timeout=1)
        message = str(caught.exception)
        self.assertIn("timed out", message)
        self.assertIn("thread/delete failed", message)
        self.assertIn("probe-thread", message)
        self.assertEqual(
            self.server.interrupted, [{"threadId": "probe-thread", "turnId": "probe-turn"}]
        )

    def test_interrupt_failure_reporting_depends_on_delete(self):
        self.server.turn_pages = [marker_turn(status="inProgress", text=None)]
        self.server.fail_interrupt = BridgeError("turn/interrupt: outcome unknown")
        with self.assertRaisesRegex(ProbeError, "timed out"):
            self.check(timeout=1)
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])
        self.server.calls.clear()
        self.server.deleted.clear()
        self.server.interrupted.clear()
        self.server.fail_delete = BridgeError("thread/delete: outcome unknown")
        with self.assertRaises(ProbeError) as caught:
            self.check(timeout=1)
        message = str(caught.exception)
        self.assertIn("turn/interrupt failed", message)
        self.assertIn("thread/delete failed", message)

    def test_keyboard_interrupt_still_cleans_up(self):
        original = self.server.call

        def interrupted(method, params):
            if method == "thread/read":
                raise KeyboardInterrupt
            return original(method, params)

        with patch.object(self.server, "call", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.check()
        self.assertEqual(
            self.server.interrupted, [{"threadId": "probe-thread", "turnId": "probe-turn"}]
        )
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])
        self.server.calls.clear()
        self.server.deleted.clear()
        self.server.interrupted.clear()
        self.server.fail_delete = BridgeError("thread/delete: outcome unknown")
        with patch.object(self.server, "call", side_effect=interrupted):
            with self.assertRaises(ProbeError) as caught:
                self.check()
        message = str(caught.exception)
        self.assertIn("probe-thread", message)
        self.assertIn("thread/delete failed", message)
        self.assertIsInstance(caught.exception.__cause__, KeyboardInterrupt)

    def test_interrupt_during_cleanup_reports_the_owned_thread(self):
        self.server.fail_delete = KeyboardInterrupt()
        with self.assertRaisesRegex(ProbeError, "probe-thread.*cleaned up"):
            self.check()
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])

    def test_creation_timeout_without_id_is_unknown_and_never_deletes(self):
        self.server.fail_start = BridgeError(
            "thread/start: outcome unknown; inspect session state before retrying"
        )
        with self.assertRaises(ProbeError) as caught:
            self.check()
        message = str(caught.exception)
        self.assertIn("unknown", message)
        self.assertIn("list_sessions", message)
        self.assertIn("qwen", message)
        self.assertEqual(self.server.created, 0)
        self.assertEqual(self.server.deleted, [])
        self.assertEqual(self.server.interrupted, [])
        self.assertNotIn("thread/delete", self.server.methods())

    def test_creation_without_usable_id_is_unknown(self):
        self.server.start_without_id = True
        with self.assertRaisesRegex(ProbeError, "unknown"):
            self.check()
        self.assertEqual(self.server.deleted, [])
        self.assertNotIn("turn/start", self.server.methods())

    def test_effort_only_uses_daemon_resolved_model(self):
        bare = Backend("qwen", self.home, "qwen", enabled=False)
        result = check_model(bare, effort="high", client_factory=self.factory, timeout=5)
        start = self.server.params("thread/start")
        self.assertNotIn("model", start)
        self.assertEqual(start["config"]["model_reasoning_effort"], "high")
        self.assertEqual(result["model"], "custom-model")
        self.assertEqual(result["effort"], "high")
        self.assertEqual(self.server.params("turn/start")["model"], "custom-model")
        self.assertEqual(self.server.methods().count("model/list"), 1)

    def test_fully_default_selection_is_resolved_by_daemon(self):
        bare = Backend("qwen", self.home, "qwen", enabled=False)
        result = check_model(bare, client_factory=self.factory, timeout=5)
        start = self.server.params("thread/start")
        self.assertNotIn("model", start)
        self.assertNotIn("model_reasoning_effort", start["config"])
        self.assertEqual((result["model"], result["effort"]), ("custom-model", "low"))

    def test_unresolved_or_unsupported_daemon_selection_fails_with_cleanup(self):
        bare = Backend("qwen", self.home, "qwen", enabled=False)
        self.server.ignore_requested = True
        self.server.resolved_model = None
        with self.assertRaisesRegex(ProbeError, "probe-thread.*did not resolve a model"):
            check_model(bare, client_factory=self.factory, timeout=5)
        self.assertNotIn("turn/start", self.server.methods())
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])
        self.server.calls.clear()
        self.server.deleted.clear()
        self.server.resolved_model = "custom-model"
        self.server.resolved_effort = "ultra"
        with self.assertRaisesRegex(ProbeError, "probe-thread"):
            check_model(bare, client_factory=self.factory, timeout=5)
        self.assertNotIn("turn/start", self.server.methods())
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])

    def test_other_threads_turns_are_ignored(self):
        foreign = marker_turn(turn_id="foreign-turn")
        self.server.extra_turns = [foreign]
        self.server.turn_pages = [marker_turn(status="inProgress", text=None), marker_turn()]
        result = self.check()
        self.assertTrue(result["available"])
        self.assertEqual(self.server.methods().count("thread/read"), 2)
        self.server.calls.clear()
        self.server.deleted.clear()
        self.server.interrupted.clear()
        self.server.turn_pages = [foreign]
        with self.assertRaisesRegex(ProbeError, "timed out"):
            self.check(timeout=1)
        self.assertEqual(
            self.server.interrupted, [{"threadId": "probe-thread", "turnId": "probe-turn"}]
        )
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])

    def test_no_reasoning_or_tool_output_in_result(self):
        self.server.turn_pages = [
            marker_turn(
                items=[
                    {"type": "reasoning", "text": "private-reasoning"},
                    {"type": "commandExecution", "output": "private-tool-output"},
                    {"type": "agentMessage", "text": PROBE_MARKER},
                ]
            )
        ]
        result = self.check()
        self.assertNotIn("private", str(result))
        self.assertEqual(
            set(result),
            {
                "available",
                "backend",
                "provider",
                "model",
                "effort",
                "checked_at",
                "thread_id",
                "turn_id",
                "deleted",
            },
        )

    def test_invalid_input_does_not_connect(self):
        for timeout in (0, -1, float("nan"), float("inf"), True, "60"):
            with self.assertRaises(ValueError):
                self.check(timeout=timeout)
        with self.assertRaises(ValueError):
            self.check(model="   ")
        self.assertEqual(self.homes, [])

    def test_connection_failure_is_probe_error(self):
        def broken(home):
            raise BridgeError("Cannot connect to App Server; start it first")

        with self.assertRaisesRegex(ProbeError, "qwen"):
            check_model(self.backend, client_factory=broken)

    def test_name_failure_is_tolerated(self):
        self.server.fail_name = True
        result = self.check()
        self.assertTrue(result["available"])
        self.assertEqual(self.server.deleted, [{"threadId": "probe-thread"}])


if __name__ == "__main__":
    unittest.main()
