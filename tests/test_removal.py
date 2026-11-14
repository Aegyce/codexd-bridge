"""Removal boundaries, retry behavior and preservation of other daemons."""

import errno
import io
import json
import os
import socket
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from codexd_bridge.cli import main
from codexd_bridge.config import load_backends
from codexd_bridge.installation import registration_index, remember_registration
from codexd_bridge.providers import _launcher_text
from codexd_bridge.provisioning import _toml_dumps, attach_daemon
from codexd_bridge.removal import remove_daemon
from codexd_bridge.transport import BridgeError


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "a"
        self.other = self.root / "b"
        self.registry = self.root / "bridge.toml"
        self.executable = self.root / "codexd-bridge"
        self.executable.write_text("#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o755)
        for home in (self.home, self.other):
            home.mkdir()
            (home / "session.json").write_text("session data")
            (home / "config.toml").write_text('model_provider="demo"\n[mcp_servers.codexd-bridge]\n'
                f'command={json.dumps(str(self.executable))}\n'
                f'args={json.dumps(["--config", str(self.registry), "serve"])}\nenabled=true\n'
                '[mcp_servers.other]\ncommand="/another-tool"\nenabled=true\n')
        self.registry.write_text(_toml_dumps({"delegation": {"max_depth": 4}, "backends": {
            "demo": {"home": str(self.home), "model_provider": "demo"},
            "other": {"home": str(self.other), "model_provider": "demo"}}}))
        self.launcher_dir = self.root / "custom"
        self.launcher_dir.mkdir()
        self.launcher = self.launcher_dir / "codex-demo"
        self.launcher.write_text(_launcher_text(self.executable, self.registry, "demo"))
        remember_registration(self.home, self.executable, self.registry, launcher=self.launcher)
        self.stop = patch("codexd_bridge.removal.daemon_action")
        self.action = self.stop.start()
        self.addCleanup(self.stop.stop)

    def remove(self, **kwargs):
        return remove_daemon("demo", registry_path=self.registry, executable=self.executable, **kwargs)

    def test_default_removes_only_selected_route_and_owned_launcher(self):
        other_files = {path.name: path.read_bytes() for path in self.other.iterdir()}
        history = self.root / "delegations.json"
        history.write_text("retained ancestry")
        result = self.remove()
        self.assertTrue(result["removed"])
        self.assertFalse(result["data_deleted"])
        self.assertTrue(self.executable.exists())
        self.assertTrue((self.home / "session.json").exists())
        self.assertFalse(self.launcher.exists())
        config = tomllib.loads((self.home / "config.toml").read_text())
        self.assertFalse(config["mcp_servers"]["codexd-bridge"]["enabled"])
        self.assertTrue(config["mcp_servers"]["other"]["enabled"])
        self.assertEqual(list(load_backends(self.registry)), ["other"])
        self.assertEqual(tomllib.loads(self.registry.read_text())["delegation"]["max_depth"], 4)
        self.assertEqual({path.name: path.read_bytes() for path in self.other.iterdir()}, other_files)
        self.assertEqual(history.read_text(), "retained ancestry")
        self.action.assert_not_called()
        self.assertEqual(json.loads(registration_index(self.executable, self.registry).read_text()), [])

    def test_retained_home_can_be_attached_again(self):
        self.remove()
        alias = self.root / "home-alias"
        alias.symlink_to(self.home, target_is_directory=True)
        backend = attach_daemon("demo", alias, registry_path=self.registry)
        self.assertFalse(backend.enabled)
        with self.assertRaises(BridgeError):
            self.remove(purge=True)
        self.assertTrue(self.remove()["removed"])
        self.assertTrue(alias.is_symlink())
        self.assertEqual((self.home / "session.json").read_text(), "session data")

    def test_removal_disables_owned_legacy_entry_and_keeps_other_registry(self):
        config = self.home / "config.toml"
        original = config.read_text()
        for name, registry in (("codex-qwen", self.registry),
                               ("codex-bridge", self.root / "unrelated.toml")):
            args = ["-m", "codexd_bridge.compat", "--config", str(registry),
                    "--backend", "demo", "--cwd", str(self.root)]
            original += f'\n[mcp_servers.{name}]\ncommand={json.dumps(str(self.executable.parent / "python"))}\n'
            original += f'args={json.dumps(args)}\nenabled=true\n'
        config.write_text(original)
        self.remove()
        entries = tomllib.loads(config.read_text())["mcp_servers"]
        self.assertFalse(entries["codex-qwen"]["enabled"])
        self.assertTrue(entries["codex-bridge"]["enabled"])

    def test_cli_purge_deletes_the_named_daemon_without_prompting(self):
        output = io.StringIO()
        with redirect_stdout(output), patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
            main(["--config", str(self.registry), "daemon", "remove", "demo", "--purge",
                  "--executable", str(self.executable)])
        self.assertTrue(json.loads(output.getvalue())["data_deleted"])
        self.assertFalse(self.home.exists())
        self.assertTrue(self.other.exists())

    def test_purge_deletes_only_target_home(self):
        external = self.root / "unrelated.txt"
        external.write_text("keep")
        (self.home / "link").symlink_to(external)
        result = self.remove(purge=True)
        self.assertTrue(result["data_deleted"])
        self.assertFalse(self.home.exists())
        self.assertTrue(self.other.exists())
        self.assertTrue(self.executable.exists())
        self.assertEqual(external.read_text(), "keep")

    def test_last_route_removal_leaves_a_usable_empty_registry(self):
        data = tomllib.loads(self.registry.read_text())
        del data["backends"]["other"]
        self.registry.write_text(_toml_dumps(data))
        self.remove()
        self.assertEqual(load_backends(self.registry), {})
        attach_daemon("again", self.home, registry_path=self.registry)
        self.assertIn("again", load_backends(self.registry))

    def test_missing_home_can_be_unregistered(self):
        import shutil
        shutil.rmtree(self.home)
        self.assertTrue(self.remove()["removed"])

    def test_stale_socket_is_treated_as_stopped(self):
        directory = self.home / "app-server-control"
        directory.mkdir()
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(directory / "app-server-control.sock"))
        self.remove()
        self.action.assert_not_called()

    def test_live_daemon_stops_before_registration_is_removed(self):
        def stop(backend, action, **kwargs):
            self.assertEqual((backend.name, action), ("demo", "stop"))
            self.assertFalse(load_backends(self.registry)["demo"].enabled)
            self.assertTrue(self.launcher.exists())
        self.action.side_effect = stop
        with patch("codexd_bridge.removal._running", side_effect=[True, False]), patch(
            "codexd_bridge.removal.AppServer", return_value=MagicMock()):
            self.remove()
        self.action.assert_called_once()

    def test_stop_failure_retains_disabled_route_and_data_for_retry(self):
        self.action.side_effect = BridgeError("stop failed")
        with patch("codexd_bridge.removal._running", return_value=True), patch(
            "codexd_bridge.removal.AppServer", return_value=MagicMock()):
            with self.assertRaisesRegex(BridgeError, "route remains disabled"):
                self.remove(purge=True)
        self.assertFalse(load_backends(self.registry)["demo"].enabled)
        self.assertTrue(self.home.exists())
        self.assertTrue(self.launcher.exists())
        self.action.side_effect = None
        self.assertTrue(self.remove()["removed"])

    def test_stop_success_without_shutdown_is_not_removal_success(self):
        with patch("codexd_bridge.removal._running", return_value=True), patch(
            "codexd_bridge.removal.AppServer", return_value=MagicMock()), patch(
            "codexd_bridge.removal.time.monotonic", side_effect=[0, 100]), patch(
            "codexd_bridge.removal.detach_home", return_value=None):
            with self.assertRaisesRegex(BridgeError, "still accepts"):
                self.remove(timeout=1)
        self.assertIn("demo", load_backends(self.registry))

    def test_partial_data_deletion_failure_keeps_a_retryable_disabled_route(self):
        with patch("codexd_bridge.removal.shutil.rmtree", side_effect=OSError("permission denied")):
            with self.assertRaisesRegex(BridgeError, "route remains disabled"):
                self.remove(purge=True)
        self.assertFalse(load_backends(self.registry)["demo"].enabled)
        self.assertTrue(self.home.exists())
        self.assertTrue(self.remove(purge=True)["data_deleted"])

    def test_purge_retries_a_late_file_from_a_stopped_background_job(self):
        rmdir = os.rmdir
        writes = []
        def late_write(path, **kwargs):
            if Path(path) == self.home and not writes:
                (self.home / "late-plugin-file").write_text("background output")
                writes.append(path)
            return rmdir(path, **kwargs)
        with patch("codexd_bridge.removal.shutil.os.rmdir", side_effect=late_write):
            self.assertTrue(self.remove(purge=True)["data_deleted"])
        self.assertEqual(len(writes), 1)
        self.assertFalse(self.home.exists())
        self.assertEqual((self.other / "session.json").read_text(), "session data")

    def test_purge_retry_keeps_a_replacement_directory(self):
        old = self.root / "original"
        def replace(_path):
            self.home.rename(old)
            self.home.mkdir()
            (self.home / "keep").write_text("replacement data")
            raise OSError(errno.ENOTEMPTY, "late write")
        with patch("codexd_bridge.removal.shutil.rmtree", side_effect=replace), \
                patch("codexd_bridge.removal.time.sleep"):
            with self.assertRaisesRegex(BridgeError, "directory changed"):
                self.remove(purge=True)
        self.assertEqual((self.home / "keep").read_text(), "replacement data")
        self.assertTrue((old / "session.json").exists())
        self.assertFalse(load_backends(self.registry)["demo"].enabled)

    def test_purge_retry_stops_at_deadline(self):
        with patch("codexd_bridge.removal.shutil.rmtree", side_effect=OSError(errno.ENOTEMPTY, "late write")) as remove, \
                patch("codexd_bridge.removal.time.monotonic", side_effect=[0, 2]), \
                patch("codexd_bridge.removal.detach_home", return_value=None):
            with self.assertRaisesRegex(BridgeError, "route remains disabled"):
                self.remove(purge=True, timeout=1)
        remove.assert_called_once_with(self.home)
        self.assertTrue((self.home / "session.json").exists())

    def test_modified_and_foreign_launchers_are_retained(self):
        self.launcher.write_text("a replacement command")
        result = self.remove()
        self.assertEqual(self.launcher.read_text(), "a replacement command")
        self.assertIn(str(self.launcher), result["kept_files"])

    def test_same_executable_for_another_registry_is_not_disabled(self):
        config = self.home / "config.toml"
        before = config.read_text().replace(str(self.registry), str(self.root / "different.toml"))
        config.write_text(before)
        self.remove()
        self.assertEqual(config.read_text(), before)

    def test_shared_home_and_nested_other_home_prevent_destructive_removal(self):
        before = tomllib.loads(self.registry.read_text())
        for path, purge in ((self.home, False), (self.home / "nested", True), (self.root, True)):
            data = json.loads(json.dumps(before))
            data["backends"]["other"]["home"] = str(path)
            self.registry.write_text(_toml_dumps(data))
            with self.assertRaises(BridgeError):
                self.remove(purge=purge)
            self.assertTrue(self.launcher.exists())
        self.action.assert_not_called()

    def test_changed_route_after_ui_selection_is_rejected(self):
        expected = load_backends(self.registry)["demo"]
        self.registry.write_text(self.registry.read_text().replace('model_provider = "demo"', 'model_provider = "changed"'))
        with self.assertRaisesRegex(BridgeError, "changed after selection"):
            self.remove(expected_backend=expected)
        self.action.assert_not_called()

    def test_default_codex_home_cannot_be_purged(self):
        with patch("codexd_bridge.removal.Path.home", return_value=self.home):
            with self.assertRaisesRegex(BridgeError, "Refusing to delete"):
                self.remove(purge=True)
        self.action.assert_not_called()

    def test_same_home_in_another_registry_is_protected(self):
        remember_registration(self.home, self.executable, self.registry, launcher=self.launcher)
        index = registration_index(self.executable, self.registry)
        records = json.loads(index.read_text())
        records.append({"home": str(self.home), "registry": str(self.root / "other.toml")})
        index.write_text(json.dumps(records))
        with self.assertRaisesRegex(BridgeError, "another registry"):
            self.remove()
        self.action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
