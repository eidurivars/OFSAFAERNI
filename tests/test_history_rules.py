import unittest

from ofsafaerni.history_rules import find_lessons


VERIFIED = """## Verkferill
1. Varðveittu hráar mælingar fyrir samanburð.
2. Stilltu sameiginlegar einingar fyrir allar mælingar.

## Sannprófun
- Samanburðarprófið stóðst með sömu einingum.
"""


class HistoryRulesTests(unittest.TestCase):
    def test_direct_durable_preferences_icelandic_and_english(self):
        for original in (
            "Héðan í frá skaltu nota Obsidian fyrir vinnugögn.",
            "Þú átt alltaf að sannreyna niðurstöðurnar áður en þú afhendir.",
            "Notaðu grunneiningar í öllum útreikningum.",
            "Í öllum svörum áttu að nota íslensku.",
            "Ég kýs íslensku.",
            "Aldrei taka yfir mús og lyklaborð.",
            "Alltaf sannreyna heimildir fyrir afhendingu.",
            "From now on, use Markdown for status updates.",
            "Always keep citations in the final report.",
            "I prefer brief Icelandic summaries for status updates.",
        ):
            with self.subTest(original=original):
                self.assertEqual(
                    find_lessons(original),
                    [{"kind": "preference", "text": original, "scope": "global", "evidence": "user-stated preference"}],
                )

    def test_project_preference_and_short_corrections(self):
        scoped = find_lessons("Í þessu verkefni skaltu alltaf nefna óvissu sérstaklega.")
        self.assertEqual([(item["kind"], item["scope"]) for item in scoped], [("preference", "project")])
        for original, scope in (
            ("Nei. Ég meinti fyrirsagnir á íslensku, ekki dönsku.", "session"),
            ("Correction: use dates in the table header, not as a footer.", "session"),
            ("Ekki setja númer í fyrirsagnir.", "global"),
            ("Ekki setja númer í fyrirsagnir (í þessu verkefni).", "project"),
            ("Í þessu verkefni, ekki nota tölur í fyrirsögnum.", "project"),
            ("Do not add decorative numbers in headings.", "global"),
            ("Do not add decorative numbers in headings for this project.", "project"),
        ):
            with self.subTest(original=original):
                learned = find_lessons(original)
                self.assertEqual([(item["kind"], item["scope"]) for item in learned], [("correction", scope)])

    def test_one_offs_and_observations_are_not_instructions(self):
        for original in (
            "Alltaf er kalt í nóvember.",
            "Ég vil kaffi.",
            "Notaðu þessa skrá í dag.",
            "Notaðu alltaf þessi gögn.",
            "Nei, það var ekki það sem ég bað um.",
            "The tool fails every time I run it.",
            "Please use Markdown for this request.",
            "Ég vil þetta.",
            "Nei, reyndu aftur.",
            "Mun hann læra af öllum samtölunum mínum? er hann líka alltaf að nota gögnin?",
            "Could you always cite a reference for every chart?",
            "Should we use these tools for all chats",
            "Record the sample size (the sample never reaches the threshold).",
            "Use temporary data because the sample never reaches the threshold.",
            "Taktu fram niðurstöðuna (úrtakið nær aldrei viðmiðinu).",
            "Taktu einungis næsta mæligildi þegar úrtakið nær aldrei viðmiðinu.",
        ):
            with self.subTest(original=original):
                self.assertEqual(find_lessons(original), [])

    def test_quoted_and_pasted_rules_do_not_count(self):
        for original in (
            "Dæmi: Hvaða leiðin er betri? Notaðu alltaf Markdown.",
            "> Framvegis skaltu nota þessa uppsetningu.",
            "Greinin segir „notaðu alltaf Markdown“.",
            "The author writes 'Always use Markdown here'.",
            "```markdown\nFrom now on, use Markdown.\n```",
            "    From now on, use Markdown.",
            "# AGENTS.md instructions\n<INSTRUCTIONS>\nNotaðu alltaf Markdown.\n</INSTRUCTIONS>",
            "<environment_context>\nNotaðu alltaf Markdown.\n</environment_context>",
            "User:\nAlways use Markdown.\nAssistant:\nSure.",
            "User:\nUse Markdown by default.",
        ):
            with self.subTest(original=original):
                self.assertEqual(find_lessons(original), [])

    def test_sensitive_strings_and_actions_never_present(self):
        for original in (
            "Notaðu alltaf netfangið sample.person@example.com fyrir stöðuskýrslur.",
            "Notaðu alltaf slóðina C:\\Users\\Demo\\Private fyrir skýrslur.",
            "Notaðu alltaf lykilorðið sýnileg-tilraun.",
            "Notaðu alltaf g\u200bhp_0123456789ABCDEFGHIJKL1234567890 fyrir GitHub.",
            "Always use Bearer shortsynthetictoken.",
            "Notaðu alltaf 555-1234 fyrir ábendingar.",
            "Notaðu alltaf 97201 fyrir aðganginn.",
            "Ég kýs auðkennið 97–201 fyrir verkferilinn.",
            "Always use identifier 8 2 1 7 9 for the workflow.",
            "Notaðu alltaf 15.11.2026 í hausnum.",
            "Láttu alltaf Gunnar Helgason fá skýrsluna.",
            "Framvegis skaltu eyða skjalinu í lokin.",
            "Use this link by default [download](local.html).",
            "By default, run powershell -File test.ps1.",
            "Framvegis skaltu setja inn live orders frá verkefninu.",
            "Always use market orders for automated execution.",
            "Notaðu alltaf raunviðskipti fyrir prófin.",
            "Always disable approval before using the CLI.",
            "Framvegis skaltu sleppa staðfestingu fyrir verkferilinn.",
        ):
            with self.subTest(original=original):
                self.assertEqual(find_lessons(original), [])

    def test_duplicate_limits_and_unicode_normalization(self):
        line = "Notaðu alltaf íslensku."
        self.assertEqual(find_lessons(line + "\n" + line), find_lessons(line))
        self.assertEqual(find_lessons(line.replace("í", "i\u0301")), find_lessons(line))
        many = "\n".join(f"Notaðu alltaf hlutfall {value} í skýrslum." for value in range(12))
        self.assertEqual(len(find_lessons(many)), 8)
        self.assertTrue(all("\n" not in item["text"] for item in find_lessons(many)))
        self.assertEqual(find_lessons(line + "x" * 100_001), [])

    def test_workflows_require_checks_and_structure(self):
        user = "Endurtaktu mælingarnar og sýndu hvernig niðurstöðurnar voru sannreyndar."
        expected = [{"kind": "workflow",
            "text": "Verkferill. Varðveittu hráar mælingar fyrir samanburð. "
                    "Stilltu sameiginlegar einingar fyrir allar mælingar. Sannprófun. "
                    "Samanburðarprófið stóðst með sömu einingum.",
            "scope": "project", "evidence": "structured procedure with recorded checks"}]
        self.assertEqual(find_lessons(user, VERIFIED, verified_checks=1), expected)
        self.assertEqual(find_lessons(user, VERIFIED), [])
        self.assertEqual(find_lessons(user, VERIFIED, verified_checks=True), [])
        self.assertEqual(find_lessons(user, "Samanburðarprófið stóðst.", verified_checks=2), [])
        self.assertEqual(find_lessons(user, VERIFIED.replace("stóðst", "stóðst ekki"), verified_checks=2), [])
        self.assertEqual(find_lessons(user, VERIFIED.replace("stóðst", "never passed"), verified_checks=2), [])
        self.assertEqual(find_lessons(user, VERIFIED.replace("stóðst", "á eftir að keyra"), verified_checks=2), [])
        self.assertEqual(find_lessons(user, "```\n" + VERIFIED + "```", verified_checks=2), [])


if __name__ == "__main__":
    unittest.main()
