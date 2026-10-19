import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import type { RunDetail } from '../api/types';
import { StructureReviewPage } from './StructureReviewPage';

vi.mock('../api/client', () => ({
  api: { submitReview: vi.fn().mockResolvedValue({}) },
}));

const run = {
  id: 'run-1',
  status: 'completed',
  cpgName: 'Synthetic Hypertension',
  createdAt: '2026-10-09T00:00:00Z',
  steps: [],
  sectionMap: [],
  manifest: [
    {
      id: 'process-home-bp',
      type: 'process',
      name: 'Home blood pressure monitoring',
      tier_hint: 'A',
      trigger_hint: 'Adults starting treatment',
      linked_recommendations: ['Home BP readings'],
      linked_decisions: ['Treatment recommendation'],
    },
    { id: 'rec-1', type: 'recommendation', name: 'Lifestyle changes' },
  ],
} as RunDetail;

describe('StructureReviewPage identified items', () => {
  it('shows processes grouped with tier, trigger and linked item names', () => {
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <StructureReviewPage run={run} />
      </QueryClientProvider>,
    );
    expect(screen.getByText('Identified items')).toBeInTheDocument();
    expect(screen.getByText('Home blood pressure monitoring')).toBeInTheDocument();
    expect(screen.getByText(/Tier A/)).toBeInTheDocument();
    expect(screen.getByText(/Trigger: Adults starting treatment/)).toBeInTheDocument();
    expect(screen.getByText(/Home BP readings, Treatment recommendation/)).toBeInTheDocument();
    expect(screen.getByText('Lifestyle changes')).toBeInTheDocument();
  });

  it('submits process feedback with the process item type', async () => {
    vi.mocked(api.submitReview).mockClear();
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <StructureReviewPage run={{ ...run, awaitingReview: 'manifest' }} />
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Add Feedback' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Feedback for process-home-bp' }), {
      target: { value: 'Keep this process item.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Request Changes (1)' }));
    fireEvent.click(screen.getByRole('button', { name: 'Submit Feedback' }));

    await waitFor(() => expect(api.submitReview).toHaveBeenCalledWith('run-1', 'manifest', {
      action: 'request_changes',
      feedback: [{
        itemId: 'process-home-bp',
        itemType: 'process',
        comment: 'Keep this process item.',
      }],
      overallComment: '',
    }));
  });

  it('uses the classification color mapping for the supported classes', () => {
    const queryClient = new QueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <StructureReviewPage run={{
          ...run,
          sectionMap: [{ heading: '1. Overview', classification: 'both' }],
        }} />
      </QueryClientProvider>,
    );

    expect(screen.getByText('both').closest('.pf-v6-c-label')).toHaveClass('pf-m-orange');
  });
});
