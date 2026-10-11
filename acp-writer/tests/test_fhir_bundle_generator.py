import json

from acp_writer.nodes.fhir_bundle_generator import fhir_bundle_generator
from acp_writer.planning_brief import ActivityType, PlanActivity, PlanningBrief


def test_generator_writes_the_brief_and_bundle_for_local_artifact_export(tmp_path):
    brief = PlanningBrief(
        patient_reference="Patient/example",
        applicable_cpgs=["SYN-HTN-2026-002"],
        goals=[],
        activities=[PlanActivity(
            type=ActivityType.MONITORING,
            description="Monitor home blood pressure readings.",
            id="activity-home-bp",
            source_cpg="SYN-HTN-2026-002",
        )],
    )

    result = fhir_bundle_generator({
        "planning_brief": brief.model_dump(mode="json", exclude_none=True),
        "output_dir": str(tmp_path),
        "careplan_id": "slice-careplan",
    })

    saved_brief = json.loads((tmp_path / "planning-brief.json").read_text())
    saved_bundle = json.loads((tmp_path / "fhir-bundle.json").read_text())
    assert saved_brief["activities"][0]["id"] == "activity-home-bp"
    assert saved_bundle == result["fhir_bundle"]
