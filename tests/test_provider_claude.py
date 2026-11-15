import json
from pathlib import Path
import stat
import tempfile
import unittest
import warnings
import zipfile

from ofsafaerni.providers.claude import iter_claude


SESSION = "019db112-6f77-7e00-906a-701607e530a1"


def record(role, identity, content, *, parent=None, **fields):
    stop = fields.pop("stop", None)
    return {
        "type": role,
        "uuid": identity,
        "sessionId": SESSION,
        "timestamp": "2026-10-19T13:15:00Z",
        "parentUuid": parent,
        "message": {"role": role, "content": content, "stop_reason": stop},
        **fields,
    }


class ClaudeProviderTests(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.root = Path(area.name)

    def write(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def test_profile_discovery_skips_workers_and_tool_payloads(self):
        project = self.root / "projects" / "project-name"
        self.write(project / f"{SESSION}.jsonl", [
            record("user", "user-01", [{"type": "text", "text": "Notaðu staðfestar heimildir."},
                   {"type": "image", "text": "Image payload"}]),
            record("assistant", "assistant-01", "Partial response", parent="user-01"),
            record("user", "tool-result", [
                {"type": "text", "text": "Not user authored"},
                {"type": "tool_result", "content": "Tool output"},
            ], parent="assistant-01"),
            record("assistant", "assistant-02", [
                {"type": "thinking", "text": "Internal thinking"},
                {"type": "text", "text": "Sýnilegt svar."},
            ], parent="tool-result", stop="end_turn"),
            record("user", "context", "<system-reminder>Injected</system-reminder>"),
            record("user", "meta", "Injected role", isMeta=True),
            record("user", "channel", "External channel", origin={"kind": "channel"}),
        ])
        self.write(project / SESSION / "subagents" / f"{SESSION}.jsonl", [
            record("user", "worker", "Do not retrieve workers."),
        ])
        self.write(self.root / f"{SESSION}.jsonl", [
            record("user", "outside", "Outside selected projects."),
        ])
        self.write(self.root / "credentials.jsonl", [{"token": "SYNTHETIC-PLACEHOLDER"}])
        rows = list(iter_claude(self.root))
        self.assertEqual(rows, [{
            "thread_id": SESSION, "turn_id": "user-01", "at": "2026-10-19T13:15:00+00:00",
            "user": "Notaðu staðfestar heimildir.", "assistant": "Sýnilegt svar.", "verified_checks": 0,
        }])

    def test_answers_attach_to_their_own_ancestry(self):
        file = self.write(self.root / "main.jsonl", [
            record("user", "root-1", "Fyrri beiðni."),
            record("user", "root-2", "Seinni beiðni."),
            record("assistant", "old-reply", "Answer to first prompt.", parent="root-1", stop="end_turn"),
            record("assistant", "unrelated", "Unknown answer.", parent="unknown", stop="end_turn"),
            record("assistant", "incomplete", "Aborted answer.", parent="root-2", stop="max_tokens"),
        ])
        rows = list(iter_claude(file))
        self.assertEqual([(row["turn_id"], row["assistant"]) for row in rows], [
            ("root-1", "Answer to first prompt."), ("root-2", ""),
        ])

    def test_direct_subagent_and_sidechain_are_not_supported(self):
        direct = self.write(self.root / "agent-one.jsonl", [
            record("user", "worker", "Worker text."),
        ])
        with self.assertRaises(ValueError):
            list(iter_claude(direct))
        file = self.write(self.root / f"{SESSION}.jsonl", [
            record("user", "worker", "Worker text.", isSidechain=True),
            record("user", "another", "Worker text without sidechain flag.", agentId="agent"),
        ])
        self.assertEqual(list(iter_claude(file)), [])

    @staticmethod
    def export_data():
        return [{
            "uuid": "conversation-1",
            "chat_messages": [
                {"uuid": "human-1", "sender": "human", "text": "Use visible text.",
                 "created_at": "2026-10-19T13:15:00Z", "attachments": [{"text": "Ignore attachments"}]},
                {"uuid": "assistant-1", "sender": "assistant", "text": "Do not copy the legacy duplicate.",
                 "content": [{"type": "tool_result", "text": "Ignore tools"},
                             {"type": "text", "text": "Visible answer."}]},
                {"uuid": "system-1", "sender": "system", "text": "Ignore systems."},
                {"uuid": "human-2", "sender": "human", "content": [{"type": "text", "text": "Þetta er dæmi."}]},
                {"uuid": "assistant-2", "sender": "assistant", "text": "Aborted answer.", "status": "failed"},
            ],
        }]

    def test_consumer_export_from_json_and_root_zip_member(self):
        file = self.root / "claude.json"
        file.write_text(json.dumps(self.export_data(), ensure_ascii=False), encoding="utf-8")
        archive = self.root / "export.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("conversations.json", file.read_bytes())
            package.writestr("users.json", "Unused provider data")
            package.writestr("../../unused", "Not extracted")
        direct = list(iter_claude(file))
        self.assertEqual(direct, list(iter_claude(archive)))
        self.assertEqual([(row["user"], row["assistant"], row["verified_checks"]) for row in direct], [
            ("Use visible text.", "Visible answer.", 0), ("Þetta er dæmi.", "", 0),
        ])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["claude.json", "export.zip"])

    def test_zip_rejects_ambiguous_and_linked_json(self):
        archive = self.root / "export.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("conversations.json", "[]")
                package.writestr("conversations.json", "[]")
        with self.assertRaisesRegex(ValueError, "one conversations.json"):
            list(iter_claude(archive))
        member = zipfile.ZipInfo("conversations.json")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(member, "../unused.json")
        with self.assertRaisesRegex(ValueError, "regular"):
            list(iter_claude(archive))

    def test_unfamiliar_export_is_reported_without_file_contents(self):
        file = self.root / "claude.json"
        file.write_text('[{"other": "Private example content"}]', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "conversation 1 is not in the supported format") as error:
            list(iter_claude(file))
        self.assertNotIn("Private example content", str(error.exception))


if __name__ == "__main__":
    unittest.main()
