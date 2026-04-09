"""Sequential and parallel code-agent execution for plot pipeline."""
from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import ui
from pipeline.agent_runtime import spawn_subagent
from pipeline.context import get_ctx
from pipeline.figure_lint import (
    LintReport,
    lint_figure_code,
    lint_figure_output,
    lint_styled_spec,
)
from pipeline.plot_planning import build_code_agent_prompt
from pipeline.run_state import (
    finalize_plot_experiment,
    persist_plot_execution_state,
    plot_experiment_workspace,
    read_critic_result,
)


def run_code_agent(
    run_dir: str,
    experiment: str,
    experiments_dir: str,
    spec_path: str | None = None,
    verbose: bool = False,
    prior_feedback: str | None = None,
    work_dir: str | None = None,
    label: str | None = None,
    feedback_paths: list[str] | None = None,
) -> dict:
    """Run code-agent for a single experiment. Returns critic result.

    Args:
        work_dir: Directory where the agent writes code, figures, and critic
                  results. Defaults to {run_dir}/experiments/{experiment}.
    """
    if spec_path is None:
        spec_path = os.path.join(plot_experiment_workspace(run_dir, experiment), "styled_spec.md")
    if work_dir is None:
        work_dir = plot_experiment_workspace(run_dir, experiment)
    os.makedirs(work_dir, exist_ok=True)

    # Build critic invocation instruction.
    # All platforms now support native subagents — use @figure-critic uniformly.
    ctx = get_ctx()
    if not ctx.critic_available:
        critic_instruction = (
            "SKIP the critic evaluation step — the scoring LLM is unavailable. "
            "Generate the figure code, execute it, and save the output. "
            "Do NOT attempt to run the critic."
        )
    else:
        critic_instruction = "invoke @figure-critic for scoring"

    # Pre-read shared context so parallel agents don't each spend tool calls
    # discovering and reading the same files.  The prompt composer decides
    # whether to inline or emit a path reference based on max_bundled_lines.
    global_style_path = os.path.join(run_dir, "global_style.md")
    global_style_content = ""
    if os.path.exists(global_style_path):
        with open(global_style_path) as f:
            global_style_content = f.read()

    spec_content = ""
    if os.path.exists(spec_path):
        with open(spec_path) as f:
            spec_content = f.read()

    prompt = build_code_agent_prompt(
        run_dir,
        experiment,
        experiments_dir,
        spec_path,
        work_dir,
        critic_instruction,
        global_style_content,
        prior_feedback=prior_feedback,
        spec_content=spec_content,
        global_style_path=global_style_path if global_style_content else None,
        feedback_paths=feedback_paths,
    )
    log_suffix = os.path.basename(work_dir.rstrip(os.sep))
    spawn_subagent(
        "code-agent",
        prompt,
        verbose=verbose,
        log_dir=os.path.join(run_dir, "logs"),
        log_name=f"code-agent_{experiment}_{log_suffix}",
        label=label or experiment,
    )

    # ── Deterministic lint gate ─────────────────────────────────────
    # Run fast mechanical checks on code + output AFTER the agent finishes.
    # Results are logged and injected into critic_result for downstream use.
    _run_lint_gate(run_dir, work_dir, experiment)

    # Read critic result from work_dir
    critic_path = os.path.join(work_dir, "critic_result.json")
    if os.path.exists(critic_path):
        with open(critic_path) as f:
            return json.load(f)
    # Fallback: try old location
    return read_critic_result(run_dir, experiment)


