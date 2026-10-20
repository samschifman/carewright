#!/usr/bin/env python3
"""Render the operator workflow resources as local SonataFlow dev-mode files."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {
    "cpg-ingester": REPO_ROOT / "cpg-ingester/deploy/orchestrator/cpg-ingester-workflow.yaml",
    "acp-writer": REPO_ROOT / "acp-writer/deploy/orchestrator/acp-writer-workflow.yaml",
}
WORKFLOW_PROPERTIES = (
    REPO_ROOT / "cpg-ingester/deploy/orchestrator/cpgingester-props.yaml",
    REPO_ROOT / "acp-writer/deploy/orchestrator/acpwriter-props.yaml",
)
HOST_REWRITES = {
    "cpg-ingester-llm-analysis": "cpg-ingester-llm",
    "cpgingester": "sonataflow-devmode",
    "acpwriter": "sonataflow-devmode",
    "acp-bff": "acp-writer-bff",
}
URL_PATTERN = re.compile(r"(?P<scheme>https?://)(?P<host>[A-Za-z0-9_.-]+)(?::(?P<port>[0-9]+))?")


def _rewrite_url_hosts(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        host = match.group("host")
        rewritten = HOST_REWRITES.get(host, host)
        port = match.group("port")
        if host in {"cpgingester", "acpwriter"} and port == "80":
            port = "8080"
        suffix = f":{port}" if port else ""
        return f"{match.group('scheme')}{rewritten}{suffix}"

    return URL_PATTERN.sub(replace, value)


def _rewrite_tree(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_tree(item) for item in value]
    if isinstance(value, str):
        return _rewrite_url_hosts(value)
    return value


def render_workflows(output_dir: Path, repo_root: Path = REPO_ROOT) -> list[Path]:
    """Render both SonataFlow CRs into standalone workflow files for dev-mode."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[Path] = []
    for name, source in WORKFLOWS.items():
        source = repo_root / source.relative_to(REPO_ROOT)
        resource = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(resource, dict) or resource.get("kind") != "SonataFlow":
            raise ValueError(f"{source} is not a SonataFlow resource")
        metadata = resource.get("metadata") or {}
        flow = (resource.get("spec") or {}).get("flow")
        if not isinstance(flow, dict):
            raise ValueError(f"{source} has no spec.flow mapping")
        annotations = metadata.get("annotations") or {}
        workflow = dict(flow)
        workflow["id"] = metadata.get("name")
        workflow["version"] = str(annotations.get("sonataflow.org/version", "1.0.0"))
        workflow = _rewrite_tree(workflow)
        target = output_dir / f"{name}.sw.yaml"
        target.write_text(
            yaml.safe_dump(workflow, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        rendered.append(target)
    properties: dict[str, str] = {}
    for source in WORKFLOW_PROPERTIES:
        source = repo_root / source.relative_to(REPO_ROOT)
        resource = yaml.safe_load(source.read_text(encoding="utf-8"))
        content = (resource.get("data") or {}).get("application.properties")
        if not isinstance(content, str):
            raise ValueError(f"{source} has no application.properties content")
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, separator, value = stripped.partition("=")
            if not separator:
                raise ValueError(f"Invalid application property in {source}: {stripped}")
            prior = properties.setdefault(key.strip(), value.strip())
            if prior != value.strip():
                raise ValueError(f"Conflicting application property {key!r} in {source}")
    properties_path = output_dir / "application.properties"
    properties_path.write_text(
        "\n".join(f"{key}={value}" for key, value in properties.items()) + "\n",
        encoding="utf-8",
    )
    return rendered


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "working/orchestrated-workflows",
    )
    args = parser.parse_args()
    for path in render_workflows(args.output_dir):
        print(path)


if __name__ == "__main__":
    main()
