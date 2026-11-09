import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Card,
  CardBody,
  DataList,
  DataListCell,
  DataListItem,
  DataListItemCells,
  DataListItemRow,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  ExpandableSection,
  Flex,
  FlexItem,
  PageSection,
  Stack,
  StackItem,
  Title,
} from "@patternfly/react-core";
import { PipelineStepper, useAdaptivePolling } from "@cpg-to-acp/ui-shared";
import type { CodedItem, RunDetail, ReviewAction } from "@app/api/models";
import { ApiError, getRunDetail, submitReview } from "@app/services/api";
import { toPipelineSteps } from "@app/pipeline/steps";
import { ReviewPanel } from "@app/components/ReviewPanel";

export function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const [refreshKey, setRefreshKey] = useState(0);

  // fetcher carries runId + refreshKey so submitting a review restarts polling
  // (the run goes back to `running` after request_changes). See #125 fix: a new
  // fetcher identity is the intended restart trigger.
  const fetcher = useCallback(
    () => getRunDetail(runId!),
    [runId, refreshKey],
  );
  const isComplete = useCallback(
    (r: RunDetail) => r.status === "completed" || r.status === "failed" || r.status === "cancelled",
    [],
  );
  const { data: run, error } = useAdaptivePolling<RunDetail>({
    fetcher,
    isComplete,
    shouldRetry: (error) => !(error instanceof ApiError && [401, 403, 404].includes(error.status)),
    enabled: !!runId,
  });

  const steps = useMemo(() => toPipelineSteps(run?.steps ?? []), [run]);

  // Navigate to the persisted plan once the run completes.
  useEffect(() => {
    if (run?.status === "completed" && run.careplanId) {
      navigate(`/careplans/${run.careplanId}`, { replace: true });
    }
  }, [run?.status, run?.careplanId, navigate]);

  const handleReview = async (action: ReviewAction) => {
    await submitReview(runId!, action);
    // Resume polling: request_changes -> back to running; approve -> terminal.
    setRefreshKey((k) => k + 1);
  };

  const patient = run?.patient;

  if (error) {
    return <PageSection><p role="alert">{error instanceof ApiError && [403, 404].includes(error.status)
      ? "This run is not part of your current patient session."
      : error.message}</p></PageSection>;
  }

  if (!run) {
    return (
      <PageSection>
        <Title headingLevel="h1">Loading run…</Title>
      </PageSection>
    );
  }

  return (
    <PageSection>
      <Flex direction={{ default: "column" }} gap={{ default: "gapLg" }}>
        <FlexItem>
          <Title headingLevel="h1">
            Care Plan Run{patient?.name ? ` — ${patient.name}` : ""}
          </Title>
          <p>Status: {run.status}</p>
        </FlexItem>
        {patient && (
          <FlexItem>
            <PatientSummarySection patient={patient} />
          </FlexItem>
        )}
        <FlexItem>
          <Stack hasGutter>
            <StackItem>
              <Title headingLevel="h2">Pipeline</Title>
            </StackItem>
            <StackItem>
              <PipelineStepper steps={steps} />
            </StackItem>
          </Stack>
        </FlexItem>
        {run.awaitingReview === "careplan" && run.carePlan && (
          <FlexItem>
            {/* Key by round so the panel remounts fresh (idle, empty comment)
                when a request_changes loop arms the next gate — round N's
                submitted/error state can never bleed into round N+1. */}
            <ReviewPanel
              key={run.reviewIteration}
              carePlan={run.carePlan}
              reviewIteration={run.reviewIteration}
              artifactRevision={run.artifactRevision}
              previousFeedback={run.previousFeedback}
              onSubmit={handleReview}
            />
          </FlexItem>
        )}
        {run.status === "failed" && (
          <FlexItem>
            <p style={{ color: "var(--pf-t--global--color--status--danger--default)" }}>
              Run failed{run.error?.message ? `: ${run.error.message}` : "."}
            </p>
          </FlexItem>
        )}
      </Flex>
    </PageSection>
  );
}

function CodedItemList({ title, items }: { title: string; items: CodedItem[] }) {
  if (!items.length) return null;
  return (
    <StackItem>
      <strong>{title}</strong>
      <DataList aria-label={title} isCompact>
        {items.map((item, i) => (
          <DataListItem key={i}>
            <DataListItemRow>
              <DataListItemCells
                dataListCells={[
                  <DataListCell key="display">{item.display}</DataListCell>,
                  item.code ? <DataListCell key="code">{item.code}</DataListCell> : null,
                ].filter(Boolean)}
              />
            </DataListItemRow>
          </DataListItem>
        ))}
      </DataList>
    </StackItem>
  );
}

function PatientSummarySection({ patient }: { patient: NonNullable<RunDetail["patient"]> }) {
  const [expanded, setExpanded] = useState(true);
  return (
    <ExpandableSection
      toggleText={expanded ? "Hide patient summary" : "Show patient summary"}
      isExpanded={expanded}
      onToggle={(_e, v) => setExpanded(v)}
    >
      <Card>
        <CardBody>
          <Stack hasGutter>
            <StackItem>
              <DescriptionList isHorizontal>
                <DescriptionListGroup>
                  <DescriptionListTerm>Name</DescriptionListTerm>
                  <DescriptionListDescription>{patient.name || "Unknown"}</DescriptionListDescription>
                </DescriptionListGroup>
                {patient.birthDate && (
                  <DescriptionListGroup>
                    <DescriptionListTerm>Date of Birth</DescriptionListTerm>
                    <DescriptionListDescription>{patient.birthDate}</DescriptionListDescription>
                  </DescriptionListGroup>
                )}
                {patient.gender && (
                  <DescriptionListGroup>
                    <DescriptionListTerm>Gender</DescriptionListTerm>
                    <DescriptionListDescription>{patient.gender}</DescriptionListDescription>
                  </DescriptionListGroup>
                )}
              </DescriptionList>
            </StackItem>
            <CodedItemList title="Conditions" items={patient.conditions ?? []} />
            <CodedItemList title="Medications" items={patient.medications ?? []} />
            <CodedItemList title="Allergies" items={patient.allergies ?? []} />
            <CodedItemList title="Observations" items={patient.observations ?? []} />
          </Stack>
        </CardBody>
      </Card>
    </ExpandableSection>
  );
}
