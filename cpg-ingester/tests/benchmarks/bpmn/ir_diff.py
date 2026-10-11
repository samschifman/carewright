"""Structure-level ProcessIR comparison for the BPMN creator benchmark."""

from __future__ import annotations

from difflib import SequenceMatcher

import mlflow


ACTIVITY_TYPES = {"task", "userTask", "businessRuleTask"}
SOURCE_REQUIRED_TYPES = ACTIVITY_TYPES | {"intermediateCatchEvent", "boundaryEvent"}


def _process(value: dict) -> dict:
    """Accept either a ProcessIR payload or an enclosing benchmark record."""
    if "process" in value:
        return value["process"]
    if "ir" in value and isinstance(value["ir"], dict):
        return value["ir"].get("process", value["ir"])
    return value


def _elements(value: dict, element_type: str | None = None) -> list[dict]:
    elements = _process(value).get("flowElements", [])
    return [
        element for element in elements
        if isinstance(element, dict)
        and (element_type is None or element.get("type") == element_type)
    ]


def _acp(element: dict) -> dict:
    value = element.get("acp")
    return value if isinstance(value, dict) else {}


def _is_source_grounded(element: dict) -> bool:
    provenance = _acp(element).get("provenance")
    return isinstance(provenance, dict) and provenance.get("kind") == "source"


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 1.0


def _prf(matched: int, golden_count: int, generated_count: int) -> dict:
    precision = _ratio(matched, generated_count)
    recall = _ratio(matched, golden_count)
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "matched": matched,
        "golden_count": golden_count,
        "generated_count": generated_count,
    }


def _activity_score(golden: dict, generated: dict) -> float:
    """Rank a candidate by catalog capability and human-readable label."""
    golden_capability = golden.get("taskName")
    generated_capability = generated.get("taskName")
    label_score = SequenceMatcher(
        None,
        str(golden.get("name", "")).casefold(),
        str(generated.get("name", "")).casefold(),
    ).ratio()
    if golden_capability and golden_capability == generated_capability:
        return 0.65 + 0.35 * label_score
    return 0.65 * label_score


