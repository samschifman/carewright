"""Prompt builders for generating BPMN process IR from a CPG process."""

import json
from importlib.resources import files

from cpg_contracts.automation.catalog import Catalog, load_catalog
from cpg_contracts.automation.ir import ProcessIR


_SYSTEM_RULES = """\
You are a clinical informaticist translating a guideline process into the
project's Process IR. The IR is a strict JSON contract. Return exactly one JSON
object that validates against the schema below.

Never write BPMN XML and never write coordinates. Never invent timing,
thresholds, actors, or clinical steps. Preserve the source's clinical meaning
and cite source text on source-derived elements.

## BPMN element subset and compiler macros
The only flow element types are startEvent, endEvent, task, userTask,
businessRuleTask, exclusiveGateway, intermediateCatchEvent, boundaryEvent, and
sequenceFlow. Use one start, at least one end, explicit converging gateways
instead of implicit joins, and one default outgoing flow for each diverging
exclusive gateway. Every flow node has a short, human-readable name; only
sequence flows may omit it. A boundary event attaches to `main` and uses a timer.

Every node with multiple incoming sequence flows must be preceded by an explicit
converging `exclusiveGateway`: redirect each incoming flow to the gateway and
add one unconditional flow from the gateway to the original target. Never send
two flows directly to an end event, task, or gateway. When a parameter represents
a categorical DMN output, set `constraints.enum` to the complete finite set of
that output's values so its trigger conditions are enum-typed.

A template is the care team's automation expressed with catalog capabilities,
such as patient outreach, EHR queries, notifications, and visit requests. The
patient's own actions are not process steps. Use `userTask` only for clinician
or care-team review.

Timer ValueRefs must be either `{"param":"parameter_name"}` or a duration
literal such as `{"literal":"P7D","type":"duration"}`. Never use a
`property` reference for a timer. Duration literals use ISO-8601 day/hour form;
week, month and year forms are rejected, so express one week as `P7D`, one
month as `P30D`, 12 months as `P365D`, and 24 months as `P730D`. A cadence the
source states in words ("annually", "every 12 months") grounds its day form.

The deterministic compiler expands these macros; do not add their generated
elements to the IR:
1. M1 adds catalog-defined task inputs and mapped outputs, plus the Kogito
   TaskName work parameter.
2. M2 binds a businessRuleTask to the named DMN model and maps its inputs and
   outputs.
3. M3 adds the increment, init and reset scripts for a task's acp.counter; the
   counter is an integer property (declared by you or added for you).
4. M4 wraps the flow in the `main` sub-process and adds outcome routing.
5. M5 serializes provenance and template metadata as `acp` extension blocks.
6. M6 turns timer parameter references into Kogito timer expressions.

## Source and structural provenance
Every task, userTask, businessRuleTask, timer intermediateCatchEvent,
boundaryEvent, and conditional sequenceFlow needs source provenance with
`source_text` copied from the supplied CPG and `source_location` when known.
Each `source_text` must be one exact, contiguous quote from the supplied CPG;
do not combine separate sentences or omit text from the middle of a quote.
Use separate element quotes when different sentences support different claims.
The source quote must support the element's meaning. Structural elements use
one of the schema's closed `derivation_rule` values and `supports` ids:
start-event, end-event, sequence, exclusive-split, exclusive-join,
default-flow, or plan-bound. Do not label a clinical action as structural.
Structural provenance and flow-node names are filled when omitted.
No source-derived element may use free-text literals. Free text is a string
parameter with authored provenance. Enum and unit literals must come from the
catalog. Quantity units must match the receiving property. Numeric and
duration literals must be supported by the cited text, except marked 0/1
structural constants in existence checks.

## Identifiers and reserved timeout
Element ids, property names, and parameter names are lower-case snake_case,
match `^[a-z][a-z0-9_]*$`, contain no dots, and are not Java keywords or
compiler-reserved names. Do not use `init`, `main`, `main_start`, `gw_outcome`,
`end_completed`, `f_init`, `f_main`, `f_after`, `f_completed`, `outcome`, any
id beginning `main_`, or ids containing `_counter`, `_reset`, `_outcome`, or
`_route`. Catalog capability ids retain their dots in `taskName`; these are
catalog ids rather than IR identifiers.

Every process is a `template` with its stable id and has the reserved required
duration parameter `max_duration` with no default. It also has exactly one
boundary event attached to `main`, with `timeDuration: {"param":"max_duration"}`
and structural provenance `plan-bound`. The creator deterministically adds
these two records if omitted, with boundary id `period_elapsed` and supports
listing every task id. Do not add a second main boundary.

An `acp.triggers` entry of kind `dmn-output` has exactly these fields:
`kind`, `model_id`, `output_name`, and `output_value`. Do not add a description,
interval, or other fields to this trigger.

## Grounding requirements
T1: source provenance quotes must occur in the supplied CPG section text.
T2: a CPG-sourced parameter default must be stated in its citation; authored
free-text parameters carry their authored text as a non-empty string default.
The no-default rule applies to unstated timings, thresholds, and counts only.
T3: structural provenance uses only the closed rules above and has resolvable
supports. A diverging gateway needs a source-derived condition.
T4: source-derived numeric, duration, quantity, enum, unit, and code values
must meet the literal rules above; code inputs use a `{concept}` reference.
T5: behavior-changing elements use source provenance, except the plan-bound
main boundary.
When the source does not state a timing, threshold, or count, declare a required
parameter with no default (constraints only when the source states a range); never
choose a value.

The catalog and JSON schema follow. Do not use a capability outside the
catalog. Output JSON only.
"""


