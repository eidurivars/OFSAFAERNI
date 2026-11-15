"""Local SQLite catalog with content-bound approval and aggregate feedback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from . import sources


MAX_LOCAL_SKILLS = 500
MAX_ERROR_RECORDS = 25
MAX_ROUTING_ITEMS = 20_000
ORIGIN = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_SOURCE_ERRORS = (sources.SourceError, OSError, UnicodeError)
_METADATA_FIELDS = "id,name,description,origin,source,path,sha256,tags,triggers,approved_sha256,successes,failures"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    origin TEXT NOT NULL,
    source TEXT NOT NULL,
    path TEXT,
    sha256 TEXT NOT NULL,
    tags TEXT NOT NULL,
    triggers TEXT NOT NULL,
    body TEXT,
    approved_sha256 TEXT,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0
);
"""
_UPSERT = """
INSERT INTO skills(id, name, description, origin, source, path, sha256, tags, triggers, body)
VALUES (:id, :name, :description, :origin, :source, :path, :sha256, :tags, :triggers, :body)
ON CONFLICT(id) DO UPDATE SET
    name=excluded.name,
    description=excluded.description,
    origin=excluded.origin,
    source=excluded.source,
    path=excluded.path,
    tags=excluded.tags,
    triggers=excluded.triggers,
    body=excluded.body,
    approved_sha256=CASE WHEN skills.sha256=excluded.sha256 THEN skills.approved_sha256 END,
    successes=CASE WHEN skills.sha256=excluded.sha256 THEN skills.successes ELSE 0 END,
    failures=CASE WHEN skills.sha256=excluded.sha256 THEN skills.failures ELSE 0 END,
    sha256=excluded.sha256
"""


class CatalogError(ValueError):
    """Expected input, source or approval error for the CLI."""


def _identifier(prefix: str, source: str) -> str:
    return prefix + "-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def _entry(snapshot: sources.Snapshot, origin: str, source: str, path: Path | None) -> dict:
    return {
        "id": _identifier("local", os.path.normcase(str(path))) if path else _identifier("github", source),
        "name": snapshot.name,
        "description": snapshot.description,
        "origin": origin,
        "source": source,
        "path": str(path) if path else None,
        "sha256": snapshot.sha256,
        "tags": json.dumps(snapshot.tags, ensure_ascii=False),
        "triggers": json.dumps(snapshot.triggers, ensure_ascii=False),
        "body": snapshot.body if path is None else None,
    }


def _record(row: sqlite3.Row, status: str) -> dict:
    path = row["path"]
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "origin": row["origin"],
        "source": row["source"],
        "path": path,
        "bundle_root": str(Path(path).parent) if path is not None else None,
        "sha256": row["sha256"],
        "approved": status == "approved",
        "status": status,
        "tags": json.loads(row["tags"]),
        "triggers": json.loads(row["triggers"]),
        "feedback": {"success": row["successes"], "failure": row["failures"]},
    }


