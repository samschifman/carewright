"""Round-trip tests for the BPMN compiler's self-describing XML profile."""

from __future__ import annotations

import json
from pathlib import Path

from lxml import etree
import pytest

from cpg_contracts.automation.compiler import ParseError, compile, parse
from cpg_contracts.automation.ir import ProcessIR, canonical_json


FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"
IR_FIXTURE = FIXTURES / "home-bp-monitoring.ir.json"
GOLDEN = FIXTURES / "home-bp-monitoring.bpmn"
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS = "http://www.omg.org/spec/DD/20100524/DI"
NS = {
    "bpmn2": BPMN_NS,
    "acp": "https://github.com/samschifman/cpg-to-acp/bpmn",
}


def fixture_ir() -> ProcessIR:
    return ProcessIR.model_validate_json(IR_FIXTURE.read_text(encoding="utf-8"))


def test_golden_round_trips_to_canonical_ir() -> None:
    ir = fixture_ir()

    assert canonical_json(parse(GOLDEN.read_text(encoding="utf-8"))) == canonical_json(ir)


def test_recompiling_parsed_xml_is_byte_identical() -> None:
    xml = compile(fixture_ir())

    assert compile(parse(xml)) == xml


def test_timer_literal_in_days_round_trips() -> None:
    data = json.loads(IR_FIXTURE.read_text(encoding="utf-8"))
    wait = next(item for item in data["process"]["flowElements"] if item["id"] == "wait_interval")
    wait["timerEventDefinition"]["timeDuration"] = {
        "literal": "P7D",
        "type": "duration",
    }
    ir = ProcessIR.model_validate(data)
    xml = compile(ir)
    root = etree.fromstring(xml.encode("utf-8"))

    assert root.xpath(
        ".//bpmn2:intermediateCatchEvent[@id='wait_interval']/bpmn2:timerEventDefinition/bpmn2:timeDuration/text()",
        namespaces=NS,
    ) == ["P7D"]
    assert canonical_json(parse(xml)) == canonical_json(ir)


def test_parse_ignores_bpmndi_diagram_subtrees() -> None:
    ir = fixture_ir()
    root = etree.fromstring(GOLDEN.read_bytes())
    diagram = etree.SubElement(root, f"{{{BPMNDI_NS}}}BPMNDiagram", id="diagram_home")
    plane = etree.SubElement(
        diagram,
        f"{{{BPMNDI_NS}}}BPMNPlane",
        id="plane_home",
        bpmnElement="home-bp-monitoring",
    )
    shape = etree.SubElement(
        plane,
        f"{{{BPMNDI_NS}}}BPMNShape",
        id="shape_start",
        bpmnElement="start",
    )
    etree.SubElement(
        shape,
        f"{{{DC_NS}}}Bounds",
        x="10",
        y="10",
        width="36",
        height="36",
    )
    etree.SubElement(shape, "{urn:vendor:diagram}label").text = "visual-only extension"
    edge = etree.SubElement(
        plane,
        f"{{{BPMNDI_NS}}}BPMNEdge",
        id="edge_start",
        bpmnElement="f_init",
    )
    etree.SubElement(edge, f"{{{DI_NS}}}waypoint", x="46", y="28")

    assert canonical_json(parse(etree.tostring(root))) == canonical_json(ir)


def test_parse_rejects_java_condition_that_differs_from_acp_expression() -> None:
    root = etree.fromstring(compile(fixture_ir()).encode("utf-8"))
    condition = root.xpath(
        ".//bpmn2:sequenceFlow[@id='f_some']/bpmn2:conditionExpression",
        namespaces=NS,
    )[0]
    condition.text = "return true;"

    with pytest.raises(ParseError, match="differs from its acp-expr source"):
        parse(etree.tostring(root))


def test_parse_rejects_a_foreign_element() -> None:
    root = etree.fromstring(compile(fixture_ir()).encode("utf-8"))
    etree.SubElement(
        root.find("bpmn2:process", namespaces=NS),
        "{urn:foreign}task",
        id="foreign_task",
    )

    with pytest.raises(ParseError, match="unexpected XML element"):
        parse(etree.tostring(root))


def test_parse_rejects_dotted_taskname_literal() -> None:
    root = etree.fromstring(compile(fixture_ir()).encode("utf-8"))
    task = root.xpath(".//bpmn2:task[@id='request_readings']", namespaces=NS)[0]
    task_name_input = task.xpath(
        "./bpmn2:ioSpecification/bpmn2:dataInput[@name='TaskName']/@id",
        namespaces=NS,
    )[0]
    literal = task.xpath(
        "./bpmn2:dataInputAssociation[bpmn2:targetRef=$target]/bpmn2:assignment/bpmn2:from",
        namespaces=NS,
        target=task_name_input,
    )[0]
    literal.text = "patient.request_observation"

    with pytest.raises(ParseError, match="TaskName literal"):
        parse(etree.tostring(root))


def test_every_bpmn_flow_element_has_an_acp_element_block() -> None:
    root = etree.fromstring(compile(fixture_ir()).encode("utf-8"))
    flow_elements = root.xpath(
        ".//bpmn2:process//*[self::bpmn2:startEvent or self::bpmn2:endEvent "
        "or self::bpmn2:task or self::bpmn2:userTask or self::bpmn2:businessRuleTask "
        "or self::bpmn2:exclusiveGateway or self::bpmn2:intermediateCatchEvent "
        "or self::bpmn2:boundaryEvent or self::bpmn2:scriptTask or self::bpmn2:subProcess "
        "or self::bpmn2:sequenceFlow]",
        namespaces=NS,
    )

    assert flow_elements
    assert all(element.xpath("./bpmn2:extensionElements/acp:element", namespaces=NS) for element in flow_elements)


def test_compiler_and_validator_entry_points_are_traced_when_mlflow_is_installed() -> None:
    pytest.importorskip("mlflow")
    from cpg_contracts.automation.compiler import compile
    from cpg_contracts.automation.compiler.kogito_profile import PROFILE_VERSION
    from cpg_contracts.automation.compiler.macros import expand
    from cpg_contracts.automation.compiler.serialize import serialize
    from cpg_contracts.automation.validators.preflight import validate_preflight
    from cpg_contracts.automation.validators.xsd import validate_xsd

    traced = [
        compile,
        parse,
        expand,
        serialize,
        validate_preflight,
        validate_xsd,
    ]

    assert PROFILE_VERSION == "kogito-10.2"
    assert all(getattr(function, "__mlflow_traced__", False) for function in traced)
