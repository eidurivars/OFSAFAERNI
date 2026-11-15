import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import yaml

from ofsafaerni.learning import draft_skill


SPEC = {
    "name": "review-table",
    "description": 'Yfirfar töflur: ber saman samtölur við "frumgögn".',
    "task": "Nota þegar birta á töflu með samtölum.",
    "evidence": "Tilbúin próftafla með tveimur villum. Báðar fundust.",
    "steps": ["Endurreikna dálkasummur út frá frumgögnum.", "Bera saman fjárhæðir og einingar."],
    "checks": ["Samtölur stemma og hver dálkur sýnir einingu."],
    "tags": ["töflur", "gæðarýni", "töflur"],
    "triggers": ["yfirfarðu töfluna"],
    "source": "synthetic-test",
}


class DraftSkillTests(unittest.TestCase):
    def test_icelandic_draft_keeps_evidence_in_local_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = draft_skill(SPEC, Path(tmp))
            path = Path(result["path"])
            self.assertEqual(path.parent.name, SPEC["name"])
            text = path.read_text(encoding="utf-8")
            frontmatter = yaml.safe_load(text.split("---\n", 2)[1])
            self.assertEqual(frontmatter["description"], SPEC["description"])
            self.assertEqual(frontmatter["metadata"], {"tags": "töflur, gæðarýni", "triggers": "yfirfarðu töfluna"})
            self.assertIn("## Sannprófun", text)
            self.assertIn(f"2. {SPEC['steps'][1]}", text)
            self.assertNotIn(SPEC["evidence"], text)
            receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
            self.assertEqual(receipt["evidence"], SPEC["evidence"])
            self.assertEqual(receipt["source"], SPEC["source"])
            self.assertEqual(receipt["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(result["sha256"], receipt["sha256"])
            self.assertIs(receipt["approved"], False)
            self.assertIs(result["approved"], False)

    def test_english_headings_and_yaml_keyword_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = draft_skill({**SPEC, "name": "null", "language": "en"}, Path(tmp))
            text = Path(result["path"]).read_text(encoding="utf-8")
            self.assertEqual(yaml.safe_load(text.split("---\n", 2)[1])["name"], "null")
            self.assertIn("## Verification", text)

    def test_invalid_input_never_creates_a_draft_path(self):
        for edits in (
            {"name": "../escape"}, {"name": "con"}, {"language": []}, {"checks": []},
            {"evidence": ""}, {"task": "one\ntwo"}, {"description": "<placeholder>"},
            {"steps": "not a list"}, {"tags": ["comma, tag"]},
        ):
            with self.subTest(edits=edits), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "drafts"
                with self.assertRaises(ValueError):
                    draft_skill({**SPEC, **edits}, root)
                self.assertFalse(root.exists())

    def test_duplicate_writers_cannot_replace_an_existing_draft(self):
        with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=4) as pool:
            root = Path(tmp) / "drafts"

            def write(index):
                try:
                    return draft_skill({**SPEC, "evidence": f"Check {index} passed."}, root)
                except FileExistsError:
                    return None

            results = [result for result in pool.map(write, range(4)) if result is not None]
            self.assertEqual(len(results), 1)
            result = results[0]
            original = Path(result["path"]).read_bytes()
            with self.assertRaises(FileExistsError):
                draft_skill({**SPEC, "description": "Changed description."}, root)
            self.assertEqual(Path(result["path"]).read_bytes(), original)

    def test_failed_publish_leaves_no_discoverable_or_reserved_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("ofsafaerni.learning.os.rename", side_effect=OSError("simulated rename error")):
                with self.assertRaisesRegex(OSError, "simulated rename error"):
                    draft_skill(SPEC, root)
            self.assertFalse((root / SPEC["name"]).exists())
            self.assertTrue(Path(draft_skill(SPEC, root)["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
