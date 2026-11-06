"""FHIR Syntax Validator — structural validation of the FHIR Bundle.

Deterministic checks: required fields, reference resolution,
AI Transparency IG compliance, coded field completeness.
"""

import logging

from acp_writer.output import write_artifact
from acp_writer.state import CarePlanComposerState
from acp_writer.validators.fhir_syntax import validate_fhir_bundle
from cpg_contracts.tracing import trace_safe

logger = logging.getLogger(__name__)


@trace_safe(name="fhir_syntax_validator")
def fhir_syntax_validator(state: CarePlanComposerState) -> dict:
    """Run structural validation on the FHIR Bundle."""
    logger.info("── FHIR Syntax Validator ──")
    bundle = state.get("fhir_bundle", {})
    output_dir = state.get("output_dir", "")

    if not bundle.get("entry"):
        logger.info("No entries in FHIR bundle — skipping syntax validation")
        return {"syntax_errors": []}

    errors = validate_fhir_bundle(bundle)

    if errors:
        logger.warning("FHIR syntax validation: %d errors", len(errors))
        for err in errors:
            logger.warning('Patient data validation issue (details redacted)')
    else:
        logger.info("FHIR syntax validation passed")

    if output_dir and errors:
        write_artifact(output_dir, "fhir-syntax-errors.json", errors)

    return {"syntax_errors": errors}
