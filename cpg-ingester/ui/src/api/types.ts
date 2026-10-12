export type RunStatus =
  | 'parsing'
  | 'analyzing'
  | 'awaiting_manifest_review'
  | 'generating'
  | 'awaiting_artifact_review'
  | 'assembling'
  | 'delivering'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface RunSummary {
  id: string;
  status: RunStatus;
  cpgName: string;
  createdAt: string;
  currentStep: string;
}

export interface PipelineStep {
  name: string;
  status: 'pending' | 'active' | 'completed' | 'failed' | 'cancelled';
  startedAt?: string;
  completedAt?: string;
  iteration?: number;
}

export interface CPGMetadata {
  cpg_id: string;
  title: string;
  version?: string;
  effective_date?: string;
  issuing_body?: string;
  grading_system?: string;
  scope?: string;
}

export interface SectionMapEntry {
  heading: string;
  page_start?: number;
  page_end?: number;
  classification: string;
}

export interface DecisionVariable {
  name: string;
  type: string;
}

export interface DecisionResult {
  artifact_id?: string;
  dmn_xml: string;
  validation_warnings?: string[];
  escalated?: boolean;
  escalation_reason?: string;
  escalation_errors?: string[];
  item: {
    name: string;
    type: string;
    category?: string;
    section?: string;
    tier?: string;
  };
  decision_model_summary: {
    id: string;
    name: string;
    inputs: DecisionVariable[];
    outputs: DecisionVariable[];
    category?: string;
  };
}

export interface AutomationResult {
  artifact_id?: string;
  bpmn_xml: string;
  process_ir: Record<string, unknown>;
  template_summary: {
    id: string;
    version: string;
    name: string;
    description?: string;
    source_cpg?: string;
    section?: string;
    triggers: Array<Record<string, unknown>>;
    linked_recommendation_ids: string[];
    linked_decision_model_ids: string[];
    capabilities_used: string[];
    pattern_family: string;
    automatability?: {
      tier: 'A' | 'B';
      rationale: string;
    };
    validation?: {
      status: 'valid' | 'incomplete' | 'escalated';
      rungs_passed?: string[];
      deferred_invariants?: string[];
      kogito_checked?: boolean;
      warnings?: string[];
      errors?: string[];
    };
    [key: string]: unknown;
  };
  item: {
    id?: string;
    type?: string;
    name: string;
    section?: string;
    linked_recommendations?: string[];
    linked_decisions?: string[];
  };
  escalated?: boolean;
  escalation_reason?: string;
  escalation_errors?: string[];
}

export interface RecommendationResult {
  artifact_id?: string;
  id: string;
  source_cpg: string;
  escalated?: boolean;
  escalation_reason?: string;
  escalation_errors?: string[];
  title: string;
  content: string;
  recommendation_type: string;
  section?: string;
  certainty?: {
    strength: string;
    evidence_quality?: string;
    grading_system?: string;
    original_grade?: string;
  };
  rationale?: string;
  scope_notes?: string;
  remarks?: string[];
  cross_references?: Array<{
    target_id: string;
    target_type: string;
    relationship: string;
  }>;
  source_location?: {
    page_start?: number;
    page_end?: number;
    source_text?: string;
  };
}

export interface AssemblyReport {
  cpg_id: string;
  recommendations_count: number;
  dmn_models_count: number;
  automation_templates_count?: number;
  escalated_count: number;
  integrity_errors: string[];
}

export interface EscalatedItem {
  id?: string;
  name: string;
  type: string;
  section?: string;
  escalation_reason?: string;
  escalation_errors?: string[];
}

export interface PublishedArtifact {
  type: 'metadata' | 'dmn' | 'bpmn' | 'recommendations' | 'assembly_report' | 'escalated_items';
  ref: string;
  artifact_id?: string;
  name?: string;
  template_id?: string;
  version?: string;
  cpg_id?: string;
  count?: number;
}

export interface DeliveryStatus {
  published: boolean;
  cpg_id: string;
  artifact_location?: string;
  artifacts: PublishedArtifact[];
  errors: string[];
  escalated_items_count?: number;
  reason?: string;
}

export interface ReviewFeedbackItem {
  itemId: string;
  itemType: 'decision' | 'recommendation' | 'classification' | 'process' | 'automation';
  comment: string;
}

export interface ReviewAction {
  action: 'approve' | 'request_changes';
  feedback?: ReviewFeedbackItem[];
  overallComment?: string;
}

export interface StepError {
  step: string;
  message: string;
}

export interface RunWarning {
  type: string;
  message: string;
}

export interface RunDetail {
  id: string;
  status: RunStatus;
  cpgName: string;
  createdAt: string;
  steps: PipelineStep[];
  awaitingReview?: 'manifest' | 'pre-delivery';
  reviewIteration?: number;
  previousFeedback?: ReviewAction;
  metadata?: CPGMetadata;
  sectionMap?: SectionMapEntry[];
  manifest?: ManifestItem[];
  decisions?: DecisionResult[];
  automations?: AutomationResult[];
  recommendations?: RecommendationResult[];
  recommendationEscalations?: EscalatedItem[];
  assemblyReport?: AssemblyReport;
  deliveryStatus?: DeliveryStatus;
  escalatedItems?: EscalatedItem[];
  errors?: StepError[];
  warnings?: RunWarning[];
}

export interface ManifestItem {
  id: string;
  type: 'decision' | 'recommendation' | 'process' | string;
  name: string;
  section?: string;
  tier?: 'A' | 'B' | string;
  tier_hint?: 'A' | 'B' | 'C' | string;
  automatability?: { tier?: string };
  trigger_hint?: string;
  linked_recommendations?: string[];
  linked_decisions?: string[];
  [key: string]: unknown;
}
