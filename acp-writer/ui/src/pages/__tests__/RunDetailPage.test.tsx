import { describe, it, expect } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithRouter } from "@app/test/renderWithRouter";
import { RunDetailPage } from "../RunDetailPage";

describe("RunDetailPage", () => {
  it("renders the server-authored stepper and patient while running", async () => {
    renderWithRouter(<RunDetailPage />, {
      routePath: "/runs/:runId",
      initialPath: "/runs/run-123",
    });
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Care Plan Run — Ada Lovelace",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("DMN decisions evaluated")).toBeInTheDocument();
    // running -> no review panel yet
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
  });

  it("shows the review panel when awaiting care-plan review", async () => {
    renderWithRouter(<RunDetailPage />, {
      routePath: "/runs/:runId",
      initialPath: "/runs/run-review",
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument(),
    );
    expect(screen.getByText("Achieve HbA1c < 7%")).toBeInTheDocument();
  });
});
