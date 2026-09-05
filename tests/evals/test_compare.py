import pytest

from evals.compare import compare


def report():
    return {
        "driver": "api",
        "model": "test",
        "dataset_sha256": "fixture",
        "settings": {},
        "case_errors": 0,
        "runs": [{"case": "one", "success": True, "calls": [], "usage": {}}],
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
