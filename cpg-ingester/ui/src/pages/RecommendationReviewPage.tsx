import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Content,
  DataList,
  DataListCell,
  DataListItem,
  DataListItemCells,
  DataListItemRow,
  ExpandableSection,
  Label,
  LabelGroup,
} from '@patternfly/react-core';
import { useCallback, useState } from 'react';
import type { ReviewFeedbackItem, RunDetail } from '../api/types';
import { FeedbackInput } from '../components/FeedbackInput';
import { ReviewActionBar } from '../components/ReviewActionBar';
import { useReviewGate } from '../hooks/useReviewGate';
import { recommendationAlerts, type RecommendationAlert } from '../utils/escalations';
import { BpmnViewer } from '@cpg-to-acp/ui-shared';
import { automationsForRecommendation } from '../utils/automationLinks';

const STRENGTH_COLORS: Record<string, 'green' | 'blue' | 'orange' | 'grey'> = {
  'strong-for': 'green',
  'conditional-for': 'blue',
  'conditional-against': 'orange',
  'strong-against': 'orange',
};

interface RecommendationReviewPageProps {
  run: RunDetail;
  onOpenAutomations?: () => void;
}

function EscalationAlert({ alert }: { alert: RecommendationAlert }) {
  return (
    <Alert
      variant="danger"
      title={alert.title}
      isInline
      style={{ marginBottom: 8 }}
    >
      {alert.reason && <Content component="p">Reason: {alert.reason}</Content>}
      {alert.errors?.length ? (
        <ul>
          {alert.errors.map((error, index) => <li key={index}>{error}</li>)}
        </ul>
      ) : null}
    </Alert>
  );
}

export function RecommendationReviewPage({ run, onOpenAutomations }: RecommendationReviewPageProps) {
  const review = useReviewGate(run);
  const [expandedRecs, setExpandedRecs] = useState<Set<string>>(new Set());
  const [feedbackMap, setFeedbackMap] = useState<Map<string, { itemType: ReviewFeedbackItem['itemType']; comment: string }>>(new Map());
  const alerts = recommendationAlerts(run);
  const sectionAlerts = alerts.filter(alert => alert.scope === 'section');
  const recommendationAlertsById = new Map<string, RecommendationAlert>(
    alerts
      .filter(alert => alert.scope === 'recommendation')
      .map(alert => [alert.id, alert] as const),
  );

  const handleFeedbackChange = useCallback((itemId: string, comment: string) => {
    setFeedbackMap(prev => {
      const next = new Map(prev);
      if (comment) {
        next.set(itemId, { itemType: 'recommendation', comment });
      } else {
        next.delete(itemId);
      }
      return next;
    });
  }, []);

  if (!run.recommendations || run.recommendations.length === 0) {
    return (
      <Card style={{ marginTop: 16 }}>
        <CardBody>
          {sectionAlerts.map((alert, index) => (
            <EscalationAlert key={`${alert.id}-${index}`} alert={alert} />
          ))}
          <Content component="p">No recommendations available yet.</Content>
        </CardBody>
      </Card>
    );
  }

  return (
    <div style={{ marginTop: 16 }}>
      <Card>
        <CardTitle>
          Recommendations ({run.recommendations.length})
          {review.reviewIteration > 1 && (
            <Label color="blue" isCompact style={{ marginLeft: 8 }}>
              Iteration {review.reviewIteration}
            </Label>
          )}
        </CardTitle>
        <CardBody>
          {sectionAlerts.map((alert, index) => (
            <EscalationAlert key={`${alert.id}-${index}`} alert={alert} />
          ))}
          <DataList aria-label="Recommendations">
            {run.recommendations.map((rec) => {
              const isExpanded = expandedRecs.has(rec.id);
              const strengthLabel = rec.certainty?.strength ?? '';
              const strengthColor = STRENGTH_COLORS[strengthLabel] ?? 'grey';
              const escalationAlert = recommendationAlertsById.get(rec.id);
              const linkedAutomations = automationsForRecommendation(rec, run.automations ?? []);

              return (
                <DataListItem key={rec.id} aria-labelledby={`rec-${rec.id}`}>
                  <DataListItemRow>
                    <DataListItemCells
                      dataListCells={[
                        <DataListCell key="title" width={3}>
                          <div>
                            <strong id={`rec-${rec.id}`}>{rec.title}</strong>
                          </div>
                          {rec.section && (
                            <Content component="small">
                              Source: {rec.section}
                            </Content>
                          )}
                        </DataListCell>,
                        <DataListCell key="labels" width={2}>
                          <LabelGroup>
                            <Label isCompact color="blue">{rec.recommendation_type}</Label>
                            {strengthLabel && (
                              <Label isCompact color={strengthColor}>{strengthLabel}</Label>
                            )}
                            {rec.certainty?.evidence_quality && (
                              <Label isCompact>{rec.certainty.evidence_quality}</Label>
                            )}
                          </LabelGroup>
                        </DataListCell>,
                      ]}
                    />
                  </DataListItemRow>
                  {escalationAlert && <EscalationAlert alert={escalationAlert} />}
                  <div style={{ padding: '0 16px 16px' }}>
                    <ExpandableSection
                      toggleText={isExpanded ? 'Hide details' : 'Show details'}
                      isExpanded={isExpanded}
                      onToggle={() => {
                        setExpandedRecs(prev => {
                          const next = new Set(prev);
                          if (next.has(rec.id)) next.delete(rec.id);
                          else next.add(rec.id);
                          return next;
                        });
                      }}
                    >
                      <Content component="p">{rec.content}</Content>
                      {rec.rationale && (
                        <>
                          <Content component="h6">Rationale</Content>
                          <Content component="p">{rec.rationale}</Content>
                        </>
                      )}
                      {rec.scope_notes && (
                        <>
                          <Content component="h6">Scope Notes</Content>
                          <Content component="p">{rec.scope_notes}</Content>
                        </>
                      )}
                      {rec.cross_references && rec.cross_references.length > 0 && (
                        <>
                          <Content component="h6">Cross-References</Content>
                          <LabelGroup>
                            {rec.cross_references.map((xref, j) => (
                              <Label key={j} isCompact color="purple">
                                {xref.target_type}: {xref.target_id} ({xref.relationship})
                              </Label>
                            ))}
                          </LabelGroup>
                        </>
                      )}
                      {rec.source_location?.source_text && (
                        <>
                          <Content component="h6">Source Text</Content>
                          <Content component="blockquote">{rec.source_location.source_text}</Content>
                        </>
                      )}
                    </ExpandableSection>
                    {linkedAutomations.length > 0 && (
                      <ExpandableSection toggleText="Linked automations" isIndented>
                        {linkedAutomations.map(automation => (
                          <section key={automation.template_summary.id} style={{ marginTop: 8 }}>
                            <Content component="h5">{automation.template_summary.name}</Content>
                            <Content component="p">This automation is linked to this recommendation.</Content>
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
                        itemId={rec.id}
                        onFeedbackChange={handleFeedbackChange}
                        existingComment={feedbackMap.get(rec.id)?.comment}
                      />
                    )}
                  </div>
                </DataListItem>
              );
            })}
          </DataList>
        </CardBody>
      </Card>

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
