"""Cheap BPMN XML checks that run before XSD validation."""

from __future__ import annotations

import re

from lxml import etree

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.validators.results import Finding

BPMN_MODEL_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_FORBIDDEN_XML_CONTROLS = re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]")


def _parse(xml: str) -> etree._Element:
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    return etree.fromstring(xml.encode("utf-8"), parser=parser)


@trace(name="bpmn.preflight")
def validate_preflight(xml: str) -> list[Finding]:
    """Check XML well-formedness, forbidden controls, and the BPMN definitions root."""
    controls = sorted({ord(char) for char in _FORBIDDEN_XML_CONTROLS.findall(xml)})
    if controls:
        rendered = ", ".join(f"U+{code:04X}" for code in controls)
        return [
            Finding(
                rung="L1",
                severity="ERROR",
                code="control-character",
                message=f"XML contains forbidden control character(s): {rendered}",
            )
        ]

    try:
        root = _parse(xml)
    except (etree.XMLSyntaxError, ValueError) as exc:
        return [
            Finding(
                rung="L1",
                severity="ERROR",
                code="xml-well-formed",
                message=f"XML parse error: {exc}",
            )
        ]

    expected_root = f"{{{BPMN_MODEL_NS}}}definitions"
    if root.tag != expected_root:
        actual = etree.QName(root)
        namespace = actual.namespace or "(none)"
        return [
            Finding(
                rung="L1",
                severity="ERROR",
                code="bpmn-root",
                message=(
                    "Root element must be BPMN definitions in "
                    f"'{BPMN_MODEL_NS}', got '{{{namespace}}}{actual.localname}'"
                ),
            )
        ]
    return []


def strip_schema_location(xml: str) -> str:
    """Remove xsi:schemaLocation hints so validation uses only vendored schemas."""
    root = _parse(xml)
    for element in root.iter():
        element.attrib.pop(f"{{{XSI_NS}}}schemaLocation", None)
        element.attrib.pop(f"{{{XSI_NS}}}noNamespaceSchemaLocation", None)
    return etree.tostring(root, encoding="unicode")
