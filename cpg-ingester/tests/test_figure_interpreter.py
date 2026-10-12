"""Tests for the Figure Interpreter node.

Pure-helper tests run offline. The node test mocks the vision LLM so it never
calls out — a live end-to-end run against a real vision model is exercised by
the benchmark's ``--interpret`` path, not here.
"""

import base64
from unittest.mock import MagicMock, patch

import pytest

from cpg_ingester.nodes.figure_interpreter import (
    IMAGE_PLACEHOLDER,
    _inline_interpretations,
    _interpretation_enabled,
    _is_flowchart,
    _is_trivial,
    _render_block,
    _resolve_figure_png_b64,
    _validate_mermaid,
    figure_interpreter,
)

# A tiny valid PNG (1x1) so bitmap-resolution paths have real bytes.
_PNG_1x1 = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
).decode("ascii")

_GOOD_MERMAID = "flowchart TD\n  A[Start] -->|Yes| B{Decide}\n  B -->|No| C[Stop]"


class TestClassHelpers:
    def test_flowchart_detection(self):
        assert _is_flowchart("flow_chart")
        assert _is_flowchart("flowchart")
        assert not _is_flowchart("bar_chart")

    def test_trivial_detection(self):
        assert _is_trivial("logo")
        assert _is_trivial("stamp")
        assert not _is_trivial("flow_chart")


class TestValidateMermaid:
    def test_structural_ok(self):
        # mmdc is typically absent in CI → structural check applies.
        ok, _ = _validate_mermaid(_GOOD_MERMAID)
        assert ok

    def test_bad_header(self):
        ok, detail = _validate_mermaid("not a diagram\nA -> B")
        assert not ok
        assert "header" in detail

    def test_no_edges(self):
        ok, detail = _validate_mermaid("flowchart TD\n  A[Only a node]")
        assert not ok

    def test_empty(self):
        ok, _ = _validate_mermaid("")
        assert not ok


class TestResolveBitmap:
    def test_from_figure_images(self):
        fig = {"id": "fig-001"}
        assert _resolve_figure_png_b64(fig, {"fig-001": _PNG_1x1}) == _PNG_1x1

    def test_from_inline_b64(self):
        fig = {"id": "fig-001", "image_b64": _PNG_1x1}
        assert _resolve_figure_png_b64(fig, {}) == _PNG_1x1

    def test_from_image_ref_via_store(self):
        # The caller resolves the store once and passes it in.
        fig = {"id": "fig-001", "image_ref": "cpg-artifacts:x/fig-001.png"}
        store = MagicMock()
        store.get_raw.return_value = base64.b64decode(_PNG_1x1)
        assert _resolve_figure_png_b64(fig, {}, store) == _PNG_1x1
        store.get_raw.assert_called_once_with("cpg-artifacts:x/fig-001.png")

    def test_image_ref_without_store_returns_none(self):
        fig = {"id": "fig-001", "image_ref": "cpg-artifacts:x/fig-001.png"}
        assert _resolve_figure_png_b64(fig, {}, None) is None

    def test_none_when_no_bitmap(self):
        assert _resolve_figure_png_b64({"id": "fig-001"}, {}) is None