def _align_activities(golden: dict, generated: dict) -> dict[str, str]:
    """Greedily align activities by taskName and label similarity."""
    expected = _elements(golden)
    actual = _elements(generated)
    expected = [item for item in expected if item.get("type") in ACTIVITY_TYPES]
    actual = [item for item in actual if item.get("type") in ACTIVITY_TYPES]
    candidates = sorted(
        (
            (_activity_score(left, right), left, right)
            for left in expected for right in actual
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    used_expected: set[str] = set()
    used_actual: set[str] = set()
    mapping: dict[str, str] = {}
    for score, left, right in candidates:
        left_id, right_id = left.get("id"), right.get("id")
        if not left_id or not right_id or left_id in used_expected or right_id in used_actual:
            continue
        same_capability = bool(left.get("taskName")) and left.get("taskName") == right.get("taskName")
        if (
            left.get("taskName") and right.get("taskName")
            and left.get("taskName") != right.get("taskName")
        ):
            continue
        if not same_capability and score < 0.52:
            continue
        mapping[left_id] = right_id
        used_expected.add(left_id)
        used_actual.add(right_id)
    return mapping


def _align_other_nodes(golden: dict, generated: dict, mapping: dict[str, str]) -> None:
    """Align gateways and events by exact id/type, then by same-type labels."""
    expected = [
        item for item in _elements(golden)
        if item.get("type") not in ACTIVITY_TYPES | {"sequenceFlow"}
    ]
    actual = [
        item for item in _elements(generated)
        if item.get("type") not in ACTIVITY_TYPES | {"sequenceFlow"}
    ]
    unused = {item.get("id"): item for item in actual if item.get("id")}
    for left in expected:
        left_id = left.get("id")
        if not left_id:
            continue
        exact = unused.get(left_id)
        if exact and exact.get("type") == left.get("type"):
            mapping[left_id] = left_id
            unused.pop(left_id, None)
            continue
        candidates = [
            (
                SequenceMatcher(
                    None,
                    str(left.get("name", "")).casefold(),
                    str(right.get("name", "")).casefold(),
                ).ratio(),
                right,
            )
            for right in unused.values()
            if right.get("type") == left.get("type")
        ]
        if candidates:
            score, best = max(candidates, key=lambda item: item[0])
            if score >= 0.58:
                mapping[left_id] = best["id"]
                unused.pop(best["id"], None)


def _mapped_flows(golden: dict, generated: dict, node_map: dict[str, str]) -> list[tuple[dict, dict]]:
    """Match flows when both endpoints align to the generated model."""
    expected = _elements(golden, "sequenceFlow")
    actual = _elements(generated, "sequenceFlow")
    actual_by_endpoints: dict[tuple[str, str], list[dict]] = {}
    for flow in actual:
        actual_by_endpoints.setdefault(
            (flow.get("sourceRef", ""), flow.get("targetRef", "")), []
        ).append(flow)
    used: set[str] = set()
    matches = []
    for flow in expected:
        source = node_map.get(flow.get("sourceRef", ""))
        target = node_map.get(flow.get("targetRef", ""))
        if source is None or target is None:
            continue
        candidates = actual_by_endpoints.get((source, target), [])
        match = next(
            (item for item in candidates if item.get("id") not in used),
            None,
        )
        if match is not None:
            used.add(match.get("id"))
            matches.append((flow, match))
    return matches


def _parameters(value: dict) -> list[dict]:
    return _process(value).get("acp", {}).get("parameters", [])


def _parameter_metrics(golden: dict, generated: dict) -> dict:
    expected = {item["name"]: item for item in _parameters(golden) if item.get("name")}
    actual = {item["name"]: item for item in _parameters(generated) if item.get("name")}
    common = set(expected) & set(actual)
    defaults = [
        expected[name].get("default") == actual[name].get("default")
        for name in common
        if "default" in expected[name] or "default" in actual[name]
    ]
    units = [
        expected[name].get("unit") == actual[name].get("unit")
        for name in common
        if "unit" in expected[name] or "unit" in actual[name]
    ]
    return {
        **_prf(len(common), len(expected), len(actual)),
        "default_accuracy": _ratio(sum(defaults), len(defaults)),
        "default_comparisons": len(defaults),
        "unit_accuracy": _ratio(sum(units), len(units)),
        "unit_comparisons": len(units),
        "missing": sorted(set(expected) - set(actual)),
        "extra": sorted(set(actual) - set(expected)),
    }


def _grounding_coverage(golden: dict, generated: dict, node_map: dict[str, str],
                        flow_matches: list[tuple[dict, dict]]) -> dict:
    """Measure source provenance retained for golden source-derived elements."""
    required = [
        item for item in _elements(golden)
        if item.get("type") in SOURCE_REQUIRED_TYPES
        or (
            item.get("type") == "sequenceFlow"
            and item.get("conditionExpression") is not None
        )
    ]
    flow_map = {left.get("id"): right for left, right in flow_matches}
    generated_by_id = {
        item.get("id"): item for item in _elements(generated) if item.get("id")
    }
    grounded = 0
    for element in required:
        if element.get("type") == "sequenceFlow":
            candidate = flow_map.get(element.get("id"))
        else:
            candidate = generated_by_id.get(node_map.get(element.get("id", "")))
        if candidate is not None and _is_source_grounded(candidate):
            grounded += 1
    return {
        "coverage": _ratio(grounded, len(required)),
        "grounded": grounded,
        "required": len(required),
    }


@mlflow.trace(name="bpmn_ir_golden_diff")
def diff_process_ir(golden: dict, generated: dict) -> dict:
    """Compare activity/flow structure, gateway types, parameters, and grounding."""
    node_map = _align_activities(golden, generated)
    _align_other_nodes(golden, generated, node_map)
    expected_activities = [
        item for item in _elements(golden) if item.get("type") in ACTIVITY_TYPES
    ]
    generated_activities = [
        item for item in _elements(generated) if item.get("type") in ACTIVITY_TYPES
    ]
    activity = _prf(
        sum(item.get("id") in node_map for item in expected_activities),
        len(expected_activities),
        len(generated_activities),
    )

    expected_flows = _elements(golden, "sequenceFlow")
    generated_flows = _elements(generated, "sequenceFlow")
    flow_matches = _mapped_flows(golden, generated, node_map)
    gateways = [
        item for item in _elements(golden, "exclusiveGateway")
        if item.get("id") in node_map
    ]
    generated_by_id = {
        item.get("id"): item for item in _elements(generated) if item.get("id")
    }
    gateway_matches = [
        (left, generated_by_id.get(node_map.get(left.get("id", ""))))
        for left in gateways
    ]
    gateway_matches = [(left, right) for left, right in gateway_matches if right is not None]
    gateway_accuracy = _ratio(
        sum(left.get("type") == right.get("type")
            for left, right in gateway_matches),
        len(gateway_matches),
    )
    return {
        "activities": activity,
        "flows": _prf(len(flow_matches), len(expected_flows), len(generated_flows)),
        "gateway_type_accuracy": {
            "accuracy": gateway_accuracy,
            "matched": len(gateway_matches),
            "golden_count": len(_elements(golden, "exclusiveGateway")),
        },
        "parameters": _parameter_metrics(golden, generated),
        "grounding": _grounding_coverage(golden, generated, node_map, flow_matches),
        "activity_alignment": node_map,
        "flow_alignment": {
            left.get("id", ""): right.get("id", "") for left, right in flow_matches
        },
    }
