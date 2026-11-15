"""Bounded, read-only Agent Skills collection from local folders and GitHub."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib import error, parse, request

import yaml


MAX_SKILL_BYTES = 128 * 1024
MAX_HEADER_BYTES = 12 * 1024
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_FILES = 512
MAX_SCAN_ENTRIES = 25_000
MAX_SCAN_DEPTH = 20
MAX_REMOTE_BYTES = 8 * 1024 * 1024
IGNORED_DIRS = frozenset({".git", ".hg", ".svn", ".venv", "__pycache__", "node_modules"})
SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
GITHUB_REPO = re.compile(
    r"(?:https://github\.com/)?([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_][A-Za-z0-9_.-]{0,99})(?:/)?\Z"
)


class SourceError(ValueError):
    """Source cannot be read or validated."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    name: str
    description: str
    tags: list[str]
    triggers: list[str]
    sha256: str
    body: str
    files: dict[str, bytes]


class HeaderLoader(yaml.BaseLoader):
    """Parse only ordinary YAML maps, sequences and strings, without aliases."""

    depth = 0

    def compose_node(self, parent, index):
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None or getattr(event, "tag", None) is not None:
            raise SourceError("YAML anchors, aliases and explicit tags are unsupported in SKILL.md")
        self.depth += 1
        if self.depth > 12:
            raise SourceError("SKILL.md frontmatter is nested too deeply")
        try:
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1

    def construct_mapping(self, node, deep=False):
        values = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in values:
                raise SourceError("SKILL.md frontmatter requires unique string keys")
            values[key] = self.construct_object(value_node, deep=deep)
        return values


