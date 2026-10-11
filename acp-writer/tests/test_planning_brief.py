"""Tests for PlanningBrief Pydantic schema."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from acp_writer.planning_brief import (
    ActivityType,
    ActivityWorkflow,
    ConflictCategory,
    ConflictEntry,
    ConflictSeverity,
    ConflictSource,
    ConflictStatus,
    DMNAuditEntry,
    FHIRCode,
    PlanActivity,
    PlanGoal,
    PlanningBrief,
    ReviewStatus,
    RevisionRound,
    TargetValue,
    coerce_conflicts,
    conflict_id,
    normalize_review_history,
    render_clinician_directives,
    render_feedback_history,
    stamp_activity_ids,
)


def _sample_brief() -> PlanningBrief:
    return PlanningBrief(
        patient_reference="Patient/123",
        applicable_cpgs=["SYN-HTN-2026-001"],
        dmn_audit_trail=[
            DMNAuditEntry(
                model_id="treatment-recommendation",
                model_name="Treatment Recommendation",
                inputs={"Systolic BP": 145, "Has Diabetes": True, "Has Kidney Disease": False},
                outputs={
                    "Action": "Start medication",
                    "Medication": "Lisinopril",
                    "Dose": "10 mg daily",
                    "Follow Up Weeks": 4,
                },
                fhir_references=["Observation/bp-001", "Condition/diabetes-001"],
                timestamp=datetime(2026, 7, 21, 10, 0, 0, tzinfo=timezone.utc),
            ),
        ],
        goals=[
            PlanGoal(
                description="Lower blood pressure to target range",
                target_measure_code=FHIRCode(
                    system="http://loinc.org",
                    code="8480-6",
                    display="Systolic blood pressure",
                ),
                target_value=TargetValue(high=140, unit="mmHg"),
                source_recommendation_id="rec-guid-123",
                source_cpg="SYN-HTN-2026-001",
            ),
        ],
        activities=[
            PlanActivity(
                type=ActivityType.MEDICATION,
                description="Start Lisinopril 10mg daily",
                code=FHIRCode(
                    system="http://www.nlm.nih.gov/research/umls/rxnorm",
                    code="29046",
                    display="Lisinopril",
                ),
                dose="10 mg",
                route="oral",
                frequency="daily",
                source_recommendation_id="rec-guid-456",
                source_cpg="SYN-HTN-2026-001",
                source_dmn_call=0,
                clinical_rationale="ACE inhibitor selected due to renal protective effects",
                workflow=ActivityWorkflow(
                    actor="prescribing_physician",
                    escalation="If BP not at target after 4 weeks, consider dose increase",
                    monitoring_trigger="BMP in 4 weeks to check renal function",
                ),
            ),
            PlanActivity(
                type=ActivityType.MONITORING,
                description="Basic Metabolic Panel",
                code=FHIRCode(
                    system="http://loinc.org",
                    code="51990-0",
                    display="Basic metabolic panel",
                ),
                frequency="4 weeks",
                source_recommendation_id="rec-guid-789",
                source_cpg="SYN-HTN-2026-001",
                source_dmn_call=0,
            ),
            PlanActivity(
                type=ActivityType.LIFESTYLE,
                description="DASH diet - reduce sodium intake to less than 2300mg/day",
                source_recommendation_id="rec-guid-abc",
                source_cpg="SYN-HTN-2026-001",
                clinical_rationale="Dietary modification is first-line for all hypertension stages",
            ),
        ],
        review_status=ReviewStatus.APPROVED,
    )


def test_plan_activity_has_uuid_hex_id_and_empty_automations_by_default():
    activity = _sample_brief().activities[0]

    assert len(activity.id) == 32
    assert all(char in "0123456789abcdef" for char in activity.id)
    assert activity.automations == []


def test_stamp_activity_ids_preserves_matching_prior_ids_and_assigns_new_ids():
    prior = _sample_brief().model_dump(mode="json")
    prior["activities"][0]["id"] = "prior-medication"
    prior["activities"][1]["id"] = "prior-lab"
    current = _sample_brief()
    current.activities[0].id = "generated-medication"
    current.activities[1].id = "generated-lab"
    current.activities.append(PlanActivity(
        type=ActivityType.PROCESS,
        description="Schedule care-team review",
        source_cpg="SYN-HTN-2026-001",
    ))

    result = stamp_activity_ids(current, prior)

    assert [item.id for item in result.activities[:2]] == ["prior-medication", "prior-lab"]
    assert result.activities[2].id not in {"prior-medication", "prior-lab"}


class TestFHIRCode:
    def test_with_display(self):
        code = FHIRCode(system="http://loinc.org", code="8480-6", display="Systolic BP")
        assert code.system == "http://loinc.org"
        assert code.display == "Systolic BP"

    def test_without_display(self):
        code = FHIRCode(system="http://loinc.org", code="8480-6")
        assert code.display is None

    def test_missing_required(self):
        with pytest.raises(ValidationError):
            FHIRCode(system="http://loinc.org")


class TestTargetValue:
    def test_high_only(self):
        tv = TargetValue(high=140, unit="mmHg")
        assert tv.low is None

    def test_range(self):
        tv = TargetValue(low=90, high=140, unit="mmHg")
        assert tv.low == 90
        assert tv.high == 140

    def test_missing_unit(self):
        with pytest.raises(ValidationError):
            TargetValue(high=140)


class TestDMNAuditEntry:
    def test_roundtrip(self):
        entry = DMNAuditEntry(
            model_id="treatment-recommendation",
            model_name="Treatment Recommendation",
            inputs={"Systolic BP": 145},
            outputs={"Action": "Start medication"},
            timestamp=datetime(2026, 7, 21, 10, 0, 0, tzinfo=timezone.utc),
        )
        data = entry.model_dump(mode="json")
        restored = DMNAuditEntry.model_validate(data)
        assert restored.model_id == entry.model_id
        assert restored.inputs == entry.inputs

    def test_fhir_references_default_empty(self):
        entry = DMNAuditEntry(
            model_id="test",
            model_name="Test",
            inputs={},
            outputs={},
            timestamp=datetime.now(timezone.utc),
        )
        assert entry.fhir_references == []


class TestActivityWorkflow:
    def test_all_fields(self):
        wf = ActivityWorkflow(
            actor="prescribing_physician",
            sequence_after="Initial assessment",
            escalation="Refer to specialist if uncontrolled after 3 months",
            monitoring_trigger="BMP in 4 weeks",
        )
        assert wf.actor == "prescribing_physician"
        assert wf.sequence_after == "Initial assessment"

    def test_all_optional(self):
        wf = ActivityWorkflow()
        assert wf.actor is None
        assert wf.sequence_after is None


class TestPlanActivity:
    def test_medication_activity(self):
        act = PlanActivity(
            type=ActivityType.MEDICATION,
            description="Start Lisinopril",
            code=FHIRCode(system="http://www.nlm.nih.gov/research/umls/rxnorm", code="29046"),
            dose="10 mg",
            route="oral",
            frequency="daily",
            source_cpg="SYN-HTN-2026-001",
        )
        assert act.type == ActivityType.MEDICATION
        assert act.dose == "10 mg"

    def test_lifestyle_activity_minimal(self):
        act = PlanActivity(
            type=ActivityType.LIFESTYLE,
            description="Reduce sodium intake",
            source_cpg="SYN-HTN-2026-001",
        )
        assert act.code is None
        assert act.dose is None

    def test_missing_required(self):
        with pytest.raises(ValidationError):
            PlanActivity(type=ActivityType.MEDICATION, description="test")

    def test_activity_type_enum(self):
        for t in ["medication", "monitoring", "lifestyle", "referral", "educational", "process"]:
            act = PlanActivity(type=t, description="test", source_cpg="test")
            assert act.type == ActivityType(t)


class TestConflictEntry:
    def test_new_model(self):
        c = ConflictEntry(
            id="conf-abc123",
            category=ConflictCategory.DIVERGENT_TARGET,
            severity=ConflictSeverity.WARNING,
            description="Conflicting BP targets from two guidelines",
            goal_indices=[0, 1],
            sources=[
                ConflictSource(cpg_id="SYN-HTN-2026-001", recommendation_id="rec-1"),
                ConflictSource(cpg_id="SYN-DM2-2026-001", recommendation_id="rec-2"),
            ],
        )
        assert c.status == ConflictStatus.DETECTED
        assert len(c.sources) == 2
        assert c.resolution is None

    def test_defaults(self):
        c = ConflictEntry(id="conf-1", description="x")
        assert c.category == ConflictCategory.OTHER
        assert c.severity == ConflictSeverity.WARNING
        assert c.goal_indices == []
        assert c.activity_indices == []
        assert c.sources == []

    def test_json_roundtrip(self):
        c = ConflictEntry(
            id="conf-1",
            category=ConflictCategory.CONTRADICTION,
            description="titrate up vs. reduce lisinopril",
            activity_indices=[0, 3],
            sources=[ConflictSource(cpg_id="A", recommendation_id="r1", excerpt="q")],
            confidence="high",
        )
        restored = ConflictEntry.model_validate(c.model_dump(mode="json"))
        assert restored == c


class TestConflictId:
    def test_deterministic(self):
        sources = [ConflictSource(cpg_id="A", recommendation_id="r1")]
        a = conflict_id(ConflictCategory.OVERLAP, sources)
        b = conflict_id(ConflictCategory.OVERLAP, sources)
        assert a == b
        assert a.startswith("conf-")

    def test_order_insensitive(self):
        s1 = [ConflictSource(cpg_id="A", recommendation_id="r1"),
              ConflictSource(cpg_id="B", recommendation_id="r2")]
        s2 = list(reversed(s1))
        assert conflict_id(ConflictCategory.OVERLAP, s1) == conflict_id(ConflictCategory.OVERLAP, s2)

    def test_category_and_content_key_change_id(self):
        sources = [ConflictSource(cpg_id="A", recommendation_id="r1")]
        base = conflict_id(ConflictCategory.OVERLAP, sources)
        assert conflict_id(ConflictCategory.CONTRADICTION, sources) != base
        assert conflict_id(ConflictCategory.OVERLAP, sources, content_key="bp") != base

    def test_falls_back_to_cpg_ids(self):
        # No recommendation ids → cpg ids drive the hash (still stable).
        s = [ConflictSource(cpg_id="A"), ConflictSource(cpg_id="B")]
        assert conflict_id(ConflictCategory.OVERLAP, s) == conflict_id(
            ConflictCategory.OVERLAP, list(reversed(s))
        )


class TestCoerceConflicts:
    def test_empty(self):
        assert coerce_conflicts(None) == []
        assert coerce_conflicts([]) == []

    def test_bare_string(self):
        [c] = coerce_conflicts(["Two guidelines disagree on BP target"])
        assert c["description"] == "Two guidelines disagree on BP target"
        assert c["id"].startswith("conf-")
        ConflictEntry.model_validate(c)  # must validate

    def test_legacy_sources_list_of_str(self):
        [c] = coerce_conflicts([
            {"description": "dup monitoring", "activity_indices": [2, 3], "sources": ["CPG-A", "CPG-B"]}
        ])
        assert c["sources"] == [{"cpg_id": "CPG-A"}, {"cpg_id": "CPG-B"}]
        ConflictEntry.model_validate(c)

    def test_legacy_recommendation_ids_key(self):
        [c] = coerce_conflicts([{"description": "x", "recommendation_ids": ["r1"]}])
        assert c["sources"] == [{"cpg_id": "r1"}]
        assert "recommendation_ids" not in c
        ConflictEntry.model_validate(c)

    def test_preserves_existing_id(self):
        [c] = coerce_conflicts([
            {"id": "conf-keep", "description": "x", "sources": []}
        ])
        assert c["id"] == "conf-keep"

    # --- F7: coerce_conflicts must be total (never raise on loose input) ---

    def test_camelcase_source_keys(self):
        [c] = coerce_conflicts([
            {"description": "x", "sources": [
                {"cpgId": "CPG-A", "recommendationId": "r1", "excerpt": "quote"}
            ]}
        ])
        assert c["sources"] == [
            {"cpg_id": "CPG-A", "recommendation_id": "r1", "excerpt": "quote"}
        ]
        ConflictEntry.model_validate(c)

    def test_source_missing_cpg_id_is_dropped_not_fatal(self):
        # A source with no recoverable cpg_id must be dropped without sinking
        # the whole conflict (previously raised inside ConflictSource(**s)).
        [c] = coerce_conflicts([
            {"description": "x", "sources": [{"recommendation_id": "r1"}, {"cpg_id": "CPG-A"}]}
        ])
        assert c["sources"] == [{"cpg_id": "CPG-A"}]
        ConflictEntry.model_validate(c)

    def test_non_string_source_fields_coerced(self):
        [c] = coerce_conflicts([{"description": "x", "sources": [{"cpg_id": 123}]}])
        assert c["sources"] == [{"cpg_id": "123"}]
        ConflictEntry.model_validate(c)

    def test_severity_synonyms_coerced(self):
        [c] = coerce_conflicts([{"description": "x", "severity": "high", "sources": []}])
        assert c["severity"] == "critical"
        [c2] = coerce_conflicts([{"description": "x", "severity": "low", "sources": []}])
        assert c2["severity"] == "info"
        ConflictEntry.model_validate(c)
        ConflictEntry.model_validate(c2)

    def test_unknown_category_and_status_defaulted(self):
        [c] = coerce_conflicts([
            {"description": "x", "category": "banana", "status": "whatever", "sources": []}
        ])
        assert c["category"] == "other"
        assert c["status"] == "detected"
        ConflictEntry.model_validate(c)

    def test_conflict_entry_instances_accepted(self):
        entry = ConflictEntry(id="conf-1", description="x")
        [c] = coerce_conflicts([entry])
        assert c["id"] == "conf-1"
        ConflictEntry.model_validate(c)

    def test_non_string_description_coerced(self):
        [c] = coerce_conflicts([{"description": 42, "sources": []}])
        assert c["description"] == "42"
        ConflictEntry.model_validate(c)


class TestRenderClinicianDirectives:
    """F18a: the durable clinician-directives composer section."""

    def _conflict(self, **over) -> dict:
        c = {
            "id": "conf-abc12345",
            "category": "divergent_target",
            "severity": "warning",
            "description": "Two guidelines set different BP targets",
            "rationale": "HTN <140/90 vs DM2 <130/80",
            "suggested_resolution": "Prefer the diabetes guideline's <130/80 target",
            "sources": [
                {"cpg_id": "SYN-HTN-2026-001", "recommendation_id": "htn-rec-002"},
                {"cpg_id": "SYN-DM2-2026-001"},
            ],
        }
        c.update(over)
        return c

    def test_empty_is_blank(self):
        assert render_clinician_directives(None) == ""
        assert render_clinician_directives([], comment="") == ""

    def test_renders_instruction_and_referents_compactly(self):
        block = render_clinician_directives(
            [self._conflict()], comment="resolve all conflicts as suggested"
        )
        assert "## Clinician-directed changes (MANDATORY" in block
        assert "### Clinician instruction (this round)" in block
        assert "resolve all conflicts as suggested" in block
        assert "### Unresolved conflicts" in block
        assert "[conf-abc12345]" in block
        assert "divergent_target" in block
        assert "Two guidelines set different BP targets" in block
        assert "Rationale: HTN <140/90 vs DM2 <130/80" in block
        assert "Suggested: Prefer the diabetes guideline's <130/80 target" in block
        assert "SYN-HTN-2026-001/htn-rec-002" in block
        assert "SYN-DM2-2026-001" in block
        # Compact — no raw JSON dump of the conflict object.
        assert "{" not in block

    def test_resolved_conflicts_listed_as_keep_resolved(self):
        resolved = self._conflict(
            id="conf-done", status="resolved", resolution="Merged the diet items"
        )
        block = render_clinician_directives(
            [resolved, self._conflict()], comment="now resolve the BP conflict"
        )
        assert "### Resolved in earlier rounds" in block
        assert "[conf-done]: Merged the diet items" in block
        # Resolved conflicts are NOT in the unresolved referent list.
        unresolved_section = block.split("### Unresolved conflicts")[1].split(
            "### Resolved in earlier rounds")[0]
        assert "conf-done" not in unresolved_section

    def test_enforcement_note_rendered_first(self):
        block = render_clinician_directives(
            [self._conflict()], comment="resolve as suggested",
            enforcement_note="- [conf-abc12345] was not applied",
        )
        assert "### NOT APPLIED in your previous attempt" in block
        assert "- [conf-abc12345] was not applied" in block
        assert block.index("NOT APPLIED") < block.index("Clinician instruction")

    def test_comment_only(self):
        block = render_clinician_directives([], comment="please remove the statin activity")
        assert "please remove the statin activity" in block
        assert "### Unresolved conflicts" not in block

    def test_coerces_loose_shapes(self):
        # Bare-string / legacy conflicts must not raise (coerced first).
        block = render_clinician_directives(["two guidelines disagree on BP"], comment="x")
        assert "two guidelines disagree on BP" in block


class TestPlanningBriefCoercesConflicts:
    """F4: a single malformed conflict must never sink the whole brief."""

    def _base(self, conflicts):
        return {
            "patient_reference": "Patient/1",
            "applicable_cpgs": ["CPG-A"],
            "goals": [{"description": "g", "source_cpg": "CPG-A"}],
            "activities": [
                {"type": "lifestyle", "description": "a", "source_cpg": "CPG-A"}
            ],
            "conflicts": conflicts,
        }

    def test_loose_conflict_does_not_drop_goals_and_activities(self):
        # Before the before-validator, this brief raised on validate — and every
        # caller that swallowed the error dropped the goals/activities too.
        brief = PlanningBrief.model_validate(self._base([
            {"description": "loose", "severity": "high", "sources": ["CPG-A"]}
        ]))
        assert len(brief.goals) == 1
        assert len(brief.activities) == 1
        assert len(brief.conflicts) == 1
        assert brief.conflicts[0].severity == ConflictSeverity.CRITICAL

    def test_bare_string_conflict_validates(self):
        brief = PlanningBrief.model_validate(self._base(["two guidelines disagree"]))
        assert brief.conflicts[0].description == "two guidelines disagree"
        assert len(brief.goals) == 1

    def test_source_without_cpg_id_does_not_raise(self):
        brief = PlanningBrief.model_validate(self._base([
            {"description": "x", "sources": [{"recommendation_id": "r1"}]}
        ]))
        assert brief.conflicts[0].sources == []
        assert len(brief.activities) == 1

    def test_clean_brief_is_idempotent(self):
        clean = self._base([
            {"id": "conf-x", "description": "x",
             "category": "overlap", "severity": "info", "sources": [{"cpg_id": "CPG-A"}]}
        ])
        once = PlanningBrief.model_validate(clean)
        twice = PlanningBrief.model_validate(once.model_dump(mode="json"))
        assert once == twice


class TestPlanningBrief:
    def test_roundtrip(self):
        brief = _sample_brief()
        data = brief.model_dump(mode="json")
        restored = PlanningBrief.model_validate(data)
        assert restored.patient_reference == "Patient/123"
        assert len(restored.goals) == 1
        assert len(restored.activities) == 3
        assert len(restored.dmn_audit_trail) == 1
        assert restored.review_status == ReviewStatus.APPROVED

    def test_json_roundtrip(self):
        brief = _sample_brief()
        json_str = brief.model_dump_json()
        restored = PlanningBrief.model_validate_json(json_str)
        assert restored == brief

    def test_missing_goals(self):
        with pytest.raises(ValidationError):
            PlanningBrief(
                patient_reference="Patient/123",
                applicable_cpgs=["SYN-HTN-2026-001"],
                activities=[],
            )

    def test_missing_activities(self):
        with pytest.raises(ValidationError):
            PlanningBrief(
                patient_reference="Patient/123",
                applicable_cpgs=["SYN-HTN-2026-001"],
                goals=[],
            )

    def test_empty_goals_allowed(self):
        brief = PlanningBrief(
            patient_reference="Patient/123",
            applicable_cpgs=["SYN-HTN-2026-001"],
            goals=[],
            activities=[],
        )
        assert brief.goals == []

    def test_defaults(self):
        brief = PlanningBrief(
            patient_reference="Patient/123",
            applicable_cpgs=[],
            goals=[],
            activities=[],
        )
        assert brief.dmn_audit_trail == []
        assert brief.conflicts == []
        assert brief.review_status == ReviewStatus.PENDING
        assert brief.review_feedback is None

    def test_review_status_values(self):
        for status in ["pending", "approved", "revised", "flagged"]:
            brief = PlanningBrief(
                patient_reference="Patient/123",
                applicable_cpgs=[],
                goals=[],
                activities=[],
                review_status=status,
            )
            assert brief.review_status == ReviewStatus(status)

    def test_workflow_context_serializes(self):
        brief = _sample_brief()
        data = brief.model_dump(mode="json")
        med_activity = data["activities"][0]
        assert med_activity["workflow"]["actor"] == "prescribing_physician"
        assert med_activity["workflow"]["escalation"] is not None
        assert med_activity["clinical_rationale"] is not None

    def test_provenance_chain(self):
        brief = _sample_brief()
        for activity in brief.activities:
            assert activity.source_cpg is not None
            assert activity.source_recommendation_id is not None

        assert brief.dmn_audit_trail[0].fhir_references == [
            "Observation/bp-001",
            "Condition/diabetes-001",
        ]


class TestFeedbackHistory:
    """F17b: accumulated clinician feedback rendered oldest-first, with the
    newest round marked current and older rounds under a growth guard."""

    def _rounds(self, n: int) -> list[dict]:
        return [
            {"decision": "request_changes", "comment": f"change number {i}",
             "clinician": {"display": f"Dr. {i}"}, "completed_at": f"2026-08-27T0{i}:00:00Z"}
            for i in range(1, n + 1)
        ]

    def test_empty_is_blank(self):
        assert render_feedback_history(None) == ""
        assert render_feedback_history([]) == ""
        # Rounds with no comment (e.g. an approve) render nothing.
        assert render_feedback_history([{"decision": "approve", "comment": ""}]) == ""

    def test_three_rounds_oldest_first_newest_marked_current(self):
        block = render_feedback_history(self._rounds(3))
        assert "## Feedback history (oldest first)" in block
        # Oldest first: round 1 appears before round 3.
        assert block.index("change number 1") < block.index("change number 3")
        # Newest round is flagged as the one to act on now.
        assert "(address THIS round now)" in block
        newest_header = block.split("### Round 3")[1].splitlines()[0]
        assert "address THIS round now" in newest_header
        # Round 1 is NOT marked current.
        assert "### Round 1" in block
        round1_header = block.split("### Round 1")[1].splitlines()[0]
        assert "address THIS round now" not in round1_header
        # Standing-constraint guidance is present.
        assert "standing context" in block

    def test_growth_guard_summarizes_older_rounds(self):
        # 5 rounds: the oldest 2 collapse to one-line summaries, the newest 3
        # render in full (### headers).
        block = render_feedback_history(self._rounds(5))
        assert block.count("### Round") == 3
        assert "### Round 3" in block  # verbatim
        assert "### Round 5" in block  # verbatim
        assert "### Round 1" not in block  # summarized
        assert "### Round 2" not in block  # summarized
        # Older rounds still appear as one-line summaries.
        assert "- Round 1 (Dr. 1): change number 1" in block
        assert "- Round 2 (Dr. 2): change number 2" in block

    def test_skips_empty_comment_rounds_but_keeps_numbering(self):
        rounds = [
            {"decision": "request_changes", "comment": "first ask", "clinician": "Dr. A"},
            {"decision": "approve", "comment": ""},
            {"decision": "request_changes", "comment": "second ask", "clinician": "Dr. B"},
        ]
        block = render_feedback_history(rounds)
        assert "first ask" in block
        assert "second ask" in block
        # Round numbers reflect original position (3, not 2, for the last).
        assert "### Round 3" in block


class TestNormalizeReviewHistory:
    def test_flattens_to_audit_shape(self):
        rounds = normalize_review_history([
            {"decision": "request_changes", "comment": "  fix BP  ",
             "clinician": {"display": "Dr. Reynolds"}, "completed_at": "2026-08-27T01:00:00Z"},
        ])
        assert rounds == [{
            "round": 1,
            "comment": "fix BP",
            "reviewer": "Dr. Reynolds",
            "timestamp": "2026-08-27T01:00:00Z",
            "decision": "request_changes",
        }]

    def test_reviewer_fallbacks(self):
        # dict without display → name → id → default; bare string; missing.
        assert normalize_review_history([{"reviewer": "Dr. Bare"}])[0]["reviewer"] == "Dr. Bare"
        assert normalize_review_history([{"clinician": {"name": "N"}}])[0]["reviewer"] == "N"
        assert normalize_review_history([{"comment": "x"}])[0]["reviewer"] == "clinician"

    def test_skips_non_dict_entries(self):
        assert normalize_review_history(["oops", None, {"comment": "ok"}]) == [
            {"round": 3, "comment": "ok", "reviewer": "clinician",
             "timestamp": None, "decision": None},
        ]

    def test_revision_round_serializes_on_brief(self):
        brief = PlanningBrief(
            patient_reference="Patient/1",
            applicable_cpgs=["CPG-A"],
            goals=[PlanGoal(description="g", source_cpg="CPG-A")],
            activities=[],
            revision_history=[RevisionRound(round=1, comment="fix it", reviewer="Dr. A")],
        )
        data = brief.model_dump(mode="json")
        assert data["revision_history"][0]["round"] == 1
        assert data["revision_history"][0]["comment"] == "fix it"

    def test_revision_history_defaults_empty(self):
        brief = PlanningBrief(
            patient_reference="Patient/1", applicable_cpgs=["CPG-A"],
            goals=[PlanGoal(description="g", source_cpg="CPG-A")], activities=[],
        )
        assert brief.revision_history == []
