"""Learn from local conversation history and save concise, sourced notes."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata

from .history_rules import find_lessons
from .history_sources import coverage, iter_codex_turns
from .language import GENERIC_WORDS, features
from .providers import SUPPORTED, reader_for
from .sources import local_root

_MARKER = "<!-- managed by OFSAFAERNI conversation learning -->"
_NOTES = Path("07 Skills") / "OFSAFAERNI - samtalslaerdomur"
_KINDS = {
    "preference": "Óskir notanda",
    "correction": "Leiðréttingar notanda",
    "workflow": "Endurnýtanlegt verklag",
}
_SCOPES = {"global": "almennt", "project": "verkefni", "session": "spjall"}
_IDENTIFIER = re.compile(r"[\w:.@+-]{1,160}\Z", re.UNICODE)
_RULE_REVISION = "rules-v3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL, thread TEXT NOT NULL, turn TEXT NOT NULL,
    digest TEXT NOT NULL, PRIMARY KEY (source, thread, turn, digest)
);
CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL,
    text TEXT NOT NULL, evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS occurrences (
    lesson TEXT NOT NULL REFERENCES lessons(id), source TEXT NOT NULL, thread TEXT NOT NULL,
    turn TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (lesson,source,thread,turn)
);
CREATE TABLE IF NOT EXISTS chatgpt_index (
    thread TEXT PRIMARY KEY, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS export_checkpoints (
    identity TEXT PRIMARY KEY, signature TEXT NOT NULL
);
"""


def _digest(*values: str) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()


def _path(data: dict, name: str, *, optional: bool = False) -> Path | None:
    value = data.get(name)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(f"Expected an absolute {name} path")
    return Path(value)


def read_config(path: Path) -> dict:
    with path.open("rb") as file:
        raw = file.read(16_385)
    if len(raw) > 16_384:
        raise ValueError("History configuration exceeds 16 KiB")
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Expected a configuration object")
    providers = data.get("providers", {})
    if not isinstance(providers, dict) or any(name not in SUPPORTED for name in providers):
        raise ValueError("Unrecognized history providers")
    return {
        "codex_home": _path(data, "codex_home"),
        "vault": _path(data, "vault"),
        "state": _path(data, "state") / "history.sqlite3",
        "chatgpt_export": _path(data, "chatgpt_export", optional=True),
        "providers": {name: _path(providers, name) for name in providers},
    }


def _stamp(value: object) -> str:
    if type(value) in {int, float} and 0 < value < 10 ** 14:
        try:
            seconds = value / 1000 if value >= 10 ** 11 else value
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    if isinstance(value, str) and len(value) < 60:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    return ""


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=25)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(_SCHEMA)
    return connection


def _text_key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def _record(connection: sqlite3.Connection, record: dict, source: str) -> int | None:
    thread, turn = record.get("thread_id"), record.get("turn_id")
    if any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in (thread, turn)):
        return None
    user, assistant = record.get("user"), record.get("assistant") or ""
    if not isinstance(user, str) or not user or not isinstance(assistant, str):
        return None
    if len(user) > 120_000 or len(assistant) > 120_000:
        return None
    checks = record.get("verified_checks", 0)
    checks = checks if type(checks) is int and 0 < checks <= 1000 else 0
    digest = _digest(_RULE_REVISION, user, assistant, str(checks))
    if not connection.execute(
        "INSERT OR IGNORE INTO seen VALUES (?,?,?,?)", (source, thread, turn, digest)
    ).rowcount:
        return None
    added = 0
    for lesson in find_lessons(user, assistant, verified_checks=checks):
        text = lesson["text"]
        kind, scope = lesson["kind"], lesson.get("scope", "session")
        if kind not in _KINDS or scope not in _SCOPES or len(text) > 1500:
            continue
        stable_key = _text_key(text)
        if not stable_key:
            continue
        key = _digest(kind, scope, stable_key)
        connection.execute(
            "INSERT OR IGNORE INTO lessons VALUES (?,?,?,?,?)",
            (key, kind, scope, text, lesson.get("evidence", "")),
        )
        added += connection.execute(
            "INSERT OR IGNORE INTO occurrences VALUES (?,?,?,?,?)",
            (key, source, thread, turn, _stamp(record.get("at"))),
        ).rowcount
    return added


