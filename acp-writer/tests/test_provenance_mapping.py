"""Unit tests for the review-gate provenance mappers (#128).

These map planning-brief snake_case goals/activities → BFF camelCase, mirroring
``plan_conflict_from_entry``.
"""

from acp_writer.services.artifact_resolver import (
    _format_brief_goal_target,
)


class TestFormatBriefGoalTarget:
    def test_measure_with_upper_bound(self):
        s = _format_brief_goal_target(
            {"display": "HbA1c", "code": "4548-4"}, {"high": 7, "unit": "%"}
        )
        assert s == "HbA1c < 7 %"

    def test_measure_with_lower_bound(self):
        s = _format_brief_goal_target({"display": "Steps"}, {"low": 8000, "unit": "count"})
        assert s == "Steps > 8000 count"

    def test_measure_with_range(self):
        s = _format_brief_goal_target(
            {"display": "SBP"}, {"low": 110, "high": 130, "unit": "mmHg"}
        )
        assert s == "SBP: 110–130 mmHg"

    def test_falls_back_to_code_when_no_display(self):
        s = _format_brief_goal_target({"code": "4548-4"}, {"high": 7, "unit": "%"})
        assert s == "4548-4 < 7 %"

    def test_measure_only(self):
        assert _format_brief_goal_target({"display": "HbA1c"}, None) == "HbA1c"

    def test_empty(self):
        assert _format_brief_goal_target(None, None) == ""
        assert _format_brief_goal_target({}, {}) == ""

    def test_strips_trailing_zero_on_float_bounds(self):
        # TargetValue bounds are floats, so 7 round-trips through JSON as 7.0.
        assert (
            _format_brief_goal_target({"display": "HbA1c"}, {"high": 7.0, "unit": "%"})
            == "HbA1c < 7 %"
        )
        assert (
            _format_brief_goal_target(
                {"display": "SBP"}, {"low": 110.0, "high": 130.0, "unit": "mmHg"}
            )
            == "SBP: 110–130 mmHg"
        )

    def test_keeps_fractional_float_bounds(self):
        assert (
            _format_brief_goal_target({"display": "HbA1c"}, {"high": 7.5, "unit": "%"})
            == "HbA1c < 7.5 %"
        )


from acp_writer.services.artifact_resolver import plan_goal_from_entry


class TestPlanGoalFromEntry:
    def test_maps_snake_to_camel(self):
        pg = plan_goal_from_entry(
            {
                "description": "Achieve HbA1c < 7%",
                "target_measure_code": {"display": "HbA1c"},
                "target_value": {"high": 7, "unit": "%"},
                "source_cpg": "ada-2024",
                "source_recommendation_id": "rec-1",
            },
            0,
        )
        assert pg["id"] == "g0"
        assert pg["description"] == "Achieve HbA1c < 7%"
        assert pg["target"] == "HbA1c < 7 %"
        assert pg["sourceCpgId"] == "ada-2024"
        assert pg["sourceRecommendationId"] == "rec-1"

    def test_uses_index_id_when_absent(self):
        assert plan_goal_from_entry({"description": "d"}, 3)["id"] == "g3"

    def test_omits_absent_optional_fields(self):
        pg = plan_goal_from_entry({"description": "d", "source_cpg": "c"}, 0)
        assert "target" not in pg
        assert "sourceRecommendationId" not in pg
        assert pg["sourceCpgId"] == "c"


from acp_writer.services.artifact_resolver import plan_activity_from_entry


class TestPlanActivityFromEntry:
    def test_maps_snake_to_camel(self):
        pa = plan_activity_from_entry(
            {
                "type": "medication",
                "description": "Metformin 500mg",
                "dose": "500mg",
                "route": "oral",
                "frequency": "twice daily",
                "specialty": "endocrinology",
                "source_recommendation_id": "rec-9",
                "source_cpg": "ada-2024",
                "clinical_rationale": "First-line for T2DM.",
            },
            0,
        )
        assert pa["id"] == "a0"
        assert pa["description"] == "Metformin 500mg"
        assert pa["dose"] == "500mg"
        assert pa["route"] == "oral"
        assert pa["frequency"] == "twice daily"
        assert pa["specialty"] == "endocrinology"
        assert pa["sourceRecommendationId"] == "rec-9"
        assert pa["sourceCpg"] == "ada-2024"
        assert pa["clinicalRationale"] == "First-line for T2DM."

    def test_uses_index_id_when_absent(self):
        assert plan_activity_from_entry({"description": "d"}, 2)["id"] == "a2"

    def test_omits_absent_optional_fields(self):
        pa = plan_activity_from_entry({"description": "d"}, 0)
        for k in ("dose", "route", "frequency", "specialty",
                  "sourceRecommendationId", "sourceCpg", "clinicalRationale"):
            assert k not in pa

    def test_maps_automation_state_template_source_and_bindings(self):
        pa = plan_activity_from_entry({
            "id": "activity-1",
            "description": "Monitor home readings",
            "automations": [{
                "id": "00000000-0000-4000-8000-000000000001",
                "template_refs": [{
                    "template_id": "home-bp-monitoring",
                    "version": "a" * 12,
                    "source_cpg": "SYN-HTN-2026-002",
                }],
                "enabled": True,
                "revision": "b" * 12,
                "bpmn_xml": "<bpmn:definitions />",
                "capabilities_used": ["patient.request_observation"],
                "bindings": [{"name": "reporting_interval", "value": "P1W", "source": "cpg-default"}],
                "validation": {"status": "valid", "rungs_passed": ["L0"], "warnings": [], "errors": []},
                "review_notes": [],
            }],
        }, 0)
        automation = pa["automations"][0]
        assert automation["enabled"] is True
        assert automation["templateRefs"][0]["sourceCpg"] == "SYN-HTN-2026-002"
        assert automation["bindings"][0]["name"] == "reporting_interval"
        assert automation["validation"]["status"] == "valid"