def _clean_text(value: object, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise SourceError(f"SKILL.md {field} must be text")
    clean = unicodedata.normalize("NFC", " ".join(value.split()))
    if not clean or len(clean) > max_length or any(unicodedata.category(char).startswith("C") for char in clean):
        raise SourceError(f"SKILL.md {field} must contain 1–{max_length} displayable characters")
    return clean


def _phrases(value: object, field: str) -> list[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list) or len(value) > 40:
        raise SourceError(f"SKILL.md {field} must be a list of at most 40 phrases")
    return list(dict.fromkeys(_clean_text(part, field=field, max_length=160) for part in value))


def bundle_digest(files: Mapping[str, bytes]) -> str:
    """Hash filenames and contents with length framing, independent of file order."""
    digest = hashlib.sha256(b"OFSAFAERNI bundle v1\0")
    for name, data in sorted(files.items()):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def parse_skill(files: dict[str, bytes]) -> Snapshot:
    raw = files["SKILL.md"]
    if len(raw) > MAX_SKILL_BYTES:
        raise SourceError("SKILL.md is too large")
    try:
        body = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError("SKILL.md must use UTF-8") from exc
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SourceError("SKILL.md must begin with YAML frontmatter")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---" and not line[0].isspace())
    except StopIteration as exc:
        raise SourceError("SKILL.md YAML frontmatter has no closing delimiter") from exc
    header = "\n".join(lines[1:end])
    if len(header.encode("utf-8")) > MAX_HEADER_BYTES:
        raise SourceError("SKILL.md frontmatter is too large")
    try:
        frontmatter = yaml.load(header, Loader=HeaderLoader)
    except yaml.YAMLError as exc:
        raise SourceError("SKILL.md contains invalid YAML frontmatter") from exc
    if not isinstance(frontmatter, dict):
        raise SourceError("SKILL.md frontmatter must be a mapping")
    name = _clean_text(frontmatter.get("name"), field="name", max_length=64)
    if not SKILL_NAME.fullmatch(name):
        raise SourceError("SKILL.md name must use lowercase ASCII letters, digits and single hyphens")
    description = _clean_text(frontmatter.get("description"), field="description", max_length=1024)
    metadata = frontmatter.get("metadata", {})
    if not isinstance(metadata, dict):
        raise SourceError("SKILL.md metadata must be a mapping")
    tags = _phrases(frontmatter.get("tags", metadata.get("tags", [])), "tags")
    triggers = _phrases(frontmatter.get("triggers", metadata.get("triggers", [])), "triggers")
    return Snapshot(name, description, tags, triggers, bundle_digest(files), body, files)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def local_root(root: Path) -> Path:
    """Normalize an explicit root without resolving a symlink or junction."""
    result = Path(os.path.abspath(root.expanduser()))
    for parent in (*reversed(result.parents), result):
        info = parent.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise SourceError(f"Expected an ordinary directory, without links: {parent}")
    return result


def _children(directory: Path, limit: int) -> list[tuple[Path, os.stat_result]]:
    items = []
    local_root(directory)
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(items) >= limit:
                raise SourceError(f"Directory scan limit reached: {directory}")
            path = Path(entry.path)
            items.append((path, path.lstat()))
    return sorted(items, key=lambda item: item[0].name)


def find_local_skills(root: Path) -> Iterator[Path]:
    """Find skill roots without treating examples inside a skill as installed."""
    directories = [(local_root(root), 0)]
    remaining = MAX_SCAN_ENTRIES
    while directories:
        directory, depth = directories.pop()
        children = _children(directory, remaining)
        remaining -= len(children)
        if any(path.name == "SKILL.md" for path, _ in children):
            yield directory / "SKILL.md"
            continue
        if depth >= MAX_SCAN_DEPTH:
            continue
        for child, info in reversed(children):
            if child.name not in IGNORED_DIRS and stat.S_ISDIR(info.st_mode) and not _is_link(info):
                directories.append((child, depth + 1))


def _file_key(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink


def _read_local_file(path: Path, expected: os.stat_result, max_size: int) -> bytes:
    if _is_link(expected) or not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
        raise SourceError(f"Skill bundle contains a link or unsupported file: {path.name}")
    if expected.st_size > max_size:
        raise SourceError(f"Skill bundle size limit reached at: {path.name}")
    local_root(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        initial = os.fstat(handle.fileno())
        local_root(path.parent)
        if not stat.S_ISREG(initial.st_mode) or _file_key(initial) != _file_key(expected):
            raise SourceError(f"Skill bundle changed during read: {path.name}")
        data = handle.read(max_size + 1)
        final = os.fstat(handle.fileno())
    local_root(path.parent)
    current = path.lstat()
    # On Windows Python's pathname and descriptor stats can report different
    # ctime semantics. Compare ctime only between like calls.
    if (
        _is_link(current)
        or _file_key(current) != _file_key(final)
        or _file_key(initial) != _file_key(final)
        or current.st_ctime_ns != expected.st_ctime_ns
        or initial.st_ctime_ns != final.st_ctime_ns
    ):
        raise SourceError(f"Skill bundle changed during read: {path.name}")
    if len(data) > max_size:
        raise SourceError(f"Skill bundle size limit reached at: {path.name}")
    return data


def snapshot_local(skill_path: Path) -> Snapshot:
    directory = local_root(skill_path.parent)
    if skill_path.name != "SKILL.md":
        raise SourceError("SKILL.md no longer exists")
    remaining = MAX_BUNDLE_BYTES
    entries_left = MAX_BUNDLE_FILES
    directories = [(directory, 0)]
    files = {}
    observed = {}
    while directories:
        current, depth = directories.pop()
        if depth > MAX_SCAN_DEPTH:
            raise SourceError("Skill bundle is nested too deeply")
        observed[current] = current.lstat()
        children = _children(current, entries_left)
        entries_left -= len(children)
        for path, info in children:
            if _is_link(info):
                raise SourceError(f"Skill bundle contains a link or junction: {path.name}")
            if stat.S_ISDIR(info.st_mode):
                if path.name not in IGNORED_DIRS:
                    directories.append((path, depth + 1))
                continue
            name = path.relative_to(directory).as_posix()
            maximum = min(MAX_SKILL_BYTES, remaining) if name == "SKILL.md" else remaining
            data = _read_local_file(path, info, maximum)
            remaining -= len(data)
            files[name] = data
            observed[path] = info
    if "SKILL.md" not in files:
        raise SourceError("SKILL.md disappeared during scan")
    local_root(directory)
    for path, before in observed.items():
        after = path.lstat()
        if _is_link(after) or _file_key(after) != _file_key(before) or after.st_ctime_ns != before.st_ctime_ns:
            raise SourceError(f"Skill bundle changed during scan: {path.name}")
    return parse_skill(files)


class _NoRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _http_bytes(url: str, maximum: int) -> bytes:
    req = request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "OFSAFAERNI"})
    try:
        with request.build_opener(_NoRedirects()).open(req, timeout=6) as response:
            length = response.headers.get("Content-Length", "")
            if length.isascii() and length.isdigit() and int(length) > maximum:
                raise SourceError("GitHub response is too large")
            data = response.read(maximum + 1)
    except error.HTTPError as exc:
        raise SourceError(f"GitHub returned HTTP {exc.code}") from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise SourceError("Could not read GitHub source") from exc
    if len(data) > maximum:
        raise SourceError("GitHub response is too large")
    return data


def _github_json(url: str) -> dict:
    try:
        parsed = json.loads(_http_bytes(url, MAX_REMOTE_BYTES))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError("Invalid GitHub response") from exc
    if not isinstance(parsed, dict):
        raise SourceError("Invalid GitHub response")
    return parsed


def _valid_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return bool(
        len(path) <= 600
        and "\\" not in path
        and not posix.is_absolute()
        and len(posix.parts) <= MAX_SCAN_DEPTH + 1
        and posix.as_posix() == path
        and not any(part in {"..", "."} for part in posix.parts)
        and not any(unicodedata.category(char).startswith("C") for char in path)
    )


def github_skills(repo: str, ref: str | None, limit: int) -> tuple[str, str, list[dict], bool]:
    match = GITHUB_REPO.fullmatch(repo.strip())
    if not match:
        raise SourceError("Use a public GitHub repository as OWNER/REPO or https://github.com/OWNER/REPO")
    owner, name = match.groups()
    canonical = f"{owner}/{name.removesuffix('.git')}".lower()
    ref = ref or "HEAD"
    if (
        len(ref) > 255
        or not re.fullmatch(r"[A-Za-z0-9_.\-/]+", ref)
        or ref.startswith(("/", "-"))
        or ".." in ref
        or any(part in {"", "."} for part in ref.split("/"))
    ):
        raise SourceError("Invalid GitHub branch, tag or commit")
    if not 1 <= limit <= 100:
        raise SourceError("GitHub skill limit must be between 1 and 100")
    endpoint = f"https://api.github.com/repos/{canonical}"
    info = _github_json(f"{endpoint}/commits/{parse.quote(ref, safe='')}")
    try:
        revision = info["sha"]
        tree_sha = info["commit"]["tree"]["sha"]
    except (KeyError, TypeError) as exc:
        raise SourceError("GitHub did not return an immutable commit and tree") from exc
    if any(not isinstance(value, str) or not GIT_SHA.fullmatch(value) for value in (revision, tree_sha)):
        raise SourceError("GitHub did not return an immutable commit and tree")
    tree = _github_json(f"{endpoint}/git/trees/{tree_sha}?recursive=1")
    if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
        raise SourceError("GitHub tree is incomplete; collect a reviewed local checkout instead")
    paths = {}
    for item in tree["tree"]:
        if not isinstance(item, dict) or item.get("mode") not in {"100644", "100755"} or item.get("type") != "blob":
            continue
        path, sha, size = item.get("path"), item.get("sha"), item.get("size")
        if not isinstance(path, str) or path.split("/")[-1] != "SKILL.md" or not _valid_relative(path):
            continue
        if not isinstance(sha, str) or not GIT_SHA.fullmatch(sha) or type(size) is not int or not 0 < size <= MAX_SKILL_BYTES:
            continue
        paths[path] = {"path": path, "sha": sha, "size": size}
    return canonical, revision, [paths[path] for path in sorted(paths)[:limit]], len(paths) > limit


def snapshot_github(repo: str, revision: str, entry: dict) -> tuple[str, Snapshot]:
    relative = parse.quote(entry["path"], safe="/")
    data = _http_bytes(f"https://raw.githubusercontent.com/{repo}/{revision}/{relative}", entry["size"])
    # Git tree integrity uses the repository's Git object SHA, separate from the
    # catalog's SHA-256 content identifier.
    framed = f"blob {len(data)}\0".encode("ascii") + data
    sha = hashlib.sha1(framed, usedforsecurity=False).hexdigest()
    if sha != entry["sha"] or len(data) != entry["size"]:
        raise SourceError("GitHub source failed pinned blob integrity verification")
    return f"https://github.com/{repo}/blob/{revision}/{relative}", parse_skill({"SKILL.md": data})
