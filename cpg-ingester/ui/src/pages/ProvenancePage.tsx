import {
  Card,
  CardBody,
  CardTitle,
  ClipboardCopy,
  Content,
  Label,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import type { DecisionResult, RecommendationResult, ReviewFeedbackItem, RunDetail } from '../api/types';
import { MermaidDiagram } from '../components/MermaidDiagram';
import { ReviewActionBar } from '../components/ReviewActionBar';
import { useReviewGate } from '../hooks/useReviewGate';

const ARTIFACT_TYPE_LABELS: Record<string, string> = {
  metadata: 'Guidelines Metadata',
  dmn: 'DMN Model',
  recommendations: 'Recommendations',
  assembly_report: 'Assembly Report',
  escalated_items: 'Escalated Items',
};

function sanitize(text: string): string {
  return text.replace(/"/g, '#quot;').replace(/</g, '').replace(/>/g, '');
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return text.slice(0, max - 3) + '...';
}

function buildLineageDefinition(run: RunDetail): string {
  const lines = ['graph LR'];
  const cpgLabel = sanitize(truncate(run.metadata?.title ?? run.cpgName, 50));
  const cpgId = run.metadata?.cpg_id ?? '';

  lines.push(`  CPG["${cpgLabel}<br/><i>${sanitize(cpgId)}</i>"]`);

  const sectionArtifacts = new Map<
    string,
    { decisions: DecisionResult[]; recommendations: RecommendationResult[] }
  >();

  for (const d of run.decisions ?? []) {
    const sec = d.item.section ?? 'Unknown Section';
    if (!sectionArtifacts.has(sec))
      sectionArtifacts.set(sec, { decisions: [], recommendations: [] });
    sectionArtifacts.get(sec)!.decisions.push(d);
  }

  for (const r of run.recommendations ?? []) {
    const sec = r.section ?? 'Unknown Section';
    if (!sectionArtifacts.has(sec))
      sectionArtifacts.set(sec, { decisions: [], recommendations: [] });
    sectionArtifacts.get(sec)!.recommendations.push(r);
  }

  const sectionNodeIds: string[] = [];
  const decisionNodeIds: string[] = [];
  const recNodeIds: string[] = [];

  const decisionIdMap = new Map<string, string>();
  const recIdMap = new Map<string, string>();

  let sIdx = 0;
  for (const [section, artifacts] of sectionArtifacts) {
    const sId = `S${sIdx}`;
    sectionNodeIds.push(sId);
    lines.push(`  CPG --> ${sId}(["${sanitize(truncate(section, 40))}"])`);

    let aIdx = 0;
    for (const d of artifacts.decisions) {
      const nodeId = `${sId}_D${aIdx++}`;
      decisionNodeIds.push(nodeId);
      const name = sanitize(truncate(d.decision_model_summary?.name ?? d.item.name, 35));
      const artId = d.artifact_id ? `<br/>${sanitize(d.artifact_id)}` : '';
      lines.push(`  ${sId} --> ${nodeId}{{"${name}${artId}"}}`);

      if (d.decision_model_summary?.id) {
        decisionIdMap.set(d.decision_model_summary.id, nodeId);
      }
    }

    for (const r of artifacts.recommendations) {
      const nodeId = `${sId}_R${aIdx++}`;
      recNodeIds.push(nodeId);
      const name = sanitize(truncate(r.title, 35));
      const artId = r.artifact_id ? `<br/>${sanitize(r.artifact_id)}` : '';
      lines.push(`  ${sId} --> ${nodeId}("${name}${artId}")`);

      if (r.id) {
        recIdMap.set(r.id, nodeId);
      }
    }

    sIdx++;
  }

  for (const r of run.recommendations ?? []) {
    const srcNode = r.id ? recIdMap.get(r.id) : undefined;
    if (!srcNode || !r.cross_references) continue;
    for (const xref of r.cross_references) {
      let targetNode: string | undefined;
      if (xref.target_type === 'decision') {
        targetNode = decisionIdMap.get(xref.target_id);
      } else if (xref.target_type === 'recommendation') {
        targetNode = recIdMap.get(xref.target_id);
      }
      if (targetNode) {
        lines.push(`  ${srcNode} -.->|${sanitize(xref.relationship)}| ${targetNode}`);
      }
    }
  }

  lines.push('');
  lines.push('  classDef cpg fill:#fff3cd,stroke:#856404,color:#533f03');
  lines.push('  classDef section fill:#f0f0f0,stroke:#666,color:#333');
  lines.push('  classDef decision fill:#bee1f4,stroke:#0066cc,color:#003366');
  lines.push('  classDef recommendation fill:#c3e6cb,stroke:#28a745,color:#155724');
  lines.push('  class CPG cpg');
  if (sectionNodeIds.length) lines.push(`  class ${sectionNodeIds.join(',')} section`);
  if (decisionNodeIds.length) lines.push(`  class ${decisionNodeIds.join(',')} decision`);
  if (recNodeIds.length) lines.push(`  class ${recNodeIds.join(',')} recommendation`);

  return lines.join('\n');
}

interface ProvenancePageProps {
  run: RunDetail;
}

export function ProvenancePage({ run }: ProvenancePageProps) {
  const review = useReviewGate(run);
  const ds = run.deliveryStatus;
  const hasArtifacts =
    (run.decisions?.length ?? 0) > 0 || (run.recommendations?.length ?? 0) > 0;

  if (!hasArtifacts && !ds) {
    return (
      <Card style={{ marginTop: 16 }}>
        <CardBody>
          <Content component="p">No artifacts available yet.</Content>
        </CardBody>
      </Card>
    );
  }

  const emptyFeedback = new Map<
    string,
    { itemType: ReviewFeedbackItem['itemType']; comment: string }
  >();

  return (
    <div style={{ marginTop: 16 }}>
      {ds && (
        <Card isCompact style={{ marginBottom: 16 }}>
          <CardBody style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <Label color={ds.published ? 'green' : 'red'}>
              {ds.published ? 'Published' : 'Not Published'}
            </Label>
            <span>{ds.artifacts?.length ?? 0} artifacts</span>
            {ds.artifact_location && (
              <ClipboardCopy isReadOnly variant="inline-compact">
                {ds.artifact_location}
              </ClipboardCopy>
            )}
          </CardBody>
        </Card>
      )}

      {hasArtifacts && (
        <Card style={{ marginBottom: 16 }}>
          <CardTitle>Artifact Lineage</CardTitle>
          <CardBody>
            <MermaidDiagram definition={buildLineageDefinition(run)} />
          </CardBody>
        </Card>
      )}


      {ds && ds.artifacts && ds.artifacts.length > 0 && (
        <Card style={{ marginBottom: 16 }}>
          <CardTitle>Published Artifacts</CardTitle>
          <CardBody>
            <Table aria-label="Published artifacts">
              <Thead>
                <Tr>
                  <Th>Type</Th>
                  <Th>Artifact ID</Th>
                  <Th>Details</Th>
                  <Th>Reference</Th>
                </Tr>
              </Thead>
              <Tbody>
                {ds.artifacts.map((artifact, i) => (
                  <Tr key={i}>
                    <Td>{ARTIFACT_TYPE_LABELS[artifact.type] ?? artifact.type}</Td>
                    <Td>
                      {artifact.artifact_id ? (
                        <code style={{ fontSize: '0.85em' }}>{artifact.artifact_id}</code>
                      ) : (
                        '—'
                      )}
                    </Td>
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
              </Tbody>
            </Table>
          </CardBody>
        </Card>
      )}

      {review.isReviewActive && review.gate === 'pre-delivery' && (
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
      )}
    </div>
  );
}
