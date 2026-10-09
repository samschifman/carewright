#!/usr/bin/env python3
"""Export the canonical JSON Schema for the process automation IR."""

import json
from pathlib import Path

from cpg_contracts.automation.ir import ProcessIR


def main() -> None:
    schema = ProcessIR.model_json_schema()
    output = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "process-ir-1.0.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
