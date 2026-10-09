"""Tests for BPMN L3 structural rules."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from lxml import etree
import pytest

from cpg_contracts.automation.validators.lint import validate_structure


FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
DROOLS_NS = "http://www.jboss.org/drools"
NS = {"b": BPMN_NS}


def golden_root() -> etree._Element:
    return etree.fromstring((FIXTURES / "home-bp-monitoring.bpmn").read_bytes())


def xml_of(root: etree._Element) -> str:
    return etree.tostring(root, encoding="unicode")


def findings_for(mutator) -> list:
    root = golden_root()
    mutator(root)
    return validate_structure(xml_of(root))


def codes(findings: list) -> set[str]:
    return {finding.code for finding in findings}


def test_engine_verified_golden_passes_l3() -> None:
    findings = validate_structure((FIXTURES / "home-bp-monitoring.bpmn").read_text())

    assert findings == []


def test_boundary_attached_to_reachable_host_is_not_disconnected() -> None:
    findings = validate_structure((FIXTURES / "home-bp-monitoring.bpmn").read_text())

    assert not any(
        finding.code == "no-disconnected" and finding.element_id == "period_elapsed"
        for finding in findings
    )


@pytest.mark.parametrize(
    "scope_selector",
    [
        ".//b:process",
        ".//b:subProcess[@id='main']",
    ],
    ids=["process", "subprocess"],
)
def test_single_start_is_required_in_every_scope(scope_selector: str) -> None:
    def remove_start(root: etree._Element) -> None:
        scope = root.xpath(scope_selector, namespaces=NS)[0]
        scope.remove(scope.find("b:startEvent", namespaces=NS))

    assert "single-start" in codes(findings_for(remove_start))


@pytest.mark.parametrize(
    "scope_selector",
    [
        ".//b:process",
        ".//b:subProcess[@id='main']",
    ],
    ids=["process", "subprocess"],
)
def test_every_scope_requires_an_end(scope_selector: str) -> None:
    def remove_ends(root: etree._Element) -> None:
        scope = root.xpath(scope_selector, namespaces=NS)[0]
        for end in scope.findall("b:endEvent", namespaces=NS):
            scope.remove(end)

    assert "end-required" in codes(findings_for(remove_ends))


def test_unreachable_node_is_reported() -> None:
    def add_orphan(root: etree._Element) -> None:
        main = root.xpath(".//b:subProcess[@id='main']", namespaces=NS)[0]
        etree.SubElement(main, f"{{{BPMN_NS}}}task", id="orphan", name="Orphan task")

    findings = findings_for(add_orphan)

    assert any(
        finding.code == "no-disconnected" and finding.element_id == "orphan"
        for finding in findings
    )


def test_implicit_split_is_reported() -> None:
    def add_outgoing(root: etree._Element) -> None:
        process = root.find("b:process", namespaces=NS)
        etree.SubElement(
            process,
            f"{{{BPMN_NS}}}sequenceFlow",
            id="f_implicit_split",
            sourceRef="start",
            targetRef="period_elapsed",
        )

    assert "no-implicit-split" in codes(findings_for(add_outgoing))


def test_implicit_join_is_reported() -> None:
    def add_incoming(root: etree._Element) -> None:
        main = root.xpath(".//b:subProcess[@id='main']", namespaces=NS)[0]
        etree.SubElement(
            main,
            f"{{{BPMN_NS}}}sequenceFlow",
            id="f_implicit_join",
            sourceRef="main_end_escalated",
            targetRef="request_readings",
        )

    assert "no-implicit-join" in codes(findings_for(add_incoming))


def test_duplicate_flows_are_reported() -> None:
    def duplicate_flow(root: etree._Element) -> None:
        original = root.xpath(".//b:sequenceFlow[@id='f_loop']", namespaces=NS)[0]
        duplicate = deepcopy(original)
        duplicate.set("id", "f_loop_copy")
        original.getparent().append(duplicate)

    assert "no-duplicate-flows" in codes(findings_for(duplicate_flow))


def test_non_default_diverging_flow_requires_a_condition() -> None:
    def remove_condition(root: etree._Element) -> None:
        flow = root.xpath(".//b:sequenceFlow[@id='f_some']", namespaces=NS)[0]
        flow.remove(flow.find("b:conditionExpression", namespaces=NS))

    assert "conditional-flows" in codes(findings_for(remove_condition))


def test_condition_is_only_allowed_on_diverging_exclusive_gateway() -> None:
    def add_condition(root: etree._Element) -> None:
        flow = root.xpath(".//b:sequenceFlow[@id='f_loop']", namespaces=NS)[0]
        etree.SubElement(flow, f"{{{BPMN_NS}}}conditionExpression").text = "return true;"

    assert "conditional-flows" in codes(findings_for(add_condition))


def test_diverging_gateway_requires_an_outgoing_default() -> None:
    def remove_default(root: etree._Element) -> None:
        gateway = root.xpath(".//b:exclusiveGateway[@id='gw_received']", namespaces=NS)[0]
        gateway.attrib.pop("default")

    assert "conditional-flows" in codes(findings_for(remove_default))


def test_default_flow_must_not_be_conditional() -> None:
    def condition_default(root: etree._Element) -> None:
        flow = root.xpath(".//b:sequenceFlow[@id='f_none']", namespaces=NS)[0]
        etree.SubElement(flow, f"{{{BPMN_NS}}}conditionExpression").text = "return true;"

    assert "conditional-flows" in codes(findings_for(condition_default))


def test_boundary_must_attach_to_activity_in_its_scope() -> None:
    def attach_to_start(root: etree._Element) -> None:
        boundary = root.xpath(".//b:boundaryEvent[@id='period_elapsed']", namespaces=NS)[0]
        boundary.set("attachedToRef", "start")

    assert "boundary-attached" in codes(findings_for(attach_to_start))


def test_duplicate_ids_are_reported() -> None:
    def duplicate_id(root: etree._Element) -> None:
        input_element = root.xpath(".//b:dataInput[@id='request_readings_code_input']", namespaces=NS)[0]
        input_element.set("id", "start")

    assert "unique-ids" in codes(findings_for(duplicate_id))


def test_duplicate_top_level_boundary_names_are_warning_only() -> None:
    def duplicate_boundary(root: etree._Element) -> None:
        boundary = root.xpath(".//b:boundaryEvent[@id='period_elapsed']", namespaces=NS)[0]
        duplicate = deepcopy(boundary)
        duplicate.set("id", "period_elapsed_copy")
        boundary.getparent().append(duplicate)

    findings = findings_for(duplicate_boundary)
    duplicates = [f for f in findings if f.code == "duplicate-boundary-names"]

    assert len(duplicates) == 1
    assert duplicates[0].severity == "WARNING"


def test_flow_nodes_require_labels_except_generated_main_start() -> None:
    def clear_label(root: etree._Element) -> None:
        task = root.xpath(".//b:task[@id='query_diastolic']", namespaces=NS)[0]
        task.set("name", "   ")

    findings = findings_for(clear_label)

    assert any(
        finding.code == "label-required" and finding.element_id == "query_diastolic"
        for finding in findings
    )


@pytest.mark.parametrize(
    ("timer_value", "expected_code"),
    [
        ("P1W", "timer-format"),
        ("P7D", None),
        ("PT24H", None),
        ("#{reporting_interval}", None),
    ],
)
def test_timer_format_is_enforced(timer_value: str, expected_code: str | None) -> None:
    def set_timer(root: etree._Element) -> None:
        timer = root.xpath(
            ".//b:intermediateCatchEvent[@id='wait_interval']/b:timerEventDefinition/b:timeDuration",
            namespaces=NS,
        )[0]
        timer.text = timer_value

    findings = findings_for(set_timer)

    assert ("timer-format" in codes(findings)) is (expected_code is not None)


@pytest.mark.parametrize(
    ("cycle", "valid"),
    [("R3/PT1H", True), ("R/P7D", True), ("Rfoo/P7D", False), ("R3/P1W", False)],
)
def test_timer_cycle_format_is_enforced(cycle: str, valid: bool) -> None:
    def set_cycle(root: etree._Element) -> None:
        event = root.xpath(".//b:intermediateCatchEvent[@id='wait_interval']", namespaces=NS)[0]
        definition = event.find("b:timerEventDefinition", namespaces=NS)
        duration = definition.find("b:timeDuration", namespaces=NS)
        definition.remove(duration)
        etree.SubElement(definition, f"{{{BPMN_NS}}}timeCycle").text = cycle

    findings = findings_for(set_cycle)

    assert ("timer-format" not in codes(findings)) is valid


@pytest.mark.parametrize("reserved_name", ["Rest", "Service Task"])
def test_kogito_reserved_work_item_names_are_rejected(reserved_name: str) -> None:
    def set_reserved_name(root: etree._Element) -> None:
        task = root.xpath(".//b:task[@id='request_readings']", namespaces=NS)[0]
        task.set(f"{{{DROOLS_NS}}}taskName", reserved_name)

    assert "reserved-task-names" in codes(findings_for(set_reserved_name))


def test_invalid_xml_is_reported_as_an_l3_finding() -> None:
    findings = validate_structure("<bpmn2:definitions")

    assert len(findings) == 1
    assert findings[0].code == "xml-parse"
    assert findings[0].rung == "L3"


def test_structural_rung_is_traced_when_mlflow_is_installed() -> None:
    pytest.importorskip("mlflow")

    assert getattr(validate_structure, "__mlflow_traced__", False)
