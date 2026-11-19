"""V23 wiring and real-node routing tests with scripted LLM replies.

These fixtures exercise graph mechanics, not model detection quality or a clinical
benchmark. No reviewer finding is treated as independently generated evidence.
"""
import hashlib
import importlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cpg_contracts import Recommendation
from cpg_ingester.generation import _build_rec_subgraph
from cpg_ingester.prompts.rec_extractor import REC_EXTRACTOR_SYSTEM, REC_EXTRACTOR_USER

EXTRACTOR = importlib.import_module('cpg_ingester.nodes.rec_extractor')
REVIEWER = importlib.import_module('cpg_ingester.nodes.rec_semantic_reviewer')
PROMPT_DIGEST = '88e87d41d8f9d6b727bfd6fac458573f76252a9da6b828787db6d790ffeb0faa'
REC = {
    'id': 'assigned-item', 'source_cpg': 'TBD', 'section': 'assigned-section',
    'title': 'Source intervention', 'content': 'Offer the source intervention.',
    'recommendation_type': 'treatment',
    'certainty': {'strength': 'consensus', 'evidence_quality': 'ungraded',
                  'grading_system': None, 'original_grade': None},
    'scope_notes': 'Group A and Group B.', 'remarks': None,
    'cross_references': [], 'source_location': None,
}
SOURCE = 'The guideline covers Group A and Group B. Offer the source intervention.'
CRITICAL = 'CRITICAL: Explicit source-defined population is missing.'


def test_runtime_prompt_is_the_exact_measured_v23():
    components = {'system': hashlib.sha256(REC_EXTRACTOR_SYSTEM.encode()).hexdigest(),
                  'user_template': hashlib.sha256(REC_EXTRACTOR_USER.encode()).hexdigest()}
    canonical = json.dumps(components, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    assert hashlib.sha256(canonical.encode()).hexdigest() == PROMPT_DIGEST
    assert EXTRACTOR.REC_EXTRACTOR_SYSTEM == REC_EXTRACTOR_SYSTEM
    assert EXTRACTOR.REC_EXTRACTOR_USER == REC_EXTRACTOR_USER


@pytest.mark.parametrize('scenario,extractor_calls,reviewer_calls,reason', [
    ('accept', 1, 1, None),
    ('minor', 1, 1, None),
    ('semantic-repair', 2, 2, None),
    ('schema-repair', 2, 1, None),
    ('semantic-budget', 3, 3, 'semantic-budget-exhausted'),
    ('invalid-review', 1, 2, 'reviewer-unparseable'),
    ('empty-source', 1, 0, 'no-source-text'),
])
def test_real_nodes_use_v23_through_accept_repair_and_escalation(
        scenario, extractor_calls, reviewer_calls, reason, monkeypatch, tmp_path):
    first = deepcopy(REC)
    if scenario in ('semantic-repair', 'semantic-budget'):
        first['scope_notes'] = None
    if scenario == 'schema-repair':
        first['source_location'] = {'page_start': None}
    extraction_responses = [first] + [deepcopy(REC) for _ in range(extractor_calls - 1)]
    if scenario == 'semantic-budget':
        extraction_responses = [deepcopy(first) for _ in range(extractor_calls)]
    extractor = MagicMock()
    extractor.invoke.side_effect = [SimpleNamespace(content=json.dumps({'recommendations': [rec]}))
                                    for rec in extraction_responses]
    reviews = []
    for index in range(reviewer_calls):
        if scenario == 'invalid-review':
            reviews.append(SimpleNamespace(content='not JSON'))
            continue
        critical = scenario == 'semantic-budget' or (scenario == 'semantic-repair' and index == 0)
        minor = scenario == 'minor'
        issues = [CRITICAL] if critical else ['MINOR: Certainty metadata needs review.'] if minor else []
        reviews.append(SimpleNamespace(content=json.dumps({
            'checks': [{'recommendation_title': REC['title'], 'content_faithful': not critical,
                        'certainty_accurate': not minor, 'type_correct': True, 'issues': issues}],
            'missing_recommendations': [], 'discrepancies_found': critical,
            'discrepancies': [CRITICAL] if critical else [], 'summary': '',
        })))
    reviewer = MagicMock()
    reviewer.invoke.side_effect = reviews
    monkeypatch.setattr(EXTRACTOR, 'get_llm', lambda state: extractor)
    monkeypatch.setattr(REVIEWER, 'get_llm', lambda state: reviewer)
    result = _build_rec_subgraph().compile().invoke({
        'items': [{'id': REC['id'], 'section': REC['section']}],
        'source_pages': '   ' if scenario == 'empty-source' else SOURCE,
        'output_dir': str(tmp_path), 'review_count': 0,
    })
    assert extractor.invoke.call_count == extractor_calls
    assert reviewer.invoke.call_count == reviewer_calls
    assert result['escalated'] is (reason is not None)
    assert result.get('escalation_reason') == reason
    for call in extractor.invoke.call_args_list:
        assert call.args[0][0]['content'] == REC_EXTRACTOR_SYSTEM
    if reason is None:
        Recommendation.model_validate(result['recommendations'][0])
        assert result['recommendations'][0]['scope_notes'] == REC['scope_notes']
    if scenario in ('semantic-repair', 'schema-repair'):
        feedback = extractor.invoke.call_args_list[1].args[0][1]['content']
        assert ('SEMANTIC ISSUES' if scenario == 'semantic-repair' else 'SCHEMA ERRORS') in feedback
        if scenario == 'semantic-repair':
            assert CRITICAL in feedback
    if reason in ('reviewer-unparseable', 'no-source-text'):
        assert result['force_escalate'] is True
        assert result['review_count'] == 0
