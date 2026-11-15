"""Read selected Gemini CLI sessions and recognizable Gemini Apps activity."""

from __future__ import annotations

import json
import os
import re
import stat
import zipfile
from collections.abc import Iterator
from hashlib import sha256
from io import TextIOWrapper
from pathlib import Path

from ..history_sources import (
    _MAX_EXPORT_ENTRIES,
    _MAX_EXPORT_SOURCE,
    _iso,
    _join,
    _json_array,
    _ordinary,
)
from ..sources import local_root

_SESSION = re.compile(r"session-.+\.jsonl?$")
_ID = re.compile(r"[a-zA-Z0-9_.:-]{1,128}")
_FILE_BYTES = 128 * 1024**2
_LINE_BYTES = 16 * 1024**2
_MESSAGES = 65_536
_SESSION_TEXT = 32 * 1024**2
_CONTEXT = (
    "<session_context>",
    "<hook_context>",
    "This is the Gemini CLI. We are setting up the context for our chat.",
)
_REFERENCE = "--- Content from referenced files ---"
_TAKEOUT_MEMBER = "Takeout/My Activity/Gemini Apps/MyActivity.json"


def _children(root: Path) -> list[Path]:
    items = []
    with os.scandir(root) as entries:
        for entry in entries:
            if len(items) >= _MAX_EXPORT_ENTRIES:
                raise ValueError("Gemini directory entry limit reached")
            items.append(Path(entry.path))
    return sorted(items)


def _session_paths(root: Path) -> Iterator[Path]:
    yield from (item for item in _children(root) if _SESSION.fullmatch(item.name))
    tmp = root / "tmp"
    if _ordinary(tmp, stat.S_IFDIR):
        for project in _children(tmp):
            if _ordinary(project, stat.S_IFDIR):
                chats = project / "chats"
                if _ordinary(chats, stat.S_IFDIR):
                    yield from (item for item in _children(chats) if _SESSION.fullmatch(item.name))
    chats = root / "chats"
    if _ordinary(chats, stat.S_IFDIR):
        yield from (item for item in _children(chats) if _SESSION.fullmatch(item.name))


def _plain(content: object) -> str:
    if isinstance(content, str):
        content = [content]
    elif isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts = []
    remaining = 32_768
    for part in content[:128]:
        if isinstance(part, dict):
            if any(key not in {"text", "thought"} for key in part) or part.get("thought"):
                continue
            part = part.get("text")
        if not isinstance(part, str) or part.lstrip().startswith(_CONTEXT):
            continue
        # The CLI appends referenced file contents and MCP resource results here.
        marker = part.find(_REFERENCE)
        end = min(marker, remaining) if marker >= 0 else remaining
        snippet = part[:end]
        parts.append(snippet)
        remaining -= len(snippet)
        if marker >= 0 or remaining <= 0:
            break
    return _join(parts)


def _read_jsonl(path: Path) -> Iterator[dict]:
    total = 0
    with path.open("rb") as stream:
        for _ in range(_MESSAGES + 1):
            raw = stream.readline(_LINE_BYTES + 1)
            if not raw:
                return
            total += len(raw)
            if len(raw) > _LINE_BYTES or total > _FILE_BYTES:
                raise ValueError("Gemini session size limit reached")
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                # The active CLI may still be appending this final record.
                if not raw.endswith(b"\n"):
                    return
                raise ValueError("Invalid Gemini session record") from None
            if isinstance(row, dict):
                yield row
            else:
                raise ValueError("Invalid Gemini session record")
    raise ValueError("Gemini session record limit reached")