class TestInlineInterpretations:
    def _figs(self):
        return [
            {"id": "fig-001", "classification": "flow_chart", "page": 1, "reading_order_index": 0},
            {"id": "fig-002", "classification": "flow_chart", "page": 2, "reading_order_index": 1},
        ]

    def test_positional_splice_places_each_at_its_own_slot(self):
        md = f"# A\n\n{IMAGE_PLACEHOLDER}\n\n# B\n\n{IMAGE_PLACEHOLDER}\n\n# Notes\n"
        interps = {
            "fig-001": {"description": "First chart.", "mermaid": _GOOD_MERMAID, "mermaid_valid": True},
            "fig-002": {"description": "Second chart.", "mermaid": _GOOD_MERMAID, "mermaid_valid": True},
        }
        out = _inline_interpretations(md, self._figs(), interps)
        assert IMAGE_PLACEHOLDER not in out
        # Each interpretation lands in reading order between the right headings.
        assert out.index("First chart.") < out.index("# B") < out.index("Second chart.")
        assert out.index("Second chart.") < out.index("# Notes")
        assert out.count("```mermaid") == 2

    def test_uninterpreted_figure_keeps_placeholder(self):
        md = f"{IMAGE_PLACEHOLDER}\n\n{IMAGE_PLACEHOLDER}"
        interps = {"fig-001": {"description": "Only the first."}}
        out = _inline_interpretations(md, self._figs(), interps)
        assert "Only the first." in out
        assert out.count(IMAGE_PLACEHOLDER) == 1  # fig-002 slot untouched

    def _figs_with_self_ref(self):
        return [
            {"id": "fig-001", "self_ref": "#/pictures/0", "classification": "flow_chart",
             "page": 1, "reading_order_index": 0},
            {"id": "fig-002", "self_ref": "#/pictures/1", "classification": "flow_chart",
             "page": 2, "reading_order_index": 1},
        ]

    def test_body_order_match_splices_positionally(self):
        md = f"# A\n\n{IMAGE_PLACEHOLDER}\n\n# B\n\n{IMAGE_PLACEHOLDER}\n"
        interps = {
            "fig-001": {"description": "First chart."},
            "fig-002": {"description": "Second chart."},
        }
        docling_json = {"body": {"children": [
            {"$ref": "#/texts/0"},
            {"$ref": "#/pictures/0"},
            {"$ref": "#/pictures/1"},
        ]}}
        out = _inline_interpretations(md, self._figs_with_self_ref(), interps, docling_json)
        assert IMAGE_PLACEHOLDER not in out
        assert out.index("First chart.") < out.index("Second chart.")

    def test_body_order_mismatch_falls_back_to_appendix(self):
        # docling body lists the pictures in the OPPOSITE order to the figures
        # index — a positional splice would attach the wrong Mermaid to the wrong
        # figure, so we must fall back to the appendix and leave placeholders.
        md = f"# A\n\n{IMAGE_PLACEHOLDER}\n\n# B\n\n{IMAGE_PLACEHOLDER}\n"
        interps = {
            "fig-001": {"description": "First chart."},
            "fig-002": {"description": "Second chart."},
        }
        docling_json = {"body": {"children": [
            {"$ref": "#/texts/0"},
            {"$ref": "#/pictures/1"},
            {"$ref": "#/pictures/0"},
        ]}}
        out = _inline_interpretations(md, self._figs_with_self_ref(), interps, docling_json)
        assert "## Figure Interpretations" in out
        assert out.count(IMAGE_PLACEHOLDER) == 2  # placeholders untouched
        assert "First chart." in out and "Second chart." in out

    def test_missing_self_ref_falls_back_when_body_present(self):
        md = f"# A\n\n{IMAGE_PLACEHOLDER}\n\n# B\n\n{IMAGE_PLACEHOLDER}\n"
        figs = self._figs()  # no self_ref on these
        interps = {"fig-001": {"description": "First."}, "fig-002": {"description": "Second."}}
        docling_json = {"body": {"children": [
            {"$ref": "#/pictures/0"}, {"$ref": "#/pictures/1"},
        ]}}
        out = _inline_interpretations(md, figs, interps, docling_json)
        assert "## Figure Interpretations" in out

    def test_count_mismatch_falls_back_to_appendix(self):
        # One placeholder but two figures → do not guess; append an appendix.
        md = f"# A\n\n{IMAGE_PLACEHOLDER}\n"
        interps = {
            "fig-001": {"description": "First."},
            "fig-002": {"description": "Second."},
        }
        out = _inline_interpretations(md, self._figs(), interps)
        assert "## Figure Interpretations" in out
        assert "First." in out and "Second." in out
        assert out.count(IMAGE_PLACEHOLDER) == 1  # original placeholder untouched


