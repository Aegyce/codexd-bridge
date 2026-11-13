"""Unit tests for codexd_bridge.provisioning.

Stdlib only; subprocess is mocked so no real daemon is ever started or stopped.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from codexd_bridge.config import Backend, ConfigError, load_backends
from codexd_bridge.provisioning import (
    ProvisioningError,
    create_daemon,
    daemon_action,
    set_backend_enabled,
)
from codexd_bridge.transport import BridgeError


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class ProvisioningTestCase(unittest.TestCase):
    """Temp workspace with HOME inside it; daemon commands are forbidden."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"HOME": str(self.root)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.registry = self.root / "bridge.toml"
        self.homes = self.root / "homes"
        self.homes.mkdir()
        run = patch("codexd_bridge.provisioning.subprocess.run")
        self.run = run.start()
        self.addCleanup(run.stop)
        self.run.side_effect = AssertionError("daemon commands must not run in unit tests")

    def home(self, name: str) -> Path:
        return self.homes / name

    def read_registry(self) -> dict:
        return tomllib.loads(self.registry.read_text(encoding="utf-8"))["backends"]

    def make_template(self, provider="qwen", *, catalog=None, extra="", fields=None) -> Path:
        template = self.root / "template"
        template.mkdir(exist_ok=True)
        if fields is None:
            fields = (
                'name = "Qwen"\n'
                'base_url = "https://dashscope.example.invalid/compatible-mode/v1"\n'
                'env_key = "QWEN_API_KEY"\n'
                'wire_api = "responses"\n'
                "requires_openai_auth = false\n"
            )
        text = ""
        if catalog is not None:
            text += f'model_catalog_json = "{catalog}"\n'
        text += f"[model_providers.{provider}]\n" + fields + extra
        (template / "config.toml").write_text(text, encoding="utf-8")
        (template / "auth.json").write_text(
            '{"OPENAI_API_KEY": "sk-TEMPLATE-SECRET"}', encoding="utf-8"
        )
        return template

    def create_qwen(self, name="qwen", **kwargs) -> Backend:
        if "template_home" not in kwargs:
            kwargs["template_home"] = self.make_template()
        params = dict(registry_path=self.registry, model_provider="qwen", model="qwen3-max")
        params.update(kwargs)
        return create_daemon(name, self.home(name), **params)

    def create_openai(self, name="gpt", **kwargs) -> Backend:
        params = dict(registry_path=self.registry, model_provider="openai", model="gpt-5")
        params.update(kwargs)
        return create_daemon(name, self.home(name), **params)


