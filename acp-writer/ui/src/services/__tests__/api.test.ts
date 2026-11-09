import { http, HttpResponse } from "msw";
import { server } from "@app/mocks/server";
import { describe, it, expect } from "vitest";
import {
  createRun,
  listRuns,
  getRunDetail,
  submitReview,
  listCarePlans,
  getCarePlan,
  getSystemStatus,
} from "../api";

describe("api service", () => {
  it("createRun posts the ips bundle and returns runId", async () => {
    const res = await createRun({ resourceType: "Bundle" });
    expect(res.runId).toBe("run-123");
    expect(res.status).toBe("running");
  });

  it("listRuns returns summaries", async () => {
    const rows = await listRuns();
    expect(rows).toHaveLength(2);
    expect(rows[0].patientName).toBe("Ada Lovelace");
  });

  it("getRunDetail returns the run", async () => {
    const run = await getRunDetail("run-123");
    expect(run.status).toBe("running");
    expect(run.steps?.length).toBe(10);
  });

  it("submitReview approve returns a completed run", async () => {
    const run = await submitReview("run-review", { decision: "approve", reviewRound: 0, artifactRevision: "a".repeat(64) });
    expect(run.status).toBe("completed");
  });

  it("listCarePlans and getCarePlan return view-models", async () => {
    expect((await listCarePlans())[0].patientName).toBe("Alan Turing");
    expect((await getCarePlan("cp-1")).view?.goals?.[0].id).toBe("g1");
  });

  it("getSystemStatus returns health", async () => {
    expect((await getSystemStatus()).decisionEngine?.available).toBe(true);
  });
  it.each([
    ["out-of-scope", "This run is not part of your current patient session."],
    ["csrf", "Your session could not validate this request. Reload the page and try again."],
    ["approval-stale", "This review has changed. Reload it before submitting again."],
    ["session-required", "Launch the application from your EHR to start a session."],
  ])("renders %s errors readably", async (code, message) => {
    server.use(http.get("/api/v1/runs/forbidden", () => HttpResponse.json({detail:{error:code}}, {status:403})));
    await expect(getRunDetail("forbidden")).rejects.toThrow(message);
  });

});
