"""Hand-derived unit examples, not clinically reviewed benchmark goldens."""

import copy
import hashlib
import json

from prompt_eval.models import CaseRef
from prompt_eval.security import digest


def example_case():
    quote = "Adults should walk >=150 minutes/week unless unable to exercise."
    rec = {
        "id": "walk",
        "source_cpg": "unit-cpg",
        "section": "1",
        "title": "Walking",
        "content": quote,
        "recommendation_type": "lifestyle",
        "certainty": {
            "strength": "consensus",
            "evidence_quality": "ungraded",
            "grading_system": None,
            "original_grade": None,
        },
        "source_location": {"page_start": 1, "source_text": quote},
    }
    return {
        "schema_version": "1.0.0",
        "dataset_version": "unit-v1",
        "case_id": "walking",
        "corpus": "unit",
        "split": "tuning",
        "source_cpg": "unit-cpg",
        "section": "1",
        "source_text": quote,
        "source_document_sha256": "a" * 64,
        "transformation_sha256": "b" * 64,
        "items": [{"id": "walk", "section": "1", "title": "Walking"}],
        "grading_definitions": "",
        "abbreviations": {},
        "golden": {
            "contract_version": "1.0",
            "source_cpg": "unit-cpg",
            "recommendations": [rec],
        },
        "annotations": [
            {
                "recommendation_id": "walk",
                "source_quote": quote,
                "action_aliases": ["walk"],
                "required_atoms": [">=150", "minutes/week"],
                "allowed_atoms": [">=150", "minutes/week"],
                "population_phrases": ["Adults"],
                "qualification_phrases": ["unless unable to exercise"],
                "normalization_rationale": "Exact synthetic source wording.",
            }
        ],
        "derivation_status": "draft",
        "assumptions": ["Unit fixture only"],
        "unscored_fields": [],
    }


def case_ref(data):
    golden_bytes = json.dumps(
        data["golden"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return CaseRef(
        case_id=data["case_id"],
        corpus=data["corpus"],
        split=data["split"],
        source_digests={
            "source": hashlib.sha256(data["source_text"].encode()).hexdigest(),
            "golden": hashlib.sha256(golden_bytes).hexdigest(),
            "case": digest(data),
        },
    )


def write_case(root, data=None):
    data = copy.deepcopy(data or example_case())
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{data['case_id']}.json").write_text(json.dumps(data), encoding="utf-8")
    return data, case_ref(data)