def _read_json(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(_FILE_BYTES + 1)
    if len(raw) > _FILE_BYTES:
        raise ValueError("Gemini session size limit reached")
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid Gemini session JSON") from None
    if not isinstance(record, dict) or not isinstance(record.get("messages"), list):
        raise ValueError("Expected a Gemini CLI session JSON object")
    return record


def _retain(messages: dict, row: object) -> None:
    if not isinstance(row, dict):
        return
    identifier = row.get("id")
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        return
    role = row.get("type")
    if role not in ("user", "gemini"):
        messages[identifier] = ("", "", "")
        return
    content = _plain(row.get("content"))
    if role == "user" and row.get("displayContent") is not None and content:
        content = _plain(row["displayContent"])
    messages[identifier] = (role, _iso(row.get("timestamp")), content)


def _apply_patch(messages: dict, patch: dict, retained_size: int) -> int:
    def update(row):
        nonlocal retained_size
        if not isinstance(row, dict):
            return
        identifier = row.get("id")
        if isinstance(identifier, str) and identifier in messages and "content" in row:
            role, at, previous = messages[identifier]
            if not role:
                return
            content = _plain(row["content"])
            retained_size += len(content) - len(previous)
            if retained_size > _SESSION_TEXT:
                raise ValueError("Gemini session text limit reached")
            messages[identifier] = (role, at, content)

    if isinstance(patch.get("id"), str):
        update(patch)
    if isinstance(patch.get("updates"), list):
        for row in patch["updates"]:
            update(row)
    if isinstance(patch.get("removeIds"), list):
        for identifier in patch["removeIds"]:
            if isinstance(identifier, str):
                _, _, content = messages.pop(identifier, ("", "", ""))
                retained_size -= len(content)
    if isinstance(patch.get("orderIds"), list):
        moved = {}
        for identifier in patch["orderIds"]:
            if isinstance(identifier, str) and identifier in messages:
                moved[identifier] = messages.pop(identifier)
        messages.update(moved)
    return retained_size


def _session(path: Path) -> tuple[str, str, dict]:
    records = _read_jsonl(path) if path.suffix.lower() == ".jsonl" else (_read_json(path),)
    messages = {}
    session_id = kind = ""
    processed = retained_size = 0
    for row in records:
        if "$rewindTo" in row:
            identifier = row["$rewindTo"]
            if not isinstance(identifier, str):
                raise ValueError("Invalid Gemini rewind record")
            while messages:
                removed, (_, _, content) = messages.popitem()
                retained_size -= len(content)
                if removed == identifier:
                    break
            continue
        if "$patch" in row:
            patch = row["$patch"]
            if not isinstance(patch, dict):
                raise ValueError("Invalid Gemini patch record")
            retained_size = _apply_patch(messages, patch, retained_size)
            continue
        metadata = row.get("$set", row)
        if not isinstance(metadata, dict):
            raise ValueError("Invalid Gemini metadata record")
        if "id" in row:
            entries = [row]
        else:
            if isinstance(metadata.get("sessionId"), str):
                session_id = metadata["sessionId"]
            if isinstance(metadata.get("kind"), str):
                kind = metadata["kind"]
            entries = metadata.get("messages", [])
            if not isinstance(entries, list):
                raise ValueError("Invalid Gemini session messages")
            if "$set" in row and "messages" in metadata:
                messages.clear()
                retained_size = 0
        for entry in entries:
            processed += 1
            if processed > _MESSAGES:
                raise ValueError("Gemini session message limit reached")
            identifier = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(identifier, str):
                retained_size -= len(messages.get(identifier, ("", "", ""))[2])
                _retain(messages, entry)
                retained_size += len(messages.get(identifier, ("", "", ""))[2])
                if retained_size > _SESSION_TEXT:
                    raise ValueError("Gemini session text limit reached")
    if not _ID.fullmatch(session_id):
        raise ValueError("Gemini CLI session identifier is missing or invalid")
    return session_id, kind, messages


def _turns(session_id: str, messages: dict) -> Iterator[dict]:
    turn = None
    answers = []
    for identifier, (role, at, content) in messages.items():
        if role == "user":
            if turn is not None:
                yield {**turn, "assistant": _join(answers)}
            answers = []
            turn = None
            if content and not content.lstrip().startswith(("/", "?", *_CONTEXT)):
                turn = {"thread_id": session_id, "turn_id": identifier, "at": at,
                        "user": content, "verified_checks": 0}
        elif role == "gemini" and turn is not None and content and len(answers) < 64:
            answers.append(content)
    if turn is not None:
        yield {**turn, "assistant": _join(answers)}


def _takeout(stream) -> Iterator[dict]:
    for record in _json_array(stream, "Gemini Apps MyActivity.json"):
        products = record.get("products")
        provider = {"Gemini", "Gemini Apps"}
        header = record.get("header")
        if not (isinstance(header, str) and header in provider) and not (
            isinstance(products, list)
            and any(isinstance(product, str) and product in provider for product in products)
        ):
            continue
        at = _iso(record.get("time"))
        title = record.get("title")
        if not at or not isinstance(title, str) or not title.startswith("Prompted "):
            continue
        user = _join([title[len("Prompted "):]])
        if user:
            # Takeout activity does not identify threads or reliably label answers.
            identifier = sha256((at + "\n" + title).encode("utf-8")).hexdigest()
            yield {"thread_id": identifier, "turn_id": identifier, "at": at,
                   "user": user, "assistant": "", "verified_checks": 0}


def _takeout_archive(path: Path) -> Iterator[dict]:
    if path.stat().st_size > _MAX_EXPORT_SOURCE:
        raise ValueError("Gemini activity archive size limit reached")
    try:
        with zipfile.ZipFile(path) as archive:
            items = archive.infolist()
            members = [item for item in items if item.filename == _TAKEOUT_MEMBER]
            if len(items) > _MAX_EXPORT_ENTRIES or len(members) != 1:
                raise ValueError("Expected one Gemini Apps MyActivity.json in the Takeout archive")
            member = members[0]
            mode = stat.S_IFMT(member.external_attr >> 16)
            if (member.file_size > _FILE_BYTES or mode not in (0, stat.S_IFREG)
                    or member.flag_bits & 1):
                raise ValueError("Invalid, encrypted, or oversized Gemini Apps archive member")
            with archive.open(member) as raw, TextIOWrapper(raw, encoding="utf-8-sig") as stream:
                yield from _takeout(stream)
    except (zipfile.BadZipFile, NotImplementedError):
        raise ValueError("Invalid Gemini Apps Takeout ZIP archive") from None


def iter_gemini(path: Path) -> Iterator[dict]:
    """Read an explicitly chosen session, chats/project/profile directory or Takeout."""
    path = path.expanduser()
    if _ordinary(path, stat.S_IFDIR):
        paths = []
        for item in _session_paths(local_root(path)):
            paths.append(item)
            if len(paths) > _MAX_EXPORT_ENTRIES:
                raise ValueError("Gemini session file limit reached")
        paths.sort(key=lambda item: (item.suffix != ".jsonl", item))
    else:
        path = local_root(path.parent) / path.name
        if not _ordinary(path, stat.S_IFREG):
            raise ValueError("Expected an ordinary Gemini history source")
        if path.suffix.lower() == ".zip":
            yield from _takeout_archive(path)
            return
        if path.name == "MyActivity.json":
            if path.stat().st_size > _FILE_BYTES:
                raise ValueError("Gemini activity export size limit reached")
            with path.open(encoding="utf-8-sig") as stream:
                yield from _takeout(stream)
            return
        if path.suffix.lower() not in {".json", ".jsonl"}:
            raise ValueError("Expected Gemini CLI session JSON/JSONL or Gemini Apps Takeout")
        paths = [path]
    seen = set()
    for item in paths:
        if not _ordinary(item, stat.S_IFREG):
            continue
        session_id, kind, messages = _session(item)
        if kind not in {"", "main"} or session_id in seen:
            continue
        seen.add(session_id)
        yield from _turns(session_id, messages)
