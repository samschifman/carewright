import { http, HttpResponse } from "msw";
import type { ReviewAction } from "@app/api/models";
import {
  carePlanDetail,
  carePlanSummaries,
  runAwaitingReview,
  runCompleted,
  runRunning,
  runSummaries,
  sliceCarePlanDetail,
  systemHealth,
} from "./fixtures";

const B = "/api/v1";
const isSliceScenario = import.meta.env.VITE_MOCK_SCENARIO === "slice";

export const handlers = [
  http.get(`${B}/runs`, () => HttpResponse.json(
    isSliceScenario
      ? [{
          runId: "run-slice",
          status: "awaiting_careplan_review",
          patientName: "Ada Lovelace",
          patientReference: "Patient/example",
          currentSteps: ["review_careplan"],
          careplanId: sliceCarePlanDetail.id,
          createdAt: "2026-10-09T12:00:00Z",
        }, ...runSummaries]
      : runSummaries,
  )),

  http.post(`${B}/runs`, async () =>
    HttpResponse.json({ runId: "run-123", status: "running" }, { status: 202 }),
  ),

  http.get(`${B}/runs/:runId`, ({ params }) => {
    if (isSliceScenario && params.runId === "run-slice") {
      return HttpResponse.json({
        ...runAwaitingReview,
        runId: "run-slice",
        careplanId: sliceCarePlanDetail.id,
        carePlan: sliceCarePlanDetail.view,
      });
    }
    if (params.runId === "run-review") return HttpResponse.json(runAwaitingReview);
    if (params.runId === "run-done") return HttpResponse.json(runCompleted);
    return HttpResponse.json(runRunning);
  }),

  http.delete(`${B}/runs/:runId`, () => new HttpResponse(null, { status: 204 })),

  http.post(`${B}/runs/:runId/review/careplan`, async ({ request }) => {
    const body = (await request.json()) as ReviewAction;
    // approve -> completed; request_changes -> back to running
    return HttpResponse.json(
      body.decision === "approve" ? runCompleted : runRunning,
      { status: 202 },
    );
  }),

  http.get(`${B}/careplans`, () => HttpResponse.json(
    isSliceScenario
      ? [{
          id: sliceCarePlanDetail.id,
          patientName: sliceCarePlanDetail.patientName,
          patientReference: sliceCarePlanDetail.patientReference,
          status: sliceCarePlanDetail.status,
          generatedAt: sliceCarePlanDetail.generatedAt,
          runId: sliceCarePlanDetail.runId,
        }, ...carePlanSummaries]
      : carePlanSummaries,
  )),
  http.get(`${B}/careplans/:id`, ({ params }) => HttpResponse.json(
    params.id === sliceCarePlanDetail.id ? sliceCarePlanDetail : carePlanDetail,
  )),
  http.get(`${B}/status`, () => HttpResponse.json(systemHealth)),
];
