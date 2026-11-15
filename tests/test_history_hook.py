import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ofsafaerni.history_hook import _MAX_STOP_BYTES, handle_stop


class HistoryStopHookTests(unittest.TestCase):
    thread_id = "019db112-6f77-7e00-906a-701607e530a1"
    turn_id = "019db112-6f77-7e00-906a-701607e53110"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.location = Path(directory.name).resolve()
        self.profile = self.location / "profile"
        self.profile.mkdir()
        self.config = self.location / "history.json"
        self.config.write_text(json.dumps({
            "codex_home": str(self.profile),
            "vault": str(self.location / "vault"),
            "state": str(self.location / "state"),
        }), encoding="utf-8")
        self.launch = self.enterContext(patch("ofsafaerni.history_hook.subprocess.Popen"))

    def event(self, **overrides):
        event = {
            "hook_event_name": "Stop",
            "session_id": self.thread_id,
            "turn_id": self.turn_id,
            "stop_hook_active": False,
            "transcript_path": None,
            "last_assistant_message": "synthetic response must not leave the hook",
        }
        event.update(overrides)
        return io.BytesIO(json.dumps(event).encode("utf-8"))

    def filename(self, identifier=None):
        return f"rollout-2026-01-01T00-00-00-{identifier or self.thread_id}.jsonl"

    def transcript(self, base, identifier=None):
        base.mkdir(parents=True, exist_ok=True)
        path = base / self.filename(identifier)
        path.write_text("{}\n", encoding="utf-8")
        return path

    def test_native_stop_starts_only_the_targeted_scanner(self):
        path = self.transcript(self.profile / "sessions" / "2026" / "01")
        self.assertEqual(handle_stop(self.event(transcript_path=str(path)), self.config), {})
        self.launch.assert_called_once()
        args, kwargs = self.launch.call_args
        self.assertEqual(args[0], [
            sys.executable, "-I", "-m", "ofsafaerni.history",
            "--config", str(self.config), "scan", "--thread-id", self.thread_id,
        ])
        self.assertEqual(kwargs["cwd"], self.config.parent)
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kwargs[stream], subprocess.DEVNULL)
        self.assertTrue(kwargs["close_fds"])
        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"], 0x00000008)
        else:
            self.assertTrue(kwargs["start_new_session"])

    def test_projected_stop_with_null_path_still_has_exact_scope(self):
        self.assertEqual(handle_stop(self.event(), self.config), {})
        self.launch.assert_called_once()
        self.assertEqual(self.launch.call_args.args[0][-2:], ["--thread-id", self.thread_id])

    def test_archive_is_allowed_and_mismatched_sources_are_refused(self):
        archived = self.transcript(self.profile / "archived_sessions")
        self.assertEqual(handle_stop(self.event(transcript_path=str(archived)), self.config), {})
        self.launch.assert_called_once()
        self.launch.reset_mock()
        outside = self.transcript(self.location / "another-profile" / "sessions")
        other_thread = self.transcript(self.profile / "sessions", self.turn_id)
        for path in (outside, other_thread, Path("sessions") / self.filename()):
            with self.subTest(path=path):
                self.assertEqual(handle_stop(self.event(transcript_path=str(path)), self.config), {})
        self.launch.assert_not_called()

    def test_transcript_links_are_not_opened(self):
        outside = self.transcript(self.location / "unrelated")
        root = self.profile / "sessions"
        root.mkdir()
        link = root / self.filename()
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic link creation is unavailable")
        self.assertEqual(handle_stop(self.event(transcript_path=str(link)), self.config), {})
        self.launch.assert_not_called()

    def test_reparse_transcript_is_refused_without_needing_symlink_privilege(self):
        path = self.transcript(self.profile / "sessions")
        lstat = Path.lstat

        def simulated_lstat(target):
            if target == path:
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            return lstat(target)

        with patch.object(Path, "lstat", simulated_lstat):
            self.assertEqual(handle_stop(self.event(transcript_path=str(path)), self.config), {})
        self.launch.assert_not_called()

    def test_invalid_hook_arguments_do_not_trigger_a_process(self):
        for kwargs in (
            {"hook_event_name": "SubagentStop"},
            {"session_id": "not-a-uuid"},
            {"turn_id": "not-a-uuid"},
            {"stop_hook_active": 1},
            {"transcript_path": 3},
            {"transcript_path": "\\\\Unrelated\\Folder\\rollout.jsonl"},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(handle_stop(self.event(**kwargs), self.config), {})
        self.assertEqual(handle_stop(io.BytesIO(b"{broken"), self.config), {})
        self.assertEqual(handle_stop(io.BytesIO(b" " * (_MAX_STOP_BYTES + 1)), self.config), {})
        self.launch.assert_not_called()

    def test_invalid_configuration_leaves_stop_silent(self):
        self.config.write_text('{"codex_home": "relative"}', encoding="utf-8")
        self.assertEqual(handle_stop(self.event(), self.config), {})
        self.launch.assert_not_called()

    def test_launch_failure_leaves_stop_silent(self):
        self.launch.side_effect = OSError("synthetic launch failure")
        self.assertEqual(handle_stop(self.event(), self.config), {})
        self.launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
