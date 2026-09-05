# Evaluating agent behavior

Tests prove that a tool implements its contract. Evals measure whether a model
can use that contract to finish a task. Keep both: passing an eval does not
prove authorization, encryption, atomicity, or correct retry behavior.

## Run locally

Install [uv](https://docs.astral.sh/uv/) and start Docker. From the repository root:

```sh
uv sync --locked --group evals
uv run python -m devtools.postgres
uv run python -m devtools.postgres -- uv run --group evals python -m evals.run --split all
```

The first command after installation runs **all** tests, including PostgreSQL
tests. The second runs a scripted reference solution against every eval fixture;
it costs nothing and validates the environment and graders. It is **not an LLM
benchmark**. Each invocation starts a loopback-only PostgreSQL container with a
random password/port, no persistent volume, and cleanup on exit. Each eval case
gets a separate schema, random encryption keys, and the production store and MCP
adapter. An existing disposable database can be supplied with
`AGENT_FILETREE_MEMORY_TEST_DATABASE_URL` instead of the Docker wrapper.

For an actual model run, set `OPENROUTER_API_KEY` in the environment or an ignored
root `.env`, then run two cheap smoke cases first:

```sh
uv run python -m devtools.postgres -- uv run --group evals python -m evals.run \
  --driver openrouter --model z-ai/glm-5.3-flash \
  --case find-decision --case targeted-edit --output eval-results/smoke.json
```

OpenRouter uses only the inference key; the runner never needs a management key.
Use a dedicated key with a spending cap. Prices/provider availability change;
check the [model page](https://openrouter.ai/z-ai/glm-5.3-flash) before longer runs.
For another provider, use `--driver api --model provider:model` and its normal
environment credentials (OpenAI and Anthropic adapters are installed).

## Compare changes

Before changing tools, run `--split dev --repeat 3 --label baseline --output
eval-results/baseline.json`. Run the same command after the change with label
`candidate` and a new output path. Then:

```sh
uv run --group evals python -m evals.compare eval-results/baseline.json eval-results/candidate.json
```

Keep model, dataset, budgets, repeat count, and provider settings fixed. Reports
record the Git commit, dirty state, complete MCP catalog/instructions and their
hash, dataset hash, tool arguments/results, usage, latency, and final saved state.
The model sees the task and real MCP schemas; it never receives expected state or
the scripted solution. The API driver invokes the actual MCP protocol via the
FastMCP client, without shell access. It uses sequential calls, a per-case timeout,
request/token/tool-call limits, and reports failures with a nonzero exit status.
For historical comparisons, run the same frozen dataset from two Git worktrees;
do not recreate an approximate old tool description from memory.

Read failed traces before changing a prompt. Check final state, tool errors,
response bytes, calls, tokens, and latency together. A model claiming success is
not evidence that it saved anything. State graders compare the **entire tree**,
catching collateral edits, extra files, and incomplete writes. Retrieval tasks
also check specific factual answers. Multiple valid tool sequences are allowed.
The grader itself has negative tests (for example, saying “done” without saving).

Use `--split validation` only after choosing a candidate on `dev`. These small,
synthetic validation cases are a development guard against tuning to one prompt,
not a statistically independent benchmark. Add unseen cases and repeat trials
before claiming general improvement. Report sample sizes and failures; never
present reference replay as a model success rate. Compare repeated success rates
and consistency, not just the best run. Add cases from real failures after
removing all private information. Keep prompts at the task level; don't tell the
model which tool sequence the grader prefers.

## Optional Logfire

Authenticate/select your own project with the Logfire CLI, or supply its write
token through the environment, then add `--logfire`. Pydantic Evals uploads the
experiment for inspection in Logfire. Without the flag, results stay local even
if ambient Logfire credentials exist. Only synthetic fixtures belong here;
reports contain plaintext task data and tool arguments and are Git-ignored.
The runner's state/usage graders work without hosted spans or an LLM judge.

Don't add a judge where exact state can answer the question. For a future
subjective criterion (such as summary usefulness), define a narrow pass/fail
rubric, calibrate it against human-labeled examples, and record the judge model
and prompt. A judge that hasn't been calibrated is another unmeasured model.

## Why Pydantic Evals?

[Pydantic Evals](https://pydantic.dev/docs/ai/evals/evals/) fits the existing Python
project, supports code-based graders and repeated experiments, runs locally,
and can send results to Logfire. [Inspect](https://inspect.aisi.org.uk/tools.html)
is a strong option for broader agent benchmarks and sandboxed tasks.
[Promptfoo](https://www.promptfoo.dev/docs/providers/mcp/) is useful for
configuration-driven provider comparisons and MCP robustness/red-team testing.
We use one framework here to keep the contributor workflow small.

The design follows the emphasis on verifiable outcomes, readable traces, and
held-out tasks in Anthropic's [tool design guide](https://www.anthropic.com/engineering/writing-tools-for-agents)
and [agent eval guide](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).
