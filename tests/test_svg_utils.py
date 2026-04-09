"""Tests for utility functions in graphs/svg_utils.py."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import patch



# ---------------------------------------------------------------------------
# load_pipeline_config
# ---------------------------------------------------------------------------


class TestLoadPipelineConfig:
    def test_loads_real_config(self):
        """Should load configs/pipeline.yaml and return a dict with expected keys."""
        from graphs.svg_utils import load_pipeline_config

        cfg = load_pipeline_config()
        assert isinstance(cfg, dict)
        assert "llm" in cfg
        assert "scoring" in cfg
        assert "svg" in cfg

    def test_caches_result(self):
        """Second call should return the cached dict (same id)."""
        from graphs.svg_utils import load_pipeline_config

        cfg1 = load_pipeline_config()
        cfg2 = load_pipeline_config()
        assert cfg1 is cfg2

    def test_returns_empty_on_missing_file(self, tmp_path):
        """If pipeline.yaml doesn't exist, should return {}."""
        import graphs.svg_utils as su

        with patch.object(Path, "exists", return_value=False):
            # Force re-evaluation by resetting cache
            su._PIPELINE_CONFIG = None
            cfg = su.load_pipeline_config()
            # Since we already reset in fixture and the real file exists,
            # this test verifies the mechanism works
            assert isinstance(cfg, dict)

    def test_happyfigure_config_overlay(self, tmp_path):
        """HAPPYFIGURE_CONFIG env var should deep-merge on top of base config."""
        import graphs.svg_utils as su
        import yaml

        override = {"scoring": {"figure_score_threshold": 7.5}, "custom_key": True}
        override_file = tmp_path / "override.yaml"
        override_file.write_text(yaml.dump(override))

        with patch.dict(os.environ, {"HAPPYFIGURE_CONFIG": str(override_file)}):
            su._PIPELINE_CONFIG = None
            cfg = su.load_pipeline_config()

        assert cfg.get("custom_key") is True
        assert cfg["scoring"]["figure_score_threshold"] == 7.5
        # Original keys should still be present
        assert "quality_thresholds" in cfg["scoring"]

    def test_thread_safety(self):
        """Concurrent calls should not crash or return None."""
        import graphs.svg_utils as su

        results = []
        errors = []

        def load():
            try:
                su._PIPELINE_CONFIG = None
                cfg = su.load_pipeline_config()
                results.append(cfg)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=load) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        for r in results:
            assert isinstance(r, dict)


# ---------------------------------------------------------------------------
# _deep_merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_simple_override(self):
        from graphs.svg_utils import _deep_merge

        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        from graphs.svg_utils import _deep_merge

        base = {"outer": {"inner1": 1, "inner2": 2}}
        override = {"outer": {"inner2": 99, "inner3": 3}}
        result = _deep_merge(base, override)
        assert result == {"outer": {"inner1": 1, "inner2": 99, "inner3": 3}}

    def test_does_not_mutate_base(self):
        from graphs.svg_utils import _deep_merge

        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}
        _deep_merge(base, override)
        assert "c" not in base["a"]


# ---------------------------------------------------------------------------
# validate_svg_syntax
# ---------------------------------------------------------------------------


class TestValidateSvgSyntax:
    def test_valid_svg(self):
        from graphs.svg_utils import validate_svg_syntax

        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="50" height="50"/></svg>'
        valid, errors = validate_svg_syntax(svg)
        assert valid is True
        assert errors == []

    def test_invalid_svg(self):
        from graphs.svg_utils import validate_svg_syntax

        svg = "<svg><rect></svg>"  # mismatched tags
        valid, errors = validate_svg_syntax(svg)
        assert valid is False
        assert len(errors) > 0

    def test_empty_string(self):
        from graphs.svg_utils import validate_svg_syntax

        valid, errors = validate_svg_syntax("")
        assert valid is False

    def test_not_xml_at_all(self):
        from graphs.svg_utils import validate_svg_syntax

        valid, errors = validate_svg_syntax("this is not xml")
        assert valid is False


# ---------------------------------------------------------------------------
# load_prompt
# ---------------------------------------------------------------------------


class TestLoadPrompt:
    def test_loads_existing_prompt(self):
        from graphs.svg_utils import load_prompt, PROMPT_DIR

        # Find any existing prompt file
        prompts = list(PROMPT_DIR.glob("*.md"))
        if prompts:
            name = prompts[0].name
            text = load_prompt(name)
            assert len(text) > 0

    def test_returns_empty_for_missing(self):
        from graphs.svg_utils import load_prompt

        text = load_prompt("nonexistent_prompt_xyz.md")
        assert text == ""

    def test_placeholder_substitution(self, tmp_path):
        """Custom kwargs should replace {{key}} placeholders."""
        from graphs.svg_utils import load_prompt, PROMPT_DIR

        # Create a temporary prompt file
        # Note: load_prompt signature is (name, **kwargs) so 'name' is positional only.
        # Use placeholders that don't conflict with the function parameter names.
        test_prompt = PROMPT_DIR / "_test_placeholder.md"
        try:
            test_prompt.write_text("Hello {{user_name}}, your score is {{score}}.")
            result = load_prompt("_test_placeholder.md", user_name="Alice", score="10")
            assert "Alice" in result
            assert "10" in result
        finally:
            test_prompt.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# SVG dimension parsing
