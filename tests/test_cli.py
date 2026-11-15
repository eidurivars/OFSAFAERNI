import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ofsafaerni-test-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.db = self.workspace / "catalog.sqlite3"

    def invoke(self, *args, input_data=None, success=True):
        process = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "ofsafaerni", "--db", str(self.db), *map(str, args)],
            cwd=ROOT,
            input=input_data,
            capture_output=True,
            encoding="utf-8",
            timeout=45,
            check=False,
        )
        if success:
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertFalse(process.stdout)
        return process.stderr

    def skills(self, all_records=False):
        return {
            record["name"]: record
            for record in self.invoke("list", *(["--all"] if all_records else []))["skills"]
        }

    def approve(self, record):
        reviewed = self.invoke("review", record["id"])
        self.assertIn("body", reviewed)
        return self.invoke("approve", record["id"], "--sha256", reviewed["sha256"])

    def test_collection_current_approval_and_revocation(self):
        source = self.workspace / "skills"
        shutil.copytree(EXAMPLES / "demo-skills", source)
        self.invoke("collect", "--root", source)
        self.assertEqual(self.skills(), {})
        skill = self.skills(all_records=True)["pdf-reading"]
        self.assertEqual(self.invoke("route", "$pdf-reading")["selected"], [])
        self.invoke("load", skill["id"], success=False)
        self.invoke("approve", skill["id"], "--sha256", "0" * 64, success=False)
        self.approve(skill)
        result = self.invoke("route", "Dragðu saman helstu niðurstöðurnar úr PDF-skjalinu.")
        self.assertEqual([record["name"] for record in result["selected"]], ["pdf-reading"], result)
        self.assertIn("# Read a PDF", self.invoke("load", skill["id"])["body"])
        self.invoke("feedback", skill["id"], "success")
        self.assertEqual(self.skills()["pdf-reading"]["feedback"]["success"], 1)
        self.invoke("revoke", skill["id"])
        self.assertEqual(self.invoke("route", "$pdf-reading")["selected"], [])

    def test_subtask_dispatch_preserves_roles_and_can_abstain(self):
        self.invoke("collect", "--root", EXAMPLES / "demo-skills")
        for record in self.skills(all_records=True).values():
            self.approve(record)
        spec = json.loads((EXAMPLES / "subtasks.json").read_text(encoding="utf-8"))
        spec["subtasks"].append({"id": "matreidsla", "task": "Hvernig sýð ég kartöflur?"})
        file = self.workspace / "subtasks.json"
        file.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        assignments = self.invoke("assign", file)["assignments"]
        self.assertEqual([item["id"] for item in assignments], ["heimildir", "yfirferd", "matreidsla"])
        self.assertEqual([item["agent"] for item in assignments], ["yfirlesari", "kodarynir", None])
        self.assertEqual(
            [[match["name"] for match in item["selected"]] for item in assignments],
            [["pdf-reading"], ["defensive-code-review"], []],
            assignments,
        )
        spec["subtasks"][2]["id"] = "heimildir"
        file.write_text(json.dumps(spec), encoding="utf-8")
        self.assertIn("Duplicate subtask id", self.invoke("assign", file, success=False))

    def test_learning_to_hermes_and_back(self):
        drafts = self.workspace / "drafts"
        generated = self.invoke("learn", EXAMPLES / "workflow.json", "--out", drafts)
        self.assertFalse(generated["approved"])
        self.invoke("collect", "--root", drafts)
        skill = self.skills(all_records=True)["tafla-yfirferd"]
        self.assertEqual(self.skills(), {})
        self.approve(skill)
        hermes_skills = self.workspace / "hermes" / "skills"
        hermes_skills.mkdir(parents=True)
        self.invoke("export", skill["id"], "--out", hermes_skills)
        exported = hermes_skills / "tafla-yfirferd" / "SKILL.md"
        self.assertEqual(exported.read_bytes(), (drafts / "tafla-yfirferd" / "SKILL.md").read_bytes())
        self.assertFalse((exported.parent / ".ofsafaerni-draft.json").exists())
        self.invoke("export", skill["id"], "--out", hermes_skills, success=False)
        self.invoke("collect", "--root", hermes_skills)
        indexes = self.invoke("list", "--all")["skills"]
        self.assertEqual(len(indexes), 2)
        self.assertEqual(sum(record["approved"] for record in indexes), 1)

    def test_hook_is_advisory_and_keeps_prompt_private(self):
        config = self.invoke("hook-config")
        command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn("ofsafaerni", command)
        prompt = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "$pdf-reading internal-receipt"})
        self.assertEqual(self.invoke("codex-hook", input_data=prompt), {})
        self.invoke("collect", "--root", EXAMPLES / "demo-skills")
        self.assertEqual(self.invoke("codex-hook", input_data=prompt), {})
        skill = self.skills(all_records=True)["pdf-reading"]
        self.approve(skill)
        reply = self.invoke("codex-hook", input_data=prompt)["hookSpecificOutput"]
        self.assertEqual(reply["hookEventName"], "UserPromptSubmit")
        self.assertIn(skill["id"], reply["additionalContext"])
        self.assertNotIn("internal-receipt", reply["additionalContext"])
        self.assertEqual(self.invoke("codex-hook", input_data="invalid JSON"), {})
        self.assertEqual(self.invoke("codex-hook", input_data='"' + "x" * 66_000 + '"'), {})

    def test_hook_config_quotes_paths_for_powershell(self):
        self.db = self.workspace / "O'Brien'S Docs" / "catalog.sqlite3"
        config = self.invoke("hook-config", "--shell", "powershell")
        command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertTrue(command.startswith("& '"))
        self.assertIn("'" + str(self.db).replace("'", "''") + "'", command)
        self.assertTrue(command.endswith("'codex-hook'"))

    def test_changed_bundle_leaves_routing_shortlist_until_reapproved(self):
        source = self.workspace / "skills"
        shutil.copytree(EXAMPLES / "demo-skills", source)
        self.invoke("collect", "--root", source)
        skill = self.skills(all_records=True)["pdf-reading"]
        self.approve(skill)
        candidate = source / "pdf-reading" / "notes.txt"
        candidate.write_text("A new companion file.", encoding="utf-8")
        self.assertEqual(self.invoke("route", "$pdf-reading")["selected"], [])
        self.assertEqual(self.skills(all_records=True)["pdf-reading"]["status"], "changed")
        self.approve(skill)
        self.assertEqual(self.invoke("route", "$pdf-reading")["selected"][0]["id"], skill["id"])


if __name__ == "__main__":
    unittest.main()
