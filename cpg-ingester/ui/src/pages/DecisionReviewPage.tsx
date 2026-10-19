import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  CodeBlock,
  CodeBlockCode,
  Content,
  ExpandableSection,
  Label,
} from '@patternfly/react-core';
import { useCallback, useState } from 'react';
import type { ReviewFeedbackItem, RunDetail } from '../api/types';
import { DmnDecisionTable } from '../components/DmnDecisionTable';
import { FeedbackInput } from '../components/FeedbackInput';
import { ReviewActionBar } from '../components/ReviewActionBar';
import { useReviewGate } from '../hooks/useReviewGate';
import { BpmnViewer } from '@cpg-to-acp/ui-shared';
import { automationsForDecision } from '../utils/automationLinks';

interface DecisionReviewPageProps {
  run: RunDetail;
  onOpenAutomations?: () => void;
}

export function DecisionReviewPage({ run, onOpenAutomations }: DecisionReviewPageProps) {
  const review = useReviewGate(run);
  const [expandedXml, setExpandedXml] = useState<Set<string>>(new Set());
  const [feedbackMap, setFeedbackMap] = useState<Map<string, { itemType: ReviewFeedbackItem['itemType']; comment: string }>>(new Map());

  const handleFeedbackChange = useCallback((itemId: string, comment: string) => {
    setFeedbackMap(prev => {
      const next = new Map(prev);
      if (comment) {
        next.set(itemId, { itemType: 'decision', comment });
      } else {
        next.delete(itemId);
      }
      return next;
    });
  }, []);

  if (!run.decisions || run.decisions.length === 0) {
    return (
      <Card style={{ marginTop: 16 }}>
        <CardBody>
          <Content component="p">No decision models available yet.</Content>
        </CardBody>
      </Card>
    );
  }

  return (
    <div style={{ marginTop: 16 }}>
      {run.decisions.map((decision, i) => {
        const key = decision.decision_model_summary?.id ?? `dmn-${i}`;
        const name = decision.decision_model_summary?.name ?? decision.item.name;
        const isXmlExpanded = expandedXml.has(key);
        const linkedAutomations = automationsForDecision(decision, run.automations ?? []);

        return (
          <Card key={key} style={{ marginBottom: 16 }}>
            <CardTitle>
              {name}
              {decision.item.category && (
                <Label color="blue" isCompact style={{ marginLeft: 8 }}>
                  {decision.item.category}
                </Label>
              )}
              {decision.item.section && (
                <Content component="small" style={{ marginLeft: 8, fontWeight: 'normal' }}>
                  Source: {decision.item.section}
                </Content>
              )}
            </CardTitle>
            <CardBody>
              {decision.escalated && (
                <Alert
                  variant="danger"
                  title={`Escalated for human review: ${decision.escalation_reason ?? 'unknown'}`}
                  isInline
                  style={{ marginBottom: 8 }}
                >
                  {decision.escalation_errors?.length ? (
                    <ul>
                      {decision.escalation_errors.map((error, errorIndex) => (
                        <li key={errorIndex}>{error}</li>
                      ))}
                    </ul>
                  ) : null}
                </Alert>
              )}

              {decision.validation_warnings?.map((warning, warningIndex) => (
                <Alert
                  key={warningIndex}
                  variant="warning"
                  title="DMN validation warning"
                  isInline
                  isPlain
                  style={{ marginBottom: 8 }}
                >
                  {warning}
                </Alert>
              ))}

              {decision.dmn_xml && (
                <DmnDecisionTable xml={decision.dmn_xml} />
              )}

              <ExpandableSection
                toggleText={isXmlExpanded ? 'Hide DMN XML' : 'Show DMN XML'}
                isExpanded={isXmlExpanded}
                onToggle={() => {
                  setExpandedXml(prev => {
                    const next = new Set(prev);
                    if (next.has(key)) next.delete(key);
                    else next.add(key);
                    return next;
                  });
                }}
                style={{ marginTop: 16 }}
              >
                <CodeBlock>
                  <CodeBlockCode>{decision.dmn_xml}</CodeBlockCode>
                </CodeBlock>
              </ExpandableSection>

              {linkedAutomations.length > 0 && (
                <ExpandableSection toggleText="Linked automations" isIndented>
                  {linkedAutomations.map(automation => (
                    <section key={automation.template_summary.id} style={{ marginTop: 8 }}>
                      <Content component="h5">{automation.template_summary.name}</Content>
                      <Content component="p">This automation is linked to this decision model.</Content>
                      {automation.bpmn_xml && (
                        <BpmnViewer
                          xml={automation.bpmn_xml}
                          title={`${automation.template_summary.name} process diagram`}
                        />
                      )}
                    </section>
                  ))}
                  {onOpenAutomations && (
                    <Button variant="link" onClick={onOpenAutomations}>
                      Review in Automations tab
                    </Button>
                  )}
                </ExpandableSection>
              )}

              {review.isReviewActive && review.gate === 'pre-delivery' && (
                <FeedbackInput
                  itemId={key}
                  onFeedbackChange={handleFeedbackChange}
                  existingComment={feedbackMap.get(key)?.comment}
                />
              )}
            </CardBody>
          </Card>
        );
      })}

      {review.isReviewActive && review.gate === 'pre-delivery' && (
        <ReviewActionBar
          onApprove={() => review.approveMutation.mutate({})}
          onRequestChanges={(feedback, comment) =>
            review.requestChangesMutation.mutate({ feedback, overallComment: comment })
          }
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
