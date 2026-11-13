"""Exercise caller binding, persistent ancestry and concurrent call reservations."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codexd_bridge.config import Backend, ConfigError, load_backends
from codexd_bridge.delegation import DelegationGuard, SessionRef, request_meta
from codexd_bridge.provisioning import create_daemon, set_backend_enabled
from codexd_bridge.server import with_caller_context
from codexd_bridge.sessions import Bridge
from codexd_bridge.transport import BridgeError


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = self.root / "bridge.toml"
        self.config.write_text(
            f'[backends.demo]\nhome={json.dumps(str(self.home))}\nmodel_provider="demo"\n'
        )
        self.client = Mock()
        self.client.__enter__ = Mock(return_value=self.client)
        self.client.__exit__ = Mock(return_value=False)
        self.client.call.return_value = {"thread": {"id": "parent", "modelProvider": "demo"}}
        self.guard = DelegationGuard(self.config, connector=Mock(return_value=self.client))
        self.parent = SessionRef(self.home, "parent")
        self.child = SessionRef(self.home, "child")
        self.leaf = SessionRef(self.home, "leaf")
        env = patch.dict(os.environ, {"CODEXD_BRIDGE_CALLER_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        token = request_meta.set({"threadId": "parent", "sessionId": "parent"})
        self.addCleanup(request_meta.reset, token)

    def test_native_metadata_matches_daemon_provider_and_thread(self):
        self.assertEqual(self.guard.caller(), self.parent)
        self.client.call.assert_called_once_with(
            "thread/read", {"threadId": "parent", "includeTurns": False}
        )

    def test_missing_conflicting_or_malformed_metadata_is_rejected(self):
        for meta in (None, {}, {"threadId": ""}, {"threadId": "a", "x-codex-turn-metadata": {"thread_id": "b"}},
                     {"threadId": "parent", "x-codex-turn-metadata": "invalid"},
                     {"sessionId": "parent", "x-codex-turn-metadata": {"session_id": "b"}},
                     {"threadId": 12}, {"threadId": "x\n"}):
            with self.subTest(meta=meta):
                request_meta.set(meta)
                with self.assertRaises(BridgeError):
                    self.guard.caller()
        self.client.call.assert_not_called()

    def test_native_child_and_fork_import_transitive_ancestry(self):
        request_meta.set({"threadId": "leaf"})
        lineage = {
            "leaf": {"parentThreadId": "child"},
            "child": {"forkedFromId": "parent"},
            "parent": {},
        }
        self.client.call.side_effect = lambda method, params: {"thread": {
            "id": params["threadId"], "modelProvider": "demo", **lineage[params["threadId"]]}}
        caller = self.guard.caller()
        with self.assertRaisesRegex(BridgeError, "upstream"):
            self.guard.record(caller, self.parent)

    def test_unbound_home_and_foreign_thread_are_rejected(self):
        with patch.dict(os.environ, {"CODEXD_BRIDGE_CALLER_HOME": ""}):
            with self.assertRaises(BridgeError):
                self.guard.caller()
        for thread in ({"id": "other", "modelProvider": "demo"},
                       {"id": "parent", "modelProvider": "other"}):
            self.client.call.return_value = {"thread": thread}
            with self.assertRaises(BridgeError):
                self.guard.caller()

    def test_self_upstream_and_transitive_cycles_are_rejected_after_reload(self):
        self.guard.record(self.parent, self.child)
        self.guard.record(self.child, self.leaf)
        reloaded = DelegationGuard(self.config)
        for caller, target in ((self.child, self.parent), (self.leaf, self.parent),
                               (self.parent, self.parent)):
            with self.assertRaisesRegex(BridgeError, "upstream"):
                reloaded.record(caller, target)
        reloaded.record(self.parent, self.child)
        self.assertEqual(self.guard.path.stat().st_mode & 0o777, 0o600)

    def test_backend_home_aliases_cannot_bypass_ancestry(self):
        alias = self.root / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        self.guard.record(self.parent, self.child)
        with self.assertRaisesRegex(BridgeError, "upstream"):
            self.guard.record(SessionRef(alias, "child"), SessionRef(alias, "parent"))

    def test_same_thread_ids_on_different_daemons_remain_distinct(self):
        other = SessionRef(self.root / "other", "parent")
        self.guard.record(self.parent, other)
        with self.assertRaisesRegex(BridgeError, "upstream"):
            self.guard.record(other, self.parent)

    def test_multiple_parents_and_forks_preserve_both_ancestries(self):
        other = SessionRef(self.home, "other")
        fork = SessionRef(self.home, "fork")
        self.guard.record(self.parent, self.child)
        self.guard.record(other, fork, fork_from=self.child)
        for ancestor in (other, self.child, self.parent):
            with self.assertRaisesRegex(BridgeError, "upstream"):
                self.guard.record(fork, ancestor)

    def test_fork_rechecks_ancestry_after_concurrent_change(self):
        self.guard.check(self.child, self.parent)
        self.guard.record(self.parent, self.child)
        with self.assertRaisesRegex(BridgeError, "upstream"):
            self.guard.record(self.child, self.leaf, fork_from=self.parent)

    def test_depth_limit_blocks_new_chain_and_reattaching_deep_subtree(self):
        self.config.write_text('[delegation]\nmax_depth=2\n' + self.config.read_text())
        self.guard.record(self.parent, self.child)
        self.guard.record(self.child, self.leaf)
        with self.assertRaisesRegex(BridgeError, "depth"):
            self.guard.check(self.leaf)
        with self.assertRaisesRegex(BridgeError, "depth"):
            self.guard.record(SessionRef(self.home, "earlier"), self.parent)
        self.guard.record(self.parent, self.leaf)

    def test_simultaneous_opposite_calls_allow_exactly_one_direction(self):
        barrier = Barrier(2)

        def reserve(caller, target):
            barrier.wait(timeout=5)
            try:
                DelegationGuard(self.config).record(caller, target)
                return True
            except BridgeError:
                return False

        with ThreadPoolExecutor(max_workers=2) as workers:
            a = workers.submit(reserve, self.parent, self.child)
            b = workers.submit(reserve, self.child, self.parent)
            self.assertEqual(sorted([a.result(), b.result()]), [False, True])

    def test_existing_deep_tree_does_not_block_an_unrelated_task_or_followup(self):
        self.guard.record(self.parent, self.child)
        self.guard.record(self.child, self.leaf)
        self.config.write_text('[delegation]\nmax_depth=1\n' + self.config.read_text())
        self.guard.record(self.parent, self.child)
        self.guard.record(None, self.leaf)
        self.guard.record(SessionRef(self.home, "other"), SessionRef(self.home, "new"))
        with self.assertRaisesRegex(BridgeError, "depth"):
            self.guard.record(self.leaf, SessionRef(self.home, "too-deep"))

    def test_state_read_limit_counts_utf8_bytes(self):
        raw = json.dumps({"version": 2, "links": {}, "note": "中文" * 40}, ensure_ascii=False)
        self.guard.path.write_text(raw)
        with patch("codexd_bridge.delegation.MAX_STATE_BYTES", len(raw)):
            with self.assertRaisesRegex(BridgeError, "size limit"):
                self.guard.describe([])

    def test_created_id_collision_never_deletes_a_known_session(self):
        bridge = Bridge(self.config, guard=self.guard)
        alias = self.root / "home-alias"
        alias.symlink_to(self.home, target_is_directory=True)
        # An unrecorded caller must remain protected when its route uses a directory alias.
        with self.assertRaisesRegex(BridgeError, "existing"):
            bridge._record_created(self.client, self.parent, Backend("demo", alias, "demo"),
                                   {"thread": {"id": "parent"}}, assignment={"task": "new"})
        self.client.call.assert_not_called()
        self.guard.record(self.parent, self.child)
        for thread_id in ("parent", "child"):
            with self.subTest(thread_id=thread_id), self.assertRaisesRegex(BridgeError, "existing"):
                bridge._record_created(self.client, self.parent,
                                       Backend("demo", self.home, "demo"),
                                       {"thread": {"id": thread_id}}, assignment={"task": "new"})
        self.client.call.assert_not_called()

    def test_corrupt_state_and_symlinks_fail_closed(self):
        for raw in ('broken', '{}', '{"version":1,"links":[]}',
                    json.dumps({"version": 1, "links": {self.parent.key: [self.parent.key]}}),
                    json.dumps({"version": 1, "links": {'["relative","id"]': []}})):
            self.guard.path.write_text(raw)
            with self.assertRaises(BridgeError):
                self.guard.record(self.parent, self.child)
        self.guard.path.unlink()
        self.guard.path.symlink_to(self.root / "absent")
        with self.assertRaises(BridgeError):
            self.guard.record(self.parent, self.child)
        self.guard.path.unlink()
        os.mkfifo(self.guard.path)
        result = subprocess.run([sys.executable, "-c",
            "from pathlib import Path; import sys; from codexd_bridge.delegation import DelegationGuard; "
            "DelegationGuard(Path(sys.argv[1])).check(None)", str(self.config)],
            text=True, capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regular file", result.stderr)

    def test_failed_state_write_preserves_previous_graph(self):
        self.guard.record(self.parent, self.child)
        before = self.guard.path.read_bytes()
        with patch("codexd_bridge.delegation.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(BridgeError):
                self.guard.record(self.child, self.leaf)
        self.assertEqual(before, self.guard.path.read_bytes())
        self.assertEqual(list(self.root.glob(".delegations-*")), [])

    def test_callback_is_denied_before_native_resume_or_steer(self):
        self.guard.record(self.parent, self.child)
        request_meta.set({"threadId": "child"})

        def read(method, params):
            if method == "thread/read":
                return {"thread": {"id": params["threadId"], "modelProvider": "demo",
                                   "model": "model", "reasoningEffort": "low",
                                   "status": {"type": "idle"}}}
            if method == "model/list":
                return {"data": [{"id": "model", "model": "model",
                                  "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}]}
            raise AssertionError(f"Unexpected daemon mutation: {method}")

        self.client.call.side_effect = read
        bridge = Bridge(self.config, connector=self.guard.connector, guard=self.guard)
        with self.assertRaisesRegex(BridgeError, "upstream"):
            bridge.send_message("demo", "parent", "Do work")
        with self.assertRaisesRegex(BridgeError, "upstream"):
            bridge.steer_session("demo", "parent", "turn", "Do work")
        with self.assertRaisesRegex(BridgeError, "upstream"):
            bridge.fork_session("demo", "parent", "Do work", "Fork")
        self.assertTrue(all(call.args[0] in ("thread/read", "model/list")
                            for call in self.client.call.call_args_list))

    def test_unstarted_child_is_deleted_when_recording_fails(self):
        bridge = Bridge(self.config, guard=self.guard)
        with patch.object(self.guard, "record", side_effect=BridgeError("disk full")):
            with self.assertRaisesRegex(BridgeError, "disk full"):
                bridge._record_created(self.client, self.parent,
                                       Backend("demo", self.home, "demo"),
                                       {"thread": {"id": "unstarted"}})
        self.client.call.assert_called_once_with("thread/delete", {"threadId": "unstarted"})

    def test_context_adapter_restores_context_after_failure(self):
        def handler(value: str):
            self.assertEqual(request_meta.get(), {"threadId": "injected"})
            raise ValueError(value)

        wrapped = with_caller_context(handler)
        context = SimpleNamespace(request_context=SimpleNamespace(meta={"threadId": "injected"}))
        before = request_meta.get()
        with self.assertRaises(ValueError):
            wrapped("failure", ctx=context)
        self.assertEqual(request_meta.get(), before)

    def test_registry_updates_preserve_delegation_policy(self):
        self.config.write_text('[delegation]\nmax_depth=3\n' + self.config.read_text())
        set_backend_enabled("demo", False, registry_path=self.config)
        create_daemon("other", self.root / "other", "openai", "model", registry_path=self.config)
        self.assertEqual(DelegationGuard(self.config)._max_depth(), 3)

    def test_invalid_policy_is_rejected_during_configuration_loading(self):
        original = self.config.read_text()
        for value in ('true', '0', '65', '"8"'):
            self.config.write_text('[delegation]\nmax_depth=' + value + '\n' + original)
            with self.assertRaises(ConfigError):
                load_backends(self.config)


if __name__ == "__main__":
    unittest.main()
