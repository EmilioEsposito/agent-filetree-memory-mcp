"""Modified AgentBench-OS tasks; new deterministic Markdown fixtures.

Upstream: THUDM/AgentBench (Apache-2.0). See fixtures/public_search for pinned
record provenance, modification notices, and the upstream license.
"""

import json
from pathlib import Path

from .cases import Scenario


def search_scenarios() -> list[Scenario]:
    provenance = json.loads(
        (Path(__file__).parent / "fixtures/public_search/provenance.json").read_text()
    )
    noise = {
        f"/archive/team-{i % 4}/note-{i:02}.md": f"# Archived note {i}\nStatus: closed\n"
        for i in range(16)
    }

    def task(name, prompt, files, answer):
        files = {**noise, **files}
        return Scenario(
            name=name,
            prompt=prompt
            + " Do not modify any files. Return only a JSON object: "
            + {
                "search-exact-basename": '{"paths": [full virtual paths]}. Order does not matter.',
                "search-file-absence": '{"exists": true or false}.',
                "search-log-set-difference": '{"count": integer, "symbols": [stock symbols]}. Order does not matter.',
            }.get(name, '{"count": integer}.'),
            files=files,
            expected=dict(files),
            answer_json=answer,
            provenance=provenance[name],
        )

    basename = {
        "/workspace/teams/field notes/TOOLS.md": "# Field toolkit\n",
        "/workspace/reference/TOOLS.md": "# Reference toolkit\n",
        "/workspace/TOOLS.md/overview.md": "# Directory named TOOLS.md\n",
        "/workspace/reference/tools.md": "# Lowercase toolkit\n",
        "/workspace/reference/TOOLS.md.bak.md": "# Backup\n",
        "/workspace/reference/MYTOOLS.md": "# Personal toolkit\n",
    }
    absence = {
        "/working/.tmp.md": "# Temporary\n",
        "/working/OS/.theme.md": "# Theme\n",
        "/working/OS/workspace.md/index.md": "# Directory, not a document\n",
        "/working/OS/workspace-notes.md": "# Workspace notes\n",
        "/working/OS/Workspace.md": "# Different case\n",
        "/elsewhere/workspace.md": "# Outside the requested directory\n",
    }
    hidden = {
        "/usr/.tmp.md": "# Temporary\n",
        "/usr/.tm12p.md": "# Temporary 12\n",
        "/usr/.f.md": "# Hidden file\n",
        "/usr/.u.md": "# Excluded letter\n",
        "/usr/public.md": "# Visible file\n",
        "/usr/sub/.nested.md": "# Nested file\n",
        "/usr/.cache/index.md": "# Hidden directory\n",
    }
    suffix = {
        f"/projects/team-{i % 4}/record-{i:03}.decision.md": f"# Decision {i}\n"
        for i in range(123)
    }
    suffix.update(
        {
            "/projects/.decision.md": "# Hidden decision\n",
            "/projects/record.decision.md/summary.md": "# Directory decoy\n",
            "/projects/record.decision.md.bak.md": "# Backup decoy\n",
            "/projects/record.DECISION.md": "# Case decoy\n",
            "/elsewhere/record.decision.md": "# Scope decoy\n",
        }
    )
    for i in range(16):
        suffix[f"/projects/team-{i % 4}/status-{i:03}.md"] = "# Status\n"

    log_lines = ["# Service log"]
    for i in range(80):
        log_lines.extend(
            [
                f"event {i}: Error ERROR error",  # one matching line, three occurrences
                f"event {i}: errors terror error_code",  # no whole-word matches
                f"event {i}: success",
            ]
        )
    word_lines = {
        "/logs/service.log.md": "\n".join(log_lines) + "\n",
        "/logs/worker.log.md": "# Worker log\nERROR: reset\nerror-free\n(error)\nerrors\n",
        "/logs/notes.md": "error in a non-log note\n",
        "/logs/old/worker.log.md": "error in a nested log\n",
        "/elsewhere/service.log.md": "error outside the scope\n",
    }
    trades = [
        "# Stock transactions",
        "Trader | Action | Symbol | Quantity",
        "Bob | Sell | ALFA | 3",
        "Bob | Sell | BETA | 7",
        "Bob | Sell | DELT | 1",
        "Bob | Sell | MOSS | 9",
        "Bob | Sell | ALFA | 4",
        "Bob | Purchase | BETA | 1",
        "Bob | Purchase | CEDR | 5",
        "Bobby | Purchase | ALFA | 3",
        "Alice | Purchase | DELT | 1",
        "Bobbie | Sell | PINE | 9",
    ]
    # Put another relevant purchase beyond the default read page. Repetition
    # tests set semantics; trader prefixes must not count as Bob.
    trades.extend(f"Alice | Sell | FERN | {i + 1}" for i in range(220))
    trades.extend(["Bob | Sell | ELM | 1", "Bob | Purchase | ELM | 2"])
    stock = {
        "/trades/stock.log.md": "\n".join(trades) + "\n",
        "/trades/old/stock.log.md": "Bob | Purchase | MOSS | 1\n",
    }

    return [
        task(
            "search-exact-basename",
            "Find every document anywhere in memory whose filename is exactly TOOLS.md (case-sensitive). Give its full virtual path.",
            basename,
            {
                "paths": [
                    "/workspace/teams/field notes/TOOLS.md",
                    "/workspace/reference/TOOLS.md",
                ]
            },
        ),
        task(
            "search-file-absence",
            "Does a document named exactly workspace.md (case-sensitive) exist anywhere under /working, recursively? A directory with that name does not count.",
            absence,
            {"exists": False},
        ),
        task(
            "search-hidden-filter",
            "How many hidden documents directly inside /usr have filenames that do not contain the lowercase character u? Hidden means the filename begins with a dot. Exclude directories and descendants.",
            hidden,
            {"count": 3},
        ),
        task(
            "search-recursive-suffix",
            "How many documents under /projects, recursively, have filenames ending exactly in .decision.md (case-sensitive)? Include hidden documents; exclude directories.",
            suffix,
            {"count": 124},
        ),
        task(
            "search-word-lines",
            "Across documents directly inside /logs with filenames ending in .log.md, count lines containing the whole word error, case-insensitively (so ERROR counts). Letters, digits, and underscore are word characters. Count each matching line once even if error occurs repeatedly. Exclude subdirectories.",
            word_lines,
            {"count": 83},
        ),
        task(
            "search-log-set-difference",
            "In /trades/stock.log.md, which stock symbols did the trader named exactly Bob sell but never purchase? Each transaction has Trader | Action | Symbol | Quantity fields. Return the distinct symbols and their count; quantities do not affect the answer.",
            stock,
            {"count": 3, "symbols": ["ALFA", "DELT", "MOSS"]},
        ),
    ]
