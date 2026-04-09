"""Tests for pipeline.figure_lint deterministic validation module."""
from __future__ import annotations

import json
import os
import textwrap

import pytest

from pipeline.figure_lint import (
    LintReport,
    detect_iteration_stuck,
    lint_color_compliance,
    lint_cross_panel_consistency,
    lint_figure_code,
    lint_figure_output,
    lint_styled_spec,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_run(tmp_path):
    """Create a minimal run directory with color registry."""
    registry = {
        "conditions": {
            "alpha": "#4C78A8",
            "beta": "#F58518",
            "gamma": "#54A24B",
        },
        "groups": {
            "control": "#9E9E9E",
            "treatment": "#E45756",
        },
    }
    reg_path = tmp_path / "color_registry.json"
    reg_path.write_text(json.dumps(registry))
    return tmp_path, str(reg_path)


@pytest.fixture()
def good_code(tmp_path):
    """Generate a valid figure code file that passes all checks."""
    code = textwrap.dedent("""\
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        plt.rcParams.update({
            'font.size': 8,
            'figure.dpi': 300,
            'axes.spines.top': False,
            'axes.spines.right': False,
            'legend.frameon': False,
        })

        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar([1, 2, 3], [4, 5, 6], color='#4C78A8')
        ax.legend(['series'])
        fig.savefig('panel.png', dpi=300, bbox_inches='tight')
        plt.close(fig)
    """)
    path = tmp_path / "figure_code.py"
    path.write_text(code)
    return str(path)


@pytest.fixture()
def bad_code(tmp_path):
    """Generate a figure code with multiple issues."""
    code = textwrap.dedent("""\
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots()
        ax.bar([1, 2, 3], [4, 5, 6], color='#FF0000')
        plt.show()
        fig.savefig('panel.png', dpi=72)
    """)
    path = tmp_path / "figure_code.py"
    path.write_text(code)
    return str(path)


# ---------------------------------------------------------------------------
# LintReport
# ---------------------------------------------------------------------------


class TestLintReport:
    def test_passed_report(self):
        r = LintReport(passed=True)
        assert not r.blocking
        assert r.summary() == "All checks passed"

    def test_failed_report(self):
        r = LintReport(passed=False, issues=["bad color", "wrong DPI"])
        assert r.blocking
        assert "2 issue(s)" in r.summary()

    def test_merge(self):
        a = LintReport(passed=True, warnings=["minor"])
        b = LintReport(passed=False, issues=["critical"])
        merged = a.merge(b)
        assert not merged.passed
        assert len(merged.issues) == 1
        assert len(merged.warnings) == 1


# ---------------------------------------------------------------------------
# lint_figure_code
# ---------------------------------------------------------------------------


class TestLintFigureCode:
    def test_good_code_passes(self, good_code):
        report = lint_figure_code(good_code)
        assert report.passed

    def test_bad_code_fails(self, bad_code):
        report = lint_figure_code(bad_code)
        assert not report.passed
        issues_text = " ".join(report.issues)
        assert "plt.show()" in issues_text
        assert "dpi=72" in issues_text or "below 300" in issues_text

    def test_missing_file(self):
        report = lint_figure_code("/nonexistent/code.py")
        assert not report.passed
        assert "not found" in report.issues[0]

    def test_syntax_error(self, tmp_path):
        path = tmp_path / "bad_syntax.py"
        path.write_text("def foo(\n")
        report = lint_figure_code(str(path))
        assert not report.passed
        assert "Syntax error" in report.issues[0]

    def test_destructive_ops_detected(self, tmp_path):
        code = textwrap.dedent("""\
            import os
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            os.remove('old_figure.png')
            fig, ax = plt.subplots()
            fig.savefig('panel.png', dpi=300, bbox_inches='tight')
            plt.close(fig)
        """)
        path = tmp_path / "destructive.py"
        path.write_text(code)
        report = lint_figure_code(str(path))
        assert not report.passed
        assert any("Destructive" in i for i in report.issues)

    def test_color_registry_integration(self, good_code, tmp_run):
        _, reg_path = tmp_run
        report = lint_figure_code(good_code, color_registry_path=reg_path)
        # #4C78A8 is in the registry, so should pass
        assert report.passed

    def test_off_palette_detected(self, tmp_run):
        tmp_path, reg_path = tmp_run
        code = textwrap.dedent("""\
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            plt.rcParams.update({
                'font.size': 8,
                'axes.spines.top': False,
                'axes.spines.right': False,
                'legend.frameon': False,
            })
            fig, ax = plt.subplots()
            ax.bar([1, 2], [3, 4], color='#FF00FF')
            fig.savefig('panel.png', dpi=300, bbox_inches='tight')
            plt.close(fig)
        """)
        path = tmp_path / "offcolor.py"
        path.write_text(code)
        report = lint_figure_code(str(path), color_registry_path=reg_path)
        assert not report.passed
        assert any("off-palette" in i for i in report.issues)

    def test_nature_style_warnings(self, tmp_path):
        """Code missing despine/frameon gets Nature-style warnings."""
        code = textwrap.dedent("""\
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            plt.rcParams.update({'font.size': 8})
            fig, ax = plt.subplots()
            ax.bar([1, 2], [3, 4])
            ax.legend(['a'])
            fig.savefig('out.png', dpi=300, bbox_inches='tight')
            plt.close(fig)
        """)
        path = tmp_path / "no_despine.py"
        path.write_text(code)
        report = lint_figure_code(str(path), enforce_nature_style=True)
        warn_text = " ".join(report.warnings)
        assert "Top spine" in warn_text
        assert "Right spine" in warn_text
        assert "frameon" in warn_text

    def test_grid_enabled_warning(self, tmp_path):
        """Code enabling gridlines gets a Nature-style warning."""
        code = textwrap.dedent("""\
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            plt.rcParams.update({'font.size': 8})
            fig, ax = plt.subplots()
            ax.grid(True)
            fig.savefig('out.png', dpi=300, bbox_inches='tight')
            plt.close(fig)
        """)
        path = tmp_path / "with_grid.py"
        path.write_text(code)
        report = lint_figure_code(str(path), enforce_nature_style=True)
        warn_text = " ".join(report.warnings)
        assert "Gridlines" in warn_text or "grid" in warn_text.lower()

    def test_grid_rcparams_warning(self, tmp_path):
        """Grid enabled via rcParams gets warning."""
        code = textwrap.dedent("""\
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            plt.rcParams['axes.grid'] = True
            fig, ax = plt.subplots()
            fig.savefig('out.png', dpi=300, bbox_inches='tight')
            plt.close(fig)
        """)
        path = tmp_path / "grid_rcparams.py"
        path.write_text(code)
        report = lint_figure_code(str(path), enforce_nature_style=True)
        warn_text = " ".join(report.warnings)
        assert "grid" in warn_text.lower()

    def test_grid_visible_kwarg_warning(self, tmp_path):
        """Grid enabled via visible=True kwarg gets warning."""
        code = textwrap.dedent("""\
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            plt.rcParams.update({'font.size': 8})
            fig, ax = plt.subplots()
            ax.grid(visible=True)
            fig.savefig('out.png', dpi=300, bbox_inches='tight')
            plt.close(fig)
        """)
        path = tmp_path / "grid_visible.py"
        path.write_text(code)
        report = lint_figure_code(str(path), enforce_nature_style=True)
        warn_text = " ".join(report.warnings)
        assert "grid" in warn_text.lower()

    def test_no_grid_no_warning(self, tmp_path):
        """Code without grid does not trigger grid warning."""
        code = textwrap.dedent("""\
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            plt.rcParams.update({
                'axes.spines.top': False, 'axes.spines.right': False,
                'axes.grid': False, 'legend.frameon': False,
            })
            fig, ax = plt.subplots()
            ax.bar([1, 2], [3, 4])
            ax.legend(['a'])
            fig.savefig('out.png', dpi=300, bbox_inches='tight')
            plt.close(fig)
        """)
        path = tmp_path / "no_grid.py"
        path.write_text(code)
        report = lint_figure_code(str(path), enforce_nature_style=True)
        warn_text = " ".join(report.warnings)
        assert "Gridlines" not in warn_text

    def test_nature_style_disabled(self, tmp_path):
        """With enforce_nature_style=False, no spine warnings."""
        code = textwrap.dedent("""\
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            plt.rcParams.update({'font.size': 8})
            fig, ax = plt.subplots()
            fig.savefig('out.png', dpi=300, bbox_inches='tight')
            plt.close(fig)
        """)
        path = tmp_path / "no_style.py"
        path.write_text(code)
        report = lint_figure_code(str(path), enforce_nature_style=False)
        assert not any("spine" in w.lower() for w in report.warnings)


# ---------------------------------------------------------------------------
# lint_figure_output
# ---------------------------------------------------------------------------


class TestLintFigureOutput:
    def test_missing_file(self):
        report = lint_figure_output("/nonexistent/figure.png")
        assert not report.passed

    def test_small_file(self, tmp_path):
        path = tmp_path / "tiny.png"
        path.write_bytes(b"\x89PNG" + b"\x00" * 100)
        report = lint_figure_output(str(path), min_file_bytes=10_000)
        assert not report.passed
        assert any("10,000" in i for i in report.issues)

    def test_valid_figure(self, tmp_path):
        """Generate a real PNG and verify it passes."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            pytest.skip("matplotlib not available")

        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar([1, 2, 3], [4, 5, 6])
        path = tmp_path / "good.png"
        fig.savefig(str(path), dpi=300, bbox_inches="tight")
        plt.close(fig)

        report = lint_figure_output(str(path))
        assert report.passed

    def test_svg_skips_image_checks(self, tmp_path):
        """SVG files skip PIL-based checks."""
        path = tmp_path / "figure.svg"
        path.write_text("<svg></svg>" * 1000)  # large enough
        report = lint_figure_output(str(path))
        assert report.passed


# ---------------------------------------------------------------------------
# lint_styled_spec
# ---------------------------------------------------------------------------


class TestLintStyledSpec:
    def test_missing_file(self):
        report = lint_styled_spec("/nonexistent/spec.md")
        assert not report.passed

    def test_too_short(self, tmp_path):
        path = tmp_path / "spec.md"
        path.write_text("# Short\nJust two lines.\n")
        report = lint_styled_spec(str(path))
        assert not report.passed
        assert any("lines" in i for i in report.issues)

    def test_valid_spec(self, tmp_path):
        spec = "\n".join([
            "# Figure Spec",
            "## Data source and mapping",
            "Read from results.csv",
            "## Color palette",
            "Condition colors: #4C78A8, #F58518",
            "## Axes and scales",
            "Y-axis: 0-1.0",
            "DPI: 300",
            "figsize = (6, 4) inches",
        ] + ["additional detail"] * 5)
        path = tmp_path / "styled_spec.md"
        path.write_text(spec)
        report = lint_styled_spec(str(path))
        assert report.passed

    def test_missing_keywords_are_warnings(self, tmp_path):
        """Missing section keywords produce warnings, not blocking issues."""
        spec = "\n".join(["# Spec"] + ["line"] * 15)
        path = tmp_path / "sparse.md"
        path.write_text(spec)
        report = lint_styled_spec(str(path))
        # Should pass (no blocking issues) but have warnings
        assert report.passed
        assert len(report.warnings) > 0


# ---------------------------------------------------------------------------
# lint_color_compliance
# ---------------------------------------------------------------------------


class TestLintColorCompliance:
    def test_compliant_code(self, tmp_run):
        tmp_path, reg_path = tmp_run
        code = "colors = {'a': '#4C78A8', 'b': '#F58518'}\n"
        path = tmp_path / "code.py"
        path.write_text(code)
        report = lint_color_compliance(str(path), reg_path)
        assert report.passed

    def test_violation_detected(self, tmp_run):
        tmp_path, reg_path = tmp_run
        code = "colors = {'a': '#FF00FF', 'b': '#4C78A8'}\n"
        path = tmp_path / "code.py"
        path.write_text(code)
        report = lint_color_compliance(str(path), reg_path)
        assert not report.passed
        assert any("#ff00ff" in i for i in report.issues)

    def test_grays_allowed(self, tmp_run):
        tmp_path, reg_path = tmp_run
        code = "bg = '#FFFFFF'\nfg = '#000000'\ngrid = '#E6E6E6'\n"
        path = tmp_path / "code.py"
        path.write_text(code)
        report = lint_color_compliance(str(path), reg_path)
        assert report.passed

    def test_missing_registry_is_warning(self, tmp_path):
        """Missing registry file produces a warning, not a failure."""
        code = "color = '#FF0000'\n"
        path = tmp_path / "code.py"
        path.write_text(code)
        report = lint_color_compliance(str(path), "/nonexistent/reg.json")
        assert report.passed
        assert any("Could not load" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# lint_cross_panel_consistency
# ---------------------------------------------------------------------------


class TestLintCrossPanelConsistency:
    def test_consistent_panels(self, tmp_path):
        for name in ("panel_a", "panel_b"):
            code = textwrap.dedent("""\
                import matplotlib.pyplot as plt
                plt.rcParams['font.size'] = 8
                fig, ax = plt.subplots(figsize=(6, 4))
                fig.savefig('out.png', dpi=300)
            """)
            (tmp_path / f"{name}.py").write_text(code)
        paths = {
            "panel_a": str(tmp_path / "panel_a.py"),
            "panel_b": str(tmp_path / "panel_b.py"),
        }
        report = lint_cross_panel_consistency(paths)
        assert report.passed

    def test_inconsistent_figsize(self, tmp_path):
        (tmp_path / "small.py").write_text(
            "fig, ax = plt.subplots(figsize=(3, 2))\nfig.savefig('out.png', dpi=300)\n"
        )
        (tmp_path / "huge.py").write_text(
            "fig, ax = plt.subplots(figsize=(12, 9))\nfig.savefig('out.png', dpi=300)\n"
        )
        paths = {"small": str(tmp_path / "small.py"), "huge": str(tmp_path / "huge.py")}
        report = lint_cross_panel_consistency(paths)
        assert not report.passed
        assert any("vary by" in i for i in report.issues)

    def test_inconsistent_font(self, tmp_path):
        (tmp_path / "a.py").write_text("plt.rcParams['font.size'] = 7\n")
        (tmp_path / "b.py").write_text("plt.rcParams['font.size'] = 12\n")
        paths = {"a": str(tmp_path / "a.py"), "b": str(tmp_path / "b.py")}
        report = lint_cross_panel_consistency(paths)
        assert not report.passed
        assert any("font" in i.lower() for i in report.issues)

    def test_single_panel_skipped(self, tmp_path):
        (tmp_path / "solo.py").write_text("fig = plt.figure()\n")
        report = lint_cross_panel_consistency({"solo": str(tmp_path / "solo.py")})
        assert report.passed
        assert len(report.warnings) == 1


# ---------------------------------------------------------------------------
# detect_iteration_stuck
# ---------------------------------------------------------------------------


class TestDetectIterationStuck:
    def test_not_stuck_with_one_result(self):
        assert not detect_iteration_stuck([{"score": 7.0}])

    def test_stuck_same_score(self):
        results = [{"score": 7.5}, {"score": 7.8}]
        assert detect_iteration_stuck(results)

    def test_not_stuck_improving(self):
        results = [{"score": 6.0}, {"score": 8.0}]
        assert not detect_iteration_stuck(results)

    def test_oscillation_detected(self):
        results = [{"score": 6.0}, {"score": 8.0}, {"score": 6.5}]
        assert detect_iteration_stuck(results)

    def test_same_issue_detected(self):
        results = [
            {"score": 6.0, "issues": ["text overlap on x-axis"]},
            {"score": 6.5, "issues": ["text overlap on x-axis"]},
        ]
        assert detect_iteration_stuck(results)

    def test_different_issues_not_stuck(self):
        results = [
            {"score": 6.0, "issues": ["text overlap"]},
            {"score": 8.5, "issues": ["color mismatch"]},
        ]
        assert not detect_iteration_stuck(results)

    def test_same_error_string(self):
        """Rule 4: same error for last 2 = stuck."""
        results = [
            {"score": 0, "error": "FileNotFoundError: data.csv"},
            {"score": 0, "error": "FileNotFoundError: data.csv"},
        ]
        assert detect_iteration_stuck(results)

    def test_custom_tolerance(self):
        """Custom score_tolerance changes sensitivity."""
        results = [{"score": 7.0}, {"score": 8.0}]
        # Default tolerance 0.5 — not stuck
        assert not detect_iteration_stuck(results)
        # Wider tolerance 1.5 — now stuck
        assert detect_iteration_stuck(results, score_tolerance=1.5)

    def test_missing_score_fields(self):
        """Handles results with no score gracefully."""
        results = [{"verdict": "FAIL"}, {"verdict": "FAIL"}]
        # No scores to compare — not stuck (scores are None)
        assert not detect_iteration_stuck(results)


# ---------------------------------------------------------------------------
# PIL assembly tests
# ---------------------------------------------------------------------------

class TestPILAssembly:
    """Tests for pipeline.assembly.assemble_pil."""

    def test_basic_assembly(self, tmp_path):
        """PIL assembly produces output from spec + panels."""
        from pipeline.assembly import assemble_pil
        from PIL import Image

        # Create two small test panel PNGs
        for name in ("a", "b"):
            img = Image.new("RGB", (200, 150), (100, 150, 200))
            img.save(str(tmp_path / f"panel_{name}.png"))

        spec = {
            "figure_id": "Test",
            "dpi": 150,
            "layout": {
                "rows": [{"row_index": 0, "panels": [
                    {"panel_id": "a"}, {"panel_id": "b"}
                ]}],
            },
            "panel_labels": {"scheme": "lowercase"},
        }
        panel_paths = {
            "a": str(tmp_path / "panel_a.png"),
            "b": str(tmp_path / "panel_b.png"),
        }
        out = str(tmp_path / "assembled.png")
        ok = assemble_pil(spec, panel_paths, out)
        assert ok
        assert os.path.exists(out)
        result = Image.open(out)
        assert result.width > 200  # wider than single panel
        assert result.height > 150  # taller due to label

    def test_missing_panels_get_placeholder(self, tmp_path):
        """Missing panels become placeholders, not crashes."""
        from pipeline.assembly import assemble_pil

        spec = {
            "figure_id": "Test",
            "dpi": 150,
            "layout": {"rows": [{"row_index": 0, "panels": [{"panel_id": "x"}]}]},
            "panel_labels": {"scheme": "lowercase"},
        }
        out = str(tmp_path / "assembled.png")
        ok = assemble_pil(spec, {}, out)
        assert ok
        assert os.path.exists(out)

    def test_multi_row_assembly(self, tmp_path):
        """Assembly with 2 rows produces taller output."""
        from pipeline.assembly import assemble_pil
        from PIL import Image

        for name in ("a", "b", "c", "d"):
            img = Image.new("RGB", (200, 150), (80, 120, 200))
            img.save(str(tmp_path / f"panel_{name}.png"))

        spec = {
            "figure_id": "Test",
            "dpi": 150,
            "layout": {
                "rows": [
                    {"row_index": 0, "panels": [{"panel_id": "a"}, {"panel_id": "b"}]},
                    {"row_index": 1, "panels": [{"panel_id": "c"}, {"panel_id": "d"}]},
                ],
            },
            "panel_labels": {"scheme": "lowercase"},
        }
        paths = {n: str(tmp_path / f"panel_{n}.png") for n in "abcd"}
        out = str(tmp_path / "multi_row.png")
        ok = assemble_pil(spec, paths, out)
        assert ok
        result = Image.open(out)
        # 2 rows should be taller than 1 row
        assert result.height > 300

    def test_empty_spec_returns_false(self, tmp_path):
        """Empty row spec returns False, doesn't crash."""
        from pipeline.assembly import assemble_pil

        spec = {
            "figure_id": "Empty",
            "dpi": 150,
            "layout": {"rows": []},
            "panel_labels": {"scheme": "lowercase"},
        }
        out = str(tmp_path / "empty.png")
        ok = assemble_pil(spec, {}, out)
        assert not ok