def _render_text(text: str) -> str:
    # Historical language appears as evidence, including literal Markdown syntax.
    return escape(text).replace("[", "&#91;").replace("]", "&#93;").replace("`", "&#96;")


def _existing_note(path: Path) -> str | None:
    if path.is_symlink():
        raise ValueError(f"Unexpected link in generated notes: {path.name}")
    if path.exists():
        existing = path.read_text(encoding="utf-8-sig")
        if not existing.startswith(_MARKER):
            raise ValueError(f"Refusing to overwrite a personal note: {path.name}")
        return existing
    return None


def _write_note(path: Path, contents: str) -> None:
    if _existing_note(path) == contents:
        return
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                     dir=path.parent, prefix=".ofsafaerni-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(contents)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


_LESSONS = """
SELECT l.id,l.kind,l.scope,l.text,l.evidence,COUNT(*) AS uses,MAX(o.at) AS last_seen
FROM lessons l JOIN occurrences o ON l.id=o.lesson
GROUP BY l.id ORDER BY last_seen DESC, l.id
"""


def _root(vault: Path) -> Path:
    if not vault.is_dir():
        raise ValueError("The configured Obsidian vault is unavailable")
    root = vault / _NOTES
    for folder in (vault, root.parent, root):
        if folder != vault:
            folder.mkdir(exist_ok=True)
        local_root(folder)
    return root


def render(connection: sqlite3.Connection, vault: Path) -> dict:
    root = _root(vault)
    for file in ("Yfirlit", *_KINDS.values()):
        _existing_note(root / f"{file}.md")
    rows = connection.execute(_LESSONS).fetchall()
    grouped = Counter(row["kind"] for row in rows)
    for kind, title in _KINDS.items():
        lines = [_MARKER, f"# {title}", "", "Samhengi úr spjallsögu. Nýjustu fyrirmæli notanda og staðfest verkefnaglósugögn ráða.", ""]
        for row in rows:
            if row["kind"] != kind:
                continue
            sources = connection.execute(
                "SELECT source,thread,turn,at FROM occurrences WHERE lesson=? ORDER BY at DESC LIMIT 2",
                (row["id"],),
            ).fetchall()
            refs = ", ".join(
                f"{r['source']} `{_digest(r['thread'], r['turn'])[:12]}`{(' · ' + r['at'][:10]) if r['at'] else ''}"
                for r in sources
            )
            lines.append(f"- {_render_text(row['text'])}")
            lines.append(
                f"  - {_SCOPES[row['scope']]} · {refs} · {row['uses']} heimildafærslur"
                f" · {_render_text(row['evidence'])} · `{row['id'][:12]}`"
            )
        _write_note(root / f"{title}.md", "\n".join(lines) + "\n")
    index = [_MARKER, "# OFSAFAERNI · Lærdómur úr samtölum", "",
             "Safnið geymir stuttar heimildatengdar færslur um vinnulag. Tölur og fyrirmæli í eldri spjöllum þarf að meta í samhengi við verkefnið.", ""]
    index.extend(f"- [[{_NOTES.as_posix()}/{title}|{title}]] · {grouped[kind]}" for kind, title in _KINDS.items())
    if (root / "Yfirfarin vinnubrögð.md").is_file():
        index.append(f"- [[{_NOTES.as_posix()}/Yfirfarin vinnubrögð|Yfirfarin vinnubrögð og leiðréttingar]]")
    _write_note(root / "Yfirlit.md", "\n".join(index) + "\n")
    return {"total": len(rows), **{kind: grouped[kind] for kind in _KINDS}}


