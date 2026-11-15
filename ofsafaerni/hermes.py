"""Discover a Hermes skill library and export reviewed portable bundles."""

from __future__ import annotations

import hmac
import os
import re
import stat
import sys
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .learning import DRAFT_RECEIPT, SKILL_DOCUMENT, _RESERVED_NAMES, _publish_skill_dir, _skill_name
from .sources import _is_link, bundle_digest, local_root


_MAX_CONFIG_BYTES = 256 * 1024
_PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_WINDOWS_FORBIDDEN = frozenset('<>:"\\|?*')


def _config_path(value: str, home: Path) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else home / path


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _profile_roots(home: Path) -> list[Path]:
    home = _absolute_path(home)
    if home.exists() or home.is_symlink():
        local_root(home)
    local = home / "skills"
    roots = [local]
    return roots + [path for path in _configured_roots(home) if path != local]


def _native_home() -> Path:
    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    if sys.platform == "win32":
        default = Path.home() / "AppData" / "Local"
        base = os.environ.get("LOCALAPPDATA", "").strip()
        return (Path(base) if base else default) / ("hermes" + suffix)
    return Path.home() / (".hermes" + suffix)


def _active_home(home: Path) -> Path:
    home = _absolute_path(home)
    if home.parent.name.lower() == "profiles" or not (home.exists() or home.is_symlink()):
        return home
    local_root(home)
    marker = home / "active_profile"
    try:
        info = marker.lstat()
        if _is_link(info) or not stat.S_ISREG(info.st_mode):
            raise ValueError("Hermes active_profile must be a regular file without links")
        with marker.open("rb") as handle:
            raw = handle.read(257)
    except FileNotFoundError:
        return home
    if len(raw) > 256:
        raise ValueError("Hermes active_profile exceeds 256 bytes")
    name = raw.decode("utf-8-sig").strip().lower()
    if not name or name == "default":
        return home
    if not _PROFILE_NAME.fullmatch(name) or name in _RESERVED_NAMES:
        raise ValueError("Hermes active_profile contains an invalid profile name")
    profile = home / "profiles" / name
    try:
        return local_root(profile)
    except FileNotFoundError as error:
        raise ValueError(f"Hermes active profile directory does not exist: {name}") from error


def hermes_skill_roots(home: Path | None = None) -> list[Path]:
    """Return the profile's skills folder and configured creation/external roots.

    A supplied home is the Hermes profile directory. Reading config.yaml does
    not call Hermes, start a model or write to its profile.
    """
    if home is not None:
        return _profile_roots(Path(home))
    configured_home = os.environ.get("HERMES_HOME", "").strip()
    if configured_home:
        return _profile_roots(_active_home(Path(os.path.expandvars(configured_home))))
    roots = _profile_roots(_active_home(_native_home()))
    if sys.platform == "win32":
        legacy = Path.home() / ".hermes"
        if legacy.is_dir():
            roots.extend(_profile_roots(_active_home(legacy)))
    return list(dict.fromkeys(roots))


def _configured_roots(home: Path) -> list[Path]:
    config_path = home / "config.yaml"
    try:
        info = config_path.lstat()
        if _is_link(info) or not stat.S_ISREG(info.st_mode):
            raise ValueError("Hermes config must be a regular file without links")
        with config_path.open("rb") as handle:
            contents = handle.read(_MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return []
    if len(contents) > _MAX_CONFIG_BYTES:
        raise ValueError("Hermes config exceeds 256 KiB")
    try:
        data = yaml.safe_load(contents)
    except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError("cannot read Hermes config as YAML") from exc
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ValueError("Hermes config must be a YAML mapping")
    settings = data.get("skills")
    if settings is None:
        return []
    if not isinstance(settings, dict):
        raise ValueError("Hermes skills settings must be a YAML mapping")
    create_dir = settings.get("create_dir")
    external_dirs = settings.get("external_dirs", [])
    if create_dir is not None and not isinstance(create_dir, str):
        raise ValueError("Hermes skills.create_dir must be a path string")
    if not isinstance(external_dirs, list) or len(external_dirs) > 128:
        raise ValueError("Hermes skills.external_dirs must be a list of up to 128 paths")
    if any(not isinstance(item, str) or not item.strip() for item in external_dirs):
        raise ValueError("Hermes external skill paths must be nonempty strings")
    roots = [_absolute_path(_config_path(item.strip(), home)) for item in (create_dir, *external_dirs) if item and item.strip()]
    return list(dict.fromkeys(roots))


def _validate_portable_files(files: Mapping[str, bytes]) -> None:
    if SKILL_DOCUMENT not in files:
        raise ValueError("the bundle is missing SKILL.md")
    canonical_paths = {}
    for relative, contents in files.items():
        if not isinstance(relative, str) or not isinstance(contents, bytes):
            raise ValueError("bundle entries must map portable names to bytes")
        parts = relative.split("/")
        for index, part in enumerate(parts, 1):
            # Windows forbids reserved device names even when followed by an extension.
            if (
                part in {"", ".", ".."}
                or part.endswith((".", " "))
                or part.split(".", 1)[0].rstrip().casefold() in _RESERVED_NAMES
                or any(char in _WINDOWS_FORBIDDEN or ord(char) < 32 for char in part)
            ):
                raise ValueError(f"nonportable bundle path: {relative!r}")
            prefix = "/".join(parts[:index])
            canonical = unicodedata.normalize("NFC", prefix).casefold()
            entry = (prefix, index == len(parts))
            if canonical in canonical_paths and canonical_paths[canonical] != entry:
                raise ValueError(f"colliding bundle path: {relative!r}")
            canonical_paths[canonical] = entry


def export_skill(record: Mapping[str, Any], target_dir: Path) -> dict[str, Any]:
    """Install a verified Catalog.bundle snapshot into a Hermes-compatible root.

    Supply the approved catalog metadata with its verified `files` mapping.
    The local learning receipt is retained in the source library.
    """
    if record.get("approved") is not True:
        raise ValueError("export requires an approved catalog record")
    name = _skill_name(record.get("name"))
    expected = record.get("sha256")
    files = record.get("files")
    if not isinstance(expected, str) or not isinstance(files, Mapping):
        raise ValueError("export requires a verified bundle snapshot")
    files = dict(files)
    _validate_portable_files(files)
    actual = bundle_digest(files)
    if not expected.isascii() or not hmac.compare_digest(expected, actual):
        raise ValueError("bundle bytes do not match the approved digest")
    portable_files = {name: data for name, data in files.items() if name != DRAFT_RECEIPT}
    destination = _publish_skill_dir(Path(target_dir), name, portable_files)
    return {
        "name": name,
        "path": str(destination / SKILL_DOCUMENT),
        "files": len(portable_files),
        "source_sha256": actual,
        "sha256": bundle_digest(portable_files),
    }
