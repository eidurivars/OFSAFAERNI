"""OFSAFAERNI command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .catalog import Catalog
from .hermes import export_skill, hermes_skill_roots
from .integrations import codex_hook, codex_roots, hook_config, route_catalog
from .learning import draft_skill

_MAX_JSON_BYTES = 131_072
_MAX_SUBTASKS = 30


def default_database() -> Path:
    explicit = os.environ.get("OFSAFAERNI_DB")
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        parent = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return parent / "OFSAFAERNI" / "catalog.sqlite3"
    parent = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return parent / "ofsafaerni" / "catalog.sqlite3"


def _read_object(path: Path) -> dict:
    with path.open("rb") as file:
        data = file.read(_MAX_JSON_BYTES + 1)
    if len(data) > _MAX_JSON_BYTES:
        raise ValueError("Input JSON exceeds 128 KiB.")
    value = json.loads(data.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Input must be a JSON object.")
    return value


def _route_limit(value: str) -> int:
    limit = int(value)
    if not 1 <= limit <= 10:
        raise argparse.ArgumentTypeError("Expected a limit from 1 to 10.")
    return limit


def _assign(spec: dict, catalog: Catalog, limit: int) -> dict:
    subtasks = spec.get("subtasks")
    if not isinstance(subtasks, list) or not 1 <= len(subtasks) <= _MAX_SUBTASKS:
        raise ValueError(f"Provide 1–{_MAX_SUBTASKS} subtasks.")
    seen = set()
    assignments = []
    inventory = catalog.routing_items()
    for item in subtasks:
        if not isinstance(item, dict):
            raise ValueError("Each subtask must be an object.")
        subtask_id, task, agent = item.get("id"), item.get("task"), item.get("agent")
        if not isinstance(subtask_id, str) or not 1 <= len(subtask_id.strip()) <= 80 or len(subtask_id) > 80:
            raise ValueError("Each subtask needs a short, non-empty id.")
        if not isinstance(task, str) or not 1 <= len(task.strip()) <= 6_000 or len(task) > 6_000:
            raise ValueError("Each subtask needs a task of at most 6,000 characters.")
        if agent is not None and (not isinstance(agent, str) or not 1 <= len(agent.strip()) <= 80 or len(agent) > 80):
            raise ValueError("An optional agent label must be 1–80 characters.")
        if subtask_id in seen:
            raise ValueError(f"Duplicate subtask id: {subtask_id!r}")
        seen.add(subtask_id)
        choice = route_catalog(task, catalog, limit=limit, metadata=inventory)
        assignments.append({"id": subtask_id, "agent": agent, **choice})
    return {"assignments": assignments}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ofsafaerni", description="Local bilingual skill collection and routing.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", type=Path, help="Local catalog database.")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="Discover local skills or stage public GitHub metadata for review.")
    source = collect.add_mutually_exclusive_group(required=True)
    source.add_argument("--root", type=Path, action="append", help="Explicit local root, repeatable.")
    source.add_argument("--codex", action="store_true", help="Scan known Codex roots.")
    source.add_argument("--hermes", action="store_true", help="Scan the active Hermes profile skills directory.")
    source.add_argument("--github", metavar="OWNER/REPO", help="Stage a public GitHub repository.")
    collect.add_argument("--project", type=Path, help="Project root for --codex.")
    collect.add_argument("--ref", help="Branch, tag or commit for --github.")
    collect.add_argument("--limit", type=int, default=60, help="Maximum skill files for --github (default 60).")

    listing = commands.add_parser("list", help="List approved skills.")
    listing.add_argument("--all", action="store_true", help="Include pending and changed entries.")
    for command, help_text in (
        ("review", "Read a skill for review, including pending entries."),
        ("approve", "Approve an exact reviewed local bundle."),
        ("revoke", "Revoke approval for a skill."),
        ("load", "Read an approved skill after verifying the current bundle."),
    ):
        action = commands.add_parser(command, help=help_text)
        action.add_argument("id")
        if command == "approve":
            action.add_argument("--sha256", required=True)

    choose = commands.add_parser("route", help="Select approved skills for a request.")
    choose.add_argument("query")
    choose.add_argument("--limit", type=_route_limit, default=3)
    assign = commands.add_parser("assign", help="Choose skills for every named subtask in a JSON file.")
    assign.add_argument("file", type=Path)
    assign.add_argument("--limit", type=_route_limit, default=3)

    feedback = commands.add_parser("feedback", help="Record a verified outcome for a skill.")
    feedback.add_argument("id")
    feedback.add_argument("outcome", choices=("success", "failure"))
    learn = commands.add_parser("learn", help="Write a skill draft from a curated successful workflow.")
    learn.add_argument("file", type=Path)
    learn.add_argument("--out", type=Path, required=True)
    export = commands.add_parser("export", help="Export a verified local bundle as portable Agent Skills.")
    export.add_argument("id")
    export.add_argument("--out", type=Path, required=True, help="The explicit destination skills directory.")
    hook_setup = commands.add_parser("hook-config", help="Print a Codex UserPromptSubmit hook configuration.")
    hook_setup.add_argument("--shell", choices=("powershell", "cmd", "sh"), help="Shell used by the Codex session.")
    commands.add_parser("codex-hook", help="Handle a Codex hook event from stdin.")
    commands.add_parser("history", help="Learn from local conversation history.")
    return parser


def _collect(args: argparse.Namespace, catalog: Catalog) -> dict:
    if args.github:
        if args.project:
            raise ValueError("--project is supported with --codex.")
        if not 1 <= args.limit <= 100:
            raise ValueError("--limit must be between 1 and 100.")
        return catalog.collect_github(args.github, ref=args.ref, limit=args.limit)
    if args.ref or args.limit != 60:
        raise ValueError("--ref and --limit require --github.")
    if args.project and not args.codex:
        raise ValueError("--project is supported with --codex.")
    if args.hermes:
        detected = hermes_skill_roots()
        roots = [root for root in detected if root.is_dir()]
        return {
            "sources": [catalog.collect_local(root, origin="hermes") for root in roots],
            "unavailable": [str(root) for root in detected if root not in roots],
        }
    elif args.codex:
        roots, origin = codex_roots(args.project or Path.cwd()), "codex"
    else:
        roots, origin = args.root, "local"
    return {"sources": [catalog.collect_local(root, origin=origin) for root in roots]}


def _run(args: argparse.Namespace, database: Path) -> dict:
    command = args.command
    if command == "learn":
        return draft_skill(_read_object(args.file), args.out)
    if command == "hook-config":
        return hook_config(database, shell=args.shell)
    if command == "codex-hook":
        return codex_hook(sys.stdin.buffer, database)
    with Catalog(database) as catalog:
        if command == "collect":
            return _collect(args, catalog)
        if command == "list":
            return {"skills": catalog.items(include_pending=args.all)}
        if command == "review":
            return catalog.review(args.id)
        if command == "approve":
            return catalog.approve(args.id, args.sha256)
        if command == "revoke":
            return catalog.revoke(args.id)
        if command == "load":
            return catalog.load(args.id)
        if command == "route":
            return route_catalog(args.query, catalog, limit=args.limit)
        if command == "assign":
            return _assign(_read_object(args.file), catalog, args.limit)
        if command == "feedback":
            return catalog.feedback(args.id, args.outcome)
        if command == "export":
            record, files = catalog.bundle(args.id)
            return export_skill({**record, "files": files}, args.out)
    raise ValueError(f"Unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "history":
        from .history import main as history_main

        return history_main(argv[1:])
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = _parser().parse_args(argv)
    database = (args.db or default_database()).expanduser().absolute()
    try:
        result = _run(args, database)
    except (OSError, ValueError, sqlite3.Error, RecursionError) as error:
        if args.command == "codex-hook":
            print("{}")
            return 0
        print(f"ofsafaerni: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=None if args.command == "codex-hook" else 2))
    return 0
