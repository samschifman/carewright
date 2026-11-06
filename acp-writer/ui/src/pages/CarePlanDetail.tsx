import { useCallback, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Button,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Label,
  PageSection,
  Stack,
  StackItem,
  Tab,
  Tabs,
  TabTitleText,
  Title,
} from "@patternfly/react-core";
import { useAdaptivePolling } from "@cpg-to-acp/ui-shared";
import type { CarePlanDetail as CarePlanDetailModel } from "@app/api/models";
import { ApiError, getCarePlan } from "@app/services/api";
import { GoalCard } from "@app/components/GoalCard";
import { ActivityCard } from "@app/components/ActivityCard";
import { ConflictAlert, conflictCountLabel, orderConflicts } from "@app/components/ConflictAlert";
import { FhirJsonViewer } from "@app/components/FhirJsonViewer";

const statusColor: Record<string, "blue" | "green" | "red"> = {
  draft: "blue",
  active: "green",
  "entered-in-error": "red",
};

function formatDate(iso?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

export function CarePlanDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState(0);
  const fetcher = useCallback(() => getCarePlan(id!), [id]);
  const { data: plan, error } = useAdaptivePolling<CarePlanDetailModel>({
    fetcher,
    isComplete: () => true,
    shouldRetry: (error) => !(error instanceof ApiError && [401, 403, 404].includes(error.status)),
    enabled: !!id,
  });

  if (error) {
    return <PageSection><p role="alert">{error instanceof ApiError && [403, 404].includes(error.status)
      ? "This care plan is not part of your current patient session."
      : error.message}</p></PageSection>;
  }

  if (!plan) {
    return (
      <PageSection>
        <Title headingLevel="h1">Loading care plan…</Title>
      </PageSection>
    );
  }

  const view = plan.view ?? {};
  const goals = view.goals ?? [];
  const activities = view.activities ?? [];
  const conflicts = view.conflicts ?? [];
  const patient = plan.patient;
  const patientName = patient?.name ?? plan.patientName ?? "Unknown patient";

  return (
    <>
      <PageSection>
        <Title headingLevel="h1">Care Plan — {patientName}</Title>
        <DescriptionList isHorizontal isCompact style={{ marginTop: "0.5rem" }}>
          <DescriptionListGroup>
            <DescriptionListTerm>Care Plan ID</DescriptionListTerm>
            <DescriptionListDescription>{plan.id}</DescriptionListDescription>
          </DescriptionListGroup>
          <DescriptionListGroup>
            <DescriptionListTerm>Status</DescriptionListTerm>
            <DescriptionListDescription>
              <Label color={statusColor[plan.status] ?? "blue"}>{plan.status}</Label>
            </DescriptionListDescription>
          </DescriptionListGroup>
          {plan.generatedAt && (
            <DescriptionListGroup>
              <DescriptionListTerm>Generated</DescriptionListTerm>
              <DescriptionListDescription>{formatDate(plan.generatedAt)}</DescriptionListDescription>
            </DescriptionListGroup>
          )}
          <DescriptionListGroup>
            <DescriptionListTerm>Patient</DescriptionListTerm>
            <DescriptionListDescription>
              {patientName}
              {patient?.birthDate ? ` (DOB: ${patient.birthDate})` : ""}
              {patient?.gender ? ` · ${patient.gender}` : ""}
            </DescriptionListDescription>
          </DescriptionListGroup>
          {plan.runId && (
            <DescriptionListGroup>
              <DescriptionListTerm>Source Run</DescriptionListTerm>
              <DescriptionListDescription>
                <Button variant="link" isInline onClick={() => navigate(`/runs/${plan.runId}`)}>
                  {plan.runId}
                </Button>
              </DescriptionListDescription>
            </DescriptionListGroup>
          )}
        </DescriptionList>
      </PageSection>
      <PageSection isFilled>
        <Tabs activeKey={activeTab} onSelect={(_e, key) => setActiveTab(key as number)}>
          <Tab eventKey={0} title={<TabTitleText>Goals ({goals.length})</TabTitleText>}>
            <Stack hasGutter style={{ paddingTop: "1rem" }}>
              {goals.length === 0 ? (
                <StackItem><p>No goals defined.</p></StackItem>
              ) : (
                goals.map((g) => <StackItem key={g.id}><GoalCard goal={g} /></StackItem>)
              )}
            </Stack>
          </Tab>
          <Tab eventKey={1} title={<TabTitleText>Activities ({activities.length})</TabTitleText>}>
            <Stack hasGutter style={{ paddingTop: "1rem" }}>
              {activities.length === 0 ? (
                <StackItem><p>No activities defined.</p></StackItem>
              ) : (
                activities.map((a) => <StackItem key={a.id}><ActivityCard activity={a} /></StackItem>)
              )}
            </Stack>
          </Tab>
          <Tab eventKey={2} title={<TabTitleText>{conflictCountLabel(conflicts)}</TabTitleText>}>
            <Stack hasGutter style={{ paddingTop: "1rem" }}>
              {conflicts.length === 0 ? (
                <StackItem><p>No conflicts detected.</p></StackItem>
              ) : (
                orderConflicts(conflicts).map((c) => <StackItem key={c.id}><ConflictAlert conflict={c} /></StackItem>)
              )}
            </Stack>
          </Tab>
        </Tabs>
        {view.fhirBundle && (
          <div style={{ marginTop: "1rem" }}>
            <FhirJsonViewer json={view.fhirBundle} title="View FHIR Bundle JSON" />
          </div>
        )}
      </PageSection>
    </>
  );
}