class TestRenderBlock:
    def test_includes_valid_mermaid(self):
        fig = {"id": "fig-001", "classification": "flow_chart", "page": 1}
        block = _render_block(fig, {"description": "Walk.", "mermaid": _GOOD_MERMAID, "mermaid_valid": True})
        assert "Figure fig-001" in block
        assert "Walk." in block
        assert "```mermaid" in block

    def test_omits_invalid_mermaid(self):
        fig = {"id": "fig-001", "classification": "flow_chart", "page": 1}
        block = _render_block(fig, {"description": "Walk.", "mermaid": "broken", "mermaid_valid": False})
        assert "```mermaid" not in block
        assert "Walk." in block


class TestFigureInterpreterNode:
    def _state_two_flowcharts(self):
        md = f"# A\n\n{IMAGE_PLACEHOLDER}\n\n# B\n\n{IMAGE_PLACEHOLDER}\n"
        figures = [
            {"id": "fig-001", "classification": "flow_chart", "page": 1, "reading_order_index": 0},
            {"id": "fig-002", "classification": "flow_chart", "page": 2, "reading_order_index": 1},
        ]
        figure_images = {"fig-001": _PNG_1x1, "fig-002": _PNG_1x1}
        return {"markdown": md, "figures": figures, "figure_images": figure_images}

    def test_interprets_and_inlines_distinct_outputs(self):
        import json

        replies = [
            json.dumps({"description": "Condition C chart.", "mermaid": _GOOD_MERMAID,
                        "nodes": ["Start", "Decide", "Stop"], "edges": []}),
            json.dumps({"description": "AFib chart.", "mermaid": _GOOD_MERMAID,
                        "nodes": ["Start", "Decide", "Stop"], "edges": []}),
        ]
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(side_effect=[MagicMock(content=r, usage_metadata=None) for r in replies])

        state = self._state_two_flowcharts()
        with patch("cpg_ingester.nodes.figure_interpreter.get_llm", return_value=mock_llm):
            result = figure_interpreter(state)

        assert mock_llm.invoke.call_count == 2
        md = result["markdown"]
        assert IMAGE_PLACEHOLDER not in md
        assert md.index("Condition C chart.") < md.index("AFib chart.")
        assert md.count("```mermaid") == 2
        # Figures annotated with their interpretation.
        by_id = {f["id"]: f for f in result["figures"]}
        assert by_id["fig-001"]["interpretation"]["description"] == "Condition C chart."
        assert by_id["fig-002"]["interpretation"]["mermaid_valid"] is True

    def test_trivial_class_skips_llm(self):
        state = {
            "markdown": IMAGE_PLACEHOLDER,
            "figures": [{"id": "fig-001", "classification": "logo", "reading_order_index": 0}],
            "figure_images": {"fig-001": _PNG_1x1},
        }
        mock_llm = MagicMock()
        with patch("cpg_ingester.nodes.figure_interpreter.get_llm", return_value=mock_llm):
            result = figure_interpreter(state)
        mock_llm.invoke.assert_not_called()
        assert result["figures"][0]["interpretation"] == {"label": "logo"}

    def test_already_interpreted_figure_skips_llm(self):
        # Belt-and-braces: a figure re-fed with an existing
        # interpretation must not trigger a second vision call.
        state = {
            "markdown": IMAGE_PLACEHOLDER,
            "figures": [{
                "id": "fig-001", "classification": "flow_chart",
                "reading_order_index": 0, "interpretation": {"description": "prior"},
            }],
            "figure_images": {"fig-001": _PNG_1x1},
        }
        mock_llm = MagicMock()
        with patch("cpg_ingester.nodes.figure_interpreter.get_llm", return_value=mock_llm):
            result = figure_interpreter(state)
        mock_llm.invoke.assert_not_called()
        assert result["figures"][0]["interpretation"] == {"description": "prior"}

    def test_no_figures_is_noop(self):
        state = {"markdown": "# Just text", "figures": []}
        with patch("cpg_ingester.nodes.figure_interpreter.get_llm") as g:
            result = figure_interpreter(state)
        g.assert_not_called()
        assert result["markdown"] == "# Just text"

    def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.setenv("FIGURE_INTERPRETATION_ENABLED", "0")
        state = self._state_two_flowcharts()
        with patch("cpg_ingester.nodes.figure_interpreter.get_llm") as g:
            result = figure_interpreter(state)
        g.assert_not_called()
        assert result["markdown"].count(IMAGE_PLACEHOLDER) == 2

    def test_bitmap_failure_skips_gracefully(self):
        # A figure with no resolvable bitmap must not crash or emit a block.
        state = {
            "markdown": IMAGE_PLACEHOLDER,
            "figures": [{"id": "fig-001", "classification": "flow_chart", "reading_order_index": 0}],
        }
        mock_llm = MagicMock()
        with patch("cpg_ingester.nodes.figure_interpreter.get_llm", return_value=mock_llm):
            result = figure_interpreter(state)
        mock_llm.invoke.assert_not_called()
        assert result["markdown"] == IMAGE_PLACEHOLDER


