"""Architecture boundary checks for cpg-ingester production code."""

import ast
import re
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


def test_state_models_do_not_define_credential_fields():
    """State TypedDicts and models must not carry credentials into traces/checkpoints."""
    src = Path(__file__).parent.parent / "src" / "cpg_ingester"
    credential_field = re.compile(r"(?i)(api_key|secret|token|password)")
    violations = []

    for py_file in src.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if py_file.name != "state.py" and not node.name.endswith("State"):
                continue
            for child in node.body:
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    if credential_field.search(child.target.id):
                        violations.append(
                            f"{py_file.relative_to(src)}:{node.name}.{child.target.id}"
                        )

    assert not violations, "Credential fields found in state models: " + ", ".join(violations)


def test_src_contains_no_llm_api_key_literal():
    """Pipeline code must not name a credential state field."""
    src = Path(__file__).parent.parent / "src" / "cpg_ingester"
    hits = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if "llm_api_key" in path.read_text()
    ]
    assert not hits, "Credential state field literal found in: " + ", ".join(hits)