class Catalog:
    """A catalog rooted at an explicit SQLite path; skills remain in their own folders."""

    def __init__(self, path: Path):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=8)
        self._db.row_factory = sqlite3.Row
        try:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise CatalogError(f"Unsupported catalog format: {version}")
            if version == 0:
                with self._db:
                    self._db.execute("BEGIN IMMEDIATE")
                    self._db.execute(_SCHEMA)
                    self._db.execute("PRAGMA user_version=1")
        except BaseException:
            self._db.close()
            raise

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def _row(self, skill_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()

    def _require(self, skill_id: str) -> sqlite3.Row:
        row = self._row(skill_id)
        if row is None:
            raise CatalogError(f"Unknown catalog ID: {skill_id}")
        return row

    def _upsert(self, records: list[dict]) -> None:
        with self._db:
            self._db.executemany(_UPSERT, records)

    def collect_local(self, root: Path, *, origin: str = "local") -> dict:
        if not ORIGIN.fullmatch(origin) or origin == "github":
            raise CatalogError("Invalid local skill origin")
        try:
            root = sources.local_root(Path(root))
        except _SOURCE_ERRORS as exc:
            raise CatalogError(f"Cannot collect local skill root: {exc}") from exc
        records = []
        errors = []
        error_count = 0
        truncated = False
        try:
            for index, skill_path in enumerate(sources.find_local_skills(root)):
                if index == MAX_LOCAL_SKILLS:
                    truncated = True
                    break
                try:
                    snapshot = sources.snapshot_local(skill_path)
                    records.append(_entry(snapshot, origin, str(skill_path), skill_path))
                except _SOURCE_ERRORS as exc:
                    error_count += 1
                    if len(errors) < MAX_ERROR_RECORDS:
                        errors.append({"source": str(skill_path), "error": str(exc)})
        except _SOURCE_ERRORS as exc:
            truncated = True
            error_count += 1
            if len(errors) < MAX_ERROR_RECORDS:
                errors.append({"source": str(root), "error": str(exc)})
        self._upsert(records)
        return {
            "root": str(root), "origin": origin, "collected": len(records),
            "ids": [record["id"] for record in records],
            "errors": errors, "error_count": error_count, "truncated": truncated,
        }

    def collect_github(self, repo: str, *, ref: str | None = None, limit: int = 60) -> dict:
        try:
            canonical, revision, entries, truncated = sources.github_skills(repo, ref, limit)
        except _SOURCE_ERRORS as exc:
            raise CatalogError(str(exc)) from exc
        records = []
        errors = []
        error_count = 0
        for entry in entries:
            try:
                source, snapshot = sources.snapshot_github(canonical, revision, entry)
                records.append(_entry(snapshot, "github", source, None))
            except _SOURCE_ERRORS as exc:
                error_count += 1
                if len(errors) < MAX_ERROR_RECORDS:
                    errors.append({"source": entry["path"], "error": str(exc)})
        self._upsert(records)
        return {
            "repo": canonical, "commit": revision, "origin": "github", "collected": len(records),
            "ids": [record["id"] for record in records],
            "errors": errors, "error_count": error_count, "truncated": truncated,
        }

    @staticmethod
    def _fresh(row: sqlite3.Row) -> sources.Snapshot:
        try:
            return sources.snapshot_local(Path(row["path"]))
        except _SOURCE_ERRORS as exc:
            raise CatalogError(f"Skill bundle cannot be read: {exc}") from exc

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict:
        if row["path"] is None:
            return _record(row, "remote-review")
        if row["approved_sha256"] is None:
            return _record(row, "pending")
        try:
            current = sources.snapshot_local(Path(row["path"]))
        except FileNotFoundError:
            return _record(row, "missing")
        except _SOURCE_ERRORS:
            return _record(row, "changed")
        return _record(row, "approved" if current.sha256 == row["approved_sha256"] else "changed")

    def items(self, *, include_pending: bool = False) -> list[dict]:
        query = f"SELECT {_METADATA_FIELDS} FROM skills"
        if include_pending:
            rows = self._db.execute(query + " ORDER BY name,id").fetchall()
        else:
            rows = self._db.execute(
                query + " WHERE path IS NOT NULL AND approved_sha256=sha256 ORDER BY name,id"
            ).fetchall()
        records = [self._summary(row) for row in rows]
        return records if include_pending else [record for record in records if record["approved"]]

    def routing_items(self, *, max_items: int = MAX_ROUTING_ITEMS) -> list[dict]:
        """Read stored metadata only; reverify every finalist with get or load."""
        if not 1 <= max_items <= MAX_ROUTING_ITEMS:
            raise CatalogError(f"Routing catalog limit must be between 1 and {MAX_ROUTING_ITEMS}")
        rows = self._db.execute(
            f"SELECT {_METADATA_FIELDS} FROM skills "
            "WHERE path IS NOT NULL AND approved_sha256=sha256 ORDER BY name,id LIMIT ?",
            (max_items + 1,),
        ).fetchall()
        if len(rows) > max_items:
            raise CatalogError(f"More than {max_items} approved skills in this catalog")
        return [_record(row, "approved") for row in rows]

    def get(self, skill_id: str) -> dict | None:
        row = self._row(skill_id)
        return self._summary(row) if row is not None else None

    def review(self, skill_id: str) -> dict:
        row = self._require(skill_id)
        if row["path"] is None:
            return {**_record(row, "remote-review"), "body": row["body"]}
        current = self._fresh(row)
        status = "pending"
        if row["approved_sha256"] is not None:
            status = "approved" if current.sha256 == row["approved_sha256"] else "changed"
        return {
            **_record(row, status), "body": current.body, "sha256": current.sha256,
            "name": current.name, "description": current.description,
            "tags": current.tags, "triggers": current.triggers,
            "files": sorted(current.files),
        }

    def approve(self, skill_id: str, digest: str) -> dict:
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise CatalogError("Approval requires the full reviewed SHA-256")
        row = self._require(skill_id)
        if row["path"] is None:
            raise CatalogError("GitHub entries are review-only. Collect a reviewed local checkout to approve its full bundle.")
        current = self._fresh(row)
        if current.sha256 != digest.lower():
            raise CatalogError("Skill bundle changed. Review its current content and SHA-256 before approval.")
        with self._db:
            self._db.execute(_UPSERT, _entry(current, row["origin"], row["source"], Path(row["path"])))
            self._db.execute("UPDATE skills SET approved_sha256=? WHERE id=?", (current.sha256, skill_id))
        return self._summary(self._require(skill_id))

    def revoke(self, skill_id: str) -> dict:
        self._require(skill_id)
        with self._db:
            self._db.execute("UPDATE skills SET approved_sha256=NULL WHERE id=?", (skill_id,))
        return self._summary(self._require(skill_id))

    def _approved_snapshot(self, row: sqlite3.Row) -> sources.Snapshot:
        if row["path"] is None or row["approved_sha256"] is None:
            raise CatalogError("Skill bundle has not been approved")
        current = self._fresh(row)
        if current.sha256 != row["approved_sha256"]:
            raise CatalogError("Skill bundle changed after approval. Review it before use.")
        latest = self._require(row["id"])
        if latest["approved_sha256"] != current.sha256:
            raise CatalogError("Skill approval changed during read")
        return current

    def load(self, skill_id: str) -> dict:
        row = self._require(skill_id)
        current = self._approved_snapshot(row)
        return {**_record(row, "approved"), "body": current.body}

    def bundle(self, skill_id: str) -> tuple[dict, dict[str, bytes]]:
        row = self._require(skill_id)
        return _record(row, "approved"), self._approved_snapshot(row).files

    def feedback(self, skill_id: str, outcome: str) -> dict:
        if outcome not in {"success", "failure"}:
            raise CatalogError("Feedback must be success or failure")
        row = self._require(skill_id)
        current = self._approved_snapshot(row)
        with self._db:
            result = self._db.execute(
                "UPDATE skills SET successes=successes+?,failures=failures+? "
                "WHERE id=? AND approved_sha256=? AND sha256=?",
                (int(outcome == "success"), int(outcome == "failure"), skill_id, current.sha256, current.sha256),
            )
            if result.rowcount != 1:
                raise CatalogError("Skill approval changed during feedback")
        return self._summary(self._require(skill_id))
