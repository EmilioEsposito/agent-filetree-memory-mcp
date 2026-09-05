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
gets a separate schema initialized by the shipped migrations, random encryption
keys, and the production store and MCP adapter. This validates the same schema
installation path used by standalone deployments. An existing disposable database can be supplied with
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
Model messages and partial usage are retained on failures too, including argument
validation retries that never reach MCP. `--provider` pins an OpenRouter provider
and disables fallback so an experiment does not silently switch serving providers.
For historical comparisons, run the same frozen dataset and compatible harness from two Git worktrees;
do not recreate an approximate old tool description from memory.

Format 2 reports checkpoint each finished trial using atomic file replacement.
Setup, model, state-capture, and cleanup failures are retained with their phase;
timed-out models still have their final saved state captured when the database
is available. `--timeout` bounds setup and model execution together. State
capture and cleanup each have a separate 30-second bound. A failed cleanup
makes the trial fail; inspect the disposable database before reusing it.
Grader/framework exceptions also cause a nonzero exit status.

Comparisons require complete case/repetition coverage, matching harness and
dataset fingerprints, matching dependency versions, and matching model/budget
settings. Interrupted reports remain marked `running` and cannot be compared,
even when both reports are missing the same trials. Unknown case names and
cases outside the selected split fail before execution. Per-case successes,
failure phases, and missing/extra/changed file paths make regressions inspectable.
The catalog hash may differ because the tool surface is the experiment's target.
Historical format 1 reports remain readable JSON, but must be regenerated with
the same current harness before automated comparison; do not mix grader versions.

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

The validation partition also covers coordinated edits to two files and an
untrusted instruction embedded in a retrieved memory. Their graders check the
entire final tree, including preservation of unrelated files. Reference replay
validates these oracles; it does not demonstrate that a model resists injection.

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

## Initial 0.6 development experiment

On September 5, 2026, ten synthetic tasks ran three times per surface using
`openai/gpt-5.4-nano`, pinned to OpenRouter's `openai` provider, low reasoning,
4,096 output tokens per request, and the same fixture/grader revision.
The [machine-readable summary](../evals/results/0.6.0.json) records hashes and
per-case results. The previous seven-tool surface was unchanged through 0.5.1.

| Metric | Previous surface | Selected 0.6 surface |
| --- | ---: | ---: |
| Successful trials | 26/30 | 30/30 |
| Mean MCP calls | 4.53 | 3.53 |
| Mean serialized result bytes | 7,342 | 1,914 |
| Reported input tokens, total | 457,848 | 484,073 |
| Reported output tokens, total | 42,283 | 7,303 |

Three baseline failures required rewriting a 600-line document for a one-line
change and exhausted the per-response output budget. One inserted an extra
blank line beyond the requested append. The new edit tool avoided whole-document
rewrites. The larger catalog still increased input tokens about 6%; smaller
results do not automatically mean less prompt context. A shorter-description
variant also passed 30/30 but consumed 515,016 input tokens, so the clearer
descriptions were retained. Costs depend on input/output rates and caching.

These are development observations on a small, repeatedly inspected dataset,
including its validation partition during final tuning—not independent benchmark
evidence. The initial append grader was corrected to accept an unspecified
terminal newline; both surfaces were rerun with that same corrected grader.
Use fresh tasks and more trials before claiming broader model performance gains.
