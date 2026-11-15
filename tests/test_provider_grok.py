import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ofsafaerni.providers.grok import _Reader, _turns, iter_grok


def response(sender, message, **fields):
    return {"response": {"sender": sender, "message": message, **fields}}


def conversation(identifier, *responses):
    return {"conversation": {"id": identifier}, "responses": list(responses)}


class GrokExportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_explicit_native_json_is_ordinary_utf8(self):
        source = self.folder / "prod-grok-backend.json"
        records = {
            "profile": {"metadata": ['escaped quote with structural characters: \\"[]{}']},
            "conversations": [
                conversation(
                    "thread-one",
                    response("human", "Notaðu íslensku.", _id={"$oid": "message-one"},
                             create_time={"$date": {"$numberLong": "1761955200000"}}),
                    response("tool", "tool output", parent_response_id="message-one"),
                    response("grok-3", "Ég nota íslensku.", parent_response_id="message-one",
                             thinking_trace="ignore private reasoning"),
                    response("human", "hidden text", hidden=True),
                    response("system", "system text"),
                ),
                conversation(
                    "thread-two",
                    response("HUMAN", "Sýndu næsta skref.", _id="message-two",
                             create_time={"$date": "2025-11-02T10:15:00Z"}),
                    response("ASSISTANT", "Næsta skref.", parent_response_id="message-two"),
                ),
            ],
            "bookmarks": [1],
        }
        source.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8-sig")
        self.assertEqual(list(iter_grok(source)), [
            {"thread_id": "thread-one", "turn_id": "message-one", "at": "2025-11-01T00:00:00+00:00",
             "user": "Notaðu íslensku.", "assistant": "Ég nota íslensku.", "verified_checks": 0},
            {"thread_id": "thread-two", "turn_id": "message-two", "at": "2025-11-02T10:15:00+00:00",
             "user": "Sýndu næsta skref.", "assistant": "Næsta skref.", "verified_checks": 0},
        ])

    def test_branch_candidates_are_not_guessed(self):
        records = {"conversations": [conversation(
            "thread-one",
            response("human", "Choose the next step.", _id="message-one"),
            response("grok-3", "First branch", parent_response_id="message-one"),
            response("grok-4", "Second branch", parent_response_id="message-one"),
            response("human", "Try the next one.", _id="message-two", parent_response_id="another-id"),
            response("grok-4", "Retry failure", parent_response_id="message-two", error="failed"),
        )]}
        self.assertEqual([turn["assistant"] for turn in _turns(io.StringIO(json.dumps(records)))], ["", ""])

    def test_read_vendor_zip_member_only_with_order_fallback(self):
        source = self.folder / "account.zip"
        records = {"conversations": [conversation(
            "thread-one",
            response("grok-3", "An answer with no matching question."),
            response("Human", "Fyrri spurning."),
            response("unknown", "Unrecognized sender."),
            response("Grok", "Fyrra svar."),
            response("human", "Seinni spurning."),
            response("grok-3-mini", "Seinna svar."),
        )]}
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ttl/30d/export_data/random-id/prod-grok-backend.json", json.dumps(records))
            archive.writestr("metadata/prod-grok-backend.json", "{broken}")
            archive.writestr("ttl/30d/export_data/random-id/prod-account.json", "{private}")
        turns = list(iter_grok(source))
        self.assertEqual([(turn["user"], turn["assistant"]) for turn in turns], [
            ("Fyrri spurning.", "Fyrra svar."), ("Seinni spurning.", "Seinna svar."),
        ])
        self.assertEqual(len({turn["turn_id"] for turn in turns}), 2)
        self.assertEqual(turns, list(iter_grok(source)))

    def test_excluded_human_turn_cannot_attach_an_answer_to_earlier_turn(self):
        records = {"conversations": [conversation(
            "thread-one",
            response("Human", "Fyrri spurning."),
            response("grok-tool", "Internal tool text."),
            response("human", "Deleted question.", is_deleted=True),
            response("assistant", "Answer to the deleted question."),
        )]}
        turns = list(_turns(io.StringIO(json.dumps(records))))
        self.assertEqual([(turn["user"], turn["assistant"]) for turn in turns], [("Fyrri spurning.", "")])

    def test_values_stream_across_chunk_boundaries(self):
        class ChunkedStream(io.StringIO):
            def read(self, size):
                return super().read(min(size, 4))

        records = {"count": 123456.78, "conversations": [conversation(
            "thread-one",
            response("human", "Komdu með nýtt dæmi."),
            response("assistant", "Hér kemur það."),
        )]}
        turns = list(_turns(ChunkedStream(json.dumps(records, ensure_ascii=False))))
        self.assertEqual(turns[0]["assistant"], "Hér kemur það.")

    def test_fallback_ids_survive_overlapping_dated_exports(self):
        first = response("human", "A dropped earlier turn.", create_time="2025-11-01T00:00:00Z")
        retained = response("human", "A retained turn.", create_time="2025-11-02T00:00:00Z")

        def identifiers(*turns):
            records = {"conversations": [conversation("thread-one", *turns)]}
            return [turn["turn_id"] for turn in _turns(io.StringIO(json.dumps(records)))]

        initial = identifiers(first, retained, retained)
        self.assertEqual(initial[1:], identifiers(retained, retained))
        self.assertEqual(len(set(initial)), 3)

    def test_reject_ambiguous_traversal_or_link_members(self):
        source = self.folder / "account.zip"
        missing = {"../prod-grok-backend.json": '{"conversations":[]}'}
        ambiguous = {"prod-grok-backend.json": "{}", "export_data/id/prod-grok-backend.json": "{}"}
        for members in (missing, ambiguous):
            with self.subTest(members=list(members)), zipfile.ZipFile(source, "w") as archive:
                for name, entry in members.items():
                    archive.writestr(name, entry)
            with self.assertRaisesRegex(ValueError, "exactly one"):
                list(iter_grok(source))
        link = zipfile.ZipInfo("prod-grok-backend.json")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(link, "target.json")
        with self.assertRaisesRegex(ValueError, "regular, readable"):
            list(iter_grok(source))

    def test_reject_malformed_export_and_bounded_reads(self):
        for content in ('{}', '[]', '{"conversations":[', '{"conversations":[1]}',
                        '{"conversations":[],"conversations":[]}', '{"conversations":[]} trailing'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                list(_turns(io.StringIO(content)))
        with self.assertRaisesRegex(ValueError, "size limit"):
            _Reader(io.StringIO('{"text":"0123456789"}')).value(limit=10)
        source = self.folder / "prod-grok-backend.json"
        source.write_text('{"conversations":[]}', encoding="utf-8")
        with patch("ofsafaerni.providers.grok._MAX_EXPORT_SOURCE", 8):
            with self.assertRaisesRegex(ValueError, "ordinary Grok export"):
                list(iter_grok(source))
