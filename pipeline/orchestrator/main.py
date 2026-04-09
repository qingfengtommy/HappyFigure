"""Top-level pipeline driver: explore → design → generate."""

from __future__ import annotations

import argparse
import datetime
import os
import sys

import ui
from pipeline.agent_runtime import launch_orchestrator_session, require_agent_success
from pipeline.contracts import StageRecord, StageStatus
from pipeline.context import get_ctx
from pipeline.orchestrator import artifacts as orch_art
from pipeline.orchestrator import steps as orch_steps
from pipeline.orchestrator.modes import resolve_mode
from pipeline.pipeline_backend import start_services, stop_services
from pipeline.run_state import read_manifest, read_manifest_stage, write_manifest_stage

_MODE_LABELS: dict[str, str] = {
    "exp_plot": "Pipeline",
    "composite": "Composite pipeline",
    "agent_svg": "Agent SVG pipeline",
    "paper_composite": "Paper composite pipeline",
}


def _emit_review_output(
    run_dir: str,
    experiments: list[str],
    review_feedback: object | None,
) -> None:
    """Generate review template and update preferences after a --review run."""
    from pipeline.feedback import generate_review_template, update_style_preferences

    review_path = generate_review_template(run_dir, experiments)
    ui.info(f"Review template: {ui.short_path(review_path)}")
    ui.dim(f"  Edit it or run: python cli.py review {ui.short_path(run_dir)}")
    ui.dim(f"  Then apply: python cli.py plot --proposal ... --resume {ui.short_path(run_dir)} --review")
    if review_feedback:
        added = update_style_preferences(review_feedback)  # type: ignore[arg-type]
        if added:
            ui.info(f"Updated project style preferences ({added} new rule(s))")


def _orchestrator_mode(args: argparse.Namespace, config: dict) -> str:
    mode = getattr(args, "orchestrator_mode", None) or config.get("orchestrator", {}).get("mode", "python-stages")
    return str(mode).replace("_", "-")


