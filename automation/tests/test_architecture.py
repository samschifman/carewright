"""Automation service may depend on shared contracts, not producer/runtime apps."""

import ast
from pathlib import Path


def test_service_has_no_cross_component_imports():
    source = Path(__file__).parents[1] / "src/automation_service"
    violations = []
    for file_path in source.rglob("*.py"):
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    if module.startswith(("cpg_ingester", "acp_writer")):
                        violations.append(f"{file_path.name}: {module}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(("cpg_ingester", "acp_writer")):
                    violations.append(f"{file_path.name}: {module}")
    assert not violations, "cross-component imports: " + ", ".join(violations)
