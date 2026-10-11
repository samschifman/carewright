import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { RunDetail } from '../api/types';
import { StructureReviewPage } from './StructureReviewPage';

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
});
