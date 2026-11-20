import { http, HttpResponse } from "msw";
import { server } from "@app/mocks/server";
import { describe, it, expect } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { SessionProvider } from "@app/SessionProvider";
import { AppRoutes } from "@app/routes";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SessionProvider><AppRoutes /></SessionProvider>
    </MemoryRouter>,
  );
}

describe("AppRoutes", () => {
  it("routes /runs to the run list", async () => {
    renderAt("/runs");
    await waitFor(() => expect(screen.getByText("Runs")).toBeInTheDocument());
  });
  it("routes /careplans to the care plan list", async () => {
    renderAt("/careplans");
    await waitFor(() => expect(screen.getByText("Care Plans")).toBeInTheDocument());
  });
  it("routes /runs/:id to the run detail page", async () => {
    renderAt("/runs/run-123");
    await waitFor(() => expect(screen.getByText(/Care Plan Run/)).toBeInTheDocument());
  });
  it.each([403, 404])("surfaces run scope errors (%s)", async (status) => {
    server.use(http.get("/api/v1/runs/forbidden", () => HttpResponse.json({detail:{error:"out-of-scope"}}, {status})));
    renderAt("/runs/forbidden");
    expect(await screen.findByRole("alert")).toHaveTextContent("This run is not part of your current patient session.");
    expect(screen.queryByText("Loading run…")).not.toBeInTheDocument();
  });

  it.each([403, 404])("surfaces care-plan scope errors (%s)", async (status) => {
    server.use(http.get("/api/v1/careplans/forbidden", () => HttpResponse.json({detail:{error:"out-of-scope"}}, {status})));
    renderAt("/careplans/forbidden");
    expect(await screen.findByRole("alert")).toHaveTextContent("This care plan is not part of your current patient session.");
    expect(screen.queryByText("Loading care plan…")).not.toBeInTheDocument();
  });

});
