from pathlib import Path

from lxml import etree

from cpg_contracts.automation.validators.preflight import validate_preflight
from cpg_contracts.automation.validators.xsd import load_schema, validate_xsd

FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_schema_loads_and_valid_fixture_passes_l1_l2():
    assert isinstance(load_schema(), etree.XMLSchema)
    xml = read_fixture("minimal-valid.bpmn")
    assert validate_preflight(xml) == []
    assert validate_xsd(xml) == []


def test_unknown_unqualified_attribute_fails_l2():
    findings = validate_xsd(read_fixture("minimal-invalid-xsd.bpmn"))

    assert findings
    assert all(finding.rung == "L2" for finding in findings)
    assert all(finding.severity == "ERROR" for finding in findings)
    assert any("unknownAttribute" in finding.message for finding in findings)


def test_xsd_does_not_catch_dangling_refs():
    xml = read_fixture("minimal-valid.bpmn")
    assert 'targetRef="end_period"' in xml
    dangling = xml.replace('targetRef="end_period"', 'targetRef="missing_target"', 1)

    assert validate_xsd(dangling) == []
