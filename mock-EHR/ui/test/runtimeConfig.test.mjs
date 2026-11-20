import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

async function load(name) {
  const compiled = ts.transpileModule(readFileSync(new URL(`../src/${name}.ts`, import.meta.url), 'utf8'),
    { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
  return import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'));
}
const { resolveConfig } = await load('runtimeConfig');
const { oauthRedirect } = await load('oauthRedirect');

const CALLBACK = 'https://writer.test/api/v1/launch/callback';
const BUILD = { medplumBaseUrl: 'http://build.test/', smartRedirectOrigins: 'https://built.test' };

test('deploy-time allowlist replaces the one compiled into the image', () => {
  const config = resolveConfig({ medplumBaseUrl: 'https://ehr.test/', smartRedirectOrigins: 'https://writer.test' }, BUILD);
  assert.deepEqual(config, { medplumBaseUrl: 'https://ehr.test/', smartRedirectOrigins: 'https://writer.test' });
  assert.equal(oauthRedirect(CALLBACK, config.smartRedirectOrigins).toString(), CALLBACK);
  assert.equal(oauthRedirect('https://built.test/api/v1/launch/callback', config.smartRedirectOrigins), undefined);
});

test('a published image built without the argument works once the deployment supplies origins', () => {
  const config = resolveConfig({ medplumBaseUrl: 'https://ehr.test/', smartRedirectOrigins: 'https://a.test,https://writer.test' }, {});
  assert.equal(oauthRedirect(CALLBACK, config.smartRedirectOrigins).toString(), CALLBACK);
});

test('an empty or malformed deploy-time value refuses every redirect, never falling back', () => {
  for (const value of ['', null, 42, ['https://writer.test'], { origin: 'https://writer.test' }]) {
    const config = resolveConfig({ smartRedirectOrigins: value }, { smartRedirectOrigins: 'https://writer.test' });
    assert.equal(config.smartRedirectOrigins, '');
    assert.equal(oauthRedirect(CALLBACK, config.smartRedirectOrigins), undefined);
  }
});

test('without the key (compose, dev server) the build-time value applies', () => {
  for (const runtime of [undefined, null, 'not json', [], {}, { medplumBaseUrl: 'https://ehr.test/' }]) {
    assert.equal(resolveConfig(runtime, BUILD).smartRedirectOrigins, 'https://built.test');
  }
  assert.equal(resolveConfig(undefined, {}).smartRedirectOrigins, '');
  assert.equal(oauthRedirect(CALLBACK, resolveConfig(undefined, {}).smartRedirectOrigins), undefined);
});

test('medplum base URL keeps its existing precedence', () => {
  assert.equal(resolveConfig({ medplumBaseUrl: '' }, BUILD).medplumBaseUrl, 'http://build.test/');
  assert.equal(resolveConfig(undefined, {}).medplumBaseUrl, 'http://localhost:8103/');
});
