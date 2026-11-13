"""Integration helpers are exercised with mock clients and temporary directories only."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codexd_bridge import integration
from codexd_bridge.integration import IntegrationError, install_skill, register_bridge
from codexd_bridge.transport import BridgeError

SESSION_WRITE_TOOLS = ("create_session", "send_message", "fork_session", "steer_session")


def managed_for(executable: Path, registry: Path) -> dict:
    return {
        "command": str(executable),
        "args": ["--config", str(registry), "serve"],
        "enabled": True,
        "startup_timeout_sec": 20,
        "tool_timeout_sec": 60,
    }


class FakeClient:
    """Records native config calls; never opens a socket or touches a daemon."""

    def __init__(self, config=None, *, write_status="ok", read_error=None, write_error=None):
        self.config = {} if config is None else config
        self.write_status = write_status
        self.read_error = read_error
        self.write_error = write_error
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "config/read":
            if self.read_error is not None:
                raise self.read_error
            return {"config": self.config, "origins": {}}
        if method == "config/value/write":
            if self.write_error is not None:
                raise self.write_error
            return {"filePath": "/daemon/config.toml", "status": self.write_status, "version": "7"}
        if method == "config/mcpServer/reload":
            return {}
        raise AssertionError(f"unexpected method: {method}")

    def methods(self):
        return [method for method, _ in self.calls]

    def params_for(self, method):
        return [params for name, params in self.calls if name == method]


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "daemon-home"
        self.home.mkdir()
        self.executable = root / "bin" / "codexd-bridge"
        self.registry = root / "bridge.toml"

    def register(self, config=None, *, client=None, **kwargs):
        if client is None:
            client = FakeClient(config)
        factory = Mock(return_value=client)
        result = register_bridge(
            self.home, self.executable, self.registry, client_factory=factory, **kwargs
        )
        return result, client, factory

    def test_integration_error_is_catchable_as_bridge_error(self):
        self.assertTrue(issubclass(IntegrationError, BridgeError))

    def test_fresh_registration_writes_only_the_owned_entry(self):
        config = {
            "mcp_servers": {"other": {"command": "/bin/other"}},
            "model_providers": {"p": {"env_key": "SECRET-SENTINEL"}},
        }
        result, client, factory = self.register(config)
        self.assertEqual(
            client.methods(), ["config/read", "config/value/write", "config/mcpServer/reload"]
        )
        self.assertEqual(client.params_for("config/read"), [{"includeLayers": False}])
        writes = client.params_for("config/value/write")
        self.assertEqual(len(writes), 1)
        self.assertEqual(
            writes[0],
            {
                "keyPath": "mcp_servers.codexd-bridge",
                "value": managed_for(self.executable, self.registry),
                "mergeStrategy": "replace",
            },
        )
        self.assertEqual(client.params_for("config/mcpServer/reload"), [None])
        self.assertTrue(result["changed"])
        self.assertEqual(result["write_status"], "ok")
        self.assertEqual(result["version"], "7")
        self.assertTrue(result["effective"])
        self.assertIsNone(result["warning"])
        self.assertTrue(result["reloaded"])
        self.assertFalse(result["allow_session_writes"])
        factory.assert_called_once_with(self.home)
        self.assertTrue(client.closed)
        self.assertNotIn("SECRET-SENTINEL", repr(result))

    def test_default_flag_preserves_existing_permission_fields(self):
        existing = {
            "command": "/old/bin",
            "args": ["--config", "/old.toml"],
            "enabled": False,
            "startup_timeout_sec": 5,
            "tool_timeout_sec": 5,
            "tools": {
                "create_session": {"approval_mode": "prompt", "enabled": True},
                "read_session": {"approval_mode": "auto"},
            },
            "default_tools_approval_mode": "prompt",
            "approvals_reviewer": "user",
        }
        result, client, _ = self.register({"mcp_servers": {"codexd-bridge": existing}})
        expected = {**existing, **managed_for(self.executable, self.registry)}
        self.assertEqual(client.params_for("config/value/write")[0]["value"], expected)
        self.assertTrue(result["changed"])

    def test_allow_session_writes_approves_only_the_four_tools(self):
        existing = {
            **managed_for(self.executable, self.registry),
            "tools": {
                "create_session": {"approval_mode": "prompt", "enabled": True},
                "send_message": {"approval_mode": "prompt"},
                "fork_session": {},
                "steer_session": {"approval_mode": "prompt", "enabled": False},
                "read_session": {"approval_mode": "auto"},
            },
            "default_tools_approval_mode": "prompt",
        }
        result, client, _ = self.register(
            {"mcp_servers": {"codexd-bridge": existing}}, allow_session_writes=True
        )
        value = client.params_for("config/value/write")[0]["value"]
        expected_tools = {
            "create_session": {"approval_mode": "approve", "enabled": True},
            "send_message": {"approval_mode": "approve"},
            "fork_session": {"approval_mode": "approve"},
            "steer_session": {"approval_mode": "approve", "enabled": False},
            "read_session": {"approval_mode": "auto"},
        }
        self.assertEqual(value["tools"], expected_tools)
        self.assertEqual(value["default_tools_approval_mode"], "prompt")
        self.assertTrue(result["allow_session_writes"])

    def test_allow_session_writes_creates_missing_tools_table(self):
        existing = managed_for(self.executable, self.registry)
        _, client, _ = self.register(
            {"mcp_servers": {"codexd-bridge": existing}}, allow_session_writes=True
        )
        value = client.params_for("config/value/write")[0]["value"]
        self.assertEqual(
            value["tools"],
            {name: {"approval_mode": "approve"} for name in SESSION_WRITE_TOOLS},
        )

    def test_allow_session_writes_is_idempotent(self):
        existing = {
            **managed_for(self.executable, self.registry),
            "tools": {name: {"approval_mode": "approve"} for name in SESSION_WRITE_TOOLS},
        }
        result, client, _ = self.register(
            {"mcp_servers": {"codexd-bridge": existing}}, allow_session_writes=True
        )
        self.assertFalse(result["changed"])
        self.assertEqual(client.methods(), ["config/read", "config/mcpServer/reload"])

    def test_identical_entry_skips_write_but_reloads(self):
        existing = managed_for(self.executable, self.registry)
        result, client, _ = self.register({"mcp_servers": {"codexd-bridge": existing}})
        self.assertFalse(result["changed"])
        self.assertIsNone(result["write_status"])
        self.assertIsNone(result["version"])
        self.assertTrue(result["effective"])
        self.assertEqual(client.methods(), ["config/read", "config/mcpServer/reload"])

    def test_url_based_entry_is_refused(self):
        client = FakeClient(
            {"mcp_servers": {"codexd-bridge": {"url": "https://example.invalid/mcp"}}}
        )
        with self.assertRaisesRegex(IntegrationError, "URL-based"):
            self.register(client=client)
        self.assertEqual(client.methods(), ["config/read"])

    def test_read_failure_stops_before_write(self):
        client = FakeClient(read_error=BridgeError("config/read: outcome unknown"))
        with self.assertRaises(BridgeError):
            self.register(client=client)
        self.assertEqual(client.methods(), ["config/read"])

    def test_write_failure_skips_reload(self):
        client = FakeClient(write_error=BridgeError("config/value/write: outcome unknown"))
        with self.assertRaises(BridgeError):
            self.register(client=client)
        self.assertEqual(client.methods(), ["config/read", "config/value/write"])

    def test_unexpected_read_shapes_are_refused(self):
        for config in (None, {"mcp_servers": []}, {"mcp_servers": {"codexd-bridge": "table?"}}):
            with self.subTest(config=config):
                client = FakeClient()
                client.config = config
                with self.assertRaises(IntegrationError):
                    self.register(client=client)
                self.assertEqual(client.methods(), ["config/read"])

    def test_relative_paths_are_rejected_before_connecting(self):
        factory = Mock()
        for home, executable, registry in (
            (Path("relative-home"), self.executable, self.registry),
            (self.home, Path("codexd-bridge"), self.registry),
            (self.home, self.executable, Path("bridge.toml")),
        ):
            with self.subTest(home=home, executable=executable, registry=registry):
                with self.assertRaisesRegex(IntegrationError, "absolute"):
                    register_bridge(home, executable, registry, client_factory=factory)
        factory.assert_not_called()

    def test_ok_overridden_reports_not_effective_with_warning(self):
        client = FakeClient(write_status="okOverridden")
        result, _, _ = self.register(client=client)
        self.assertTrue(result["changed"])
        self.assertEqual(result["write_status"], "okOverridden")
        self.assertFalse(result["effective"])
        self.assertIsInstance(result["warning"], str)
        self.assertIn("overrides", result["warning"])
        self.assertTrue(result["reloaded"])
        self.assertEqual(client.methods()[-1], "config/mcpServer/reload")

    def test_unexpected_write_status_requires_inspection(self):
        client = FakeClient(write_status="mystery")
        with self.assertRaisesRegex(IntegrationError, "inspect"):
            self.register(client=client)
        self.assertEqual(client.methods(), ["config/read", "config/value/write"])

    def test_secrets_are_never_echoed_in_results_or_errors(self):
        config = {
            "mcp_servers": {"codexd-bridge": {"env": {"TOKEN": "SECRET-SENTINEL"}}},
            "model_providers": {"p": {"env_key": "SECRET-SENTINEL"}},
        }
        result, client, _ = self.register(config)
        self.assertNotIn("SECRET-SENTINEL", repr(result))
        written = client.params_for("config/value/write")[0]["value"]
        self.assertEqual(written["env"], {"TOKEN": "SECRET-SENTINEL"})
        failing = FakeClient(config, write_error=BridgeError("config/value/write: outcome unknown"))
        with self.assertRaises(BridgeError) as raised:
            self.register(client=failing)
        self.assertNotIn("SECRET-SENTINEL", str(raised.exception))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "codex-home"
        self.source = self.root / "skill-src"
        (self.source / "references").mkdir(parents=True)
        (self.source / "SKILL.md").write_text("# test skill\n", encoding="utf-8")
        (self.source / "references" / "notes.md").write_text("notes\n", encoding="utf-8")

    def test_install_copies_tree_and_leaves_other_skills_alone(self):
        other = self.home / "skills" / "other-skill"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("other\n", encoding="utf-8")
        target = install_skill(self.home, source=self.source)
        self.assertEqual(target, self.home / "skills" / "codexd-bridge")
        self.assertEqual((target / "SKILL.md").read_text(), "# test skill\n")
        self.assertEqual((target / "references" / "notes.md").read_text(), "notes\n")
        self.assertEqual((other / "SKILL.md").read_text(), "other\n")
        self.assertEqual((self.source / "SKILL.md").read_text(), "# test skill\n")

    def test_reinstall_identical_is_idempotent(self):
        first = install_skill(self.home, source=self.source)
        second = install_skill(self.home, source=self.source)
        self.assertEqual(first, second)
        self.assertEqual(
            sorted(p.name for p in (self.home / "skills").iterdir()), ["codexd-bridge"]
        )

    def test_differing_content_is_refused(self):
        target = install_skill(self.home, source=self.source)
        (target / "SKILL.md").write_text("locally edited\n", encoding="utf-8")
        with self.assertRaisesRegex(IntegrationError, "differing"):
            install_skill(self.home, source=self.source)
        self.assertEqual((target / "SKILL.md").read_text(), "locally edited\n")

    def test_extra_target_file_is_refused(self):
        target = install_skill(self.home, source=self.source)
        (target / "extra.txt").write_text("extra\n", encoding="utf-8")
        with self.assertRaises(IntegrationError):
            install_skill(self.home, source=self.source)

    def test_target_file_is_refused(self):
        occupant = self.home / "skills" / "codexd-bridge"
        occupant.parent.mkdir(parents=True)
        occupant.write_text("not a directory\n", encoding="utf-8")
        with self.assertRaisesRegex(IntegrationError, "non-directory"):
            install_skill(self.home, source=self.source)
        self.assertEqual(occupant.read_text(), "not a directory\n")

    def test_target_symlink_is_refused(self):
        skills = self.home / "skills"
        skills.mkdir(parents=True)
        link = skills / "codexd-bridge"
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(IntegrationError, "non-directory"):
            install_skill(self.home, source=self.source)
        self.assertTrue(link.is_symlink())

    def test_target_internal_symlink_is_refused(self):
        target = install_skill(self.home, source=self.source)
        notes = target / "references" / "notes.md"
        notes.unlink()
        notes.symlink_to(self.source / "references" / "notes.md")
        with self.assertRaises(IntegrationError):
            install_skill(self.home, source=self.source)

    def test_missing_or_invalid_source_is_refused(self):
        empty = self.root / "empty"
        empty.mkdir()
        file_source = self.root / "file.md"
        file_source.write_text("x\n", encoding="utf-8")
        for source in (self.root / "missing", empty, file_source):
            with self.subTest(source=source):
                with self.assertRaisesRegex(IntegrationError, "SKILL.md"):
                    install_skill(self.home, source=source)
        self.assertFalse((self.home / "skills").exists())

    def test_source_with_internal_symlink_is_refused(self):
        (self.source / "references" / "link.md").symlink_to(self.source / "SKILL.md")
        with self.assertRaisesRegex(IntegrationError, "symlinks"):
            install_skill(self.home, source=self.source)

    def test_default_source_prefers_packaged_share(self):
        prefix = self.root / "prefix"
        packaged = prefix / "share" / "codexd-bridge" / "skills" / "codexd-bridge"
        packaged.mkdir(parents=True)
        (packaged / "SKILL.md").write_text("packaged\n", encoding="utf-8")
        with patch.object(sys, "prefix", str(prefix)):
            target = install_skill(self.home)
        self.assertEqual((target / "SKILL.md").read_text(), "packaged\n")

    def test_default_source_falls_back_to_repository(self):
        with patch.object(sys, "prefix", str(self.root / "empty-prefix")):
            target = install_skill(self.home)
        repo_skill = (
            Path(integration.__file__).resolve().parents[2]
            / "skills"
            / "codexd-bridge"
            / "SKILL.md"
        )
        self.assertEqual((target / "SKILL.md").read_bytes(), repo_skill.read_bytes())

    def test_relative_home_is_refused(self):
        with self.assertRaisesRegex(IntegrationError, "absolute"):
            install_skill(Path("relative-home"), source=self.source)

    def test_failed_copy_leaves_no_staging_or_target(self):
        with patch("codexd_bridge.integration.shutil.copytree", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                install_skill(self.home, source=self.source)
        self.assertEqual(list((self.home / "skills").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
