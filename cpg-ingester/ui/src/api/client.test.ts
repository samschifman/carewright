import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api, clearSession } from './client';
vi.mock('../config', () => ({ getConfig: () => ({ apiUrl: '' }) }));

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  clearSession();
  fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

it('attaches the server-issued CSRF token to every mutation, including upload', async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, status: 200,
    json: async () => ({ kind: 'operator', csrfToken: 'synthetic-server-csrf' }) });
  await api.getSession();
  await api.uploadCpg(new File(['synthetic'], 'synthetic.pdf'));
  await api.submitReview('run-1', 'manifest', { action: 'approve', reviewRound: 0, artifactRevision: 'a'.repeat(64) });
  await api.cancelRun('run-1');
  await api.rerunPipeline('run-1');
  await api.logout();
  const mutations = fetchMock.mock.calls.slice(1);
  expect(mutations).toHaveLength(5);
  for (const [, options] of mutations) {
    expect(options.headers['X-CSRF-Token']).toBe('synthetic-server-csrf');
    expect(options.credentials).toBe('same-origin');
  }
  expect(fetchMock.mock.calls[0][1].headers).not.toHaveProperty('X-CSRF-Token');
});
