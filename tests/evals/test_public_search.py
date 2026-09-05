from dataclasses import replace
import json
from pathlib import PurePosixPath
import re

import pytest

pytest.importorskip("pydantic_evals")

from evals.cases import scenarios
from evals.graders import answer_is_correct, state_is_correct


def test_suites_are_separate_and_provenance_is_pinned():
    original = scenarios()
    public = scenarios("public-search")
    assert len(original) == 10 and len(public) == 6
    assert len(scenarios("all")) == 16
    assert len({c.name for c in scenarios("all")}) == 16
    for case in public:
        assert len(case.provenance["revision"]) == 40
        assert len(case.provenance["source_file_sha256"]) == 64
        assert case.provenance["license"] == "Apache-2.0"
        assert case.provenance["original_prompt"]
        assert case.provenance["adaptations"]
        assert state_is_correct(case, case.files)
        assert answer_is_correct(case, json.dumps(case.answer_json))
    with pytest.raises(ValueError, match="unknown suite"):
        scenarios("typo")


def test_gold_answers_recomputed_from_fixtures_without_mcp_or_reference_driver():
    cases = {c.name: c for c in scenarios("public-search")}
    case = cases["search-exact-basename"]
    assert set(case.answer_json["paths"]) == {
        p for p in case.files if PurePosixPath(p).name == "TOOLS.md"
    }
    case = cases["search-file-absence"]
    assert case.answer_json["exists"] is any(
        p.startswith("/working/") and PurePosixPath(p).name == "workspace.md"
        for p in case.files
    )
    case = cases["search-hidden-filter"]
    assert case.answer_json["count"] == sum(
        PurePosixPath(p).parent == PurePosixPath("/usr")
        and PurePosixPath(p).name.startswith(".")
        and "u" not in PurePosixPath(p).name
        for p in case.files
    )
    case = cases["search-recursive-suffix"]
    count = sum(
        p.startswith("/projects/") and p.endswith(".decision.md") for p in case.files
    )
    assert case.answer_json["count"] == count > 100
    case = cases["search-word-lines"]
    count = sum(
        "error" in re.split(r"[^a-z0-9_]+", line.lower())
        for p, content in case.files.items()
        if PurePosixPath(p).parent == PurePosixPath("/logs") and p.endswith(".log.md")
        for line in content.splitlines()
    )
    assert case.answer_json["count"] == count > 50
    case = cases["search-log-set-difference"]
    records = [
        [field.strip() for field in line.split("|")]
        for line in case.files["/trades/stock.log.md"].splitlines()
        if "|" in line
    ]
    sold = {r[2] for r in records if r[:2] == ["Bob", "Sell"]}
    bought = {r[2] for r in records if r[:2] == ["Bob", "Purchase"]}
    assert set(case.answer_json["symbols"]) == sold - bought
    assert case.answer_json["count"] == len(sold - bought)


def test_path_grader_rejects_missing_extra_duplicate_and_wrong_case_results():
    case = scenarios("public-search")[0]
    paths = case.answer_json["paths"]
    assert answer_is_correct(case, json.dumps({"paths": list(reversed(paths))}))
    for invalid in (
        paths[:1],
        paths + ["/wrong.md"],
        paths + paths[:1],
        [p.lower() for p in paths],
        [p + ".bak" for p in paths],
        [True, 1],
    ):
        assert not answer_is_correct(case, json.dumps({"paths": invalid}))


@pytest.mark.parametrize(
    "answer",
    [
        '{"count": 13}',
        '{"count": "3"}',
        '{"count": 3.0}',
        '{"count": 3, "count": 3}',
        '{"count": 3, "extra": "ignored?"}',
        "Found 3 matching files.",
        "null",
        "[]",
        '{"count": 3} trailing prose',
        '{"count": NaN}',
        '{"count": Infinity}',
    ],
)
def test_count_grader_requires_exact_typed_answer(answer):
    case = scenarios("public-search")[2]
    assert not answer_is_correct(case, answer)


def test_boolean_cannot_impersonate_integer_and_fences_are_accepted():
    case = scenarios("public-search")[2]
    assert answer_is_correct(case, '```json\n{"count": 3}\n```')
    assert not answer_is_correct(
        replace(case, answer_json={"count": 1}), '{"count": true}'
    )
    absence = scenarios("public-search")[1]
    assert not answer_is_correct(absence, '{"exists": 0}')
    assert not answer_is_correct(absence, '{"exists": "false"}')


def test_retrieval_cannot_mutate_or_dump_the_corpus_to_pass():
    for case in scenarios("public-search"):
        assert not state_is_correct(case, {**case.files, "/answer.md": "done"})
        assert not answer_is_correct(case, json.dumps(case.files))
