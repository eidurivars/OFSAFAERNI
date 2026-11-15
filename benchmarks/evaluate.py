"""Evaluate approved skill matching on a fixed bilingual sample."""

import argparse
import json
from pathlib import Path

from ofsafaerni.routing import route


def evaluate(suite: str = "holdout") -> dict:
    filenames = {"calibration": "bilingual.json", "holdout": "bilingual-holdout.json"}
    fixture = json.loads(Path(__file__).with_name(filenames[suite]).read_text(encoding="utf-8"))
    skills = [dict(record, id=record["name"], approved=True) for record in fixture["skills"]]
    groups = {}
    results = []
    for item in fixture["cases"]:
        output = route(item["query"], skills)
        selected = sorted(candidate["name"] for candidate in output["selected"])
        correct = selected == sorted(item["expected"])
        counts = groups.setdefault(item["group"], {"correct": 0, "total": 0})
        counts["correct"] += correct
        counts["total"] += 1
        results.append(
            {
                "case": item["id"],
                "expected": item["expected"],
                "selected": selected,
                "decision": output["decision"],
                "correct": correct,
            }
        )
    return {
        "suite": suite,
        "skills": len(skills),
        "cases": len(results),
        "correct": sum(item["correct"] for item in results),
        "by_group": groups,
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("holdout", "calibration"), default="holdout")
    args = parser.parse_args()
    print(json.dumps(evaluate(args.suite), ensure_ascii=True, indent=2))
