"""SVG/diagram/sketch/composite pipeline steps."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import ui
from pipeline.agent_runtime import require_agent_success, spawn_subagent
from pipeline.context import PROJECT_ROOT


# ---------------------------------------------------------------------------
# Step: init drawing image (skip method-explore when image is provided)
# ---------------------------------------------------------------------------


def step_init_drawing_image(args: argparse.Namespace) -> str:
    """Skip method-explore when a drawing image is provided.

    Creates run_dir, copies the image, runs pipeline_cli init, and sets up
    state.json for the svg-builder agent to work in image-replication mode
    (no method_description.md, purely visual).
    """
    proposal = os.path.abspath(args.proposal) if args.proposal else ""
    raw_exp = getattr(args, "experiments_dir", None) or ""
    first_experiments_dir = raw_exp.split(",")[0].strip() if raw_exp else ""

    # Create run_dir
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    runs_dir = Path.cwd() / "runs" / "diagram_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir = str(runs_dir / f"run_{ts}")
    os.makedirs(run_dir, exist_ok=True)

    # Copy drawing image to run_dir/figure.png
    import shutil

    src = os.path.abspath(args.drawing_image)
    dst = os.path.join(run_dir, "figure.png")
    shutil.copy2(src, dst)
    ui.info(f"Copied drawing image: {src} -> {dst}")
    ui.info(f"Run dir: {run_dir}")
    ui.dim("Skipping method-explore (user-provided image)")

    # If no proposal provided, create a minimal placeholder so pipeline_cli init works
    if not proposal:
        proposal = os.path.join(run_dir, "proposal.md")
        Path(proposal).write_text(
            "# Image Replication\n\nReplicate the provided drawing image as SVG.\n",
            encoding="utf-8",
        )

    # Run pipeline init to create state.json
    llm_preset = getattr(args, "llm_preset", None)
    init_cmd = [
        sys.executable,
        "scripts/pipeline_cli.py",
        "init",
        "--proposal",
        proposal,
        "--run-dir",
        run_dir,
        "--mode",
        "composite",
    ]
    if llm_preset:
        init_cmd.extend(["--llm-preset", llm_preset])
    if first_experiments_dir:
        init_cmd += ["--results-dir", first_experiments_dir]
    subprocess.run(init_cmd, cwd=str(PROJECT_ROOT), check=True)

    # Mark state as user-provided image, no method description
    state_path = os.path.join(run_dir, "state.json")
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
        state["user_provided_image"] = True
        state["generated_image_path"] = dst
        state["completed_steps"] = ["init-drawing-image"]
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)

    ui.success("Init complete. Ready for svg-builder.")
    return run_dir


# ---------------------------------------------------------------------------
# Step 1: method exploration
# ---------------------------------------------------------------------------


def step_method_explore(args: argparse.Namespace) -> str:
    """Step 1: Run @method-explore agent.

    The agent reads the proposal, explores data, and writes:
      - method_exploration_report.md
      - method_description.md
    Then the orchestrator runs pipeline_cli init.
    """
    proposal = os.path.abspath(args.proposal) if args.proposal else ""
    raw_exp = getattr(args, "experiments_dir", None) or ""
    first_experiments_dir = raw_exp.split(",")[0].strip() if raw_exp else ""

    # Create run_dir
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    runs_dir = Path.cwd() / "runs" / "diagram_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir = str(runs_dir / f"run_{ts}")
    os.makedirs(run_dir, exist_ok=True)

    ui.section("Step 1: Method exploration")
    ui.info(f"Run dir: {run_dir}")

    prompt = (
        f"Explore the research proposal and write a method description for architecture diagram generation. "
        f"Proposal: {proposal}. "
    )
    if raw_exp:
        results_label = "Results directories" if "," in raw_exp else "Results directory"
        prompt += f"{results_label}: {raw_exp}. "
    prompt += (
        f"Write the exploration report to {run_dir}/method_exploration_report.md "
        f"and the method description to {run_dir}/method_description.md."
    )

    log_dir = os.path.join(run_dir, "logs")
    rc = spawn_subagent("method-explore", prompt, verbose=getattr(args, "verbose", False), log_dir=log_dir)
    require_agent_success("method-explore", rc)

    # Validate outputs
    md_path = os.path.join(run_dir, "method_description.md")
    if not os.path.exists(md_path):
        ui.error("method_description.md not created by agent")
        sys.exit(1)
    with open(md_path) as f:
        lines = f.readlines()
    if len(lines) < 10:
        ui.warn(f"method_description.md is short ({len(lines)} lines)")

    # Run pipeline init to create state.json
    llm_preset = getattr(args, "llm_preset", None)
    init_cmd = [
        sys.executable,
        "scripts/pipeline_cli.py",
        "init",
        "--proposal",
        proposal,
        "--run-dir",
        run_dir,
        "--mode",
        "composite",
    ]
    if llm_preset:
        init_cmd.extend(["--llm-preset", llm_preset])
    if first_experiments_dir:
        init_cmd += ["--results-dir", first_experiments_dir]
    subprocess.run(init_cmd, cwd=str(PROJECT_ROOT), check=True)

    # Write method_description into state.json so svg-builder can read it
    state_path = os.path.join(run_dir, "state.json")
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
        with open(md_path) as f:
            state["method_description"] = f.read()
        state["completed_steps"] = state.get("completed_steps", []) + ["method-explore"]
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)

    ui.success(f"Step 1 complete. Method description: {md_path}")
    return run_dir


# ---------------------------------------------------------------------------
# Step 2: SVG building
# ---------------------------------------------------------------------------


def step_svg_build(run_dir: str, args: argparse.Namespace) -> None:
    """Step 2: Run @svg-builder agent.

    The agent generates the raster image, runs SAM3/OCR/BEN2,
    and writes the initial SVG code. Services must already be running.
    """
    ui.section("Step 2: SVG building")

    has_user_image = getattr(args, "drawing_image", None) is not None
    if has_user_image:
        prompt = (
            f"Replicate the provided drawing image as an SVG diagram. "
            f"Run directory: {run_dir}. "
            f"This is IMAGE-REPLICATION MODE: no method_description.md exists. "
            f"Work purely from the image at {run_dir}/figure.png. "
            f"Do NOT generate or regenerate the image. "
            f"Start at Step 5 (SAM3 detection). "
            f"Services are already running (SAM3:8001, OCR:8002, BEN2:8003)."
        )
    else:
        quality_profile = getattr(args, "quality_profile", "journal")
        prompt = (
            f"Build an SVG architecture diagram from the method description. "
            f"Run directory: {run_dir}. Quality profile: {quality_profile}. "
            f"Read method_description.md, view the image, "
            f"run SAM3/OCR/BEN2 segmentation, and write SVG code. "
            f"Services are already running (SAM3:8001, OCR:8002, BEN2:8003)."
        )

    log_dir = os.path.join(run_dir, "logs")
    rc = spawn_subagent("svg-builder", prompt, verbose=getattr(args, "verbose", False), log_dir=log_dir)
    require_agent_success("svg-builder", rc)

    # Validate outputs
    final_svg = os.path.join(run_dir, "final.svg")
    if not os.path.exists(final_svg):
        # Check template.svg as fallback
        template = os.path.join(run_dir, "template.svg")
        if os.path.exists(template):
            ui.warn("final.svg missing but template.svg exists — proceeding")
        else:
            ui.error("No SVG output from svg-builder agent")
            sys.exit(1)

    rendered = os.path.join(run_dir, "method_architecture_v0.png")
    if not os.path.exists(rendered):
        ui.warn("method_architecture_v0.png not found")

    ui.success(f"Step 2 complete. SVG: {final_svg}")


# ---------------------------------------------------------------------------
# Step 3: SVG refinement
# ---------------------------------------------------------------------------


def step_svg_refine(run_dir: str, args: argparse.Namespace) -> None:
    """Step 3: Run @svg-refiner agent.

    The refiner compares the rendered SVG against figure.png element-by-element,
    fixes position/visual/overlap issues, and iterates until no fixable issues remain.
    """
    ui.section("Step 3: SVG refinement")

    quality_profile = getattr(args, "quality_profile", "journal")
    max_iters = getattr(args, "max_team_iterations", 3)
    prompt = (
        f"Refine the SVG architecture diagram at {run_dir}/final.svg. "
        f"Compare the rendered PNG against {run_dir}/figure.png element-by-element "
        f"using {run_dir}/boxlib.json. Fix position, visual, and overlap issues. "
        f"Write element_check_iter{{N}}.json for each iteration. "
        f"Quality profile: {quality_profile}. Max review iterations: {max_iters}. "
        f"Stop when no fixable issues remain, only aesthetic issues remain, or max iterations reached. "
        f"Write review_log.json and canonical method_architecture.svg/png when done."
    )

    log_dir = os.path.join(run_dir, "logs")
    rc = spawn_subagent("svg-refiner", prompt, verbose=getattr(args, "verbose", False), log_dir=log_dir)
    require_agent_success("svg-refiner", rc)

    # Read final results
    review_log = os.path.join(run_dir, "review_log.json")
    if os.path.exists(review_log):
        with open(review_log) as f:
            log = json.load(f)
        reason = log.get("terminate_reason", "unknown")
        iters = log.get("total_iterations", 0)
        ui.success(f"Step 3 complete. Iterations: {iters}, Terminate: {reason}")
    else:
        ui.warn("review_log.json not found")

    # Report final paths
    final_svg = os.path.join(run_dir, "method_architecture.svg")
    final_png = os.path.join(run_dir, "method_architecture.png")
    if os.path.exists(final_png):
        ui.info(f"Final figure: {final_png}")
    if os.path.exists(final_svg):
        ui.info(f"Final SVG: {final_svg}")


# ---------------------------------------------------------------------------
# Step 4: visualization composition
# ---------------------------------------------------------------------------


def step_viz_compose(run_dir: str, args: argparse.Namespace) -> None:
    """Step 4: Run @viz-composer agent.

    Discovers visualization regions in the diagram, generates programmatic
    replacements, evaluates raster vs programmatic, composites winners into SVG.
    Services are NOT needed (already stopped).
    """
    ui.section("Step 4: Visualization composition")

    raw_exp = getattr(args, "experiments_dir", "") or ""

    prompt = (
        f"Compose programmatic visualizations into the architecture diagram. "
        f"Run directory: {run_dir}. "
        f"Read boxlib.json and method_description.md to identify visualization regions. "
        f"Discover available tools and data files"
    )
    if raw_exp:
        prompt += f" in {raw_exp}"
    prompt += (
        ". For each viz region: generate programmatic version, compare with raster crop, "
        "pick the better one, and composite winners into method_architecture.svg. "
        "Write viz_composition_report.md when done."
    )

    log_dir = os.path.join(run_dir, "logs")
    rc = spawn_subagent("viz-composer", prompt, verbose=getattr(args, "verbose", False), log_dir=log_dir)
    require_agent_success("viz-composer", rc)

    # Check outputs
    composed_svg = os.path.join(run_dir, "method_architecture_composed.svg")
    composed_png = os.path.join(run_dir, "method_architecture_composed.png")
    report = os.path.join(run_dir, "viz_composition_report.md")

    if os.path.exists(composed_svg):
        ui.info(f"Composed SVG: {composed_svg}")
    else:
        ui.warn("No composed SVG produced — keeping svg-refiner output")
    if os.path.exists(composed_png):
        ui.info(f"Composed PNG: {composed_png}")
    if os.path.exists(report):
        ui.info(f"Composition report: {report}")

    ui.success("Step 4 complete.")


# ---------------------------------------------------------------------------
# Step 2 (sketch mode): agent-driven SVG authoring
# ---------------------------------------------------------------------------


def step_svg_author(run_dir: str, args: argparse.Namespace) -> None:
    """Step 2 (agent_svg mode): Run @svg-author agent.

    The agent directly creates SVG from method_description.md — no raster
    image generation, no SAM3/OCR/BEN2 services needed.
    """
    ui.section("Step 2: Agent-driven SVG authoring (no services)")

    prompt = (
        f"Create an SVG architecture diagram directly from the method description. "
        f"Run directory: {run_dir}. "
        f"Read method_description.md, study reference examples in configs/method_examples/, "
        f"write SVG as an architecture diagram (not a flowchart), validate, render to PNG, "
        f"and self-review (iterate until no fixable issues or max iterations). "
        f"Output canonical method_architecture.svg and method_architecture.png when done."
    )

    log_dir = os.path.join(run_dir, "logs")
    rc = spawn_subagent("svg-author", prompt, verbose=getattr(args, "verbose", False), log_dir=log_dir)
    require_agent_success("svg-author", rc)

    # Validate outputs
    final_svg = os.path.join(run_dir, "method_architecture.svg")
    final_png = os.path.join(run_dir, "method_architecture.png")
    if not os.path.exists(final_svg):
        # Check template.svg as fallback
        template = os.path.join(run_dir, "template.svg")
        if os.path.exists(template):
            ui.warn("method_architecture.svg missing but template.svg exists — proceeding")
        else:
            ui.error("No SVG output from svg-author agent")
            sys.exit(1)

    if os.path.exists(final_png):
        ui.info(f"Final figure: {final_png}")
    else:
        ui.warn("method_architecture.png not found")

    if os.path.exists(final_svg):
        ui.info(f"Final SVG: {final_svg}")

    # Read review log if available
    review_log = os.path.join(run_dir, "review_log.json")
    if os.path.exists(review_log):
        with open(review_log) as f:
            log = json.load(f)
        reason = log.get("terminate_reason", "unknown")
        iters = log.get("total_iterations", 0)
        ui.success(f"Step 2 complete. Iterations: {iters}, Terminate: {reason}")
    else:
        ui.success("Step 2 complete.")