def _catalog_payload(catalog: Catalog) -> list[dict]:
    """Convert catalog dataclasses into a compact prompt representation."""
    return [
        {
            "id": capability.id,
            "direction": capability.direction,
            "description": capability.description,
            "inputs": [
                {
                    "name": value.name,
                    "type": value.type,
                    "required": value.required,
                    **({"default": value.default} if value.default is not None else {}),
                    **({"values": value.values} if value.values is not None else {}),
                    **({"free_text": True} if value.free_text else {}),
                }
                for value in capability.inputs
            ],
            "outputs": [
                {"name": value.name, "type": value.type}
                for value in capability.outputs
            ],
            "human_in_loop": capability.human_in_loop,
        }
        for capability in catalog.capabilities
    ]


def build_bpmn_ir_creator_system(catalog: Catalog | None = None) -> str:
    """Render the schema, capability catalog, and canonical worked example."""
    active_catalog = catalog or load_catalog()
    schema = ProcessIR.model_json_schema()
    # Package data, not a repository path: the pod images install the package
    # without the repository's data/ directory.
    example_path = files("cpg_ingester").joinpath(
        "resources/automation/worked-example.ir.json"
    )
    example = json.loads(example_path.read_text(encoding="utf-8"))
    return (
        _SYSTEM_RULES
        + "\n## Capability catalog v"
        + active_catalog.version
        + "\n"
        + json.dumps(_catalog_payload(active_catalog), indent=2, ensure_ascii=False)
        + "\n\n## Worked example: follow-up-visit\n"
        + json.dumps(example, indent=2, ensure_ascii=False)
        + "\n\n## ProcessIR JSON schema\n"
        + json.dumps(schema, indent=2, ensure_ascii=False)
    )


def build_bpmn_ir_creator_user(
    *,
    item: dict,
    source_pages: str,
    linked_dmn_summaries: list[dict],
    linked_recs: list[dict],
    mermaid_figures: str,
    abbreviations: str,
    human_feedback: str = "",
    repair_feedback: str = "",
) -> str:
    """Render source context, linked artifacts, and repair or human feedback."""
    parts = [
        "Create a ProcessIR template for this identified care process.",
        "\n## Manifest process item\n"
        + json.dumps(item, indent=2, ensure_ascii=False, default=str),
        "\n## CPG source sections\n" + (source_pages or "(no source text available)"),
        "\n## Linked DMN summaries (including model namespace)\n"
        + json.dumps(linked_dmn_summaries, indent=2, ensure_ascii=False, default=str),
        "\n## Linked recommendations\n"
        + json.dumps(linked_recs, indent=2, ensure_ascii=False, default=str),
        "\n## Figure flowcharts as Mermaid\n" + (mermaid_figures or "(none)"),
        "\n## Abbreviations\n" + (abbreviations or "(none)"),
    ]
    if human_feedback.strip():
        if human_feedback.lstrip().startswith("## Reviewer feedback from round "):
            parts.append("\n" + human_feedback)
        else:
            parts.append("\n## Reviewer feedback from round 1\n" + human_feedback)
    if repair_feedback.strip():
        parts.append("\n## REPAIR MODE — CORRECT THESE FINDINGS\n" + repair_feedback)
    parts.append(
        "\nReturn the complete ProcessIR JSON object. Do not return BPMN XML or prose."
    )
    return "\n".join(parts)
