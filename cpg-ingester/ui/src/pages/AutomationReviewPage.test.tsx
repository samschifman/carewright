import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { RunDetail } from '../api/types';
import { AutomationReviewPage } from './AutomationReviewPage';

vi.mock('@cpg-to-acp/ui-shared', () => ({
  BpmnViewer: ({ title }: { title: string }) => <div data-testid="bpmn-viewer">{title}</div>,
}));

const run = {
  id: 'run-1',
  status: 'completed',
  cpgName: 'Synthetic Hypertension',
  createdAt: '2026-10-09T00:00:00Z',
  steps: [],
  automations: [
    {
      artifact_id: 'automation-home-bp',
      bpmn_xml: '<definitions/>',
      process_ir: { process: { id: 'home-bp' } },
      item: { name: 'Home BP monitoring', linked_recommendations: ['rec-home-bp'], linked_decisions: ['dmn-treatment'] },
      template_summary: {
        id: 'home-bp-monitoring',
        version: 'aabbccddeeff',
        name: 'Home blood pressure monitoring',
        description: 'Monitor home readings and escalate concerning values.',
        triggers: [{ kind: 'recommendation', recommendation_id: 'rec-home-bp' }],
        linked_recommendation_ids: ['rec-home-bp'],
        linked_decision_model_ids: ['dmn-treatment'],
        capabilities_used: ['request_readings', 'notify_clinician'],
        pattern_family: 'monitoring-with-escalation',
        automatability: { tier: 'A', rationale: 'Repeated home monitoring.' },
        validation: { status: 'valid', warnings: [], deferred_invariants: [] },
      },
    },
    {
      artifact_id: 'automation-follow-up',
      bpmn_xml: '<definitions/>',
      process_ir: { process: { id: 'follow-up' } },
      item: { name: 'Follow-up contact' },
      escalated: true,
      escalation_reason: 'Needs clinical review',
      escalation_errors: ['Trigger does not define a schedule.'],
      template_summary: {
        id: 'follow-up-contact',
        version: '112233445566',
        name: 'Follow-up contact',
        triggers: [],
        linked_recommendation_ids: [],
        linked_decision_model_ids: [],
        capabilities_used: [],
        pattern_family: 'follow-up',
        validation: { status: 'escalated', warnings: ['Missing trigger schedule.'], deferred_invariants: ['requires clinician review'] },
      },
    },
  ],
} as RunDetail;

function renderPage() {
  const queryClient = new QueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <AutomationReviewPage run={run} />
    </QueryClientProvider>,
  );
}

describe('AutomationReviewPage', () => {
  it('shows valid and escalated templates with diagrams, links, and validation findings', () => {
    renderPage();
    expect(screen.getByText('Home blood pressure monitoring')).toBeInTheDocument();
    expect(screen.getByText('Follow-up contact')).toBeInTheDocument();
    expect(screen.getByText('Recommendation: rec-home-bp')).toBeInTheDocument();
    expect(screen.getByText('Decision: dmn-treatment')).toBeInTheDocument();
    expect(screen.getByText(/Escalated for human review: Needs clinical review/)).toBeInTheDocument();
    expect(screen.getByText('Missing trigger schedule.')).toBeInTheDocument();
    expect(screen.getAllByTestId('bpmn-viewer')).toHaveLength(2);
  });
});