def _run_lint_gate(run_dir: str, work_dir: str, experiment: str) -> LintReport | None:
    """Run deterministic lint checks on spec, code, and figure output.

    Reads thresholds from ``pipeline.yaml`` ``figure_lint`` section,
    logs results via *ui*, and writes ``lint_report.json`` to *work_dir*.
    Returns the merged report, or ``None`` if lint is disabled.
    """
    from graphs.svg_utils import load_pipeline_config

    config = load_pipeline_config().get("figure_lint", {})
    if not config.get("enabled", True):
        return None

    checks = config.get("checks", {})
    enforcement = checks.get("color_registry_enforcement", "warn")

    color_reg: str | None = os.path.join(run_dir, "color_registry.json")
    if not os.path.exists(color_reg) or enforcement == "off":
        color_reg = None

    report = LintReport(passed=True)

    # Lint the styled spec (catches incomplete/missing specs)
    spec_path = os.path.join(work_dir, "styled_spec.md")
    if os.path.exists(spec_path):
        report = report.merge(lint_styled_spec(spec_path))

    # Lint the generated code
    code_path = os.path.join(work_dir, "figure_code.py")
    if os.path.exists(code_path):
        code_report = lint_figure_code(code_path, color_registry_path=color_reg)
        report = report.merge(code_report)

    # Lint the generated figure output
    figure_path = os.path.join(work_dir, "panel.png")
    if not os.path.exists(figure_path):
        figure_path = os.path.join(run_dir, "outputs", experiment, "figure.png")
    if os.path.exists(figure_path):
        min_bytes = checks.get("min_file_size_kb", 10) * 1000
        min_dpi = float(checks.get("min_dpi", 300)) - 0.5  # float tolerance
        output_report = lint_figure_output(
            figure_path, min_file_bytes=min_bytes, min_dpi=min_dpi
        )
        report = report.merge(output_report)

    # Persist for traceability and downstream critic consumption
    lint_result = {
        "passed": report.passed,
        "issues": report.issues,
        "warnings": report.warnings,
        "summary": report.summary(),
    }
    lint_path = os.path.join(work_dir, "lint_report.json")
    try:
        with open(lint_path, "w") as f:
            json.dump(lint_result, f, indent=2)
    except OSError:
        pass

    # Log
    if report.issues:
        ui.warn(f"Lint [{experiment}]: {report.summary()}")
        for issue in report.issues[:5]:
            ui.dim(f"  - {issue}")
    elif report.warnings:
        ui.dim(f"Lint [{experiment}]: {report.summary()}")

    # Append lint issues to critic_result (atomic write to avoid races
    # when multiple agents finish concurrently in parallel mode).
    if report.issues:
        critic_path = os.path.join(work_dir, "critic_result.json")
        if os.path.exists(critic_path):
            try:
                with open(critic_path) as f:
                    critic = json.load(f)
                critic["lint_issues"] = critic.get("lint_issues", []) + report.issues
                tmp_path = critic_path + ".tmp"
                with open(tmp_path, "w") as f:
                    json.dump(critic, f, indent=2)
                os.replace(tmp_path, critic_path)
            except (OSError, json.JSONDecodeError):
                pass

    return report


def step_execute_sequential(run_dir: str, experiments: list[str], args: argparse.Namespace) -> None:
    """Step 3: Execute code-agent sequentially for each experiment."""
    experiments_dir = os.path.abspath(args.experiments_dir) if args.experiments_dir else ""
    from pipeline.feedback import collect_feedback_paths

    results: list[tuple[str, dict]] = []
    summaries: list[dict] = []
    for exp in experiments:
        ui.section(f"Generating figure: {exp}")
        fb_paths = collect_feedback_paths(run_dir, "generate", exp)
        result = run_code_agent(run_dir, exp, experiments_dir, verbose=args.verbose,
                                feedback_paths=fb_paths)
        summary = finalize_plot_experiment(
            run_dir,
            exp,
            result,
            work_dir=plot_experiment_workspace(run_dir, exp),
        )
        results.append((exp, summary))
        summaries.append(summary)
        score = summary.get("score", "N/A")
        verdict = summary.get("verdict", "N/A")
        ui.result(exp, score, verdict)

    persist_plot_execution_state(run_dir, "sequential", summaries)
    ui.summary_table(results)


def step_execute_parallel(run_dir: str, experiments: list[str], args: argparse.Namespace) -> None:
    """Step 3: Execute code-agent in parallel for all experiments."""
    experiments_dir = os.path.abspath(args.experiments_dir) if args.experiments_dir else ""
    from pipeline.feedback import collect_feedback_paths

    dashboard = ui.ProgressDashboard(experiments)
    dashboard.start()

    results: list[tuple[str, dict]] = []
    summaries: list[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=min(len(experiments), 4)) as pool:
            futures = {
                pool.submit(run_code_agent, run_dir, exp, experiments_dir, None, args.verbose,
                            feedback_paths=collect_feedback_paths(run_dir, "generate", exp)): exp
                for exp in experiments
            }
            # Mark submitted experiments as running
            for exp in experiments:
                dashboard.update(exp, "running")

            for future in as_completed(futures):
                exp = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"score": 0, "verdict": "FAILED", "error": str(exc)}
                    dashboard.update(exp, "failed")
                summary = finalize_plot_experiment(
                    run_dir,
                    exp,
                    result,
                    work_dir=plot_experiment_workspace(run_dir, exp),
                )
                results.append((exp, summary))
                summaries.append(summary)
                score = summary.get("score", "N/A")
                verdict = summary.get("verdict", "N/A")
                dashboard.update(exp, f"done ({score})")
                ui.result(exp, score, verdict)
    finally:
        dashboard.stop()

    persist_plot_execution_state(run_dir, "parallel", summaries)
    ui.summary_table(results)
