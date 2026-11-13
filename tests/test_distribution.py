"""Exercise downloaded-project installation without network or touching the real home."""

import contextlib
import io
import json
import os
import socket
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from codexd_bridge.installation import (
    detach_home,
    detach_installation,
    disabled_config,
    installation_operation,
    load_registrations,
    refresh_installation,
    registration_index,
    remember_registration,
    uninstall_current,
)
from codexd_bridge.providers import _launcher_text, _resolve_executable
from codexd_bridge.transport import BridgeError
from scripts import manage_install as installer


class DistributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.home = self.directory / "user home"
        self.home.mkdir()
        self.layout = installer.Layout(self.home, self.home / "data/program", self.home / "bin")
        self.source = self.directory / "download"
        self.source.mkdir()
        for name in ("pyproject.toml", "requirements.lock"):
            (self.source / name).write_text("test fixture\n")
        self.build = patch.object(installer, "build_release", side_effect=self.build_release)
        self.build.start()
        self.addCleanup(self.build.stop)

    def build_release(self, source, release):
        self.assertTrue((source / "pyproject.toml").is_file())
        (release / "bin").mkdir()
        python = release / "bin/python"
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$CODEXD_BRIDGE_EXECUTABLE" "$@"\n')
        python.chmod(0o755)

    def install(self, shell="/bin/bash"):
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.source, self.layout, shell=shell)

    def uninstall(self):
        with contextlib.redirect_stdout(io.StringIO()):
            installer.uninstall(self.layout.root)

    def test_launcher_works_from_another_directory_and_after_download_is_removed(self):
        self.install()
        import shutil

        shutil.rmtree(self.source)
        launcher = self.layout.bin_dir / "codexd-bridge"
        result = subprocess.run([str(launcher), "--config", "a path", "ui"], cwd="/",
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.splitlines(), [str(self.layout.command),
                         "-I", "-m", "codexd_bridge.cli", "--config", "a path", "ui"])
        with patch.dict(os.environ, {"CODEXD_BRIDGE_EXECUTABLE": str(self.layout.command)}):
            self.assertEqual(_resolve_executable(None), self.layout.command)
        self.assertFalse(os.path.lexists(self.layout.bin_dir / "codexd-bridge-uninstall"))

    def test_upgrade_switches_release_without_duplicating_path_or_changing_command(self):
        self.install()
        first = (self.layout.root / "current").readlink()
        (self.layout.root / "registrations.json").write_text("[]")
        with patch.object(installer.subprocess, "run") as refresh:
            self.install()
        self.assertIn("refresh_installation", refresh.call_args.args[0][3])
        self.assertEqual(refresh.call_args.args[0][-2:], [str(self.layout.root), str(first)])
        second = (self.layout.root / "current").readlink()
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())  # Existing workers may still need the old interpreter.
        self.assertEqual((self.home / ".bashrc").read_text().count(installer.PATH_BEGIN), 1)
        self.assertEqual((self.layout.bin_dir / "codexd-bridge").readlink(), self.layout.command)

    def test_upgrade_refreshes_owned_entries_and_preserves_custom_or_disabled_integrations(self):
        self.install()
        previous = (self.layout.root / "current").resolve()
        old_guide = previous / "lib/python3.12/site-packages/codexd_bridge/skill"
        old_guide.mkdir(parents=True)
        (old_guide / "SKILL.md").write_text("old bundled guide")
        records = []
        for name in ("owned", "custom", "disabled", "other", "offline", "compat-only"):
            home = self.directory / name
            guide = home / "skills/codexd-bridge"
            guide.mkdir(parents=True)
            (guide / "SKILL.md").write_text("user edits" if name == "custom" else "old bundled guide")
            registry = self.directory / (name + ".toml")
            command = self.layout.command if name != "other" else Path("/other/bridge")
            entry = "codex-qwen" if name == "compat-only" else "codexd-bridge"
            (home / "config.toml").write_text(f'[mcp_servers.{entry}]\ncommand=' + json.dumps(str(command))
                + '\nargs=' + json.dumps(["--config", str(registry), "serve"])
                + '\nenabled=' + ("false" if name == "disabled" else "true") + '\n')
            records.append({"home": str(home), "registry": str(registry)})
        (self.layout.root / "registrations.json").write_text(json.dumps(records))

        def register(home, *args, **kwargs):
            if home.name == "offline":
                raise BridgeError("offline") from ConnectionRefusedError()
            return {"new_sessions_ready": True}

        with patch("codexd_bridge.integration.register_bridge", side_effect=register) as registration, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            refresh_installation(self.layout.root, previous)
        self.assertEqual([call.args[0].name for call in registration.call_args_list], ["owned", "custom", "offline"])
        for name in ("owned", "offline"):
            self.assertIn("name: codexd-bridge", (self.directory / name / "skills/codexd-bridge/SKILL.md").read_text())
        for name, expected in (("custom", "user edits"), ("disabled", "old bundled guide"), ("other", "old bundled guide")):
            self.assertEqual((self.directory / name / "skills/codexd-bridge/SKILL.md").read_text(), expected)
        self.assertIn("stopped daemon", output.getvalue())
        self.assertIn("compatibility-only", output.getvalue())

    def test_uninstall_subcommand_uses_its_installation_without_the_download(self):
        self.install()
        with patch.dict(os.environ, {"CODEXD_BRIDGE_EXECUTABLE": str(self.layout.command)}), patch(
            "codexd_bridge.installation.os.execv"
        ) as execute:
            uninstall_current()
        python = str(self.layout.root / "current/bin/python")
        execute.assert_called_once_with(python, [python, "-I",
                                       str(self.layout.root / "manage_install.py"),
                                       "uninstall", "--prefix", str(self.layout.root)])

    def test_uninstall_subcommand_refuses_an_unmanaged_copy(self):
        with patch("codexd_bridge.providers._resolve_executable", return_value=self.directory / "bin/codexd-bridge"), patch(
            "codexd_bridge.installation.os.execv"
        ) as execute:
            with self.assertRaisesRegex(RuntimeError, "not installed by install.sh"):
                uninstall_current()
        execute.assert_not_called()

    def test_installer_paths_work_through_parent_symlinks_and_dotdot(self):
        alias = self.directory / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        (self.home / "data").mkdir()
        prefix = alias / "data/../data/program"
        public_bin = alias / "bin"
        argv = ["installer", "install", "--source", str(self.source), "--prefix", str(prefix),
                "--bin-dir", str(public_bin), "--no-launch"]
        with patch("sys.argv", argv), patch.dict(os.environ, {"HOME": str(self.home)}), \
                contextlib.redirect_stdout(io.StringIO()):
            installer.main()
        executable = _resolve_executable(public_bin / "codexd-bridge")
        self.assertEqual(executable, self.layout.command)
        self.assertEqual(registration_index(executable, self.home / "registry.toml"),
                         self.layout.root / "registrations.json")
        self.uninstall()
        self.assertFalse(os.path.lexists(public_bin / "codexd-bridge"))

    def test_invalid_ownership_records_fail_without_touching_daemon_files(self):
        self.install()
        index = self.layout.root / "registrations.json"
        for record in (None, {}, [None], [{"home": "relative", "registry": "/r"}],
                       [{"home": "/h", "registry": "/r", "launchers": "invalid"}]):
            index.write_text(json.dumps(record))
            with self.subTest(record=record), self.assertRaisesRegex(BridgeError, "ownership records"):
                load_registrations(index)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            refresh_installation(self.layout.root, (self.layout.root / "current").resolve())
        self.assertIn("Application updated", output.getvalue())
        self.assertIn("ownership", output.getvalue())
        index.unlink()
        target = self.directory / "other.json"
        target.write_text("[]")
        index.symlink_to(target)
        with self.assertRaisesRegex(BridgeError, "ownership records"):
            load_registrations(index)
        self.assertEqual(target.read_text(), "[]")

    def test_invalid_installation_manifest_cannot_remove_commands_or_profiles(self):
        self.install()
        marker = self.layout.root / installer.MARKER
        original = json.loads(marker.read_text())
        for data in ([], {**original, "bin_dir": "relative"},
                     {**original, "profiles": [{"path": "/etc/profile", "block": "unowned", "created": True}]}):
            marker.write_text(json.dumps(data))
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.uninstall()
            self.assertTrue(self.layout.command.exists())
            self.assertIn(installer.PATH_BEGIN, (self.home / ".bashrc").read_text())

    def test_invalid_marker_refuses_ownership_changes_before_any_mutation(self):
        self.install()
        marker = self.layout.root / installer.MARKER
        original = json.loads(marker.read_text())
        for data in ([], {}, {**original, "bin_dir": None}, {**original, "version": True}):
            marker.write_text(json.dumps(data))
            with self.subTest(data=data), self.assertRaisesRegex(BridgeError, "Installation marker"):
                registration_index(self.layout.command, self.home / "registry.toml")
            self.assertFalse((self.layout.root / "registrations.json").exists())

    def test_registration_cannot_race_an_installation_change(self):
        from codexd_bridge.providers import install_provider_launcher
        from codexd_bridge.provisioning import create_daemon
        from codexd_bridge.removal import remove_daemon

        self.install()
        registry = self.home / "routes.toml"
        backend = create_daemon("demo", self.home / "backend", "openai", "test-model",
                                registry_path=registry)
        before = registry.read_bytes()
        with installer.locked(self.layout.root):
            for operation in (install_provider_launcher, remove_daemon):
                with self.assertRaisesRegex(BridgeError, "installation is being changed"):
                    operation("demo", registry_path=registry, executable=self.layout.command)
        self.assertEqual(registry.read_bytes(), before)
        self.assertTrue(backend.home.exists())
        with installation_operation(self.layout.command):
            self.assertTrue(self.layout.command.exists())
        self.uninstall()
        with self.assertRaisesRegex(BridgeError, "executable is unavailable"):
            with installation_operation(self.layout.command):
                self.fail("Uninstalled executables cannot be registered")

    def test_uninstall_refuses_a_broken_ownership_symlink(self):
        self.install()
        (self.layout.root / "registrations.json").symlink_to(self.home / "missing.json")
        with self.assertRaisesRegex(BridgeError, "ownership records"):
            detach_installation(self.layout.root)
        self.assertTrue(self.layout.command.exists())

    def test_upgrade_removes_only_the_owned_legacy_uninstall_command(self):
        self.install()
        legacy = self.layout.bin_dir / "codexd-bridge-uninstall"
        legacy.symlink_to(self.layout.root / "bin" / legacy.name)
        self.install()
        self.assertFalse(os.path.lexists(legacy))
        legacy.write_text("unrelated command")
        self.install()
        self.assertEqual(legacy.read_text(), "unrelated command")

    def test_failed_build_preserves_previous_installation_and_profiles(self):
        self.install()
        before = (self.layout.root / "current").readlink()
        profile = (self.home / ".bashrc").read_bytes()
        with patch.object(installer, "build_release", side_effect=RuntimeError("dependency unavailable")):
            with self.assertRaisesRegex(RuntimeError, "dependency unavailable"):
                self.install()
        self.assertEqual((self.layout.root / "current").readlink(), before)
        self.assertEqual((self.home / ".bashrc").read_bytes(), profile)
        self.assertEqual(len(list((self.layout.root / "releases").iterdir())), 1)

    def test_edits_made_while_building_are_preserved(self):
        profile = self.home / ".bashrc"
        profile.write_text("# original\n")

        def build_and_edit(source, release):
            self.build_release(source, release)
            profile.write_text("# edited while dependencies were installing\n")

        with patch.object(installer, "build_release", side_effect=build_and_edit):
            self.install()
        self.assertTrue(profile.read_text().startswith("# edited while dependencies were installing\n"))
        self.uninstall()
        self.assertEqual(profile.read_text(), "# edited while dependencies were installing\n")

    def test_profile_symlink_created_during_build_is_not_replaced(self):
        original = self.home / "original-shell"
        original.write_text("keep")

        def build_and_link(source, release):
            self.build_release(source, release)
            (self.home / ".bashrc").symlink_to(original)

        with patch.object(installer, "build_release", side_effect=build_and_link):
            with self.assertRaisesRegex(ValueError, "non-regular"):
                self.install()
        self.assertTrue((self.home / ".bashrc").is_symlink())
        self.assertEqual(original.read_text(), "keep")
        self.assertFalse(self.layout.root.exists())

    def test_shell_line_endings_and_missing_final_newline_survive_uninstall(self):
        for original in (b"# a\r\n# b\r\n", b"export EDITOR=vi"):
            with self.subTest(original=original):
                profile = self.home / ".bashrc"
                profile.write_bytes(original)
                self.install()
                self.assertTrue(profile.read_bytes().startswith(original))
                self.uninstall()
                self.assertEqual(profile.read_bytes(), original)

    def test_failed_publish_rolls_back_existing_launcher_and_profile(self):
        self.install()
        previous = (self.layout.root / "current").readlink()
        original = installer.atomic_text

        def fail_marker(path, text, mode=0o600):
            if path.name == installer.MARKER:
                raise OSError("disk full")
            return original(path, text, mode)

        # Fail only once so rollback can restore the original manifest.
        failed = False

        def once(path, text, mode=0o600):
            nonlocal failed
            if path.name == installer.MARKER and not failed:
                failed = True
                return fail_marker(path, text, mode)
            return original(path, text, mode)

        with patch.object(installer, "atomic_text", side_effect=once), self.assertRaises(OSError):
            self.install()
        self.assertEqual((self.layout.root / "current").readlink(), previous)
        self.assertEqual(len(list((self.layout.root / "releases").iterdir())), 1)

    def test_foreign_command_and_unmanaged_application_directory_are_preserved(self):
        self.layout.bin_dir.mkdir(parents=True)
        command = self.layout.bin_dir / "codexd-bridge"
        command.write_text("someone else's command")
        with self.assertRaisesRegex(ValueError, "Another file"):
            self.install()
        self.assertEqual(command.read_text(), "someone else's command")
        command.unlink()
        self.layout.root.mkdir(parents=True)
        (self.layout.root / "important").write_text("keep")
        with self.assertRaisesRegex(ValueError, "not an installation"):
            self.uninstall()
        self.assertEqual((self.layout.root / "important").read_text(), "keep")

    def test_uninstall_preserves_data_and_unrelated_shell_edits_and_is_repeatable(self):
        original = 'export EDITOR=vi\n'
        (self.home / ".bashrc").write_text(original)
        data = self.layout.root.parent / "homes/provider/session.json"
        data.parent.mkdir(parents=True)
        data.write_text("session must survive")
        self.install()
        with (self.home / ".bashrc").open("a") as stream:
            stream.write("# later user edit\n")
        self.uninstall()
        self.uninstall()
        self.assertFalse(self.layout.root.exists())
        self.assertFalse(os.path.lexists(self.layout.bin_dir / "codexd-bridge"))
        self.assertEqual(data.read_text(), "session must survive")
        self.assertEqual((self.home / ".bashrc").read_text(), original + "# later user edit\n")
        self.assertFalse((self.home / ".profile").exists())

    def test_shell_profiles_are_written_for_zsh_and_fish(self):
        for shell, path in (("/bin/zsh", ".zshrc"), ("/bin/fish", ".config/fish/conf.d/codexd-bridge.fish")):
            self.install(shell)
            self.assertIn(installer.PATH_BEGIN, (self.home / path).read_text())
            self.uninstall()
            self.assertFalse((self.home / path).exists())

    def test_custom_shell_config_directories_and_bash_login_are_respected(self):
        custom = self.home / "custom"
        for shell, key, filename in (("zsh", "ZDOTDIR", ".zshrc"),
                                     ("fish", "XDG_CONFIG_HOME", "fish/conf.d/codexd-bridge.fish")):
            with patch.dict(os.environ, {key: str(custom)}):
                self.install("/bin/" + shell)
                self.assertIn(installer.PATH_BEGIN, (custom / filename).read_text())
            self.uninstall()
            self.assertFalse((custom / filename).exists())
        login = self.home / ".bash_login"
        login.write_text("# login\n")
        self.install()
        self.assertIn(installer.PATH_BEGIN, login.read_text())
        self.assertFalse((self.home / ".profile").exists())
        self.uninstall()
        self.assertEqual(login.read_text(), "# login\n")

    def test_uninstall_keeps_a_replaced_command(self):
        self.install()
        command = self.layout.bin_dir / "codexd-bridge"
        command.unlink()
        command.write_text("replacement")
        self.uninstall()
        self.assertEqual(command.read_text(), "replacement")

    def test_offline_unregister_disables_only_owned_mcp_and_preserves_sessions(self):
        from codexd_bridge.integration import install_skill

        self.install()
        daemon = self.home / "codex-home"
        daemon.mkdir()
        config = daemon / "config.toml"
        config.write_text('# preserve comment\nmodel_provider="demo"\n'
                          '[mcp_servers.codexd-bridge]\n'
                          f'command={json.dumps(str(self.layout.command))}\nenabled=true\n'
                          '[mcp_servers.codexd-bridge.env]\nUNCHANGED="kept"\n'
                          '[mcp_servers.other]\ncommand="/other"\nenabled=true\n')
        session = daemon / "session.json"
        session.write_text("keep")
        skill = install_skill(daemon)
        registry = self.home / "registry.toml"
        remember_registration(daemon, self.layout.command, registry)
        remember_registration(daemon, self.layout.command, registry)
        self.assertEqual(len(json.loads((self.layout.root / "registrations.json").read_text())), 1)
        self.layout.bin_dir.mkdir(exist_ok=True)
        launcher = self.layout.bin_dir / "codex-demo"
        launcher.write_text(_launcher_text(self.layout.command, registry, "demo"))
        binary = self.layout.bin_dir / "codex-unrelated"
        binary.write_bytes(b"\x7fELF\xff")
        changed = self.layout.bin_dir / "codex-edited"
        changed_text = _launcher_text(self.layout.command, registry, "edited") + "# user notes\n"
        changed.write_text(changed_text)
        detach_installation(self.layout.root)
        result = tomllib.loads(config.read_text())
        self.assertFalse(result["mcp_servers"]["codexd-bridge"]["enabled"])
        self.assertTrue(result["mcp_servers"]["other"]["enabled"])
        self.assertEqual(result["mcp_servers"]["codexd-bridge"]["env"], {"UNCHANGED": "kept"})
        self.assertTrue(config.read_text().startswith("# preserve comment"))
        self.assertTrue(session.exists())
        self.assertFalse(skill.exists())
        self.assertFalse(launcher.exists())
        self.assertEqual(binary.read_bytes(), b"\x7fELF\xff")
        self.assertEqual(changed.read_text(), changed_text)
        # A separate installation may reuse the same bundled guide in this home.
        install_skill(daemon)
        config.write_text('[mcp_servers.codexd-bridge]\ncommand="/another/installation"\n'
                          '[mcp_servers.codex-qwen]\ncommand=' + json.dumps(str(self.layout.command)) + '\n')
        detach_home(daemon, self.layout.command)
        self.assertTrue(skill.exists())
        self.assertFalse(tomllib.loads(config.read_text())["mcp_servers"]["codex-qwen"]["enabled"])

    def test_offline_config_handles_quoted_tables_and_refuses_unsupported_edits(self):
        command = self.layout.command
        original = ('[mcp_servers."codexd-bridge"] # a comment\n'
                    f'command={json.dumps(str(command))}\n')
        updated = disabled_config(original, command)
        self.assertFalse(tomllib.loads(updated)["mcp_servers"]["codexd-bridge"]["enabled"])
        self.assertEqual(disabled_config(updated, command), updated)
        with self.assertRaisesRegex(RuntimeError, "safely disable"):
            disabled_config('[mcp_servers]\ncodexd-bridge={command=' + json.dumps(str(command)) + '}\n', command)

    def test_stale_daemon_socket_does_not_prevent_uninstall(self):
        self.install()
        daemon = self.directory / "d"
        control = daemon / "app-server-control"
        control.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as stopped:
            stopped.bind(str(control / "app-server-control.sock"))
        config = daemon / "config.toml"
        config.write_text('[mcp_servers.codexd-bridge]\n'
                          f'command={json.dumps(str(self.layout.command))}\n')
        remember_registration(daemon, self.layout.command, self.home / "registry.toml")
        detach_installation(self.layout.root)
        self.assertFalse(tomllib.loads(config.read_text())["mcp_servers"]["codexd-bridge"]["enabled"])
        self.assertTrue((control / "app-server-control.sock").exists())

    def test_uncertain_daemon_connection_does_not_fall_back_to_editing_disk(self):
        self.install()
        daemon = self.directory / "d"
        daemon.mkdir()
        config = daemon / "config.toml"
        original = '[mcp_servers.codexd-bridge]\n' + f'command={json.dumps(str(self.layout.command))}\n'
        config.write_text(original)
        remember_registration(daemon, self.layout.command, self.home / "registry.toml")
        with patch("codexd_bridge.installation.AppServer", side_effect=BridgeError("outcome unknown")):
            with self.assertRaisesRegex(BridgeError, "outcome unknown"):
                detach_installation(self.layout.root)
        self.assertEqual(config.read_text(), original)

    def test_registered_custom_launchers_are_removed_but_edited_ones_are_kept(self):
        self.install()
        directory = self.home / "custom commands"
        directory.mkdir()
        registry = self.home / "registry.toml"
        launchers = [directory / "codex-demo", directory / "codex-edited"]
        for path in launchers:
            path.write_text(_launcher_text(self.layout.command, registry, path.name[6:]))
            remember_registration(self.home / "removed-daemon", self.layout.command, registry, launcher=path)
        launchers[1].write_text(launchers[1].read_text() + "# edited\n")
        detach_installation(self.layout.root)
        self.assertFalse(launchers[0].exists())
        self.assertTrue(launchers[1].exists())

    def test_backend_data_inside_application_directory_prevents_removal(self):
        self.install()
        daemon = self.layout.root / "backend"
        daemon.mkdir()
        data = daemon / "session.json"
        data.write_text("keep this session")
        remember_registration(daemon, self.layout.command, self.home / "registry.toml")
        with self.assertRaisesRegex(BridgeError, "Backend data is inside"):
            detach_installation(self.layout.root)
        self.assertEqual(data.read_text(), "keep this session")
        self.assertTrue(self.layout.command.exists())

    def test_daemon_creation_cannot_place_data_inside_the_installed_application(self):
        from codexd_bridge.provisioning import ProvisioningError, create_daemon

        self.install()
        home = self.layout.root / "backend"
        registry = self.home / "registry.toml"
        with patch.dict(os.environ, {"CODEXD_BRIDGE_EXECUTABLE": str(self.layout.command)}):
            with self.assertRaisesRegex(ProvisioningError, "outside.*application"):
                create_daemon("demo", home, "openai", "test-model", registry_path=registry)
        self.assertFalse(home.exists())
        self.assertFalse(registry.exists())
