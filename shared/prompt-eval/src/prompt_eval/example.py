"""Synthetic, no-model adapter. Metrics deliberately mean different things by stage."""

from .models import CaseResult, RunConfig, StageSummary
from .security import digest, traced


def revision(value):
    return {"version": value, "digest": digest(value)}


@traced
def example_config():
    return RunConfig.model_validate(
        {
            "experiment_id": "synthetic-smoke",
            "source_revision": "0" * 40,
            "dataset": revision("synthetic-dataset-v1"),
            "production_behavior": revision("synthetic-no-model-v1"),
            "model": {
                "provider": "synthetic",
                "endpoint": "https://example.invalid",
                "model": "none",
                "service_tier": "none",
                "inference_parameters": {},
            },
            "variant": "development",
            "repetitions": 2,
            "stages": [
                {
                    "name": name,
                    "configuration": revision("synthetic-config-v1"),
                    "parameters": {},
                    "evaluator": revision(f"{name}-v1"),
                    "prompt": revision("no-prompt"),
                }
                for name in ("recommendation-extractor", "recommendation-reviewer")
            ],
            "cases": [
                {
                    "case_id": "one",
                    "corpus": "synthetic",
                    "split": "tuning",
                    "source_digests": {"source": digest("synthetic tuning")},
                },
                {
                    "case_id": "sealed",
                    "corpus": "synthetic",
                    "split": "holdout",
                    "source_digests": {"source": digest("synthetic holdout")},
                },
            ],
        }
    )


class SyntheticAdapter:
    def __init__(self, stage):
        self.metric = "characters" if stage.name.endswith("extractor") else "checks"

    @traced
    def execute(self, case, repetition, capture):
        with capture.attempt({"input": case.case_id}) as attempt:
            response = {"echo": case.case_id}
            attempt.response(response)
            attempt.usage(input_tokens=0, output_tokens=0)
        return CaseResult(
            case_id=case.case_id,
            metrics={self.metric: len(case.case_id)},
            findings=response,
            produced_count=1,
            accounted_count=1,
            artifacts={"synthetic.json": response},
            known_limitations=("Synthetic transport smoke; no model or clinical scoring",),
        )

    @traced
    def validate(self, record):
        return record.usable and self.metric in record.metrics

    @traced
    def summarize(self, records):
        values = [r.metrics[self.metric] for r in records if r.usable]
        return StageSummary(
            metrics={self.metric: sum(values) / len(values) if values else 0},
            known_limitations=("Synthetic transport smoke only",),
        )


@traced
def adapters(stages):
    """Factory receives stage configurations only, never the sealed case manifest."""
    return {stage.name: SyntheticAdapter(stage) for stage in stages}
