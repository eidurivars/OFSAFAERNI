"""Read an explicitly selected Claude export or local Claude Code transcripts."""

from __future__ import annotations

from collections.abc import Iterator
import io
import os
from pathlib import Path
import re
import stat
from typing import TextIO
import zipfile

from ..history_sources import (
    _MAX_EXPORT_ENTRIES,
    _MAX_EXPORT_SOURCE,
    _MAX_LINE,
    _decode,
    _iso,
    _join,
    _json_array,
    _ordinary,
)
from ..sources import local_root


_IDENTITY = re.compile(r"[a-zA-Z0-9_-]{1,128}")
_SESSION = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
_IGNORED_FOLDERS = {"subagents", "tool-results", "debug", "cache", "shell-snapshots"}
_FINAL_STOPS = {"end_turn", "stop_sequence"}
_MAX_DIRECTORY_ITEMS = 50_000
_MAX_SESSION_ITEMS = 250_000
_MAX_SESSION_TEXT = 64 * 1024**2


def _identity(value: object) -> str | None:
    if isinstance(value, str) and _IDENTITY.fullmatch(value):
        return value
    return None


def _text(content: object) -> str:
    if isinstance(content, str):
        return _join([content])
    if not isinstance(content, list):
        return ""
    return _join([
        block["text"] for block in content[:64]
        if isinstance(block, dict) and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ])


def _tool_result(content: object) -> bool:
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def _turn(thread: str, identity: str, timestamp: object, text: str) -> dict:
    return {
        "thread_id": thread,
        "turn_id": identity,
        "at": _iso(timestamp),
        "user": text,
        "assistant": "",
        "verified_checks": 0,
    }


def _export(stream: TextIO) -> Iterator[dict]:
    for number, conversation in enumerate(_json_array(stream, "Claude conversations.json"), 1):
        thread = _identity(conversation.get("uuid"))
        messages = conversation.get("chat_messages")
        if thread is None or not isinstance(messages, list):
            raise ValueError(f"Claude export conversation {number} is not in the supported format")
        current = None
        for message in messages:
            if not isinstance(message, dict) or message.get("sender") not in ("human", "assistant"):
                continue
            content = message.get("content")
            sender = message["sender"]
            if sender == "human":
                if current is not None:
                    yield current
                    current = None
                if _tool_result(content):
                    continue
            # Older exports provide text without typed content blocks.
            text = _text(content) if isinstance(content, list) and content else _text(message.get("text"))
            if not text:
                continue
            if sender == "human":
                identity = _identity(message.get("uuid"))
                if identity is None:
                    raise ValueError(f"Claude export conversation {number} lacks a user-message identifier")
                current = _turn(thread, identity, message.get("created_at"), text)
            elif current is not None:
                status, stop = message.get("status"), message.get("stop_reason")
                if status in (None, "completed", "finished_successfully") and stop in (
                    None, "end_turn", "stop_sequence",
                ):
                    current["assistant"] = text
        if current is not None:
            yield current


def _archive(path: Path) -> Iterator[dict]:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_EXPORT_ENTRIES:
                raise ValueError("Claude archive has too many entries")
            matches = [entry for entry in entries if entry.filename == "conversations.json"]
            if len(matches) != 1:
                raise ValueError("Expected one conversations.json file in the Claude ZIP root")
            entry = matches[0]
            if (entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16)
                    or entry.file_size > _MAX_EXPORT_SOURCE):
                raise ValueError("Expected regular, readable Claude conversations JSON of at most 4 GiB")
            with io.TextIOWrapper(archive.open(entry), encoding="utf-8-sig") as stream:
                yield from _export(stream)
    except zipfile.BadZipFile as error:
        raise ValueError("Invalid Claude export archive") from error


def _session_records(path: Path) -> Iterator[dict]:
    with path.open("rb") as source:
        for number in range(_MAX_SESSION_ITEMS + 1):
            line = source.readline(_MAX_LINE + 1)
            if not line:
                return
            if number == _MAX_SESSION_ITEMS:
                raise ValueError("Claude Code transcript exceeds the event limit")
            if len(line) > _MAX_LINE:
                while line and not line.endswith(b"\n"):
                    line = source.readline(_MAX_LINE + 1)
                continue
            if number == 0:
                line = line.removeprefix(b"\xef\xbb\xbf")
            record = _decode(line, _MAX_LINE)
            if record:
                yield record


def _unrelated(record: dict) -> bool:
    origin = record.get("origin")
    return any(record.get(flag) for flag in (
        "isSidechain", "isMeta", "isVisibleInTranscriptOnly", "isCompactSummary",
        "isSynthetic", "agentId", "parentToolUseID", "parent_tool_use_id",
    )) or (isinstance(origin, dict) and origin.get("kind") not in (None, "human"))


