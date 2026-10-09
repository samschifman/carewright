"""Static validation ladder integration tests."""

import json
from pathlib import Path

from lxml import etree

from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.ir import ProcessIR, ir_version
from cpg_contracts.automation.validators.ladder import run_static_ladder


FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
ACP_NS = "https://github.com/samschifman/cpg-to-acp/bpmn"
DROOLS_NS = "http://www.jboss.org/drools"


def fixture_ir() -> ProcessIR:
    return ProcessIR.model_validate_json(
        (FIXTURES / "home-bp-monitoring.ir.json").read_text(encoding="utf-8")
    )


def fixture_xml() -> str:
    return (FIXTURES / "home-bp-monitoring.bpmn").read_text(encoding="utf-8")


def section_text() -> str:
    return (FIXTURES / "syn-htn-v2-section-3.5-3.6.md").read_text(encoding="utf-8")


def test_golden_template_passes_every_static_rung_and_defers_only_max_duration() -> None:
    ir = fixture_ir()
    result = run_static_ladder(
        ir,
        catalog=load_catalog(),
        stage="template",
        section_text=section_text(),
    )

    assert result.ok is True
    assert result.rungs_passed == ["L0", "L1", "L2", "L3", "L4", "L5a", "L5b"]
    assert result.deferred == ir.process.acp.invariants
    assert result.errors() == []


def test_xml_input_passes_after_recovering_its_ir() -> None:
    result = run_static_ladder(
        fixture_xml(),
        catalog=load_catalog(),
        stage="template",
        section_text=section_text(),
    )

    assert result.ok is True
    assert result.rungs_passed == ["L0", "L1", "L2", "L3", "L4", "L5a", "L5b"]


def test_template_receipt_can_skip_text_grounding_when_source_text_is_unavailable() -> None:
    result = run_static_ladder(
        fixture_ir(),
        catalog=load_catalog(),
        stage="template",
    )

    assert result.ok is True
    assert result.rungs_passed == ["L0", "L1", "L2", "L3", "L4", "L5a"]


def test_known_patient_value_in_literal_assignment_fails_at_l3() -> None:
    sentinel = "patient-specific-sentinel-7391"
    root = etree.fromstring(compile(fixture_ir(), catalog=load_catalog()).encode("utf-8"))
    assignment_from = root.xpath(
        ".//bpmn:dataInputAssociation[bpmn:targetRef='notify_urgency_input']"
        "/bpmn:assignment/bpmn:from",
        namespaces={"bpmn": BPMN_NS},
    )[0]
    assert assignment_from is not None
    assignment_from.text = sentinel

    # Keep the recovered IR digest consistent so validation reaches L3.
    updated_ir = fixture_ir()
    notify = next(
        element for element in updated_ir.process.flowElements if element.id == "notify"
    )
    notify.inputs["urgency"] = notify.inputs["urgency"].model_copy(
        update={"literal": sentinel}
    )
    version = ir_version(updated_ir)
    process = root.find(f"{{{BPMN_NS}}}process")
    assert process is not None
    process.set(f"{{{DROOLS_NS}}}version", version)
    template = process.find(f".//{{{ACP_NS}}}template")
    assert template is not None and template.text is not None
    payload = json.loads(template.text)
    payload["version"] = version
    template.text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    xml = etree.tostring(root, encoding="unicode")

    result = run_static_ladder(
        xml,
        catalog=load_catalog(),
        stage="template",
        phi_known_values=[sentinel],
    )

    assert result.ok is False
    assert result.rungs_passed == ["L0", "L1", "L2"]
    assert any(
        finding.rung == "L3"
        and finding.code == "known-value"
        and finding.severity == "ERROR"
        for finding in result.findings
    )


def test_ladder_stops_at_the_first_failing_rung() -> None:
    result = run_static_ladder(
        "<not-xml",
        catalog=load_catalog(),
        stage="template",
    )

    assert result.ok is False
    assert result.rungs_passed == []
    assert {finding.rung for finding in result.errors()} == {"L1"}


def test_ladder_reports_missing_instance_context_at_l5a() -> None:
    ir = fixture_ir()
    ir.process.kind = "instance"
    result = run_static_ladder(
        ir,
        catalog=load_catalog(),
        stage="instance",
        templates=[],
    )

    assert result.ok is False
    assert result.rungs_passed == ["L0", "L1", "L2", "L3", "L4"]
    assert result.errors()[0].code == "instance-context"


def test_public_ladder_entrypoint_is_traced_when_mlflow_is_installed() -> None:
    import pytest

    pytest.importorskip("mlflow")

    assert getattr(run_static_ladder, "__mlflow_traced__", False)
