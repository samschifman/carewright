"""Credential comparison emits digests only, including command failures."""
import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('actual,status', [('match',0),('mismatch',1),('failed',2)])
def test_digest_probe_never_prints_secret(monkeypatch,capsys,actual,status):
    import base64
    import hashlib
    import sys
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    root=Path(__file__).resolve().parents[2]
    spec=importlib.util.spec_from_file_location('credential_probe',root/'tests/security/credential-digest-probe.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    secret=b'synthetic-private-probe-value'
    document=json.dumps({'data':{'EXACT_TOKEN':base64.b64encode(secret).decode()}}).encode()
    digest=hashlib.sha256(secret if actual=='match' else b'placeholder').hexdigest().encode()
    results=[Mock(stdout=document),Mock(stdout=digest)]
    if actual=='failed':results[1]=subprocess.CalledProcessError(1,['exec'],stderr=secret)
    run=Mock(side_effect=results)
    monkeypatch.setattr(module.subprocess,'run',run)
    monkeypatch.setattr(sys,'argv',['probe','--namespace','test','--pod','sb-test','--secret','tokens','--key','EXACT_TOKEN','--env','EXACT_TOKEN'])
    assert module.main()==status
    output=capsys.readouterr()
    assert secret.decode() not in output.out+output.err
    assert base64.b64encode(secret).decode() not in output.out+output.err
    command=run.call_args.args[0]
    assert command[-1]=='EXACT_TOKEN'
    assert command[command.index('--') + 1] == 'python3'
    assert 'printenv' not in command
