"""Capability catalog contract tests."""

from __future__ import annotations

import re

import pytest

from cpg_contracts.automation.catalog import (
    RESERVED_TASK_NAMES,
    Catalog,
    load_catalog,
)


ALLOWED_TYPES = {
    "code",
    "duration",
    "integer",
    "decimal",
    "boolean",
    "string",
    "quantity",
    "enum",
    "unit",
}


def test_catalog_loads_eight_unique_capabilities() -> None:
    catalog = load_catalog()
    ids = [capability.id for capability in catalog.capabilities]

    assert catalog.version == "1.0"
    assert len(ids) == 8
    assert len(ids) == len(set(ids))
    assert set(ids) == {
        "patient.request_observation",
        "patient.send_message",
        "ehr.query_observations",
        "ehr.query_results",
        "ehr.query_orders",
        "clinician.notify",
        "care_team.escalate",
        "schedule.request_visit",
    }


def test_capability_ids_follow_the_catalog_grammar_and_avoid_reserved_names() -> None:
    catalog = load_catalog()
    for capability in catalog.capabilities:
        assert re.fullmatch(r"[a-z_]+\.[a-z_]+", capability.id)
        assert capability.id not in RESERVED_TASK_NAMES


def test_every_catalog_io_type_is_allowed() -> None:
    catalog = load_catalog()
    for capability in catalog.capabilities:
        for item in [*capability.inputs, *capability.outputs]:
            assert item.type in ALLOWED_TYPES


def test_enum_inputs_have_at_least_two_values() -> None:
    catalog = load_catalog()
    enum_inputs = [
        item
        for capability in catalog.capabilities
        for item in capability.inputs
        if item.type == "enum"
    ]

    assert enum_inputs
    assert all(item.values is not None and len(item.values) >= 2 for item in enum_inputs)
    assert catalog.get("patient.send_message").inputs[1].values == [
        "reminder",
        "education",
        "result",
    ]
    assert catalog.get("clinician.notify").inputs[0].values == [
        "routine",
        "urgent",
        "emergency",
    ]
    assert next(
        item for item in catalog.get("clinician.notify").inputs if item.name == "context"
    ).required is False


def test_catalog_marks_free_text_strings_and_only_those_inputs() -> None:
    catalog = load_catalog()
    free_text_inputs = {
        (capability.id, item.name)
        for capability in catalog.capabilities
        for item in capability.inputs
        if item.free_text
    }

    assert free_text_inputs == {
        ("patient.request_observation", "instructions"),
        ("patient.send_message", "message"),
        ("clinician.notify", "reason"),
        ("clinician.notify", "context"),
        ("care_team.escalate", "reason"),
        ("schedule.request_visit", "reason"),
    }
    for capability_id, input_name in free_text_inputs:
        item = next(
            input_
            for input_ in catalog.get(capability_id).inputs
            if input_.name == input_name
        )
        assert item.type == "string"


def test_observation_catalog_shape_defaults_and_get() -> None:
    catalog = load_catalog("1.0")
    observation = catalog.get("ehr.query_observations")
    inputs = {item.name: item for item in observation.inputs}
    request_observation_inputs = {
        item.name: item for item in catalog.get("patient.request_observation").inputs
    }

    assert observation.version == "1.0"
    assert inputs["unit"].type == "unit"
    assert inputs["min_count"].default == 1
    assert inputs["min_count"].required is False
    assert request_observation_inputs["channel_policy"].default == "preferred"
    assert request_observation_inputs["channel_policy"].required is False
    assert {item.name: item.type for item in observation.outputs} == {
        "count": "integer",
        "latest_value": "quantity",
        "latest_date": "string",
        "average_value": "quantity",
    }
    with pytest.raises(KeyError):
        catalog.get("unknown.capability")


def test_loader_rejects_unknown_catalog_versions() -> None:
    with pytest.raises(ValueError, match="unsupported capability catalog version"):
        load_catalog("2.0")


def test_catalog_api_is_traced_when_mlflow_is_installed() -> None:
    pytest.importorskip("mlflow")
    assert getattr(load_catalog, "__mlflow_traced__", False)
    assert getattr(Catalog.get, "__mlflow_traced__", False)
