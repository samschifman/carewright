import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import type { RunDetail } from '../api/types';
import { AutomationReviewPage } from './AutomationReviewPage';

vi.mock('@cpg-to-acp/ui-shared', () => ({
  BpmnViewer: ({ title }: { title: string }) => <div data-testid="bpmn-viewer">{title}</div>,
}));
vi.mock('../api/client', () => ({ api: { submitReview: vi.fn().mockResolvedValue({}) } }));

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
      escalation_reason: 'semantic-budget-exhausted',
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
        parameters: [{ name: 'review_window', type: 'integer', required: true, constraints: { min: 1, max: 4 } }],
        validation: { status: 'escalated', warnings: ['Missing trigger schedule.'], deferred_invariants: ['requires clinician review'] },
      },
    },
  ],
} as RunDetail;

function renderPage(value: RunDetail = run) {
  const queryClient = new QueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <AutomationReviewPage run={value} />
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
    expect(screen.getByText(/Escalated for human review: semantic-budget-exhausted/)).toBeInTheDocument();
    expect(screen.getByText('Missing trigger schedule.')).toBeInTheDocument();
    expect(screen.getAllByTestId('bpmn-viewer')).toHaveLength(2);
  });

  it('shows parameter editing and acknowledgement only for an L7 escalation', () => {
    renderPage({ ...run, awaitingReview: 'pre-delivery' });
    expect(screen.getByRole('spinbutton', { name: 'Default for review_window' })).toBeInTheDocument();
    expect(screen.getAllByRole('checkbox', { name: 'Acknowledge and deliver' })).toHaveLength(1);
    expect(screen.getByRole('textbox', { name: 'Review note for review_window' })).toBeInTheDocument();
  });

  it('does not offer acknowledgement for a mechanical escalation', () => {
    const mechanical: RunDetail = {
      ...run,
      awaitingReview: 'pre-delivery',
      automations: [{
        ...run.automations![1],
        escalation_reason: 'static-budget-exhausted',
      }],
    };
    renderPage(mechanical);
    expect(screen.queryByRole('checkbox', { name: 'Acknowledge and deliver' })).not.toBeInTheDocument();
    expect(screen.getByText(/Escalated for human review: static-budget-exhausted/)).toBeInTheDocument();
  });

  it('submits parameter edits and L7 acknowledgement with the approval action', async () => {
    vi.mocked(api.submitReview).mockClear();
    renderPage({ ...run, awaitingReview: 'pre-delivery' });
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Default for review_window' }), {
      target: { value: '3' },
    });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Acknowledge and deliver' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Acknowledgement note for Follow-up contact' }), {
      target: { value: 'Reviewed.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Approve & Continue' }));

    await waitFor(() => expect(api.submitReview).toHaveBeenCalledWith('run-1', 'pre-delivery', {
      action: 'approve',
      parameterEdits: [{ templateId: 'follow-up-contact', parameter: 'review_window', value: 3 }],
      acknowledgements: [{ templateId: 'follow-up-contact', note: 'Reviewed.' }],
    }));
  });
});
