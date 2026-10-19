"""Tests for local DMN and automation-template deployment commands."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from click.testing import CliRunner

from cpg_ingester.deploy import deploy_template, main

GOLDEN_TEMPLATE = (
    Path(__file__).parent.parent
    / "data"
    / "golden"
    / "automation"
    / "home-bp-monitoring.template.json"
)


def _response(payload=None, *, status=201, body=""):
    response = MagicMock()
    response.status_code = status
    response.text = body
    response.json.return_value = payload or {}
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP {status}", response=response
        )
    return response


def test_deploy_template_posts_json_with_source_cpg():
    payload = json.loads(GOLDEN_TEMPLATE.read_text())
    response = _response({"summary": {"id": "home-bp-monitoring", "version": "abc"}})
    with patch("cpg_ingester.deploy.requests.post", return_value=response) as post:
        result = deploy_template(
            GOLDEN_TEMPLATE, "http://localhost:8082", source_cpg="SYN-HTN-2026-002"
        )

    post.assert_called_once_with(
        "http://localhost:8082/api/v1/automation/templates",
        json=payload,
        params={"source_cpg": "SYN-HTN-2026-002"},
        timeout=30,
    )
    assert result["summary"]["id"] == "home-bp-monitoring"


def test_template_option_is_repeatable():
    runner = CliRunner()
    response = _response({"summary": {"name": "Home BP monitoring"}})
    args = [
        "--source-cpg", "SYN-HTN-2026-002",
        "--template", str(GOLDEN_TEMPLATE),
        "--template", str(GOLDEN_TEMPLATE),
    ]
    with patch("cpg_ingester.deploy.requests.post", return_value=response) as post:
        result = runner.invoke(main, args)

    assert result.exit_code == 0, result.output
    assert post.call_count == 2
    assert result.output.count("Deployed template: Home BP monitoring") == 2


def test_template_422_prints_response_and_exits_nonzero():
    runner = CliRunner()
    response = _response(
        status=422,
        body='{"messages":[{"severity":"ERROR","text":"invalid BPMN"}]}',
    )
    with patch("cpg_ingester.deploy.requests.post", return_value=response):
        result = runner.invoke(
            main,
            ["--source-cpg", "SYN-HTN-2026-002", "--template", str(GOLDEN_TEMPLATE)],
        )

    assert result.exit_code == 1
    assert "invalid BPMN" in result.output


def test_template_rejection_does_not_prevent_later_registration(tmp_path):
    first = tmp_path / "rejected.template.json"
    second = tmp_path / "accepted.template.json"
    first.write_text(GOLDEN_TEMPLATE.read_text())
    second.write_text(GOLDEN_TEMPLATE.read_text())
    rejected = _response(
        status=422,
        body='{"messages":[{"severity":"ERROR","text":"enum mismatch"}]}',
    )
    accepted = _response({"summary": {"name": "Accepted template"}})
    runner = CliRunner()

    with patch(
        "cpg_ingester.deploy.requests.post", side_effect=[rejected, accepted]
    ) as post:
        result = runner.invoke(main, [
            "--template", str(first),
            "--template", str(second),
        ])

    assert result.exit_code == 1
    assert post.call_count == 2
    assert "enum mismatch" in result.output
    assert "Deployed template: Accepted template" in result.output
    assert "Template registration failures:" in result.output
    assert str(first) in result.output
