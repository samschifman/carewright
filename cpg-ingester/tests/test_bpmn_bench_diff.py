"""Offline tests for ProcessIR golden-diff scoring."""

import sys
from copy import deepcopy
from pathlib import Path

_BENCH = Path(__file__).parent / "benchmarks" / "bpmn"
sys.path.insert(0, str(_BENCH))

from ir_diff import diff_process_ir


def _ir():
    source = {"kind": "source", "source_text": "The CPG says to review."}
    return {
        "process": {
            "acp": {
                "parameters": [
                    {"name": "interval", "type": "duration", "default": "P7D", "unit": "d"}
                ]
            },
            "flowElements": [
                {"id": "start", "type": "startEvent", "name": "Begin"},
                {"id": "request", "type": "task", "taskName": "patient.request_observation",
                 "name": "Request readings", "acp": {"provenance": source}},
                {"id": "end", "type": "endEvent", "name": "Done"},
                {"id": "f_start", "type": "sequenceFlow", "sourceRef": "start",
                 "targetRef": "request"},
                {"id": "f_end", "type": "sequenceFlow", "sourceRef": "request",
                 "targetRef": "end", "acp": {"provenance": source}},
            ],
        }
    }


def test_identical_ir_scores_perfectly():
    ir = _ir()
    result = diff_process_ir(ir, deepcopy(ir))
    assert result["activities"]["f1"] == 1
    assert result["flows"]["precision"] == 1
    assert result["flows"]["recall"] == 1
    assert result["parameters"]["default_accuracy"] == 1
    assert result["parameters"]["unit_accuracy"] == 1
    assert result["grounding"]["coverage"] == 1


def test_activity_and_flow_deltas_reduce_recall_and_precision():
    golden = _ir()
    generated = deepcopy(golden)
    elements = generated["process"]["flowElements"]
    elements[1]["taskName"] = "clinician.notify"
    elements.append({
        "id": "extra", "type": "task", "taskName": "patient.send_message",
        "name": "Send a reminder",
    })
    result = diff_process_ir(golden, generated)
    assert result["activities"]["recall"] < 1
    assert result["activities"]["precision"] < 1
    assert result["flows"]["recall"] < 1


def test_parameter_and_grounding_differences_are_reported():
    golden = _ir()
    generated = deepcopy(golden)
    generated["process"]["acp"]["parameters"][0]["default"] = "P14D"
    generated["process"]["acp"]["parameters"][0]["unit"] = "h"
    generated["process"]["flowElements"][1]["acp"] = {}
    result = diff_process_ir(golden, generated)
    assert result["parameters"]["default_accuracy"] == 0
    assert result["parameters"]["unit_accuracy"] == 0
    assert result["grounding"]["coverage"] == 0
