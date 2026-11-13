"""Unit tests for codexd_bridge.config (stdlib only; no daemon or model access)."""

from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexd_bridge.config import CONFIG_ENV_VAR, Backend, ConfigError, load_backends


class ConfigTestCase(unittest.TestCase):
    """Shared temp workspace: cleared environment with HOME inside it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"HOME": str(self.root)}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def make_home(self, name: str = "qwen") -> Path:
        home = self.root / "homes" / name
        home.mkdir(parents=True, exist_ok=True)
        return home

    def write_config(self, text: str, filename: str = "bridge.toml") -> Path:
        path = self.root / filename
        path.write_text(text, encoding="utf-8")
        return path

    def write_default_config(self, text: str) -> Path:
        path = self.root / ".config" / "codexd-bridge" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def config_for(self, alias: str, home: Path | str, extra: str = "") -> str:
        return f'[backends."{alias}"]\nhome = "{home}"\nmodel_provider = "qwen"\n{extra}'

    def config_with_raw(self, field: str, raw: str) -> str:
        """Build a config assigning a raw TOML value to a non-home field."""
        home = self.make_home()
        lines = ["[backends.qwen]", f'home = "{home}"']
        if field != "model_provider":
            lines.append('model_provider = "qwen"')
        lines.append(f"{field} = {raw}")
        return "\n".join(lines) + "\n"

    def assert_config_error(self, text: str, *fragments: str) -> str:
        path = self.write_config(text)
        with self.assertRaises(ConfigError) as ctx:
            load_backends(path)
        message = str(ctx.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)
        return message


class PathResolutionTests(ConfigTestCase):
    def test_explicit_path_dual_backends(self):
        qwen_home = self.make_home("qwen")
        gpt_home = self.make_home("gpt")
        text = (
            f'[backends.qwen]\nhome = "{qwen_home}"\nmodel_provider = "qwen"\n'
            'default_model = "qwen3-max"\ndefault_effort = "high"\n\n'
            f'[backends.gpt]\nhome = "{gpt_home}"\nmodel_provider = "openai"\n'
        )
        backends = load_backends(self.write_config(text))
        self.assertIsInstance(backends, dict)
        self.assertEqual(list(backends), ["qwen", "gpt"])
        qwen = backends["qwen"]
        self.assertIsInstance(qwen, Backend)
        self.assertEqual(qwen.name, "qwen")
        self.assertIsInstance(qwen.home, Path)
        self.assertEqual(qwen.home, qwen_home)
        self.assertTrue(qwen.home.is_absolute())
        self.assertEqual(qwen.model_provider, "qwen")
        self.assertEqual(qwen.default_model, "qwen3-max")
        self.assertEqual(qwen.default_effort, "high")
        gpt = backends["gpt"]
        self.assertEqual(gpt.home, gpt_home)
        self.assertEqual(gpt.model_provider, "openai")
        self.assertIsNone(gpt.default_model)
        self.assertIsNone(gpt.default_effort)
        self.assertEqual(
            qwen.socket_path, qwen_home / "app-server-control" / "app-server-control.sock"
        )
        self.assertEqual(
            gpt.socket_path, gpt_home / "app-server-control" / "app-server-control.sock"
        )

    def test_env_var_path_used(self):
        path = self.write_config(self.config_for("qwen", self.make_home()))
        with patch.dict(os.environ, {CONFIG_ENV_VAR: str(path)}):
            backends = load_backends()
        self.assertEqual(list(backends), ["qwen"])

    def test_explicit_path_overrides_env_var(self):
        env_path = self.write_config(
            self.config_for("fromenv", self.make_home("fromenv")), "env.toml"
        )
        explicit_path = self.write_config(
            self.config_for("fromexplicit", self.make_home("fromexplicit")), "explicit.toml"
        )
        with patch.dict(os.environ, {CONFIG_ENV_VAR: str(env_path)}):
            backends = load_backends(explicit_path)
        self.assertEqual(list(backends), ["fromexplicit"])
        # An invalid env path must not matter when an explicit path is given.
        with patch.dict(os.environ, {CONFIG_ENV_VAR: str(self.root / "missing.toml")}):
            backends = load_backends(explicit_path)
        self.assertEqual(list(backends), ["fromexplicit"])

    def test_env_var_overrides_default(self):
        self.write_default_config(self.config_for("fromdefault", self.make_home("fromdefault")))
        env_path = self.write_config(self.config_for("fromenv", self.make_home("fromenv")))
        with patch.dict(os.environ, {CONFIG_ENV_VAR: str(env_path)}):
            backends = load_backends()
        self.assertEqual(list(backends), ["fromenv"])

    def test_default_path_used(self):
        self.write_default_config(self.config_for("fromdefault", self.make_home("fromdefault")))
        backends = load_backends()
        self.assertEqual(list(backends), ["fromdefault"])

    def test_env_var_missing_file_no_silent_fallback(self):
        self.write_default_config(self.config_for("fromdefault", self.make_home("fromdefault")))
        with patch.dict(os.environ, {CONFIG_ENV_VAR: str(self.root / "missing.toml")}):
            with self.assertRaises(ConfigError) as ctx:
                load_backends()
        self.assertIn(CONFIG_ENV_VAR, str(ctx.exception))

    def test_explicit_missing_file(self):
        with self.assertRaises(ConfigError) as ctx:
            load_backends(self.root / "missing.toml")
        self.assertIn("explicit path", str(ctx.exception))

    def test_default_missing_file(self):
        with self.assertRaises(ConfigError) as ctx:
            load_backends()
        self.assertIn("default", str(ctx.exception))

    def test_empty_env_var_treated_as_unset(self):
        self.write_default_config(self.config_for("fromdefault", self.make_home("fromdefault")))
        with patch.dict(os.environ, {CONFIG_ENV_VAR: ""}):
            backends = load_backends()
        self.assertEqual(list(backends), ["fromdefault"])

    def test_tilde_in_env_var_path(self):
        self.write_config(self.config_for("qwen", self.make_home()), "bridge.toml")
        with patch.dict(os.environ, {CONFIG_ENV_VAR: "~/bridge.toml"}):
            backends = load_backends()
        self.assertEqual(list(backends), ["qwen"])


class HomeValidationTests(ConfigTestCase):
    def test_tilde_home_expanded(self):
        home = self.make_home("qwen")
        rel = "~/" + home.relative_to(self.root).as_posix()
        path = self.write_config(self.config_for("qwen", rel))
        backends = load_backends(path)
        self.assertEqual(backends["qwen"].home, home)
        self.assertTrue(backends["qwen"].home.is_absolute())
        self.assertEqual(
            backends["qwen"].socket_path,
            home / "app-server-control" / "app-server-control.sock",
        )

    def test_relative_home_rejected(self):
        self.assert_config_error(
            self.config_for("qwen", "relative/dir"), "qwen", "'home'", "absolute"
        )

    def test_nonexistent_home_rejected(self):
        self.assert_config_error(
            self.config_for("qwen", self.root / "no-such-dir"), "qwen", "'home'", "directory"
        )

    def test_home_pointing_at_file_rejected(self):
        file_path = self.root / "not-a-dir"
        file_path.write_text("x", encoding="utf-8")
        self.assert_config_error(self.config_for("qwen", file_path), "qwen", "'home'")

    def test_unknown_user_home_rejected(self):
        message = self.assert_config_error(
            self.config_for("qwen", "~no-such-user-zzz9/homes/qwen"), "qwen", "home"
        )
        self.assertNotIn("no-such-user-zzz9", message)

    def test_home_wrong_type_rejected(self):
        for raw in ("42", "true", "1.5", '["x"]', "{ x = 1 }", "1979-05-27"):
            with self.subTest(raw=raw):
                text = f'[backends.qwen]\nhome = {raw}\nmodel_provider = "qwen"\n'
                self.assert_config_error(text, "qwen", "'home'", "string")

    def test_home_empty_rejected(self):
        self.assert_config_error(self.config_for("qwen", ""), "qwen", "'home'", "non-empty")

    def test_target_home_config_and_credentials_not_read(self):
        home = self.make_home()
        (home / "config.toml").write_text("!!! not valid toml [[[", encoding="utf-8")
        (home / "auth.json").write_text('{"token": "sk-DO-NOT-READ"}', encoding="utf-8")
        backends = load_backends(self.write_config(self.config_for("qwen", home)))
        self.assertEqual(backends["qwen"].home, home)


class StructureValidationTests(ConfigTestCase):
    def test_missing_backends_rejected(self):
        self.assert_config_error("", "backends")

    def test_backends_wrong_type_rejected(self):
        self.assert_config_error('backends = "nope"\n', "backends", "table")

    def test_empty_backends_rejected(self):
        self.assert_config_error("[backends]\n", "backends", "at least one")

    def test_backend_not_a_table_rejected(self):
        self.assert_config_error("[backends]\nqwen = 7\n", "qwen", "table")

    def test_unknown_top_level_field_rejected(self):
        text = self.config_for("qwen", self.make_home()) + "\n[server]\nport = 8080\n"
        self.assert_config_error(text, "server")

    def test_unknown_top_level_value_not_echoed(self):
        text = 'token = "sk-TOPSECRET"\n' + self.config_for("qwen", self.make_home())
        message = self.assert_config_error(text, "'token'")
        self.assertNotIn("sk-TOPSECRET", message)

    def test_unknown_backend_field_rejected_without_echo(self):
        text = self.config_for("qwen", self.make_home(), 'api_key = "sk-SUPERSECRET"\n')
        message = self.assert_config_error(text, "qwen", "api_key")
        self.assertNotIn("sk-SUPERSECRET", message)

    def test_name_field_rejected(self):
        text = self.config_for("qwen", self.make_home(), 'name = "other"\n')
        self.assert_config_error(text, "qwen", "'name'")

    def test_endpoint_field_rejected(self):
        text = self.config_for("qwen", self.make_home(), 'endpoint = "ws://127.0.0.1:9999"\n')
        message = self.assert_config_error(text, "qwen", "endpoint")
        self.assertNotIn("ws://127.0.0.1:9999", message)

    def test_invalid_toml_rejected_without_echo(self):
        message = self.assert_config_error('model_provider = "sk-SECRET"\nbroken ==\n', "TOML")
        self.assertNotIn("sk-SECRET", message)

    def test_missing_home_rejected(self):
        self.assert_config_error(
            '[backends.qwen]\nmodel_provider = "qwen"\n', "qwen", "'home'", "missing"
        )

    def test_missing_model_provider_rejected(self):
        home = self.make_home()
        self.assert_config_error(
            f'[backends.qwen]\nhome = "{home}"\n', "qwen", "'model_provider'", "missing"
        )


class NameValidationTests(ConfigTestCase):
    def test_invalid_names_rejected(self):
        home = self.make_home()
        for name in ("Qwen", "1qwen", "_qwen", "-qwen", "q wen", "q.wen", "qüeen", ""):
            with self.subTest(name=name):
                self.assert_config_error(self.config_for(name, home), "name")

    def test_valid_names_accepted(self):
        for name in ("q", "qwen-2", "a_b-c9", "z9"):
            with self.subTest(name=name):
                text = self.config_for(name, self.make_home(name))
                backends = load_backends(self.write_config(text))
                self.assertEqual(backends[name].name, name)


class TextFieldValidationTests(ConfigTestCase):
    def test_model_provider_wrong_type_rejected(self):
        for raw in ("42", "true", '["a"]', "{ a = 1 }"):
            with self.subTest(raw=raw):
                self.assert_config_error(
                    self.config_with_raw("model_provider", raw), "qwen", "'model_provider'"
                )

    def test_model_provider_empty_or_blank_rejected(self):
        for raw in ('""', '"   "'):
            with self.subTest(raw=raw):
                self.assert_config_error(
                    self.config_with_raw("model_provider", raw), "qwen", "non-empty"
                )

    def test_model_provider_length_limit(self):
        ok = "p" * 128
        backends = load_backends(
            self.write_config(self.config_with_raw("model_provider", f'"{ok}"'))
        )
        self.assertEqual(backends["qwen"].model_provider, ok)
        too_long = '"sk-' + "x" * 126 + '"'
        message = self.assert_config_error(
            self.config_with_raw("model_provider", too_long), "qwen", "128"
        )
        self.assertNotIn("sk-", message)

    def test_optional_fields_wrong_type_rejected(self):
        for field in ("default_model", "default_effort"):
            for raw in ("42", "true", '["a"]', "{ a = 1 }"):
                with self.subTest(field=field, raw=raw):
                    self.assert_config_error(
                        self.config_with_raw(field, raw), "qwen", f"'{field}'", "string"
                    )

    def test_optional_fields_empty_rejected(self):
        for field in ("default_model", "default_effort"):
            for raw in ('""', '"   "'):
                with self.subTest(field=field, raw=raw):
                    self.assert_config_error(
                        self.config_with_raw(field, raw), "qwen", f"'{field}'", "non-empty"
                    )

    def test_optional_fields_length_limit(self):
        for field in ("default_model", "default_effort"):
            with self.subTest(field=field):
                ok = "v" * 128
                backends = load_backends(self.write_config(self.config_with_raw(field, f'"{ok}"')))
                self.assertEqual(getattr(backends["qwen"], field), ok)
                too_long = '"sk-' + "x" * 126 + '"'
                message = self.assert_config_error(
                    self.config_with_raw(field, too_long), "qwen", f"'{field}'", "128"
                )
                self.assertNotIn("sk-", message)

    def test_effort_is_not_a_fixed_enum(self):
        text = self.config_for("qwen", self.make_home(), 'default_effort = "ultra-max-9000"\n')
        backends = load_backends(self.write_config(text))
        self.assertEqual(backends["qwen"].default_effort, "ultra-max-9000")


class BackendDataclassTests(unittest.TestCase):
    def test_config_error_is_value_error(self):
        self.assertTrue(issubclass(ConfigError, ValueError))

    def test_fields_exact(self):
        names = tuple(f.name for f in dataclasses.fields(Backend))
        self.assertEqual(
            names, ("name", "home", "model_provider", "default_model", "default_effort")
        )

    def test_frozen(self):
        backend = Backend(name="qwen", home=Path("/tmp/h"), model_provider="qwen")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            backend.name = "other"

    def test_socket_path_derived_and_no_endpoint(self):
        backend = Backend(name="qwen", home=Path("/tmp/h"), model_provider="qwen")
        self.assertEqual(
            backend.socket_path, Path("/tmp/h/app-server-control/app-server-control.sock")
        )
        self.assertFalse(hasattr(backend, "endpoint"))
        self.assertIsNone(backend.default_model)
        self.assertIsNone(backend.default_effort)


if __name__ == "__main__":
    unittest.main()
