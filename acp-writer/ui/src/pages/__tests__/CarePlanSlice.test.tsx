import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithRouter } from "@app/test/renderWithRouter";
import { sliceCarePlanDetail } from "@app/mocks/fixtures";
import { CarePlanDetail } from "../CarePlanDetail";

vi.mock("@cpg-to-acp/ui-shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@cpg-to-acp/ui-shared")>();
  return {
    ...actual,
    BpmnViewer: ({ title }: { title: string }) => <div aria-label={title}>Mock BPMN viewer</div>,
  };
});

describe("slice care-plan fixture", () => {
  it("renders the persisted automation inside its activity", async () => {
    renderWithRouter(<CarePlanDetail />, {
      routePath: "/careplans/:id",
      initialPath: "/careplans/slice-careplan-example",
    });
    expect(await screen.findByRole("heading", { level: 1, name: "Care Plan — Ada Lovelace" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", {
      name: `Activities (${sliceCarePlanDetail.view.activities.length})`,
    }));
    fireEvent.click(screen.getByRole("button", { name: /automations \(1\)/i }));
    expect(screen.getByText("home-bp-monitoring")).toBeInTheDocument();
    expect(screen.getAllByText("SYN-HTN-2026-002").length).toBeGreaterThan(0);
    expect(screen.getByRole("switch", { name: /enable automation/i })).toBeDisabled();
    expect(screen.getByLabelText("home-bp-monitoring BPMN")).toBeInTheDocument();
  });
});
