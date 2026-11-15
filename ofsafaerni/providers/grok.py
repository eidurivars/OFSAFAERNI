"""Read the observed Grok account-export format from an explicitly chosen file."""

from __future__ import annotations

from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import stat
from typing import Iterator, TextIO
import zipfile

from ..history_sources import (
    _EXPORT_CHUNK,
    _MAX_EXPORT_ENTRIES,
    _MAX_EXPORT_RECORD,
    _MAX_EXPORT_SOURCE,
    _THREAD_ID,
    _iso,
    _join,
    _ordinary,
)

_MEMBER = re.compile(
    r"(?:(?:ttl(?:-\d{1,6})?/\d{1,3}d/)?export_data/[A-Za-z0-9_-]{1,128}/)?"
    r"prod-grok-backend\.json"
)
_ASSISTANT = re.compile(r"assistant|grok(?:-[0-9][a-z0-9_.-]{0,94})?")
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _distinct_fields(pairs: list[tuple[str, object]]) -> dict:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Duplicate field in Grok export")
    return fields


class _Reader:
    """Decode a conversation at a time, with bounded buffers."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.buffer = ""
        self.offset = 0
        self.done = False
        self.decoder = json.JSONDecoder(object_pairs_hook=_distinct_fields)

    def _more(self, amount: int = _EXPORT_CHUNK) -> None:
        self.buffer = self.buffer[self.offset:]
        self.offset = 0
        extra = self.stream.read(amount)
        self.buffer += extra
        self.done = not extra

    def peek(self) -> str:
        while True:
            while self.offset < len(self.buffer) and self.buffer[self.offset] in " \t\r\n":
                self.offset += 1
            if self.offset < len(self.buffer):
                return self.buffer[self.offset]
            if self.done:
                return ""
            self._more()

    def take(self, char: str) -> None:
        if self.peek() != char:
            raise ValueError("Malformed structure in Grok export")
        self.offset += 1

    def value(self, limit: int = _MAX_EXPORT_RECORD) -> object:
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.offset)
            except (json.JSONDecodeError, RecursionError) as error:
                remaining = len(self.buffer) - self.offset
                if self.done:
                    raise ValueError("Malformed record in Grok export") from error
                if remaining >= limit:
                    raise ValueError("Grok export record exceeds its size limit") from error
                self._more(min(max(_EXPORT_CHUNK, remaining), limit - remaining))
            else:
                if end - self.offset > limit:
                    raise ValueError("Grok export record exceeds its size limit")
                # Unlike strings and objects, a number may end midway through a
                # token at the current chunk boundary.
                if type(value) in (int, float) and not self.done and (
                    end == len(self.buffer) or self.buffer[end] in ".eE"
                ):
                    remaining = len(self.buffer) - self.offset
                    if remaining >= limit:
                        raise ValueError("Grok export record exceeds its size limit")
                    self._more(min(_EXPORT_CHUNK, limit - remaining))
                    continue
                self.offset = end
                return value

    def skip_value(self) -> None:
        """Skip unrelated object or array bytes without decoding their contents."""
        first = self.peek()
        if first not in ("{", "["):
            self.value()
            return
        closers: list[str] = []
        quoted = escaped = False
        while True:
            if self.offset == len(self.buffer):
                if self.done:
                    raise ValueError("Unterminated object in Grok export")
                self._more()
                continue
            char = self.buffer[self.offset]
            self.offset += 1
            if quoted:
                if not escaped and char == '"':
                    quoted = False
                escaped = not escaped and char == "\\"
                continue
            if char == '"':
                quoted = True
            elif char in "{[":
                closers.append("}" if char == "{" else "]")
                if len(closers) > 200:
                    raise ValueError("Grok export nesting exceeds its limit")
            elif char in "}]":
                if not closers or char != closers.pop():
                    raise ValueError("Mismatched brackets in Grok export")
                if not closers:
                    return


def _conversations(stream: TextIO) -> Iterator[dict]:
    reader = _Reader(stream)
    reader.take("{")
    if reader.peek() == "}":
        raise ValueError("No conversations array in Grok export")
    found = False
    while True:
        key = reader.value(1024)
        if not isinstance(key, str):
            raise ValueError("Expected an object key in Grok export")
        reader.take(":")
        if key == "conversations":
            if found:
                raise ValueError("Duplicate conversations array in Grok export")
            found = True
            reader.take("[")
            if reader.peek() != "]":
                while True:
                    record = reader.value()
                    if not isinstance(record, dict):
                        raise ValueError("Expected a Grok conversation object")
                    yield record
                    if reader.peek() == "]":
                        break
                    reader.take(",")
            reader.take("]")
        else:
            reader.skip_value()
        if reader.peek() == "}":
            break
        reader.take(",")
    reader.take("}")
    if not found or reader.peek():
        raise ValueError("Expected one Grok export with a conversations array")


def _date(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("$date")
        if isinstance(value, dict):
            encoded = value.get("$numberLong")
            if not isinstance(encoded, str) or not re.fullmatch(r"\d{1,16}", encoded):
                return ""
            value = int(encoded) / 1000
    return _iso(value)


def _identity(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("$oid")
    return value if isinstance(value, str) and _THREAD_ID.fullmatch(value) else None


def _responses(record: dict) -> Iterator[dict]:
    meta, wrappers = record.get("conversation"), record.get("responses")
    if not isinstance(meta, dict) or not isinstance(wrappers, list):
        raise ValueError("Incomplete conversation in Grok export")
    thread = _identity(meta.get("id"))
    if thread is None:
        raise ValueError("Invalid conversation identifier in Grok export")
    responses = []
    for wrapper in wrappers:
        response = wrapper.get("response") if isinstance(wrapper, dict) else None
        if not isinstance(response, dict):
            raise ValueError("Invalid response record in Grok export")
        responses.append(response)

    has_lineage = any("parent_response_id" in response for response in responses)
    turns: dict[str, dict] = {}
    followups: dict[str, list[str]] = {}
    fallback_ids: Counter[str] = Counter()
    current = None
    for index, response in enumerate(responses):
        sender, message = response.get("sender"), response.get("message")
        if not isinstance(sender, str):
            continue
        role = sender.casefold()
        is_human = role == "human"
        if is_human:
            current = None
        if not is_human and not _ASSISTANT.fullmatch(role):
            continue
        if not isinstance(message, str) or _LONE_SURROGATE.search(message) or any(
            response.get(key) is True for key in ("hidden", "is_hidden", "is_deleted")
        ):
            continue
        text = _join([message])
        if not text:
            continue
        if is_human:
            source_id = response.get("_id")
            identifier = _identity(source_id)
            stamp = _date(response.get("create_time"))
            if source_id is None:
                # A timestamp keeps the fallback ID stable when a later export
                # drops preceding turns. The ordinal works for undated exports.
                anchor = stamp or str(index)
                digest = hashlib.sha256(f"{thread}\x00{anchor}\x00{message}".encode("utf-8")).hexdigest()
                fallback_ids[digest] += 1
                identifier = f"grok-{digest}-{fallback_ids[digest]}"
            if identifier is None or identifier in turns:
                raise ValueError("Invalid or duplicate human message identifier in Grok export")
            turns[identifier] = {
                "thread_id": thread,
                "turn_id": identifier,
                "at": stamp,
                "user": text,
                "assistant": "",
                "verified_checks": 0,
            }
            current = identifier
        elif not response.get("error"):
            parent = _identity(response.get("parent_response_id")) if has_lineage else current
            if parent is not None:
                candidates = followups.setdefault(parent, [])
                if len(candidates) < 2:
                    candidates.append(text)

    for identifier, turn in turns.items():
        candidates = followups.get(identifier, [])
        if len(candidates) == 1:
            turn["assistant"] = candidates[0]
        yield turn


def _turns(stream: TextIO) -> Iterator[dict]:
    for record in _conversations(stream):
        yield from _responses(record)


def iter_grok(path: Path) -> Iterator[dict]:
    """Read a local Grok JSON export or only its named conversation file from a ZIP."""
    path = path.absolute()
    if not _ordinary(path, stat.S_IFREG) or path.stat().st_size > _MAX_EXPORT_SOURCE:
        raise ValueError("Expected an ordinary Grok export file of at most 4 GiB")
    if path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8-sig") as stream:
            yield from _turns(stream)
        return
    if path.suffix.lower() != ".zip":
        raise ValueError("Expected Grok prod-grok-backend.json or an account-export ZIP")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_EXPORT_ENTRIES:
                raise ValueError("Grok export ZIP contains too many entries")
            candidates = [entry for entry in entries if _MEMBER.fullmatch(entry.filename)]
            if len(candidates) != 1:
                raise ValueError("Expected exactly one prod-grok-backend.json in the Grok ZIP")
            entry = candidates[0]
            if entry.file_size > _MAX_EXPORT_SOURCE or entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Expected a regular, readable Grok JSON member of at most 4 GiB")
            with io.TextIOWrapper(archive.open(entry), encoding="utf-8-sig") as stream:
                yield from _turns(stream)
    except zipfile.BadZipFile as error:
        raise ValueError("Invalid Grok export ZIP") from error