class CreateDaemonTests(ProvisioningTestCase):
    def test_openai_without_template_registers_disabled(self):
        backend = self.create_openai(effort="high")
        self.assertIsInstance(backend, Backend)
        self.assertEqual(backend.name, "gpt")
        self.assertEqual(backend.model_provider, "openai")
        self.assertEqual(backend.default_model, "gpt-5")
        self.assertEqual(backend.default_effort, "high")
        self.assertIs(backend.enabled, False)
        self.assertEqual(
            backend.socket_path, self.home("gpt") / "app-server-control/app-server-control.sock"
        )
        config = tomllib.loads((self.home("gpt") / "config.toml").read_text())
        self.assertEqual(
            config,
            {
                "model_provider": "openai",
                "model": "gpt-5",
                "model_reasoning_effort": "high",
                "approval_policy": "never",
                "sandbox_mode": "workspace-write",
            },
        )
        self.assertIs(self.read_registry()["gpt"]["enabled"], False)
        self.assertIs(load_backends(self.registry)["gpt"].enabled, False)
        self.run.assert_not_called()

    def test_effort_omitted(self):
        self.create_openai()
        config = tomllib.loads((self.home("gpt") / "config.toml").read_text())
        self.assertNotIn("model_reasoning_effort", config)
        self.assertNotIn("default_effort", self.read_registry()["gpt"])

    def test_permissions(self):
        self.create_openai()
        self.assertEqual(mode(self.home("gpt")), 0o700)
        self.assertEqual(mode(self.home("gpt") / "config.toml"), 0o600)
        self.assertEqual(mode(self.registry), 0o600)

    def test_new_home_contains_only_config(self):
        self.create_qwen()
        entries = sorted(p.name for p in self.home("qwen").iterdir())
        self.assertEqual(entries, ["config.toml"])
        self.assertFalse((self.home("qwen") / "config.toml").is_symlink())

    def test_existing_empty_dir_refused(self):
        self.home("gpt").mkdir()
        with self.assertRaises(ProvisioningError):
            self.create_openai()
        self.assertEqual(list(self.home("gpt").iterdir()), [])
        self.assertFalse(self.registry.exists())

    def test_existing_nonempty_dir_refused(self):
        self.home("gpt").mkdir()
        marker = self.home("gpt") / "keep.txt"
        marker.write_text("data")
        with self.assertRaises(ProvisioningError):
            self.create_openai()
        self.assertEqual(marker.read_text(), "data")
        self.assertFalse(self.registry.exists())

    def test_existing_file_refused(self):
        self.home("gpt").write_text("not a dir")
        with self.assertRaises(ProvisioningError):
            self.create_openai()
        self.assertEqual(self.home("gpt").read_text(), "not a dir")

    def test_dangling_symlink_refused(self):
        self.home("gpt").symlink_to(self.root / "missing-target")
        with self.assertRaises(ProvisioningError):
            self.create_openai()
        self.assertTrue(self.home("gpt").is_symlink())
        self.assertFalse(self.registry.exists())

    def test_symlink_to_dir_refused(self):
        target = self.root / "real"
        target.mkdir()
        self.home("gpt").symlink_to(target, target_is_directory=True)
        with self.assertRaises(ProvisioningError):
            self.create_openai()
        self.assertEqual(list(target.iterdir()), [])

    def test_relative_home_refused(self):
        with self.assertRaises(ProvisioningError):
            create_daemon(
                "gpt", Path("relative/home"), "openai", "gpt-5", registry_path=self.registry
            )
        self.assertFalse(self.registry.exists())

    def test_home_parent_missing_refused(self):
        with self.assertRaises(ProvisioningError):
            create_daemon(
                "gpt", self.root / "nope" / "gpt", "openai", "gpt-5", registry_path=self.registry
            )

    def test_invalid_name(self):
        for bad in ("", "Gpt", "1gpt", "-gpt", "g pt", "gpt!", "gpt.x"):
            with self.subTest(name=bad):
                with self.assertRaises(ProvisioningError):
                    create_daemon(
                        bad, self.home("gpt"), "openai", "gpt-5", registry_path=self.registry
                    )
        self.assertFalse(self.homes.exists() and any(self.homes.iterdir()))

    def test_invalid_text_fields(self):
        cases = [
            {"model_provider": ""},
            {"model_provider": "x" * 129},
            {"model": ""},
            {"model": "   "},
            {"model": "x" * 129},
            {"effort": ""},
            {"effort": "x" * 129},
        ]
        for override in cases:
            with self.subTest(override=override):
                params = dict(registry_path=self.registry, model_provider="openai", model="gpt-5")
                params.update(override)
                with self.assertRaises(ProvisioningError):
                    create_daemon("gpt", self.home("gpt"), **params)
        self.assertFalse(self.registry.exists())

    def test_non_builtin_requires_template(self):
        with self.assertRaises(ProvisioningError) as ctx:
            create_daemon(
                "qwen", self.home("qwen"), "qwen", "qwen3-max", registry_path=self.registry
            )
        self.assertIn("qwen", str(ctx.exception))
        self.assertFalse(self.home("qwen").exists())

    def test_non_builtin_template_missing_definition(self):
        template = self.make_template(provider="other")
        with self.assertRaises(ProvisioningError):
            create_daemon(
                "qwen",
                self.home("qwen"),
                "qwen",
                "qwen3-max",
                registry_path=self.registry,
                template_home=template,
            )
        self.assertFalse(self.home("qwen").exists())

    def test_template_missing_config(self):
        template = self.root / "empty-template"
        template.mkdir()
        with self.assertRaises(ProvisioningError):
            create_daemon(
                "qwen",
                self.home("qwen"),
                "qwen",
                "qwen3-max",
                registry_path=self.registry,
                template_home=template,
            )

    def test_template_vetted_fields_copied(self):
        fields = (
            'name = "Qwen"\n'
            'base_url = "https://dashscope.example.invalid/v1"\n'
            'env_key = "QWEN_API_KEY"\n'
            'wire_api = "chat"\n'
            "requires_openai_auth = false\n"
            "request_max_retries = 3\n"
            "stream_max_retries = 2\n"
            "stream_idle_timeout_ms = 60000\n"
            "[model_providers.qwen.env_http_headers]\n"
            'X-Custom = "CUSTOM_ENV"\n'
        )
        template = self.make_template(fields=fields)
        create_daemon(
            "qwen",
            self.home("qwen"),
            "qwen",
            "qwen3-max",
            registry_path=self.registry,
            template_home=template,
        )
        config = tomllib.loads((self.home("qwen") / "config.toml").read_text())
        self.assertEqual(config["model_provider"], "qwen")
        self.assertEqual(
            config["model_providers"]["qwen"],
            {
                "name": "Qwen",
                "base_url": "https://dashscope.example.invalid/v1",
                "env_key": "QWEN_API_KEY",
                "wire_api": "chat",
                "requires_openai_auth": False,
                "request_max_retries": 3,
                "stream_max_retries": 2,
                "stream_idle_timeout_ms": 60000,
                "env_http_headers": {"X-Custom": "CUSTOM_ENV"},
            },
        )

    def test_template_source_unchanged(self):
        template = self.make_template(catalog="catalogs/qwen.json")
        config_before = (template / "config.toml").read_bytes()
        auth_before = (template / "auth.json").read_bytes()
        self.create_qwen(template_home=template)
        self.assertEqual((template / "config.toml").read_bytes(), config_before)
        self.assertEqual((template / "auth.json").read_bytes(), auth_before)

    def test_no_secrets_copied(self):
        with patch.dict(os.environ, {"QWEN_API_KEY": "sk-ENV-SECRET-VALUE"}):
            self.create_qwen()
        config_text = (self.home("qwen") / "config.toml").read_text()
        registry_text = self.registry.read_text()
        for secret in ("sk-ENV-SECRET-VALUE", "sk-TEMPLATE-SECRET"):
            self.assertNotIn(secret, config_text)
            self.assertNotIn(secret, registry_text)
        self.assertIn("QWEN_API_KEY", config_text)

    def test_auth_json_never_opened(self):
        template = self.make_template()
        auth = template / "auth.json"
        auth.unlink()
        auth.mkdir()  # opening this as a file would raise IsADirectoryError
        create_daemon(
            "qwen",
            self.home("qwen"),
            "qwen",
            "qwen3-max",
            registry_path=self.registry,
            template_home=template,
        )
        self.assertTrue((self.home("qwen") / "config.toml").exists())

    def test_catalog_relative_resolved(self):
        template = self.make_template(catalog="catalogs/qwen.json")
        self.create_qwen(template_home=template)
        config = tomllib.loads((self.home("qwen") / "config.toml").read_text())
        self.assertEqual(config["model_catalog_json"], str(template / "catalogs/qwen.json"))

    def test_catalog_absolute_preserved(self):
        absolute = str(self.root / "shared" / "catalog.json")
        template = self.make_template(catalog=absolute)
        self.create_qwen(template_home=template)
        config = tomllib.loads((self.home("qwen") / "config.toml").read_text())
        self.assertEqual(config["model_catalog_json"], absolute)

    def test_catalog_file_not_copied(self):
        template = self.make_template(catalog="catalogs/qwen.json")
        catalog_file = template / "catalogs" / "qwen.json"
        catalog_file.parent.mkdir()
        catalog_file.write_text('{"models": []}')
        self.create_qwen(template_home=template)
        self.assertFalse((self.home("qwen") / "catalogs").exists())
        self.assertTrue(catalog_file.exists())

    def test_unknown_provider_field_refused(self):
        for bad in (
            'api_key = "sk-LITERAL-SECRET"\n',
            '[model_providers.qwen.http_headers]\nAuthorization = "Bearer sk-xyz"\n',
        ):
            with self.subTest(bad=bad):
                template = self.make_template(extra=bad)
                with self.assertRaises(ProvisioningError) as ctx:
                    create_daemon(
                        "qwen",
                        self.home("qwen"),
                        "qwen",
                        "qwen3-max",
                        registry_path=self.registry,
                        template_home=template,
                    )
                message = str(ctx.exception)
                self.assertNotIn("sk-LITERAL-SECRET", message)
                self.assertNotIn("sk-xyz", message)
                self.assertFalse(self.home("qwen").exists())

    def test_catalog_inside_provider_table_refused(self):
        template = self.make_template(extra='model_catalog_json = "x.json"\n')
        with self.assertRaises(ProvisioningError) as ctx:
            create_daemon(
                "qwen",
                self.home("qwen"),
                "qwen",
                "qwen3-max",
                registry_path=self.registry,
                template_home=template,
            )
        self.assertIn("model_catalog_json", str(ctx.exception))

    def test_literal_secret_env_key_refused(self):
        fields = (
            'name = "Qwen"\n'
            'base_url = "https://dashscope.example.invalid/v1"\n'
            'env_key = "sk-live-abc123"\n'
            'wire_api = "responses"\n'
        )
        template = self.make_template(fields=fields)
        with self.assertRaises(ProvisioningError) as ctx:
            create_daemon(
                "qwen",
                self.home("qwen"),
                "qwen",
                "qwen3-max",
                registry_path=self.registry,
                template_home=template,
            )
        self.assertNotIn("sk-live-abc123", str(ctx.exception))

    def test_base_url_userinfo_refused(self):
        fields = (
            'name = "Qwen"\n'
            'base_url = "https://user:pass@host.example.invalid/v1"\n'
            'env_key = "QWEN_API_KEY"\n'
        )
        template = self.make_template(fields=fields)
        with self.assertRaises(ProvisioningError) as ctx:
            create_daemon(
                "qwen",
                self.home("qwen"),
                "qwen",
                "qwen3-max",
                registry_path=self.registry,
                template_home=template,
            )
        self.assertNotIn("pass", str(ctx.exception))

    def test_base_url_query_refused(self):
        fields = (
            'name = "Qwen"\n'
            'base_url = "https://host.example.invalid/v1?api-key=SECRET"\n'
            'env_key = "QWEN_API_KEY"\n'
        )
        template = self.make_template(fields=fields)
        with self.assertRaises(ProvisioningError):
            create_daemon(
                "qwen",
                self.home("qwen"),
                "qwen",
                "qwen3-max",
                registry_path=self.registry,
                template_home=template,
            )

    def test_env_http_headers_literal_refused(self):
        extra = '[model_providers.qwen.env_http_headers]\nAuthorization = "Bearer sk-xyz"\n'
        template = self.make_template(extra=extra)
        with self.assertRaises(ProvisioningError) as ctx:
            create_daemon(
                "qwen",
                self.home("qwen"),
                "qwen",
                "qwen3-max",
                registry_path=self.registry,
                template_home=template,
            )
        self.assertNotIn("sk-xyz", str(ctx.exception))

    def test_wire_api_invalid_refused(self):
        template = self.make_template(extra='wire_api = "grpc"\n')
        with self.assertRaises(ProvisioningError):
            create_daemon(
                "qwen",
                self.home("qwen"),
                "qwen",
                "qwen3-max",
                registry_path=self.registry,
                template_home=template,
            )

    def test_requires_openai_auth_type_refused(self):
        template = self.make_template(extra='requires_openai_auth = "false"\n')
        with self.assertRaises(ProvisioningError):
            create_daemon(
                "qwen",
                self.home("qwen"),
                "qwen",
                "qwen3-max",
                registry_path=self.registry,
                template_home=template,
            )

    def test_retry_field_type_refused(self):
        for bad in (
            "request_max_retries = -1\n",
            "stream_max_retries = true\n",
            "stream_idle_timeout_ms = 0\n",
        ):
            with self.subTest(bad=bad):
                template = self.make_template(extra=bad)
                with self.assertRaises(ProvisioningError):
                    create_daemon(
                        "qwen",
                        self.home("qwen"),
                        "qwen",
                        "qwen3-max",
                        registry_path=self.registry,
                        template_home=template,
                    )

    def test_non_builtin_missing_base_url_refused(self):
        fields = 'name = "Qwen"\nenv_key = "QWEN_API_KEY"\nwire_api = "responses"\n'
        template = self.make_template(fields=fields)
        with self.assertRaises(ProvisioningError) as ctx:
            create_daemon(
                "qwen",
                self.home("qwen"),
                "qwen",
                "qwen3-max",
                registry_path=self.registry,
                template_home=template,
            )
        self.assertIn("base_url", str(ctx.exception))

    def test_openai_with_template_no_definition(self):
        template = self.make_template(provider="qwen", catalog="catalogs/qwen.json")
        create_daemon(
            "gpt",
            self.home("gpt"),
            "openai",
            "gpt-5",
            registry_path=self.registry,
            template_home=template,
        )
        config = tomllib.loads((self.home("gpt") / "config.toml").read_text())
        self.assertNotIn("model_providers", config)
        self.assertEqual(config["model_catalog_json"], str(template / "catalogs/qwen.json"))

    def test_error_is_value_error(self):
        with self.assertRaises(ValueError):
            create_daemon("gpt", self.home("gpt"), "openai", "", registry_path=self.registry)


