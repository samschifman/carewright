"""Aggregate model token usage from MLflow chat spans by traced pipeline node."""

from collections import defaultdict


_TOKEN_USAGE_ATTRIBUTE = "mlflow.chat.tokenUsage"
_NODE_NAMES = {
    "metadata_extractor",
    "item_identifier",
    "classification_reviewer",
    "dmn_creator",
    "dmn_semantic_reviewer",
    "rec_extractor",
    "rec_semantic_reviewer",
    "bpmn_ir_creator",
    "bpmn_semantic_reviewer",
}


def summarize_trace_usage(traces) -> dict[str, dict[str, int]]:
    """Sum MLflow input/output/total tokens by nearest traced cpg-ingester node."""
    totals: dict[str, dict[str, int]] = defaultdict(lambda: {
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    })
    for trace in traces:
        spans = list(getattr(getattr(trace, "data", None), "spans", []) or [])
        by_id = {span.span_id: span for span in spans if getattr(span, "span_id", None)}
        for span in spans:
            usage = span.get_attribute(_TOKEN_USAGE_ATTRIBUTE)
            if not isinstance(usage, dict):
                continue
            input_tokens = _as_int(usage.get("input_tokens", usage.get("prompt_tokens")))
            output_tokens = _as_int(usage.get("output_tokens", usage.get("completion_tokens")))
            total_tokens = _as_int(usage.get("total_tokens")) or input_tokens + output_tokens
            node = _nearest_node(span, by_id)
            totals[node]["llm_calls"] += 1
            totals[node]["input_tokens"] += input_tokens
            totals[node]["output_tokens"] += output_tokens
            totals[node]["total_tokens"] += total_tokens
    return {node: values for node, values in sorted(totals.items())}


def _as_int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _nearest_node(span, by_id) -> str:
    parent_id = getattr(span, "parent_id", None)
    while parent_id:
        parent = by_id.get(parent_id)
        if parent is None:
            break
        name = getattr(parent, "name", "")
        if name in _NODE_NAMES:
            return name
        parent_id = getattr(parent, "parent_id", None)
    return "unattributed"
