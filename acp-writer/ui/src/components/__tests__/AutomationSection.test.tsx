import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ActivityAutomationView } from "@app/api/models";
import { AutomationSection } from "../AutomationSection";

vi.mock("@cpg-to-acp/ui-shared", () => ({
  BpmnViewer: ({ xml, title }: { xml: string; title: string }) => (
    <div data-testid="bpmn-viewer" aria-label={title}>{xml}</div>
  ),
}));

const automation = (overrides: Partial<ActivityAutomationView> = {}): ActivityAutomationView => ({
  id: "automation-1",
  templateRefs: [{ templateId: "home-bp-monitoring", version: "a".repeat(12), sourceCpg: "SYN-HTN-2026-002" }],
  activityIds: ["activity-1"],
  enabled: true,
  revision: "b".repeat(12),
  bpmnXml: "<bpmn:definitions />",
  capabilitiesUsed: ["patient.request_observation"],
  bindings: [{ name: "reporting_interval", value: "P1W", source: "cpg-default" }],
  validation: { status: "valid", rungsPassed: ["L0", "L1"], warnings: [], errors: [] },
  reviewNotes: [],
  ...overrides,
});

describe("AutomationSection", () => {
  it("shows status, source template, viewer, bindings, and expandable XML", () => {
    render(<AutomationSection automations={[automation()]} />);
    fireEvent.click(screen.getByRole("button", { name: /automations \(1\)/i }));
    expect(screen.getByText("Valid")).toBeInTheDocument();
    expect(screen.getByText(/home-bp-monitoring.*SYN-HTN-2026-002/)).toBeInTheDocument();
    expect(screen.getByText("reporting_interval")).toBeInTheDocument();
    expect(screen.getByTestId("bpmn-viewer")).toHaveTextContent("<bpmn:definitions />");
    expect(screen.getByRole("switch", { name: /enable automation/i })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /view bpmn xml/i }));
    expect(screen.getAllByText("<bpmn:definitions />")).toHaveLength(2);
  });

  it("locks escalated automations and marks them escalated", () => {
    render(
      <AutomationSection
        automations={[automation({
          enabled: false,
          validation: { status: "escalated", rungsPassed: [], warnings: ["Review needed"], errors: ["L7"] },
        })]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /automations \(1\)/i }));
    expect(screen.getByText("Escalated")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /enable automation/i })).toBeDisabled();
    expect(screen.getByText("Review needed")).toBeInTheDocument();
  });

  it("shows a policy badge for operational defaults", () => {
    render(<AutomationSection automations={[automation({
      bindings: [{ name: "max_duration", value: "P90D", source: "policy" }],
    })]} />);
    fireEvent.click(screen.getByRole("button", { name: /automations \(1\)/i }));

    expect(screen.getByText("Policy")).toBeInTheDocument();
    expect(screen.getByText(/max_duration/)).toBeInTheDocument();
  });
});
