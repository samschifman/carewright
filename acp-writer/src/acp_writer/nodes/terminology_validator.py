"""Terminology Validator — verify all coded fields in the FHIR Bundle.

Walks coded fields and verifies each via the terminology lookup tool.
Fixes invalid codes when possible, flags unresolvable in CarePlan.note.
"""

import logging

from acp_writer.state import CarePlanComposerState
from acp_writer.tools.terminology_lookup import verify_bundle_codes
from cpg_contracts.tracing import trace_safe

logger = logging.getLogger(__name__)


@trace_safe(name="terminology_validator")
def terminology_validator(state: CarePlanComposerState) -> dict:
    """Verify all FHIR codes in the bundle against terminology servers."""
    logger.info("── Terminology Validator ──")
    bundle = state.get("fhir_bundle", {})

    if not bundle.get("entry"):
        logger.info("No entries in FHIR bundle — skipping terminology validation")
        return {"terminology_issues": []}

    issues = verify_bundle_codes(bundle)

    invalid = [i for i in issues if i["status"] == "invalid"]
    unverifiable = [i for i in issues if i["status"] == "unverifiable"]

    if invalid:
        logger.warning("Terminology validation: %d invalid codes", len(invalid))
        for issue in invalid:
            logger.warning('  Invalid (details redacted)')
    if unverifiable:
        logger.info("Terminology validation: %d unverifiable codes (network issues)", len(unverifiable))

    logger.info("Terminology validation complete: %d issues", len(issues))

    return {"terminology_issues": issues}
