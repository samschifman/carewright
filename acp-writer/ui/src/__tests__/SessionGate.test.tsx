import userEvent from "@testing-library/user-event";
import { createRun, clearSession } from "@app/services/api";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, useNavigate, useLocation } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { SessionProvider, SessionGate } from "@app/SessionProvider";
import { AppRoutes } from "@app/routes";
import { server } from "@app/mocks/server";

function mount(path = "/runs") {
  render(<MemoryRouter initialEntries={[path]}><SessionProvider><AppRoutes /></SessionProvider></MemoryRouter>);
}

describe("SessionGate", () => {
  it("shows launch instructions when the session is missing", async () => {
    server.use(http.get("/api/v1/session", () => HttpResponse.json({ error: "session-required" }, { status: 401 })));
    mount();
    expect(await screen.findByText("Launch from your EHR")).toBeInTheDocument();
    expect(screen.queryByText("Runs")).not.toBeInTheDocument();
  });
  it("shows runs with an authenticated session", async () => {
    mount();
    expect(await screen.findByText("Runs")).toBeInTheDocument();
  });
  it("keeps the explicit dev entry point available without a session", async () => {
    server.use(http.get("/api/v1/session", () => HttpResponse.json({ directIpsEnabled: true }, { status: 401 })));
    mount("/");
    expect(await screen.findByText(/Care Plan Generator/i)).toBeInTheDocument();
  });
  it("shows a service failure without treating it as logout", async () => {
    server.use(http.get("/api/v1/session", () => HttpResponse.json({ error: "session-store-unavailable" }, { status: 503 })));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("Session service is unavailable");
  });
});


it("enters the protected run after the development upload creates its session", async () => {
  let created = false;
  clearSession();
  server.use(
    http.get("/api/v1/session", () => created
      ? HttpResponse.json({ kind: "patient", subject: "synthetic", runIds: ["run-123"], csrfToken: "synthetic", expiresAt: 9999999999, directIpsEnabled: true })
      : HttpResponse.json({ directIpsEnabled: true }, { status: 401 })),
    http.post("/api/v1/runs", () => { created = true; return HttpResponse.json({ runId: "run-123", status: "starting" }); }),
  );
  function Entry() {
    const navigate = useNavigate();
    const { pathname } = useLocation();
    return <SessionGate>{pathname === "/"
      ? <button onClick={() => void createRun({ resourceType: "Bundle" }).then(run => navigate("/runs/" + run.runId))}>Start development run</button>
      : <p>Protected patient run</p>}</SessionGate>;
  }
  render(<MemoryRouter><SessionProvider><Entry /></SessionProvider></MemoryRouter>);
  await userEvent.click(await screen.findByRole("button", { name: "Start development run" }));
  expect(await screen.findByText("Protected patient run")).toBeInTheDocument();
});