def run_agent_pipeline(args: argparse.Namespace) -> None:
    """Explore → design → generate."""
    ctx = get_ctx()
    os.environ["HAPPYFIGURE_AGENT"] = "1"
    os.environ["HAPPYFIGURE_OPENCODE"] = "1"

    mode = resolve_mode(args)
    args.mode = mode

    execution = getattr(args, "execution", "sequential")
    resume_dir = getattr(args, "resume", None)
    review_active = getattr(args, "review", False)
    orchestration_mode = _orchestrator_mode(args, getattr(ctx, "config", {}))

    if mode in ("exp_plot", "paper_composite"):
        label = "HappyFigure" if mode == "exp_plot" else "Paper composite"
        ui.info(f"Starting {label} pipeline ({ctx.platform_name} mode)")
        exp_dirs = getattr(args, "experiments_dir", "") or ""
        exp_display = ", ".join(ui.short_path(d) for d in exp_dirs.split(",") if d.strip()) if exp_dirs else "(none)"
        proposal_display = ui.short_path(args.proposal) if getattr(args, "proposal", None) else "(none)"
        ui.dim(f"Mode: {mode} | Proposal: {proposal_display} | Experiments: {exp_display} | Execution: {execution}")

    if orchestration_mode == "agent-first" and not (
        mode in {"composite", "agent_svg"} and getattr(args, "drawing_image", None)
    ):
        run_dir = orch_steps.prepare_agent_session_run(args, mode)

        # Parse human review feedback on --resume --review
        _review_feedback = None
        if review_active and resume_dir:
            from pipeline.feedback import parse_review, invalidate_stages_from

            _review_feedback = parse_review(run_dir)
            if _review_feedback:
                ui.info(f"Parsed human review: earliest affected stage = {_review_feedback.earliest_affected_stage}")
                invalidate_stages_from(run_dir, _review_feedback.earliest_affected_stage)

        ctx.orchestrator.setup(run_dir=run_dir, mode=mode, execution=execution)
        prompt = orch_steps.build_orchestrator_session_prompt(run_dir, args, mode)
        generate_rec = read_manifest_stage(run_dir, "generate")
        resume_completed = bool(
            getattr(args, "resume", None) and generate_rec and generate_rec.status == StageStatus.COMPLETED
        )

        with ui.orchestrator_log(run_dir):
            if resume_completed:
                ui.dim("Resuming agent-first run with completed generate stage; skipping relaunch")
            else:
                # Agent-first: start services proactively for modes that MAY need them.
                # For paper_composite, the agent decides which panels need services
                # at runtime, so we start them upfront. Plot-only papers pay a startup
                # cost but avoid mid-session service bootstrap complexity.
                if mode in {"composite", "paper_composite"}:
                    start_services()
                try:
                    rc = launch_orchestrator_session(
                        "happyfigure-orchestrator",
                        prompt,
                        verbose=getattr(args, "verbose", False),
                        log_dir=os.path.join(run_dir, "logs"),
                        log_name="happyfigure-orchestrator",
                    )
                    require_agent_success("happyfigure-orchestrator", rc)
                finally:
                    if mode in {"composite", "paper_composite"}:
                        stop_services()

            # Guard: if the agent session produced no artifacts at all (e.g., API error
            # that caused the CLI to exit 0 without generating anything), fail fast
            # with a clear message instead of a confusing missing-files RuntimeError.
            if mode == "exp_plot":
                explore_marker = os.path.join(run_dir, "exploration_report.md")
            elif mode == "paper_composite":
                # Paper composite: either exploration_report or figure_classification is sufficient
                explore_marker = os.path.join(run_dir, "exploration_report.md")
                if not os.path.exists(explore_marker):
                    explore_marker = os.path.join(run_dir, "figure_classification.json")
            else:
                explore_marker = os.path.join(run_dir, "method_description.md")
            if not resume_completed and not os.path.exists(explore_marker):
                ui.error(
                    "Agent session produced no outputs (likely an LLM API error). "
                    "Check logs/agent_happyfigure-orchestrator.log for details."
                )
                log_path = os.path.join(run_dir, "logs", "agent_happyfigure-orchestrator.log")
                if os.path.exists(log_path):
                    with open(log_path) as f:
                        tail = f.read()[-500:]
                    if tail.strip():
                        ui.dim(f"Last log output:\n{tail.strip()}")
                sys.exit(1)

            design = orch_steps.sync_agent_session_manifest(run_dir, args, mode)
            label = _MODE_LABELS.get(mode, "Pipeline")
            ui.success(f"{label} complete. Run dir: {ui.short_path(run_dir)}")
            if mode in ("exp_plot", "paper_composite"):
                ui.dim(f"  Indexed {len(design.experiments)} experiments from agent-session outputs")
            ui.pipeline_cost_summary()

            if review_active and mode in ("exp_plot", "paper_composite"):
                _emit_review_output(run_dir, design.experiments, _review_feedback)
        return

    if mode == "paper_composite":
        ui.error(
            "Paper composite mode requires agent-first orchestration. "
            "Set orchestrator.mode to 'agent-first' in pipeline.yaml or use --orchestrator-mode agent-first."
        )
        sys.exit(1)

    ctx.orchestrator.setup(run_dir="", mode=mode, execution=execution)

    # Parse human review feedback for python-stages mode
    _review_feedback_ps = None
    if review_active and resume_dir:
        _resume_abs = os.path.abspath(resume_dir)
        if os.path.isdir(_resume_abs):
            from pipeline.feedback import parse_review, invalidate_stages_from

            _review_feedback_ps = parse_review(_resume_abs)
            if _review_feedback_ps:
                ui.info(f"Parsed human review: earliest affected stage = {_review_feedback_ps.earliest_affected_stage}")
                invalidate_stages_from(_resume_abs, _review_feedback_ps.earliest_affected_stage)

    resumed_explore = None
    resumed_design = None
    resumed_generate = False
    if resume_dir:
        resume_dir = os.path.abspath(resume_dir)
        if not os.path.isdir(resume_dir):
            ui.error(f"Resume directory does not exist: {resume_dir}")
            sys.exit(1)
        manifest = read_manifest(resume_dir)
        manifest_mode = manifest.get("mode", "")
        layout_v = manifest.get("artifact_layout_version")
        if layout_v is not None and layout_v != orch_art.ARTIFACT_LAYOUT_VERSION:
            ui.warn(
                f"Resume dir artifact_layout_version={layout_v} differs from current "
                f"{orch_art.ARTIFACT_LAYOUT_VERSION} — paths may not match."
            )
        if manifest_mode and manifest_mode != mode:
            ui.warn(f"Resume dir mode '{manifest_mode}' differs from current mode '{mode}'")
        resumed_explore, resumed_design = orch_steps.try_resume(resume_dir, mode)
        generate_rec = read_manifest_stage(resume_dir, "generate")
        resumed_generate = bool(generate_rec and generate_rec.status == StageStatus.COMPLETED)
        if resumed_generate and not resumed_design:
            ui.warn("Generate stage is complete but design stage is missing; rerunning generate.")
            resumed_generate = False

    if resumed_explore:
        exploration = resumed_explore
        ui.dim(f"Resuming from existing explore stage in {exploration.run_dir}")
    else:
        exploration = orch_steps.stage_explore(args, mode)
        write_manifest_stage(
            exploration.run_dir,
            "explore",
            StageRecord(
                status=StageStatus.COMPLETED,
                completed_at=datetime.datetime.now().isoformat(),
                artifacts=exploration.artifacts,
                experiments=exploration.experiments,
                metadata=orch_steps.stage_metadata(exploration.run_dir, mode, args, stage="explore"),
            ),
            mode=mode,
        )

    with ui.orchestrator_log(exploration.run_dir):
        if mode == "exp_plot":
            ui.dim(f"Mode: {mode}, execution: {execution}")

        if resumed_design:
            design = resumed_design
            ui.dim(f"Resuming from existing design stage ({len(design.experiments)} experiments)")
        else:
            design = orch_steps.stage_design(exploration, args, mode)
            write_manifest_stage(
                exploration.run_dir,
                "design",
                StageRecord(
                    status=StageStatus.COMPLETED,
                    completed_at=datetime.datetime.now().isoformat(),
                    artifacts=design.artifacts,
                    experiments=design.experiments,
                    metadata=orch_steps.stage_metadata(exploration.run_dir, mode, args, stage="design", design=design),
                ),
                mode=mode,
            )

        if resumed_generate:
            ui.dim("Resuming from existing generate stage (already completed)")
        else:
            orch_steps.stage_generate(exploration.run_dir, args, mode, design)

        label = {
            "exp_plot": "Pipeline",
            "composite": "Composite pipeline",
            "agent_svg": "Agent SVG pipeline",
            "paper_composite": "Paper composite pipeline",
        }.get(mode, "Pipeline")
        ui.success(f"{label} complete. Run dir: {exploration.run_dir}")

        if review_active and mode in ("exp_plot", "paper_composite"):
            _emit_review_output(exploration.run_dir, design.experiments, _review_feedback_ps)
