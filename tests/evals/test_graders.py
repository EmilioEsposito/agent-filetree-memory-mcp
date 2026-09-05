from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic_evals")

from evals.cases import scenarios
from evals.graders import Outcome, TaskSuccess


def score(case, outcome):
    return TaskSuccess().evaluate(SimpleNamespace(inputs=case, output=outcome))


def test_reference_outcomes_satisfy_every_oracle():
    for case in scenarios():
        result = score(case, Outcome(" ".join(case.answer_contains), case.expected))
        assert result["state_correct"] and result["answer_correct"] and result["completed"]


def test_claiming_success_without_saving_fails():
    case = next(c for c in scenarios() if c.name == "targeted-edit")
    assert not score(case, Outcome("Done", case.files))["state_correct"]


def test_collateral_changes_and_extra_files_fail():
    case = scenarios()[0]
    for files in ({**case.expected, "/unexpected.md": "extra"}, {}):
        assert not score(case, Outcome("Mira PostgreSQL", files))["state_correct"]


def test_missing_answer_and_timeout_are_failures():
    case = scenarios()[0]
    result = score(case, Outcome("I found it", case.expected, error="TimeoutError"))
    assert not result["answer_correct"] and not result["completed"]


def test_dataset_names_unique_and_validation_split_present():
    cases = scenarios()
    assert len({c.name for c in cases}) == len(cases)
    assert {c.split for c in cases} == {"dev", "validation"}
