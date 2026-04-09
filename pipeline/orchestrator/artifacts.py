"""Canonical on-disk artifact paths for plot and diagram runs (v2 layout).

Single source of truth for experiment-centric paths under ``experiments/<name>/``.
"""

from __future__ import annotations

import os

# Recorded in ``run_manifest.json`` so resume and tooling know which paths to expect.
ARTIFACT_LAYOUT_VERSION = 3

EXPERIMENTS_DIR = "experiments"
OUTPUTS_DIR = "outputs"
DEBUG_DIR = "debug"
PAPER_FIGURES_DIR = "paper_figures"

# Plot — explore / style
PLOT_EXPLORATION_REPORT = "exploration_report.md"
PLOT_EXPLORATION_SUMMARY = "exploration_summary.json"
GLOBAL_STYLE = "global_style.md"
MULTI_FIGURE_PLAN = "multi_figure_plan.md"
DESIGN_SUMMARY = "design_summary.json"
EXPERIMENT_DESCRIPTION = "description.md"
STYLED_SPEC = "styled_spec.md"
FIGURE_CODE = "figure_code.py"
CRITIC_RESULT = "critic_result.json"

# Diagram — explicit design artifact (explore still produces method_description.md)
DIAGRAM_DESIGN_SPEC = "diagram_design_spec.md"

# Paper composite (unified mode)
FIGURE_CLASSIFICATION = "figure_classification.json"
ASSEMBLY_SPEC_DIR = "assembly_specs"
ASSEMBLY_DIR = "assembly"
PAPER_FIGURE_PLAN = "paper_figure_plan.md"
DATA_DISTRIBUTION_REPORT = "data_distribution_report.json"
COLOR_REGISTRY = "color_registry.json"
PANELS_DIR = "panels"
DATA_RECOVERY_DIR = "data_recovery"
RECOVERY_LOG = "data_recovery/recovery_log.json"

# Feedback (human review)
FEEDBACK_DIR = "feedback"
REVIEW_TEMPLATE = "review.md"
HUMAN_STYLE_FEEDBACK = "feedback/human_style_feedback.md"
HUMAN_DATA_FEEDBACK = "feedback/human_data_feedback.md"
HUMAN_CODE_FEEDBACK = "feedback/human_code_feedback.md"
STYLE_PREFERENCES_REL = os.path.join("configs", "feedback", "style_preferences.yaml")


def experiment_dir(run_dir: str, experiment: str) -> str:
    return os.path.join(run_dir, EXPERIMENTS_DIR, experiment)


def experiment_rel_dir(experiment: str) -> str:
    return os.path.join(EXPERIMENTS_DIR, experiment)


def plot_exploration_report_path(run_dir: str) -> str:
    return os.path.join(run_dir, PLOT_EXPLORATION_REPORT)


def plot_experiment_workspace(run_dir: str, experiment: str) -> str:
    """Directory where code-agent writes figure code, critic JSON, and iter archives."""
    return experiment_dir(run_dir, experiment)


def experiment_description_path(run_dir: str, experiment: str) -> str:
    return os.path.join(experiment_dir(run_dir, experiment), EXPERIMENT_DESCRIPTION)


def experiment_styled_spec_path(run_dir: str, experiment: str) -> str:
    return os.path.join(experiment_dir(run_dir, experiment), STYLED_SPEC)


def beam_styled_spec_path(run_dir: str, experiment: str, variant_idx: int) -> str:
    """Beam style variant *sN* — ``styled_spec_s0.md``, ``styled_spec_s1.md``, …"""
    return os.path.join(experiment_dir(run_dir, experiment), f"styled_spec_s{variant_idx}.md")


def beam_styled_spec_rel_path(experiment: str, variant_idx: int) -> str:
    return os.path.join(experiment_rel_dir(experiment), f"styled_spec_s{variant_idx}.md")


def diagram_design_spec_path(run_dir: str) -> str:
    return os.path.join(run_dir, DIAGRAM_DESIGN_SPEC)


def design_summary_path(run_dir: str) -> str:
    return os.path.join(run_dir, DESIGN_SUMMARY)


def paper_figures_dir(run_dir: str) -> str:
    """Directory for composite paper figures assembled from individual panels."""
    return os.path.join(run_dir, OUTPUTS_DIR, PAPER_FIGURES_DIR)