def ingest(config: dict, records, source: str, *, indexed=(), checkpoint=None) -> dict:
    from contextlib import closing

    with closing(_open(config["state"])) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        processed = new_turns = new_occurrences = 0
        for row in records:
            processed += 1
            added = _record(conn, row, source)
            if added is not None:
                new_turns += 1
                new_occurrences += added
        for entry in indexed:
            if not isinstance(entry, dict):
                continue
            thread = entry.get("id")
            updated = _stamp(entry.get("updatedAt"))
            if isinstance(thread, str) and _IDENTIFIER.fullmatch(thread) and updated:
                conn.execute(
                    "INSERT INTO chatgpt_index VALUES (?,?) ON CONFLICT(thread) "
                    "DO UPDATE SET updated_at=MAX(updated_at,excluded.updated_at)",
                    (thread, updated),
                )
        if checkpoint:
            conn.execute(
                "INSERT INTO export_checkpoints VALUES (?,?) ON CONFLICT(identity) "
                "DO UPDATE SET signature=excluded.signature",
                checkpoint,
            )
        notes = config["vault"] / _NOTES
        if new_occurrences or any(not (notes / f"{name}.md").is_file() for name in ("Yfirlit", *_KINDS.values())):
            totals = render(conn, config["vault"])
        else:
            counts = dict(conn.execute("SELECT kind,COUNT(*) FROM lessons GROUP BY kind"))
            totals = {"total": sum(counts.values()), **{kind: counts.get(kind, 0) for kind in _KINDS}}
    return {"source": source, "turns": processed, "new_turns": new_turns,
            "new_occurrences": new_occurrences, "lessons": totals}


def scan(config: dict, thread_id: str | None = None) -> dict:
    def local_scan() -> dict:
        try:
            return ingest(config, iter_codex_turns(config["codex_home"], thread_id=thread_id), "codex")
        except (OSError, ValueError, sqlite3.DatabaseError) as error:
            return {"source": "codex", "error": str(error)}

    if thread_id:
        # Stop fires before the terminal turn has been committed.
        time.sleep(0.8)
    results = [local_scan()]
    if thread_id:
        if not results[0].get("new_turns"):
            time.sleep(1.2)
            results = [local_scan()]
    else:
        export = config["chatgpt_export"]
        sources = dict(config.get("providers", {}))
        if export:
            sources["chatgpt"] = export
        for name, path in sources.items():
            try:
                results.append(_scan_export(config, path, name))
            except (OSError, ValueError, sqlite3.DatabaseError) as error:
                results.append({"source": name, "error": str(error)})
    return {"results": results}


def _scan_export(config: dict, export: Path, provider: str = "chatgpt") -> dict:
    from contextlib import closing
    from .history_sources import iter_chatgpt_export

    reader = iter_chatgpt_export if provider == "chatgpt" else reader_for(provider)
    if export.is_dir():
        # Child files can change without changing the directory timestamp.
        return ingest(config, reader(export), provider)
    info = export.stat()
    identity = _digest(provider, str(export))
    signature = _digest(_RULE_REVISION, str(info.st_size), str(info.st_mtime_ns), str(info.st_ino))
    with closing(_open(config["state"])) as conn:
        existing = conn.execute("SELECT signature FROM export_checkpoints WHERE identity=?", (identity,)).fetchone()
    if existing and existing["signature"] == signature:
        return {"source": provider, "unchanged": True}
    return ingest(config, reader(export), provider, checkpoint=(identity, signature))


def lookup(config: dict, query: str) -> dict:
    from contextlib import closing

    terms = features(query[:6000])["terms"] - GENERIC_WORDS
    scored = []
    with closing(_open(config["state"])) as conn:
        for entry in conn.execute(_LESSONS):
            if entry["scope"] != "global":
                continue
            evidence = features(entry["text"])
            hits = terms & (evidence["terms"] - GENERIC_WORDS)
            if len(hits) >= 2:
                scored.append((len(hits), entry["last_seen"], dict(entry)))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return {"lessons": [item[2] for item in scored[:5]]}


