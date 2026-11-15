import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ofsafaerni.hermes import _native_home, export_skill, hermes_skill_roots
from ofsafaerni.sources import bundle_digest


def approved_record(files):
    return {"name": "review-table", "approved": True, "sha256": bundle_digest(files), "files": files}


class HermesRootsTests(unittest.TestCase):
    def test_profile_config_and_external_roots_are_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile"
            profile.mkdir()
            config = profile / "config.yaml"
            config.write_text(
                "skills:\n  create_dir: created\n  external_dirs:\n    - ${OFSAFAERNI_SHARED_TEST}/notes\n    - skills\n",
                encoding="utf-8",
            )
            original = config.read_bytes()
            with patch.dict(os.environ, {"OFSAFAERNI_SHARED_TEST": str(root)}):
                self.assertEqual(
                    hermes_skill_roots(profile),
                    [profile / "skills", profile / "created", root / "notes"],
                )
            self.assertEqual(config.read_bytes(), original)
            self.assertFalse((profile / "skills").exists())

    def test_hermes_environment_selects_its_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profile = base / "custom"
            with patch.dict(os.environ, {"HERMES_HOME": str(profile)}):
                self.assertEqual(hermes_skill_roots(), [profile / "skills"])

    def test_sticky_profile_and_explicit_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profile = base / "profiles" / "research"
            profile.mkdir(parents=True)
            marker = base / "active_profile"
            marker.write_text("research\n", encoding="utf-8-sig")
            with patch.dict(os.environ, {"HERMES_HOME": str(base)}):
                self.assertEqual(hermes_skill_roots(), [profile / "skills"])
                marker.write_text("default", encoding="utf-8")
                self.assertEqual(hermes_skill_roots(), [base / "skills"])
            with patch.dict(os.environ, {"HERMES_HOME": str(profile)}):
                self.assertEqual(hermes_skill_roots(), [profile / "skills"])
            self.assertEqual(hermes_skill_roots(base), [base / "skills"])

    def test_invalid_or_missing_sticky_profile_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            marker = base / "active_profile"
            with patch.dict(os.environ, {"HERMES_HOME": str(base)}):
                marker.write_text("../unrelated", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "invalid profile name"):
                    hermes_skill_roots()
                marker.write_text("missing", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "does not exist"):
                    hermes_skill_roots()

    def test_native_default_matches_hermes_platform_convention(self):
        with patch.dict(os.environ, {"HERMES_DATA_DIR_SUFFIX": "-ofsafaerni-test"}):
            if sys.platform == "win32":
                local = os.environ.get("LOCALAPPDATA", "").strip()
                parent = Path(local) if local else Path.home() / "AppData" / "Local"
                self.assertEqual(_native_home(), parent / "hermes-ofsafaerni-test")
            else:
                self.assertEqual(_native_home(), Path.home() / ".hermes-ofsafaerni-test")

    def test_invalid_skill_roots_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / "config.yaml").write_text("skills:\n  external_dirs: secret\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "external_dirs"):
                hermes_skill_roots(profile)


class ExportSkillTests(unittest.TestCase):
    def test_export_preserves_bundle_and_omits_local_learning_receipt(self):
        files = {
            "SKILL.md": b"---\nname: review-table\ndescription: Review a table\n---\n",
            "references/guide.md": "Gátlisti".encode(),
            "scripts/check.py": b"raise AssertionError('not for execution during import')\n",
            ".ofsafaerni-draft.json": b'{"evidence":"local only"}',
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = approved_record(files)
            result = export_skill(record, root)
            dest = Path(result["path"]).parent
            self.assertEqual((dest / "SKILL.md").read_bytes(), files["SKILL.md"])
            self.assertEqual((dest / "references" / "guide.md").read_bytes(), files["references/guide.md"])
            self.assertEqual((dest / "scripts" / "check.py").read_bytes(), files["scripts/check.py"])
            self.assertFalse((dest / ".ofsafaerni-draft.json").exists())
            self.assertEqual(result["source_sha256"], record["sha256"])
            self.assertNotEqual(result["sha256"], record["sha256"])
            with self.assertRaises(FileExistsError):
                export_skill(record, root)

    def test_unapproved_or_changed_bundles_leave_no_activated_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = approved_record({"SKILL.md": b"original"})
            with self.assertRaisesRegex(ValueError, "approved"):
                export_skill({**record, "approved": False}, root)
            record["files"]["SKILL.md"] = b"different"
            with self.assertRaisesRegex(ValueError, "digest"):
                export_skill(record, root)
            self.assertEqual(list(root.iterdir()), [])

    def test_nonportable_and_colliding_paths_are_rejected_before_writing(self):
        for unsafe in (
            {"../escaped.md": b"one"}, {"references\\escape.md": b"one"}, {"AUX.md": b"one"},
            {"notes.": b"one"}, {"notes/a": b"one", "Notes/b": b"two"},
            {"refs": b"one", "refs/child": b"two"}, {"/absolute.md": b"one"},
        ):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                record = approved_record({"SKILL.md": b"normal", **unsafe})
                with self.assertRaisesRegex(ValueError, "nonportable|colliding"):
                    export_skill(record, root)
                self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
