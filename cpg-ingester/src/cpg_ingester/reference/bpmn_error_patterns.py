"""Known Process IR and BPMN findings with concise repair guidance."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BpmnErrorPattern:
    match: str
    fix: str


ERROR_PATTERNS = (
    BpmnErrorPattern(
        "implicit join",
        "Add an explicit converging exclusiveGateway and route each incoming flow through it.",
    ),
    BpmnErrorPattern(
        "default flow",
        "Set exactly one outgoing flow as the diverging gateway's default and leave it unconditioned.",
    ),
    BpmnErrorPattern(
        "compiler-reserved",
        "Rename the IR identifier; use lower-case snake_case outside the compiler-reserved names.",
    ),
    BpmnErrorPattern(
        "plan-bound",
        "Keep exactly one main boundary event using {\"param\":\"max_duration\"} and structural plan-bound provenance.",
    ),
    BpmnErrorPattern(
        "catalog",
        "Use the exact capability id and input/output names from the supplied catalog.",
    ),
    BpmnErrorPattern(
        "source text",
        "Quote a concise, exact source passage that supports this element or parameter value.",
    ),
    BpmnErrorPattern(
        "authored text parameter",
        "Give each authored free-text parameter a non-empty string default containing its authored text; only unstated timings, thresholds, and counts omit defaults.",
    ),
)


def format_bpmn_error_pattern_hints(findings: list[dict | str]) -> str:
    """Return targeted fixes for findings whose messages match known patterns."""
    messages = [
        item if isinstance(item, str) else str(item.get("message", ""))
        for item in findings
    ]
    matched = []
    for pattern in ERROR_PATTERNS:
        if any(pattern.match.lower() in message.lower() for message in messages):
            matched.append(f"- Cause: {pattern.match}. Fix: {pattern.fix}")
    return "\n".join(matched)