def _session(path: Path, known_session: str | None = None) -> Iterator[dict]:
    if not _ordinary(path, stat.S_IFREG) or path.stat().st_size > _MAX_EXPORT_SOURCE:
        raise ValueError("Expected an ordinary Claude Code transcript of at most 4 GiB")
    turns: dict[str, dict] = {}
    owner: dict[str, str | None] = {}
    latest: str | None = None
    size = 0
    for record in _session_records(path):
        role, message = record.get("type"), record.get("message")
        if (
            _unrelated(record) or role not in ("user", "assistant")
            or not isinstance(message, dict) or message.get("role") != role
        ):
            continue
        session = record.get("sessionId")
        identity = _identity(record.get("uuid"))
        if not isinstance(session, str) or not _SESSION.fullmatch(session) or identity is None:
            continue
        if known_session is None:
            known_session = session
        elif known_session.lower() != session.lower():
            continue
        if identity in owner:
            continue
        # Following the transcript ancestry keeps answers separate when a
        # session contains alternate branches or a compacted prefix.
        parent = _identity(record.get("parentUuid"))
        active = owner.get(parent) if "parentUuid" in record else latest
        content = message.get("content")
        if role == "user" and not _tool_result(content):
            text = _text(content)
            # These harness-injected messages can be persisted as user input.
            if text and not text.startswith((
                "<system-reminder>", "<local-command-caveat>", "<local-command-stdout>",
                "<command-name>", "<task-notification>", "<channel ",
            )):
                active = latest = identity
                turns[identity] = _turn(session.lower(), identity, record.get("timestamp"), text)
                size += len(text)
        elif role == "assistant" and active is not None and message.get("stop_reason") in _FINAL_STOPS:
            text = _text(content)
            if text:
                previous = turns[active]["assistant"]
                turns[active]["assistant"] = text
                size += len(text) - len(previous)
        owner[identity] = active
        if size > _MAX_SESSION_TEXT:
            raise ValueError("Claude Code transcript exceeds the visible text limit")
    yield from turns.values()


def _children(folder: Path) -> Iterator[Path]:
    with os.scandir(folder) as entries:
        for number, entry in enumerate(entries, 1):
            if number > _MAX_DIRECTORY_ITEMS:
                raise ValueError("Claude Code folder exceeds the entry limit")
            yield folder / entry.name


def _discover(path: Path) -> Iterator[tuple[Path, str]]:
    def sessions(folder: Path) -> Iterator[tuple[Path, str]]:
        for entry in _children(folder):
            if entry.suffix.lower() == ".jsonl" and _SESSION.fullmatch(entry.stem):
                if _ordinary(entry, stat.S_IFREG):
                    yield entry, entry.stem.lower()
            elif _SESSION.fullmatch(entry.name) and _ordinary(entry, stat.S_IFDIR):
                main = entry / "main.jsonl"
                if _ordinary(main, stat.S_IFREG):
                    yield main, entry.name.lower()

    path = local_root(path)
    projects = path / "projects"
    if projects.exists() or projects.is_symlink():
        path = local_root(projects)
    if path.name.casefold() != "projects":
        yield from sessions(path)
        return
    for project in _children(path):
        if (project.name.casefold() not in _IGNORED_FOLDERS and not project.name.startswith(".")
                and _ordinary(project, stat.S_IFDIR)):
            yield from sessions(project)


def iter_claude(path: Path) -> Iterator[dict]:
    """Read a selected Claude export, Claude Code session, profile, or projects directory."""
    path = path.expanduser().absolute()
    if any(part.casefold() in _IGNORED_FOLDERS for part in path.parts):
        raise ValueError("Choose the main Claude history instead of an internal worker folder")
    if _ordinary(path, stat.S_IFDIR):
        for session, identity in _discover(path):
            yield from _session(session, identity)
        return
    local_root(path.parent)
    if not _ordinary(path, stat.S_IFREG) or path.stat().st_size > _MAX_EXPORT_SOURCE:
        raise ValueError("Expected an ordinary Claude source of at most 4 GiB")
    suffix = path.suffix.lower()
    if suffix == ".jsonl" and not path.name.casefold().startswith("agent-"):
        expected = path.stem if _SESSION.fullmatch(path.stem) else None
        yield from _session(path, expected)
    elif suffix == ".json":
        with path.open("r", encoding="utf-8-sig") as source:
            yield from _export(source)
    elif suffix == ".zip":
        yield from _archive(path)
    else:
        raise ValueError("Expected Claude conversation JSON, ZIP, or a Claude Code transcript or folder")
