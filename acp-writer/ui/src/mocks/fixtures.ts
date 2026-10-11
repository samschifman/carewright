import type {
  CarePlanDetail,
  CarePlanSummary,
  CarePlanView,
  RunDetail,
  RunSummary,
  SystemHealth,
} from "@app/api/models";
import sliceCarePlanViewData from "./fixtures/slice-careplan.json";

const sliceCarePlanView = sliceCarePlanViewData as CarePlanView;

const patient = {
  name: "Ada Lovelace",
  birthDate: "1815-12-10",
  gender: "female",
  patientReference: "Patient/ada",
  conditions: [{ display: "Type 2 diabetes mellitus", code: "44054006", system: "http://snomed.info/sct" }],
  medications: [{ display: "Metformin 500 MG" }],
  allergies: [{ display: "Penicillin" }],
  observations: [{ display: "HbA1c 8.2%" }],
};

export const carePlanView: CarePlanView = {
  goals: [
    {
      id: "g1",
      description: "Achieve HbA1c < 7%",
      rationale: "Glycemic control per ADA 2024",
      target: "HbA1c < 7 %",
      sourceCpgId: "ada-2024",
      sourceRecommendationId: "ada-2024-rec-3",
    },
  ],
  activities: [
    {
      id: "a1",
      description: "Metformin 500mg twice daily",
      goalId: "g1",
      dose: "500mg",
      route: "oral",
      frequency: "twice daily",
      specialty: "endocrinology",
      sourceCpg: "ada-2024",
      sourceRecommendationId: "ada-2024-rec-7",
      clinicalRationale: "First-line pharmacotherapy for type 2 diabetes.",
    },
    { id: "a2", description: "HbA1c recheck in 3 months", goalId: "g1" },
  ],
  conflicts: [
    {
      id: "c1",
      category: "divergent_target",
      severity: "warning",
      status: "detected",
      confidence: "high",
      description: "Two guidelines set different blood-pressure targets.",
      sources: [
        { cpgId: "ada-2024", recommendationId: "rec-123", excerpt: "target < 140/90" },
        { cpgId: "aha-2023", recommendationId: "rec-456", excerpt: "target < 130/80" },
      ],
    },
    {
      id: "c2",
      category: "overlap",
      severity: "info",
      status: "detected",
      description: "Overlapping recommendation with hypertension CPG on renal dosing.",
      sources: [{ cpgId: "ada-2024", recommendationId: "rec-789" }],
    },
  ],
  fhirBundle: { resourceType: "Bundle", type: "transaction", entry: [] },
};

export const runRunning: RunDetail = {
  runId: "run-123",
  status: "running",
  patient,
  steps: [
    { key: "scan_patient", status: "done" },
    { key: "resolve_guidelines", status: "done" },
    { key: "execute_dmn", status: "active" },
    { key: "retrieve_recommendations", status: "pending" },
    { key: "compose_plan", status: "pending" },
    { key: "generate_bundle", status: "pending" },
    { key: "review_fhir", status: "pending" },
    { key: "review_careplan", status: "pending" },
    { key: "write_fhir", status: "pending" },
    { key: "done", status: "pending" },
  ],
  currentSteps: ["execute_dmn"],
  awaitingReview: null,
  carePlan: null,
  reviewIteration: 0,
  previousFeedback: null,
  createdAt: "2026-08-19T15:00:00Z",
  updatedAt: "2026-08-19T15:01:00Z",
};

export const runAwaitingReview: RunDetail = {
  ...runRunning,
  status: "awaiting_careplan_review",
  steps: runRunning.steps!.map((s) =>
    s.key === "review_careplan"
      ? { ...s, status: "active" }
      : s.key === "write_fhir" || s.key === "done"
        ? s
        : { ...s, status: "done" },
  ),
  currentSteps: ["review_careplan"],
  awaitingReview: "careplan",
  carePlan: carePlanView,
  reviewIteration: 0,
  previousFeedback: null,
};

export const runCompleted: RunDetail = {
  ...runRunning,
  status: "completed",
  steps: runRunning.steps!.map((s) => ({ ...s, status: "done" })),
  currentSteps: [],
  awaitingReview: null,
  carePlan: null,
  careplanId: "cp-1",
};

export const runSummaries: RunSummary[] = [
  {
    runId: "run-123",
    status: "running",
    patientName: "Ada Lovelace",
    patientReference: "Patient/ada",
    currentSteps: ["execute_dmn"],
    createdAt: "2026-08-19T15:00:00Z",
    updatedAt: "2026-08-19T15:01:00Z",
  },
  {
    runId: "run-100",
    status: "completed",
    patientName: "Alan Turing",
    patientReference: "Patient/alan",
    currentSteps: [],
    careplanId: "cp-1",
    createdAt: "2026-08-18T09:00:00Z",
    updatedAt: "2026-08-18T09:05:00Z",
  },
];

export const carePlanSummaries: CarePlanSummary[] = [
  {
    id: "cp-1",
    patientName: "Alan Turing",
    patientReference: "Patient/alan",
    status: "active",
    generatedAt: "2026-08-18T09:05:00Z",
    runId: "run-100",
  },
];

export const carePlanDetail: CarePlanDetail = {
  ...carePlanSummaries[0],
  patient,
  view: carePlanView,
};

export const sliceCarePlanDetail: CarePlanDetail = {
  ...carePlanDetail,
  id: "slice-careplan-example",
  patientName: "Ada Lovelace",
  status: "active",
  runId: "run-slice",
  view: sliceCarePlanView,
};

export const systemHealth: SystemHealth = {
  version: "0.1.0",
  decisionEngine: {
    available: true,
    modelsDeployed: 2,
    decisions: [
      { id: "hypertension-treatment-v1", name: "Hypertension Treatment", sourceCpg: "acc-aha-hbp-2017" },
      { id: "diabetes-screening-v1", name: "Diabetes Screening", sourceCpg: "ada-diabetes-2024" },
    ],
  },
  knowledgeBase: {
    available: true,
    guidelines: 2,
    recommendations: 12,
    cpgs: [
      { cpgId: "acc-aha-hbp-2017", title: "ACC/AHA Guideline for High Blood Pressure", version: "2017", issuingBody: "ACC/AHA" },
      { cpgId: "ada-diabetes-2024", title: "ADA Standards of Care in Diabetes", version: "2024", issuingBody: "American Diabetes Association" },
    ],
  },
};
