import {
  Alert,
  Card,
  CardBody,
  CardTitle,
  Checkbox,
  CodeBlock,
  CodeBlockCode,
  Content,
  ExpandableSection,
  Label,
  LabelGroup,
  TextArea,
} from '@patternfly/react-core';
import { useCallback, useState } from 'react';
import { BpmnViewer } from '@cpg-to-acp/ui-shared';

import type {
  AutomationAcknowledgement,
  AutomationParameter,
  AutomationResult,
  ParameterEdit,
  ReviewFeedbackItem,
  RunDetail,
} from '../api/types';
import { FeedbackInput } from '../components/FeedbackInput';
import { ParameterDefaultsForm } from '../components/ParameterDefaultsForm';
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

function automationParameters(automation: AutomationResult): AutomationParameter[] {
  if (automation.template_summary.parameters?.length) return automation.template_summary.parameters;
  const process = (automation.process_ir as {
    process?: { acp?: { parameters?: AutomationParameter[] } };
  }).process;
  return process?.acp?.parameters ?? [];
}

function isL7Escalation(automation: AutomationResult): boolean {
  return automation.escalated === true && new Set([
    'semantic-budget-exhausted',
    'no-source-text',
    'reviewer-unparseable',
  ]).has(automation.escalation_reason ?? '');
}

function AutomationCard({
  automation,
  feedback,
  onFeedbackChange,
  canFeedback,
  parameterEdits,
  onParameterEdit,
  acknowledgement,
  onAcknowledgementChange,
}: {
  automation: AutomationResult;
  feedback?: string;
  onFeedbackChange: (id: string, comment: string) => void;
  canFeedback: boolean;
  parameterEdits: ParameterEdit[];
  onParameterEdit: (templateId: string, edit: ParameterEdit | null, error: string | null, parameter: string) => void;
  acknowledgement?: AutomationAcknowledgement;
  onAcknowledgementChange: (templateId: string, checked: boolean, note: string) => void;
}) {
  const [showXml, setShowXml] = useState(false);
  const [showIr, setShowIr] = useState(false);
  const summary = automation.template_summary;
  const id = automation.artifact_id ?? summary.id;
  const status = validationStatus(automation);
  const statusColor = status === 'valid' ? 'green' : status === 'incomplete' ? 'orange' : 'red';
  const warnings = summary.validation?.warnings ?? [];
  const deferred = summary.validation?.deferred_invariants ?? [];
  const templateId = summary.id;
  const parameters = automationParameters(automation);
  const feedbackId = automation.item.id ?? templateId;
  const l7Escalated = isL7Escalation(automation);

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

        {canFeedback && parameters.length > 0 && (
          <ParameterDefaultsForm
            templateId={templateId}
            parameters={parameters}
            edits={parameterEdits.filter(edit => edit.templateId === templateId)}
            onEditChange={onParameterEdit}
          />
        )}

        {canFeedback && l7Escalated && (
          <section aria-label={`Acknowledgement for ${summary.name}`} style={{ marginTop: 12 }}>
            <Checkbox
              id={`acknowledge-${templateId}`}
              label="Acknowledge and deliver"
              isChecked={!!acknowledgement}
              onChange={(_event, checked) => onAcknowledgementChange(
                templateId,
                checked,
                acknowledgement?.note ?? '',
              )}
            />
            {acknowledgement && (
              <TextArea
                value={acknowledgement.note}
                onChange={(_event, note) => onAcknowledgementChange(templateId, true, note)}
                aria-label={`Acknowledgement note for ${summary.name}`}
                placeholder="Add the review note required to deliver this template."
                rows={2}
              />
            )}
          </section>
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
          <FeedbackInput itemId={feedbackId} onFeedbackChange={onFeedbackChange} existingComment={feedback} />
        )}
      </CardBody>
    </Card>
  );
}

export function AutomationReviewPage({ run }: AutomationReviewPageProps) {
  const review = useReviewGate(run);
  const [feedbackMap, setFeedbackMap] = useState<Map<string, { itemType: ReviewFeedbackItem['itemType']; comment: string }>>(new Map());
  const [parameterEdits, setParameterEdits] = useState<ParameterEdit[]>([]);
  const [parameterErrors, setParameterErrors] = useState<Map<string, string>>(new Map());
  const [acknowledgements, setAcknowledgements] = useState<AutomationAcknowledgement[]>([]);
  const canFeedback = review.isReviewActive && review.gate === 'pre-delivery';

  const handleFeedbackChange = useCallback((itemId: string, comment: string) => {
    setFeedbackMap((previous) => {
      const next = new Map(previous);
      if (comment) next.set(itemId, { itemType: 'automation', comment });
      else next.delete(itemId);
      return next;
    });
  }, []);

  const handleParameterEdit = useCallback((
    templateId: string,
    edit: ParameterEdit | null,
    error: string | null,
    parameter: string,
  ) => {
    const key = `${templateId}:${parameter}`;
    setParameterEdits(previous => {
      const next = previous.filter(item => `${item.templateId}:${item.parameter}` !== key);
      return edit ? [...next, edit] : next;
    });
    setParameterErrors(previous => {
      const next = new Map(previous);
      if (error) next.set(key, error);
      else next.delete(key);
      return next;
    });
  }, []);

  const handleAcknowledgementChange = useCallback((templateId: string, checked: boolean, note: string) => {
    setAcknowledgements(previous => [
      ...previous.filter(item => item.templateId !== templateId),
      ...(checked ? [{ templateId, note }] : []),
    ]);
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
          feedback={feedbackMap.get(automation.item.id ?? automation.template_summary.id)?.comment}
          onFeedbackChange={handleFeedbackChange}
          canFeedback={canFeedback}
          parameterEdits={parameterEdits}
          onParameterEdit={handleParameterEdit}
          acknowledgement={acknowledgements.find(item => item.templateId === automation.template_summary.id)}
          onAcknowledgementChange={handleAcknowledgementChange}
        />
      ))}
      {canFeedback && (
        <ReviewActionBar
          onApprove={extras => review.approveMutation.mutate(extras)}
          onRequestChanges={(feedback, comment, extras) => review.requestChangesMutation.mutate({
            feedback,
            overallComment: comment,
            ...extras,
          })}
          isApproving={review.approveMutation.isPending}
          isRequestingChanges={review.requestChangesMutation.isPending}
          error={review.approveMutation.error ?? review.requestChangesMutation.error}
          itemFeedback={feedbackMap}
          reviewIteration={review.reviewIteration}
          parameterEdits={parameterEdits}
          acknowledgements={acknowledgements}
          hasInvalidParameters={parameterErrors.size > 0}
        />
      )}
    </div>
  );
}
