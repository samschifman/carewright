from pathlib import Path

from cpg_contracts.automation.validators.preflight import (
    BPMN_MODEL_NS,
    validate_preflight,
    strip_schema_location,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_valid_fixture_passes_l1():
    assert validate_preflight(read_fixture("minimal-valid.bpmn")) == []


def test_control_character_fails_l1():
    findings = validate_preflight(read_fixture("control-char.bpmn"))

    assert len(findings) == 1
    assert findings[0].rung == "L1"
    assert findings[0].severity == "ERROR"
    assert findings[0].code == "control-character"
    assert "U+0001" in findings[0].message


def test_malformed_xml_fails_l1():
    findings = validate_preflight("<bpmn2:definitions>")

    assert len(findings) == 1
    assert findings[0].code == "xml-well-formed"


def test_wrong_root_namespace_fails_l1():
    findings = validate_preflight("<definitions xmlns='urn:wrong'/>")

    assert len(findings) == 1
    assert findings[0].code == "bpmn-root"
    assert BPMN_MODEL_NS in findings[0].message


def test_strip_schema_location_removes_remote_hints():
    xml = read_fixture("minimal-valid.bpmn")
    xml = xml.replace(
        "<bpmn2:definitions",
        '<bpmn2:definitions xsi:schemaLocation="urn:test https://example.invalid/schema.xsd"',
        1,
    )

    stripped = strip_schema_location(xml)

    assert "schemaLocation" not in stripped
    assert "home-bp-monitoring" in stripped
