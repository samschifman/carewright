import { render, screen } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { api, ApiError } from './api/client';
import { SessionGate } from './SessionGate';

vi.mock('./api/client', async (original) => {
  const actual = await original<typeof import('./api/client')>();
  return { ...actual, api: { getSession: vi.fn(), logout: vi.fn() } };
});
beforeEach(() => vi.clearAllMocks());

it('requires a launch link before rendering protected content', async () => {
  vi.mocked(api.getSession).mockRejectedValue(new ApiError(401, 'session-required'));
  render(<SessionGate><p>Protected runs</p></SessionGate>);
  expect(await screen.findByText(/new launch link/i)).toBeInTheDocument();
  expect(screen.queryByText('Protected runs')).not.toBeInTheDocument();
});

it('renders protected content after the server confirms the session', async () => {
  vi.mocked(api.getSession).mockResolvedValue({ kind: 'operator', csrfToken: 'synthetic' });
  render(<SessionGate><p>Protected runs</p></SessionGate>);
  expect(await screen.findByText('Protected runs')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument();
});
