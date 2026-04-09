"""Panel dispatch and assembly orchestration for paper composite mode.

Coordinates type-dispatched panel generation (statistical, diagram,
hybrid, placeholder) and figure assembly for the ``paper`` CLI command.
Used by the python-stages orchestrator; agent-first mode handles dispatch
within the main orchestrator session.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import defaultdict

import ui
from pipeline.assembly import (
    execute_assembly_script,
    generate_assembly_script,
    load_assembly_spec,
    parse_assembly_spec,
    render_placeholder_png,
    validate_assembly_deterministic,
)
from pipeline.contracts import (
    AssemblyResult,
    DesignResult,
    FigureClassification,
    PanelEntry,
    PanelType,
)
from pipeline.figure_lint import lint_cross_panel_consistency
from pipeline.orchestrator import artifacts as art


# ---------------------------------------------------------------------------
# Classification loading
# ---------------------------------------------------------------------------


def load_figure_classification(run_dir: str) -> FigureClassification:
    """Load figure_classification.json from a run directory."""
    path = art.figure_classification_path(run_dir)
    with open(path, encoding="utf-8") as f:
        return FigureClassification.from_dict(json.load(f))


def partition_panels(
    classification: FigureClassification,
) -> dict[PanelType, list[tuple[str, str, PanelEntry]]]:
    """Partition all panels by type.

    Returns:
        Dict mapping PanelType to list of (figure_id, panel_id, PanelEntry).
    """
    result: dict[PanelType, list[tuple[str, str, PanelEntry]]] = defaultdict(list)
    for fig_id, fig in classification.figures.items():
        for panel_id, panel in fig.panels.items():
            result[panel.panel_type].append((fig_id, panel_id, panel))
    return result


def _copy_to_panel_dir(
    src: str, run_dir: str, fig_id: str, panel_id: str,
) -> None:
    """Copy a generated output to the canonical panel directory."""
    dst = art.panel_output_path(run_dir, fig_id, panel_id)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# Panel generation by type
# ---------------------------------------------------------------------------


def generate_statistical_panels(
    run_dir: str,
    panels: list[tuple[str, str, PanelEntry]],
    args: argparse.Namespace,
    design: DesignResult,
) -> None:
    """Generate statistical panels via the existing plot execution pipeline.

    Each panel maps to an experiment slug and uses the standard code-agent flow.
    Respects the --execution flag (sequential/parallel/beam).
    """
    from pipeline.orchestrator.strategies import execute_plot_strategy

    experiments = []
    for fig_id, panel_id, panel in panels:
        experiments.append(panel.slug)
        ws = art.experiment_dir(run_dir, panel.slug)
        os.makedirs(ws, exist_ok=True)

        desc_path = art.experiment_description_path(run_dir, panel.slug)
        if not os.path.exists(desc_path):
            with open(desc_path, "w", encoding="utf-8") as f:
                f.write(f"# {fig_id} Panel {panel_id}\n\n{panel.description}\n")

    if not experiments:
        return

    ui.info(f"Generating {len(experiments)} statistical panels")
    execution = getattr(args, "execution", "sequential")
    # Create a paper-specific DesignResult with panel slugs as experiments
    paper_design = DesignResult(
        mode=design.mode,
        artifacts=design.artifacts,
        experiments=experiments,
        variant_specs=design.variant_specs,
    )
    execute_plot_strategy(execution, run_dir, args, paper_design)

    for fig_id, panel_id, panel in panels:
        src = os.path.join(run_dir, art.OUTPUTS_DIR, panel.slug, "figure.png")
        if os.path.exists(src):
            _copy_to_panel_dir(src, run_dir, fig_id, panel_id)


def generate_diagram_panels(
    run_dir: str,
    panels: list[tuple[str, str, PanelEntry]],
    args: argparse.Namespace,
) -> None:
    """Generate diagram panels via svg-builder + svg-refiner.

    Each diagram panel gets its own working directory under
    ``experiments/<slug>/diagram/`` to avoid artifact collisions when
    multiple diagrams are generated in the same run.
    """
    from pipeline.drawing import step_svg_build, step_svg_refine

    for fig_id, panel_id, panel in panels:
        slug = panel.slug
        panel_work_dir = _setup_diagram_work_dir(run_dir, slug, panel)
        ui.info(f"Generating diagram panel: {fig_id}/{panel_id} → {panel_work_dir}")

        step_svg_build(panel_work_dir, args)
        step_svg_refine(panel_work_dir, args)

        # Copy final output to canonical panel location
        for src_name in ("method_architecture.png", "method_architecture.svg",
                         "final.svg", "figure.png"):
            src = os.path.join(panel_work_dir, src_name)
            if os.path.exists(src):
                _copy_to_panel_dir(src, run_dir, fig_id, panel_id)
                if src_name.endswith(".svg"):
                    # Also copy SVG for vector output
                    svg_dst = art.panel_output_path(run_dir, fig_id, panel_id).replace(
                        "panel.png", "panel.svg"
                    )
                    shutil.copy2(src, svg_dst)
                if src_name.endswith(".png"):
                    break  # found the primary PNG


def _setup_diagram_work_dir(
    run_dir: str, slug: str, panel: PanelEntry,
) -> str:
    """Create an isolated working directory for one diagram panel.

    Symlinks read-only inputs (method_description.md, proposal.md) and
    creates a panel-specific state.json so the SVG pipeline runs in
    isolation without clobbering other panels.
    """
    work_dir = os.path.join(run_dir, "experiments", slug, "diagram")
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(os.path.join(work_dir, "logs"), exist_ok=True)

    # Symlink read-only inputs from run_dir (avoids redundant copies)
    for fname in ("method_description.md", "proposal.md"):
        src = os.path.join(run_dir, fname)
        dst = os.path.join(work_dir, fname)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.symlink(os.path.abspath(src), dst)
            except OSError:
                shutil.copy2(src, dst)  # fallback if symlinks unsupported

    # Create a panel-specific method_description if the panel has one
    # and no shared one was linked above
    desc_path = os.path.join(work_dir, "method_description.md")
    if not os.path.exists(desc_path) and panel.description:
        with open(desc_path, "w", encoding="utf-8") as f:
            f.write(f"# {panel.description}\n\n")
            f.write("Generate a diagram for: " + panel.description + "\n")

    # Create state.json pointing to this work_dir
    state_src = os.path.join(run_dir, "state.json")
    state_dst = os.path.join(work_dir, "state.json")
    if os.path.exists(state_src) and not os.path.exists(state_dst):
        with open(state_src, encoding="utf-8") as f:
            state = json.load(f)
        # Override run_dir in state so the pipeline writes here
        state["run_dir"] = work_dir
        state["diagram_panel_slug"] = slug
        if panel.description:
            state["method_description"] = panel.description
        with open(state_dst, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    return work_dir


def create_placeholder_panels(
    run_dir: str,
    panels: list[tuple[str, str, PanelEntry]],
) -> None:
    """Generate labeled gray placeholders for non-generatable panels."""
    for fig_id, panel_id, panel in panels:
        dst = art.panel_output_path(run_dir, fig_id, panel_id)
        strategy = panel.placeholder_strategy or "labeled_gray"

        if strategy == "source_image" and panel.source_image and os.path.exists(panel.source_image):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(panel.source_image, dst)
        else:
            desc = panel.description or panel_id
            render_placeholder_png(
                dst, panel_id, panel.panel_type.value,
                text=f"Panel ({panel_id})\n{desc}\n[to be provided]",
            )


# ---------------------------------------------------------------------------
# Figure assembly
# ---------------------------------------------------------------------------


def assemble_all_figures(
    run_dir: str,
    classification: FigureClassification,
    args: argparse.Namespace,
    *,
    max_iterations: int = 3,
) -> list[AssemblyResult]:
    """Assemble all figures from their panel outputs."""
    results: list[AssemblyResult] = []

    for fig_id in classification.figures:
        spec_path = art.assembly_spec_path(run_dir, fig_id)
        if not os.path.exists(spec_path):
            ui.warn(f"No assembly spec for {fig_id}, skipping assembly")
            continue

        spec = load_assembly_spec(spec_path)
        result = _assemble_one_figure(run_dir, fig_id, spec,
                                       max_iterations=max_iterations,
                                       classification=classification)
        results.append(result)

    return results


def _assemble_one_figure(
    run_dir: str,
    fig_id: str,
    spec: dict,
    *,
    max_iterations: int = 3,
    classification: FigureClassification | None = None,
) -> AssemblyResult:
    """Assemble one figure with validation and iteration."""
    parsed = parse_assembly_spec(spec)

    # Look up panel generatability from classification to detect failed generation
    fig_entry = classification.figures.get(fig_id) if classification else None
    panel_paths: dict[str, str] = {}
    generated = 0
    placeholder = 0
    failed_panels: list[str] = []

    for panel_id in parsed.panel_ids:
        panel_png = art.panel_output_path(run_dir, fig_id, panel_id)
        if os.path.exists(panel_png):
            panel_paths[panel_id] = panel_png
            generated += 1
        else:
            # Check if this panel was supposed to be generated
            panel_meta = fig_entry.panels.get(panel_id) if fig_entry else None
            if panel_meta and panel_meta.generatable:
                failed_panels.append(panel_id)
                ui.warn(f"Panel {fig_id}/{panel_id} was generatable but output is missing")
            placeholder += 1

    if failed_panels:
        ui.warn(
            f"{fig_id}: {len(failed_panels)} generatable panel(s) missing: "
            f"{', '.join(failed_panels)}. They will appear as placeholders."
        )

    total = generated + placeholder
    output_path = art.paper_figure_output_path(run_dir, fig_id)

    asm_dir = art.figure_assembly_dir(run_dir, fig_id)
    os.makedirs(asm_dir, exist_ok=True)

    best_score: float | None = None
    best_output: str = ""
    iters_used = 0

    # Try PIL-based assembly first (pixel-perfect, no re-rasterization blur).
    # Falls back to matplotlib script if PIL assembly fails.
    from graphs.svg_utils import load_pipeline_config
    assembly_cfg = load_pipeline_config().get("assembly", {})
    use_pil = assembly_cfg.get("method", "pil") == "pil"

    if use_pil:
        from pipeline.assembly import assemble_pil
        ui.dim(f"Assembling {fig_id} via PIL (pixel-perfect)")
        pil_ok = assemble_pil(spec, panel_paths, output_path)
        if pil_ok:
            iters_used = 1
            issues = validate_assembly_deterministic(output_path, spec)
            det_check_path = os.path.join(asm_dir, "deterministic_check.json")
            with open(det_check_path, "w", encoding="utf-8") as f:
                json.dump({"iteration": 1, "method": "pil", "passed": not issues, "issues": issues}, f, indent=2)
            if not issues:
                ui.success(f"Assembly {fig_id}: PIL assembly passed")
                best_output = output_path
                best_score = 16.0
            else:
                ui.warn(f"Assembly {fig_id} PIL issues: {issues} — falling back to matplotlib")
                use_pil = False
        else:
            ui.warn(f"PIL assembly failed for {fig_id} — falling back to matplotlib")
            use_pil = False

    if not use_pil or best_score is None:
        for iteration in range(1, max_iterations + 1):
            iters_used = iteration

            script_text = generate_assembly_script(spec, panel_paths, output_path)
            script_path = art.figure_assembly_code_path(run_dir, fig_id)
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script_text)

            success, error = execute_assembly_script(script_path)
            if not success:
                ui.warn(f"Assembly script failed for {fig_id} (iter {iteration}): {error}")
                continue

            iter_path = os.path.join(asm_dir, f"assembly_iter{iteration}.png")
            if os.path.exists(output_path):
                shutil.copy2(output_path, iter_path)

            issues = validate_assembly_deterministic(output_path, spec)
            det_check_path = os.path.join(asm_dir, "deterministic_check.json")
            with open(det_check_path, "w", encoding="utf-8") as f:
                json.dump({"iteration": iteration, "passed": not issues, "issues": issues}, f, indent=2)

            if issues:
                ui.warn(f"Assembly validation issues for {fig_id}: {issues}")
            else:
                ui.success(f"Assembly {fig_id} iter {iteration}: deterministic checks passed")
                best_output = output_path
                best_score = 16.0
                break

            best_output = output_path

    result = AssemblyResult(
        figure_id=fig_id,
        total_panels=total,
        generated_panels=generated,
        placeholder_panels=placeholder,
        assembly_score=best_score,
        iterations_used=iters_used,
        output_path=best_output,
        deterministic_checks_passed=best_score is not None,
    )
    result_path = os.path.join(asm_dir, "assembly_result.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2)

    return result


# ---------------------------------------------------------------------------
# Top-level composite pipeline
# ---------------------------------------------------------------------------


def run_composite_pipeline(
    run_dir: str,
    args: argparse.Namespace,
    design: DesignResult,
) -> list[AssemblyResult]:
    """Full paper composite generate + assemble pipeline.

    Services are NOT managed here — the caller (strategies.py or main.py)
    is responsible for starting/stopping services when diagram panels exist.
    """
    classification = load_figure_classification(run_dir)
    partitioned = partition_panels(classification)

    stat_panels = partitioned.get(PanelType.STATISTICAL, [])
    if stat_panels:
        ui.section(f"Generating {len(stat_panels)} statistical panels")
        generate_statistical_panels(run_dir, stat_panels, args, design)

    diag_panels = partitioned.get(PanelType.DIAGRAM, [])
    hybrid_panels = partitioned.get(PanelType.HYBRID, [])
    all_diagram_like = diag_panels + hybrid_panels
    if len(all_diagram_like) > 1:
        ui.warn(
            f"Multiple diagram/hybrid panels ({len(all_diagram_like)}) share one run_dir. "
            "Only the last panel's output will be captured. "
            "Use agent-first mode for multi-diagram papers."
        )
    if diag_panels:
        ui.section(f"Generating {len(diag_panels)} diagram panels")
        generate_diagram_panels(run_dir, diag_panels, args)
    if hybrid_panels:
        ui.section(f"Generating {len(hybrid_panels)} hybrid panels")
        generate_diagram_panels(run_dir, hybrid_panels, args)

    placeholder_panels = partitioned.get(PanelType.PLACEHOLDER, [])
    if placeholder_panels:
        ui.dim(f"Creating {len(placeholder_panels)} placeholder panels")
        create_placeholder_panels(run_dir, placeholder_panels)

    # ── Cross-panel lint ────────────────────────────────────────────
    # Check consistency across all generated panel code files before assembly.
    panel_code_paths: dict[str, str] = {}
    for fig_id, fig in classification.figures.items():
        for panel_id, panel in fig.panels.items():
            if panel.panel_type == PanelType.STATISTICAL:
                code_path = os.path.join(
                    run_dir, "experiments", panel.slug, "figure_code.py"
                )
                if os.path.exists(code_path):
                    panel_code_paths[panel.slug] = code_path
    if len(panel_code_paths) >= 2:
        consistency = lint_cross_panel_consistency(panel_code_paths)
        if consistency.issues:
            ui.warn(f"Cross-panel lint: {consistency.summary()}")
            for issue in consistency.issues:
                ui.warn(f"  - {issue}")
        elif consistency.warnings:
            for w in consistency.warnings:
                ui.dim(f"  Cross-panel: {w}")

    if getattr(args, "skip_assembly", False):
        ui.dim("Skipping assembly (--skip-assembly)")
        return []

    ui.section("Assembling paper figures")
    from graphs.svg_utils import load_pipeline_config
    config = load_pipeline_config().get("assembly", {})
    max_iters = config.get("max_iterations", 3)
    results = assemble_all_figures(run_dir, classification, args,
                                   max_iterations=max_iters)

    for r in results:
        status = "OK" if r.deterministic_checks_passed else "ISSUES"
        ui.info(
            f"  {r.figure_id}: {r.generated_panels}/{r.total_panels} panels, "
            f"{r.iterations_used} iters, {status}"
        )

    # Cross-figure consistency check
    if len(results) >= 2 and config.get("cross_figure_check", True):
        from pipeline.assembly import cross_figure_consistency_check
        figure_ids = [r.figure_id for r in results]
        checks = cross_figure_consistency_check(run_dir, figure_ids)
        if checks.get("passed"):
            ui.success("Cross-figure consistency: passed")
        else:
            for issue in checks.get("issues", []):
                ui.warn(f"Cross-figure: {issue}")

    return results