def plot_outputs_root(run_dir: str) -> str:
    return os.path.join(run_dir, OUTPUTS_DIR)


def plot_debug_root(run_dir: str) -> str:
    return os.path.join(run_dir, DEBUG_DIR)


def review_template_path(run_dir: str) -> str:
    return os.path.join(run_dir, REVIEW_TEMPLATE)


def feedback_dir(run_dir: str) -> str:
    return os.path.join(run_dir, FEEDBACK_DIR)


def human_experiment_feedback_path(run_dir: str, experiment: str) -> str:
    return os.path.join(run_dir, FEEDBACK_DIR, f"human_feedback_{experiment}.md")


def style_preferences_path() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent.parent / STYLE_PREFERENCES_REL)


def normalize_relative_path(run_dir: str, path: str) -> str:
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.relpath(path, run_dir)
    return path


# ---------------------------------------------------------------------------
# Paper composite paths
# ---------------------------------------------------------------------------


def figure_classification_path(run_dir: str) -> str:
    return os.path.join(run_dir, FIGURE_CLASSIFICATION)


def assembly_spec_dir(run_dir: str) -> str:
    return os.path.join(run_dir, ASSEMBLY_SPEC_DIR)


def assembly_spec_path(run_dir: str, figure_id: str) -> str:
    return os.path.join(run_dir, ASSEMBLY_SPEC_DIR, f"{figure_id}.json")


def assembly_dir(run_dir: str) -> str:
    return os.path.join(run_dir, ASSEMBLY_DIR)


def figure_assembly_dir(run_dir: str, figure_id: str) -> str:
    return os.path.join(run_dir, ASSEMBLY_DIR, figure_id)


def figure_assembly_code_path(run_dir: str, figure_id: str) -> str:
    return os.path.join(run_dir, ASSEMBLY_DIR, figure_id, "assembly_code.py")


def panel_dir(run_dir: str, figure_id: str, panel_id: str) -> str:
    return os.path.join(run_dir, PANELS_DIR, figure_id, panel_id)


def panel_output_path(run_dir: str, figure_id: str, panel_id: str) -> str:
    return os.path.join(run_dir, PANELS_DIR, figure_id, panel_id, "panel.png")


def paper_figure_output_path(run_dir: str, figure_id: str, ext: str = "png") -> str:
    return os.path.join(run_dir, OUTPUTS_DIR, PAPER_FIGURES_DIR, f"{figure_id}.{ext}")


def paper_figure_plan_path(run_dir: str) -> str:
    return os.path.join(run_dir, PAPER_FIGURE_PLAN)


def color_registry_path(run_dir: str) -> str:
    return os.path.join(run_dir, COLOR_REGISTRY)


def data_distribution_path(run_dir: str) -> str:
    return os.path.join(run_dir, DATA_DISTRIBUTION_REPORT)


def ensure_paper_composite_layout(run_dir: str) -> None:
    """Create all required subdirectories for a paper composite run."""
    for subdir in (
        EXPERIMENTS_DIR,
        PANELS_DIR,
        ASSEMBLY_SPEC_DIR,
        ASSEMBLY_DIR,
        DATA_RECOVERY_DIR,
        os.path.join(OUTPUTS_DIR, PAPER_FIGURES_DIR),
        "logs",
    ):
        os.makedirs(os.path.join(run_dir, subdir), exist_ok=True)


def plot_experiment_index_entry(
    run_dir: str,
    experiment: str,
    *,
    beam_variant_specs: list[str] | None = None,
) -> dict[str, object]:
    workspace_dir = experiment_rel_dir(experiment)
    output_dir = os.path.join(OUTPUTS_DIR, experiment)
    entry: dict[str, object] = {
        "description": os.path.join(workspace_dir, EXPERIMENT_DESCRIPTION),
        "styled_spec": os.path.join(workspace_dir, STYLED_SPEC),
        "workspace_dir": workspace_dir,
        "output_dir": output_dir,
        "debug_dir": os.path.join(DEBUG_DIR, experiment),
        "figure_code": os.path.join(workspace_dir, FIGURE_CODE),
        "critic_result": os.path.join(workspace_dir, CRITIC_RESULT),
        "figure_output": os.path.join(output_dir, "figure.png"),
    }
    if beam_variant_specs:
        entry["beam_variant_specs"] = [normalize_relative_path(run_dir, path) for path in beam_variant_specs]
    return entry
