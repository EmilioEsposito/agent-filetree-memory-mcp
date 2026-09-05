# AgentBench-OS adaptations

Six modified tasks from [AgentBench](https://github.com/THUDM/AgentBench),
revision `d1e4a10db08c87075c78972e48ecc182be03e2d5`, licensed under
[Apache-2.0](AGENTBENCH_LICENSE.txt). Credit: the AgentBench authors.
The upstream repository contains no separate NOTICE file at this revision.

[`provenance.json`](provenance.json) records the original prompt, source URL,
JSON Pointer (zero-based array index; empty means the root object), source-file
and record SHA-256, and modifications for each case. Record hashes use UTF-8
`json.dumps(record, sort_keys=True, ensure_ascii=False)` with default separators.
The original shell setup/checker scripts are not copied or executed.

[`../../public_search.py`](../../public_search.py) contains rewritten prompts
and newly authored deterministic fixtures. Linux paths/extensions become virtual
Markdown paths; added decoys exercise directory/file distinctions, search scope,
case sensitivity, and pagination. The hidden-file task follows the upstream
prompt's `u` exclusion and nonrecursive scope, correcting its example command's
`k` exclusion and recursion. The word-search task explicitly includes `ERROR`,
resolving contradictory case-sensitivity wording in the original.

Gold answers were independently recomputed from the fixtures in unit tests.
The free reference driver computes answers from real MCP tool results, including
pagination. Model runs receive only the rewritten task and the MCP catalog;
provenance, gold answers, and reference code are not in the model context.

This is a small adapted development suite, **not an official AgentBench score**.
New fixtures, changed prompts, and a different tool environment prevent direct
comparison to upstream results. Public provenance also means these are not secret
held-out tasks. No upstream benchmark package, corpus download, or network access
is needed to run the reference suite.