class RegistryTests(ProvisioningTestCase):
    def test_registry_preserves_existing(self):
        self.create_openai("gpt")
        before = self.read_registry()["gpt"]
        self.create_qwen("qwen")
        registry = self.read_registry()
        self.assertEqual(sorted(registry), ["gpt", "qwen"])
        self.assertEqual(registry["gpt"], before)
        self.assertEqual(registry["qwen"]["model_provider"], "qwen")

    def test_duplicate_name_refused(self):
        self.create_openai("gpt")
        before = self.registry.read_bytes()
        with self.assertRaises(ProvisioningError) as ctx:
            create_daemon("gpt", self.home("gpt2"), "openai", "gpt-5", registry_path=self.registry)
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse(self.home("gpt2").exists())

    def test_invalid_registry_refused(self):
        self.registry.write_text("this is not valid toml {{{")
        before = self.registry.read_bytes()
        with self.assertRaises(ValueError):
            self.create_openai()
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse(self.home("gpt").exists())

    def test_registry_unknown_top_level_refused(self):
        self.registry.write_text(
            f'foo = "bar"\n[backends.gpt]\nhome = "{self.home("gpt")}"\nmodel_provider = "openai"\n'
        )
        with self.assertRaises(ConfigError):
            self.create_openai()
        self.assertFalse(self.home("gpt").exists())

    def test_registry_inside_home_refused(self):
        with self.assertRaises(ProvisioningError) as ctx:
            create_daemon(
                "gpt",
                self.home("gpt"),
                "openai",
                "gpt-5",
                registry_path=self.home("gpt") / "bridge.toml",
            )
        self.assertIn("inside", str(ctx.exception))
        self.assertFalse(self.home("gpt").exists())

    def test_registry_equals_template_config_refused(self):
        template = self.make_template()
        before = (template / "config.toml").read_bytes()
        with self.assertRaises(ProvisioningError):
            create_daemon(
                "qwen",
                self.home("qwen"),
                "qwen",
                "qwen3-max",
                registry_path=template / "config.toml",
                template_home=template,
            )
        self.assertEqual((template / "config.toml").read_bytes(), before)
        self.assertFalse(self.home("qwen").exists())

    def test_registry_symlink_refused(self):
        self.registry.symlink_to(self.root / "real-registry.toml")
        with self.assertRaises(ProvisioningError):
            self.create_openai()
        self.assertFalse(self.home("gpt").exists())

    def test_default_registry_env_var(self):
        env_path = self.root / "env-registry.toml"
        with patch.dict(os.environ, {"CODEXD_BRIDGE_CONFIG": str(env_path)}):
            self.create_openai(registry_path=None)
        self.assertTrue(env_path.exists())
        self.assertIs(load_backends(env_path)["gpt"].enabled, False)

    def test_default_registry_home_path(self):
        self.create_openai(registry_path=None)
        default = self.root / ".config" / "codexd-bridge" / "config.toml"
        self.assertTrue(default.exists())
        self.assertIs(load_backends(default)["gpt"].enabled, False)

    def test_explicit_registry_overrides_env(self):
        with patch.dict(os.environ, {"CODEXD_BRIDGE_CONFIG": str(self.root / "no" / "x.toml")}):
            self.create_openai(registry_path=self.registry)
        self.assertTrue(self.registry.exists())

    def test_concurrent_registration(self):
        def worker(index):
            name = f"b{index}"
            return create_daemon(
                name, self.home(name), "openai", "gpt-5", registry_path=self.registry
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(worker, range(4)))
        registry = self.read_registry()
        self.assertEqual(sorted(registry), ["b0", "b1", "b2", "b3"])
        for index in range(4):
            self.assertTrue(self.home(f"b{index}").is_dir())
            self.assertIs(registry[f"b{index}"]["enabled"], False)
        self.run.assert_not_called()

    def test_atomic_write_failure_leaves_registry(self):
        self.create_openai("gpt")
        before = self.registry.read_bytes()
        with patch("codexd_bridge.provisioning.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(ProvisioningError):
                self.create_qwen("qwen")
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse(self.home("qwen").exists())
        self.assertEqual(list(self.registry.parent.glob("*.tmp")), [])

    def test_lock_collision_rolls_back_home(self):
        self.create_openai("gpt")
        before = self.registry.read_bytes()
        gpt_entry = self.read_registry()["gpt"]
        qwen_entry = {
            "home": str(self.home("qwen")),
            "model_provider": "qwen",
            "default_model": "qwen3-max",
            "enabled": False,
        }
        with patch(
            "codexd_bridge.provisioning._load_registry",
            side_effect=[{"gpt": gpt_entry}, {"gpt": gpt_entry, "qwen": qwen_entry}],
        ):
            with self.assertRaises(ProvisioningError) as ctx:
                self.create_qwen("qwen")
        self.assertIn("already exists", str(ctx.exception))
        self.assertFalse(self.home("qwen").exists())
        self.assertEqual(self.registry.read_bytes(), before)

    def test_stale_home_blocks_registration(self):
        stale = self.root / "stale-home"
        self.registry.write_text(f'[backends.gpt]\nhome = "{stale}"\nmodel_provider = "openai"\n')
        with self.assertRaises(ValueError):
            self.create_qwen("qwen")
        self.assertFalse(self.home("qwen").exists())

    def test_home_reuse_error_names_backend(self):
        target = self.home("gpt")
        conflicting = {"home": str(target), "model_provider": "openai"}
        with patch(
            "codexd_bridge.provisioning._load_registry",
            return_value={"other": conflicting},
        ):
            with self.assertRaises(ProvisioningError) as ctx:
                create_daemon("gpt", target, "openai", "gpt-5", registry_path=self.registry)
        self.assertIn("other", str(ctx.exception))
        self.assertFalse(target.exists())

    def test_serializer_preserves_other_enabled(self):
        self.create_openai("gpt")
        set_backend_enabled("gpt", True, registry_path=self.registry)
        self.create_qwen("qwen")
        registry = self.read_registry()
        self.assertIs(registry["gpt"]["enabled"], True)
        self.assertIs(registry["qwen"]["enabled"], False)

    def test_serializer_preserves_old_entry_without_enabled(self):
        old_home = self.home("old")
        old_home.mkdir()
        self.registry.write_text(
            f'[backends.old]\nhome = "{old_home}"\nmodel_provider = "openai"\n'
        )
        self.create_openai("gpt")
        registry = self.read_registry()
        self.assertNotIn("enabled", registry["old"])
        self.assertIs(registry["gpt"]["enabled"], False)
        self.assertIs(load_backends(self.registry)["old"].enabled, True)


class SetBackendEnabledTests(ProvisioningTestCase):
    def test_enable_disable_roundtrip(self):
        self.create_openai("gpt")
        enabled = set_backend_enabled("gpt", True, registry_path=self.registry)
        self.assertIs(enabled.enabled, True)
        self.assertIs(self.read_registry()["gpt"]["enabled"], True)
        disabled = set_backend_enabled("gpt", False, registry_path=self.registry)
        self.assertIs(disabled.enabled, False)
        self.assertIs(self.read_registry()["gpt"]["enabled"], False)
        self.assertIs(load_backends(self.registry)["gpt"].enabled, False)

    def test_preserves_other_entries_raw(self):
        self.create_openai("gpt")
        self.create_qwen("qwen")
        qwen_before = self.read_registry()["qwen"]
        set_backend_enabled("gpt", True, registry_path=self.registry)
        registry = self.read_registry()
        self.assertEqual(registry["qwen"], qwen_before)
        self.assertIs(registry["gpt"]["enabled"], True)

    def test_preserves_target_other_fields(self):
        self.create_openai("gpt", effort="high")
        before = self.read_registry()["gpt"]
        set_backend_enabled("gpt", True, registry_path=self.registry)
        after = self.read_registry()["gpt"]
        self.assertEqual(
            {k: v for k, v in after.items() if k != "enabled"},
            {k: v for k, v in before.items() if k != "enabled"},
        )
        self.assertIs(after["enabled"], True)

    def test_noop_leaves_bytes_identical(self):
        self.create_openai("gpt")
        before = self.registry.read_bytes()
        result = set_backend_enabled("gpt", False, registry_path=self.registry)
        self.assertIs(result.enabled, False)
        self.assertEqual(self.registry.read_bytes(), before)
        set_backend_enabled("gpt", True, registry_path=self.registry)
        middle = self.registry.read_bytes()
        set_backend_enabled("gpt", True, registry_path=self.registry)
        self.assertEqual(self.registry.read_bytes(), middle)

    def test_old_entry_without_enabled(self):
        old_home = self.home("old")
        old_home.mkdir()
        self.registry.write_text(
            f'[backends.old]\nhome = "{old_home}"\nmodel_provider = "openai"\n'
        )
        set_backend_enabled("old", False, registry_path=self.registry)
        self.assertIs(self.read_registry()["old"]["enabled"], False)
        old2 = self.home("old2")
        old2.mkdir()
        self.registry.write_text(f'[backends.old2]\nhome = "{old2}"\nmodel_provider = "openai"\n')
        result = set_backend_enabled("old2", True, registry_path=self.registry)
        self.assertIs(result.enabled, True)
        self.assertNotIn("enabled", self.read_registry()["old2"])

    def test_not_found_refused(self):
        self.create_openai("gpt")
        before = self.registry.read_bytes()
        with self.assertRaises(ProvisioningError) as ctx:
            set_backend_enabled("missing", True, registry_path=self.registry)
        self.assertIn("missing", str(ctx.exception))
        self.assertEqual(self.registry.read_bytes(), before)

    def test_registry_absent_refused(self):
        self.assertFalse(self.registry.exists())
        with self.assertRaises(ProvisioningError):
            set_backend_enabled("gpt", True, registry_path=self.registry)

    def test_invalid_enabled_type_refused(self):
        self.create_openai("gpt")
        for bad in (1, 0, "true", None, 1.0):
            with self.subTest(enabled=bad):
                with self.assertRaises(ProvisioningError):
                    set_backend_enabled("gpt", bad, registry_path=self.registry)

    def test_invalid_name_refused(self):
        for bad in ("", "Gpt", "1x", "g pt"):
            with self.subTest(name=bad):
                with self.assertRaises(ProvisioningError):
                    set_backend_enabled(bad, True, registry_path=self.registry)

    def test_returns_backend(self):
        self.create_openai("gpt", effort="high")
        backend = set_backend_enabled("gpt", True, registry_path=self.registry)
        self.assertIsInstance(backend, Backend)
        self.assertEqual(backend.name, "gpt")
        self.assertEqual(backend.home, self.home("gpt"))
        self.assertEqual(backend.model_provider, "openai")
        self.assertEqual(backend.default_model, "gpt-5")
        self.assertEqual(backend.default_effort, "high")
        self.assertIs(backend.enabled, True)

    def test_concurrent_set_and_create(self):
        self.create_openai("gpt")

        def creator(index):
            name = f"b{index}"
            create_daemon(name, self.home(name), "openai", "gpt-5", registry_path=self.registry)

        def toggler(value):
            set_backend_enabled("gpt", value, registry_path=self.registry)

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(creator, i) for i in range(4)]
            futures += [executor.submit(toggler, True), executor.submit(toggler, False)]
            for future in futures:
                future.result()
        registry = load_backends(self.registry)
        self.assertEqual(sorted(registry), ["b0", "b1", "b2", "b3", "gpt"])
        self.assertIsInstance(registry["gpt"].enabled, bool)

    def test_atomic_failure_leaves_bytes(self):
        self.create_openai("gpt")
        before = self.registry.read_bytes()
        with patch("codexd_bridge.provisioning.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(ProvisioningError):
                set_backend_enabled("gpt", True, registry_path=self.registry)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_default_registry_resolution(self):
        env_path = self.root / "env-registry.toml"
        with patch.dict(os.environ, {"CODEXD_BRIDGE_CONFIG": str(env_path)}):
            self.create_openai(registry_path=None)
            set_backend_enabled("gpt", True, registry_path=None)
        self.assertIs(load_backends(env_path)["gpt"].enabled, True)


class DaemonActionTests(ProvisioningTestCase):
    def setUp(self):
        super().setUp()
        self.backend = self.create_openai("gpt")
        self.run.side_effect = None
        self.run.return_value = self.completed()

    def completed(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def make_socket(self):
        socket = self.home("gpt") / "app-server-control" / "app-server-control.sock"
        socket.parent.mkdir(parents=True, exist_ok=True)
        socket.touch()
        return socket

    def test_start_routing_and_env(self):
        result = daemon_action(self.backend, "start")
        self.run.assert_called_once()
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], ["codex", "app-server", "daemon", "start"])
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.home("gpt")))
        self.assertIs(kwargs["capture_output"], True)
        self.assertIs(kwargs["text"], True)
        self.assertEqual(kwargs["timeout"], 45)
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["backend"], "gpt")
        self.assertEqual(result["home"], str(self.home("gpt")))
        self.assertEqual(
            result["socketPath"],
            str(self.home("gpt") / "app-server-control" / "app-server-control.sock"),
        )

    def test_status_maps_version(self):
        self.run.return_value = self.completed(
            0, '{"status":"running","socketPath":"/native.sock","pid":7}'
        )
        result = daemon_action(self.backend, "status")
        self.assertEqual(self.run.call_args[0][0][-1], "version")
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["pid"], 7)
        self.assertEqual(result["socketPath"], "/native.sock")
        self.assertEqual(result["backend"], "gpt")
        self.assertEqual(result["home"], str(self.home("gpt")))

    def test_stop_routing(self):
        result = daemon_action(self.backend, "stop")
        self.assertEqual(self.run.call_args[0][0][-1], "stop")
        self.assertEqual(result["status"], "stopped")

    def test_start_provider_mismatch_refused(self):
        (self.home("gpt") / "config.toml").write_text('model_provider = "other"\n')
        with self.assertRaises(ProvisioningError):
            daemon_action(self.backend, "start")
        self.run.assert_not_called()

    def test_start_missing_config_refused(self):
        (self.home("gpt") / "config.toml").unlink()
        with self.assertRaises(ProvisioningError):
            daemon_action(self.backend, "start")
        self.run.assert_not_called()

    def test_start_invalid_toml_refused(self):
        (self.home("gpt") / "config.toml").write_text("not toml {{{")
        with self.assertRaises(ProvisioningError):
            daemon_action(self.backend, "start")
        self.run.assert_not_called()

    def test_status_and_stop_allow_mismatch(self):
        (self.home("gpt") / "config.toml").write_text('model_provider = "other"\n')
        self.run.return_value = self.completed(1, "", "anything")
        self.assertEqual(daemon_action(self.backend, "status")["status"], "stopped")
        self.run.return_value = self.completed()
        self.assertEqual(daemon_action(self.backend, "stop")["status"], "stopped")
        self.assertEqual(self.run.call_count, 2)

    def test_status_stopped_no_socket(self):
        self.run.return_value = self.completed(1, "", "anything")
        result = daemon_action(self.backend, "status")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            result["socketPath"],
            str(self.home("gpt") / "app-server-control" / "app-server-control.sock"),
        )

    def test_status_unreachable(self):
        self.make_socket()
        self.run.return_value = self.completed(1, "", "failed to connect to daemon")
        self.assertEqual(daemon_action(self.backend, "status")["status"], "unreachable")

    def test_status_other_error_no_secret_echo(self):
        self.make_socket()
        self.run.return_value = self.completed(1, "OUT-SECRET", "ERR-SECRET")
        with self.assertRaises(BridgeError) as ctx:
            daemon_action(self.backend, "status")
        message = str(ctx.exception)
        self.assertNotIn("OUT-SECRET", message)
        self.assertNotIn("ERR-SECRET", message)
        log = self.home("gpt") / "daemon-logs" / "daemon-status.log"
        self.assertTrue(log.exists())
        self.assertEqual(mode(log), 0o600)
        self.assertEqual(mode(log.parent), 0o700)
        self.assertIn("ERR-SECRET", log.read_text())

    def test_start_failure_log(self):
        self.run.return_value = self.completed(2, "OUT-SECRET", "ERR-SECRET")
        with self.assertRaises(BridgeError) as ctx:
            daemon_action(self.backend, "start")
        message = str(ctx.exception)
        self.assertIn("exit code 2", message)
        self.assertNotIn("SECRET", message)
        log = self.home("gpt") / "daemon-logs" / "daemon-start.log"
        self.assertTrue(log.exists())
        self.assertIn("ERR-SECRET", log.read_text())

    def test_stop_failure(self):
        self.run.return_value = self.completed(1, "", "ERR-SECRET")
        with self.assertRaises(BridgeError) as ctx:
            daemon_action(self.backend, "stop")
        self.assertNotIn("ERR-SECRET", str(ctx.exception))
        self.assertTrue((self.home("gpt") / "daemon-logs" / "daemon-stop.log").exists())

    def test_timeout(self):
        self.run.side_effect = subprocess.TimeoutExpired(
            ["codex"], 3, output=b"OUT-SECRET", stderr=b"ERR-SECRET"
        )
        with self.assertRaises(BridgeError) as ctx:
            daemon_action(self.backend, "start", timeout=3)
        message = str(ctx.exception)
        self.assertIn("timed out", message)
        self.assertNotIn("SECRET", message)
        self.assertEqual(self.run.call_args.kwargs["timeout"], 3)
        log = self.home("gpt") / "daemon-logs" / "daemon-start.log"
        self.assertIn("ERR-SECRET", log.read_text())

    def test_invalid_action(self):
        for bad in ("restart", "bogus", "", None):
            with self.subTest(action=bad):
                with self.assertRaises(ProvisioningError):
                    daemon_action(self.backend, bad)
        self.run.assert_not_called()

    def test_invalid_timeout(self):
        for bad in (0, -1, float("nan"), float("inf"), True, "45"):
            with self.subTest(timeout=bad):
                with self.assertRaises(ProvisioningError):
                    daemon_action(self.backend, "status", timeout=bad)
        self.run.assert_not_called()

    def test_missing_home(self):
        ghost = Backend(name="ghost", home=self.root / "ghost", model_provider="openai")
        for action in ("start", "status", "stop"):
            with self.subTest(action=action):
                with self.assertRaises(ProvisioningError):
                    daemon_action(ghost, action)
        self.run.assert_not_called()

    def test_status_invalid_json(self):
        self.run.return_value = self.completed(0, "not json SECRET")
        with self.assertRaises(BridgeError) as ctx:
            daemon_action(self.backend, "status")
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertTrue((self.home("gpt") / "daemon-logs" / "daemon-status.log").exists())

    def test_exec_oserror(self):
        self.run.side_effect = FileNotFoundError(2, "No such file")
        with self.assertRaises(BridgeError) as ctx:
            daemon_action(self.backend, "start")
        self.assertIn("cannot execute codex CLI", str(ctx.exception))

    def test_backend_type(self):
        for bad in (object(), None, "gpt"):
            with self.subTest(backend=bad):
                with self.assertRaises(ProvisioningError):
                    daemon_action(bad, "status")

    def test_env_only_codex_home_overridden(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin", "QWEN_API_KEY": "sk-ENV-SECRET"}):
            result = daemon_action(self.backend, "start")
        env = self.run.call_args.kwargs["env"]
        self.assertEqual(env["CODEX_HOME"], str(self.home("gpt")))
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["QWEN_API_KEY"], "sk-ENV-SECRET")
        self.assertIsNot(env, os.environ)
        self.assertNotIn("sk-ENV-SECRET", str(result))

    def test_errors_are_catchable(self):
        self.run.return_value = self.completed(1, "", "x")
        with self.assertRaises(BridgeError):
            daemon_action(self.backend, "stop")
        with self.assertRaises(ValueError):
            daemon_action(self.backend, "restart")


if __name__ == "__main__":
    unittest.main()
