import { useState } from "react";
import {
  ExpandableSection,
  Flex,
  FlexItem,
  Label,
  Stack,
  StackItem,
  Switch,
  Tooltip,
} from "@patternfly/react-core";
import { BpmnViewer } from "@cpg-to-acp/ui-shared";
import type { ActivityAutomationView } from "@app/api/models";

interface AutomationSectionProps {
  automations: ActivityAutomationView[];
}

function statusFor(automation: ActivityAutomationView, enabled: boolean) {
  if (automation.validation.status === "escalated") return "Escalated";
  if (automation.validation.status === "incomplete") return "Incomplete";
  return enabled ? "Valid" : "Disabled";
}

function statusColor(status: string): "green" | "orange" | "red" | "grey" {
  if (status === "Valid") return "green";
  if (status === "Incomplete") return "orange";
  if (status === "Escalated") return "red";
  return "grey";
}

export function AutomationSection({ automations }: AutomationSectionProps) {
  const [expandedXml, setExpandedXml] = useState<Record<string, boolean>>({});
  if (!automations.length) return null;

  return (
    <ExpandableSection toggleText={`Automations (${automations.length})`} isIndented>
      <Stack hasGutter>
        {automations.map((automation) => {
          const enabled = automation.enabled;
          const status = statusFor(automation, enabled);
          return (
            <StackItem key={automation.id}>
              <section aria-label={`Automation ${automation.id}`}>
                <Flex alignItems={{ default: "alignItemsCenter" }} gap={{ default: "gapSm" }}>
                  <FlexItem><strong>{automation.templateRefs[0]?.templateId ?? "Automation"}</strong></FlexItem>
                  <FlexItem><Label color={statusColor(status)}>{status}</Label></FlexItem>
                  {automation.activityIds && automation.activityIds.length > 1 && (
                    <FlexItem><Label color="blue">Shared across {automation.activityIds.length} activities</Label></FlexItem>
                  )}
                </Flex>
                <Flex gap={{ default: "gapSm" }} style={{ marginBlock: "0.5rem" }}>
                  {automation.templateRefs.map((ref) => (
                    <FlexItem key={`${ref.templateId}@${ref.version}`}>
                      <Label color="purple">
                        {ref.templateId} · {ref.version}
                        {ref.sourceCpg ? ` · ${ref.sourceCpg}` : ""}
                      </Label>
                    </FlexItem>
                  ))}
                </Flex>
                <Tooltip content="Automation enable controls will be available in a later review update.">
                  <span>
                    <Switch
                      id={`automation-enabled-${automation.id}`}
                      aria-label={`Enable automation ${automation.id}`}
                      label="Enabled"
                      isChecked={enabled}
                      isDisabled
                    />
                  </span>
                </Tooltip>
                {automation.bindings.length > 0 && (
                  <dl>
                    {automation.bindings.map((binding) => (
                      <div key={binding.name}>
                        <dt>{binding.name}</dt>
                        <dd>
                          {String(binding.value)}{binding.unit ? ` ${binding.unit}` : ""}
                          {` · ${binding.source}`}
                        </dd>
                      </div>
                    ))}
                  </dl>
                )}
                {automation.validation.warnings?.map((warning) => (
                  <p key={warning}><Label color="orange">Warning</Label> {warning}</p>
                ))}
                <BpmnViewer
                  xml={automation.bpmnXml}
                  title={`${automation.templateRefs[0]?.templateId ?? "Automation"} BPMN`}
                />
                <ExpandableSection
                  toggleText="View BPMN XML"
                  isExpanded={expandedXml[automation.id] ?? false}
                  onToggle={(_event, expanded) =>
                    setExpandedXml((current) => ({ ...current, [automation.id]: expanded }))
                  }
                >
                  <pre style={{ maxHeight: 240, overflow: "auto", whiteSpace: "pre-wrap" }}>
                    {automation.bpmnXml}
                  </pre>
                </ExpandableSection>
              </section>
            </StackItem>
          );
        })}
      </Stack>
    </ExpandableSection>
  );
}
