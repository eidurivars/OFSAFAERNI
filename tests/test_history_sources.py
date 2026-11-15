import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ofsafaerni.history_sources import _MAX_LINE, _MAX_TEXT, coverage, iter_chatgpt_export, iter_codex_turns


class HistorySourcesTests(unittest.TestCase):
    def setUp(self):
        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        self.home = Path(private.name)

    def build_projection(self):
        history = sqlite3.connect(self.home / "thread_history_1.sqlite")
        state = sqlite3.connect(self.home / "state_5.sqlite")
        self.addCleanup(history.close)
        self.addCleanup(state.close)
        history.executescript("""
            CREATE TABLE thread_turns(
                thread_id TEXT, turn_id TEXT, started_at INTEGER, status TEXT,
                final_agent_item_id TEXT, rollout_ordinal INTEGER
            );
            CREATE TABLE thread_items(
                thread_id TEXT, turn_id TEXT, item_id TEXT, item_type TEXT,
                item_json TEXT, created_at_ms INTEGER, rollout_ordinal INTEGER
            );
        """)
        state.executescript("""
            CREATE TABLE threads(id TEXT, source TEXT, archived INTEGER);
            CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT);
        """)
        return history, state

    def item(self, history, thread, turn, kind, content, ordinal, identifier=None):
        identifier = identifier or f"item-{ordinal}"
        history.execute("INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?)", (
            thread, turn, identifier, kind,
            json.dumps({"id": identifier, "type": kind, **content}, ensure_ascii=False),
            1_700_000_000_000, ordinal,
        ))

    def test_projected_keep_root_user_final_and_successful_checks(self):
        history, state = self.build_projection()
        state.executemany("INSERT INTO threads VALUES (?, ?, ?)", [
            ("parent", "vscode", 0), ("child", "vscode", 0),
            ("another-child", '{"subagent": {"thread_spawn": {}}}', 0),
            ("archive", "cli", 1),
        ])
        state.execute("INSERT INTO thread_spawn_edges VALUES ('parent', 'child')")
        history.executemany("INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?)", [
            ("parent", "turn-core", 1_700_000_000, "completed", "final", 1),
            ("parent", "turn-open", 1_700_000_100, "inProgress", "hint", 2),
            ("child", "turn-child", 1_700_000_000, "completed", None, 1),
            ("another-child", "turn-extra", 1_700_000_000, "completed", None, 1),
            ("archive", "turn-archive", 1_700_000_000, "completed", None, 1),
        ])
        self.item(history, "parent", "turn-core", "userMessage", {
            "content": [{"type": "text", "text": "Beiðni"}, {"type": "image", "text": "ignored"}],
        }, 1)
        self.item(history, "parent", "turn-core", "userMessage", {
            "content": [{"type": "text", "text": "Viðbót"}],
        }, 2)
        self.item(history, "parent", "turn-core", "agentMessage", {
            "phase": "commentary", "text": "internal progress",
        }, 3)
        self.item(history, "parent", "turn-core", "commandExecution", {
            "status": "completed", "exitCode": 0, "command": "python -m unittest",
            "aggregatedOutput": "private output never part of result",
        }, 4)
        self.item(history, "parent", "turn-core", "commandExecution", {
            "status": "completed", "exitCode": 1, "command": "pytest",
        }, 5)
        self.item(history, "parent", "turn-core", "commandExecution", {
            "status": "completed", "exitCode": 0, "command": "python script.py",
        }, 6)
        self.item(history, "parent", "turn-core", "commandExecution", {
            "status": "completed", "exitCode": False, "command": "pytest",
        }, 7)
        self.item(history, "parent", "turn-core", "commandExecution", {
            "status": "completed", "exitCode": 0, "command": "echo pytest",
        }, 21)
        self.item(history, "parent", "turn-core", "agentMessage", {
            "phase": "final_answer", "text": "Staðfest niðurstaða",
        }, 8, "final")
        self.item(history, "parent", "turn-open", "userMessage", {
            "content": [{"type": "text", "text": "Ný leiðrétting"}],
        }, 9)
        self.item(history, "parent", "turn-open", "agentMessage", {
            "phase": "final_answer", "text": "Óstaðfest svar",
        }, 10, "hint")
        for ix, (tid, turn) in enumerate((
            ("child", "turn-child"), ("another-child", "turn-extra"), ("archive", "turn-archive"),
        ), start=11):
            self.item(history, tid, turn, "userMessage", {"content": [{"type": "text", "text": tid}]}, ix)
        history.commit()
        state.commit()

        result = list(iter_codex_turns(self.home))
        self.assertEqual([r["turn_id"] for r in result], ["turn-archive", "turn-core", "turn-open"])
        self.assertEqual(result[1], {
            "thread_id": "parent",
            "turn_id": "turn-core",
            "at": "2023-11-14T22:13:20+00:00",
            "user": "Beiðni\n\nViðbót",
            "assistant": "Staðfest niðurstaða",
            "verified_checks": 1,
        })
        self.assertEqual(result[2]["assistant"], "")
        self.assertEqual(len(list(iter_codex_turns(self.home, thread_id="archive"))), 1)
        self.assertEqual(list(iter_codex_turns(self.home, thread_id="child")), [])
        info = coverage(self.home)
        self.assertEqual(info["top_level_threads"], 2)
        self.assertEqual(info["archived_top_level_threads"], 1)
        self.assertEqual(info["projected_top_level_turns"], 3)

    def rollout(self, identity, source="vscode", parent=None, when="sessions"):
        root = self.home / when
        root.mkdir(exist_ok=True)
        path = root / f"rollout-2026-11-15T00-00-00-{identity}.jsonl"
        payloads = [
            {"type": "session_meta", "payload": {"id": identity, "source": source, "parent_thread_id": parent}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "start"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Fyrsta inntak"}]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Fyrsta inntak"}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
                "content": [{"type": "input_text", "text": "Not the user"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary",
                "content": [{"type": "output_text", "text": "Not a final answer"}]}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "start",
                "last_agent_message": "Lokið"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "later"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Ólokin beiðni"}},
        ]
        with path.open("wb") as f:
            for ix, item in enumerate(payloads):
                if ix == 3:
                    f.write(b"x" * (_MAX_LINE * 2) + b"\n")
                f.write((json.dumps({"timestamp": "2026-11-15T00:00:00Z", **item}) + "\n").encode())
        return path

    def test_legacy_root_and_archive_fallback(self):
        identity = "11111111-1111-1111-1111-111111111111"
        self.rollout(identity, when="archived_sessions")
        self.rollout("22222222-2222-2222-2222-222222222222", parent=identity)
        self.rollout("33333333-3333-3333-3333-333333333333", {"subagent": "review"})
        self.rollout("44444444-4444-4444-4444-444444444444", "unrecognized")
        result = list(iter_codex_turns(self.home))
        self.assertEqual([r["thread_id"] for r in result], [identity, identity])
        self.assertEqual(result[0]["user"], "Fyrsta inntak")
        self.assertEqual(result[0]["assistant"], "Lokið")
        self.assertEqual(result[1]["user"], "Ólokin beiðni")
        self.assertEqual(result[1]["assistant"], "")
        self.assertEqual(coverage(self.home)["archive_rollouts"], 1)
        self.assertEqual(coverage(self.home)["session_rollouts"], 3)
        self.assertEqual(len(list(iter_codex_turns(self.home, thread_id=identity))), 2)
        with self.assertRaises(ValueError):
            list(iter_codex_turns(self.home, thread_id="../a"))

    def test_retained_rollout_fills_only_distinct_unprojected_turns(self):
        identity = "11111111-1111-1111-1111-111111111111"
        self.rollout(identity)
        history, state = self.build_projection()
        state.execute("INSERT INTO threads VALUES (?, 'vscode', 0)", (identity,))
        history.execute("INSERT INTO thread_turns VALUES (?, 'now', 1, 'completed', NULL, 1)", (identity,))
        self.item(history, identity, "now", "userMessage", {
            "content": [{"type": "text", "text": "upphaf " + "x" * _MAX_TEXT + " endir"}],
        }, 2)
        history.execute(
            "INSERT INTO thread_turns VALUES (?, 'start', 2, 'completed', 'projected-final', 3)",
            (identity,),
        )
        self.item(history, identity, "start", "userMessage", {
            "content": [{"type": "text", "text": "Sama innsláttarfærsla úr vörpun"}],
        }, 4)
        self.item(history, identity, "start", "agentMessage", {
            "phase": "final_answer", "text": "Varpað lokasvar",
        }, 5, "projected-final")
        history.commit()
        state.commit()
        result = list(iter_codex_turns(self.home))
        self.assertCountEqual([row["turn_id"] for row in result], ["now", "start", "later"])
        turns = {row["turn_id"]: row for row in result}
        self.assertTrue(turns["now"]["user"].startswith("upphaf"))
        self.assertTrue(turns["now"]["user"].endswith("endir"))
        self.assertLessEqual(len(turns["now"]["user"]), _MAX_TEXT)
        self.assertEqual(turns["start"]["assistant"], "Varpað lokasvar")
        self.assertEqual(turns["later"]["user"], "Ólokin beiðni")
        self.assertEqual(coverage(self.home)["unprojected_rollout_candidates"], 0)
        self.assertEqual(coverage(self.home)["top_level_rollout_candidates"], 1)

    def test_targeted_following_starts_at_live_turn_and_merges_missing_final(self):
        identity = "11111111-1111-1111-1111-111111111111"
        path = self.rollout(identity)
        def event(name, payload):
            return (
                json.dumps({"timestamp": "2026-11-15T00:00:00Z", "type": name, "payload": payload}) + "\n"
            ).encode()
        with path.open("ab") as stream:
            start = stream.tell()
            stream.write(event("event_msg", {"type": "task_started", "turn_id": "inflight"}))
            stream.write(event("event_msg", {"type": "user_message", "message": "Ný beiðni"}))
            cursor = stream.tell()
            stream.write(event("event_msg", {
                "type": "task_complete", "turn_id": "inflight", "last_agent_message": "Ný niðurstaða",
            }))
            stream.write(event("event_msg", {"type": "task_started", "turn_id": "tail"}))
            stream.write(event("event_msg", {"type": "user_message", "message": "Næsta beiðni"}))
        history, state = self.build_projection()
        state.execute("INSERT INTO threads VALUES (?, 'vscode', 0)", (identity,))
        history.execute("ALTER TABLE thread_turns ADD COLUMN rollout_byte_offset INTEGER")
        history.execute("CREATE TABLE thread_history_projection_state(thread_id TEXT, next_rollout_byte_offset INTEGER)")
        history.execute("INSERT INTO thread_history_projection_state VALUES (?, ?)", (identity, cursor))
        history.execute(
            "INSERT INTO thread_turns VALUES (?, 'inflight', 1, 'inProgress', NULL, 1, ?)",
            (identity, start),
        )
        self.item(history, identity, "inflight", "userMessage", {
            "content": [{"type": "text", "text": "Ný beiðni"}],
        }, 2)
        history.commit()
        state.commit()
        recent = list(iter_codex_turns(self.home, thread_id=identity))
        self.assertEqual([row["turn_id"] for row in recent], ["inflight", "tail"])
        self.assertEqual(recent[0]["assistant"], "Ný niðurstaða")
        self.assertEqual(recent[1]["assistant"], "")
        self.assertCountEqual(
            [row["turn_id"] for row in iter_codex_turns(self.home)],
            ["start", "later", "inflight", "tail"],
        )

    @staticmethod
    def chatgpt_record():
        def message(identity, role, parts, **kw):
            return {
                "id": identity, "author": {"role": role}, "create_time": 1_700_000_000.25,
                "content": {"content_type": "multimodal_text", "parts": parts}, **kw,
            }
        return {
            "title": "Excluded title",
            "conversation_id": "conversation-42",
            "current_node": "pending",
            "mapping": {
                "root": {"parent": None, "message": None},
                "user-1": {"parent": "root", "message": message("message-1", "user", ["Leiðrétting",
                    {"type": "asset_pointer", "asset_pointer": "not text"}])},
                "discarded": {"parent": "user-1", "message": message("message-2", "assistant",
                    ["Prior inactive branch"])},
                "thought": {"parent": "user-1", "message": message("message-3", "assistant",
                    ["Tool request"], recipient="some_tool", channel="commentary")},
                "hidden": {"parent": "thought", "message": message("message-4", "user",
                    ["Hidden synthetic user"], metadata={"is_visually_hidden_from_conversation": True})},
                "system": {"parent": "hidden", "message": message("message-5", "system",
                    ["System instructions"])},
                "assistant": {"parent": "system", "message": message("message-6", "assistant",
                    ["Niðurstaða"], status="finished_successfully", end_turn=True, channel="final")},
                "user-2": {"parent": "assistant", "message": message("message-7", "user",
                    ["Ókláruð beiðni"])},
                "pending": {"parent": "user-2", "message": message("message-8", "assistant",
                    ["Work in progress"], status="in_progress")},
            },
        }

    def test_chatgpt_export_reads_only_the_active_visible_chat(self):
        path = self.home / "conversations.json"
        path.write_text(json.dumps([self.chatgpt_record()], ensure_ascii=False), encoding="utf-8-sig")
        rows = list(iter_chatgpt_export(path))
        self.assertEqual(rows, [
            {"thread_id": "conversation-42", "turn_id": "message-1", "user": "Leiðrétting",
                "assistant": "Niðurstaða", "at": "2023-11-14T22:13:20.250000+00:00",
                "verified_checks": 0},
            {"thread_id": "conversation-42", "turn_id": "message-7", "user": "Ókláruð beiðni",
                "assistant": "", "at": "2023-11-14T22:13:20.250000+00:00", "verified_checks": 0},
        ])

    def test_chatgpt_zip_reads_only_standard_json_members(self):
        path = self.home / "ChatGPT Export.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("../../ignored.txt", "unused file")
            archive.writestr("user.json", '{"email":"unused field"}')
            archive.writestr("conversations-001.json", json.dumps([self.chatgpt_record()]))
            archive.writestr("conversations-002.json", "[]")
        self.assertEqual(len(list(iter_chatgpt_export(path))), 2)
        self.assertEqual(list(self.home.iterdir()), [path])
        unsafe = self.home / "not-export.zip"
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("../conversations.json", json.dumps([self.chatgpt_record()]))
        with self.assertRaisesRegex(ValueError, "conversations JSON in the ZIP root"):
            list(iter_chatgpt_export(unsafe))

    def test_chatgpt_export_reports_malformed_or_oversized_records(self):
        path = self.home / "conversations.json"
        cases = (
            ('[{"id": "abc"', "malformed conversation 1"),
            ('[{"id": "abc", "id": "def"}]', "malformed conversation 1"),
            ('[42]', "conversation 1 is missing or not an object"),
            ('[]trailing text', "data after the conversations array"),
            ('[{"id": "abc", "mapping": {}, "current_node": "bad"}]',
                "conversation 1: no valid current_node"),
            ('[{"id": "abc", "mapping": {"bad": {"parent":"bad"}}, "current_node": "bad"}]',
                "broken or cyclic active conversation branch"),
        )
        for malformed, expected in cases:
            with self.subTest(expected=expected):
                path.write_text(malformed, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected):
                    list(iter_chatgpt_export(path))
        path.write_text(json.dumps([self.chatgpt_record()]), encoding="utf-8")
        with patch("ofsafaerni.history_sources._MAX_EXPORT_RECORD", 300), patch(
            "ofsafaerni.history_sources._EXPORT_CHUNK", 64,
        ):
            with self.assertRaisesRegex(ValueError, "conversation 1 exceeds the record limit"):
                list(iter_chatgpt_export(path))


if __name__ == "__main__":
    unittest.main()
