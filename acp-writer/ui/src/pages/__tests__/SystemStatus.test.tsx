import { describe, it, expect } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithRouter } from "@app/test/renderWithRouter";
import { SystemStatus } from "../SystemStatus";

describe("SystemStatus", () => {
  it("renders BFF-shaped health fields", async () => {
    renderWithRouter(<SystemStatus />);
    await waitFor(() => expect(screen.getByText("0.1.0")).toBeInTheDocument());
    expect(screen.getByText(/2 decisions deployed/)).toBeInTheDocument();
    expect(screen.getByText(/12 recommendations/)).toBeInTheDocument(); // recommendations
  });
});
