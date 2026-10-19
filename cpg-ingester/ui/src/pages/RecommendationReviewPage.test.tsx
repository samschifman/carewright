import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { RunDetail } from '../api/types';
import { RecommendationReviewPage } from './RecommendationReviewPage';

vi.mock('@cpg-to-acp/ui-shared', () => ({
  BpmnViewer: ({ title }: { title: string }) => <div data-testid="bpmn-viewer">{title}</div>,
}));

describe('RecommendationReviewPage linked automations', () => {
  it('shows associated BPMN inline and opens the Automations tab', () => {
    const onOpenAutomations = vi.fn();
    const run = {
      id: 'run-1', status: 'completed', cpgName: 'Test', createdAt: '', steps: [],
      recommendations: [{
        id: 'rec-1', title: 'Recommendation One', source_cpg: 'cpg-1',
        content: 'Text.', recommendation_type: 'process',
      }],
      automations: [{
        artifact_id: 'bpmn-1', bpmn_xml: '<definitions/>', process_ir: {},
        item: { id: 'process-1', name: 'Process One', linked_recommendations: ['rec-1'] },
        template_summary: {
          id: 'template-1', version: '1', name: 'Template One', triggers: [],
          linked_recommendation_ids: ['rec-1'], linked_decision_model_ids: [],
          capabilities_used: [], pattern_family: 'other',
        },
      }],
    } as RunDetail;
    const client = new QueryClient();

    render(
      <QueryClientProvider client={client}>
        <RecommendationReviewPage run={run} onOpenAutomations={onOpenAutomations} />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Linked automations' }));

    expect(screen.getByText('This automation is linked to this recommendation.')).toBeInTheDocument();
    expect(screen.getByTestId('bpmn-viewer')).toHaveTextContent('Template One process diagram');
    fireEvent.click(screen.getByRole('button', { name: 'Review in Automations tab' }));
    expect(onOpenAutomations).toHaveBeenCalledOnce();
  });
});
