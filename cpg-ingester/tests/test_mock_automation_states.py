from cpg_ingester.mocks.data import RUN_DETAILS


def test_mock_run_exposes_process_and_all_automation_review_states():
    run = RUN_DETAILS["run-006"]

    assert run["manifest"][0]["type"] == "process"
    assert run["manifest"][0]["tier_hint"] == "A"
    assert len(run["automations"]) == 3
    statuses = {
        item["template_summary"]["validation"]["status"]
        for item in run["automations"]
    }
    assert statuses == {"valid", "escalated"}
    assert run["automations"][1]["escalation_reason"] == "semantic-budget-exhausted"
    assert run["automations"][2]["escalation_reason"] == "static-budget-exhausted"
    assert run["recommendations"][0]["automation_template_ids"] == [
        run["automations"][0]["template_summary"]["id"]
    ]
