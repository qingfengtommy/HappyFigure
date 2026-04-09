"""Planner-stylist prompt building, spec validation, and plan step."""
from __future__ import annotations

import argparse
import os
import sys

import ui
from pipeline.agent_runtime import spawn_subagent
from pipeline.orchestrator import artifacts as orch_art
from pipeline.prompt import PromptComposer, PromptSection, apply_budget
from pipeline.run_state import (
    get_experiments,
    persist_plot_plan_state,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLOT_SPEC_MIN_LINES = 50
CODE_AGENT_MAX_ITERATIONS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def global_style_directive(run_dir: str) -> str:
    """Build the global style instruction for planner-stylist prompts."""
    return (
        f"IMPORTANT — FIRST write a shared global style sheet to {run_dir}/global_style.md "
        f"that ALL experiment figures must follow. This file must specify:\n"
        f"  1. matplotlib rcParams block (font.family, font.size, axes.spines.right=False, "
        f"axes.spines.top=False, legend.frameon=False, figure.dpi=300, tick directions)\n"
        f"  2. Color palette — a named mapping of every method/condition/group to a hex color, "
        f"consistent across every figure. Choose a colorblind-safe palette.\n"
        f"  3. Font size hierarchy (panel_label=22pt bold, axis_label=18pt, tick_label=14pt, "
        f"annotation=14pt, legend=14pt)\n"
        f"  4. Panel label format: bold '(a)', '(b)' etc., placed top-left outside axes\n"
        f"  5. Spine rules: hide top and right, keep left and bottom\n"
        f"  6. Export: dpi=300, bbox_inches='tight', transparent=False\n"
        f"Each per-experiment experiments/<experiment>/styled_spec.md must reference global_style.md and not "
        f"redefine these shared settings. "
    )


def plot_spec_path(run_dir: str, experiment: str, *, variant_idx: int | None = None) -> str:
    if variant_idx is None:
        return orch_art.experiment_styled_spec_path(run_dir, experiment)
    return orch_art.beam_styled_spec_path(run_dir, experiment, variant_idx)


def planner_stylist_context(run_dir: str, experiments_dir: str) -> str:
    experiments_dir = experiments_dir or ""
    results_label = "Results directories" if "," in experiments_dir else "Results directory"
    return (
        f"Plan and style all figures for this publication. "
        f"Run directory: {run_dir}. "
        f"{results_label}: {experiments_dir}. "
        f"Style examples directory: configs/statistical_examples/. "
        f"Read the exploration report at {orch_art.plot_exploration_report_path(run_dir)} "
        f"and the proposal at {run_dir}/proposal.md. "
    )


def build_planner_stylist_prompt(
    run_dir: str,
    experiments_dir: str,
    *,
    write_instruction: str,
    variation_hint: str = "",
    include_style_directive: bool = True,
    experiments: list[str] | None = None,
    feedback_paths: list[str] | None = None,
) -> str:
    pc = PromptComposer()
    pc.add(PromptSection(
        "context",
        planner_stylist_context(run_dir, experiments_dir),
        priority=10,
    ))
    if feedback_paths:
        listing = "\n".join(f"- {p}" for p in feedback_paths)
        pc.add(PromptSection(
            "human_feedback_refs",
            f"## Feedback Files (read before writing specs)\n{listing}",
            priority=15,
        ))
    if include_style_directive:
        pc.add(PromptSection(
            "style_directive",
            global_style_directive(run_dir),
            priority=20,
        ))

    task_parts = [
        "Then browse style examples, plan all figures, and write detailed styled specs "
        "(100+ lines each, all 13 sections). ",
    ]
    if experiments:
        task_parts.append(f"The experiments are: {', '.join(experiments)}. ")
    task_parts.append(write_instruction)
    task_parts.append(" You MUST complete ALL file writes in this single session.")
    if variation_hint:
        task_parts.append(variation_hint)
    pc.add(PromptSection("task", "".join(task_parts), priority=60))

    return pc.compose()


def planner_base_write_instruction(run_dir: str) -> str:
    return (
        f"Write per-experiment styled specs to {run_dir}/experiments/<experiment>/styled_spec.md "
        f"(create directories with mkdir -p). "
        f"Also write the multi-figure plan to {run_dir}/multi_figure_plan.md "
        f"but do NOT modify {run_dir}/state.json; the orchestrator owns it."
    )


def planner_variant_write_instruction(run_dir: str, variant_idx: int) -> str:
    return (
        f"IMPORTANT: Write beam style variant specs to: "
        f"{run_dir}/experiments/<experiment>/styled_spec_s{variant_idx}.md "
        f"(create directories with mkdir -p). "
        f"Do NOT overwrite the base specs in {run_dir}/experiments/<experiment>/styled_spec.md. "
        f"Do NOT overwrite multi_figure_plan.md or state.json."
    )


def validate_plot_specs(run_dir: str, experiments: list[str], *, variant_idx: int | None = None) -> bool:
    if not experiments:
        return False
    for exp in experiments:
        spec = plot_spec_path(run_dir, exp, variant_idx=variant_idx)
        if not os.path.exists(spec):
            return False
        with open(spec) as f:
            if len(f.readlines()) < PLOT_SPEC_MIN_LINES:
                return False
    return True


def report_plot_specs(run_dir: str, experiments: list[str], *, variant_idx: int | None = None) -> None:
    for exp in experiments:
        spec = plot_spec_path(run_dir, exp, variant_idx=variant_idx)
        if os.path.exists(spec):
            with open(spec) as f:
                lines = len(f.readlines())
            if variant_idx is not None:
                label = f"experiments/{exp}/styled_spec_s{variant_idx}.md"
            else:
                label = f"experiments/{exp}/styled_spec.md"
            ui.dim(f"  {label}: {lines} lines")


def _bundled_lines_limit() -> int:
    """Return the max_bundled_lines threshold from config."""
    from graphs.svg_utils import load_pipeline_config
    return load_pipeline_config().get("context", {}).get("max_bundled_lines", 200)


def build_code_agent_prompt(
    run_dir: str,
    experiment: str,
    experiments_dir: str,
    spec_path: str,
    work_dir: str,
    critic_instruction: str,
    global_style_content: str,
    *,
    prior_feedback: str | None = None,
    spec_content: str | None = None,
    global_style_path: str | None = None,
    feedback_paths: list[str] | None = None,
) -> str:
    max_lines = _bundled_lines_limit()
    pc = PromptComposer()

    experiments_dir = experiments_dir or ""
    results_label = "Results directories" if "," in experiments_dir else "Results directory"
    pc.add(PromptSection(
        "context",
        f"Generate the figure for experiment: {experiment}. "
        f"Run directory: {run_dir}. "
        f"{results_label}: {experiments_dir}. "
        f"Styled spec: {spec_path}.",
        priority=10,
    ))
    if feedback_paths:
        listing = "\n".join(f"- {p}" for p in feedback_paths)
        pc.add(PromptSection(
            "human_feedback_refs",
            f"## Feedback Files (read before writing code)\n{listing}",
            priority=15,
        ))

    if global_style_content and global_style_path:
        preamble = (
            "CRITICAL: Apply the following global style EXACTLY — rcParams, color palette, "
            "font sizes, spine rules, and panel label format. "
            "All figures in this run MUST share the same visual style.\n\n"
        )
        pc.add_bundled(
            "global_style", global_style_path,
            preamble + global_style_content,
            priority=20, max_lines=max_lines,
        )
    elif global_style_content:
        # No path available — inline only (legacy callers)
        pc.add(PromptSection("global_style", global_style_content, priority=20))

    # Color registry — ensures cross-figure color consistency
    color_reg_path = os.path.join(run_dir, "color_registry.json")
    if os.path.exists(color_reg_path):
        pc.add(PromptSection(
            "color_registry",
            f"Color registry (cross-figure consistency): {color_reg_path}\n"
            f"Read this JSON file and use its hex colors for ALL data categories. "
            f"Do NOT use default matplotlib colors when registry colors are available.",
            priority=25,
        ))

    if spec_content:
        pc.add_bundled(
            "spec_content", spec_path, spec_content,
            priority=50, max_lines=max_lines,
        )

    pc.add(PromptSection(
        "task",
        f"Write Python figure code, execute it, {critic_instruction}, "
        f"and iterate up to {CODE_AGENT_MAX_ITERATIONS} times. "
        f"Save figure_code.py, figure output PNGs, and critic_result.json all to {work_dir}/. "
        f"IMPORTANT: Archive each iteration's code as figure_code_iter{{N}}.py before modifying it, "
        f"and archive each iteration's critic result as critic_result_iter{{N}}.json. "
        f"This preserves the full history of attempts. "
        f"Do NOT modify {run_dir}/state.json; the orchestrator owns it.",
        priority=60,
    ))

    if prior_feedback:
        pc.add(PromptSection(
            "prior_feedback",
            f"IMPORTANT: This is a beam search refinement. Previous attempts received "
            f"the following feedback history — use ALL of it to avoid repeating mistakes "
            f"and to build on what worked:\n{prior_feedback}",
            priority=70,
            max_chars=3000,
        ))

    # Apply budget: prefer path-ref conversion over truncation.
    from graphs.svg_utils import load_pipeline_config
    cfg = load_pipeline_config()
    budget = cfg.get("context", {}).get("initial_prompt_budget", 30_000)
    apply_budget(pc, budget_tokens=budget)

    return pc.compose()


# ---------------------------------------------------------------------------
# Orchestration step
# ---------------------------------------------------------------------------


def step_plan_and_style(run_dir: str, args: argparse.Namespace) -> list[str]:
    """Step 2: Generate styled specs via planner-stylist with Python fallback."""
    experiments_dir = os.path.abspath(args.experiments_dir) if args.experiments_dir else ""

    from pipeline.feedback import collect_feedback_paths
    fb_paths = collect_feedback_paths(run_dir, "design")

    prompt = build_planner_stylist_prompt(
        run_dir,
        experiments_dir,
        write_instruction=planner_base_write_instruction(run_dir),
        feedback_paths=fb_paths,
    )
    spawn_subagent(
        "planner-stylist",
        prompt,
        verbose=args.verbose,
        log_dir=os.path.join(run_dir, "logs"),
        log_name="planner-stylist",
    )

    experiments = get_experiments(run_dir)

    if not validate_plot_specs(run_dir, experiments):
        ui.warn("LLM planner-stylist did not produce valid specs. Falling back to Python generator.")
        from graphs.spec_fallback import generate_specs_python
        experiments = generate_specs_python(run_dir, experiments_dir)

    if not experiments:
        ui.error("No experiments generated.")
        sys.exit(1)

    persist_plot_plan_state(run_dir, experiments, execution=getattr(args, "execution", "sequential"))

    report_plot_specs(run_dir, experiments)
    ui.success(f"Planning complete. {len(experiments)} experiments: {experiments}")
    return experiments
