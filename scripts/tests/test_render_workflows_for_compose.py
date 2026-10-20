from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "render-workflows-for-compose.py"
SPEC = importlib.util.spec_from_file_location("render_workflows_for_compose", SCRIPT)
assert SPEC and SPEC.loader
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


def _walk_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, str):
        yield value


def test_rendered_workflows_are_standalone_and_use_compose_service_names(tmp_path):
    paths = renderer.render_workflows(tmp_path)

    assert {path.name for path in paths} == {"cpg-ingester.sw.yaml", "acp-writer.sw.yaml"}
    assert (tmp_path / "application.properties").is_file()
    cpg, acp = (yaml.safe_load(path.read_text()) for path in paths)
    assert (cpg["id"], cpg["version"]) == ("cpgingester", "0.3.0")
    assert (acp["id"], acp["version"]) == ("acpwriter", "0.3.0")
    assert "flow" not in cpg and "flow" not in acp

    cpg_text = "\n".join(_walk_strings(cpg))
    acp_text = "\n".join(_walk_strings(acp))
    assert "cpg-ingester-llm-analysis" not in cpg_text
    assert "http://cpg-ingester-llm:8080/api/v1/analyze-async" in cpg_text
    assert "http://sonataflow-devmode:8080/wait-parse" in cpg_text
    assert "http://acp-writer-bff:8080/api/v1/notifications/artifacts-available" in cpg_text
    assert "http://acpwriter:80/" not in acp_text
    assert "http://sonataflow-devmode:8080/wait-scan" in acp_text
    assert "http://acp-patient-data:8080/api/v1/scan-async" in acp_text
    properties = (tmp_path / "application.properties").read_text()
    assert "mp.messaging.incoming.parse-done.path=/wait-parse" in properties
    assert "mp.messaging.incoming.careplan-reviewed.path=/wait-careplan-review" in properties


def test_url_rewrite_preserves_operation_and_path():
    value = "rest:post:http://acpwriter:80/wait-compose"

    assert renderer._rewrite_url_hosts(value) == "rest:post:http://sonataflow-devmode:8080/wait-compose"


def test_compose_profiles_do_not_reference_slice_runner_variables():
    repo_root = Path(__file__).resolve().parents[2]

    for compose_name in ("compose.yml", "compose.orchestrated.yml"):
        compose_text = (repo_root / compose_name).read_text(encoding="utf-8")
        assert "SLICE_" not in compose_text, f"{compose_name} contains a slice-runner variable"
