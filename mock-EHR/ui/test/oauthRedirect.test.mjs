import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const compiled = ts.transpileModule(readFileSync(new URL('../src/oauthRedirect.ts', import.meta.url), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { oauthRedirect } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'));
test('accepts only configured origin and exact callback path', () => {
  const valid = 'https://writer.test/api/v1/launch/callback';
  assert.equal(oauthRedirect(valid, 'https://writer.test').toString(), valid);
  for (const uri of [valid, 'https://attacker.test/api/v1/launch/callback',
    'https://writer.test/other', 'https://writer.test:444/api/v1/launch/callback',
    'https://user@writer.test/api/v1/launch/callback', valid+'?code=planted', valid+'#fragment',
    'javascript:alert(1)', null]) {
    assert.equal(oauthRedirect(uri, ''), undefined);
    if (uri !== valid) assert.equal(oauthRedirect(uri, 'https://writer.test'), undefined);
  }
});
