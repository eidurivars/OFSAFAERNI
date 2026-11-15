"""Launch local history learning after a Codex Stop event."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO

from .sources import _is_link, local_root

_MAX_STOP_BYTES = 2 * 1024 * 1024
_MAX_CONFIG_BYTES = 16 * 1024
_DETACHED_PROCESS = 0x00000008
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE | re.ASCII)


def _absolute_local_path(value: object) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError("Expected a local path")
    path = Path(value)
    if not path.is_absolute() or value.startswith("//") or path.drive.startswith("\\") or ".." in path.parts:
        raise ValueError("Expected an absolute local path without parent traversal")
    return path


def _regular_file(path: Path) -> None:
    local_root(path.parent)
    info = path.lstat()
    if _is_link(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError("Expected an ordinary local file")


def _profile_root(config_path: Path) -> Path:
    _regular_file(config_path)
    with config_path.open("rb") as stream:
        raw = stream.read(_MAX_CONFIG_BYTES + 1)
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ValueError("History configuration is too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Expected history configuration")
    return local_root(_absolute_local_path(payload.get("codex_home")))


def _validate_transcript(value: object, root: Path, thread_id: str) -> None:
    if value is None:
        # A projected transcript may only exist in the local app-server store.
        # The scanner must independently confirm this exact thread is persisted.
        return
    path = _absolute_local_path(value)
    name = path.name.lower()
    if not name.startswith("rollout-") or not name.endswith(f"-{thread_id}.jsonl"):
        raise ValueError("The transcript must match the requested thread")
    if not any(path.is_relative_to(root / child) for child in ("sessions", "archived_sessions")):
        raise ValueError("Transcript is outside the configured profile")
    _regular_file(path)


def _launch_scan(config_path: Path, thread_id: str) -> None:
    args = [
        sys.executable, "-I", "-m", "ofsafaerni.history",
        "--config", str(config_path), "scan", "--thread-id", thread_id,
    ]
    launch = {"creationflags": _DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen(
        args,
        cwd=config_path.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **launch,
    )


def handle_stop(stream: BinaryIO, config_path: Path) -> dict:
    """Start one scoped scan without disclosing Stop input or blocking a turn."""
    try:
        raw = stream.read(_MAX_STOP_BYTES + 1)
        if len(raw) > _MAX_STOP_BYTES:
            return {}
        event = json.loads(raw)
        if not isinstance(event, dict) or event.get("hook_event_name") != "Stop":
            return {}
        thread_id, turn_id = event.get("session_id"), event.get("turn_id")
        if not isinstance(thread_id, str) or not _UUID.fullmatch(thread_id):
            return {}
        if not isinstance(turn_id, str) or not _UUID.fullmatch(turn_id):
            return {}
        if not isinstance(event.get("stop_hook_active"), bool) or "transcript_path" not in event:
            return {}
        config_path = _absolute_local_path(str(config_path))
        thread_id = thread_id.lower()
        _validate_transcript(event["transcript_path"], _profile_root(config_path), thread_id)
        _launch_scan(config_path, thread_id)
    except (OSError, ValueError, RecursionError):
        pass
    return {}
