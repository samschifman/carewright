"""Mock data for BFF development. Not shipped in production images."""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import mlflow

from cpg_ingester.artifact_id import make_artifact_id

SECTION_MAP = [
    {"heading": "1. Introduction", "page_start": 1, "page_end": 5, "classification": "background"},
    {"heading": "2. Methodology", "page_start": 6, "page_end": 12, "classification": "methods"},
    {"heading": "3. Blood Pressure Thresholds", "page_start": 13, "page_end": 22, "classification": "decision"},
    {"heading": "4. Pharmacological Treatment", "page_start": 23, "page_end": 38, "classification": "recommendation"},
    {"heading": "5. Lifestyle Modifications", "page_start": 39, "page_end": 45, "classification": "recommendation"},
    {"heading": "6. Monitoring and Follow-up", "page_start": 46, "page_end": 52, "classification": "decision"},
    {"heading": "7. Special Populations", "page_start": 53, "page_end": 61, "classification": "recommendation"},
    {"heading": "8. References", "page_start": 62, "page_end": 70, "classification": "background"},
]

METADATA: dict[str, Any] = {
    "cpg_id": "SYN-HTN-2026-001",
    "title": "AHA Hypertension CPG 2024",
    "version": "2024.1",
    "issuing_body": "American Heart Association",
    "grading_system": "ACC/AHA",
    "scope": "Management of high blood pressure in adults",
}

_CPG_ID = METADATA["cpg_id"]

DECISIONS = [
    {
        "artifact_id": make_artifact_id(_CPG_ID, "dmn", "Blood Pressure Treatment Threshold", "3. Blood Pressure Thresholds"),
        "dmn_xml": '<?xml version="1.0" encoding="UTF-8"?>\n<definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/">\n  <decision id="bp-threshold" name="Blood Pressure Treatment Threshold">\n    <!-- simplified for mock -->\n  </decision>\n</definitions>',
        "item": {
            "name": "Blood Pressure Treatment Threshold",
            "type": "decision_table",
            "category": "Diagnosis",
            "section": "3. Blood Pressure Thresholds",
        },
        "decision_model_summary": {
            "id": "bp-threshold",
            "name": "Blood Pressure Treatment Threshold",
            "inputs": [
                {"name": "systolicBP", "type": "number"},
                {"name": "diastolicBP", "type": "number"},
                {"name": "patientAge", "type": "number"},
                {"name": "hasDiabetes", "type": "boolean"},
            ],
            "outputs": [
                {"name": "treatmentRequired", "type": "boolean"},
                {"name": "urgency", "type": "string"},
            ],
            "category": "Diagnosis",
        },
    },
    {
        "artifact_id": make_artifact_id(_CPG_ID, "dmn", "Monitoring Frequency", "6. Monitoring and Follow-up"),
        "dmn_xml": '<?xml version="1.0" encoding="UTF-8"?>\n<definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/">\n  <decision id="monitoring-freq" name="Monitoring Frequency">\n    <!-- simplified for mock -->\n  </decision>\n</definitions>',
        "item": {
            "name": "Monitoring Frequency",
            "type": "decision_table",
            "category": "Monitoring",
            "section": "6. Monitoring and Follow-up",
        },
        "decision_model_summary": {
            "id": "monitoring-freq",
            "name": "Monitoring Frequency",
            "inputs": [
                {"name": "currentBP", "type": "number"},
                {"name": "controlStatus", "type": "string"},
            ],
            "outputs": [
                {"name": "visitIntervalWeeks", "type": "number"},
                {"name": "labFrequency", "type": "string"},
            ],
            "category": "Monitoring",
        },
    },
]

