import { getConfig } from '../config';
import type {
  ReviewAction,
  RunDetail,
  RunSummary,
} from './types';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

let csrfToken = '';
export function clearSession() { csrfToken = ''; window.dispatchEvent(new Event('cpg-session-expired')); }

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const { apiUrl } = getConfig();
  const url = `${apiUrl}${path}`;
  const response = await fetch(url, {
    ...options,
    credentials: 'same-origin',
    headers: {
      'Accept': 'application/json',
      ...(options?.method && options.method !== 'GET' ? { 'X-CSRF-Token': csrfToken } : {}),
      ...options?.headers,
    },
  });

  if (response.status === 401) clearSession();
  if (!response.ok) {
    const text = await response.text().catch(() => response.statusText);
    throw new ApiError(response.status, text);
  }

  return response.json();
}

export const api = {
  async getSession(): Promise<{ kind: string; csrfToken: string }> {
    const session = await request<{ kind: string; csrfToken: string }>('/api/v1/session');
    csrfToken = session.csrfToken;
    return session;
  },
  async logout(): Promise<void> {
    await request('/api/v1/session/logout', { method: 'POST' });
    clearSession();
  },
  getRuns(): Promise<RunSummary[]> {
    return request('/api/v1/runs');
  },

  getRunDetail(id: string): Promise<RunDetail> {
    return request(`/api/v1/runs/${encodeURIComponent(id)}`);
  },

  async uploadCpg(file: File): Promise<{ runId: string }> {
    const formData = new FormData();
    formData.append('pdf', file);
    return request('/api/v1/upload', {
      method: 'POST',
      body: formData,
    });
  },

  async submitReview(
    runId: string,
    gate: string,
    action: ReviewAction,
  ): Promise<void> {
    await request(
      `/api/v1/runs/${encodeURIComponent(runId)}/review/${encodeURIComponent(gate)}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(action),
      },
    );
  },

  async cancelRun(runId: string): Promise<void> {
    await request(
      `/api/v1/runs/${encodeURIComponent(runId)}`,
      { method: 'DELETE' },
    );
  },

  async rerunPipeline(runId: string): Promise<{ runId: string }> {
    return request(
      `/api/v1/runs/${encodeURIComponent(runId)}/rerun`,
      { method: 'POST' },
    );
  },

  getArtifact(runId: string, path: string): Promise<unknown> {
    return request(
      `/api/v1/runs/${encodeURIComponent(runId)}/artifacts/${path}`,
    );
  },
};
