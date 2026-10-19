import {
  Button,
  Card,
  CardBody,
  CardTitle,
  ClipboardCopy,
  Content,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Label,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import type { ReviewFeedbackItem, RunDetail } from '../api/types';
import { ReviewActionBar } from '../components/ReviewActionBar';
import { useReviewGate } from '../hooks/useReviewGate';

const ARTIFACT_TYPE_LABELS: Record<string, string> = {
  metadata: 'Guidelines Metadata',
  dmn: 'DMN Model',
  recommendations: 'Recommendations',
  assembly_report: 'Assembly Report',
  escalated_items: 'Escalated Items',
};

interface ApprovalDeliveryPageProps {
  run: RunDetail;
}

export function ApprovalDeliveryPage({ run }: ApprovalDeliveryPageProps) {
  const review = useReviewGate(run);

  if (run.deliveryStatus) {
    const ds = run.deliveryStatus;
    return (
      <div style={{ marginTop: 16 }}>
        <Card>
          <CardTitle>Published Artifacts</CardTitle>
          <CardBody>
            <Label color={ds.published ? 'green' : 'red'} style={{ marginBottom: 16 }}>
              {ds.published ? 'Published Successfully' : 'Publishing Failed'}
            </Label>
            <DescriptionList isCompact isHorizontal>
              <DescriptionListGroup>
                <DescriptionListTerm>CPG ID</DescriptionListTerm>
                <DescriptionListDescription>{ds.cpg_id}</DescriptionListDescription>
              </DescriptionListGroup>
              {ds.artifact_location && (
                <DescriptionListGroup>
                  <DescriptionListTerm>Location</DescriptionListTerm>
                  <DescriptionListDescription>
                    <ClipboardCopy isReadOnly variant="inline-compact">
                      {ds.artifact_location}
                    </ClipboardCopy>
                  </DescriptionListDescription>
                </DescriptionListGroup>
              )}
            </DescriptionList>

            <Table aria-label="Published artifacts" style={{ marginTop: 16 }}>
              <Thead>
                <Tr>
                  <Th>Artifact</Th>
                  <Th>Details</Th>
                  <Th>Reference</Th>
                </Tr>
              </Thead>
              <Tbody>
                {ds.artifacts?.map((artifact, i) => (
                  <Tr key={i}>
                    <Td>{ARTIFACT_TYPE_LABELS[artifact.type] ?? artifact.type}</Td>
                    <Td>
                      {artifact.name ?? ''}
                      {artifact.cpg_id ?? ''}
                      {artifact.count != null ? `${artifact.count} items` : ''}
                    </Td>
                    <Td>
                      <code style={{ fontSize: '0.85em' }}>{artifact.ref}</code>
                    </Td>
                  </Tr>
                ))}
                {ds.errors?.map((err, i) => (
                  <Tr key={`err-${i}`}>
                    <Td>Error</Td>
                    <Td colSpan={2}><Label color="red" isCompact>Error</Label> {err}</Td>
                  </Tr>
                ))}
              </Tbody>
            </Table>
          </CardBody>
        </Card>
      </div>
    );
  }

  const emptyFeedback = new Map<string, { itemType: ReviewFeedbackItem['itemType']; comment: string }>();

  return (
    <div style={{ marginTop: 16 }}>
      <Card>
        <CardTitle>Publish Artifacts</CardTitle>
        <CardBody>
          {run.metadata && (
            <DescriptionList isCompact isHorizontal style={{ marginBottom: 16 }}>
              <DescriptionListGroup>
                <DescriptionListTerm>CPG</DescriptionListTerm>
                <DescriptionListDescription>{run.metadata.title}</DescriptionListDescription>
              </DescriptionListGroup>
              {run.metadata.version && (
                <DescriptionListGroup>
                  <DescriptionListTerm>Version</DescriptionListTerm>
                  <DescriptionListDescription>{run.metadata.version}</DescriptionListDescription>
                </DescriptionListGroup>
              )}
            </DescriptionList>
          )}

          <Content component="p">The following artifacts will be published to the artifact store:</Content>
          <ul>
            <li>Guidelines metadata</li>
            {run.decisions && <li>{run.decisions.length} DMN decision model(s)</li>}
            {run.recommendations && <li>{run.recommendations.length} recommendation(s)</li>}
          </ul>

          {review.isReviewActive && review.gate === 'pre-delivery' ? (
            <ReviewActionBar
              onApprove={() => review.approveMutation.mutate({})}
              onRequestChanges={(feedback, comment) =>
                review.requestChangesMutation.mutate({ feedback, overallComment: comment })
              }
              isApproving={review.approveMutation.isPending}
              isRequestingChanges={review.requestChangesMutation.isPending}
              error={review.approveMutation.error ?? review.requestChangesMutation.error}
              itemFeedback={emptyFeedback}
              reviewIteration={review.reviewIteration}
            />
          ) : (
            <Button variant="primary" isDisabled style={{ marginTop: 16 }}>
              Waiting for pipeline to reach delivery...
            </Button>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
