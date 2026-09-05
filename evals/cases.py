"""Synthetic tasks and independent state oracles. Never seed real memories."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scenario:
    name: str
    prompt: str
    files: dict[str, str]
    expected: dict[str, str]
    answer_contains: tuple[str, ...] = ()
    split: str = "dev"
    # A reference solution validates the fixture/grader, not LLM performance.
    writes: dict[str, str] = field(default_factory=dict)
    acceptable_variants: dict[str, tuple[str, ...]] = field(default_factory=dict)


def scenarios() -> list[Scenario]:
    noise = {
        f"/archive/team-{i % 4}/note-{i}.md": f"# Retrospective {i}\nOwner: team-{i % 4}\nStatus: archived\n"
        for i in range(24)
    }
    config = "# Service\nRegion: eu-west\nRetry limit: 3\nOwner: platform\n"
    duplicate = "# Primary\nTimeout: 30\n\n# Backup\nTimeout: 30\n"
    large = (
        "# Runbook\n"
        + "".join(f"Step {i}: verify subsystem {i}.\n" for i in range(1, 601))
        + "Escalation code: ORCHID-73\n"
    )
    base = {
        **noise,
        "/ops/service.md": config,
        "/ops/failover.md": duplicate,
        "/ops/runbook.md": large,
        "/projects/harbor/decision.md": "# Harbor\nStorage: PostgreSQL\nOwner: Mira\n",
        "/projects/harbor/status.md": "# Harbor\nStage: pilot\n",
        "/people/preferences.md": "# Preferences\nLanguage: English\n",
    }

    def task(name, prompt, writes=None, answer=(), split="dev", variants=None):
        writes = writes or {}
        return Scenario(
            name,
            prompt,
            dict(base),
            {**base, **writes},
            answer,
            split,
            writes,
            variants or {},
        )

    return [
        task(
            "find-decision",
            "Who owns Harbor's storage decision, and what storage was chosen?",
            answer=("Mira", "PostgreSQL"),
        ),
        task(
            "targeted-edit",
            "Change the service retry limit from 3 to 5. Preserve everything else.",
            {"/ops/service.md": config.replace("Retry limit: 3", "Retry limit: 5")},
        ),
        task(
            "ambiguous-edit",
            "Change only the Backup timeout in the failover note to 60. Keep Primary at 30.",
            {
                "/ops/failover.md": duplicate.replace(
                    "# Backup\nTimeout: 30", "# Backup\nTimeout: 60"
                )
            },
        ),
        task(
            "read-tail",
            "Find the escalation code in the operations runbook.",
            answer=("ORCHID-73",),
        ),
        task(
            "large-edit",
            "In the operations runbook, change step 475 to 'Step 475: verify standby subsystem 475.' Preserve every other byte, including the final newline.",
            {
                "/ops/runbook.md": large.replace(
                    "Step 475: verify subsystem 475.",
                    "Step 475: verify standby subsystem 475.",
                )
            },
        ),
        task(
            "append-once",
            "Add a new final line 'Reviewed: yes' to the Harbor status note. Preserve the existing text.",
            {
                "/projects/harbor/status.md": base["/projects/harbor/status.md"]
                + "Reviewed: yes\n"
            },
            # The task does not demand a terminal newline. Both representations
            # are valid; still require exact preservation of the existing prefix.
            variants={
                "/projects/harbor/status.md": (
                    base["/projects/harbor/status.md"] + "Reviewed: yes",
                )
            },
        ),
        task(
            "create-note",
            "Create /decisions/cache.md with exactly these two lines, including the final newline:\n# Cache\nProvider: Redis\n",
            {"/decisions/cache.md": "# Cache\nProvider: Redis\n"},
        ),
        task(
            "cross-directory",
            "Which active Harbor notes mention PostgreSQL? Give their full virtual paths.",
            answer=("/projects/harbor/decision.md",),
            split="validation",
        ),
        task(
            "unicode-preservation",
            "Update the preferred language from English to Português in the preferences note, preserving all other text.",
            {"/people/preferences.md": "# Preferences\nLanguage: Português\n"},
            split="validation",
        ),
        task(
            "literal-punctuation",
            "Add the exact line 'Pattern: [a-z]+ (literal)' at the end of the service note, followed by a newline.",
            {"/ops/service.md": config + "Pattern: [a-z]+ (literal)\n"},
            split="validation",
        ),
    ]
