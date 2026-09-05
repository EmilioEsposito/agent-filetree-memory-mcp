"""Grade state independently of the agent's claims and preferred tool sequence."""

from dataclasses import dataclass, field

from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from .cases import Scenario


@dataclass
class Outcome:
    answer: str
    files: dict[str, str]
    calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class TaskSuccess(Evaluator[Scenario, Outcome]):
    def evaluate(self, ctx: EvaluatorContext[Scenario, Outcome]):
        case, out = ctx.inputs, ctx.output
        # Whole-tree equality catches collateral edits, missing files, and extra files.
        return {
            "state_correct": out.files == case.expected,
            "answer_correct": all(term.casefold() in out.answer.casefold()
                                  for term in case.answer_contains),
            "completed": out.error is None,
            "tool_calls": len(out.calls),
            "tool_errors": sum(bool(call.get("error")) for call in out.calls),
            "response_bytes": sum(call.get("response_bytes", 0) for call in out.calls),
        }
