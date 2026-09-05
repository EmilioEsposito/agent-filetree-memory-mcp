"""Grade state independently of the agent's claims and preferred tool sequence."""

from dataclasses import dataclass, field

from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from .cases import Scenario


def state_is_correct(case: Scenario, files: dict[str, str]) -> bool:
    return files.keys() == case.expected.keys() and all(
        files[path] in (expected, *case.acceptable_variants.get(path, ()))
        for path, expected in case.expected.items()
    )


@dataclass
class Outcome:
    answer: str
    files: dict[str, str]
    calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    error: str | None = None
    messages: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def grade_outcome(case: Scenario, out: Outcome) -> dict[str, bool]:
    """One success definition for JSON reports and Pydantic Evals."""
    return {
        "state_correct": state_is_correct(case, out.files),
        "answer_correct": all(
            term.casefold() in out.answer.casefold() for term in case.answer_contains
        ),
        "completed": out.error is None,
    }


def state_difference(case: Scenario, files: dict[str, str]) -> dict[str, list[str]]:
    return {
        "missing": sorted(case.expected.keys() - files.keys()),
        "extra": sorted(files.keys() - case.expected.keys()),
        "changed": sorted(
            path
            for path in case.expected.keys() & files.keys()
            if files[path]
            not in (case.expected[path], *case.acceptable_variants.get(path, ()))
        ),
    }


@dataclass
class TaskSuccess(Evaluator[Scenario, Outcome]):
    def evaluate(self, ctx: EvaluatorContext[Scenario, Outcome]):
        case, out = ctx.inputs, ctx.output
        # Whole-tree equality catches collateral edits, missing files, and extra files.
        return {
            **grade_outcome(case, out),
            "tool_calls": len(out.calls),
            "tool_errors": sum(bool(call.get("error")) for call in out.calls),
            "response_bytes": sum(call.get("response_bytes", 0) for call in out.calls),
        }
