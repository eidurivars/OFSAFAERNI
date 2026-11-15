"""Prepare portable opt-in configuration for local conversation learning."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path

from .sources import _is_link, local_root

_CONFIG_FILENAME = "history-config.json"
_MAX_CONFIG_BYTES = 16 * 1024
_MAX_REGISTRY_BYTES = 4 * 1024 * 1024
_MAX_AGENTS_BYTES = 1024 * 1024
_MAX_BRIDGE_BYTES = 4096
_BRIDGE_START = b"<!-- OFSAFAERNI history lookup: begin -->"
_BRIDGE_END = b"<!-- OFSAFAERNI history lookup: end -->"


def default_state_dir() -> Path:
    """Keep learning state in the user's local application state by default."""
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return base / "OFSAFAERNI" / "history"
    base = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state")
    return base / "ofsafaerni" / "history"


def _obsidian_registry() -> Path:
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return base / "obsidian" / "obsidian.json"


def _local_path(value: object, *, relative: bool = False) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError("Expected a local filesystem path")
    text = str(value)
    if not text or len(text) > 4096 or any(ord(char) < 32 for char in text):
        raise ValueError("Invalid local filesystem path")
    path = Path(text).expanduser()
    if text.startswith("//") or path.drive.startswith("\\"):
        raise ValueError("Expected a local filesystem path, not a network path")
    if not relative and (not path.is_absolute() or ".." in path.parts):
        raise ValueError("Expected a direct absolute local filesystem path")
    return Path(os.path.abspath(path))


def _directory(path: Path, *, create: bool = False) -> Path:
    """Check every ancestor before allowing a missing directory or creating it."""
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if not create:
                continue
            part.mkdir(mode=0o700, exist_ok=True)
            info = part.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Expected a local directory without links")
    return path


