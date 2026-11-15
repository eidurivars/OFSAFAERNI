import json
import os
import shlex
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ofsafaerni.history_start import (
    _BRIDGE_START,
    _lookup_bridge,
    _obsidian_registry,
    default_state_dir,
    install_lookup_bridge,
    prepare_config,
)


class HistoryStartTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name).resolve()
        self.state = self.work / "state" / "history"
        self.codex = self.work / "codex-home"
        self.obsidian = self.work / "settings" / "obsidian" / "obsidian.json"
        self.obsidian.parent.mkdir(parents=True)
        self.enterContext(patch("ofsafaerni.history_start._obsidian_registry", return_value=self.obsidian))

    def vault(self, name):
        path = self.work / name
        (path / ".obsidian").mkdir(parents=True)
        return path

    def registry(self, *opened):
        self.obsidian.write_text(json.dumps({
            "vaults": {f"id-{i}": {"path": str(vault), "open": True} for i, vault in enumerate(opened)},
        }), encoding="utf-8-sig")

    def start(self, **options):
        return prepare_config(**{"codex_home": self.codex, "state_dir": self.state, **options})

    def test_unique_active_vault_creates_only_requested_config(self):
        vault = self.vault("Málaskrá O'Brien")
        self.registry(vault)
        registry_before = self.obsidian.read_bytes()
        path = self.start()
        self.assertEqual(path, self.state / "history-config.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {
            "vault": str(vault),
            "codex_home": str(self.codex),
            "state": str(self.state),
            "chatgpt_export": None,
        })
        self.assertEqual(list(self.state.iterdir()), [path])
        self.assertFalse(self.codex.exists())
        self.assertEqual(registry_before, self.obsidian.read_bytes())
        self.assertEqual(list(vault.iterdir()), [vault / ".obsidian"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)

    def test_ambiguous_active_vault_requires_explicit_selection(self):
        first, second = self.vault("first"), self.vault("second")
        self.registry(first, second)
        with self.assertRaisesRegex(ValueError, r"Set --vault explicitly"):
            self.start()
        self.assertFalse(self.state.exists())
        path = self.start(vault=second)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["vault"], str(second))

    def test_autodiscovery_rejects_folder_without_vault_marker(self):
        ordinary = self.work / "ordinary"
        ordinary.mkdir()
        self.registry(ordinary)
        with self.assertRaisesRegex(ValueError, r"Set --vault explicitly"):
            self.start()
        self.assertFalse(self.state.exists())

    def test_autodiscovery_rejects_windows_reparse_vault(self):
        vault = self.vault("linked")
        self.registry(vault)
        lstat = Path.lstat

        def simulated_lstat(path):
            if path == vault:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return lstat(path)

        with patch.object(Path, "lstat", simulated_lstat):
            with self.assertRaisesRegex(ValueError, r"Set --vault explicitly"):
                self.start()
        self.assertFalse(self.state.exists())

    def test_previous_opt_in_and_extra_fields_survive_rerun(self):
        first, second = self.vault("first"), self.vault("second")
        export = self.work / "export.zip"
        export.write_bytes(b"synthetic export")
        path = self.start(vault=first, chatgpt_export=export)
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["retention"] = "same"
        with path.open("w", encoding="utf-8") as file:
            json.dump(saved, file, indent=1)
        original = path.read_bytes()
        self.assertEqual(self.start(), path)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.start(vault=second), path)
        observed = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(observed["retention"], "same")
        self.assertEqual(observed["chatgpt_export"], str(export))
        self.assertEqual(observed["vault"], str(second))

    def test_unreadable_existing_configuration_is_not_replaced(self):
        self.state.mkdir(parents=True)
        config = self.state / "history-config.json"
        config.write_bytes(b"{invalid")
        with self.assertRaises(ValueError):
            self.start(vault=self.vault("new"))
        self.assertEqual(config.read_bytes(), b"{invalid")


class PortableDefaultsTests(unittest.TestCase):
    def test_default_paths_for_linux_mac_and_windows(self):
        home = Path.home()
        for os_name, platform, env, state, registry in (
            ("nt", "win32", {"APPDATA": str(home / "roaming"), "LOCALAPPDATA": str(home / "local")},
             home / "local" / "OFSAFAERNI" / "history", home / "roaming" / "obsidian" / "obsidian.json"),
            ("posix", "linux", {"XDG_CONFIG_HOME": str(home / "xdg-config"), "XDG_STATE_HOME": str(home / "xdg-state")},
             home / "xdg-state" / "ofsafaerni" / "history", home / "xdg-config" / "obsidian" / "obsidian.json"),
            ("posix", "darwin", {}, home / ".local" / "state" / "ofsafaerni" / "history",
             home / "Library" / "Application Support" / "obsidian" / "obsidian.json"),
        ):
            with self.subTest(platform=platform):
                with (
                    patch("ofsafaerni.history_start.os", SimpleNamespace(name=os_name, environ=env)),
                    patch("ofsafaerni.history_start.sys", SimpleNamespace(platform=platform)),
                ):
                    self.assertEqual(default_state_dir(), state)
                    self.assertEqual(_obsidian_registry(), registry)


class LookupBridgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name).resolve()
        self.home = self.work / "codex"
        self.home.mkdir()
        self.config = self.work / "lookup O'Brien.json"
        self.config.write_text('{"private_note": "SYNTHETIC_NO_EXPORT"}', encoding="utf-8")
        self.agents = self.home / "AGENTS.md"
        self.override = self.home / "AGENTS.override.md"

    def test_preserves_native_bytes_and_never_duplicates_managed_block(self):
        old = b"\xef\xbb\xbf# Working agreements\r\n\r\nUse UTF-8.\r\n"
        self.agents.write_bytes(old)
        status = install_lookup_bridge(self.config, self.home)
        self.assertEqual(status, {"status": "installed", "source": str(self.agents)})
        installed = self.agents.read_bytes()
        self.assertTrue(installed.startswith(old))
        self.assertEqual(installed.count(_BRIDGE_START), 1)
        self.assertIn(b"ofsafaerni.history", installed)
        self.assertNotIn(b"SYNTHETIC_NO_EXPORT", installed)
        self.assertEqual(install_lookup_bridge(self.config, self.home)["status"], "unchanged")
        self.assertEqual(self.agents.read_bytes(), installed)

    def test_effective_nonempty_override_gets_bridge(self):
        self.agents.write_bytes(b"base directions\n")
        self.override.write_bytes(b"override directions\n")
        self.assertEqual(install_lookup_bridge(self.config, self.home)["source"], str(self.override))
        self.assertTrue(self.override.read_bytes().startswith(b"override directions\n"))
        self.assertEqual(self.agents.read_bytes(), b"base directions\n")

    def test_empty_override_stays_empty_and_the_base_gets_bridge(self):
        self.override.write_bytes(b"\r\n")
        self.assertEqual(install_lookup_bridge(self.config, self.home)["source"], str(self.agents))
        self.assertEqual(self.override.read_bytes(), b"\r\n")

    def test_update_preserves_prefix_and_suffix_around_managed_block(self):
        self.agents.write_bytes(b"prefix\r\n")
        install_lookup_bridge(self.config, self.home)
        self.agents.write_bytes(self.agents.read_bytes() + b"\r\nsuffix\r\n")
        new = self.work / "another.json"
        new.write_text("{}", encoding="utf-8")
        self.assertEqual(install_lookup_bridge(new, self.home)["status"], "updated")
        observed = self.agents.read_bytes()
        self.assertTrue(observed.startswith(b"prefix\r\n"))
        self.assertTrue(observed.endswith(b"\r\nsuffix\r\n"))
        self.assertEqual(observed.count(_BRIDGE_START), 1)
        self.assertIn(b"another.json", observed)
        self.assertNotIn(b"SYNTHETIC_NO_EXPORT", observed)

    def test_malformed_markers_are_not_overwritten(self):
        raw = b"prefix\n" + _BRIDGE_START + b"\nunfinished user content\n"
        self.agents.write_bytes(raw)
        self.assertEqual(install_lookup_bridge(self.config, self.home)["status"], "skipped")
        self.assertEqual(self.agents.read_bytes(), raw)

    def test_absent_codex_profile_remains_absent(self):
        missing = self.work / "missing-profile"
        self.assertEqual(install_lookup_bridge(self.config, missing)["reason"], "codex-home-missing")
        self.assertFalse(missing.exists())

    def test_reparse_override_is_not_opened_or_replaced(self):
        self.override.write_bytes(b"linked")
        self.agents.write_bytes(b"original")
        lstat = Path.lstat

        def simulated_lstat(path):
            if path == self.override:
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            return lstat(path)

        with patch.object(Path, "lstat", simulated_lstat):
            self.assertEqual(install_lookup_bridge(self.config, self.home)["status"], "skipped")
        self.assertEqual(self.override.read_bytes(), b"linked")
        self.assertEqual(self.agents.read_bytes(), b"original")

    def test_powershell_and_posix_escaping(self):
        path = self.work / "folder with O'Brien" / "auto config.json"
        with patch("ofsafaerni.history_start.os", SimpleNamespace(name="nt", path=os.path)):
            lines = _lookup_bridge(path).decode().splitlines()
            command = next(line for line in lines if line.startswith("& "))
            self.assertIn("'" + str(path).replace("'", "''") + "'", command)
            self.assertTrue(command.endswith("'lookup' '--' '<short task summary>'"))
        with patch("ofsafaerni.history_start.os", SimpleNamespace(name="posix", path=os.path)):
            lines = _lookup_bridge(path).decode().splitlines()
            command = next(line for line in lines if " -I -m ofsafaerni.history " in line)
            args = shlex.split(command)
            self.assertEqual(args[1:], [
                "-I", "-m", "ofsafaerni.history", "--config", str(path),
                "lookup", "--", "<short task summary>",
            ])


if __name__ == "__main__":
    unittest.main()