class TestEnabledFlag:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("FIGURE_INTERPRETATION_ENABLED", raising=False)
        assert _interpretation_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "OFF", "no"])
    def test_falsey_disable(self, monkeypatch, val):
        monkeypatch.setenv("FIGURE_INTERPRETATION_ENABLED", val)
        assert _interpretation_enabled() is False

    @pytest.mark.parametrize("val", ["", "   "])
    def test_empty_or_whitespace_is_default(self, monkeypatch, val):
        monkeypatch.setenv("FIGURE_INTERPRETATION_ENABLED", val)
        assert _interpretation_enabled() is True


class TestVisionCallCap:
    """The FIGURE_INTERPRETATION_MAX_FIGURES budget."""

    def _reply(self):
        import json
        return MagicMock(
            content=json.dumps({
                "description": "d", "mermaid": _GOOD_MERMAID,
                "nodes": ["Start", "Decide", "Stop"], "edges": [],
            }),
            usage_metadata=None,
        )

    def _state_n_flowcharts(self, n):
        md = "".join(f"# H{i}\n\n{IMAGE_PLACEHOLDER}\n\n" for i in range(n))
        figures = [
            {"id": f"fig-{i + 1:03d}", "classification": "flow_chart",
             "page": i + 1, "reading_order_index": i}
            for i in range(n)
        ]
        figure_images = {f"fig-{i + 1:03d}": _PNG_1x1 for i in range(n)}
        return {"markdown": md, "figures": figures, "figure_images": figure_images}

    def test_default_max_when_unset(self, monkeypatch):
        from cpg_ingester.nodes.figure_interpreter import DEFAULT_MAX_FIGURES, _max_figures
        monkeypatch.delenv("FIGURE_INTERPRETATION_MAX_FIGURES", raising=False)
        assert _max_figures() == DEFAULT_MAX_FIGURES == 100

    def test_cap_limits_llm_calls(self, monkeypatch):
        monkeypatch.setenv("FIGURE_INTERPRETATION_MAX_FIGURES", "2")
        state = self._state_n_flowcharts(3)
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(side_effect=lambda *_a, **_k: self._reply())
        with patch("cpg_ingester.nodes.figure_interpreter.get_llm", return_value=mock_llm):
            result = figure_interpreter(state)

        assert mock_llm.invoke.call_count == 2  # third figure capped, no call
        md = result["markdown"]
        assert md.count("```mermaid") == 2
        assert md.count(IMAGE_PLACEHOLDER) == 1  # capped figure keeps its placeholder
        interps = [f.get("interpretation") for f in result["figures"]]
        assert interps[0] and interps[1]
        assert interps[2] is None  # fig-003 was capped

    def test_trivial_figures_do_not_consume_budget(self, monkeypatch):
        # cap=1 with a trivial logo first: if trivial consumed budget the
        # flowchart would be capped. It must still be interpreted.
        monkeypatch.setenv("FIGURE_INTERPRETATION_MAX_FIGURES", "1")
        state = {
            "markdown": f"{IMAGE_PLACEHOLDER}\n\n{IMAGE_PLACEHOLDER}",
            "figures": [
                {"id": "fig-001", "classification": "logo", "reading_order_index": 0},
                {"id": "fig-002", "classification": "flow_chart", "reading_order_index": 1},
            ],
            "figure_images": {"fig-001": _PNG_1x1, "fig-002": _PNG_1x1},
        }
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(side_effect=lambda *_a, **_k: self._reply())
        with patch("cpg_ingester.nodes.figure_interpreter.get_llm", return_value=mock_llm):
            result = figure_interpreter(state)

        assert mock_llm.invoke.call_count == 1
        by_id = {f["id"]: f for f in result["figures"]}
        assert by_id["fig-001"]["interpretation"] == {"label": "logo"}
        assert by_id["fig-002"]["interpretation"]["mermaid_valid"] is True


