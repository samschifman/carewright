import {
  Alert,
  Card,
  CardBody,
  CardTitle,
  Content,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Label,
  Split,
  SplitItem,
  TreeView,
  type TreeViewDataItem,
} from '@patternfly/react-core';
import { useCallback, useMemo, useState } from 'react';
import type { ReviewFeedbackItem, RunDetail } from '../api/types';
import { FeedbackInput } from '../components/FeedbackInput';
import { ReviewActionBar } from '../components/ReviewActionBar';
import { useReviewGate } from '../hooks/useReviewGate';

const CLASSIFICATION_COLORS: Record<string, 'blue' | 'green' | 'grey' | 'orange'> = {
  decision: 'blue',
  recommendation: 'green',
  background: 'grey',
  methods: 'grey',
};

interface StructureReviewPageProps {
  run: RunDetail;
}

export function StructureReviewPage({ run }: StructureReviewPageProps) {
  const review = useReviewGate(run);
  const [feedbackMap, setFeedbackMap] = useState<Map<string, { itemType: ReviewFeedbackItem['itemType']; comment: string }>>(new Map());

  const handleFeedbackChange = useCallback((itemId: string, comment: string) => {
    setFeedbackMap(prev => {
      const next = new Map(prev);
      if (comment) {
        next.set(itemId, { itemType: 'classification', comment });
      } else {
        next.delete(itemId);
      }
      return next;
    });
  }, []);

  const treeData: TreeViewDataItem[] = useMemo(() => {
    if (!run.sectionMap) return [];
    return run.sectionMap.map((section, i) => ({
      id: `section-${i}`,
      name: (
        <Split hasGutter>
          <SplitItem>{section.heading}</SplitItem>
          <SplitItem>
            <Label color={CLASSIFICATION_COLORS[section.classification] ?? 'grey'} isCompact>
              {section.classification}
            </Label>
          </SplitItem>
          {section.page_start != null && (
            <SplitItem>
              <Content component="small">
                pp. {section.page_start}–{section.page_end ?? '?'}
              </Content>
            </SplitItem>
          )}
        </Split>
      ),
      children: review.isReviewActive && review.gate === 'manifest'
        ? [{
            id: `feedback-${i}`,
            name: (
              <FeedbackInput
                itemId={`section-${i}`}
                onFeedbackChange={handleFeedbackChange}
                existingComment={feedbackMap.get(`section-${i}`)?.comment}
              />
            ),
          }]
        : undefined,
    }));
  }, [run.sectionMap, review.isReviewActive, review.gate, feedbackMap, handleFeedbackChange]);

  const hasSectionData = run.sectionMap && run.sectionMap.length > 0;
  const analyzeError = run.errors?.find(e => e.step === 'Analyze');
  const groupedManifest = new Map<string, NonNullable<RunDetail['manifest']>>();
  for (const item of run.manifest ?? []) {
    const group = groupedManifest.get(item.type) ?? [];
    group.push(item);
    groupedManifest.set(item.type, group);
  }

  return (
    <div style={{ marginTop: 16 }}>
      {analyzeError && (
        <Alert variant="danger" title="Analysis failed" isInline style={{ marginBottom: 16 }}>
          {analyzeError.message}
        </Alert>
      )}

      {groupedManifest.size > 0 && (
        <Card style={{ marginBottom: 16 }}>
          <CardTitle>Identified items</CardTitle>
          <CardBody>
            {[...groupedManifest.entries()].map(([type, items]) => (
              <section key={type} aria-label={`${type} items`} style={{ marginBottom: 12 }}>
                <Content component="h3">{type[0]?.toUpperCase()}{type.slice(1)} ({items.length})</Content>
                <ul>
                  {items.map((item) => {
                    const automatability = item.automatability as { tier?: string } | undefined;
                    const tier = item.tier_hint ?? item.tier ?? automatability?.tier;
                    const trigger = item.trigger_hint
                      ?? item.trigger_summary
                      ?? (typeof item.trigger === 'string' ? item.trigger : undefined);
                    return (
                      <li key={item.id}>
                        <strong>{item.name}</strong>
                        {type === 'process' && (
                          <>
                            {tier ? ` · Tier ${tier}` : ''}
                            {trigger ? ` · Trigger: ${String(trigger)}` : ''}
                            {(item.linked_recommendations?.length || item.linked_decisions?.length) ? (
                              <div>
                                Linked: {[...(item.linked_recommendations ?? []), ...(item.linked_decisions ?? [])].join(', ')}
                              </div>
                            ) : null}
                          </>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
          </CardBody>
        </Card>
      )}

      {hasSectionData ? (
        <Split hasGutter>
          <SplitItem isFilled>
            <Card>
              <CardTitle>Section Structure</CardTitle>
              <CardBody>
                <TreeView data={treeData} />
              </CardBody>
            </Card>
          </SplitItem>

          {run.metadata && (
            <SplitItem style={{ width: 300 }}>
              <Card>
                <CardTitle>CPG Metadata</CardTitle>
                <CardBody>
                  <DescriptionList isCompact>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Title</DescriptionListTerm>
                      <DescriptionListDescription>{run.metadata.title}</DescriptionListDescription>
                    </DescriptionListGroup>
                    {run.metadata.version && (
                      <DescriptionListGroup>
                        <DescriptionListTerm>Version</DescriptionListTerm>
                        <DescriptionListDescription>{run.metadata.version}</DescriptionListDescription>
                      </DescriptionListGroup>
                    )}
                    {run.metadata.issuing_body && (
                      <DescriptionListGroup>
                        <DescriptionListTerm>Issuing Body</DescriptionListTerm>
                        <DescriptionListDescription>{run.metadata.issuing_body}</DescriptionListDescription>
                      </DescriptionListGroup>
                    )}
                    {run.metadata.grading_system && (
                      <DescriptionListGroup>
                        <DescriptionListTerm>Grading System</DescriptionListTerm>
                        <DescriptionListDescription>{run.metadata.grading_system}</DescriptionListDescription>
                      </DescriptionListGroup>
                    )}
                    {run.metadata.scope && (
                      <DescriptionListGroup>
                        <DescriptionListTerm>Scope</DescriptionListTerm>
                        <DescriptionListDescription>{run.metadata.scope}</DescriptionListDescription>
                      </DescriptionListGroup>
                    )}
                  </DescriptionList>
                </CardBody>
              </Card>
            </SplitItem>
          )}
        </Split>
      ) : (
        <Card>
          <CardBody>
            <Content component="p">No section structure available yet.</Content>
          </CardBody>
        </Card>
      )}

      {review.isReviewActive && review.gate === 'manifest' && (
        <ReviewActionBar
          onApprove={() => review.approveMutation.mutate()}
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
