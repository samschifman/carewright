import type { AutomationResult, DecisionResult, RecommendationResult } from '../api/types';

function normalized(values: Array<string | undefined | null>): Set<string> {
  return new Set(values.filter((value): value is string => !!value).map(value => value.trim().toLocaleLowerCase()));
}

function hasMatch(automationValues: Array<string | undefined | null>, itemValues: Array<string | undefined | null>): boolean {
  const automationIds = normalized(automationValues);
  return itemValues.some(value => value && automationIds.has(value.trim().toLocaleLowerCase()));
}

export function automationsForRecommendation(
  recommendation: RecommendationResult,
  automations: AutomationResult[],
): AutomationResult[] {
  const ids = normalized([
    recommendation.id,
    recommendation.title,
    ...(recommendation.automation_template_ids ?? []),
  ]);
  return automations.filter(automation => hasMatch([
    ...(automation.item.linked_recommendations ?? []),
    ...(automation.template_summary.linked_recommendation_ids ?? []),
    automation.template_summary.id,
  ], [...ids]));
}

export function automationsForDecision(
  decision: DecisionResult,
  automations: AutomationResult[],
): AutomationResult[] {
  const ids = normalized([
    decision.decision_model_summary.id,
    decision.decision_model_summary.name,
    decision.item.name,
  ]);
  return automations.filter(automation => hasMatch([
    ...(automation.item.linked_decisions ?? []),
    ...(automation.template_summary.linked_decision_model_ids ?? []),
  ], [...ids]));
}
