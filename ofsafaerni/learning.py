"""Turn a curated and verified experience into a portable draft skill."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DRAFT_RECEIPT = ".ofsafaerni-draft.json"
SKILL_DOCUMENT = "SKILL.md"
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_STEPS_MAX = 24
_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{suffix}" for prefix in ("com", "lpt") for suffix in "123456789¹²³"
}
_HEADINGS = {
    "is": ("Hvenær á færnin við?", "Verkferill", "Sannprófun"),
    "en": ("When to use", "Procedure", "Verification"),
}


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    value = unicodedata.normalize("NFC", value.strip())
    if not value or len(value) > limit:
        raise ValueError(f"{label} must contain 1–{limit} characters")
    if any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in value):
        raise ValueError(f"{label} must be a single line without control characters")
    return value


def _skill_name(value: Any) -> str:
    name = _text(value, "name", 64)
    if not _SLUG.fullmatch(name) or name in _RESERVED_NAMES:
        raise ValueError("name must be a portable lowercase, hyphen-separated skill name")
    return name


def _text_items(spec: Mapping[str, Any], key: str, *, required: bool) -> list[str]:
    values = spec.get(key, [])
    if not isinstance(values, list) or len(values) > _STEPS_MAX or (required and not values):
        raise ValueError(f"{key} must contain {1 if required else 0}–{_STEPS_MAX} text items")
    limit = 100 if key in {"tags", "triggers"} else 600
    return [_text(value, f"{key}[{i}]", limit) for i, value in enumerate(values)]


def _publish_skill_dir(root: Path, name: str, files: Mapping[str, bytes]) -> Path:
    """Reserve a new path and make the skill discoverable after its files arrive."""
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / name
    temporary: Path | None = None
    written: list[Path] = []
    folders: list[Path] = []
    destination.mkdir(mode=0o700)
    try:
        for relative in sorted(set(files) - {SKILL_DOCUMENT}):
            target = destination / relative
            for parent in reversed(target.parents):
                if parent != destination and parent.is_relative_to(destination) and not parent.exists():
                    parent.mkdir(mode=0o700)
                    folders.append(parent)
            with target.open("xb") as handle:
                written.append(target)
                handle.write(files[relative])
        with tempfile.NamedTemporaryFile(prefix=".ofsafaerni-", dir=destination, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(files[SKILL_DOCUMENT])
        os.rename(temporary, destination / SKILL_DOCUMENT)
    except (OSError, ValueError, KeyError):
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        for target in reversed(written):
            target.unlink(missing_ok=True)
        for folder in reversed(folders):
            folder.rmdir()
        destination.rmdir()
        raise
    return destination


def draft_skill(spec: Mapping[str, Any], output_dir: Path) -> dict[str, Any]:
    """Write a draft SKILL.md and a local receipt from a structured summary.

    Fields: name, description, task, evidence, steps, checks, and optionally
    language (is/en), tags, triggers, and source. Task and steps should describe
    the reusable workflow. Evidence and source remain in the local receipt.
    """
    if not isinstance(spec, Mapping):
        raise ValueError("the draft input must be a JSON object")
    name = _skill_name(spec.get("name"))
    description = _text(spec.get("description"), "description", 1024)
    if "<" in description or ">" in description or description.startswith("[TODO:"):
        raise ValueError("description must be complete text without angle brackets")
    task = _text(spec.get("task"), "task", 1500)
    evidence = _text(spec.get("evidence"), "evidence", 2000)
    source = _text(spec["source"], "source", 300) if "source" in spec else None
    steps = _text_items(spec, "steps", required=True)
    checks = _text_items(spec, "checks", required=True)
    language = spec.get("language", "is")
    if not isinstance(language, str) or language not in _HEADINGS:
        raise ValueError("language must be 'is' or 'en'")

    frontmatter = ["name: " + json.dumps(name), "description: " + json.dumps(description, ensure_ascii=False)]
    metadata = []
    for key in ("tags", "triggers"):
        values = _text_items(spec, key, required=False)
        if any("," in value for value in values):
            raise ValueError(f"{key} entries cannot contain commas")
        if values:
            metadata.append("  " + key + ": " + json.dumps(", ".join(dict.fromkeys(values)), ensure_ascii=False))
    if metadata:
        frontmatter.extend(["metadata:", *metadata])

    when, procedure, verification = _HEADINGS[language]
    contents = "\n".join([
        "---", *frontmatter, "---", "", f"# {name}", "", f"## {when}", "", task, "",
        f"## {procedure}", "", *(f"{i}. {step}" for i, step in enumerate(steps, 1)),
        "", f"## {verification}", "", *(f"- {item}" for item in checks), "",
    ]).encode("utf-8")
    digest = hashlib.sha256(contents).hexdigest()
    receipt = {
        "schema": "ofsafaerni.draft/v1",
        "name": name,
        "sha256": digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "evidence": evidence,
        "approved": False,
    }
    files = {
        SKILL_DOCUMENT: contents,
        DRAFT_RECEIPT: (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    }
    destination = _publish_skill_dir(Path(output_dir), name, files)
    return {
        "name": name,
        "path": str(destination / SKILL_DOCUMENT),
        "receipt": str(destination / DRAFT_RECEIPT),
        "sha256": digest,
        "approved": False,
    }
