"""Review rounds must make zero vision calls.

The LLM-analysis service caches the enriched parse (markdown + interpreted
figures) next to the parse result, keyed off ``parse_result_ref``. A second
analyze on the same ref (a review round) must reuse the cache and skip
``figure_interpreter`` entirely, so its markdown is byte-identical to round 1's.
"""

import cpg_ingester.services.llm_analysis as svc

_PARSE_REF = "cpg-artifacts:run-abc/parse_result.json"
_ENRICHED_REF = "cpg-artifacts:run-abc/parse_result_enriched.json"


class _FakeStore:
    """In-memory artifact store keyed by qualified ``bucket:key`` ref."""

    bucket = "cpg-artifacts"

    def __init__(self):
        self.data: dict = {}
        self.put_keys: list[str] = []

    def put(self, key, data):
        ref = f"{self.bucket}:{key}"
        self.data[ref] = data
        self.put_keys.append(key)
        return ref

    def get(self, ref):
        key = ref if ":" in ref and not ref.startswith("s3://") else f"{self.bucket}:{ref}"
        if key not in self.data:
            raise KeyError(key)  # mimic an S3 NoSuchKey miss
        return self.data[key]


def _stub_downstream(monkeypatch):
    """No-op the analysis nodes so _do_analyze runs fast and deterministically."""
    for name in (
        "structure_analyzer",
        "content_filter",
        "item_identifier",
        "classification_reviewer",
        "metadata_extractor",
    ):
        monkeypatch.setattr(svc, name, lambda s: {})


def _seed_parse(store, figures=None):
    store.put(
        "run-abc/parse_result.json",
        {
            "markdown": "# raw\n\n<!-- image -->",
            "docling_json": {},
            "figures": figures if figures is not None else [
                {"id": "fig-001", "classification": "flow_chart", "reading_order_index": 0}
            ],
        },
    )


def _spy_interpreter(calls):
    def _fake(state):
        calls.append(1)
        figs = [dict(f, interpretation={"description": "d"}) for f in state["figures"]]
        return {"markdown": "ENRICHED round-1", "figures": figs}

    return _fake


def _markdown_of(store, result):
    """Resolve the analysis_result_ref a store-backed _do_analyze returns."""
    return store.get(result["analysis_result_ref"])["markdown"]


class TestEnrichedRefDerivation:
    def test_derives_sibling_key(self):
        assert svc._enriched_ref(_PARSE_REF) == _ENRICHED_REF

    def test_none_without_ref(self):
        assert svc._enriched_ref(None) is None

    def test_none_on_unexpected_shape(self):
        assert svc._enriched_ref("cpg-artifacts:run-abc/something-else.json") is None

    def test_ref_key_strips_bucket(self):
        assert svc._ref_key(_ENRICHED_REF) == "run-abc/parse_result_enriched.json"


class TestReviewRoundCaching:
    def test_first_round_interprets_and_caches(self, monkeypatch):
        store = _FakeStore()
        _seed_parse(store)
        monkeypatch.setattr(svc, "_store", store)
        _stub_downstream(monkeypatch)
        calls = []
        monkeypatch.setattr(svc, "figure_interpreter", _spy_interpreter(calls))

        result = svc._do_analyze({"parse_result_ref": _PARSE_REF})

        assert len(calls) == 1  # interpreter ran on the first pass
        assert "run-abc/parse_result_enriched.json" in store.put_keys
        cached = store.get(_ENRICHED_REF)
        assert cached["markdown"] == "ENRICHED round-1"
        assert cached["figures"][0]["interpretation"] == {"description": "d"}
        analysis = store.get(result["analysis_result_ref"])
        assert analysis["figures"][0]["interpretation"] == {"description": "d"}
        assert _markdown_of(store, result) == "ENRICHED round-1"

    def test_second_round_reuses_cache_no_vision(self, monkeypatch):
        store = _FakeStore()
        _seed_parse(store)
        monkeypatch.setattr(svc, "_store", store)
        _stub_downstream(monkeypatch)
        calls = []
        monkeypatch.setattr(svc, "figure_interpreter", _spy_interpreter(calls))

        r1 = svc._do_analyze({"parse_result_ref": _PARSE_REF})
        r2 = svc._do_analyze(
            {"parse_result_ref": _PARSE_REF, "review_feedback": "fix X", "review_iteration": 1}
        )

        assert len(calls) == 1  # interpreter NOT called again on the review round
        assert _markdown_of(store, r2) == _markdown_of(store, r1) == "ENRICHED round-1"

    def test_corrupt_cache_falls_back_to_interpreting(self, monkeypatch):
        store = _FakeStore()
        _seed_parse(store)
        # Enriched entry exists but is malformed (no "markdown") — must not be trusted.
        store.put("run-abc/parse_result_enriched.json", {"garbage": 1})
        monkeypatch.setattr(svc, "_store", store)
        _stub_downstream(monkeypatch)
        calls = []
        monkeypatch.setattr(svc, "figure_interpreter", _spy_interpreter(calls))

        result = svc._do_analyze({"parse_result_ref": _PARSE_REF})

        assert len(calls) == 1  # fell back to interpreting, no crash
        assert _markdown_of(store, result) == "ENRICHED round-1"
