"""BPMN 2.0 XSD validation against the vendored OMG schema set."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from lxml import etree

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.validators.preflight import _parse
from cpg_contracts.automation.validators.results import Finding


@lru_cache(maxsize=1)
def load_schema() -> etree.XMLSchema:
    """Load the vendored BPMN schema; relative OMG imports resolve beside it."""
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "BPMN20.xsd"
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    schema_doc = etree.parse(str(schema_path), parser=parser)
    return etree.XMLSchema(schema_doc)


@trace(name="bpmn.xsd")
def validate_xsd(xml: str) -> list[Finding]:
    """Return one L2 error finding for each XSD validation diagnostic."""
    document = _parse(xml)
    schema = load_schema()
    if schema.validate(document):
        return []
    return [
        Finding(
            rung="L2",
            severity="ERROR",
            code="xsd",
            message=f"{entry.line}: {entry.message}",
        )
        for entry in schema.error_log
    ]
