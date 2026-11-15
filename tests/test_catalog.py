from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ofsafaerni import sources
from ofsafaerni.catalog import Catalog, CatalogError
from ofsafaerni.sources import bundle_digest


SKILL = """---
name: pdf-reading
description: >-
  Lesa PDF-skjöl og vísa
  í blaðsíðutöl.
metadata:
  tags: skjöl, heimildir
  triggers:
    - PDF-skjalið
    - blaðsíðutöl
---
# PDF-lestur

Lesa skjalið og merkja blaðsíður í útdrættinum.
"""


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "skills"
        self.skill = self.root / "category" / "pdf-reading"
        self.skill.mkdir(parents=True)
        self.markdown = self.skill / "SKILL.md"
        self.markdown.write_text(SKILL, encoding="utf-8")
        self.asset = self.skill / "guide.txt"
        self.asset.write_bytes(b"First guide revision")
        self.path = self.base / "state" / "catalog.sqlite"
        self.catalog = Catalog(self.path)
        self.addCleanup(self.catalog.close)

    def collect(self):
        result = self.catalog.collect_local(self.root, origin="hermes")
        self.assertEqual(result["collected"], 1, result["errors"])
        self.assertFalse(result["truncated"])
        return result["ids"][0]

    def test_local_collection_review_and_verified_bundle(self):
        skill_id = self.collect()
        self.assertEqual(self.catalog.items(), [])
        self.assertEqual(self.catalog.items(include_pending=True)[0]["status"], "pending")
        with self.assertRaises(CatalogError):
            self.catalog.load(skill_id)
        review = self.catalog.review(skill_id)
        self.assertEqual(review["description"], "Lesa PDF-skjöl og vísa í blaðsíðutöl.")
        self.assertEqual(review["tags"], ["skjöl", "heimildir"])
        self.assertEqual(review["triggers"], ["PDF-skjalið", "blaðsíðutöl"])
        self.assertIn("# PDF-lestur", review["body"])
        self.assertTrue(self.catalog.approve(skill_id, review["sha256"])["approved"])
        row, files = self.catalog.bundle(skill_id)
        self.assertEqual(sorted(files), ["SKILL.md", "guide.txt"])
        self.assertEqual(row["sha256"], bundle_digest(files))
        self.assertEqual(self.catalog.items()[0]["origin"], "hermes")
        self.assertEqual(self.catalog.load(skill_id)["body"], review["body"])
        self.assertEqual(self.catalog.revoke(skill_id)["status"], "pending")
        with self.assertRaises(CatalogError):
            self.catalog.bundle(skill_id)

    def test_companion_drift_disables_route_load_and_stale_approval(self):
        skill_id = self.collect()
        before = self.catalog.review(skill_id)["sha256"]
        self.catalog.approve(skill_id, before)
        self.catalog.feedback(skill_id, "success")
        self.asset.write_bytes(b"New guide revision")
        self.assertEqual(self.catalog.get(skill_id)["status"], "changed")
        self.assertEqual(self.catalog.items(), [])
        for operation in (self.catalog.load, self.catalog.bundle):
            with self.assertRaisesRegex(CatalogError, "changed"):
                operation(skill_id)
        with self.assertRaisesRegex(CatalogError, "changed"):
            self.catalog.approve(skill_id, before)
        current = self.catalog.review(skill_id)
        self.assertNotEqual(current["sha256"], before)
        self.assertTrue(self.catalog.approve(skill_id, current["sha256"])["approved"])
        self.assertEqual(self.catalog.get(skill_id)["feedback"], {"success": 0, "failure": 0})
        self.markdown.unlink()
        self.assertEqual(self.catalog.items(), [])

    def test_overlapping_roots_keep_one_entry_and_feedback_persists(self):
        skill_id = self.collect()
        self.catalog.approve(skill_id, self.catalog.review(skill_id)["sha256"])
        result = self.catalog.collect_local(self.skill)
        self.assertEqual(result["ids"], [skill_id])
        self.assertTrue(self.catalog.get(skill_id)["approved"])
        with Catalog(self.path) as another:
            self.catalog.feedback(skill_id, "success")
            another.feedback(skill_id, "failure")
            self.assertEqual(another.get(skill_id)["feedback"], {"success": 1, "failure": 1})
            self.assertEqual(len(another.items(include_pending=True)), 1)

    def test_skill_body_change_requires_new_approval_and_rescan_clears_old_version(self):
        skill_id = self.collect()
        previous = self.catalog.review(skill_id)["sha256"]
        self.catalog.approve(skill_id, previous)
        self.markdown.write_text(SKILL + "\nNýtt skref.", encoding="utf-8")
        self.assertFalse(self.catalog.get(skill_id)["approved"])
        self.assertEqual(self.collect(), skill_id)
        self.assertEqual(self.catalog.get(skill_id)["status"], "pending")
        self.markdown.write_text(SKILL, encoding="utf-8")
        self.assertEqual(self.catalog.review(skill_id)["sha256"], previous)
        self.assertFalse(self.catalog.get(skill_id)["approved"])

    def test_companion_added_mid_snapshot_invalidates_read(self):
        skill_id = self.collect()
        self.catalog.approve(skill_id, self.catalog.review(skill_id)["sha256"])
        read = sources._read_local_file

        def add_companion_during_read(path, expected, max_size):
            if path.name == "SKILL.md":
                (self.skill / "unexpected.txt").write_bytes(b"new companion content")
            return read(path, expected, max_size)

        with patch("ofsafaerni.sources._read_local_file", side_effect=add_companion_during_read):
            with self.assertRaisesRegex(CatalogError, "changed during scan"):
                self.catalog.load(skill_id)
        self.assertEqual(self.catalog.items(), [])

    def test_revocation_during_bundle_read_blocks_export(self):
        skill_id = self.collect()
        self.catalog.approve(skill_id, self.catalog.review(skill_id)["sha256"])
        read = sources.snapshot_local

        def revoke_during_read(path):
            with Catalog(self.path) as another:
                another.revoke(skill_id)
            return read(path)

        with patch("ofsafaerni.sources.snapshot_local", side_effect=revoke_during_read):
            with self.assertRaisesRegex(CatalogError, "approval changed"):
                self.catalog.bundle(skill_id)
        self.assertFalse(self.catalog.get(skill_id)["approved"])

    def test_routing_items_are_lightweight_and_bounded(self):
        skill_id = self.collect()
        self.catalog.approve(skill_id, self.catalog.review(skill_id)["sha256"])
        self.asset.write_bytes(b"revised")
        with patch("ofsafaerni.sources.snapshot_local") as snapshot:
            rows = self.catalog.routing_items()
            self.assertEqual([row["id"] for row in rows], [skill_id])
            self.assertNotIn("body", rows[0])
            snapshot.assert_not_called()
        self.assertFalse(self.catalog.get(skill_id)["approved"])
        self.catalog.revoke(skill_id)
        self.assertEqual(self.catalog.routing_items(), [])
        with self.assertRaises(CatalogError):
            self.catalog.routing_items(max_items=20_001)

    def test_duplicate_and_aliased_yaml_rejected(self):
        for invalid in (
            SKILL.replace("name: pdf-reading", "name: pdf-reading\nname: shadow"),
            SKILL.replace("name: pdf-reading", "name: &anchor pdf-reading"),
            SKILL.replace("name: pdf-reading", "name: !!str pdf-reading"),
        ):
            with self.subTest(frontmatter=invalid[:50]):
                self.markdown.write_text(invalid, encoding="utf-8")
                result = self.catalog.collect_local(self.root)
                self.assertEqual(result["collected"], 0)
                self.assertEqual(result["error_count"], 1)

    def test_companion_links_refused_without_cataloging(self):
        outside = self.base / "unrelated.txt"
        outside.write_text("Not part of an approved bundle.", encoding="utf-8")
        hard_link = self.skill / "copied-guide.txt"
        os.link(outside, hard_link)
        result = self.catalog.collect_local(self.root)
        self.assertEqual(result["collected"], 0)
        self.assertIn("link", result["errors"][0]["error"])

    def test_symlink_root_refused(self):
        alias = self.base / "alias"
        if os.name == "nt":
            from _winapi import CreateJunction

            CreateJunction(str(self.root), str(alias))
        else:
            alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(CatalogError, "without links"):
            self.catalog.collect_local(alias)

    def test_pending_only_github_entries_are_pinned_and_verified(self):
        data = SKILL.encode("utf-8")
        commit, tree = "a" * 40, "b" * 40
        path = "safn/lesa skjal/SKILL.md"
        file_sha = hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data, usedforsecurity=False).hexdigest()
        prefix = "https://api.github.com/repos/acme/example"
        replies = {
            f"{prefix}/commits/release%2Fstable": json.dumps({"sha": commit, "commit": {"tree": {"sha": tree}}}).encode(),
            f"{prefix}/git/trees/{tree}?recursive=1": json.dumps({"truncated": False, "tree": [
                {"mode": "100644", "type": "blob", "path": path, "sha": file_sha, "size": len(data)},
                {"mode": "120000", "type": "blob", "path": "a/SKILL.md", "sha": file_sha, "size": len(data)},
            ]}).encode(),
            f"https://raw.githubusercontent.com/acme/example/{commit}/safn/lesa%20skjal/SKILL.md": data,
        }

        def response(url, maximum):
            self.assertLessEqual(len(replies[url]), maximum)
            return replies[url]

        with patch("ofsafaerni.sources._http_bytes", side_effect=response):
            result = self.catalog.collect_github("https://github.com/ACME/Example.git", ref="release/stable")
        self.assertEqual(result["commit"], commit)
        self.assertEqual(result["collected"], 1)
        skill_id = result["ids"][0]
        row = self.catalog.review(skill_id)
        self.assertIn(commit, row["source"])
        self.assertEqual(row["status"], "remote-review")
        self.assertEqual(row["body"], SKILL)
        self.assertEqual(self.catalog.items(), [])
        with self.assertRaisesRegex(CatalogError, "review-only"):
            self.catalog.approve(skill_id, row["sha256"])
        with self.assertRaises(CatalogError):
            self.catalog.load(skill_id)

        raw = f"https://raw.githubusercontent.com/acme/example/{commit}/safn/lesa%20skjal/SKILL.md"
        replies[raw] = b"X" + data[1:]
        with patch("ofsafaerni.sources._http_bytes", side_effect=response):
            corrupt = self.catalog.collect_github("acme/example", ref="release/stable")
        self.assertEqual(corrupt["collected"], 0)
        self.assertIn("integrity", corrupt["errors"][0]["error"])
        self.assertEqual(self.catalog.review(skill_id)["body"], SKILL)

    def test_external_source_cannot_escape_named_github_repo(self):
        with patch("ofsafaerni.sources._http_bytes") as fetch:
            for invalid in ("https://example.org/acme/example", "https://github.com/acme/repo/tree/main", "-x/y"):
                with self.subTest(repository=invalid), self.assertRaises(CatalogError):
                    self.catalog.collect_github(invalid)
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
