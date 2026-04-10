"""Execution-strategy registry for statistical plot generation.

This is the orchestrator-facing seam between the stage driver and the legacy
Python execution helpers.  It keeps strategy selection in one place so the
current Python-owned implementations can later be replaced by a single
main-session agent without changing the stage contract again.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from typing import Literal

from pipeline.contracts import ArtifactKeys, DesignResult, ExplorationResult
from pipeline.orchestrator import artifacts as orch_art

ExecutionStrategy = Literal["sequential", "parallel", "beam"]
PlotExecutionHandler = Callable[[str, argparse.Namespace, DesignResult], None]
DesignMode = Literal["exp_plot", "composite", "agent_svg", "paper_composite"]
DesignHandler = Callable[[ExplorationResult, argparse.Namespace, str], DesignResult]
GenerateMode = Literal["exp_plot", "composite", "agent_svg", "paper_composite"]
GenerateHandler = Callable[[str, argparse.Namespace, DesignResult, str], None]

_T = Callable  # generic callable for the registry helper


def _resolve(registry: dict, key: str, label: str) -> _T:
    try:
        return registry[key]
    except KeyError as exc:
        supported = ", ".join(registry.keys())
        raise ValueError(f"Unknown {label} '{key}'. Expected one of: {supported}") from exc


def _run_plot_sequential(run_dir: str, args: argparse.Namespace, design: DesignResult) -> None:
    from pipeline.plot_execution import step_execute_sequential

    step_execute_sequential(run_dir, design.experiments, args)


def _run_plot_parallel(run_dir: str, args: argparse.Namespace, design: DesignResult) -> None:
    from pipeline.plot_execution import step_execute_parallel

    step_execute_parallel(run_dir, design.experiments, args)


def _run_plot_beam(run_dir: str, args: argparse.Namespace, design: DesignResult) -> None:
    from pipeline.plot_beam import step_execute_beam

    step_execute_beam(
        run_dir,
        design.experiments,
        args,
        variant_specs=design.variant_specs,
    )


_PLOT_EXECUTION_HANDLERS: dict[ExecutionStrategy, PlotExecutionHandler] = {
    "sequential": _run_plot_sequential,
    "parallel": _run_plot_parallel,
    "beam": _run_plot_beam,
}


def list_plot_execution_strategies() -> tuple[ExecutionStrategy, ...]:
    return tuple(_PLOT_EXECUTION_HANDLERS.keys())


def resolve_plot_execution_handler(strategy: str) -> PlotExecutionHandler:
    return _resolve(_PLOT_EXECUTION_HANDLERS, strategy, "plot execution strategy")


def execute_plot_strategy(
    strategy: str,
    run_dir: str,
    args: argparse.Namespace,
    design: DesignResult,
) -> None:
    handler = resolve_plot_execution_handler(strategy)
    handler(run_dir, args, design)


def _write_plot_design_summary(
    run_dir: str,
    experiments: list[str],
    execution: str,
    variant_specs: dict[str, list[str]] | None,
) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "pipeline": "exp_plot",
        "execution": execution,
        "experiments": experiments,
        "artifact_layout_version": orch_art.ARTIFACT_LAYOUT_VERSION,
        "experiment_artifacts": {
            exp: orch_art.plot_experiment_index_entry(
                run_dir,
                exp,
                beam_variant_specs=(variant_specs or {}).get(exp),
            )
            for exp in experiments
        },
    }
    if variant_specs:
        payload["beam_variant_specs"] = {
            exp: [orch_art.normalize_relative_path(run_dir, p) for p in paths] for exp, paths in variant_specs.items()
        }
    path = orch_art.design_summary_path(run_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _ensure_diagram_design_artifacts(run_dir: str, exploration: ExplorationResult) -> None:
    if ArtifactKeys.METHOD_DESC not in exploration.artifacts:
        return

    design_path = orch_art.diagram_design_spec_path(run_dir)
    if not os.path.exists(design_path):
        os.makedirs(run_dir, exist_ok=True)
        with open(design_path, "w", encoding="utf-8") as f:
            f.write(
                "## Diagram design spec\n\n"
                "This run uses **`method_description.md`** as the primary architectural "
                "source for svg-builder / svg-author.\n\n"
                "### Layout\n"
                "- Direction: see Visual Notes in `method_description.md`\n"
                "- Grouping: as described in Components\n\n"
                "### Viz panels\n"
                "- As listed under Visualization Panels in `method_description.md`\n"
            )

    summary_path = orch_art.design_summary_path(run_dir)
    if not os.path.exists(summary_path):
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "schema_version": 1,
                    "pipeline": "diagram",
                    "artifact_layout_version": orch_art.ARTIFACT_LAYOUT_VERSION,
                    "primary_design": exploration.artifacts.get(ArtifactKeys.METHOD_DESC),
                    "diagram_design_spec": orch_art.DIAGRAM_DESIGN_SPEC,
                },
                f,
                indent=2,
            )


def _design_plot(exploration: ExplorationResult, args: argparse.Namespace, mode: str) -> DesignResult:
    from pipeline.plot_beam import step_plan_and_style_beam
    from pipeline.plot_planning import plot_spec_path, step_plan_and_style

    run_dir = exploration.run_dir
    execution = getattr(args, "execution", "sequential")
    variant_specs = None
    if execution == "beam":
        experiments, variant_specs = step_plan_and_style_beam(run_dir, args)
    else:
        experiments = step_plan_and_style(run_dir, args)

    _write_plot_design_summary(run_dir, experiments, execution, variant_specs)

    artifacts: dict[str, str] = {
        ArtifactKeys.GLOBAL_STYLE: orch_art.GLOBAL_STYLE,
        ArtifactKeys.PLAN: orch_art.MULTI_FIGURE_PLAN,
        "design_summary": orch_art.DESIGN_SUMMARY,
    }
    for exp in experiments:
        rel = os.path.relpath(plot_spec_path(run_dir, exp), run_dir)
        artifacts[ArtifactKeys.spec(exp)] = rel

    if variant_specs:
        vs_path = os.path.join(run_dir, "beam_variant_specs.json")
        normalized_variant_specs = {
            exp: [orch_art.normalize_relative_path(run_dir, p) for p in paths] for exp, paths in variant_specs.items()
        }
        with open(vs_path, "w", encoding="utf-8") as f:
            json.dump(normalized_variant_specs, f, indent=2)
        artifacts["beam_variant_specs"] = "beam_variant_specs.json"
        variant_specs = normalized_variant_specs

    return DesignResult(
        mode=mode,
        artifacts=artifacts,
        experiments=experiments,
        variant_specs=variant_specs,
    )


def _design_diagram(exploration: ExplorationResult, _args: argparse.Namespace, mode: str) -> DesignResult:
    run_dir = exploration.run_dir
    design_artifacts: dict[str, str] = {}
    if ArtifactKeys.METHOD_DESC in exploration.artifacts:
        design_artifacts[ArtifactKeys.PRIMARY_DESIGN] = exploration.artifacts[ArtifactKeys.METHOD_DESC]
        _ensure_diagram_design_artifacts(run_dir, exploration)
        design_artifacts["diagram_design_spec"] = orch_art.DIAGRAM_DESIGN_SPEC
        design_artifacts["design_summary"] = orch_art.DESIGN_SUMMARY
    elif ArtifactKeys.IMAGE in exploration.artifacts:
        design_artifacts[ArtifactKeys.IMAGE] = exploration.artifacts[ArtifactKeys.IMAGE]

    return DesignResult(
        mode=mode,
        artifacts=design_artifacts,
        experiments=[],
    )


def _design_paper_composite(exploration: ExplorationResult, args: argparse.Namespace, mode: str) -> DesignResult:
    """Unified paper-level design: merges data + method exploration, classifies figures."""
    run_dir = exploration.run_dir
    design_artifacts: dict[str, str] = {}

    # Collect any available exploration artifacts
    if ArtifactKeys.REPORT in exploration.artifacts:
        design_artifacts[ArtifactKeys.REPORT] = exploration.artifacts[ArtifactKeys.REPORT]
    if ArtifactKeys.METHOD_DESC in exploration.artifacts:
        design_artifacts[ArtifactKeys.PRIMARY_DESIGN] = exploration.artifacts[ArtifactKeys.METHOD_DESC]

    # Check if figure_classification.json was produced (by agent or planner)
    fc_path = orch_art.figure_classification_path(run_dir)
    if os.path.exists(fc_path):
        design_artifacts[ArtifactKeys.FIGURE_CLASSIFICATION] = orch_art.FIGURE_CLASSIFICATION

    # Assembly specs are late-bound (written during ASSEMBLE, not PLAN).
    # They are indexed under the "assemble" manifest stage, not here.

    # For any statistical panels, run plot planning if exploration found experiments
    experiments: list[str] = list(exploration.experiments)
    variant_specs = None
    if experiments:
        try:
            from pipeline.plot_planning import step_plan_and_style

            experiments = step_plan_and_style(run_dir, args)
            design_artifacts[ArtifactKeys.GLOBAL_STYLE] = orch_art.GLOBAL_STYLE
            design_artifacts[ArtifactKeys.PLAN] = orch_art.MULTI_FIGURE_PLAN
        except Exception as exc:
            import ui

            ui.warn(f"Plot planning skipped for paper_composite: {exc}")

    # Ensure diagram design artifacts if method description exists
    if ArtifactKeys.METHOD_DESC in exploration.artifacts:
        _ensure_diagram_design_artifacts(run_dir, exploration)

    # Write design summary
    summary_path = orch_art.design_summary_path(run_dir)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "schema_version": 1,
                "pipeline": "paper_composite",
                "artifact_layout_version": orch_art.ARTIFACT_LAYOUT_VERSION,
                "experiments": experiments,
                "has_figure_classification": os.path.exists(fc_path),
            },
            f,
            indent=2,
        )
    design_artifacts["design_summary"] = orch_art.DESIGN_SUMMARY

    return DesignResult(
        mode=mode,
        artifacts=design_artifacts,
        experiments=experiments,
        variant_specs=variant_specs,
    )


_DESIGN_HANDLERS: dict[DesignMode, DesignHandler] = {
    "exp_plot": _design_plot,
    "composite": _design_diagram,
    "agent_svg": _design_diagram,
    "paper_composite": _design_paper_composite,
}


def resolve_design_handler(mode: str) -> DesignHandler:
    return _resolve(_DESIGN_HANDLERS, mode, "design mode")


def execute_design_strategy(
    exploration: ExplorationResult,
    args: argparse.Namespace,
    mode: str,
) -> DesignResult:
    handler = resolve_design_handler(mode)
    return handler(exploration, args, mode)


def _generate_plot(run_dir: str, args: argparse.Namespace, design: DesignResult, _mode: str) -> None:
    execution = getattr(args, "execution", "sequential")
    execute_plot_strategy(execution, run_dir, args, design)


def _generate_composite(run_dir: str, args: argparse.Namespace, _design: DesignResult, _mode: str) -> None:
    from pipeline.drawing import step_svg_build, step_svg_refine, step_viz_compose
    from pipeline.pipeline_backend import start_services, stop_services

    start_services()
    try:
        step_svg_build(run_dir, args)
    finally:
        stop_services()
    step_svg_refine(run_dir, args)
    if not getattr(args, "skip_viz_compose", False):
        step_viz_compose(run_dir, args)


def _generate_agent_svg(run_dir: str, args: argparse.Namespace, _design: DesignResult, _mode: str) -> None:
    from pipeline.drawing import step_svg_author

    step_svg_author(run_dir, args)


def _generate_paper_composite(run_dir: str, args: argparse.Namespace, _design: DesignResult, _mode: str) -> None:
    from pipeline.composite_generation import load_figure_classification, run_composite_pipeline

    # Start services only if diagram/hybrid panels exist
    fc_path = orch_art.figure_classification_path(run_dir)
    needs_services = False
    if os.path.exists(fc_path):
        classification = load_figure_classification(run_dir)
        needs_services = classification.needs_services

    if needs_services:
        from pipeline.pipeline_backend import start_services, stop_services

        start_services()
        try:
            run_composite_pipeline(run_dir, args, _design)
        finally:
            stop_services()
    else:
        run_composite_pipeline(run_dir, args, _design)


_GENERATE_HANDLERS: dict[GenerateMode, GenerateHandler] = {
    "exp_plot": _generate_plot,
    "composite": _generate_composite,
    "agent_svg": _generate_agent_svg,
    "paper_composite": _generate_paper_composite,
}


def resolve_generate_handler(mode: str) -> GenerateHandler:
    return _resolve(_GENERATE_HANDLERS, mode, "generate mode")


def execute_generate_strategy(
    run_dir: str,
    args: argparse.Namespace,
    mode: str,
    design: DesignResult,
) -> None:
    handler = resolve_generate_handler(mode)
    handler(run_dir, args, design, mode)
