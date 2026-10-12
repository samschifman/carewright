from types import SimpleNamespace

from cpg_ingester.mlflow_usage import summarize_trace_usage


class Span:
    def __init__(self, span_id, name, *, parent_id=None, attributes=None):
        self.span_id = span_id
        self.name = name
        self.parent_id = parent_id
        self.attributes = attributes or {}

    def get_attribute(self, key):
        return self.attributes.get(key)


def test_summarizes_usage_by_nearest_pipeline_node():
    trace = SimpleNamespace(data=SimpleNamespace(spans=[
        Span("node", "bpmn_ir_creator"),
        Span(
            "chat", "ChatOpenAI", parent_id="node",
            attributes={"mlflow.chat.tokenUsage": {
                "input_tokens": 120, "output_tokens": 35, "total_tokens": 155,
            }},
        ),
        Span(
            "chat-2", "ChatOpenAI", parent_id="node",
            attributes={"mlflow.chat.tokenUsage": {
                "prompt_tokens": 10, "completion_tokens": 4,
            }},
        ),
    ]))

    assert summarize_trace_usage([trace]) == {
        "bpmn_ir_creator": {
            "llm_calls": 2,
            "input_tokens": 130,
            "output_tokens": 39,
            "total_tokens": 169,
        }
    }
