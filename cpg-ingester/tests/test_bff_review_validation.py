from cpg_ingester.services.bff import validate_review_payload
from cpg_ingester.services.sonataflow_client import SonataFlowClient


def _run(reason=""):
    automation = {
        "template_summary": {"id": "template-1"},
        "process_ir": {"process": {"acp": {"parameters": [
            {
                "name": "review_window", "type": "integer", "required": True,
                "constraints": {"min": 1, "max": 4},
            },
            {"name": "repeat_interval", "type": "duration", "required": True, "unit": "d"},
        ]}}},
        "escalated": bool(reason),
        "escalation_reason": reason,
    }
    return {"automations": [automation]}


def test_validates_parameter_edits_against_types_and_constraints():
    assert validate_review_payload(_run(), "pre-delivery", {
        "parameterEdits": [{"templateId": "template-1", "parameter": "review_window", "value": 3}],
    }) is None

    error = validate_review_payload(_run(), "pre-delivery", {
        "parameterEdits": [{"templateId": "template-1", "parameter": "review_window", "value": 5}],
    })
    assert error == "parameter-maximum"


def test_acknowledgement_requires_l7_escalation_and_a_note():
    acknowledgement = {"templateId": "template-1", "note": "Reviewed."}
    assert validate_review_payload(_run("semantic-budget-exhausted"), "pre-delivery", {
        "acknowledgements": [acknowledgement],
    }) is None
    assert "acknowledgement-note-required" == validate_review_payload(
        _run("semantic-budget-exhausted"), "pre-delivery",
        {"acknowledgements": [{"templateId": "template-1", "note": " "}]},
    )
    assert "acknowledgement-not-escalated" == validate_review_payload(
        _run("static-budget-exhausted"), "pre-delivery",
        {"acknowledgements": [acknowledgement]},
    )


def test_structured_template_edits_are_rejected_at_manifest_gate():
    assert validate_review_payload(_run(), "manifest", {
        "parameterEdits": [{"templateId": "template-1", "parameter": "review_window", "value": 2}],
    }) == "review-gate"


def test_review_transport_preserves_parameter_edits_and_acknowledgements(monkeypatch):
    captured = {}

    class Response:
        @staticmethod
        def raise_for_status():
            return None

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("cpg_ingester.services.sonataflow_client.requests.post", post)
    edits = [{"templateId": "template-1", "parameter": "review_window", "value": 2}]
    acknowledgements = [{"templateId": "template-2", "note": "Reviewed."}]
    SonataFlowClient("http://workflow.test").send_review(
        "run-1",
        "pre-delivery",
        {"action": "approve", "parameterEdits": edits, "acknowledgements": acknowledgements},
    )

    event = captured["json"]
    assert event["data"]["parameterEdits"] == edits
    assert event["data"]["acknowledgements"] == acknowledgements
    assert event["type"] == "artifacts-reviewed"


def test_unknown_identifiers_never_echo_review_payload():
    sentinel = 'SYNTHETIC-SENSITIVE-REQUEST-CONTENT'
    for field in ('parameterEdits', 'acknowledgements'):
        assert validate_review_payload(_run(), 'pre-delivery', {
            field:[{'templateId':sentinel, 'parameter':sentinel}],
        }) == 'unknown-template'
    assert validate_review_payload(_run(), 'pre-delivery', {
        'parameterEdits':[{'templateId':'template-1','parameter':sentinel,'value':1}],
    }) == 'unknown-parameter'
