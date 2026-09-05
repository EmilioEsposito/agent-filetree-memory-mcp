"""Compare like-for-like experiments without treating a small sample as proof."""

import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import mean


def compare(before, after):
    for field in ("driver", "model", "dataset_sha256", "settings"):
        if before[field] != after[field]:
            raise ValueError(f"incomparable runs: {field} differs")
    if not before["runs"] or not after["runs"]:
        raise ValueError("incomparable runs: no completed case records")
    if Counter(r["case"] for r in before["runs"]) != Counter(
        r["case"] for r in after["runs"]
    ):
        raise ValueError("incomparable runs: case/repetition counts differ")
    if before["driver"] == "reference":
        print(
            "Reference replay checks fixture/grader plumbing; it does not measure LLM performance."
        )
    for label, report in (("before", before), ("after", after)):
        rows = report["runs"]
        print(
            f"{label}: {sum(r['success'] for r in rows)}/{len(rows)} successes; "
            f"{mean(len(r['calls']) for r in rows):.1f} calls/case; "
            f"{mean(sum(c.get('response_bytes', 0) for c in r['calls']) for r in rows):.0f} response bytes/case; "
            f"{report['case_errors']} setup/grading errors"
        )
        print(
            f"  tokens: {sum(r['usage'].get('input_tokens', 0) for r in rows):,} input, "
            f"{sum(r['usage'].get('output_tokens', 0) for r in rows):,} output"
        )
    return (
        1 if after["case_errors"] or not all(r["success"] for r in after["runs"]) else 0
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    raise SystemExit(
        compare(json.loads(args.before.read_text()), json.loads(args.after.read_text()))
    )
