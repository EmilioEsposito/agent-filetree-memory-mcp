from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_domain_layer_has_no_adapter_imports() -> None:
    forbidden_roots = {
        "alembic",
        "asyncpg",
        "fastmcp",
        "jwt",
        "prefab_ui",
        "sqlalchemy",
    }
    findings: list[str] = []
    for path in (ROOT / "src" / "agent_filetree_memory" / "domain").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".", 1)[0]}
            else:
                continue
            overlap = roots & forbidden_roots
            if overlap:
                findings.append(f"{path.name}: {sorted(overlap)}")
    assert not findings, findings


def test_retention_docs_require_an_explicit_host_janitor() -> None:
    public_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "README.md", ROOT / "docs" / "architecture.md")
    ).lower()
    assert "agent-filetree-memory-janitor" in public_text
    assert "does not schedule cleanup itself" in public_text
    assert "cleanup is scheduled" not in public_text