RECOMMENDATIONS = [
    {
        "artifact_id": make_artifact_id(_CPG_ID, "rec", "First-line pharmacological therapy with ACE inhibitor or ARB", "4. Pharmacological Treatment"),
        "id": "rec-001",
        "source_cpg": "SYN-HTN-2026-001",
        "title": "First-line pharmacological therapy with ACE inhibitor or ARB",
        "content": "For adults with confirmed hypertension and an average BP ≥130/80 mmHg, initiate pharmacological treatment with an ACE inhibitor, ARB, calcium channel blocker, or thiazide diuretic.",
        "recommendation_type": "pharmacological",
        "escalated": True,
        "escalation_reason": "reviewer-unparseable",
        "escalation_errors": [
            "Automated review could not validate the treatment recommendation."
        ],
        "section": "4. Pharmacological Treatment",
        "certainty": {
            "strength": "strong-for",
            "evidence_quality": "high",
            "grading_system": "ACC/AHA",
            "original_grade": "I (A)",
        },
        "rationale": "Multiple large-scale RCTs demonstrate that these four drug classes reduce cardiovascular events and mortality in hypertensive adults.",
        "scope_notes": "Applies to adults ≥18 years without contraindications.",
        "cross_references": [
            {"target_id": "bp-threshold", "target_type": "decision", "relationship": "depends-on"},
        ],
        "source_location": {
            "page_start": 25,
            "page_end": 28,
            "source_text": "We recommend initiating pharmacological treatment...",
        },
    },
    {
        "artifact_id": make_artifact_id(_CPG_ID, "rec", "Lifestyle modifications for all hypertensive patients", "5. Lifestyle Modifications"),
        "id": "rec-002",
        "source_cpg": "SYN-HTN-2026-001",
        "title": "Lifestyle modifications for all hypertensive patients",
        "content": "All patients with hypertension should receive counseling on lifestyle modifications including dietary sodium reduction, increased physical activity, weight management, and moderation of alcohol consumption.",
        "recommendation_type": "lifestyle",
        "section": "5. Lifestyle Modifications",
        "certainty": {
            "strength": "strong-for",
            "evidence_quality": "moderate",
            "grading_system": "ACC/AHA",
            "original_grade": "I (B-R)",
        },
        "rationale": "Lifestyle changes can reduce systolic BP by 5-10 mmHg and may reduce the need for pharmacological intervention.",
    },
    {
        "artifact_id": make_artifact_id(_CPG_ID, "rec", "Combination therapy for stage 2 hypertension", "4. Pharmacological Treatment"),
        "id": "rec-003",
        "source_cpg": "SYN-HTN-2026-001",
        "title": "Combination therapy for stage 2 hypertension",
        "content": "For adults with stage 2 hypertension (BP ≥140/90 mmHg), initiate therapy with two first-line agents of different classes.",
        "recommendation_type": "pharmacological",
        "section": "4. Pharmacological Treatment",
        "certainty": {
            "strength": "conditional-for",
            "evidence_quality": "moderate",
            "grading_system": "ACC/AHA",
            "original_grade": "IIa (B-NR)",
        },
        "rationale": "Combination therapy achieves target BP faster and more reliably than monotherapy in patients with stage 2 hypertension.",
        "scope_notes": "Consider patient comorbidities and potential drug interactions when selecting combination.",
        "cross_references": [
            {"target_id": "bp-threshold", "target_type": "decision", "relationship": "depends-on"},
            {"target_id": "rec-001", "target_type": "recommendation", "relationship": "builds-on"},
        ],
    },
]

ASSEMBLY_REPORT: dict[str, Any] = {
    "cpg_id": "SYN-HTN-2026-001",
    "recommendations_count": 3,
    "dmn_models_count": 2,
    "escalated_count": 1,
    "integrity_errors": [],
}

ESCALATED_ITEMS = [
    {
        "id": "rec-renal-denervation",
        "name": "Renal denervation for resistant hypertension",
        "type": "recommendation",
        "section": "7. Special Populations",
        "escalation_reason": "reviewer-unparseable",
        "escalation_errors": [
            "Automated reviewers could not verify evidence quality — emerging intervention with limited long-term data."
        ],
    },
]

