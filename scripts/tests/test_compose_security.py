"""Check caller/receiver agreement and key placement in local Compose profiles."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def environment(service):
    value = service.get('environment') or {}
    return dict(item.split('=', 1) for item in value) if isinstance(value, list) else value


def test_local_profile_tokens_and_signing_scope():
    base = yaml.safe_load((ROOT/'compose.yml').read_text())['services']
    overlay = yaml.safe_load((ROOT/'compose.orchestrated.yml').read_text())['services']
    env = {name:environment(service) for name,service in base.items()}
    for name,service in overlay.items():
        env.setdefault(name,{}).update(environment(service))
    workflow = env['sonataflow-devmode']
    assert 'SERVICE_TOKEN_ACP_DECISION_ENGINE' not in env['cpg-ingester-delivery']
    assert 'SERVICE_TOKEN_ACP_LLM_REASONING' not in env['cpg-ingester']
    receivers = {'acp-patient-data':'ACP_PATIENT_DATA','acp-llm-reasoning':'ACP_LLM_REASONING',
        'acp-decision-engine':'ACP_DECISION_ENGINE','acp-fhir-generation':'ACP_FHIR_GENERATION',
        'acp-fhir-server':'ACP_FHIR_SERVER','acp-writer-bff':'ACP_BFF',
        'cpg-ingester-ingestion':'CPG_INGESTION','cpg-ingester-llm':'CPG_LLM_ANALYSIS',
        'cpg-ingester-assembly':'CPG_ASSEMBLY','cpg-ingester-delivery':'CPG_DELIVERY',
        'cpg-ingester-bff':'CPG_BFF'}
    assert len(set(workflow.values())) == len(workflow)
    for service,receiver in receivers.items():
        key='SERVICE_TOKEN_'+receiver
        assert env[service][key] == workflow[key]
    for key in ['APPROVAL_SIGNING_KEY','SESSION_SIGNING_KEY']:
        assert key in env['acp-writer-bff']
        assert key not in env['cpg-ingester-bff']
        assert key not in workflow
    assert env['acp-fhir-server']['APPROVAL_SIGNING_KEY'] == env['acp-writer-bff']['APPROVAL_SIGNING_KEY']
    assert 'SESSION_SIGNING_KEY' not in env['acp-fhir-server']
    assert 'CPG_SIGNING_KEY' in env['cpg-ingester-bff']
    assert len({env['acp-writer-bff']['APPROVAL_SIGNING_KEY'],env['acp-writer-bff']['SESSION_SIGNING_KEY'],env['cpg-ingester-bff']['CPG_SIGNING_KEY']}) == 3
    assert env['acp-writer-bff']['ACP_PUBLIC_ORIGIN'] == 'http://localhost:3001'
    assert env['cpg-ingester-bff']['CPG_PUBLIC_ORIGIN'] == 'http://localhost:3003'
    assert env['acp-writer-bff']['SMART_FHIR_INTERNAL_BASE'] == 'http://medplum-server:8103/fhir/R4/'
    for services in [base, overlay]:
        for service in services.values():
            for port in service.get('ports',[]):
                assert port.startswith('127.0.0.1:')
            for volume in service.get('volumes',[]):
                assert 'smart-config.json' not in volume
