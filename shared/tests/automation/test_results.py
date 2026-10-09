from cpg_contracts.automation.validators.results import Finding, LadderResult


def test_ladder_result_filters_and_merges_findings():
    warning = Finding("L3", "WARNING", "style", "Review this label")
    error = Finding("L2", "ERROR", "xsd", "Invalid attribute")
    result = LadderResult(True, [warning], ["L1"])

    merged = result.merge(LadderResult(False, [error], ["L2"], ["L5b deferred"]))

    assert merged is result
    assert result.errors() == [error]
    assert result.warnings() == [warning]
    assert result.rungs_passed == ["L1", "L2"]
    assert result.deferred == ["L5b deferred"]
    assert result.ok is False
