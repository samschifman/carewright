// Friendly aliases over the generated OpenAPI types. Import model types from
// here, never reach into components["schemas"] directly in feature code.
// Regenerate types with `npm run gen:api` after the contract changes.
import type { components } from "./types";

type S = components["schemas"];

export type RunStatus = S["RunStatus"];
export type RunCreated = S["RunCreated"];
export type RunSummary = S["RunSummary"];
export type RunDetail = S["RunDetail"];
export type RunError = S["RunError"];
export type RunPipelineStep = S["PipelineStep"]; // clashes with shared/ui PipelineStep
export type RunStepStatus = S["StepStatus"]; // clashes with shared/ui StepStatus
export type StepKey = S["StepKey"];
export type ReviewGate = S["ReviewGate"];
export type ReviewDecision = S["ReviewDecision"];
export type ReviewAction = S["ReviewAction"];
export type ReviewerRef = S["ReviewerRef"];
export type FeedbackItem = S["FeedbackItem"];

export type PatientSummary = S["PatientSummary"];
export type CodedItem = S["CodedItem"];

export type PlanGoal = S["PlanGoal"];
export type PlanActivity = S["PlanActivity"];
export type ActivityAutomationView = S["ActivityAutomationView"];
export type PlanConflict = S["PlanConflict"];
export type ConflictSource = S["ConflictSource"];
export type CarePlanView = S["CarePlanView"];
export type CarePlanSummary = S["CarePlanSummary"];
export type CarePlanDetail = S["CarePlanDetail"];

export type SystemHealth = S["SystemStatus"]; // schema clashes with the page name
export type ApiErrorBody = S["Error"];