class TestFigureImagesThroughCompiledGraph:
    """Regression: ``figure_images`` must survive the compiled graph.

    ``docling_agent`` returns ``figure_images`` (figure id -> base64 PNG). If the
    key is not declared in ``CPGIngesterState``, LangGraph drops it when merging
    node output into state, so ``figure_interpreter`` sees an empty
    ``figure_images``, resolves no bitmap, and silently no-ops — figure
    interpretation disappears on the compiled-graph (``cli.py``) path.

    Every other figure_interpreter test calls the node as a plain function, which
    bypasses the schema and cannot catch this. This test runs the **compiled
    graph** end-to-end (heavy nodes stubbed) and asserts the interpretation
    reaches the markdown. It is RED on pre-fix code and GREEN once ``figure_images``
    is declared in the state schema.
    """

    def test_figure_images_reach_interpreter(self):
        import cpg_ingester.pipeline as pl

        # Bitmap is carried ONLY via figure_images — the figure entry has no
        # image_b64 / image_ref — so this isolates the schema-propagation bug.
        fake_parse = {
            "markdown": "# Doc\n\n<!-- image -->\n\nafter",
            "docling_json": {},
            "figures": [
                {"id": "fig-001", "classification": "flow_chart",
                 "page": 1, "reading_order_index": 0}
            ],
            "figure_images": {"fig-001": _PNG_1x1},
        }
        canned = {
            "description": "A decision flow.",
            "mermaid": _GOOD_MERMAID,
            "mermaid_valid": True,
            "mermaid_detail": "structural ok",
            "nodes": ["Start", "Decide", "Stop"],
            "edges": [],
        }
        stub = lambda s: {}  # noqa: E731 — trivial node stub
        with patch.object(pl, "docling_agent", lambda s: fake_parse), \
             patch.object(pl, "structure_analyzer", stub), \
             patch.object(pl, "content_filter", stub), \
             patch.object(pl, "item_identifier", stub), \
             patch.object(pl, "classification_reviewer", stub), \
             patch.object(pl, "metadata_extractor", stub), \
             patch.object(pl, "generate_all", stub), \
             patch.object(pl, "assembly", stub), \
             patch.object(pl, "delivery", stub), \
             patch("cpg_ingester.nodes.figure_interpreter._interpret_one",
                   return_value=canned):
            compiled = pl.build_pipeline().compile()
            result = compiled.invoke({
                "markdown": "",
                "litellm_url": "http://unused",
                "llm_model": "unused",
            })

        md = result.get("markdown", "")
        assert "<!-- figure fig-001" in md, (
            "figure_images was dropped by the compiled graph — interpretation "
            "never reached the markdown"
        )
        assert "```mermaid" in md
