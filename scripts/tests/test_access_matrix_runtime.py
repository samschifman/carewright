"""Mocked non-dry-run probe: a CLI trying to consume stdin must not eat targets."""
import json
import os
import subprocess
import tempfile
from pathlib import Path


def test_all_targets_are_probed_even_when_cli_reads_stdin():
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        cli = tmp/'kubectl'
        cli.write_text('''#!/usr/bin/env python3
import json,os,sys
args=sys.argv[1:]
if 'run' in args:
    sys.stdin.read()  # Mimic kubectl run -i consuming any inherited target TSV.
    with open(os.environ['PROBE_LOG'],'a') as out: out.write(args[-2]+'\\n')
    print('__CW_RESULT__7:000')
elif 'delete' in args: pass
elif 'exec' in args: print('/api/v1/launch/synthetic')
elif 'cpgingester' in args: print('https://ingester.test')
elif 'services,pods,sandboxrequests' in args:
    print(json.dumps({'items':[
      {'kind':'Service','metadata':{'name':'gateway'},'spec':{'ports':[{'port':8080}]}},
      {'kind':'Pod','metadata':{'name':'gateway-pod'},'status':{'podIP':'10.0.0.2'},'spec':{'containers':[{'ports':[{'containerPort':8080}]}]}},
      {'kind':'SandboxRequest','metadata':{'name':'writer'},'status':{'services':[{'url':'http://proxy/writer'}]}}
    ]}))
else:
    print(json.dumps({'items':[{'kind':'Ingress','spec':{'rules':[{'host':host} for host in ['writer.test','ingester.test','ehr.test']]}}]}))
''')
        cli.chmod(0o755)
        curl = tmp/'curl'
        curl.write_text('''#!/usr/bin/env python3
import sys
url=sys.argv[-1]
print('404' if url.endswith('/smart-config.json') else '302' if '/launch/' in url else '401' if '/api/' in url and not url.endswith('/session') else '200',end='')
''')
        curl.chmod(0o755)
        env = {**os.environ, 'PATH':str(tmp)+os.pathsep+os.environ['PATH'],
               'KUBECTL':str(cli),'PROBE_LOG':str(tmp/'probes'),
               'NAMESPACE':'carewright','PROBE_NAMESPACE':'probe','ENVIRONMENT':'kind',
               'UI_ORIGIN':'https://writer.test','INGESTER_UI_ORIGIN':'https://ingester.test',
               'INGESTER_CR_NAME':'ingester','EHR_ORIGINS':'https://ehr.test',
               'PROBE_IMAGE':'quay.io/carewright/curl:test','REQUIRED_TARGET_PATTERNS':'gateway,writer'}
        result = subprocess.run(['bash',str(root/'tests/security/access-matrix.sh')],
                                env=env,capture_output=True,text=True,timeout=20)
        # Missing A3 endpoint is deliberately pending, while all six network probes finish.
        assert result.returncode == 2, (result.stdout,result.stderr)
        probes = (tmp/'probes').read_text().splitlines()
        assert len(probes) == 6
        assert len(set(probes)) == 3
        assert 'refused=3' in result.stdout
        assert '/api/v1/notifications/artifacts-available' in result.stdout