DELIVERY_STATUS: dict[str, Any] = {
    "published": True,
    "cpg_id": "SYN-HTN-2026-001",
    "artifact_location": "cpg-artifacts:published/SYN-HTN-2026-001",
    "artifacts": [
        {"type": "metadata", "ref": "cpg-artifacts:published/SYN-HTN-2026-001/metadata.json", "cpg_id": "SYN-HTN-2026-001"},
        {"type": "dmn", "ref": "cpg-artifacts:published/SYN-HTN-2026-001/dmn/Blood Pressure Treatment Threshold.dmn", "name": "Blood Pressure Treatment Threshold", "artifact_id": make_artifact_id(_CPG_ID, "dmn", "Blood Pressure Treatment Threshold", "3. Blood Pressure Thresholds")},
        {"type": "dmn", "ref": "cpg-artifacts:published/SYN-HTN-2026-001/dmn/Monitoring Frequency.dmn", "name": "Monitoring Frequency", "artifact_id": make_artifact_id(_CPG_ID, "dmn", "Monitoring Frequency", "6. Monitoring and Follow-up")},
        {"type": "recommendations", "ref": "cpg-artifacts:published/SYN-HTN-2026-001/recommendations.json", "count": 3},
    ],
    "errors": [],
    "escalated_items_count": 1,
}


def _make_steps(up_to: str) -> list[dict[str, Any]]:
    all_steps = [
        "Parse", "Analyze", "ReviewManifest",
        "Generate", "ReviewArtifacts",
        "Assemble", "Deliver", "Done",
    ]
    now = datetime.now(timezone.utc)
    result = []
    reached = False
    for name in all_steps:
        if name == up_to:
            reached = True
            result.append({"name": name, "status": "active", "startedAt": now.isoformat()})
        elif not reached:
            result.append({"name": name, "status": "completed", "startedAt": now.isoformat(), "completedAt": now.isoformat()})
        else:
            result.append({"name": name, "status": "pending"})
    return result


def _completed_steps() -> list[dict[str, Any]]:
    steps = _make_steps("Done")
    now = datetime.now(timezone.utc).isoformat()
    for s in steps:
        s["status"] = "completed"
        s.setdefault("startedAt", now)
        s["completedAt"] = now
    return steps


def _failed_steps() -> list[dict[str, Any]]:
    steps = _make_steps("Analyze")
    for s in steps:
        if s["status"] == "active":
            s["status"] = "failed"
    return steps


