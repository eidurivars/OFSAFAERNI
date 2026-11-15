"""Synthetic routing acceptance and regression tests; no network access."""

import json
from pathlib import Path
import time
import unittest

from ofsafaerni.language import features, runtime_info
from ofsafaerni.routing import MAX_CANDIDATES, MAX_QUERY_CHARS, route


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "routing_synthetic.json").read_text(encoding="utf-8"))


class RoutingTests(unittest.TestCase):
    def test_synthetic_bilingual_acceptance(self):
        for case in FIXTURE["cases"]:
            with self.subTest(query=case["query"]):
                result = route(case["query"], FIXTURE["candidates"])
                self.assertEqual(result["decision"], case["decision"], result)
                self.assertEqual({i["id"] for i in result["selected"]}, set(case["selected"]), result)

    def test_exact_explicit_name(self):
        candidates = [{"id": "1", "name": "pdf-extra", "description": "Create PDFs"},
                      {"id": "2", "name": "pdf", "description": ""}]
        self.assertEqual(route("$pdf", candidates)["selected"][0]["id"], "2")
        self.assertEqual(route("$pdf-new", candidates)["decision"], "bypass")
        self.assertEqual(route("$missing, create PDFs", candidates)["decision"], "bypass")

    def test_read_only_skills_cannot_cover_creation(self):
        candidates = [
            {"id": "pdf", "name": "pdf-reading", "description": "Read a PDF, extract information, summarize its contents"},
            {"id": "code", "name": "defensive-code-review", "description": "Review Python source code for bugs and input validation"},
        ]
        for query, expected in (
            ("Create a PDF", []),
            ("Write Python code", []),
            ("Create a PDF and review Python source code", ["code"]),
            ("Review the PDF document then write Python code", ["pdf"]),
        ):
            with self.subTest(query=query):
                decision = route(query, candidates)
                self.assertEqual(decision["selected"], [])
                self.assertEqual([match["id"] for match in decision["candidates"]], expected)
                if expected:
                    self.assertEqual(decision["decision"], "ambiguous")

    def test_explicit_collision(self):
        candidates = [{"id": "1", "name": "one:pdf", "description": ""},
                      {"id": "2", "name": "two:pdf", "description": ""}]
        self.assertEqual(route("$pdf", candidates)["decision"], "ambiguous")
        self.assertEqual(route("$one:pdf", candidates)["selected"][0]["id"], "1")

    def test_exact_identifier_survives_punctuation_and_conjunctions(self):
        candidates = [{"id": "a", "name": "read-and-write.v2", "description": ""}]
        result = route("Use $read-and-write.v2.", candidates)
        self.assertEqual([s["id"] for s in result["selected"]], ["a"])
        self.assertEqual(route("$read-and-write.v2/other", candidates)["decision"], "bypass")

    def test_explicit_negation(self):
        self.assertEqual(route("Do not use $pdf", FIXTURE["candidates"])["decision"], "bypass")
        result = route("Don't use $pdf, use $slides", FIXTURE["candidates"])
        self.assertEqual([s["id"] for s in result["selected"]], ["slides"])
        self.assertEqual(route("Use $pdf but do not use $pdf", FIXTURE["candidates"])["decision"], "ambiguous")

    def test_explicit_multiple_limit(self):
        result = route("$pdf and $slides", FIXTURE["candidates"], limit=1)
        self.assertEqual(result["decision"], "ambiguous")
        self.assertEqual(result["selected"], [])

    def test_negation_lists_and_alternatives(self):
        cases = [("Don't create PDF, Word, or Excel; create slides", {"slides"}),
                 ("Create a Word document rather than a PDF", {"docs"}),
                 ("Create a Word document instead of a PDF", {"docs"}),
                 ("Do not use PDF and Word or Excel", set())]
        for query, expected in cases:
            with self.subTest(query=query):
                result = route(query, FIXTURE["candidates"])
                self.assertEqual({s["id"] for s in result["selected"]}, expected, result)

    def test_homonyms_do_not_select_nearby_technical_domain(self):
        cases = ["Analyze resistance in this electrical circuit", "Read my PDF printer manual",
                 "Hvernig skipti ég um blekhylki í PDF-prentaranum?", "Create five minutes of silence"]
        for query in cases:
            with self.subTest(query=query):
                self.assertEqual(route(query, FIXTURE["candidates"])["decision"], "bypass")

    def test_complete_specialist_trigger(self):
        candidates = [{"id": "dna", "name": "genome-alignment", "description": "Align nucleotide sequences.",
                       "triggers": ["Align genome sequences"]}]
        self.assertEqual(route("Align genome sequences", candidates)["selected"][0]["id"], "dna")
        self.assertEqual(route("I was told to align genome sequences yesterday", candidates)["decision"], "bypass")

    def test_duplicate_and_disallowed_candidates(self):
        candidate = {"id": "a", "name": "pdf", "description": "Create PDF", "approved": False}
        self.assertEqual(route("$pdf", [candidate])["decision"], "bypass")
        candidate["approved"] = True
        self.assertEqual(route("Create a PDF", [candidate, candidate])["decision"], "ambiguous")

    def test_identical_descriptions_abstain_despite_feedback(self):
        candidates = [{"id": "a", "name": "pdf-a", "description": "Create and read PDF files", "feedback": {"success": 1000000}},
                      {"id": "b", "name": "pdf-b", "description": "Create and read PDF files", "feedback": {"failure": 1000000}}]
        self.assertEqual(route("Read this PDF", candidates)["decision"], "ambiguous")

    def test_popularity_cannot_create_match(self):
        candidates = [{"id": "a", "name": "pdf", "description": "Create PDF", "feedback": {"success": 1000000}}]
        self.assertEqual(route("How should I boil potatoes?", candidates)["decision"], "bypass")

    def test_bodies_are_never_read_or_returned(self):
        candidates = [{"id": "a", "name": "pdf", "description": "Create PDF", "body": "PRIVATE " * 10000}]
        result = route("Create a PDF", candidates)
        self.assertEqual(result["decision"], "use_skill")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_malformed_and_overlong_metadata(self):
        valid = {"id": "a", "name": "pdf", "description": "Create PDF"}
        for override in [{"description": "x" * 8001}, {"tags": "pdf"}, {"triggers": [7]}, {"name": None}]:
            self.assertEqual(route("Create a PDF", [{**valid, **override}])["decision"], "bypass")

    def test_limits_fail_closed(self):
        self.assertEqual(route("Create PDF " + "x" * MAX_QUERY_CHARS, FIXTURE["candidates"])["decision"], "bypass")
        self.assertEqual(route("Create PDF", [{}] * (MAX_CANDIDATES + 1))["decision"], "bypass")
        for invalid in [0, -1, 11, True, "3", None]:
            with self.assertRaises(ValueError):
                route("Create a PDF", FIXTURE["candidates"], limit=invalid)
        with self.assertRaises(TypeError):
            route(None, FIXTURE["candidates"])

    def test_result_bound_and_multi_intent_completeness(self):
        result = route("Read PDF and create slides", FIXTURE["candidates"], limit=1)
        self.assertEqual(result["decision"], "ambiguous")
        self.assertEqual(result["selected"], [])
        self.assertLessEqual(len(result["candidates"]), 1)

    def test_noise_does_not_defeat_focused_skill(self):
        noise = {"id": "noise", "name": "universal-assistant", "description": "Create and edit PDF spreadsheet image website audio video database code calendar email files."}
        result = route("Read this PDF", [noise, *FIXTURE["candidates"]])
        self.assertEqual([s["id"] for s in result["selected"]], ["pdf"], result)

    def test_unknown_domain_uses_discriminative_lexical_evidence(self):
        candidates = [{"id": "dna", "name": "genome-alignment", "description": "Align genome sequences and compare nucleotide mutations."},
                      {"id": "pdf", "name": "pdf", "description": "Create PDF"}]
        self.assertEqual(route("Compare genome mutations", candidates)["selected"][0]["id"], "dna")

    def test_inflected_forms_and_unicode(self):
        for phrase in ["PDF-skjölunum", "PDF–skjalinu", "PDF‑skjalið"]:
            self.assertIn("pdf", features(phrase)["domains"])
        self.assertIn("spreadsheet", features("vinnubókunum")["domains"])
        self.assertIn("presentation", features("glærunum")["domains"])
        self.assertIn("code", features("forritunum")["domains"])
        self.assertTrue(runtime_info()["local_only"])

    def test_ten_thousand_synthetic_candidates(self):
        candidates = [{"id": f"other-{i}", "name": f"specialist-{i}", "description": "Manage quasar observations and orbital telemetry."}
                      for i in range(9999)]
        candidates += [{"id": "pdf", "name": "pdf", "description": "Create and read PDF files"}]
        start = time.perf_counter()
        result = route("Lestu PDF-skjalið", candidates)
        elapsed = time.perf_counter() - start
        self.assertEqual([s["id"] for s in result["selected"]], ["pdf"])
        self.assertLessEqual(len(result["candidates"]), 3)
        # Catch pathological algorithmic growth, with generous CI allowance.
        self.assertLess(elapsed, 20, f"10,000 metadata rows took {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
