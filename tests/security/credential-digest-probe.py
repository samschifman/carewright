#!/usr/bin/env python3
"""Compare one exact sandbox env value with a Secret; never emit credentials.

Run from an authorized operator workstation before B9 workflow verification.
Secret bytes travel only through captured pipes; no shell interpolation or files.
"""
import argparse
import base64
import hashlib
import hmac
import json
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', default='kubectl')
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--pod', required=True)
    parser.add_argument('--container')
    parser.add_argument('--secret', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--env', required=True)
    args = parser.parse_args()
    if not re.fullmatch('[A-Z][A-Z0-9_]*', args.env):
        parser.error('--env must be one exact environment variable name')
    common = [args.cli, '-n', args.namespace]
    try:
        secret = subprocess.run(common + ['get', 'secret', args.secret, '-o', 'json'],
                                check=True, capture_output=True).stdout
        value = base64.b64decode(json.loads(secret)['data'][args.key], validate=True)
        if not value:
            raise ValueError('empty credential')
        expected = hashlib.sha256(value).hexdigest()
        command = common + ['exec', args.pod]
        if args.container:
            command += ['-c', args.container]
        # Python prints only the digest, and fails when the named variable is absent.
        command += ['--', 'python3', '-c',
                    'import hashlib,os,sys; print(hashlib.sha256(os.environ[sys.argv[1]].encode()).hexdigest())',
                    args.env]
        actual = subprocess.run(command, check=True, capture_output=True).stdout.decode().strip()
        if not re.fullmatch('[0-9a-f]{64}', actual):
            raise ValueError('invalid digest output')
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError):
        print('ERROR: credential comparison unavailable (no credential output retained)')
        return 2
    matched = hmac.compare_digest(expected, actual)
    print(('MATCH' if matched else 'MISMATCH') + f': {args.env}; secret_sha256={expected}; sandbox_sha256={actual}')
    return 0 if matched else 1


if __name__ == '__main__':
    raise SystemExit(main())
