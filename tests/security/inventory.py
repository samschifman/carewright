"""Emit private service/pod/sandbox targets and validate public exposure inventory."""
import json
import sys
from urllib.parse import urlsplit


def targets(document, namespace):
    found = set()
    items = document['items']
    for item in items:
        kind, meta = item['kind'], item.get('metadata', {})
        labels = meta.get('labels', {})
        if labels.get('apps.carewright.io/component') == 'ui':
            continue
        name = meta['name']
        if kind == 'Service':
            for port in item.get('spec', {}).get('ports', []):
                if port.get('protocol', 'TCP') != 'TCP': continue
                number = port['port']
                scheme = 'https' if number in {443, 6443} else 'http'
                found.add((name + '/dns', f'{scheme}://{name}.{namespace}.svc:{number}/'))
        elif kind == 'Pod':
            address = item.get('status', {}).get('podIP')
            if not address: continue
            host = f'[{address}]' if ':' in address else address
            for container in item.get('spec', {}).get('containers', []):
                for port in container.get('ports', []):
                    if port.get('protocol', 'TCP') != 'TCP': continue
                    number = port['containerPort']
                    scheme = 'https' if number in {443, 6443} else 'http'
                    found.add((name + '/pod', f'{scheme}://{host}:{number}/'))
        elif kind == 'SandboxRequest':
            for service in item.get('status', {}).get('services', []):
                url = service.get('url', '')
                parsed = urlsplit(url)
                if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('Sandbox endpoint missing or invalid: ' + name)
                found.add((name + '/sandbox', url))
    if not any(name.endswith('/dns') for name, _ in found):
        raise ValueError('No private Service DNS targets discovered')
    if not any(name.endswith('/pod') for name, _ in found):
        raise ValueError('No private pod IP targets discovered')
    if not any(name.endswith('/sandbox') for name, _ in found):
        raise ValueError('No published sandbox endpoints discovered')
    for name, url in sorted(found):
        if any(c in name + url for c in '\t\r\n'):
            raise ValueError('Invalid inventory value')
        print(name + '\t' + url)


def exposure(document, allowed):
    observed = set()
    for item in document['items']:
        kind, spec = item['kind'], item.get('spec', {})
        if kind == 'Route':
            host = spec.get('host')
            if host: observed.add(host)
        elif kind == 'Ingress':
            for rule in spec.get('rules', []):
                if rule.get('host'): observed.add(rule['host'])
        elif kind == 'Service' and spec.get('type') in {'NodePort', 'LoadBalancer'}:
            # NodePort/LoadBalancer is independently public even with a matching ingress.
            observed.add('service:' + item['metadata']['name'])
    unexpected = observed - allowed
    if unexpected:
        print('Unexpected public entries: ' + ', '.join(sorted(unexpected)))
        return 1
    if not observed:
        print('No public entries discovered; inventory cannot prove expected UI/EHR exposure')
        return 1
    missing = {host for host in allowed if not host.startswith('service:')} - observed
    if missing:
        print('Expected public entries absent: ' + ', '.join(sorted(missing)))
        return 1
    print('Only declared public entries (' + str(len(observed)) + ')')
    return 0


if __name__ == '__main__':
    with open(sys.argv[2]) as source:
        doc = json.load(source)
    if sys.argv[1] == 'targets': targets(doc, sys.argv[3])
    elif sys.argv[1] == 'exposure': sys.exit(exposure(doc, set(sys.argv[3].split(','))))
    else: raise ValueError('Unknown inventory operation')
