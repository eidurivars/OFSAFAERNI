import json
import os
import subprocess
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from ofsafaerni.providers.gemini import iter_gemini


TIME = "2025-11-23T10:00:00Z"
TAKEOUT = "Takeout/My Activity/Gemini Apps/MyActivity.json"


def message(identifier, role, content, **extra):
    return {"id": identifier, "type": role, "content": content, "timestamp": TIME, **extra}


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class GeminiHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_legacy_session_keeps_user_text_and_normal_assistant(self):
        fixture = {
            "sessionId": "fixture",
            "projectHash": "project-1",
            "messages": [
                message("env", "user", [{"text": "<session_context>private context</session_context>"}]),
                message("intro", "gemini", "synthetic greeting"),
                message("hooks", "user", "<hook_context>synthetic hook output</hook_context>"),
                message(
                    "user-1", "user", [
                        {"text": "Ósk um úttekt @skrá"},
                        {"text": "--- Content from referenced files ---"},
                        {"text": "private contents"},
                        {"fileData": {"fileUri": "file:///private"}},
                    ],
                    displayContent="Ósk um úttekt",
                ),
                message("info", "info", "internal state"),
                message(
                    "model-1", "gemini", [
                        {"text": "Thought to discard", "thought": True},
                        {"text": "Svarið er tilbúið."},
                        {"functionResponse": {"name": "read_file", "response": {"contents": "private"}}},
                        {"inlineData": {"data": "private bytes"}},
                    ],
                    thoughts=[{"description": "remove"}], toolCalls=[{"result": "remove"}],
                ),
                message("user-2", "user", "/quit"),
                message("model-2", "gemini", "unrelated system reply"),
                message("user-3", "user", [
                    {"text": "Önnur beiðni\n--- Content from referenced files ---\nremove"}
                ]),
            ],
        }
        source = self.write("session-original.json", encode(fixture))
        rows = list(iter_gemini(source))
        self.assertEqual(rows, [
            {"thread_id": "fixture", "turn_id": "user-1", "at": "2025-11-23T10:00:00+00:00",
             "user": "Ósk um úttekt", "assistant": "Svarið er tilbúið.", "verified_checks": 0},
            {"thread_id": "fixture", "turn_id": "user-3", "at": "2025-11-23T10:00:00+00:00",
             "user": "Önnur beiðni", "assistant": "", "verified_checks": 0},
        ])

    def test_jsonl_replays_history_and_prefers_modern_session_from_profile(self):
        meta = {"sessionId": "fixture", "projectHash": "derived", "kind": "main"}
        records = [
            meta,
            message("old", "user", "Omitted by reset"),
            {"$set": {"messages": [message("user-1", "user", "Omitted by patch")]}},
            message("removed", "gemini", "Omitted by rewind"),
            {"$rewindTo": "removed"},
            message("model-1", "gemini", "Svar"),
            message("model-2", "gemini", "Antecedent"),
            message("model-3", "gemini", "Omitted by removeIds"),
            {"$patch": {
                "updates": [{"id": "user-1", "content": "Notaðu íslensku"}],
                "orderIds": ["model-1"],
                "removeIds": ["model-3"],
            }},
            {"$patch": {"id": "model-2", "content": "Fyrsta svar."}},
            {"$set": {"lastUpdated": TIME}},
        ]
        self.write("tmp/project/chats/session-now.jsonl", "\n".join(map(encode, records)) + "\n{")
        self.write("tmp/project/chats/session-migrated.json", encode({
            **meta, "messages": [message("old", "user", "stale JSON")],
        }))
        self.write("tmp/project/chats/session-agent.json", encode({
            **meta, "sessionId": "agent", "kind": "subagent",
            "messages": [message("agent-user", "user", "internal delegation")],
        }))
        self.write("tmp/project/subagents/session-hidden.json", encode({
            **meta, "sessionId": "hidden", "messages": [message("hidden", "user", "hidden")],
        }))
        rows = list(iter_gemini(self.root))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user"], "Notaðu íslensku")
        self.assertEqual(rows[0]["assistant"], "Fyrsta svar.\n\nSvar")

    def test_unknown_rewind_clears_session(self):
        source = self.write("session-clear.jsonl", "\n".join(map(encode, [
            {"sessionId": "fixture"},
            message("user-1", "user", "Previous branch"),
            {"$rewindTo": "not-present"},
            message("user-2", "user", "Current branch"),
        ])))
        self.assertEqual([row["user"] for row in iter_gemini(source)], ["Current branch"])

    def test_app_takeout_filters_non_prompt_and_does_not_import_activity_details(self):
        fixture = encode([
            {"header": "Gemini Apps", "time": TIME, "title": "Prompted Notaðu stuttar setningar.",
             "safeHtmlItem": [{"html": "an answer or other content"}]},
            {"products": ["Gemini"], "time": TIME, "title": "Prompted Komdu alltaf með heimildir."},
            {"header": "Search", "time": TIME, "title": "Prompted wrong product"},
            {"header": "Gemini Apps", "time": TIME, "title": "Visited a page"},
        ])
        path = self.write("MyActivity.json", fixture)
        direct = list(iter_gemini(path))
        with zipfile.ZipFile(self.root / "takeout.zip", "w") as archive:
            archive.writestr(TAKEOUT, fixture)
            archive.writestr("Takeout/My Activity/Search/MyActivity.json", "{invalid}")
        self.assertEqual(direct, list(iter_gemini(self.root / "takeout.zip")))
        self.assertEqual(
            [row["user"] for row in direct], ["Notaðu stuttar setningar.", "Komdu alltaf með heimildir."],
        )
        self.assertTrue(all(row["assistant"] == "" for row in direct))
        self.assertNotEqual(direct[0]["thread_id"], direct[1]["thread_id"])

    def test_rejects_duplicate_takeout_member_and_complete_invalid_record(self):
        path = self.root / "takeout.zip"
        with zipfile.ZipFile(path, "w") as archive, warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr(TAKEOUT, "[]")
            archive.writestr(TAKEOUT, "[]")
        with self.assertRaisesRegex(ValueError, "Expected one"):
            list(iter_gemini(path))
        source = self.write("session-broken.jsonl", '{"sessionId":"fixture"}\n{ invalid }\n')
        with self.assertRaisesRegex(ValueError, "Invalid Gemini session record"):
            list(iter_gemini(source))

    def test_session_does_not_follow_symbolic_links(self):
        path = self.write("outside/export.json", encode({
            "sessionId": "fixture", "messages": [message("user-1", "user", "Outside")],
        }))
        chats = self.root / "chats"
        chats.mkdir()
        link = chats / "session-linked.json"
        try:
            link.symlink_to(path)
        except OSError:
            if os.name != "nt":
                self.skipTest("This host does not allow creating symbolic links")
            # Windows can ordinarily create a junction without symlink privileges.
            target = path.parent
            path.rename(target / "session-linked.json")
            link = self.root / "project" / "chats"
            link.parent.mkdir()
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, check=False,
            )
            if result.returncode:
                self.skipTest("This host does not allow creating links or junctions")
            self.addCleanup(link.rmdir)
            self.assertEqual(list(iter_gemini(link.parent)), [])
            with self.assertRaisesRegex(ValueError, "ordinary directory"):
                list(iter_gemini(link / "session-linked.json"))
            return
        self.assertEqual(list(iter_gemini(chats)), [])
        with self.assertRaisesRegex(ValueError, "ordinary Gemini"):
            list(iter_gemini(link))


if __name__ == "__main__":
    unittest.main()
