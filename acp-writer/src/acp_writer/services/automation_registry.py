"""Validated, immutable storage for automation template versions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import mlflow
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.ir import BusinessRuleTask, ir_version
from cpg_contracts.automation.templates import AutomationTemplate
from cpg_contracts.automation.validators.ladder import run_static_ladder
from cpg_contracts.decisions import DecisionModelSummary


@dataclass(frozen=True)
class RegisterResult:
    """Result of validating and optionally storing a template."""

    template: AutomationTemplate
    status_code: int
    validate_only: bool = False


class RegistryError(Exception):
    """An expected registry rejection with an HTTP-compatible status."""

    def __init__(self, status_code: int, code: str, detail: object):
        super().__init__(str(detail))
        self.status_code = status_code
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict:
        return {"error": self.code, "detail": self.detail}


class AutomationRegistry:
    """In-memory registry retaining every accepted immutable version."""

    def __init__(self) -> None:
        self._templates: dict[tuple[str, str], AutomationTemplate] = {}
        self._latest: dict[str, str] = {}

    @mlflow.trace(name="automation_registry.register")
    def register(
        self,
        template: AutomationTemplate,
        *,
        source_cpg: str,
        replace: bool = False,
        validate_only: bool = False,
        bpmn_validator: Callable[[str], dict | None],
        dmn_namespaces: dict[str, str] | None = None,
        dmn_summaries: dict[str, DecisionModelSummary] | None = None,
    ) -> RegisterResult:
        """Validate all intake gates before adding a version to the registry."""
        if template.summary.source_cpg != source_cpg:
            raise RegistryError(
                422,
                "source-cpg-mismatch",
                "source_cpg query parameter must match the template summary",
            )

        linked_model_ids = set(template.summary.linked_decision_model_ids)
        linked_model_ids.update(
            element.dmnModel
            for element in template.ir.process.flowElements
            if isinstance(element, BusinessRuleTask)
        )
        registered_namespaces = dmn_namespaces or {}
        template = template.model_copy(update={
            "dmn_namespaces": {
                model_id: registered_namespaces[model_id]
                for model_id in sorted(linked_model_ids)
                if model_id in registered_namespaces
            }
        })

        try:
            compiled = compile(template.ir, dmn_namespaces=template.dmn_namespaces)
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistryError(422, "ir-compile-failed", str(exc)) from exc
        if compiled != template.bpmn_xml:
            raise RegistryError(422, "ir-xml-mismatch", "compiled IR differs from bpmn_xml")

        ladder = run_static_ladder(
            template.ir,
            catalog=load_catalog(),
            stage="template",
            dmn_summaries=dmn_summaries,
            dmn_namespaces=template.dmn_namespaces,
        )
        if not ladder.ok:
            raise RegistryError(
                422,
                "static-validation-failed",
                [finding.__dict__ for finding in ladder.findings],
            )
        expected_rungs = {"L1", "L2", "L3", "L4", "L5a"}
        missing = sorted(expected_rungs - set(ladder.rungs_passed))
        if missing:
            raise RegistryError(422, "static-validation-incomplete", {"missing_rungs": missing})
        if ir_version(template.ir) != template.summary.version:
            raise RegistryError(422, "ir-version-mismatch", "summary version does not match IR hash")
        if template.summary.validation.status != "valid" or "L5b" not in template.summary.validation.rungs_passed:
            raise RegistryError(
                422,
                "text-validation-missing",
                "template must carry a successful L5b source-grounding result",
            )

        engine_result = bpmn_validator(template.bpmn_xml)
        if engine_result is None:
            raise RegistryError(503, "kogito-unavailable", "Kogito BPMN validation is unavailable")
        if not engine_result.get("valid", False):
            raise RegistryError(422, "kogito-validation-failed", engine_result.get("messages", []))

        if validate_only:
            return RegisterResult(template=template, status_code=200, validate_only=True)

        current_version = self._latest.get(template.summary.id)
        current = self._templates.get((template.summary.id, current_version)) if current_version else None
        if current and current.summary.source_cpg != source_cpg and not replace:
            raise RegistryError(
                409,
                "template-id-collision",
                {
                    "template_id": template.summary.id,
                    "existing_source_cpg": current.summary.source_cpg,
                    "source_cpg": source_cpg,
                },
            )

        key = (template.summary.id, template.summary.version)
        existing_version = self._templates.get(key)
        if existing_version is not None:
            if existing_version.model_dump(mode="json") != template.model_dump(mode="json"):
                raise RegistryError(
                    409,
                    "template-version-immutable",
                    "an existing template version cannot be overwritten",
                )
            if not validate_only:
                self._latest[template.summary.id] = template.summary.version
            return RegisterResult(template=existing_version, status_code=200, validate_only=validate_only)

        self._templates[key] = template
        self._latest[template.summary.id] = template.summary.version
        return RegisterResult(template=template, status_code=201)

    def get(self, template_id: str, version: str | None = None) -> AutomationTemplate | None:
        """Return a pinned version, or the current version when omitted."""
        selected = version or self._latest.get(template_id)
        if selected is None:
            return None
        return self._templates.get((template_id, selected))

    def list(
        self,
        *,
        recommendation_id: str | None = None,
        dmn_model_id: str | None = None,
    ) -> list[AutomationTemplate]:
        """List latest versions, optionally filtered by linked artifact ids."""
        templates = [
            self._templates[(template_id, version)]
            for template_id, version in self._latest.items()
        ]
        if recommendation_id is not None:
            templates = [
                item for item in templates
                if recommendation_id in item.summary.linked_recommendation_ids
            ]
        if dmn_model_id is not None:
            templates = [
                item for item in templates
                if dmn_model_id in item.summary.linked_decision_model_ids
            ]
        return sorted(templates, key=lambda item: item.summary.id)


router = APIRouter()


@router.post("/api/v1/automation/templates")
@mlflow.trace(name="automation.register_template")
async def register_automation_template(
    request: Request,
    source_cpg: str,
    validate_only: bool = False,
    replace: bool = False,
):
    try:
        template = AutomationTemplate.model_validate_json(await request.body())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid automation template: {exc}")
    try:
        result = request.app.state.automation_registry.register(
            template,
            source_cpg=source_cpg,
            replace=replace,
            validate_only=validate_only,
            bpmn_validator=request.app.state.bpmn_validator,
            dmn_namespaces=request.app.state.dmn_namespaces(),
            dmn_summaries=request.app.state.dmn_summaries(),
        )
    except RegistryError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.as_dict())
    if validate_only:
        return JSONResponse(status_code=200, content={
            "valid": True,
            "template_id": result.template.summary.id,
            "version": result.template.summary.version,
        })
    return JSONResponse(
        status_code=result.status_code,
        content=result.template.model_dump(mode="json"),
    )


@router.get("/api/v1/automation/templates")
@mlflow.trace(name="automation.list_templates")
def list_automation_templates(
    request: Request,
    recommendation_id: str | None = None,
    dmn_model_id: str | None = None,
):
    return [
        item.model_dump(mode="json")
        for item in request.app.state.automation_registry.list(
            recommendation_id=recommendation_id,
            dmn_model_id=dmn_model_id,
        )
    ]


@router.get("/api/v1/automation/templates/{template_id}")
@mlflow.trace(name="automation.get_template")
def get_automation_template(request: Request, template_id: str, version: str | None = None):
    template = request.app.state.automation_registry.get(template_id, version)
    if template is None:
        raise HTTPException(status_code=404, detail="Automation template version not found")
    return template.model_dump(mode="json")


@router.post("/api/v1/automation/validate")
@mlflow.trace(name="automation.validate_endpoint")
async def validate_automation(request: Request):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return request.app.state.automation_validation_result(payload)
