"""Read-only, bounded readers for Codex-owned conversation history."""

from __future__ import annotations

import json
import io
import math
import os
import re
import sqlite3
import stat
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


_HISTORY_DB = "thread_history_1.sqlite"
_STATE_DB = "state_5.sqlite"
_UID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
_ROLLOUT_NAME = re.compile(r"rollout-.+-(?P<uid>" + _UID.pattern + r")\.jsonl$", re.IGNORECASE)
_THREAD_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")
_MAX_ITEM = 262_144
_MAX_LINE = 524_288
_MAX_TEXT = 32_768
_MAX_USER_ITEMS = 64
_MAX_CHECKS = 32
_MAX_EXPORT_SOURCE = 4 * 1024**3
_MAX_EXPORT_RECORD = 16 * 1024**2
_EXPORT_CHUNK = 65_536
_MAX_EXPORT_ENTRIES = 65_536
_EXPORT_MEMBER = re.compile(r"conversations(?:-\d{1,6})?\.json")
_LOCAL_SOURCES = {"cli", "vscode", "exec", "appserver", "app_server", "desktop", "codex", "mcp", "unknown"}
_CHECK = re.compile(
    r"(?im)(?:^|[;\n&|{]|-(?:command|c)[ \t]+[\"'])[ \t]*(?:"
    r"(?:(?:[\"'][^\"'\r\n]{1,200}[\"']|[^\s\"';|&{}]{1,200})[ \t]+-m[ \t]+)?"
    r"(?:pytest|unittest)(?:\.exe)?\b|"
    r"(?:npm|pnpm|yarn|go|cargo|dotnet)\s+(?:run\s+)?test\b|node\s+--test\b|"
    r"git\s+diff[^\n]{0,128}\s--check\b|"
    r"pip(?:[0-9.]*)\s+check\b|ruff\s+check\b|"
    r"tsc\s+--noEmit\b|"
    r"invoke-pester\b"
    r")"
)
_ROOT = """
NOT EXISTS (
    SELECT 1 FROM state.thread_spawn_edges e WHERE e.child_thread_id = s.id
)
AND CASE
    WHEN json_valid(s.source) THEN json_type(s.source, '$.subagent') IS NULL
    ELSE lower(s.source) NOT LIKE 'subagent%'
END
"""


