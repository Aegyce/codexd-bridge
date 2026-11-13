"""Installation selects the launching Codex home without disturbing other routes."""

import io
import json
import os
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from codexd_bridge.cli import main
from codexd_bridge.config import load_backends
from codexd_bridge.integration import configure_home_provider, install_bridge
from codexd_bridge.transport import BridgeError


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.home_config = 'model_provider="demo"\nmodel="model"\n'
        (self.home / "config.toml").write_text(self.home_config)
        self.registry = self.root / "registry.toml"
        self.executable = self.root / "codexd-bridge"
        self.executable.write_text("#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o755)
        running = patch("codexd_bridge.providers._ensure_running", return_value={"running": True})
        self.running = running.start()
        self.addCleanup(running.stop)
        connect = patch("codexd_bridge.integration.connect_backend", return_value={"enabled": True})
        self.connect = connect.start()
        self.addCleanup(connect.stop)

    def test_install_attaches_home_and_reuses_route_on_reinstall(self):
        for _ in range(2):
            install_bridge(self.home, self.executable, self.registry)
        routes = load_backends(self.registry)
        self.assertEqual(list(routes), ["codex"])
        self.assertEqual(routes["codex"].home, self.home)
        self.assertEqual(routes["codex"].model_provider, "demo")
        self.assertEqual(self.connect.call_count, 2)
        self.assertEqual((self.home / "config.toml").read_text(), self.home_config)
        self.connect.assert_called_with(routes["codex"], self.executable, self.registry,
                                        update_skill=False, allow_session_writes=False, progress=None)

    def test_install_cli_uses_codex_home_and_forwards_upgrade_options(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home)}), redirect_stdout(io.StringIO()):
            main(["--config", str(self.registry), "install", "--executable", str(self.executable),
                  "--name", "work", "--update-skill"])
        route = load_backends(self.registry)["work"]
        self.connect.assert_called_once_with(route, self.executable, self.registry,
                                             update_skill=True, allow_session_writes=False, progress=None)

    def test_existing_home_route_does_not_get_duplicated_or_rebound(self):
        self.registry.write_text(f'[backends.existing]\nhome={json.dumps(str(self.home))}\n'
                                  'model_provider="demo"\n')
        before = self.registry.read_bytes()
        install_bridge(self.home, self.executable, self.registry, name="ignored")
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.connect.call_args.args[0].name, "existing")

    def test_invalid_executable_does_not_attach_or_start_a_daemon(self):
        with self.assertRaisesRegex(BridgeError, "executable"):
            install_bridge(self.home, self.root / "missing", self.registry)
        self.assertFalse(self.registry.exists())
        self.running.assert_not_called()
        self.connect.assert_not_called()

    def test_explicit_provider_is_required_before_attaching(self):
        (self.home / "config.toml").write_text('model="model"\n')
        with self.assertRaisesRegex(ValueError, "model_provider"):
            install_bridge(self.home, self.executable, self.registry)
        self.running.assert_not_called()

    def test_explicit_selection_adds_only_provider_and_preserves_tables_and_comments(self):
        original = '# keep this comment\nmodel="model"\n[features]\nmulti_agent=true\n'
        (self.home / "config.toml").write_text(original)
        stages = []
        install_bridge(self.home, self.executable, self.registry, provider="openai",
                       progress=stages.append)
        updated = (self.home / "config.toml").read_text()
        self.assertEqual(updated, 'model_provider = "openai"\n' + original)
        self.assertEqual(tomllib.loads(updated)["model_provider"], "openai")
        self.assertEqual(stages, ["configuration", "daemon"])
        configure_home_provider(self.home, "openai")
        self.assertEqual((self.home / "config.toml").read_text(), updated)

    def test_setup_can_explicitly_configure_a_home_without_config_file(self):
        (self.home / "config.toml").unlink()
        install_bridge(self.home, self.executable, self.registry, provider="openai")
        self.assertEqual(load_backends(self.registry)["codex"].model_provider, "openai")

    def test_explicit_selection_cannot_replace_an_existing_provider(self):
        with self.assertRaisesRegex(BridgeError, "differs"):
            install_bridge(self.home, self.executable, self.registry, provider="openai")
        self.assertEqual((self.home / "config.toml").read_text(), self.home_config)
        self.assertFalse(self.registry.exists())
        self.running.assert_not_called()

    def test_symlink_config_and_invalid_setup_are_rejected_without_changing_home(self):
        config = self.home / "config.toml"
        config.unlink()
        with self.assertRaises(ValueError):
            install_bridge(self.home, self.executable, self.registry, name="INVALID", provider="openai")
        self.assertFalse(config.exists())
        original = self.root / "original.toml"
        original.write_text('model="model"\n')
        config.symlink_to(original)
        with self.assertRaisesRegex(BridgeError, "regular file"):
            configure_home_provider(self.home, "openai")
        self.assertEqual(original.read_text(), 'model="model"\n')
