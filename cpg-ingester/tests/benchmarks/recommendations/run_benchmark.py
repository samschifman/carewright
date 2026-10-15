"""Thin consumer entry point: bind model lineage, delegate all lifecycle to prompt_eval."""

import argparse
import sys

from prompt_eval.models import RunConfig
from prompt_eval.security import Redactor, canonical_bytes, traced

from .config_binding import bind_config, configuration
from .extractor_eval import ExtractorAdapter
from .models import EvaluationCase
from .scoring import ExtractionScore

FACTORY = "benchmarks.recommendations.run_benchmark:adapters"


@traced
def adapters(stages):
    config = configuration.get()
    if any(stage.name != "recommendation-extractor" for stage in stages):
        raise ValueError("Only recommendation-extractor is implemented by this layer")
    if config is None or tuple(stages) != config.stages:
        raise ValueError(
            "Adapter requires bound matching run configuration; use recommendation entry point"
        )
    return {
        stage.name: ExtractorAdapter(
            stage, config.model, require_reviewed=config.variant != "development"
        )
        for stage in stages
    }


def main(argv=None):
    from prompt_eval import cli

    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "schema":
        parser = argparse.ArgumentParser()
        parser.add_argument("--model", choices=("case", "score"), required=True)
        args = parser.parse_args(args_list[1:])
        model = EvaluationCase if args.model == "case" else ExtractionScore
        print(canonical_bytes(model.model_json_schema()).decode())
        return 0
    args = cli.parser().parse_args(args_list)
    if args.command != "run":
        return cli.main(args_list)
    try:
        if args.adapter != FACTORY:
            raise ValueError(f"Use --adapter {FACTORY}")
        config = RunConfig.model_validate_json(args.config.read_bytes())
        with bind_config(config):
            return cli.main(args_list)
    except Exception as exc:  # noqa: BLE001 -- sanitized configuration failures only
        print(
            canonical_bytes(
                {
                    "error": {
                        "kind": "configuration",
                        "message": Redactor().text(str(exc)),
                    }
                }
            ).decode()
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
