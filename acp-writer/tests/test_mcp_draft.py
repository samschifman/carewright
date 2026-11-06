"""MCP generation must never request clinical approval or publish a bundle."""
import json
from unittest.mock import Mock

from acp_writer import mcp_proxy


def test_mcp_generation_is_draft_only(monkeypatch):
    bundle = {'resourceType': 'Bundle', 'entry': []}
    graph = Mock()
    graph.compile.return_value.invoke.return_value = {'fhir_bundle': bundle}
    monkeypatch.setattr('acp_writer.pipeline.build_pipeline', Mock(return_value=graph))
    publish = Mock(side_effect=AssertionError('MCP must not publish'))
    monkeypatch.setattr(mcp_proxy.requests, 'post', publish)
    assert json.loads(mcp_proxy.generate_careplan({'resourceType': 'Bundle'})) == bundle
    state = graph.compile.return_value.invoke.call_args.args[0]
    assert state['approved'] is False
    publish.assert_not_called()
