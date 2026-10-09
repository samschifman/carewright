// TypeScript mirrors of shared/src/cpg_contracts/ Python models.
// Keep in sync with the Pydantic sources.

// --- guidelines.ts ---

export enum GradingSystem {
  GRADE = "GRADE",
  COR_LOE = "COR-LOE",
  GRADE_COR_HYBRID = "GRADE-COR-hybrid",
  SIMPLIFIED = "simplified",
  VERB_IMPLIED = "verb-implied",
  UNGRADED = "ungraded",
}

export interface CPGMetadata {
  contract_version: string;
  cpg_id: string;
  title: string;
  version?: string;
  publication_date?: string;
  evidence_review_date?: string;
  issuing_body?: string;
  grading_system?: GradingSystem;
  scope?: string;
  supersedes?: string[];
}

// --- recommendations.ts ---

export enum RecommendationStrength {
  STRONG_FOR = "strong-for",
  CONDITIONAL_FOR = "conditional-for",
  CONSENSUS = "consensus",
  NO_RECOMMENDATION = "no-recommendation",
  CONDITIONAL_AGAINST = "conditional-against",
  STRONG_AGAINST = "strong-against",
}

export enum EvidenceQuality {
  HIGH = "high",
  MODERATE = "moderate",
  LOW = "low",
  VERY_LOW = "very-low",
  UNGRADED = "ungraded",
}

export enum RecommendationType {
  TREATMENT = "treatment",
  DIAGNOSTIC = "diagnostic",
  MONITORING = "monitoring",
  LIFESTYLE = "lifestyle",
  EDUCATIONAL = "educational",
  REFERRAL = "referral",
  SCREENING = "screening",
  CONTRAINDICATION = "contraindication",
  PROCESS = "process",
}

export enum RecommendationProvenance {
  REVIEWED = "reviewed",
  NEW_ADDED = "new-added",
  AMENDED = "amended",
  NOT_CHANGED = "not-changed",
  REMOVED = "removed",
}

export enum CrossReferenceRelationship {
  PREREQUISITE = "prerequisite",
  ALTERNATIVE = "alternative",
  CONFLICTS_WITH = "conflicts-with",
  MODIFIES = "modifies",
  RELATED = "related",
  SUPERSEDES = "supersedes",
  OTHER = "other",
}

export interface SourceLocation {
  page_start: number;
  page_end?: number;
  bbox?: number[];
  source_text?: string;
}

export type PatternFamily =
  | "schedule-and-check"
  | "wait-for-result-then-decide"
  | "remind-until-done"
  | "escalate-on-threshold"
  | "do-confirm-repeat"
  | "other";

export interface Automatability {
  tier: "A" | "B";
  rationale: string;
}

export interface ValidationRecord {
  status: "valid" | "incomplete" | "escalated";
  rungs_passed: string[];
  deferred_invariants: string[];
  kogito_checked: boolean;
  warnings: string[];
  errors: string[];
  acknowledgement?: string | null;
}

export interface AutomationParameter {
  name: string;
  type: "duration" | "integer" | "decimal" | "boolean" | "string" | "quantity";
  unit?: string | null;
  default?: unknown;
  constraints?: Record<string, unknown> | null;
  required: boolean;
  description: string;
  source?: Record<string, unknown> | null;
  reserved: boolean;
}

export interface ProcessIR {
  ir_version: "1.0";
  process: {
    id: string;
    name: string;
    description: string;
    kind: "template" | "instance";
    properties: Record<string, unknown>[];
    flowElements: Record<string, unknown>[];
    acp: Record<string, unknown>;
  };
}

export interface TemplateRef {
  template_id: string;
  version: string;
  source_cpg?: string | null;
  element_id?: string | null;
}

export interface AutomationTemplateSummary {
  id: string;
  version: string;
  name: string;
  description: string;
  source_cpg: string;
  section?: string | null;
  source_location?: SourceLocation | null;
  triggers: Record<string, unknown>[];
  linked_recommendation_ids: string[];
  linked_decision_model_ids: string[];
  parameters: AutomationParameter[];
  capabilities_used: string[];
  catalog_version: string;
  ir_version: string;
  pattern_family: PatternFamily;
  automatability: Automatability;
  validation: ValidationRecord;
  artifact_id: string;
}

export interface AutomationTemplate {
  contract_version: "1.1";
  summary: AutomationTemplateSummary;
  ir: ProcessIR;
  bpmn_xml: string;
}

export interface ParameterBinding {
  name: string;
  value: unknown;
  source: "cpg-default" | "clinician" | "plan-derived" | "reviewer" | "authored";
  unit?: string | null;
  note?: string | null;
}

export interface Evidence {
  kind: string;
  payload: unknown;
}

export interface ActivityAutomation {
  id: string;
  template_refs: TemplateRef[];
  template_snapshots: ProcessIR[];
  ir: ProcessIR;
  bpmn_xml: string;
  bindings: ParameterBinding[];
  derivation_evidence: Record<string, Evidence>;
  capabilities_used: string[];
  validation: ValidationRecord;
  enabled: boolean;
  revision: string;
  review_notes: string[];
}

export interface PublishedAutomation {
  automation_id: string;
  activity_ids: string[];
  revision: string;
  template_refs: TemplateRef[];
  task: Record<string, unknown>;
  document_reference: Record<string, unknown>;
}

export interface PublicationPayload {
  job_id: string;
  careplan_id: string;
  careplan_server_id: string;
  patient_server_id: string;
  replaces_careplan_id?: string | null;
  approved_at: string;
  reviewer: string;
  automations: PublishedAutomation[];
}

export interface CertaintyGrade {
  strength: RecommendationStrength;
  evidence_quality: EvidenceQuality;
  grading_system?: GradingSystem;
  original_grade?: string;
}

export interface CrossReference {
  target_id: string;
  relationship: CrossReferenceRelationship;
  description?: string;
}

export interface Recommendation {
  id: string;
  source_cpg: string;
  section?: string;
  title: string;
  content: string;
  recommendation_type: RecommendationType;
  certainty?: CertaintyGrade;
  scope_notes?: string;
  remarks?: string[];
  rationale?: string;
  cross_references?: CrossReference[];
  provenance?: RecommendationProvenance;
  evidence_review_date?: string;
  source_location?: SourceLocation;
  automation_template_ids?: string[] | null;
}

export interface RecommendationSummary {
  id: string;
  title: string;
  source_cpg: string;
  recommendation_type: RecommendationType;
  certainty?: CertaintyGrade;
}

export interface RecommendationBundle {
  contract_version: string;
  source_cpg: string;
  recommendations: Recommendation[];
}

// --- decisions.ts ---

export enum DecisionCategory {
  TREATMENT = "treatment",
  SCREENING = "screening",
  MONITORING = "monitoring",
  RISK_ASSESSMENT = "risk-assessment",
  DIAGNOSTIC = "diagnostic",
}

export interface DecisionVariable {
  name: string;
  type: string;
  description?: string;
  codes?: string[];
}

export interface DecisionModelSummary {
  id: string;
  name: string;
  inputs: DecisionVariable[];
  outputs: DecisionVariable[];
  deployed_at?: string;
  source_cpg?: string;
  category?: DecisionCategory;
  modifies?: string[];
  source_location?: SourceLocation;
  namespace?: string | null;
}

export interface DecisionEvaluationRequest {
  model_id: string;
  inputs: Record<string, unknown>;
}

export interface DecisionEvaluationResponse {
  model_id: string;
  outputs: Record<string, unknown>;
}