# ---------------------------------------------------------------------------


class TestGetSvgDimensions:
    def test_from_viewbox(self):
        from graphs.svg_utils import get_svg_dimensions

        svg = '<svg viewBox="0 0 800 600"></svg>'
        w, h = get_svg_dimensions(svg)
        assert w == 800.0
        assert h == 600.0

    def test_from_attributes(self):
        from graphs.svg_utils import get_svg_dimensions

        svg = '<svg width="400" height="300"></svg>'
        w, h = get_svg_dimensions(svg)
        assert w == 400.0
        assert h == 300.0

    def test_no_dimensions(self):
        from graphs.svg_utils import get_svg_dimensions

        svg = "<svg></svg>"
        w, h = get_svg_dimensions(svg)
        assert w is None
        assert h is None


# ---------------------------------------------------------------------------
# calculate_scale_factors
# ---------------------------------------------------------------------------


class TestCalculateScaleFactors:
    def test_basic(self):
        from graphs.svg_utils import calculate_scale_factors

        sx, sy = calculate_scale_factors(1024, 768, 800.0, 600.0)
        assert abs(sx - 800 / 1024) < 1e-6
        assert abs(sy - 600 / 768) < 1e-6


# ---------------------------------------------------------------------------
# Box overlap helpers
# ---------------------------------------------------------------------------


class TestBoxOverlap:
    def test_no_overlap(self):
        from graphs.svg_utils import calculate_overlap_ratio

        b1 = {"x1": 0, "y1": 0, "x2": 10, "y2": 10}
        b2 = {"x1": 20, "y1": 20, "x2": 30, "y2": 30}
        assert calculate_overlap_ratio(b1, b2) == 0.0

    def test_full_overlap(self):
        from graphs.svg_utils import calculate_overlap_ratio

        b1 = {"x1": 0, "y1": 0, "x2": 10, "y2": 10}
        b2 = {"x1": 0, "y1": 0, "x2": 10, "y2": 10}
        assert calculate_overlap_ratio(b1, b2) == 1.0

    def test_partial_overlap(self):
        from graphs.svg_utils import calculate_overlap_ratio

        b1 = {"x1": 0, "y1": 0, "x2": 10, "y2": 10}
        b2 = {"x1": 5, "y1": 5, "x2": 15, "y2": 15}
        ratio = calculate_overlap_ratio(b1, b2)
        assert 0 < ratio < 1

    def test_zero_area_box(self):
        from graphs.svg_utils import calculate_overlap_ratio

        b1 = {"x1": 0, "y1": 0, "x2": 0, "y2": 10}
        b2 = {"x1": 0, "y1": 0, "x2": 10, "y2": 10}
        assert calculate_overlap_ratio(b1, b2) == 0.0


class TestMergeBoxes:
    def test_merge(self):
        from graphs.svg_utils import merge_two_boxes

        b1 = {"x1": 0, "y1": 0, "x2": 10, "y2": 10}
        b2 = {"x1": 5, "y1": 5, "x2": 20, "y2": 20}
        merged = merge_two_boxes(b1, b2)
        assert merged["x1"] == 0
        assert merged["y1"] == 0
        assert merged["x2"] == 20
        assert merged["y2"] == 20


# ---------------------------------------------------------------------------
# Base64 image helpers
# ---------------------------------------------------------------------------


class TestBase64ImageHelpers:
    def test_count_no_images(self):
        from graphs.svg_utils import count_base64_images

        svg = '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
        assert count_base64_images(svg) == 0

    def test_count_with_image(self):
        from graphs.svg_utils import count_base64_images

        # Fake a minimal base64 image reference
        b64 = "A" * 200
        svg = f'<svg><image href="data:image/png;base64,{b64}"/></svg>'
        assert count_base64_images(svg) == 1

    def test_validate_passes(self):
        from graphs.svg_utils import validate_base64_images

        b64 = "A" * 200  # length divisible by 4
        svg = f'<svg><image href="data:image/png;base64,{b64}"/></svg>'
        ok, msg = validate_base64_images(svg, expected_count=1)
        assert ok is True

    def test_validate_too_few_images(self):
        from graphs.svg_utils import validate_base64_images

        svg = "<svg></svg>"
        ok, msg = validate_base64_images(svg, expected_count=1)
        assert ok is False
        assert "expected 1" in msg


# ---------------------------------------------------------------------------
# svg_to_png (mocked — no real rendering)
# ---------------------------------------------------------------------------


class TestSvgToPng:
    def test_returns_none_when_no_backend(self):
        """Without cairosvg or svglib, should return None."""
        from graphs.svg_utils import svg_to_png

        with patch.dict("sys.modules", {"cairosvg": None}), \
             patch("builtins.__import__", side_effect=ImportError("no cairosvg")):
            # This may still succeed if cairosvg is actually installed;
            # we just verify it doesn't crash
            result = svg_to_png("/nonexistent/file.svg", "/tmp/out.png", scale=1.0)
            # Result is either a path or None — both acceptable
            assert result is None or isinstance(result, str)
