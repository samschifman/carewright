"""Architecture boundary checks for cpg-ingester production code."""

import ast
from pathlib import Path


def _get_imports(filepath: Path) -> list[str]:
    """Return the imported module names from a Python source file."""
    try:
        tree = ast.parse(filepath.read_text())
    except SyntaxError:
        return []
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports


def test_production_does_not_import_downstream_components():
    """cpg-ingester uses published contracts rather than downstream internals."""
    src = Path(__file__).parent.parent / "src" / "cpg_ingester"
    violations = []
    for path in src.rglob("*.py"):
        relative = path.relative_to(src)
        if "__pycache__" in str(relative):
            continue
        for module in _get_imports(path):
            if module.startswith(("acp_writer", "automation_service")):
                violations.append(f"{relative}: imports {module}")
    assert not violations, "Cross-component imports found:\n" + "\n".join(violations)
