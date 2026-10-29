"""Load only the selected case and verify its declared source identities."""

import hashlib
import json
from pathlib import Path

from prompt_eval.models import CaseRef
from prompt_eval.security import digest, traced

from .models import EvaluationCase


@traced
def load_case(
    ref: CaseRef, case_root: Path, *, require_reviewed: bool = False
) -> EvaluationCase:
    root = case_root.resolve(strict=True)
    path = (root / f"{ref.case_id}.json").resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("Case path escapes root")
    data = json.loads(path.read_text(encoding="utf-8"))
    case = EvaluationCase.model_validate(data)
    if (case.case_id, case.corpus, case.split) != (ref.case_id, ref.corpus, ref.split):
        raise ValueError("Case identity differs from CaseRef")
    actual = {
        "source": hashlib.sha256(case.source_text.encode("utf-8")).hexdigest(),
        "golden": digest(data["golden"]),
        "case": digest(data),
    }
    for name, value in actual.items():
        if ref.source_digests.get(name) != value:
            raise ValueError(f"{name} digest mismatch")
    if require_reviewed and case.derivation_status != "reviewed":
        raise ValueError("Official cases must have reviewed goldens")
    return case
