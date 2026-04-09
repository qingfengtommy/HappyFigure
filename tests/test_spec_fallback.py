"""Tests for graphs/spec_fallback.py — deterministic spec generation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from graphs.spec_fallback import (  # noqa: E402
    _grid_for_n,
    _size_tier,
    _assign_colors,
    _color_table,
    _color_map_python,
    _color_map_flat,
    _PALETTE_A,
    _detect_comparison_structure,
    discover_json_results,
    generate_spec,
    generate_multi_figure_plan,
    update_state_json,
)


# ---------------------------------------------------------------------------
# _grid_for_n
# ---------------------------------------------------------------------------


class TestGridForN:
    @pytest.mark.parametrize(
        "n, expected",
        [
            (1, (1, 1)),
            (2, (1, 2)),
            (3, (1, 3)),
            (4, (2, 2)),
            (5, (2, 3)),
            (6, (2, 3)),
            (7, (2, 4)),
            (8, (2, 4)),
        ],
    )
    def test_known_layouts(self, n, expected):
        assert _grid_for_n(n) == expected

    def test_large_n_uses_sqrt(self):
        rows, cols = _grid_for_n(16)
        assert rows * cols >= 16
        assert rows == 4 and cols == 4

    def test_n_equals_9(self):
        rows, cols = _grid_for_n(9)
        assert rows * cols >= 9
        assert rows == 3 and cols == 3

    def test_returns_tuple(self):
        result = _grid_for_n(5)
        assert isinstance(result, tuple)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _size_tier
# ---------------------------------------------------------------------------


class TestSizeTier:
    def test_single_panel(self):
        tier, size = _size_tier(1)
        assert tier == "small"
        assert "3.5" in size

    def test_two_panels(self):
        tier, size = _size_tier(2)
        assert tier == "medium"

    def test_four_panels(self):
        tier, size = _size_tier(4)
        assert tier == "large"

    def test_many_panels(self):
        tier, size = _size_tier(10)
        assert tier == "composition"
        assert "7.2" in size

    def test_zero_panels(self):
        # Edge case: 0 is <= 1
        tier, _ = _size_tier(0)
        assert tier == "small"


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------


class TestAssignColors:
    def test_assigns_from_palette(self):
        names = ["A", "B", "C"]
        colors = _assign_colors(names)
        assert len(colors) == 3
        assert colors["A"] == _PALETTE_A[0]
        assert colors["B"] == _PALETTE_A[1]

    def test_wraps_around_palette(self):
        names = [f"m{i}" for i in range(len(_PALETTE_A) + 2)]
        colors = _assign_colors(names)
        # Should wrap around without error
        assert len(colors) == len(_PALETTE_A) + 2
        assert colors[f"m{len(_PALETTE_A)}"] == _PALETTE_A[0]

    def test_empty_names(self):
        assert _assign_colors([]) == {}


class TestColorTable:
    def test_produces_rows(self):
        names = ["MethodA", "MethodB"]
        colors = _assign_colors(names)
        table = _color_table(names, colors)
        assert "MethodA" in table
        assert "MethodB" in table
        assert _PALETTE_A[0] in table

    def test_row_count(self):
        names = ["X", "Y", "Z"]
        colors = _assign_colors(names)
        rows = _color_table(names, colors).strip().split("\n")
        assert len(rows) == 3


class TestColorMapPython:
    def test_produces_dict_literal(self):
        names = ["A", "B"]
        colors = _assign_colors(names)
        result = _color_map_python(names, colors)
        assert "color_map = {" in result
        assert '"A"' in result
        assert _PALETTE_A[0] in result


class TestColorMapFlat:
    def test_format(self):
        names = ["X"]
        colors = _assign_colors(names)
        result = _color_map_flat(names, colors)
        assert f"X={_PALETTE_A[0]}" in result


# ---------------------------------------------------------------------------
# _detect_comparison_structure
# ---------------------------------------------------------------------------


class TestDetectComparisonStructure:
    def test_detects_methods(self):
        data = {
            "section": "test",
            "MethodA": {"accuracy": 0.9, "f1": 0.85},
            "MethodB": {"accuracy": 0.8, "f1": 0.75},
        }
        exp = {"data": data, "methods": [], "classifiers": [], "metrics": {}, "categories": []}
        _detect_comparison_structure(exp)
        assert "MethodA" in exp["methods"]
        assert "MethodB" in exp["methods"]

    def test_detects_classifiers(self):
        data = {
            "section": "clf_test",
            "logistic_regression": {"accuracy": 0.9, "f1": 0.85},
            "svm": {"accuracy": 0.8, "f1": 0.75},
        }
        exp = {"data": data, "methods": [], "classifiers": [], "metrics": {}, "categories": []}
        _detect_comparison_structure(exp)
        assert "logistic_regression" in exp["classifiers"]

    def test_detects_categories(self):
        data = {
            "section": "cat_test",
            "categories": {"cancer": {}, "normal": {}},
        }
        exp = {"data": data, "methods": [], "classifiers": [], "metrics": {}, "categories": []}
        _detect_comparison_structure(exp)
        assert "cancer" in exp["categories"]
        assert "normal" in exp["categories"]

    def test_detects_flat_metrics(self):
        data = {
            "section": "flat",
            "clustering": {"nmi": 0.8, "ari": 0.7},
        }
        exp = {"data": data, "methods": [], "classifiers": [], "metrics": {}, "categories": []}
        _detect_comparison_structure(exp)
        assert "nmi" in exp["metrics"]
        assert exp["metrics"]["nmi"] == 0.8


# ---------------------------------------------------------------------------
# discover_json_results
# ---------------------------------------------------------------------------


class TestDiscoverJsonResults:
    def test_discovers_files(self, tmp_path):
        data = {"section": "exp1", "A": {"acc": 0.9}, "B": {"acc": 0.8}}
        (tmp_path / "results_exp1.json").write_text(json.dumps(data))
        results = discover_json_results(str(tmp_path))
        assert len(results) == 1
        assert results[0]["section"] == "exp1"

    def test_skips_non_results_files(self, tmp_path):
        (tmp_path / "config.json").write_text('{"key": "value"}')
        results = discover_json_results(str(tmp_path))
        assert len(results) == 0

    def test_skips_invalid_json(self, tmp_path):
        (tmp_path / "results_bad.json").write_text("not json {{{")
        results = discover_json_results(str(tmp_path))
        assert len(results) == 0

    def test_skips_files_without_section(self, tmp_path):
        (tmp_path / "results_nosec.json").write_text('{"data": [1,2,3]}')
        results = discover_json_results(str(tmp_path))
        assert len(results) == 0

    def test_deduplicates_by_section(self, tmp_path):
        data = {"section": "dup_sec", "X": {"m": 1}}
        (tmp_path / "results_a.json").write_text(json.dumps(data))
        (tmp_path / "results_b.json").write_text(json.dumps(data))
        results = discover_json_results(str(tmp_path))
        assert len(results) == 1

    def test_searches_results_by_epoch_first(self, tmp_path):
        epoch_dir = tmp_path / "results_by_epoch"
        epoch_dir.mkdir()
        data = {"section": "epoch_exp", "A": {"m": 1}}
        (epoch_dir / "results_e1.json").write_text(json.dumps(data))
        results = discover_json_results(str(tmp_path))
        assert len(results) == 1
        assert results[0]["section"] == "epoch_exp"

    def test_empty_directory(self, tmp_path):
        results = discover_json_results(str(tmp_path))
        assert results == []

    def test_nonexistent_directory(self, tmp_path):
        results = discover_json_results(str(tmp_path / "nonexistent"))
        assert results == []


# ---------------------------------------------------------------------------
# generate_spec
# ---------------------------------------------------------------------------


class TestGenerateSpec:
    def _make_method_exp(self):
        return {
            "section": "method_cmp",
            "path": "/data/results_method.json",
            "data": {
                "section": "method_cmp",
                "MethodA": {"accuracy": 0.9, "f1": 0.85},
                "MethodB": {"accuracy": 0.8, "f1": 0.75},
            },
            "methods": ["MethodA", "MethodB"],
            "classifiers": [],
            "metrics": {},
            "categories": [],
        }

    def test_method_comparison_spec(self):
        exp = self._make_method_exp()
        section, spec = generate_spec(exp)
        assert section == "method_cmp"
        assert "FIGURE SPECIFICATION" in spec
        assert "MethodA" in spec
        assert "STYLE ENFORCEMENT" in spec

    def test_classifier_metrics_spec(self):
        exp = {
            "section": "clf_test",
            "path": "/data/results_clf.json",
            "data": {
                "section": "clf_test",
                "logistic_regression": {"accuracy": 0.9, "f1": 0.8},
                "svm": {"accuracy": 0.85, "f1": 0.7},
            },
            "methods": [],
            "classifiers": ["logistic_regression", "svm"],
            "metrics": {},
            "categories": [],
        }
        section, spec = generate_spec(exp)
        assert "classifiers" in spec.lower()
        assert "STYLE ENFORCEMENT" in spec

    def test_flat_metrics_spec(self):
        exp = {
            "section": "flat_test",
            "path": "/data/results_flat.json",
            "data": {"section": "flat_test", "clustering": {"nmi": 0.8, "ari": 0.7}},
            "methods": [],
            "classifiers": [],
            "metrics": {"nmi": 0.8, "ari": 0.7},
            "categories": [],
        }
        section, spec = generate_spec(exp)
        assert "nmi" in spec
        assert "ari" in spec

    def test_no_plottable_structure(self):
        exp = {
            "section": "empty",
            "path": "/data/results_empty.json",
            "data": {"section": "empty"},
            "methods": [],
            "classifiers": [],
            "metrics": {},
            "categories": [],
        }
        section, spec = generate_spec(exp)
        assert "No plottable structure" in spec


# ---------------------------------------------------------------------------
# generate_multi_figure_plan
# ---------------------------------------------------------------------------


class TestGenerateMultiFigurePlan:
    def test_produces_markdown(self):
        discovered = [
            {
                "section": "exp1",
                "path": "/p",
                "methods": ["A", "B"],
                "classifiers": [],
                "categories": [],
                "metrics": {},
            },
        ]
        plan = generate_multi_figure_plan(discovered, ["exp1"])
        assert "# Multi-Figure Plan" in plan
        assert "exp1" in plan
        assert "Multi-method bar" in plan

    def test_skips_experiments_not_in_list(self):
        discovered = [
            {"section": "inc", "path": "/p", "methods": ["A"], "classifiers": [], "categories": [], "metrics": {}},
            {"section": "exc", "path": "/p2", "methods": ["B"], "classifiers": [], "categories": [], "metrics": {}},
        ]
        plan = generate_multi_figure_plan(discovered, ["inc"])
        assert "inc" in plan
        assert "exc" not in plan


# ---------------------------------------------------------------------------
# update_state_json
# ---------------------------------------------------------------------------


class TestUpdateStateJson:
    def test_creates_new_state(self, tmp_path):
        update_state_json(str(tmp_path), ["exp1", "exp2"])
        state = json.loads((tmp_path / "state.json").read_text())
        assert "exp1" in state["per_experiment_specs"]
        assert "exp2" in state["per_experiment_specs"]
        assert "figure_plan" in state["completed_steps"]

    def test_updates_existing_state(self, tmp_path):
        existing = {"some_key": "preserved", "completed_steps": ["init"]}
        (tmp_path / "state.json").write_text(json.dumps(existing))
        update_state_json(str(tmp_path), ["exp1"])
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["some_key"] == "preserved"
        assert "init" in state["completed_steps"]
        assert "figure_plan" in state["completed_steps"]

    def test_does_not_duplicate_figure_plan_step(self, tmp_path):
        existing = {"completed_steps": ["init", "figure_plan"]}
        (tmp_path / "state.json").write_text(json.dumps(existing))
        update_state_json(str(tmp_path), ["exp1"])
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["completed_steps"].count("figure_plan") == 1

    def test_routes_default_to_statistical(self, tmp_path):
        update_state_json(str(tmp_path), ["exp1"])
        state = json.loads((tmp_path / "state.json").read_text())
        route = state["per_experiment_routes"]["exp1"]
        assert route["figure_category"] == "statistical"
        assert route["statistical_subcategory"] == "bar_group_plots"
