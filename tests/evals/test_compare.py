import pytest

from evals.compare import compare


def report():
    return {
        "format_version": 2,
        "status": "complete",
        "driver": "api",
        "model": "test",
        "dataset_sha256": "fixture",
        "harness_sha256": "harness",
        "runtime_versions": {},
        "selected_cases": ["one"],
        "expected_trials": 1,
        "settings": {"repeat": 1},
        "case_errors": 0,
        "runs": [
            {"case": "one", "trial": 1, "success": True, "calls": [], "usage": {}}
        ],
    }


def test_comparison_rejects_mixed_models_and_incomplete_samples():
    before = report()
    for change in ({"model": "different"}, {"runs": []}, {"runs": before["runs"] * 2}):
        with pytest.raises(ValueError, match="incomparable"):
            compare(before, {**report(), **change})


def test_failed_candidate_exits_nonzero():
    after = report()
    after["runs"][0]["success"] = False
    assert compare(report(), after) == 1
    assert compare(report(), report()) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"format_version": 1},
        {"status": "running"},
        {"harness_sha256": "changed-grader"},
        {"runtime_versions": {"pydantic-ai-slim": "different"}},
        {"selected_cases": ["one", "missing"], "expected_trials": 2},
    ],
)
def test_rejects_incomplete_or_methodologically_different_runs(change):
    with pytest.raises(ValueError, match="incomparable"):
        compare(report(), {**report(), **change})


def test_two_equally_truncated_reports_are_not_comparable():
    truncated = {**report(), "selected_cases": ["one", "missing"], "expected_trials": 2}
    with pytest.raises(ValueError, match="missing"):
        compare(truncated, truncated)


def test_repeated_trial_cannot_replace_missing_trial():
    repeated = {**report(), "settings": {"repeat": 2}, "expected_trials": 2}
    repeated["runs"] *= 2
    with pytest.raises(ValueError, match="duplicated"):
        compare(repeated, repeated)