from acp_writer.services.artifact_resolver import enrich_run_detail


class TestEnrichRunDetailProvenance:
    def _detail_with_brief(self, brief: dict) -> dict:
        # composerData carries the brief inline; fhirGenData triggers carePlan build.
        return {
            "workflowData": {
                "composerData": {"planning_brief": brief},
                "fhirGenData": {"fhir_bundle": {"resourceType": "Bundle", "entry": []}},
            }
        }

    def test_goals_and_activities_are_mapped(self):
        detail = self._detail_with_brief(
            {
                "goals": [
                    {
                        "description": "Achieve HbA1c < 7%",
                        "target_measure_code": {"display": "HbA1c"},
                        "target_value": {"high": 7, "unit": "%"},
                        "source_cpg": "ada-2024",
                    }
                ],
                "activities": [
                    {
                        "description": "Metformin 500mg",
                        "dose": "500mg",
                        "route": "oral",
                        "source_cpg": "ada-2024",
                        "clinical_rationale": "First-line.",
                    }
                ],
                "conflicts": [],
            }
        )
        out = enrich_run_detail(detail, phi_store=None, artifacts_store=None)
        cp = out["carePlan"]
        assert cp["goals"][0]["id"] == "g0"
        assert cp["goals"][0]["target"] == "HbA1c < 7 %"
        assert cp["goals"][0]["sourceCpgId"] == "ada-2024"
        assert cp["activities"][0]["id"] == "a0"
        assert cp["activities"][0]["dose"] == "500mg"
        assert cp["activities"][0]["clinicalRationale"] == "First-line."

    def test_brief_automations_remain_visible_at_review_gate_with_shared_activity_ids(self):
        automation = {
            "id": "00000000-0000-4000-8000-000000000001",
            "template_refs": [{"template_id": "bp-check", "version": "a" * 12, "source_cpg": "CPG-1"}],
            "enabled": False,
            "revision": "b" * 12,
            "bpmn_xml": "<bpmn:definitions />",
            "capabilities_used": ["patient.request_observation"],
            "bindings": [],
            "validation": {"status": "incomplete", "rungs_passed": ["L0"], "warnings": [], "errors": []},
            "review_notes": [],
        }
        detail = {
            "workflowData": {
                "composerData": {"planning_brief": {"activities": [
                    {"id": "activity-1", "description": "First activity", "automations": [automation]},
                    {"id": "activity-2", "description": "Second activity", "automations": [automation]},
                ]}},
                "fhirGenData": {"fhir_bundle": {"resourceType": "Bundle", "entry": []}},
            },
        }
        out = enrich_run_detail(detail, phi_store=None, artifacts_store=None)
        activities = out["carePlan"]["activities"]
        assert activities[0]["automations"][0]["enabled"] is False
        assert activities[0]["automations"][0]["validation"]["status"] == "incomplete"
        assert activities[0]["automations"][0]["activityIds"] == ["activity-1", "activity-2"]


from acp_writer.services.bff import _extract_view_from_bundle


class TestPersistedGoalTarget:
    def test_goal_target_populated_from_bundle(self):
        bundle = {
            "entry": [
                {
                    "fullUrl": "urn:uuid:g",
                    "resource": {
                        "resourceType": "Goal",
                        "id": "g-1",
                        "description": {"text": "Achieve HbA1c < 7%"},
                        "target": [
                            {
                                "measure": {"text": "HbA1c"},
                                "detailRange": {"high": {"value": 7.0, "unit": "%"}},
                            }
                        ],
                    },
                }
            ]
        }
        goals, _activities, _conflicts = _extract_view_from_bundle(bundle)
        assert goals[0]["target"] == "HbA1c < 7 %"
        assert "rationale" not in goals[0]
