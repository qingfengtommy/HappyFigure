"""Tests for terminal UI sizing helpers."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ui  # noqa: E402


def test_panel_width_clamps_without_exceeding_terminal():
    assert ui._panel_width(160) == 132
    assert ui._panel_width(80) == 78
    assert ui._panel_width(32) == 32


def test_truncate_text_handles_tiny_limits():
    assert ui.truncate_text("abcdef", 1) == "…"
    assert ui.truncate_text("", 1) == ""


def test_dashboard_name_budget_stays_readable_on_medium_terminal():
    assert ui._dashboard_name_budget(80) == 26
    assert ui._dashboard_name_budget(160) == 28


def test_progress_dashboard_prefers_wrapping_over_over_shrinking(monkeypatch, capsys):
    monkeypatch.setattr(ui, "term_width", lambda: 80)
    experiments = [
        "cross_cell_line_loop_f1",
        "cross_chromosome_generalization",
        "io_ablation_by_architecture",
    ]
    dashboard = ui.ProgressDashboard(experiments)

    dashboard.start()
    output = capsys.readouterr().out

    for name in experiments:
        shortened = ui._abbreviate(name, max_len=ui._dashboard_name_budget(80))
        assert shortened in output
        assert len(shortened) >= 20