def _ordinary(path: Path, mode: int) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_IFMT(info.st_mode) == mode and not (
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


@contextmanager
def _projection(home: Path):
    """SQLite reads current committed WAL records without changing either database."""
    hist, state = home / _HISTORY_DB, home / _STATE_DB
    if not (_ordinary(hist, stat.S_IFREG) and _ordinary(state, stat.S_IFREG)):
        yield None
        return
    connection = sqlite3.connect(hist.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("ATTACH DATABASE ? AS state", (state.as_uri() + "?mode=ro",))
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()


def _rollouts(home: Path) -> Iterator[tuple[str, Path]]:
    for name in ("sessions", "archived_sessions"):
        root = home / name
        if not _ordinary(root, stat.S_IFDIR):
            continue
        for location, dirs, names in os.walk(root, followlinks=False):
            path = Path(location)
            dirs[:] = sorted(value for value in dirs if _ordinary(path / value, stat.S_IFDIR))
            for value in sorted(names):
                match = _ROLLOUT_NAME.fullmatch(value)
                if match and _ordinary(path / value, stat.S_IFREG):
                    yield name, path / value


def _decode(raw: str | bytes, max_bytes: int = _MAX_ITEM) -> dict:
    if len(raw) > max_bytes:
        return {}
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return {}
    return result if isinstance(result, dict) else {}


def _text_parts(content: object, kinds: set[str]) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content[:_MAX_USER_ITEMS]:
        if (
            isinstance(part, dict)
            and isinstance(part.get("type"), str)
            and part["type"] in kinds
            and isinstance(part.get("text"), str)
        ):
            parts.append(part["text"])
    return _join(parts)


def _join(parts: list[str]) -> str:
    text = "\n\n".join(part.strip() for part in parts if part.strip())
    if len(text) <= _MAX_TEXT:
        return text
    # Preserve qualifying context near both ends without connecting fragments.
    half = (_MAX_TEXT - 12) // 2
    return text[:half] + "\n\n[… ]\n\n" + text[-half:]


def _iso(value: object) -> str:
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        try:
            if abs(value) >= 100_000_000_000:
                value = value / 1000
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return ""
    if isinstance(value, str) and len(value) <= 80:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.isoformat() if parsed.tzinfo else ""
        except ValueError:
            pass
    return ""


def _projected(connection: sqlite3.Connection, thread_id: str | None) -> Iterator[dict]:
    sql = f"""
    SELECT t.thread_id, t.turn_id, t.started_at, t.status, t.final_agent_item_id
    FROM thread_turns t JOIN state.threads s ON s.id = t.thread_id
    WHERE ({_ROOT}) AND (? IS NULL OR t.thread_id = ?)
    ORDER BY t.thread_id, t.rollout_ordinal
    """
    for thread, turn, started, status, final in connection.execute(sql, (thread_id, thread_id)):
        items = connection.execute(
            """SELECT item_json, created_at_ms
               FROM thread_items
               WHERE thread_id=? AND turn_id=? AND item_type='userMessage' AND length(item_json)<=?
               ORDER BY rollout_ordinal LIMIT ?""",
            (thread, turn, _MAX_ITEM, _MAX_USER_ITEMS),
        )
        user_parts: list[str] = []
        at = ""
        for raw, created_at in items:
            value = _decode(raw)
            if value.get("type") != "userMessage":
                continue
            text = _text_parts(value.get("content"), {"text"})
            if text:
                user_parts.append(text)
                if not at:
                    at = _iso(created_at)
        if not user_parts:
            continue
        answer = ""
        if status == "completed" and final:
            cell = connection.execute(
                """SELECT item_json FROM thread_items
                   WHERE thread_id=? AND turn_id=? AND item_id=? AND item_type='agentMessage'
                         AND length(item_json)<=?""",
                (thread, turn, final, _MAX_ITEM),
            ).fetchone()
            value = _decode(cell[0]) if cell else {}
            if (
                value.get("type") == "agentMessage"
                and value.get("phase") in (None, "final_answer")
                and isinstance(value.get("text"), str)
            ):
                answer = _join([value["text"]])
        checks = 0
        if answer:
            # Never request command output or connector results.
            for (command,) in connection.execute(
                """SELECT substr(json_extract(item_json, '$.command'), 1, 16000)
                   FROM thread_items
                   WHERE thread_id=? AND turn_id=? AND item_type='commandExecution' AND
                       CASE WHEN json_valid(item_json) THEN
                         json_extract(item_json, '$.status') = 'completed' AND
                         json_type(item_json, '$.exitCode') = 'integer' AND
                         json_extract(item_json, '$.exitCode') = 0
                       ELSE 0 END
                   ORDER BY rollout_ordinal""",
                (thread, turn),
            ):
                if isinstance(command, str) and _CHECK.search(command):
                    checks += 1
                    if checks == _MAX_CHECKS:
                        break
        yield {
            "thread_id": thread,
            "turn_id": turn,
            "at": at or _iso(started),
            "user": _join(user_parts),
            "assistant": answer,
            "verified_checks": checks,
        }


def _lines(path: Path, offset: int = 0) -> Iterator[dict]:
    with path.open("rb") as stream:
        if offset:
            # A stale or foreign cursor must not cut the middle of a record.
            stream.seek(0, os.SEEK_END)
            if offset <= stream.tell():
                stream.seek(offset - 1)
                if stream.read(1) != b"\n":
                    stream.seek(0)
            else:
                stream.seek(0)
        while raw := stream.readline(_MAX_LINE + 1):
            if len(raw) > _MAX_LINE:
                while raw and not raw.endswith(b"\n"):
                    raw = stream.readline(_MAX_LINE + 1)
                continue
            # Most large retained rows hold tool output or internal reasoning.
            # Match actual envelope fields after decoding these candidates.
            if not (
                b'"session_meta"' in raw or b'"turn_context"' in raw
                or b'"task_started"' in raw or b'"task_complete"' in raw
                or b'"user_message"' in raw
                or (b'"role"' in raw and (b'"user"' in raw or b'"final_answer"' in raw))
            ):
                continue
            event = _decode(raw, _MAX_LINE)
            if event:
                yield event


def _legacy(path: Path, expected: str, *, offset: int = 0) -> Iterator[dict]:
    events = _lines(path)
    first = next(events, {})
    value = first.get("payload")
    if first.get("type") != "session_meta" or not isinstance(value, dict):
        events.close()
        return
    identity = value.get("id") or value.get("session_id")
    source = value.get("source")
    if (
        not isinstance(identity, str)
        or identity.lower() != expected.lower()
        or value.get("parent_thread_id")
        or not isinstance(source, str)
        or source.lower() not in _LOCAL_SOURCES
    ):
        events.close()
        return
    if offset:
        events.close()
        events = _lines(path, offset)
    turn: str = ""
    at: str = ""
    user_parts: list[str] = []
    fallback_parts: list[str] = []
    final: str = ""

    def produce(assistant: str = "") -> dict | None:
        user = _join(user_parts or fallback_parts)
        if not turn or not user:
            return None
        return {
            "thread_id": identity,
            "turn_id": turn,
            "at": at,
            "user": user,
            "assistant": _join([assistant]),
            "verified_checks": 0,
        }

    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        event_type, kind = event.get("type"), payload.get("type")
        event_turn = payload.get("turn_id")
        if (event_type == "event_msg" and kind == "task_started") or event_type == "turn_context":
            if isinstance(event_turn, str) and _THREAD_ID.fullmatch(event_turn) and event_turn != turn:
                prior = produce()
                if prior:
                    yield prior
                turn, at = event_turn, _iso(event.get("timestamp")) or _iso(payload.get("started_at"))
                user_parts, fallback_parts, final = [], [], ""
            continue
        if not turn:
            continue
        if event_type == "response_item" and kind == "message":
            if payload.get("role") == "user" and len(user_parts) < _MAX_USER_ITEMS:
                text = _text_parts(payload.get("content"), {"input_text", "text"})
                if text:
                    user_parts.append(text)
            elif payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
                final = _text_parts(payload.get("content"), {"output_text", "text"})
        elif event_type == "event_msg" and kind == "user_message" and len(fallback_parts) < _MAX_USER_ITEMS:
            if isinstance(payload.get("message"), str) and payload["message"].strip():
                fallback_parts.append(payload["message"])
        elif event_type == "event_msg" and kind == "task_complete" and event_turn == turn:
            answer = payload.get("last_agent_message")
            completed = produce(final or (answer if isinstance(answer, str) else ""))
            if completed:
                yield completed
            turn, at = "", ""
            user_parts, fallback_parts, final = [], [], ""
    remaining = produce()
    if remaining:
        yield remaining


def _known_projection(connection: sqlite3.Connection) -> tuple[set[str], set[str]]:
    projected = {
        str(row[0]).lower()
        for row in connection.execute(
            """SELECT DISTINCT i.thread_id FROM thread_items i
               JOIN thread_turns t ON t.thread_id=i.thread_id AND t.turn_id=i.turn_id
               WHERE i.item_type='userMessage'"""
        )
    }
    excluded = {
        str(row[0]).lower()
        for row in connection.execute(
            f"SELECT s.id FROM state.threads s WHERE NOT ({_ROOT})"
        )
    }
    return projected, excluded


def _target_offset(connection: sqlite3.Connection, thread: str) -> int:
    """Resume a single active rollout without rereading its projected past."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "thread_history_projection_state" not in tables:
        return 0
    projection = connection.execute(
        "SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id=?",
        (thread,),
    ).fetchone()
    offset = projection[0] if projection else None
    if type(offset) is not int or offset <= 0:
        return 0
    columns = {row[1] for row in connection.execute("PRAGMA table_info(thread_turns)")}
    if "rollout_byte_offset" not in columns:
        return 0
    latest = connection.execute(
        """SELECT status, rollout_byte_offset FROM thread_turns
           WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1""",
        (thread,),
    ).fetchone()
    if latest is None:
        return 0
    status, start = latest
    if status == "inProgress":
        return min(offset, start) if type(start) is int and start > 0 else 0
    return offset


def iter_codex_turns(home: Path, *, thread_id: str | None = None) -> Iterator[dict]:
    """Yield known top-level user turns, omitting hidden/tool/intermediate messages."""
    home = home.absolute()
    if thread_id is not None and not _THREAD_ID.fullmatch(thread_id):
        raise ValueError("Invalid Codex thread identifier")
    inputs = []
    for _, path in _rollouts(home):
        match = _ROLLOUT_NAME.fullmatch(path.name)
        assert match is not None
        identity = match["uid"].lower()
        if thread_id is None or identity == thread_id.lower():
            inputs.append((identity, path))
    emitted: set[tuple[str, str]] = set()
    pending: dict[tuple[str, str], dict] = {}
    target_offset = 0
    with _projection(home) as connection:
        if connection is not None:
            _, excluded = _known_projection(connection)
            inputs = [(identity, path) for identity, path in inputs if identity not in excluded]
            retained = {identity for identity, _ in inputs}
            if thread_id is not None and len(inputs) == 1:
                target_offset = _target_offset(connection, thread_id)
            for row in _projected(connection, thread_id):
                key = (row["thread_id"].lower(), row["turn_id"])
                if key[0] in retained and not row["assistant"]:
                    # A retained terminal row can fill the native projection's
                    # missing answer. Do not publish the same user turn twice.
                    pending[key] = row
                else:
                    emitted.add(key)
                    yield row
    for identity, path in inputs:
        for row in _legacy(path, identity, offset=target_offset):
            key = (identity, row["turn_id"])
            if key in emitted:
                continue
            native = pending.get(key)
            if native is not None:
                if not row["assistant"]:
                    continue
                pending.pop(key)
                native["assistant"] = row["assistant"]
                row = native
            emitted.add(key)
            yield row
    yield from pending.values()


def coverage(home: Path) -> dict[str, Any]:
    """Summarize coverage without disclosing transcript text or thread identifiers."""
    home = home.absolute()
    info: dict[str, Any] = {
        "sqlite_available": False,
        "state_threads": 0,
        "top_level_threads": 0,
        "archived_top_level_threads": 0,
        "projected_top_level_threads": 0,
        "projected_top_level_turns": 0,
        "session_rollouts": 0,
        "archive_rollouts": 0,
        "unprojected_rollout_candidates": 0,
        "top_level_rollout_candidates": 0,
    }
    projected: set[str] = set()
    excluded: set[str] = set()
    with _projection(home) as connection:
        if connection is not None:
            projected, excluded = _known_projection(connection)
            tracked = connection.execute("SELECT count(*) FROM state.threads").fetchone()
            roots = connection.execute(
                f"SELECT count(*), sum(s.archived) FROM state.threads s WHERE ({_ROOT})"
            ).fetchone()
            rows = connection.execute(
                f"""SELECT count(DISTINCT t.thread_id), count(*) FROM thread_turns t
                    JOIN state.threads s ON s.id=t.thread_id WHERE ({_ROOT})"""
            ).fetchone()
            info.update({
                "sqlite_available": True,
                "state_threads": tracked[0],
                "top_level_threads": roots[0],
                "archived_top_level_threads": roots[1] or 0,
                "projected_top_level_threads": rows[0],
                "projected_top_level_turns": rows[1],
            })
    for name, path in _rollouts(home):
        info["session_rollouts" if name == "sessions" else "archive_rollouts"] += 1
        match = _ROLLOUT_NAME.fullmatch(path.name)
        assert match is not None
        if match["uid"].lower() not in excluded:
            info["top_level_rollout_candidates"] += 1
        if match["uid"].lower() not in projected | excluded:
            info["unprojected_rollout_candidates"] += 1
    return info


def _json_array(stream: TextIO, name: str) -> Iterator[dict]:
    """Decode one conversation at a time from the export's top-level array."""
    def distinct_fields(pairs: list[tuple[str, Any]]) -> dict:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON object field")
        return result

    decode = json.JSONDecoder(object_pairs_hook=distinct_fields)
    buffer, done = "", False

    def more(amount: int = _EXPORT_CHUNK) -> None:
        nonlocal buffer, done
        extra = stream.read(amount)
        if extra:
            buffer += extra
        else:
            done = True

    def nonspace() -> None:
        nonlocal buffer
        buffer = buffer.lstrip()
        while not buffer and not done:
            more()
            buffer = buffer.lstrip()

    nonspace()
    if not buffer.startswith("["):
        raise ValueError(f"{name}: expected a JSON array of conversations")
    buffer = buffer[1:]
    nonspace()
    number = 0
    if not buffer.startswith("]"):
        while True:
            number += 1
            if not buffer.startswith("{"):
                raise ValueError(f"{name}: conversation {number} is missing or not an object")
            while True:
                try:
                    record, end = decode.raw_decode(buffer)
                    break
                except (json.JSONDecodeError, RecursionError) as error:
                    if done:
                        raise ValueError(f"{name}: malformed conversation {number}") from error
                    if len(buffer) >= _MAX_EXPORT_RECORD:
                        raise ValueError(f"{name}: conversation {number} exceeds the record limit") from error
                    # Grow geometrically so an incomplete large object is not
                    # reparsed once for every small block.
                    more(min(max(_EXPORT_CHUNK, len(buffer)), _MAX_EXPORT_RECORD - len(buffer)))
                except ValueError as error:
                    raise ValueError(f"{name}: malformed conversation {number}") from error
            yield record
            buffer = buffer[end:]
            nonspace()
            if buffer.startswith("]"):
                break
            if not buffer.startswith(","):
                raise ValueError(f"{name}: expected an array separator after conversation {number}")
            buffer = buffer[1:]
            nonspace()
    buffer = buffer[1:]
    nonspace()
    if buffer:
        raise ValueError(f"{name}: data after the conversations array")


def _active_branch(conversation: dict, location: str) -> Iterator[dict]:
    nodes, leaf = conversation.get("mapping"), conversation.get("current_node")
    if not isinstance(nodes, dict):
        raise ValueError(f"{location}: missing conversation mapping")
    if not nodes and leaf is None:
        return
    if not isinstance(leaf, str) or leaf not in nodes:
        raise ValueError(f"{location}: no valid current_node")
    visited: set[str] = set()
    branch: list[dict] = []
    cursor: str | None = leaf
    while cursor is not None:
        node = nodes.get(cursor)
        if cursor in visited or not isinstance(node, dict):
            raise ValueError(f"{location}: broken or cyclic active conversation branch")
        visited.add(cursor)
        message = node.get("message")
        if isinstance(message, dict):
            branch.append(message)
        cursor = node.get("parent")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError(f"{location}: invalid parent node on the active branch")
    yield from reversed(branch)


def _export_message(message: dict) -> tuple[str, str]:
    author = message.get("author")
    role = author.get("role") if isinstance(author, dict) else None
    if role not in ("user", "assistant"):
        return "", ""
    metadata = message.get("metadata")
    if isinstance(metadata, dict) and any(
        metadata.get(flag) is True
        for flag in ("is_visually_hidden_from_conversation", "is_hidden", "is_system")
    ):
        return "", ""
    if message.get("recipient") not in (None, "all") or message.get("channel") not in (None, "final"):
        return "", ""
    if role == "assistant" and (
        message.get("status") not in (None, "finished_successfully", "completed")
        or message.get("end_turn") is False
    ):
        return "", ""
    content = message.get("content")
    if not isinstance(content, dict) or content.get("content_type") not in ("text", "multimodal_text"):
        return "", ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return "", ""
    return role, _join([part for part in parts[:_MAX_USER_ITEMS] if isinstance(part, str)])


def _export_turns(stream: TextIO, name: str) -> Iterator[dict]:
    for index, conversation in enumerate(_json_array(stream, name), start=1):
        location = f"{name}: conversation {index}"
        thread = conversation.get("conversation_id") or conversation.get("id")
        if not isinstance(thread, str) or not _THREAD_ID.fullmatch(thread):
            raise ValueError(f"{location}: missing or invalid conversation identifier")
        current = None
        for message in _active_branch(conversation, location):
            role, text = _export_message(message)
            if not text:
                continue
            if role == "user":
                identity = message.get("id")
                if not isinstance(identity, str) or not _THREAD_ID.fullmatch(identity):
                    raise ValueError(f"{location}: invalid user-message identifier")
                if current is not None:
                    yield current
                current = {
                    "thread_id": thread,
                    "turn_id": identity,
                    "at": _iso(message.get("create_time")),
                    "user": text,
                    "assistant": "",
                    "verified_checks": 0,
                }
            elif current is not None:
                current["assistant"] = text
        if current is not None:
            yield current


def iter_chatgpt_export(path: Path) -> Iterator[dict]:
    """Read a user-supplied official conversations JSON or the data-export ZIP."""
    path = path.absolute()
    if not _ordinary(path, stat.S_IFREG) or path.stat().st_size > _MAX_EXPORT_SOURCE:
        raise ValueError("Expected an ordinary ChatGPT export file of at most 4 GiB")
    if path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8-sig") as stream:
            yield from _export_turns(stream, "conversations.json")
    elif path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > _MAX_EXPORT_ENTRIES:
                    raise ValueError("ChatGPT archive includes too many entries")
                conversations = [entry for entry in entries if _EXPORT_MEMBER.fullmatch(entry.filename)]
                if not conversations or len({entry.filename for entry in conversations}) != len(conversations):
                    raise ValueError("Expected unambiguous conversations JSON in the ZIP root")
                if sum(entry.file_size for entry in conversations) > _MAX_EXPORT_SOURCE:
                    raise ValueError("ChatGPT archive conversation JSON exceeds 4 GiB")
                for entry in sorted(conversations, key=lambda item: item.filename):
                    if entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                        raise ValueError("Expected regular, readable conversations JSON in the ZIP")
                    with io.TextIOWrapper(archive.open(entry), encoding="utf-8-sig") as stream:
                        yield from _export_turns(stream, entry.filename)
        except zipfile.BadZipFile as error:
            raise ValueError("Invalid ChatGPT export archive") from error
    else:
        raise ValueError("Expected a conversations JSON or data-export ZIP file")
