import {
  Card,
  CardBody,
  CardTitle,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  ExpandableSection,
} from "@patternfly/react-core";
import { RunningIcon } from "@patternfly/react-icons";
import type { PlanActivity } from "@app/api/models";
import { AutomationSection } from "./AutomationSection";
import { provenanceListStyle } from "./provenanceStyle";

interface ActivityCardProps {
  activity: PlanActivity;
}

export function ActivityCard({ activity }: ActivityCardProps) {
  const dosing = [activity.dose, activity.route, activity.frequency]
    .filter(Boolean)
    .join(" · ");
  const hasProvenance =
    dosing ||
    activity.specialty ||
    activity.sourceCpg ||
    activity.sourceRecommendationId ||
    activity.clinicalRationale;
  const automations = activity.automations ?? [];

  return (
    <Card isCompact>
      <CardTitle>
        <span style={{ marginRight: "0.5rem" }}>
          <RunningIcon />
        </span>
        {activity.description}
      </CardTitle>
      {hasProvenance || automations.length > 0 ? (
        <CardBody>
          {!hasProvenance && activity.detail && <p>{activity.detail}</p>}
          {hasProvenance && <ExpandableSection toggleText="Provenance" isIndented>
            <DescriptionList isHorizontal isCompact style={provenanceListStyle}>
              {dosing && (
                <DescriptionListGroup>
                  <DescriptionListTerm>Dosing</DescriptionListTerm>
                  <DescriptionListDescription>{dosing}</DescriptionListDescription>
                </DescriptionListGroup>
              )}
              {activity.specialty && (
                <DescriptionListGroup>
                  <DescriptionListTerm>Specialty</DescriptionListTerm>
                  <DescriptionListDescription>{activity.specialty}</DescriptionListDescription>
                </DescriptionListGroup>
              )}
              {activity.sourceCpg && (
                <DescriptionListGroup>
                  <DescriptionListTerm>Source guideline</DescriptionListTerm>
                  <DescriptionListDescription>{activity.sourceCpg}</DescriptionListDescription>
                </DescriptionListGroup>
              )}
              {activity.sourceRecommendationId && (
                <DescriptionListGroup>
                  <DescriptionListTerm>Source recommendation</DescriptionListTerm>
                  <DescriptionListDescription>{activity.sourceRecommendationId}</DescriptionListDescription>
                </DescriptionListGroup>
              )}
              {activity.clinicalRationale && (
                <DescriptionListGroup>
                  <DescriptionListTerm>Clinical rationale</DescriptionListTerm>
                  <DescriptionListDescription>{activity.clinicalRationale}</DescriptionListDescription>
                </DescriptionListGroup>
              )}
            </DescriptionList>
          </ExpandableSection>}
          {automations.length > 0 && (
            <AutomationSection
              automations={automations}
            />
          )}
        </CardBody>
      ) : (
        activity.detail && <CardBody>{activity.detail}</CardBody>
      )}
    </Card>
  );
}