@mlflow.trace(name="cpg_ingester.mock.read_run_artifact")
def _read_run_artifact(directory: Path, name: str, default: Any) -> Any:
    try:
        return json.loads((directory / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


@mlflow.trace(name="cpg_ingester.mock.load_run_directory")
def load_run_directory(directory: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load completed monolith artifacts into the UI-facing run models."""
    from cpg_ingester.services.artifact_resolver import enrich_run_detail

    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"mock run directory does not exist: {root}")
    summary = _read_run_artifact(root, "run-summary.json", {})
    metadata_file = _read_run_artifact(root, "metadata.json", {})
    analysis = _read_run_artifact(root, "analysis-result.json", {
        "cpg_metadata": metadata_file,
        "section_map": _read_run_artifact(root, "section-map.json", []),
        "item_manifest": _read_run_artifact(root, "manifest.json", []),
    })
    generation = _read_run_artifact(root, "generation-result.json", {
        "dmn_results": _read_run_artifact(root, "dmn-results.json", []),
        "recommendation_results": _read_run_artifact(root, "recommendation-results.json", []),
        "bpmn_results": _read_run_artifact(root, "bpmn-results.json", []),
    })
    assembly_report = _read_run_artifact(root, "assembly-report.json", {})
    delivery_status = _read_run_artifact(root, "delivery-status.json", {})
    metadata = analysis.get("cpg_metadata") or metadata_file or {}
    run_id = summary.get("run_id") or root.name
    created_at = summary.get("created_at") or datetime.fromtimestamp(
        (root / "run-summary.json").stat().st_mtime if (root / "run-summary.json").exists()
        else root.stat().st_mtime,
        timezone.utc,
    ).isoformat()
    run = {
        "id": run_id,
        "status": "completed",
        "cpgName": metadata.get("title") or summary.get("cpg_name") or root.name,
        "createdAt": created_at,
        "currentStep": "Done",
    }
    detail = enrich_run_detail({
        **run,
        "steps": _completed_steps(),
        "workflowData": {
            "analysisResult": analysis,
            "generateResult": generation,
            "assemblyResult": {"assembly_report": assembly_report},
            "deliveryResult": {"delivery_status": delivery_status},
        },
    }, None)
    return run, detail


RUNS: dict[str, dict[str, Any]] = {
    "run-001": {
        "id": "run-001",
        "status": "completed",
        "cpgName": "AHA Hypertension CPG 2024",
        "createdAt": "2026-08-05T14:30:00Z",
        "currentStep": "Done",
    },
    "run-002": {
        "id": "run-002",
        "status": "awaiting_artifact_review",
        "cpgName": "ADA Diabetes Standards of Care 2026",
        "createdAt": "2026-08-06T09:15:00Z",
        "currentStep": "ReviewArtifacts",
    },
    "run-003": {
        "id": "run-003",
        "status": "awaiting_manifest_review",
        "cpgName": "KDIGO CKD Guideline 2025",
        "createdAt": "2026-08-06T10:45:00Z",
        "currentStep": "ReviewManifest",
    },
    "run-004": {
        "id": "run-004",
        "status": "analyzing",
        "cpgName": "ESC Heart Failure Guidelines 2025",
        "createdAt": "2026-08-06T11:20:00Z",
        "currentStep": "Analyze",
    },
    "run-005": {
        "id": "run-005",
        "status": "failed",
        "cpgName": "WHO Malaria Treatment Guidelines",
        "createdAt": "2026-08-04T08:00:00Z",
        "currentStep": "Analyze",
    },
}

RUN_DETAILS: dict[str, dict[str, Any]] = {
    "run-001": {
        **RUNS["run-001"],
        "steps": _completed_steps(),
        "metadata": METADATA,
        "sectionMap": SECTION_MAP,
        "decisions": DECISIONS,
        "recommendations": RECOMMENDATIONS,
        "assemblyReport": ASSEMBLY_REPORT,
        "escalatedItems": ESCALATED_ITEMS,
        "deliveryStatus": DELIVERY_STATUS,
    },
    "run-002": {
        **RUNS["run-002"],
        "steps": _make_steps("ReviewArtifacts"),
        "awaitingReview": "pre-delivery",
        "reviewIteration": 2,
        "previousFeedback": {
            "action": "request_changes",
            "feedback": [
                {"itemId": "bp-threshold", "itemType": "decision", "comment": "Threshold should be 140/90 not 130/80 for patients over 65"},
            ],
            "overallComment": "Please adjust the BP threshold decision table for elderly patients.",
        },
        "metadata": {**METADATA, "cpg_id": "SYN-DM2-2026-001", "title": "ADA Diabetes Standards of Care 2026", "issuing_body": "American Diabetes Association"},
        "sectionMap": SECTION_MAP,
        "decisions": DECISIONS,
        "recommendations": RECOMMENDATIONS,
        "recommendationEscalations": [
            {
                "id": "section-special-populations",
                "name": "Section: 7. Special Populations",
                "type": "recommendation",
                "section": "7. Special Populations",
                "escalation_reason": "no-source-text",
                "escalation_errors": [
                    "No source text was available to verify recommendations in this section."
                ],
            }
        ],
    },
    "run-003": {
        **RUNS["run-003"],
        "steps": _make_steps("ReviewManifest"),
        "awaitingReview": "manifest",
        "reviewIteration": 1,
        "metadata": {**METADATA, "cpg_id": "KDIGO-CKD-2025", "title": "KDIGO CKD Guideline 2025", "issuing_body": "KDIGO"},
        "sectionMap": SECTION_MAP,
    },
    "run-004": {
        **RUNS["run-004"],
        "steps": _make_steps("Analyze"),
        "metadata": {**METADATA, "cpg_id": "ESC-HF-2025", "title": "ESC Heart Failure Guidelines 2025", "issuing_body": "European Society of Cardiology"},
    },
    "run-005": {
        **RUNS["run-005"],
        "steps": _failed_steps(),
    },
}
