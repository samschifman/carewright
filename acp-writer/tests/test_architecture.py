"""Architecture boundary test: production code must not import from benchmark."""

import ast
import re
from pathlib import Path


def _get_imports(filepath: Path) -> list[str]:
    """Extract all import module names from a Python file."""
    try:
        tree = ast.parse(filepath.read_text())
    except SyntaxError:
        return []
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports


def test_no_benchmark_imports_from_production():
    """Nothing under src/acp_writer/ outside benchmark/ may import from acp_writer.benchmark."""
    src = Path(__file__).parent.parent / "src" / "acp_writer"
    violations = []
    for py_file in src.rglob("*.py"):
        rel = py_file.relative_to(src)
        if str(rel).startswith("benchmark"):
            continue
        if "__pycache__" in str(rel):
            continue
        for imp in _get_imports(py_file):
            if imp.startswith("acp_writer.benchmark"):
                violations.append(f"{rel}: imports {imp}")

    assert not violations, (
        "Production code imports from benchmark:\n" + "\n".join(violations)
    )


def test_decision_engine_service_does_not_import_langchain():
    """The decision-engine service must not pull langchain_openai.

    This enforces the thin-wrapper property: the decision pod has no
    LLM dependency. Resolution lives in the LLM-reasoning pod.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c",
         "import acp_writer.services.decision_engine; "
         "import sys; "
         "assert 'langchain_openai' not in sys.modules, "
         "'decision_engine imports langchain_openai'"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"decision_engine service imports langchain_openai:\n{result.stderr}"
    )


def test_acp_writer_does_not_import_downstream_components():
    """The writer owns its registry and consumes only the shared contracts."""
    src = Path(__file__).parent.parent / "src" / "acp_writer"
    violations = []
    for py_file in src.rglob("*.py"):
        if "__pycache__" in str(py_file):
            continue
        for imp in _get_imports(py_file):
            if imp.startswith(("cpg_ingester", "automation_service")):
                violations.append(f"{py_file.relative_to(src)}: imports {imp}")
    assert not violations, "Cross-component imports found:\n" + "\n".join(violations)


def test_nodes_and_services_do_not_lazy_import_the_api_module():
    src = Path(__file__).parent.parent / "src" / "acp_writer"
    violations = []
    for directory in (src / "nodes", src / "services"):
        for py_file in directory.rglob("*.py"):
            tree = ast.parse(py_file.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for child in ast.walk(node):
                    if isinstance(child, ast.ImportFrom) and child.module == "acp_writer.api":
                        violations.append(f"{py_file.relative_to(src)}:{child.lineno}")
                    elif isinstance(child, ast.Import):
                        if any(alias.name == "acp_writer.api" for alias in child.names):
                            violations.append(f"{py_file.relative_to(src)}:{child.lineno}")
    assert not violations, "Lazy API imports in nodes/services:\n" + "\n".join(violations)


def test_acp_writer_tests_use_owned_fixtures():
    tests = Path(__file__).parent
    references = [
        f"{path.relative_to(tests)}:{line_number}"
        for path in tests.rglob("*.py")
        for line_number, line in enumerate(path.read_text().splitlines(), 1)
        if "cpg-" + "ingester" in line
    ]
    assert not references, "acp-writer tests must not read upstream ingester data:\n" + "\n".join(references)


def test_automation_image_copies_only_the_shared_package_inputs():
    root = Path(__file__).resolve().parents[2]
    containerfile = (root / "automation" / "deploy" / "Containerfile").read_text()
    shared_copies = [line for line in containerfile.splitlines() if line.startswith("COPY shared/")]
    assert shared_copies == [
        "COPY shared/pyproject.toml /app/shared/pyproject.toml",
        "COPY shared/src/ /app/shared/src/",
    ]


def test_slice_mounts_live_only_in_the_slice_compose_override():
    root = Path(__file__).resolve().parents[2]
    base = (root / "compose.yml").read_text()
    slice_override = (root / "compose.slice.yml").read_text()
    for token in ("ACP_RUN_ARTIFACT_DIR", "CPG_INGESTER_MOCK_RUN_DIR", "./working/slice-run"):
        assert token not in base
        assert token in slice_override


def test_mock_scenario_is_not_a_container_build_argument():
    root = Path(__file__).resolve().parents[2]
    for containerfile in (
        root / "acp-writer" / "ui" / "Containerfile",
        root / ("cpg-" + "ingester") / "ui" / "Containerfile",
    ):
        assert "VITE_MOCK_SCENARIO" not in containerfile.read_text()


def test_state_models_do_not_define_credential_fields():
    """State TypedDicts and models must not carry credentials into traces/checkpoints."""
    src = Path(__file__).parent.parent / "src" / "acp_writer"
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
    src = Path(__file__).parent.parent / "src" / "acp_writer"
    hits = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if "llm_api_key" in path.read_text()
    ]
    assert not hits, "Credential state field literal found in: " + ", ".join(hits)
