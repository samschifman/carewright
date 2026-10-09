"""Keep the checked-in ProcessIR schema synchronized with its exporter."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


SHARED_ROOT = Path(__file__).resolve().parents[1]


def test_checked_in_process_ir_schema_matches_exporter(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(SHARED_ROOT / "scripts" / "export-ir-schema.py", scripts_dir)
    (tmp_path / "schemas").mkdir()

    subprocess.run(
        [sys.executable, str(scripts_dir / "export-ir-schema.py")],
        check=True,
        cwd=SHARED_ROOT,
    )

    checked_in = (SHARED_ROOT / "schemas" / "process-ir-1.0.json").read_text(
        encoding="utf-8"
    )
    exported = (tmp_path / "schemas" / "process-ir-1.0.json").read_text(encoding="utf-8")
    assert checked_in == exported
