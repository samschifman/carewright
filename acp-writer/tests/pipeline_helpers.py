"""Test-only graph builder wired to the monolith's in-process dependencies."""


def build_pipeline():
    from acp_writer import api
    from acp_writer.pipeline import build_pipeline as build

    return build(
        automation_registry=api._automation_registry,
        automation_validator=api._automation_validation_result,
        guidelines_store=api._guidelines_store,
        vector_store=api._vector_store,
        decision_models_provider=lambda: [
            entry["summary"].model_dump(mode="json")
            for entry in api._dynamic_models.values()
        ],
    )
