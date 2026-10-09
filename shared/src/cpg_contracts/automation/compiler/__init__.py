"""Deterministic BPMN compiler for the supported Kogito profile."""

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.catalog import Catalog, load_catalog
from cpg_contracts.automation.compiler.kogito_profile import PROFILE_VERSION
from cpg_contracts.automation.compiler.macros import expand
from cpg_contracts.automation.compiler.parse import ParseError, parse
from cpg_contracts.automation.compiler.serialize import serialize
from cpg_contracts.automation.ir import ProcessIR


@trace(name="bpmn.compile")
def compile(
    ir: ProcessIR,
    *,
    catalog: Catalog | None = None,
    dmn_namespaces: dict[str, str] | None = None,
) -> str:
    """Compile a validated process IR to deterministic Kogito-profile BPMN XML."""
    expanded = expand(
        ir,
        load_catalog() if catalog is None else catalog,
        {} if dmn_namespaces is None else dmn_namespaces,
    )
    return serialize(expanded)


__all__ = ["PROFILE_VERSION", "ParseError", "compile", "parse"]
