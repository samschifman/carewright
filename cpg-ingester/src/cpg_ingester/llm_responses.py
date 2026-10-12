"""Write redacted LLM response artifacts for reproducible pipeline debugging."""

import json
import re
from pathlib import Path


_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:OPENAI_API_KEY|LITELLM_API_KEY|LLM_API_KEY)\b\s*[:=]\s*[^\s,;]+"
)
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")


def _safe_component(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")[:80] or "item"


def redact_response(text: str) -> str:
    """Remove credential-shaped strings without reading environment variables."""
    text = _SECRET_ASSIGNMENT_RE.sub("[REDACTED_CREDENTIAL]", text)
    return _API_KEY_RE.sub("[REDACTED_CREDENTIAL]", text)


def persist_llm_response(
    output_dir: str | Path,
    *,
    process_name: str,
    role: str,
    content: str,
) -> Path:
    """Persist each creator/reviewer response under a unique per-role attempt."""
    if role not in {"creator", "reviewer"}:
        raise ValueError(f"unsupported BPMN response role: {role}")
    directory = Path(output_dir) / "bpmn" / "responses" / _safe_component(process_name)
    directory.mkdir(parents=True, exist_ok=True)
    existing = [
        int(match.group(1))
        for path in directory.glob(f"{role}-*.json")
        if (match := re.fullmatch(rf"{role}-(\d+)\.json", path.name))
    ]
    attempt = max(existing, default=0) + 1
    path = directory / f"{role}-{attempt:03d}.json"
    path.write_text(json.dumps({
        "process": process_name,
        "role": role,
        "attempt": attempt,
        "response": redact_response(content),
    }, indent=2) + "\n")
    return path
