"""Offline tests for selecting a scoped BPMN benchmark run."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


_BENCH = Path(__file__).parent / "benchmarks" / "bpmn"
_SPEC = spec_from_file_location("bpmn_run_benchmark", _BENCH / "run_benchmark.py")
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


TEMPLATES = [
    {"id": "home-bp-monitoring", "section": "3.5 Home Blood Pressure Monitoring"},
    {"id": "acei-lab-follow-up", "section": "3.3 Monitoring Plan"},
    {"id": "follow-up-visit", "section": "3.6 Follow-up Contact and Outreach"},
    {"id": "lifestyle-reassessment", "section": "3.4 Lifestyle Recommendations"},
]


def test_template_filter_selects_home_bp_golden_in_requested_range():
    selected = _MODULE._select_templates(
        TEMPLATES,
        section_range="3.5-3.6",
        golden="home-bp-monitoring",
    )

    assert [template["id"] for template in selected] == ["home-bp-monitoring"]


def test_section_filter_selects_both_matching_sections():
    selected = _MODULE._select_templates(TEMPLATES, section_range="3.5-3.6")

    assert [template["id"] for template in selected] == [
        "home-bp-monitoring",
        "follow-up-visit",
    ]


def test_template_filter_accepts_golden_filename():
    selected = _MODULE._select_templates(
        TEMPLATES,
        golden="/goldens/home-bp-monitoring.template.json",
    )

    assert [template["id"] for template in selected] == ["home-bp-monitoring"]


@pytest.mark.parametrize("section_range", ["3.x", "3.6-3.5"])
def test_invalid_section_range_is_rejected(section_range):
    with pytest.raises(_MODULE.click.BadParameter):
        _MODULE._select_templates(TEMPLATES, section_range=section_range)


def test_llm_config_accepts_openai_key_without_returning_credential(monkeypatch):
    for name in (
        "LLM_API_KEY",
        "LITELLM_API_KEY",
        "LITELLM_URL",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_MODEL", "gpt-6-luna")
    monkeypatch.setenv("OPENAI_API_KEY", "benchmark-test-sentinel")

    config = _MODULE._llm_config()

    assert config == {
        "litellm_url": "https://api.openai.com",
        "llm_model": "gpt-6-luna",
    }
    assert "benchmark-test-sentinel" not in repr(config)
