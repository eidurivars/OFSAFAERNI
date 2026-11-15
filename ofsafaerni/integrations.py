"""Codex hook protocol and configuration. No settings are written here."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO

from .catalog import Catalog
from .routing import route

_MAX_HOOK_BYTES = 65_536
_SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", re.ASCII)


def codex_roots(project: Path | None = None) -> list[Path]:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    roots = [home / "skills", Path.home() / ".agents" / "skills"]
    if project is not None:
        project_root = project.resolve()
        roots[:0] = [project_root / ".agents" / "skills", project_root / ".codex" / "skills"]
    return list(dict.fromkeys(root.absolute() for root in roots if root.is_dir()))


def hook_config(database: Path, *, shell: str | None = None) -> dict:
    args = [
        str(Path(sys.executable).absolute()), "-I", "-m", "ofsafaerni",
        "--db", str(database.absolute()), "codex-hook",
    ]
    if any(any(char in arg for char in "\x00\r\n") for arg in args):
        raise ValueError("Choose an interpreter and database path without control characters.")
    shell = shell or ("powershell" if os.name == "nt" else "sh")
    if shell == "powershell":
        command = "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in args)
    elif shell == "cmd":
        if any(re.search(r'[%!&|^<>"\r\n]', arg) for arg in args):
            raise ValueError("Choose an interpreter and database path without Windows shell metacharacters.")
        command = subprocess.list2cmdline(args)
    elif shell == "sh":
        command = shlex.join(args)
    else:
        raise ValueError(f"Unsupported hook shell: {shell}")
    return {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": command, "timeout": 15}]}]}}


def route_catalog(
    query: str, catalog: Catalog, *, limit: int = 3, metadata: list[dict] | None = None
) -> dict:
    """Rank metadata first and verify the limited, visible shortlist against disk."""
    inventory = catalog.routing_items() if metadata is None else metadata
    records = {record["id"]: record for record in inventory}
    for _ in range(3):
        result = route(query, list(records.values()), limit=limit)
        ids = {item["id"] for item in result["candidates"] + result["selected"]}
        stale = set()
        for skill_id in ids:
            current = catalog.get(skill_id)
            if not current or not current["approved"] or current["sha256"] != records[skill_id]["sha256"]:
                stale.add(skill_id)
        if not stale:
            return result
        for skill_id in stale:
            records.pop(skill_id)
    return {
        "decision": "bypass",
        "selected": [],
        "candidates": [],
        "explanations": ["The shortlist changed during verification. Review changed catalog entries before use."],
        "diagnostics": result["diagnostics"],
    }


def codex_hook(stream: BinaryIO, database: Path) -> dict:
    """Suggest approved IDs only. Never echo prompt text or skill descriptions."""
    raw = stream.read(_MAX_HOOK_BYTES + 1)
    if len(raw) > _MAX_HOOK_BYTES or not database.is_file():
        return {}
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError):
        return {}
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit":
        return {}
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}
    with Catalog(database) as catalog:
        result = route_catalog(prompt, catalog)
    if result["decision"] != "use_skill":
        return {}
    names = []
    for candidate in result["selected"]:
        name, skill_id = candidate.get("name"), candidate.get("id")
        if isinstance(name, str) and isinstance(skill_id, str) and _SAFE_LABEL.fullmatch(name) and _SAFE_LABEL.fullmatch(skill_id):
            names.append(f"{name} ({skill_id})")
    if not names:
        return {}
    context = (
        "OFSAFAERNI matched approved catalog entries: " + ", ".join(names)
        + ". If relevant, use the OFSAFAERNI load command with these IDs and the same --db "
        "value as the configured hook. "
        "Follow the host's permissions and the user's task."
    )
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