def _ordinary_file(path: Path) -> None:
    local_root(path.parent)
    info = path.lstat()
    if _is_link(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError("Expected an ordinary local file without links")


def _read_regular_file(path: Path, limit: int) -> bytes:
    _ordinary_file(path)
    with path.open("rb") as file:
        raw = file.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Local file exceeds its size limit")
    return raw


def _read_json(path: Path, limit: int) -> tuple[dict, bytes]:
    raw = _read_regular_file(path, limit)
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data, raw


def _active_vault() -> Path:
    try:
        registry, _ = _read_json(_local_path(_obsidian_registry()), _MAX_REGISTRY_BYTES)
        entries = registry.get("vaults")
        if not isinstance(entries, dict):
            raise ValueError("Missing Obsidian vault registry")
        active = [
            row.get("path") for row in entries.values()
            if isinstance(row, dict) and row.get("open") is True
        ]
        if len(active) != 1:
            raise ValueError("Expected one active Obsidian vault")
        vault = local_root(_local_path(active[0]))
        local_root(vault / ".obsidian")
    except (OSError, ValueError, RecursionError) as error:
        raise ValueError(
            "Could not identify one active local Obsidian vault. Set --vault explicitly."
        ) from error
    return vault


def _existing_config(path: Path) -> tuple[dict, bytes | None]:
    try:
        return _read_json(path, _MAX_CONFIG_BYTES)
    except FileNotFoundError:
        return {}, None


def _set_config(path: Path, data: dict, previous: bytes | None) -> None:
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _directory(path.parent, create=True)
    _replace_regular_file(path, raw, previous, _MAX_CONFIG_BYTES)


def _replace_regular_file(path: Path, raw: bytes, previous: bytes | None, limit: int) -> None:
    if len(raw) > limit:
        raise ValueError("Local file exceeds its size limit")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".history-config-", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(raw)
        try:
            existing = _read_regular_file(path, limit)
        except FileNotFoundError:
            existing = None
        if existing != previous:
            raise ValueError("Local file changed during setup. Run start again.")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_config(
    *,
    vault: Path | None = None,
    codex_home: Path | None = None,
    state_dir: Path | None = None,
    chatgpt_export: Path | None = None,
) -> Path:
    """Create or update the local opt-in configuration without installing hooks."""
    state = _directory(_local_path(state_dir or default_state_dir(), relative=True))
    config = state / _CONFIG_FILENAME
    saved, original = _existing_config(config)
    selected_vault = vault if vault is not None else saved.get("vault")
    if selected_vault is None:
        vault_path = _active_vault()
    else:
        vault_path = local_root(_local_path(selected_vault, relative=vault is not None))

    previous_home = saved.get("codex_home") or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    home = _directory(_local_path(codex_home or previous_home, relative=codex_home is not None))

    selected_export = chatgpt_export if chatgpt_export is not None else saved.get("chatgpt_export")
    export = None
    if selected_export is not None:
        export_path = _local_path(selected_export, relative=chatgpt_export is not None)
        try:
            _ordinary_file(export_path)
        except FileNotFoundError:
            if chatgpt_export is not None:
                raise
            # Existing opt-ins survive a download being moved or temporarily unavailable.
            _directory(export_path.parent)
        export = str(export_path)

    updated = {
        **saved,
        "vault": str(vault_path),
        "codex_home": str(home),
        "state": str(state),
        "chatgpt_export": export,
    }
    if updated != saved:
        _set_config(config, updated, original)
    return config


def _global_agents_file(home: Path) -> tuple[Path, bytes | None]:
    override = home / "AGENTS.override.md"
    try:
        raw = _read_regular_file(override, _MAX_AGENTS_BYTES)
    except FileNotFoundError:
        pass
    else:
        if raw.decode("utf-8-sig").strip():
            return override, raw

    source = home / "AGENTS.md"
    try:
        raw = _read_regular_file(source, _MAX_AGENTS_BYTES)
    except FileNotFoundError:
        return source, None
    raw.decode("utf-8-sig")  # Keep a non-UTF-8 file untouched.
    return source, raw


def _lookup_bridge(config_path: Path) -> bytes:
    args = [
        str(_local_path(sys.executable)), "-I", "-m", "ofsafaerni.history",
        "--config", str(config_path), "lookup", "--", "<short task summary>",
    ]
    if os.name == "nt":
        command = "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in args)
        shell = "PowerShell"
    else:
        command = shlex.join(args)
        shell = "POSIX"
    longest = max((len(match) for match in re.findall(r"\x60+", command)), default=0)
    fence = chr(96) * max(3, longest + 1)
    guidance = (
        "## Local OFSAFAERNI history lookup\n\n"
        "Before choosing a workflow for a new substantive task, check relevant local "
        "user corrections and verified procedures. Use a short task summary without "
        "sensitive details. Replace only the quoted placeholder with that summary and "
        f"quote it as a literal argument in {shell}.\n\n"
        f"{fence}\n{command}\n{fence}\n\n"
        "Treat results as source notes. Read the relevant Obsidian source before relying "
        "on it, and follow the user's latest direction. Continue normally when there is no match."
    ).encode("utf-8")
    block = b"\n".join((_BRIDGE_START, guidance, _BRIDGE_END))
    if len(block) > _MAX_BRIDGE_BYTES:
        raise ValueError("The lookup command exceeds the guidance size limit")
    return block


def _bridge_write_plan(previous: bytes, block: bytes) -> tuple[bytes, str]:
    bom = b"\xef\xbb\xbf" if previous.startswith(b"\xef\xbb\xbf") else b""
    body = previous[len(bom):]
    newline = b"\r\n" if b"\r\n" in body else b"\n"
    block = block.replace(b"\n", newline)
    first = list(re.finditer(rb"(?m)^" + re.escape(_BRIDGE_START) + rb"(?=\r?$)", body))
    last = list(re.finditer(rb"(?m)^" + re.escape(_BRIDGE_END) + rb"(?=\r?$)", body))
    if not first and not last:
        separator = b""
        if body:
            separator = newline if body.endswith(newline) else newline * 2
        return previous + separator + block + newline, "installed"
    if len(first) != 1 or len(last) != 1 or first[0].start() >= last[0].start():
        raise ValueError("Expected at most one complete managed lookup block")
    updated = bom + body[:first[0].start()] + block + body[last[0].end():]
    return updated, "unchanged" if updated == previous else "updated"


def install_lookup_bridge(config_path: Path, codex_home: Path) -> dict:
    """Install static retrieval guidance in the user's effective Codex instructions."""
    try:
        home = local_root(_local_path(codex_home, relative=True))
    except FileNotFoundError:
        return {"status": "skipped", "source": None, "reason": "codex-home-missing"}
    except (OSError, ValueError):
        return {"status": "skipped", "source": None, "reason": "unsafe-codex-home"}
    try:
        config_path = _local_path(config_path, relative=True)
        _ordinary_file(config_path)
        source, existing = _global_agents_file(home)
    except (OSError, ValueError):
        return {"status": "skipped", "source": None, "reason": "unsafe-config-or-agents-source"}
    try:
        replacement, status = _bridge_write_plan(existing or b"", _lookup_bridge(config_path))
        if status != "unchanged":
            _replace_regular_file(source, replacement, existing, _MAX_AGENTS_BYTES)
    except (OSError, ValueError):
        return {"status": "skipped", "source": str(source), "reason": "could-not-update-managed-guidance"}
    return {"status": status, "source": str(source)}