def _parse(argv):
    parser = argparse.ArgumentParser(prog="ofsafaerni history", description=__doc__)
    parser.add_argument("--config", type=Path)
    cmds = parser.add_subparsers(dest="command", required=True)
    start = cmds.add_parser("start", help="Read accessible local history and keep learning while running.")
    start.add_argument("--vault", type=Path)
    start.add_argument("--codex-home", type=Path)
    start.add_argument("--state-dir", type=Path)
    start.add_argument("--chatgpt-export", type=Path)
    for provider in SUPPORTED:
        start.add_argument(f"--{provider}", type=Path, help=f"Include a selected local {provider} history source.")
    start.add_argument("--no-integrate", action="store_true", help="Save notes without adding Codex user guidance.")
    start.add_argument("--once", action="store_true")
    one = cmds.add_parser("scan")
    one.add_argument("--thread-id")
    q = cmds.add_parser("lookup")
    q.add_argument("query")
    file = cmds.add_parser("import-curated")
    file.add_argument("file", type=Path)
    cmds.add_parser("cloud-index")
    cmds.add_parser("coverage")
    cmds.add_parser("hook")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from contextlib import closing

    args = _parse(argv)
    try:
        if args.command == "start":
            from .history_start import install_lookup_bridge, prepare_config

            config_path = prepare_config(vault=args.vault, codex_home=args.codex_home,
                                         state_dir=args.state_dir, chatgpt_export=args.chatgpt_export,
                                         providers={name: getattr(args, name) for name in SUPPORTED
                                                    if getattr(args, name) is not None})
            config = read_config(config_path)
            results = scan(config)
            integration = (
                {"status": "skipped"} if args.no_integrate
                else install_lookup_bridge(config_path, config["codex_home"])
            )
            print(json.dumps({"config": str(config_path), "codex_integration": integration, **results},
                             ensure_ascii=True), flush=True)
            while not args.once:
                time.sleep(300)
                try:
                    print(json.dumps(scan(config), ensure_ascii=True), flush=True)
                except (OSError, ValueError, sqlite3.DatabaseError) as error:
                    print(f"OFSAFAERNI history: {error}", file=sys.stderr, flush=True)
            return 2 if any("error" in result for result in results["results"]) else 0
        if args.config is None:
            raise ValueError("Use --config with the path printed by history start")
        if args.command == "hook":
            from .history_hook import handle_stop

            result = handle_stop(sys.stdin.buffer, args.config)
        else:
            config = read_config(args.config)
            if args.command == "scan":
                if args.thread_id and not _IDENTIFIER.fullmatch(args.thread_id):
                    raise ValueError("Invalid thread identifier")
                result = scan(config, args.thread_id)
            elif args.command == "lookup":
                result = lookup(config, args.query)
            elif args.command == "coverage":
                result = coverage(config["codex_home"])
            elif args.command == "cloud-index":
                with closing(_open(config["state"])) as conn:
                    result = {"threads": dict(conn.execute("SELECT thread, updated_at FROM chatgpt_index"))}
            else:
                with args.file.open("rb") as handle:
                    raw = handle.read(8_388_609)
                if len(raw) > 8_388_608:
                    raise ValueError("Curated import exceeds 8 MiB")
                document = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
                    raise ValueError("Expected curated user-message entries")
                indexed = document.get("completed", [])
                if not isinstance(indexed, list):
                    raise ValueError("Expected a list of completed source entries")
                # A generic imported file cannot claim an independently verified check.
                rows = ({**row, "assistant": "", "verified_checks": 0}
                        for row in document["entries"] if isinstance(row, dict))
                result = ingest(config, rows, "chatgpt", indexed=indexed)
        print(json.dumps(result, ensure_ascii=True))
        return 2 if any("error" in entry for entry in result.get("results", [])) else 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, sqlite3.DatabaseError) as error:
        if args.command == "hook":
            print("{}")
            return 0
        print(f"OFSAFAERNI history: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
