import {
  Alert,
  Card,
  CardBody,
  CardTitle,
  CodeBlock,
  CodeBlockCode,
  Content,
  ExpandableSection,
  Label,
  LabelGroup,
} from '@patternfly/react-core';
import { useCallback, useState } from 'react';
import { BpmnViewer } from '@cpg-to-acp/ui-shared';

import type { AutomationResult, ReviewFeedbackItem, RunDetail } from '../api/types';
import { FeedbackInput } from '../components/FeedbackInput';
import { ReviewActionBar } from '../components/ReviewActionBar';
import { useReviewGate } from '../hooks/useReviewGate';

interface AutomationReviewPageProps {
  run: RunDetail;
}

function validationStatus(automation: AutomationResult): 'valid' | 'incomplete' | 'escalated' {
  if (automation.escalated) return 'escalated';
  return automation.template_summary.validation?.status ?? 'valid';
}

function describeTrigger(trigger: Record<string, unknown>): string {
  return Object.entries(trigger)
    .map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`)
    .join(' · ');
}

function AutomationCard({
  automation,
  feedback,
  onFeedbackChange,
  canFeedback,
}: {
  automation: AutomationResult;
  feedback?: string;
  onFeedbackChange: (id: string, comment: string) => void;
  canFeedback: boolean;
}) {
  const [showXml, setShowXml] = useState(false);
  const [showIr, setShowIr] = useState(false);
  const summary = automation.template_summary;
  const id = automation.artifact_id ?? summary.id;
  const status = validationStatus(automation);
  const statusColor = status === 'valid' ? 'green' : status === 'incomplete' ? 'orange' : 'red';
  const warnings = summary.validation?.warnings ?? [];
  const deferred = summary.validation?.deferred_invariants ?? [];

  return (
    <Card data-testid={`automation-${id}`} style={{ marginBottom: 16 }}>
      <CardTitle>
        {summary.name}
        <Label color={statusColor} isCompact style={{ marginLeft: 8 }}>{status}</Label>
        {summary.automatability?.tier && (
          <Label color="blue" isCompact style={{ marginLeft: 8 }}>
            Tier {summary.automatability.tier}
          </Label>
        )}
      </CardTitle>
      <CardBody>
        {summary.description && <Content component="p">{summary.description}</Content>}
        <Content component="p">Pattern family: {summary.pattern_family}</Content>

        <Content component="h4">Triggers</Content>
        <LabelGroup>
          {(summary.triggers ?? []).map((trigger, index) => (
            <Label key={index} isCompact>{describeTrigger(trigger)}</Label>
          ))}
        </LabelGroup>

        <Content component="h4">Linked recommendations and decisions</Content>
        <LabelGroup>
          {(automation.item.linked_recommendations ?? summary.linked_recommendation_ids ?? []).map((name) => (
            <Label key={`rec-${name}`} color="green" isCompact>Recommendation: {name}</Label>
          ))}
          {(automation.item.linked_decisions ?? summary.linked_decision_model_ids ?? []).map((name) => (
            <Label key={`decision-${name}`} color="blue" isCompact>Decision: {name}</Label>
          ))}
        </LabelGroup>

        {automation.escalated && (
          <Alert
            variant="danger"
            title={`Escalated for human review: ${automation.escalation_reason ?? 'review required'}`}
            isInline
            style={{ marginTop: 12 }}
          >
            {automation.escalation_errors?.length ? (
              <ul>{automation.escalation_errors.map((error, index) => <li key={index}>{error}</li>)}</ul>
            ) : null}
          </Alert>
        )}

        {warnings.map((warning, index) => (
          <Alert key={`warning-${index}`} variant="warning" title="Validation warning" isInline style={{ marginTop: 8 }}>
            {warning}
          </Alert>
        ))}
        {deferred.length > 0 && (
          <>
            <Content component="h4">Deferred invariants</Content>
            <ul>{deferred.map((item, index) => <li key={index}>{item}</li>)}</ul>
          </>
        )}

        {summary.capabilities_used?.length > 0 && (
          <>
            <Content component="h4">Capabilities</Content>
            <LabelGroup>{summary.capabilities_used.map((item) => <Label key={item} isCompact>{item}</Label>)}</LabelGroup>
          </>
        )}

        {automation.bpmn_xml ? (
          <BpmnViewer
            xml={automation.bpmn_xml}
            title={`${summary.name} process diagram`}
          />
        ) : <Content component="p">No BPMN diagram is available.</Content>}

        <ExpandableSection
          toggleText={showXml ? 'Hide BPMN XML' : 'View BPMN XML'}
          isExpanded={showXml}
          onToggle={(_event, expanded) => setShowXml(expanded)}
        >
          <CodeBlock><CodeBlockCode>{automation.bpmn_xml}</CodeBlockCode></CodeBlock>
        </ExpandableSection>
        <ExpandableSection
          toggleText={showIr ? 'Hide Process IR' : 'View Process IR JSON'}
          isExpanded={showIr}
          onToggle={(_event, expanded) => setShowIr(expanded)}
        >
          <CodeBlock><CodeBlockCode>{JSON.stringify(automation.process_ir, null, 2)}</CodeBlockCode></CodeBlock>
        </ExpandableSection>

        {canFeedback && (
          <FeedbackInput itemId={id} onFeedbackChange={onFeedbackChange} existingComment={feedback} />
        )}
      </CardBody>
    </Card>
  );
}

export function AutomationReviewPage({ run }: AutomationReviewPageProps) {
  const review = useReviewGate(run);
  const [feedbackMap, setFeedbackMap] = useState<Map<string, { itemType: ReviewFeedbackItem['itemType']; comment: string }>>(new Map());
  const canFeedback = review.isReviewActive && review.gate === 'pre-delivery';

  const handleFeedbackChange = useCallback((itemId: string, comment: string) => {
    setFeedbackMap((previous) => {
      const next = new Map(previous);
      if (comment) next.set(itemId, { itemType: 'automation', comment });
      else next.delete(itemId);
      return next;
    });
  }, []);

  if (!run.automations?.length) {
    return <Content component="p">No automation templates were generated for this run.</Content>;
  }

  return (
    <div style={{ marginTop: 16 }}>
      {run.automations.map((automation, index) => (
        <AutomationCard
          key={automation.artifact_id ?? automation.template_summary.id ?? index}
          automation={automation}
          feedback={feedbackMap.get(automation.artifact_id ?? automation.template_summary.id)?.comment}
          onFeedbackChange={handleFeedbackChange}
          canFeedback={canFeedback}
        />
      ))}
      {canFeedback && (
        <ReviewActionBar
          onApprove={() => review.approveMutation.mutate()}
          onRequestChanges={(feedback, comment) => review.requestChangesMutation.mutate({ feedback, overallComment: comment })}
          isApproving={review.approveMutation.isPending}
          isRequestingChanges={review.requestChangesMutation.isPending}
          error={review.approveMutation.error ?? review.requestChangesMutation.error}
          itemFeedback={feedbackMap}
          reviewIteration={review.reviewIteration}
        />
      )}
    </div>
  );
}
