"""HTTP client for the decision-engine automation-template registry."""

from __future__ import annotations

import os
from collections.abc import Mapping

import mlflow
import requests

from cpg_contracts.automation.templates import AutomationTemplate


class TemplateClient:
    """Synchronous client used by the LLM-reasoning service."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 10.0):
        self.base_url = (base_url or os.getenv(
            "DECISION_ENGINE_URL", "http://acp-decision-engine:8080"
        )).rstrip("/")
        self.timeout = timeout

    @mlflow.trace(name="automation_template_client.list")
    def list(
        self,
        *,
        recommendation_id: str | None = None,
        dmn_model_id: str | None = None,
    ) -> list[AutomationTemplate]:
        params = {
            key: value
            for key, value in (
                ("recommendation_id", recommendation_id),
                ("dmn_model_id", dmn_model_id),
            )
            if value is not None
        }
        response = requests.get(
            f"{self.base_url}/api/v1/automation/templates",
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("automation registry list response must be a JSON array")
        return [AutomationTemplate.model_validate(item) for item in payload]

    @mlflow.trace(name="automation_template_client.get")
    def get(self, template_id: str, version: str | None = None) -> AutomationTemplate:
        params = {"version": version} if version else None
        response = requests.get(
            f"{self.base_url}/api/v1/automation/templates/{template_id}",
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return AutomationTemplate.model_validate(response.json())

    @mlflow.trace(name="automation_template_client.validate_instance")
    def validate(self, payload: dict) -> dict:
        """Run static and Kogito validation through the decision-engine API."""
        response = requests.post(
            f"{self.base_url}/api/v1/automation/validate",
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code == 503:
            try:
                unavailable = response.json()
            except ValueError:
                unavailable = None
            detail = unavailable.get("detail") if isinstance(unavailable, Mapping) else None
            if isinstance(detail, Mapping) and isinstance(detail.get("bpmn_xml"), str):
                return {
                    "valid": False,
                    "unavailable": True,
                    "bpmn_xml": detail["bpmn_xml"],
                    "messages": [{
                        "severity": "WARNING",
                        "text": str(detail.get("message") or "Decision engine validation unavailable"),
                    }],
                }
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("automation validation response must be a JSON object")
        return result
