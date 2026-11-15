import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ofsafaerni.history import _NOTES, ingest, lookup, scan
from ofsafaerni.history_start import prepare_config


def user(text, turn="one"):
    return {"thread_id": "fixture", "turn_id": turn, "at": "2025-02-01T00:00:00Z", "user": text}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        base = Path(workspace.name)
        self.config = {"vault": base / "vault", "state": base / "state.sqlite3"}
        self.config["vault"].mkdir()

    @property
    def notes(self):
        return self.config["vault"] / _NOTES

    def test_saves_useful_rules_and_retrieves_them_without_copying_raw_turn(self):
        rules = [
            user("Notaðu alltaf upprunalegar heimildir í rannsóknarskýrslum."),
            user("Geturðu reiknað þetta fyrir mig?", "transient"),
        ]
        result = ingest(self.config, rules, "codex")
        self.assertEqual(result["turns"], 2)
        self.assertEqual(result["lessons"]["preference"], 1, result)
        query = lookup(self.config, "Leitaðu að upprunalegum heimildum fyrir rannsóknarskýrslu.")
        self.assertEqual(len(query["lessons"]), 1, query)
        all_notes = "".join(path.read_text(encoding="utf-8") for path in self.notes.glob("*.md"))
        self.assertIn("upprunalegar heimildir", all_notes)
        self.assertNotIn("reiknað þetta", all_notes)

    def test_same_turn_is_idempotent_and_cloud_index_is_incremental(self):
        record = user("Always keep citations next to their claims in research reports.")
        first = ingest(self.config, [record], "chatgpt",
                       indexed=[{"id": "fixture", "updatedAt": "2025-03-01T00:00:00Z"}])
        second = ingest(self.config, [record, record], "chatgpt",
                        indexed=[{"id": "fixture", "updatedAt": "2025-02-01T00:00:00Z"}])
        self.assertEqual(first["new_turns"], 1)
        self.assertEqual(second["new_turns"], 0)
        self.assertEqual(second["lessons"], first["lessons"])
        with closing(sqlite3.connect(self.config["state"])) as con:
            self.assertTrue(con.execute("SELECT updated_at FROM chatgpt_index").fetchone()[0].startswith("2025-03"))

    def test_lesson_deduplication_preserves_product_punctuation(self):
        result = ingest(self.config, [
            user("Always use C for code examples.", "c"),
            user("Always use C++ for code examples.", "cpp"),
        ], "codex")
        self.assertEqual(result["lessons"]["total"], 2, result)

    def test_recognizes_reusable_review_and_summary_preferences(self):
        result = ingest(self.config, [
            user("Always retain page references in research summaries.", "retain"),
            user("Always list assumptions in architecture reviews.", "list"),
            user("Always compare reported inputs before trusting calculation results.", "compare"),
        ], "grok")
        self.assertEqual(result["lessons"]["total"], 3, result)

    def test_does_not_overwrite_handwritten_notes(self):
        self.notes.mkdir(parents=True)
        path = self.notes / "Óskir notanda.md"
        path.write_text("My existing Obsidian notes.", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "personal note"):
            ingest(self.config, [user("Always check source citations before publishing.")], "codex")
        self.assertEqual(path.read_text(encoding="utf-8"), "My existing Obsidian notes.")
        with closing(sqlite3.connect(self.config["state"])) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 0)

    def test_provider_selection_is_saved_and_scans_are_independent(self):
        base = self.config["state"].parent
        codex = base / "codex"
        codex.mkdir()
        export = base / "example.json"
        export.write_text("synthetic", encoding="utf-8")
        state = base / "settings"
        path = prepare_config(vault=self.config["vault"], codex_home=codex,
                              state_dir=state, providers={"claude": export})
        self.assertEqual(path, prepare_config(state_dir=state))
        with patch("ofsafaerni.history.reader_for", return_value=lambda _: [
            user("Always verify units in engineering reports.")
        ]) as get_reader:
            options = {**self.config, "codex_home": codex, "chatgpt_export": None,
                       "providers": {"claude": export, "grok": base / "missing.json"}}
            result = scan(options)["results"]
            again = scan(options)["results"]
        self.assertEqual(result[1]["source"], "claude")
        self.assertEqual(result[1]["new_occurrences"], 1)
        self.assertEqual(result[2]["source"], "grok")
        self.assertIn("error", result[2])
        self.assertTrue(again[1]["unchanged"])
        self.assertEqual(get_reader.call_count, 4)

    def test_non_codex_first_run_does_not_create_a_codex_profile(self):
        base = self.config["state"].parent
        missing = base / "no-codex"
        config = prepare_config(vault=self.config["vault"], codex_home=missing, state_dir=base / "fresh-state")
        self.assertTrue(config.is_file())
        self.assertFalse(missing.exists())

    def test_corrupt_codex_store_does_not_stop_other_providers(self):
        base = self.config["state"].parent
        export = base / "export.json"
        export.write_text("synthetic", encoding="utf-8")
        options = {**self.config, "codex_home": base, "chatgpt_export": None,
                   "providers": {"claude": export}}
        with patch("ofsafaerni.history.iter_codex_turns", side_effect=sqlite3.OperationalError("bad schema")):
            with patch("ofsafaerni.history.reader_for", return_value=lambda _: [
                user("Always verify source dates in engineering reports.")
            ]):
                outcomes = scan(options)["results"]
        self.assertEqual(outcomes[0]["source"], "codex")
        self.assertIn("error", outcomes[0])
        self.assertEqual(outcomes[1]["new_occurrences"], 1)


if __name__ == "__main__":
    unittest.main()
