"""Engine-specific names and normalization for the Kogito 10.2 BPMN profile.

The profile was checked against ``kogito-jit-runner:10.2.0`` and a
``jbpm-with-drools-quarkus`` 10.2.0 build on 2026-10-09. The validator accepts
the Appendix A shape, ``#{param}`` timers, ``P7D``, subprocess boundary timers,
Java conditions and ``kcontext`` scripts. It rejects ``P1W`` and implicit
joins, while accepting conditioned gateways without a default (project policy
still requires one). The Quarkus build confirmed that ``drools:taskName`` is
the work-item handler name and that the ``TaskName`` literal drives generated
route and method names; repeated top-level boundary names deduplicate to one
route, so duplicate names are reported as a warning by structural lint.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.expressions import ExpressionError, parse_duration


PROFILE_VERSION = "kogito-10.2"
BPMN_MODEL_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
DROOLS_NS = "http://www.jboss.org/drools"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
ACP_NS = "https://github.com/samschifman/cpg-to-acp/bpmn"
NAMESPACES = {
    "bpmn2": BPMN_MODEL_NS,
    "drools": DROOLS_NS,
    "xsi": XSI_NS,
    "acp": ACP_NS,
}

STRUCTURE_REF = {
    "integer": "Integer",
    "decimal": "Double",
    "quantity": "Double",
    "boolean": "Boolean",
    "string": "String",
    "duration": "String",
    "code": "String",
}
ITEM_DEFINITION_IDS = {
    "String": "_strItem",
    "Integer": "_intItem",
    "Double": "_dblItem",
    "Boolean": "_boolItem",
}
CONDITION_LANGUAGE = "http://www.java.com/java"
SCRIPT_FORMAT = "http://www.java.com/java"
DMN_IMPLEMENTATION = "http://www.jboss.org/drools/dmn"
PACKAGE_NAME = "org.carewright.automation"
GROUP_IDS = {
    "clinicians": "clinicians",
    "care-team": "care-team",
    "patients": "patients",
}

_WEEK_DURATION = re.compile(r"^P(?P<weeks>\d+)W$")


@trace
def task_label(capability_id: str, element_id: str) -> str:
    """Return the unique Java-safe TaskName literal for one custom task."""
    return f"{capability_id.replace('.', '_')}__{element_id}"


@trace
def normalize_duration(iso: str) -> str:
    """Validate a supported ISO duration and express weeks as whole days."""
    parse_duration(iso)
    match = _WEEK_DURATION.fullmatch(iso)
    if match is None:
        if "W" in iso:
            raise ExpressionError("fractional week durations are not supported")
        return iso
    try:
        days = Decimal(match.group("weeks")) * Decimal(7)
    except InvalidOperation as exc:
        raise ExpressionError(f"invalid ISO-8601 duration {iso!r}") from exc
    return f"P{format(days.normalize(), 'f')}D"
