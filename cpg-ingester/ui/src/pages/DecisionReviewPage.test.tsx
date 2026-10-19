import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { RunDetail } from '../api/types';
import { DecisionReviewPage } from './DecisionReviewPage';

vi.mock('@cpg-to-acp/ui-shared', () => ({
  BpmnViewer: ({ title }: { title: string }) => <div data-testid="bpmn-viewer">{title}</div>,
}));

describe('DecisionReviewPage linked automations', () => {
  it('shows associated BPMN inline and opens the Automations tab', () => {
    const onOpenAutomations = vi.fn();
    const run = {
      id: 'run-1', status: 'completed', cpgName: 'Test', createdAt: '', steps: [],
      decisions: [{
        dmn_xml: '',
        item: { name: 'Decision One', type: 'decision' },
        decision_model_summary: { id: 'model-1', name: 'Decision One', inputs: [], outputs: [] },
      }],
      automations: [{
        artifact_id: 'bpmn-1', bpmn_xml: '<definitions/>', process_ir: {},
        item: { id: 'process-1', name: 'Process One', linked_decisions: ['model-1'] },
        template_summary: {
          id: 'template-1', version: '1', name: 'Template One', triggers: [],
          linked_recommendation_ids: [], linked_decision_model_ids: ['model-1'],
          capabilities_used: [], pattern_family: 'other',
        },
      }],
    } as RunDetail;
    const client = new QueryClient();

    render(
      <QueryClientProvider client={client}>
        <DecisionReviewPage run={run} onOpenAutomations={onOpenAutomations} />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Linked automations' }));

    expect(screen.getByText('This automation is linked to this decision model.')).toBeInTheDocument();
    expect(screen.getByTestId('bpmn-viewer')).toHaveTextContent('Template One process diagram');
    fireEvent.click(screen.getByRole('button', { name: 'Review in Automations tab' }));
    expect(onOpenAutomations).toHaveBeenCalledOnce();
  });
});
