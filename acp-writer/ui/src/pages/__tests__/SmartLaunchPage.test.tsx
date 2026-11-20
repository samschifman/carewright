import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { SmartLaunchPage, launchNavigation } from "../SmartLaunchPage";

describe("SMART launch forwarding", () => {
  it("forwards EHR launch parameters to the BFF", () => {
    const replace = vi.spyOn(launchNavigation, "replace").mockImplementation(() => {});
    render(<MemoryRouter initialEntries={["/launch?iss=https%3A%2F%2Fehr.example%2Ffhir%2FR4%2F&launch=synthetic"]}><SmartLaunchPage /></MemoryRouter>);
    expect(replace).toHaveBeenCalledWith("/api/v1/launch?iss=https%3A%2F%2Fehr.example%2Ffhir%2FR4%2F&launch=synthetic");
    replace.mockRestore();
  });
  it("shows instructions without fetching EHR data", () => {
    render(<MemoryRouter><SmartLaunchPage /></MemoryRouter>);
    expect(screen.getByText("Launch from your EHR")).toBeInTheDocument();
  });
});
