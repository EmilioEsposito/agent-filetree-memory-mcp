"""Compare like-for-like experiments without treating a small sample as proof."""

import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import mean


def compare(before, after):
    for report in (before, after):
        if report.get("format_version") != 2:
            raise ValueError(
                "incomparable runs: regenerate both reports with the current harness (format 2)"
            )
        if report.get("status") != "complete":
            raise ValueError("incomparable runs: experiment is incomplete")
        selected = report["selected_cases"]
        repeat = report["settings"]["repeat"]
        expected = Counter({name: repeat for name in selected})
        if (
            not selected
            or len(set(selected)) != len(selected)
            or repeat < 1
            or len(report["runs"]) != report["expected_trials"]
            or report["expected_trials"] != len(selected) * repeat
            or Counter(row["case"] for row in report["runs"]) != expected
            or {(row["case"], row["trial"]) for row in report["runs"]}
            != {(name, trial) for name in selected for trial in range(1, repeat + 1)}
        ):
            raise ValueError(
                "incomparable runs: missing or duplicated case/repetition records"
            )
    for field in (
        "driver",
        "model",
        "dataset_sha256",
        "harness_sha256",
        "runtime_versions",
        "settings",
    ):
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
        for name in report["selected_cases"]:
            trials = [row for row in rows if row["case"] == name]
            passed = sum(row["success"] for row in trials)
            print(f"  {name}: {passed}/{len(trials)} successful")
            for row in trials:
                if not row["success"]:
                    failed = [
                        key for key, value in row.get("checks", {}).items() if not value
                    ]
                    print(
                        f"    trial {row['trial']}: {row.get('error') or ', '.join(failed) or 'failed'}"
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
