import {
  Alert,
  Button,
  Card,
  CardBody,
  EmptyState,
  EmptyStateBody,
  Label,
  PageSection,
  Skeleton,
  Tab,
  TabTitleText,
  Tabs,
  Title,
} from '@patternfly/react-core';
import { ExclamationCircleIcon, RedoIcon, TimesCircleIcon } from '@patternfly/react-icons';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';

import { api } from '../api/client';
import type { RunStatus } from '../api/types';
import { AiReasoningLog } from '../components/AiReasoningLog';
import { PipelineStepperComponent } from '../components/PipelineStepper';
import { useRunDetail } from '../hooks/useRunDetail';
import { StructureReviewPage } from './StructureReviewPage';
import { DecisionReviewPage } from './DecisionReviewPage';
import { RecommendationReviewPage } from './RecommendationReviewPage';
import { AssemblyReportPage } from './AssemblyReportPage';
import { ProvenancePage } from './ProvenancePage';
import { AutomationReviewPage } from './AutomationReviewPage';

function isAtLeast(current: RunStatus, threshold: RunStatus): boolean {
  const order: RunStatus[] = [
    'parsing', 'analyzing', 'awaiting_manifest_review',
    'generating', 'awaiting_artifact_review',
    'assembling', 'delivering', 'completed',
  ];
  if (current === 'failed') return true;
  return order.indexOf(current) >= order.indexOf(threshold);
}

export function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: run, isLoading, isError } = useRunDetail(runId ?? '');
  const [activeTab, setActiveTab] = useState(0);

  const rerunMutation = useMutation({
    mutationFn: () => api.rerunPipeline(runId ?? ''),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['runs'] });
      navigate(`/runs/${data.runId}`);
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => api.cancelRun(runId ?? ''),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['runs'] });
      queryClient.invalidateQueries({ queryKey: ['run', runId] });
      navigate('/');
    },
  });

  if (isLoading) {
    return (
      <PageSection>
        <Skeleton screenreaderText="Loading run details" width="40%" style={{ marginBottom: 16 }} />
        <Skeleton width="60%" style={{ marginBottom: 8 }} />
        <Skeleton width="100%" height="200px" />
      </PageSection>
    );
  }

  if (isError || !run) {
    return (
      <PageSection>
        <EmptyState
          headingLevel="h2"
          titleText="Run not found"
          icon={ExclamationCircleIcon}
        >
          <EmptyStateBody>
            Could not load details for run {runId}.
          </EmptyStateBody>
        </EmptyState>
      </PageSection>
    );
  }

  const showStructure = isAtLeast(run.status, 'awaiting_manifest_review');
  const showDecisions = isAtLeast(run.status, 'awaiting_artifact_review');
  const showRecommendations = showDecisions;
  const showAssembly = isAtLeast(run.status, 'assembling');
  const showProvenance = isAtLeast(run.status, 'awaiting_artifact_review');
  const showAutomations = showDecisions && !!run.automations?.length;

  return (
    <>
      <PageSection>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <Title headingLevel="h1">{run.cpgName}</Title>
            <span style={{ color: 'var(--pf-t--global--text--color--subtle)' }}>
              Run {run.id} &middot; {new Date(run.createdAt).toLocaleString()}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {run.awaitingReview && (
              <Label color="orange">
                Awaiting {run.awaitingReview === 'manifest' ? 'Manifest' : 'Artifact'} Review
                {run.reviewIteration && run.reviewIteration > 1
                  ? ` (iteration ${run.reviewIteration})`
                  : ''}
              </Label>
            )}
            {run.status !== 'completed' && run.status !== 'failed' && run.status !== 'cancelled' && (
              <Button
                variant="secondary"
                isDanger
                icon={<TimesCircleIcon />}
                onClick={() => cancelMutation.mutate()}
                isDisabled={cancelMutation.isPending}
                isLoading={cancelMutation.isPending}
              >
                Cancel
              </Button>
            )}
            {(run.status === 'completed' || run.status === 'failed' || run.status === 'cancelled') && (
              <Button
                variant="secondary"
                icon={<RedoIcon />}
                onClick={() => rerunMutation.mutate()}
                isDisabled={rerunMutation.isPending}
                isLoading={rerunMutation.isPending}
              >
                Rerun
              </Button>
            )}
          </div>
        </div>
      </PageSection>
      <PageSection>
        <Tabs activeKey={activeTab} onSelect={(_e, key) => setActiveTab(key as number)}>
          <Tab eventKey={0} title={<TabTitleText>Progress</TabTitleText>}>
            {run.warnings?.map((w, i) => (
              <Alert
                key={i}
                variant="warning"
                title={w.message}
                isInline
                style={{ marginTop: 16 }}
              />
            ))}
            <Card style={{ marginTop: 16 }}>
              <CardBody>
                <PipelineStepperComponent steps={run.steps} />
              </CardBody>
            </Card>
            <Card style={{ marginTop: 16 }}>
              <CardBody>
                <AiReasoningLog steps={run.steps} />
              </CardBody>
            </Card>
          </Tab>

          {showStructure && (
            <Tab eventKey={1} title={<TabTitleText>Structure</TabTitleText>}>
              <StructureReviewPage run={run} />
            </Tab>
          )}

          {showDecisions && (
            <Tab eventKey={2} title={<TabTitleText>Decisions</TabTitleText>}>
              <DecisionReviewPage run={run} onOpenAutomations={() => setActiveTab(6)} />
            </Tab>
          )}

          {showRecommendations && (
            <Tab eventKey={3} title={<TabTitleText>Recommendations</TabTitleText>}>
              <RecommendationReviewPage run={run} onOpenAutomations={() => setActiveTab(6)} />
            </Tab>
          )}

          {showAutomations && (
            <Tab eventKey={6} title={<TabTitleText>Automations</TabTitleText>}>
              <AutomationReviewPage run={run} />
            </Tab>
          )}

          {showAssembly && (
            <Tab eventKey={4} title={<TabTitleText>Assembly</TabTitleText>}>
              <AssemblyReportPage run={run} />
            </Tab>
          )}

          {showProvenance && (
            <Tab eventKey={5} title={<TabTitleText>Provenance</TabTitleText>}>
              <ProvenancePage run={run} />
            </Tab>
          )}
        </Tabs>
      </PageSection>
    </>
  );
}
