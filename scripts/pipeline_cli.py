#!/usr/bin/env python3
"""Unified pipeline CLI — tool backend for agent platforms.

This module is the backend for agent tool calls. When agents (data-explore,
svg-builder, etc.) run pipeline operations, they invoke these subcommands
via bash.  Only ``init`` and ``services`` are called directly by the
orchestrator (cli.py); all other subcommands are called by the agents
through their tool-use capabilities.

Subcommands wrap existing LangGraph node functions as standalone operations,
reading/writing state from a run_dir/state.json manifest.

Usage:
    python scripts/pipeline_cli.py init --proposal <file> [--results-dir <dir>] [--llm-preset azure|gemini|mixed] [--run-dir <dir>]
    python scripts/pipeline_cli.py data-scan --run-dir <dir>
    python scripts/pipeline_cli.py data-process --run-dir <dir>
    python scripts/pipeline_cli.py figure-plan --run-dir <dir>
    python scripts/pipeline_cli.py figure-execute --run-dir <dir> [--experiment <name>]
    python scripts/pipeline_cli.py figure-execute-parallel --run-dir <dir>
    python scripts/pipeline_cli.py figure-execute-beam --run-dir <dir> [--beam-width 2] [--style-variants 2] [--code-variants 2] [--beam-iterations 2]
    python scripts/pipeline_cli.py method-propose --run-dir <dir>
    python scripts/pipeline_cli.py svg-pipeline --run-dir <dir>
    python scripts/pipeline_cli.py image-generate --run-dir <dir> [--force] [--refined-prompt <text>]
    python scripts/pipeline_cli.py icon-replace --run-dir <dir> [--svg-path <file>] [--icon-infos <file>]
    python scripts/pipeline_cli.py services <start|stop|health>

All output is JSON to stdout.  Errors are JSON with an "error" key.
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Ensure repo root is on sys.path for imports
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.orchestrator import artifacts as orch_artifacts  # noqa: E402


def _json_out(obj: dict | list) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _error(msg: str) -> None:
    _json_out({"error": msg})
    sys.exit(1)


# ANSI color helpers for progress output
_USE_COLOR = sys.stderr.isatty() or os.environ.get("FORCE_COLOR")


def _c(code: str, text: str) -> str:
    """Wrap text in ANSI color if stderr is a TTY or FORCE_COLOR is set."""
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def _dim(t: str) -> str:
    return _c("2", t)


def _green(t: str) -> str:
    return _c("32", t)


def _yellow(t: str) -> str:
    return _c("33", t)


def _red(t: str) -> str:
    return _c("31", t)


def _cyan(t: str) -> str:
    return _c("36", t)


def _bold(t: str) -> str:
    return _c("1", t)


def _progress(msg: str, level: str = "info") -> None:
    """Write a styled progress line to stderr. The TS tool wrappers forward
    stderr to the terminal in real-time so the user sees what's happening."""
    colors = {"info": "36", "ok": "32", "warn": "33", "err": "31", "dim": "2"}
    code = colors.get(level, "0")
    prefix = _c(code, "▸") if _USE_COLOR else ">"
    sys.stderr.write(f"  {prefix} {msg}\n")
    sys.stderr.flush()


def _load_state(run_dir: str) -> dict:
    state_path = Path(run_dir) / "state.json"
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {}


def _save_state(run_dir: str, state: dict) -> None:
    state_path = Path(run_dir) / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _init_llm(llm_preset: str | None = None) -> None:
    """Initialize LLM routing from config, optionally applying a preset."""
    import llm

    llm.init_from_config()
    if llm_preset:
        llm.apply_preset(llm_preset)


# ── init ─────────────────────────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize a run directory, load proposal, set up style few-shots."""
    proposal_path = args.proposal
    results_dir = args.results_dir
    llm_preset = getattr(args, "llm_preset", None)
    run_dir = args.run_dir
    mode = args.mode or "happyfigure"

    _init_llm(llm_preset)

    # Read proposal (file or directory — supports .md, .pdf, .tex, .bib, etc.)
    _progress(f"Loading proposal from {_dim(proposal_path)}")
    from pipeline.proposal_loader import extract_text, gather_proposal_files

    p = Path(proposal_path)
    if not p.exists():
        _error(f"Proposal path not found: {proposal_path}")
    files = gather_proposal_files(p)
    if not files:
        _error(f"No proposal files found at: {proposal_path}")
    if len(files) > 20:
        files = files[:20]
    if p.is_dir():
        parts: list[str] = []
        for f in files:
            rel = f.relative_to(p)
            content = extract_text(f).rstrip()
            if content:
                parts.append(f"<!-- source: {rel} -->\n")
                parts.append(content + "\n\n")
        proposal_text = "".join(parts).strip()
    else:
        proposal_text = extract_text(files[0]).strip()

    # Create run directory
    if not run_dir:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        notes_dir = Path.cwd() / "notes" / "figure_runs"
        notes_dir.mkdir(parents=True, exist_ok=True)
        run_dir = str(notes_dir / f"run_{ts}")
    rd = Path(run_dir)
    rd.mkdir(parents=True, exist_ok=True)
    # Create logs/ for all modes. For exp_plot, outputs/ and debug/ are created
    # by cli._ensure_plot_run_layout(); diagram artifacts go directly
    # in run_dir. No legacy figures/data/svg dirs — outputs/ is canonical.
    (rd / "logs").mkdir(exist_ok=True)

    # Save proposal
    (rd / "proposal.md").write_text(proposal_text, encoding="utf-8")
    _progress(f"Run directory created: {_dim(run_dir)}", "ok")

    # Load style few-shots
    _progress("Loading style few-shot examples...")
    from graphs.figure_pipeline import _load_style_few_shots

    style_dir = args.style_examples
    few_shots = _load_style_few_shots(style_dir) if style_dir else _load_style_few_shots(None)
    few_shot_paths = [fs.get("image_path", "") for fs in few_shots]

    state = {
        "proposal": proposal_text,
        "results_dir": results_dir or "",
        "run_dir": run_dir,
        "llm_preset": llm_preset,
        "mode": mode,
        "style_few_shot_count": len(few_shots),
        "style_few_shot_paths": few_shot_paths[:5],
        "figure_paths": [],
        "completed_steps": ["init"],
        "created_at": datetime.datetime.now().isoformat(),
    }

    _save_state(run_dir, state)

    _json_out(
        {
            "status": "ok",
            "run_dir": run_dir,
            "proposal_length": len(proposal_text),
            "results_dir": results_dir or "(none)",
            "mode": mode,
            "style_few_shots": len(few_shots),
            "llm_preset": llm_preset,
        }
    )


# ── data-scan ────────────────────────────────────────────────────────


def _build_tree(root: str, max_depth: int = 3) -> str:
    """Build a lightweight directory tree string (no heavy I/O)."""
    lines = []
    root_path = Path(root)
    if not root_path.exists():
        return f"(not found: {root})"

    def _walk(p: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        except PermissionError:
            return
        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                ext = "    " if is_last else "│   "
                _walk(entry, prefix + ext, depth + 1)
            else:
                size = entry.stat().st_size
                size_str = (
                    f"{size}" if size < 1024 else f"{size / 1024:.0f}K" if size < 1048576 else f"{size / 1048576:.1f}M"
                )
                lines.append(f"{prefix}{connector}{entry.name} ({size_str})")

    lines.append(f"{root_path.name}/")
    _walk(root_path, "", 0)
    return "\n".join(lines)


def cmd_data_scan(args: argparse.Namespace) -> None:
    """Discover experiment subdirs, scan file schemas, and build directory trees.

    Reads actual CSV/TSV/JSON headers and sample rows so that downstream nodes
    (data_explorer, planner, code_agent) receive real column names instead of
    having to guess from file names alone.
    """
    run_dir = args.run_dir
    state = _load_state(run_dir)
    if not state:
        _error("No state.json found. Run 'init' first.")

    results_dir = state.get("results_dir", "")
    if not results_dir or not Path(results_dir).is_dir():
        _error(f"results_dir not found or not a directory: {results_dir}")

    from graphs.figure_pipeline import (
        _scan_data_files,
        _build_schemas_from_scan,
        _build_statistics_from_scan,
        _build_semantics,
        _compact_experiment_group,
        _truncate_for_prompt,
        _FULL_SCHEMAS_CHARS_MAX,
        _FULL_TREE_CHARS_MAX,
        _FULL_SEMANTICS_CHARS_MAX,
    )

    _progress("Discovering experiments and scanning file schemas...")

    # Discover experiment groups (top-level subdirectories)
    _SKIP_DIRS = {
        "__pycache__",
        ".git",
        ".ipynb_checkpoints",
        "node_modules",
        "processed_data_figures",
        ".mypy_cache",
        "__MACOSX",
    }
    _DATA_SUFFIXES = {".csv", ".tsv", ".json", ".pkl", ".npy", ".parquet", ".txt", ".log", ".h5", ".hdf5"}
    results_path = Path(results_dir)
    experiment_groups = []
    for entry in sorted(results_path.iterdir()):
        if entry.is_dir() and not entry.name.startswith(".") and entry.name not in _SKIP_DIRS:
            # Check for actual data files to avoid polluting with non-experiment dirs
            has_data = any(f.suffix.lower() in _DATA_SUFFIXES for f in entry.rglob("*") if f.is_file())
            if not has_data:
                continue
            tree = _build_tree(str(entry), max_depth=2)
            scan, errors = _scan_data_files(entry, results_path)
            experiment_groups.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "tree": tree,
                    "schemas": _build_schemas_from_scan(scan, errors, entry),
                    "statistics": _build_statistics_from_scan(scan, errors),
                    "semantics": _build_semantics(entry, results_path),
                }
            )

    # If no subdirs, treat the results_dir itself as a single experiment
    if not experiment_groups:
        tree = _build_tree(results_dir, max_depth=2)
        scan, errors = _scan_data_files(results_path, results_path)
        experiment_groups.append(
            {
                "name": results_path.name,
                "path": results_dir,
                "tree": tree,
                "schemas": _build_schemas_from_scan(scan, errors, results_path),
                "statistics": _build_statistics_from_scan(scan, errors),
                "semantics": _build_semantics(results_path, results_path),
            }
        )

    # Compact per-experiment fields to safe prompt sizes
    experiment_groups = [_compact_experiment_group(g) for g in experiment_groups]

    _progress(
        f"Discovered {_bold(str(len(experiment_groups)))} experiment(s): {[g['name'] for g in experiment_groups]}", "ok"
    )

    # Build overall tree
    overall_tree = _build_tree(results_dir, max_depth=2)

    # Build concatenated full_data_* fields across all experiments
    full_tree_parts = []
    full_schemas_parts = []
    full_semantics_parts = []
    for g in experiment_groups:
        full_tree_parts.append(f"## Experiment: {g['name']}\n\n{g.get('tree', '')}")
        full_schemas_parts.append(f"## Experiment: {g['name']}\n\n{g.get('schemas', '')}")
        full_semantics_parts.append(f"## Experiment: {g['name']}\n\n{g.get('semantics', '')}")

    state["experiment_groups"] = experiment_groups
    state["data_tree"] = overall_tree
    state["full_results_dir"] = results_dir
    state["full_data_tree"] = _truncate_for_prompt(
        "\n\n---\n\n".join(full_tree_parts), _FULL_TREE_CHARS_MAX, "full_data_tree"
    )
    state["full_data_schemas"] = _truncate_for_prompt(
        "\n\n---\n\n".join(full_schemas_parts), _FULL_SCHEMAS_CHARS_MAX, "full_data_schemas"
    )
    state["full_data_semantics"] = _truncate_for_prompt(
        "\n\n---\n\n".join(full_semantics_parts), _FULL_SEMANTICS_CHARS_MAX, "full_data_semantics"
    )
    # Set first-experiment fields for convenience
    state["data_schemas"] = experiment_groups[0].get("schemas", "")
    state["data_statistics"] = experiment_groups[0].get("statistics", "")
    state["data_semantics"] = experiment_groups[0].get("semantics", "")
    state["completed_steps"] = state.get("completed_steps", []) + ["data_scan"]
    _save_state(run_dir, state)

    _json_out(
        {
            "status": "ok",
            "experiments_found": len(experiment_groups),
            "experiment_names": [g["name"] for g in experiment_groups],
            "schemas_populated": bool(state.get("full_data_schemas")),
            "data_tree": overall_tree[:2000],
        }
    )


# ── data-process ─────────────────────────────────────────────────────


def cmd_data_process(args: argparse.Namespace) -> None:
    """Run data_processor_node + execute_data_processing_node."""
    run_dir = args.run_dir
    state = _load_state(run_dir)
    if not state:
        _error("No state.json found. Run 'init' first.")

    _init_llm(state.get("llm_preset") or state.get("backend"))

    from graphs.figure_pipeline import (
        _ensure_gpt,
        data_processor_node,
        execute_data_processing_node,
    )

    _ensure_gpt()

    pipe_state = dict(state)
    pipe_state["run_dir"] = run_dir
    pipe_state["verbose"] = args.verbose
    pipe_state["data_processing_mode"] = args.data_processing_mode or "regen"

    _progress("Planning data processing with LLM...")
    result1 = data_processor_node(pipe_state)
    pipe_state.update(result1)
    _progress("Data processing plan generated. Executing...")

    result2 = execute_data_processing_node(pipe_state)
    pipe_state.update(result2)
    success = pipe_state.get("data_processing_success", False)
    reused = pipe_state.get("data_processing_reused", False)
    if reused:
        _progress("Reused existing processed_data directory")
    elif success:
        _progress(f"Data processing complete: {_dim(pipe_state.get('processed_results_dir', ''))}", "ok")
    else:
        _progress("Data processing failed or skipped — continuing with raw data", "warn")

    for key in (
        "data_processing_code",
        "processed_results_dir",
        "data_processing_success",
        "data_processing_reused",
        "data_processing_stdout",
        "data_processing_stderr",
        "experiment_groups",
    ):
        if key in pipe_state:
            state[key] = pipe_state[key]

    state["completed_steps"] = state.get("completed_steps", []) + ["data_process"]
    _save_state(run_dir, state)

    _json_out(
        {
            "status": "ok",
            "data_processing_success": pipe_state.get("data_processing_success", False),
            "data_processing_reused": pipe_state.get("data_processing_reused", False),
            "processed_results_dir": pipe_state.get("processed_results_dir", ""),
        }
    )


# ── figure-plan ──────────────────────────────────────────────────────


def cmd_figure_plan(args: argparse.Namespace) -> None:
    """Run figure_planner + split_plan + route_figures.

    Requires a pre-computed exploration report from the @data-explore subagent.
    The LLM explorer is NOT used in opencode mode — the subagent report is the
    single source of truth for data structure.

    If state.json is missing or incomplete (no data-scan results), this will
    auto-bootstrap by running init and data-scan inline.
    """
    run_dir = args.run_dir
    state = _load_state(run_dir)

    # Auto-bootstrap: if state.json doesn't exist or lacks data-scan results,
    # run init + data-scan inline using --proposal and --results-dir args
    if not state or "full_data_schemas" not in state:
        import io
        import types

        proposal_path = getattr(args, "proposal", None)
        results_dir = getattr(args, "results_dir", None)
        llm_preset = getattr(args, "llm_preset", None)
        mode = getattr(args, "mode", None) or "exp_plot"

        if not state:
            # Need to run init first
            if not proposal_path:
                _error(
                    "No state.json found and no --proposal provided. "
                    "Run 'init' first or pass --proposal and --results-dir."
                )
            _progress("Auto-bootstrapping: running init...")
            init_args = types.SimpleNamespace(
                proposal=proposal_path,
                results_dir=results_dir,
                llm_preset=llm_preset,
                run_dir=run_dir,
                mode=mode,
                style_examples=None,
            )
            # Redirect stdout to suppress init JSON output
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                cmd_init(init_args)
            finally:
                sys.stdout = old_stdout
            state = _load_state(run_dir)

        if "full_data_schemas" not in state:
            _progress("Auto-bootstrapping: running data-scan...")
            scan_args = types.SimpleNamespace(run_dir=run_dir, verbose=False)
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                cmd_data_scan(scan_args)
            finally:
                sys.stdout = old_stdout
            state = _load_state(run_dir)
            _progress("Auto-bootstrap complete", "ok")

    _init_llm(state.get("llm_preset") or state.get("backend"))

    from graphs.figure_pipeline import (
        _ensure_gpt,
        figure_planner_node,
        split_plan_node,
        route_figures_node,
    )

    _ensure_gpt()

    pipe_state = dict(state)
    pipe_state["run_dir"] = run_dir
    pipe_state["verbose"] = args.verbose

    # Load exploration report from @data-explore subagent (required)
    exploration_report = getattr(args, "exploration_report", None)
    rd = Path(run_dir)

    # Try explicit arg first, then check run_dir for saved report
    report = ""
    if exploration_report:
        report_path = Path(exploration_report)
        if report_path.exists():
            report = report_path.read_text(encoding="utf-8").strip()
        else:
            _progress(f"Exploration report not found at: {_dim(exploration_report)}", "warn")

    if not report:
        for fallback_name in (
            orch_artifacts.PLOT_EXPLORATION_REPORT,
            "exp_exploration_report.md",
            "data_exploration_report.md",
        ):
            fallback = rd / fallback_name
            if fallback.exists():
                report = fallback.read_text(encoding="utf-8").strip()
                _progress(f"Loaded exploration report from run_dir {_dim(f'({len(report)} chars)')}", "ok")
                break

    if not report:
        _error(
            "No exploration report found. Run @data-explore subagent first.\n"
            "  Pass --exploration-report <path> or save report to <run_dir>/exploration_report.md"
        )

    pipe_state["data_exploration_report"] = report
    # Persist to run_dir
    rd.mkdir(parents=True, exist_ok=True)
    (rd / orch_artifacts.PLOT_EXPLORATION_REPORT).write_text(report, encoding="utf-8")
    _progress(f"Using exploration report {_dim(f'({len(report)} chars)')}", "ok")

    # Figure planner
    _progress(_bold("Planning figures for all experiments (LLM call)..."))
    result2 = figure_planner_node(pipe_state)
    pipe_state.update(result2)
    _progress("Figure plan generated", "ok")

    # Split plan
    _progress("Splitting plan into per-experiment specs...")
    result3 = split_plan_node(pipe_state)
    pipe_state.update(result3)

    # Route figures
    _progress(_bold("Routing experiments to generation methods..."))
    result4 = route_figures_node(pipe_state)
    pipe_state.update(result4)
    routes = pipe_state.get("per_experiment_routes", {})
    for name, info in routes.items():
        _progress(f"  {_cyan(name)} -> {info.get('figure_category', '?')}")

    for key in (
        "data_exploration_report",
        "multi_figure_plan",
        "per_experiment_specs",
        "per_experiment_routes",
        "experiment_groups",
        "current_experiment_index",
        "current_experiment_name",
        "figure_plan",
        "route_type",
    ):
        if key in pipe_state:
            state[key] = pipe_state[key]

    state["completed_steps"] = state.get("completed_steps", []) + ["figure_plan"]
    _save_state(run_dir, state)

    specs = pipe_state.get("per_experiment_specs", {})
    routes = pipe_state.get("per_experiment_routes", {})
    _json_out(
        {
            "status": "ok",
            "experiments_planned": len(specs),
            "experiment_specs": {name: spec[:200] + "..." if len(spec) > 200 else spec for name, spec in specs.items()},
            "experiment_routes": routes,
        }
    )


# ── figure-execute ───────────────────────────────────────────────────


def cmd_figure_execute(args: argparse.Namespace) -> None:
    """Execute figure generation for a single experiment (stylist + code + critic loop)."""
    run_dir = args.run_dir
    state = _load_state(run_dir)
    if not state:
        _error("No state.json found. Run 'init' first.")

    specs = state.get("per_experiment_specs", {})

    # Auto-redirect: multiple experiments without explicit name -> parallel
    if not args.experiment and len(specs) > 1:
        _progress(f"Multiple experiments ({_bold(str(len(specs)))}) detected, auto-switching to parallel mode", "info")
        return cmd_figure_execute_parallel(args)

    _init_llm(state.get("llm_preset") or state.get("backend"))

    from graphs.figure_pipeline import (
        _ensure_gpt,
        stylist_node,
        code_agent_node,
        execute_code_node,
        critic_node,
        multi_panel_node,
        MAX_ITERATIONS,
    )

    _ensure_gpt()

    # Resolve experiment name
    experiment_name = args.experiment
    if not experiment_name and len(specs) == 1:
        experiment_name = list(specs.keys())[0]
    elif not experiment_name and len(specs) == 0:
        experiment_name = None  # no specs yet, use default

    pipe_state = dict(state)
    pipe_state["run_dir"] = run_dir
    pipe_state["verbose"] = args.verbose

    # Set up experiment context
    if experiment_name:
        routes = state.get("per_experiment_routes", {})
        if experiment_name in specs:
            pipe_state["figure_plan"] = specs[experiment_name]
            pipe_state["current_experiment_name"] = experiment_name
        if experiment_name in routes:
            pipe_state["route_type"] = routes[experiment_name].get("figure_category", "statistical")
        # Find experiment index
        groups = state.get("experiment_groups", [])
        for i, g in enumerate(groups):
            if g.get("name") == experiment_name:
                pipe_state["current_experiment_index"] = i
                # Set experiment-specific data
                pipe_state["data_tree"] = g.get("tree", "")
                pipe_state["data_schemas"] = g.get("schemas", "")
                pipe_state["data_statistics"] = g.get("statistics", "")
                pipe_state["data_semantics"] = g.get("semantics", "")
                pipe_state["results_dir"] = g.get("path", state.get("results_dir", ""))
                break

    route = pipe_state.get("route_type", "statistical")
    max_iters = MAX_ITERATIONS
    iteration = 0
    last_verdict = ""
    last_score = 0.0

    # Best-iteration tracking
    best_score = 0.0
    best_state = {}

    exp_label = experiment_name or "(default)"
    _progress(f"Executing {_cyan(exp_label)} | route={route} | max_iterations={max_iters}")

    for iteration in range(1, max_iters + 1):
        pipe_state["iteration"] = iteration
        _progress(f"[{_cyan(exp_label)}] {_bold(f'Iteration {iteration}/{max_iters}')}")

        if route in ("statistical", "visualization"):
            if iteration == 1:
                _progress(f"[{_cyan(exp_label)}] Styling figure...")
                r_style = stylist_node(pipe_state)
                pipe_state.update(r_style)
            _progress(f"[{_cyan(exp_label)}] Generating code (LLM code agent)...")
            r_code = code_agent_node(pipe_state)
            pipe_state.update(r_code)
            _progress(f"[{_cyan(exp_label)}] Executing generated code...")
            r_exec = execute_code_node(pipe_state)
            pipe_state.update(r_exec)
            run_ok = pipe_state.get("run_success", False)
            if run_ok:
                _progress(f"[{_cyan(exp_label)}] Code execution: {_green('success')}", "ok")
            else:
                _progress(f"[{_cyan(exp_label)}] Code execution: {_red('FAILED')}", "err")
        elif route == "multi_panel":
            _progress(f"[{_cyan(exp_label)}] Composing multi-panel figure...")
            r_mp = multi_panel_node(pipe_state)
            pipe_state.update(r_mp)

        _progress(f"[{_cyan(exp_label)}] Running critic review...")
        r_critic = critic_node(pipe_state)
        pipe_state.update(r_critic)

        last_verdict = pipe_state.get("critic_verdict", "")
        last_score = pipe_state.get("critic_score", 0.0)
        verdict_colored = _green(last_verdict) if "ACCEPT" in last_verdict.upper() else _yellow(last_verdict)
        _progress(f"[{_cyan(exp_label)}] Critic: {verdict_colored} (score={last_score:.1f})")

        # Track best iteration
        if last_score > best_score:
            best_score = last_score
            best_state = {
                "code": pipe_state.get("code"),
                "figure_path": pipe_state.get("figure_path"),
                "critic_score": last_score,
                "critic_verdict": pipe_state.get("critic_verdict"),
                "critic_feedback": pipe_state.get("critic_feedback"),
                "iteration": iteration,
            }

        if "ACCEPT" in last_verdict.upper():
            _progress(f"[{_cyan(exp_label)}] {_green(f'Accepted at iteration {iteration}')}", "ok")
            break

    # Restore best iteration if final iteration regressed
    if best_score > pipe_state.get("critic_score", 0.0):
        best_iter = best_state["iteration"]
        _progress(
            f"[{_cyan(exp_label)}] {_yellow(f'Reverting to best iteration {best_iter} (score={best_score})')}", "warn"
        )
        pipe_state.update(best_state)

    # Persist results
    for key in (
        "figure_path",
        "figure_paths",
        "critic_feedback",
        "critic_verdict",
        "critic_score",
        "code",
        "styled_figure_spec",
        "iteration",
    ):
        if key in pipe_state:
            state[key] = pipe_state[key]

    state["completed_steps"] = state.get("completed_steps", []) + ["figure_execute"]
    _save_state(run_dir, state)

    _json_out(
        {
            "status": "ok",
            "experiment": experiment_name or "(default)",
            "iterations": iteration,
            "verdict": pipe_state.get("critic_verdict", last_verdict),
            "score": pipe_state.get("critic_score", last_score),
            "figure_path": pipe_state.get("figure_path", ""),
            "best_iteration": best_state.get("iteration", iteration),
        }
    )


# ── figure-execute-parallel ──────────────────────────────────────────


def cmd_figure_execute_parallel(args: argparse.Namespace) -> None:
    """Execute all experiments in parallel using the parallel pipeline graph."""
    run_dir = args.run_dir
    state = _load_state(run_dir)
    if not state:
        _error("No state.json found. Run 'init' first.")

    _init_llm(state.get("llm_preset") or state.get("backend"))

    from graphs.figure_pipeline import (
        _ensure_gpt,
        plan_all_experiments_node,
        execute_all_experiments_parallel_node,
    )

    _ensure_gpt()

    pipe_state = dict(state)
    pipe_state["run_dir"] = run_dir
    pipe_state["verbose"] = args.verbose

    # Wire parallel progress callback
    import graphs.figure_pipeline as _fig_mod

    def _parallel_cb(experiment_name: str, node_name: str, iteration: int, **kwargs):
        score = kwargs.get("score")
        verdict = kwargs.get("verdict")
        error = kwargs.get("error")
        if error:
            _progress(f"[{_cyan(experiment_name)}] iter {iteration} {_red(node_name)}: {error}", "err")
        elif verdict:
            v_colored = _green(verdict) if "ACCEPT" in str(verdict).upper() else _yellow(verdict)
            _progress(f"[{_cyan(experiment_name)}] iter {iteration} {node_name}: {v_colored} (score={score})")
        else:
            _progress(f"[{_cyan(experiment_name)}] iter {iteration} {_dim(node_name)}")

    _fig_mod._parallel_progress_callback = _parallel_cb
    try:
        # Plan all experiments (includes planner + split + style)
        _progress(_bold("Planning all experiments (planner + split + style)..."))
        result1 = plan_all_experiments_node(pipe_state)
        pipe_state.update(result1)
        groups = pipe_state.get("experiment_groups", [])
        _progress(f"Plan complete for {_bold(str(len(groups)))} experiment(s)", "ok")

        # Execute all in parallel
        _progress(_bold("Executing all experiments in parallel..."))
        result2 = execute_all_experiments_parallel_node(pipe_state)
        pipe_state.update(result2)
        _progress("All parallel experiments complete", "ok")
    finally:
        _fig_mod._parallel_progress_callback = None

    # Persist
    exp_states = pipe_state.get("experiment_states", [])
    figure_paths = []
    results_summary = []
    for es in exp_states:
        fp = es.get("figure_path", "")
        if fp:
            figure_paths.append(fp)
        results_summary.append(
            {
                "experiment": es.get("current_experiment_name", "?"),
                "verdict": es.get("critic_verdict", "?"),
                "score": es.get("critic_score", 0),
                "figure_path": fp,
            }
        )

    state["figure_paths"] = figure_paths
    state["experiment_states"] = exp_states
    state["completed_steps"] = state.get("completed_steps", []) + ["figure_execute_parallel"]
    _save_state(run_dir, state)

    _json_out(
        {
            "status": "ok",
            "experiments_completed": len(exp_states),
            "results": results_summary,
        }
    )


# ── figure-execute-beam ──────────────────────────────────────────────


def cmd_figure_execute_beam(args: argparse.Namespace) -> None:
    """Execute beam search for all experiments."""
    run_dir = args.run_dir
    state = _load_state(run_dir)
    if not state:
        _error("No state.json found. Run 'init' first.")

    _init_llm(state.get("llm_preset") or state.get("backend"))

    from graphs.figure_pipeline import (
        _ensure_gpt,
        plan_beam_experiments_node,
        execute_beam_all_parallel_node,
    )

    _ensure_gpt()

    pipe_state = dict(state)
    pipe_state["run_dir"] = run_dir
    pipe_state["verbose"] = args.verbose
    pipe_state["beam_width"] = args.beam_width
    pipe_state["beam_style_variants"] = args.style_variants
    pipe_state["beam_code_variants"] = args.code_variants
    pipe_state["beam_iterations"] = args.beam_iterations

    _progress(
        f"Beam search: width={args.beam_width}, styles={args.style_variants}, "
        f"code_variants={args.code_variants}, iterations={args.beam_iterations}"
    )
    _progress("Planning beam experiments...")
    result1 = plan_beam_experiments_node(pipe_state)
    pipe_state.update(result1)
    _progress("Beam plan complete. Executing beam search in parallel...")

    result2 = execute_beam_all_parallel_node(pipe_state)
    pipe_state.update(result2)
    _progress("Beam search complete")

    exp_states = pipe_state.get("experiment_states", [])
    figure_paths = []
    results_summary = []
    for es in exp_states:
        fp = es.get("best_figure_path", es.get("figure_path", ""))
        if fp:
            figure_paths.append(fp)
        results_summary.append(
            {
                "experiment": es.get("current_experiment_name", "?"),
                "score": es.get("critic_score", 0),
                "figure_path": fp,
            }
        )

    state["figure_paths"] = figure_paths
    state["experiment_states"] = exp_states
    state["completed_steps"] = state.get("completed_steps", []) + ["figure_execute_beam"]
    _save_state(run_dir, state)

    _json_out(
        {
            "status": "ok",
            "experiments_completed": len(exp_states),
            "beam_params": {
                "width": args.beam_width,
                "style_variants": args.style_variants,
                "code_variants": args.code_variants,
                "iterations": args.beam_iterations,
            },
            "results": results_summary,
        }
    )


# ── method-propose ───────────────────────────────────────────────────


def cmd_method_propose(args: argparse.Namespace) -> None:
    """Run load_markdown + method_data_explorer + method_proposer."""
    run_dir = args.run_dir
    state = _load_state(run_dir)
    if not state:
        _error("No state.json found. Run 'init' first.")

    _init_llm(state.get("llm_preset") or state.get("backend"))

    from graphs.figure_pipeline import _ensure_gpt
    from graphs._method_shared import (
        load_markdown_node,
        method_data_explorer_node,
        method_proposer_node,
    )

    _ensure_gpt()

    # Prepare method drawing input directory
    proposal_text = state.get("proposal", "")
    results_dir = state.get("results_dir", "")

    input_dir = str(Path(run_dir) / "method_input")
    Path(input_dir).mkdir(parents=True, exist_ok=True)
    (Path(input_dir) / "proposal.md").write_text(proposal_text + "\n", encoding="utf-8")
    if results_dir:
        from graphs.pipeline_helpers import build_experiments_context_markdown as _build_experiments_context_markdown

        context_md = _build_experiments_context_markdown(results_dir)
        (Path(input_dir) / "experiments_context.md").write_text(context_md, encoding="utf-8")

    pipe_state = {
        "input_dir": input_dir,
        "results_dir": results_dir,
        "run_dir": run_dir,
        "verbose": args.verbose,
        "doc_type": args.doc_type or "journal",
        "architecture_examples_dir": args.architecture_examples or "",
        "architecture_few_shots": [],
        "figure_paths": [],
    }

    _progress("Loading markdown files and architecture examples...")
    result1 = load_markdown_node(pipe_state)
    pipe_state.update(result1)

    _progress(_bold("Running method data exploration (tool-grounded)..."))
    result2 = method_data_explorer_node(pipe_state)
    pipe_state.update(result2)
    _progress(
        f"Data exploration complete: {_dim(str(len(pipe_state.get('data_exploration_report', ''))) + ' chars')}", "ok"
    )

    _progress(_bold("Extracting method description and generating drawing prompt..."))
    result3 = method_proposer_node(pipe_state)
    pipe_state.update(result3)
    _progress(f"Drawing prompt generated: {_dim(str(len(pipe_state.get('drawing_prompt', ''))) + ' chars')}", "ok")

    for key in (
        "proposal",
        "method_description",
        "drawing_prompt",
        "data_exploration_report",
        "architecture_few_shots",
    ):
        if key in pipe_state:
            state[key] = pipe_state[key]

    state["method_input_dir"] = input_dir
    state["completed_steps"] = state.get("completed_steps", []) + ["method_propose"]
    _save_state(run_dir, state)

    _json_out(
        {
            "status": "ok",
            "method_description_length": len(pipe_state.get("method_description", "")),
            "drawing_prompt_length": len(pipe_state.get("drawing_prompt", "")),
            "drawing_prompt_preview": pipe_state.get("drawing_prompt", "")[:300],
        }
    )


# ── svg-pipeline ─────────────────────────────────────────────────────


def cmd_svg_pipeline(args: argparse.Namespace) -> None:
    """Run the full SVG method pipeline end-to-end."""
    run_dir = args.run_dir
    state = _load_state(run_dir)
    if not state:
        _error("No state.json found. Run 'init' first.")

    _init_llm(state.get("llm_preset") or state.get("backend"))

    from graphs.svg_method_pipeline import app_svg_method

    # Build initial state for SVG pipeline
    input_dir = state.get("method_input_dir", "")
    if not input_dir:
        # Create from proposal if method-propose wasn't run separately
        proposal_text = state.get("proposal", "")
        input_dir = str(Path(run_dir) / "method_input")
        Path(input_dir).mkdir(parents=True, exist_ok=True)
        (Path(input_dir) / "proposal.md").write_text(proposal_text + "\n", encoding="utf-8")
        results_dir = state.get("results_dir", "")
        if results_dir:
            from graphs.pipeline_helpers import (
                build_experiments_context_markdown as _build_experiments_context_markdown,
            )

            context_md = _build_experiments_context_markdown(results_dir)
            (Path(input_dir) / "experiments_context.md").write_text(context_md, encoding="utf-8")

    # Set up run_dir for SVG pipeline (creates svg/ subdirectory)
    svg_run_dir = str(Path(run_dir) / "svg")
    Path(svg_run_dir).mkdir(parents=True, exist_ok=True)

    pipe_state = {
        "input_dir": input_dir,
        "results_dir": state.get("results_dir", ""),
        "run_dir": svg_run_dir,
        "verbose": args.verbose,
        "doc_type": args.doc_type or "journal",
        "max_team_iterations": args.max_team_iterations,
        "sam_prompts": args.sam_prompts,
        "sam_min_score": args.sam_min_score,
        "sam_merge_threshold": args.sam_merge_threshold,
        "optimize_iterations": args.optimize_iterations,
        "figure_paths": [],
        "architecture_few_shots": [],
        "architecture_examples_dir": args.architecture_examples or "",
    }

    # Carry over method_description/drawing_prompt if already computed
    if state.get("method_description"):
        pipe_state["method_description"] = state["method_description"]
    if state.get("drawing_prompt"):
        pipe_state["drawing_prompt"] = state["drawing_prompt"]

    # Run the full compiled pipeline with progress via streaming
    _progress("Starting SVG method pipeline (image gen -> SAM3 -> SVG -> review)...")
    _progress("This may take 5-15 minutes. Progress updates will appear below.")

    # Use stream mode to report progress per-node
    accumulated = {}
    _SVG_STAGE_LABELS = {
        "load_markdown": "Loading markdown files",
        "method_data_explorer": "Exploring data (tool-grounded)",
        "method_proposer": "Extracting method description",
        "image_generation": "Generating raster image (Gemini/Azure)",
        "architecture_review": "Architecture review (structural check)",
        "sam3_detect": "SAM3 detection (all supported prompts)",
        "sam3_review": "SAM3 review (finding missed regions)",
        "sam3_merge_classify": "SAM3 merge & classify (filtering detections)",
        "ocr_text_detection": "OCR text detection (PaddleOCR)",
        "icon_extraction": "Classifying icons vs structural elements",
        "visualization_code_gen": "Generating icon visualization code",
        "svg_generation": "Generating SVG code from detected elements",
        "svg_validation": "Validating SVG syntax",
        "svg_fix": "Fixing SVG errors",
        "svg_optimization": "Optimizing SVG visual quality (LLM)",
        "icon_replacement": "Replacing icon placeholders with images",
        "svg_render": "Rendering SVG to PNG",
        "architect_review": "Technical architect review (5 dimensions)",
        "advocate_review": "Devil's advocate review (readability)",
        "consensus_router": "Consensus routing (accept/refine/regenerate)",
        "svg_refinement": "Refining SVG based on feedback",
        "regenerate_prompt": "Improving prompt for regeneration",
        "finalize": "Finalizing artifacts (SVG + PNG + PDF)",
        "fail_end": "Pipeline failed",
    }

    for event in app_svg_method.stream(pipe_state, stream_mode="updates"):
        for node_name, update in event.items():
            if not isinstance(update, dict):
                continue
            accumulated.update(update)
            label = _SVG_STAGE_LABELS.get(node_name, node_name)
            _progress(f"  [{_dim(node_name)}] {label}")

            if node_name == "consensus_router":
                action = accumulated.get("refinement_action", "?")
                score = accumulated.get("combined_score", 0)
                _progress(f"  Consensus: score={_bold(f'{score:.1f}')}/10 -> {action}", "ok" if score >= 7 else "warn")
            elif node_name == "finalize":
                score = accumulated.get("combined_score", 0)
                _progress(f"  Final score: {_bold(f'{score:.1f}')}/10", "ok" if score >= 7 else "warn")
            elif update.get("error"):
                _progress(f"  ERROR: {_red(update['error'][:100])}", "err")

    result = accumulated

    # Persist results
    for key in (
        "figure_path",
        "final_svg_path",
        "combined_score",
        "combined_issues",
        "review_history",
        "team_iteration",
        "refinement_action",
        "error",
        "success",
    ):
        if key in result:
            state[key] = result[key]

    figure_path = result.get("figure_path", "")
    if figure_path:
        state.setdefault("figure_paths", []).append(figure_path)

    state["completed_steps"] = state.get("completed_steps", []) + ["svg_pipeline"]
    _save_state(run_dir, state)

    _json_out(
        {
            "status": "ok" if not result.get("error") else "error",
            "error": result.get("error", ""),
            "figure_path": figure_path,
            "final_svg_path": result.get("final_svg_path", ""),
            "combined_score": result.get("combined_score", 0),
            "team_iterations": result.get("team_iteration", 0),
            "refinement_action": result.get("refinement_action", ""),
        }
    )


# ── image-generate ───────────────────────────────────────────────────


def cmd_image_generate(args: argparse.Namespace) -> None:
    """Generate a raster method/architecture image from the method description.

    Wraps image_generation_node() for standalone use by the @svg-builder agent.
    Reads method_description from state.json, calls the image generation API,
    saves figure.png to run_dir.
    """
    run_dir = args.run_dir
    state = _load_state(run_dir)
    if not state:
        _error("No state.json found. Run 'init' first.")

    _init_llm(state.get("llm_preset") or state.get("backend"))

    run_path = Path(run_dir)

    # Skip if figure.png already exists (user-provided drawing image)
    existing = run_path / "figure.png"
    if existing.exists() and not args.force:
        _progress("figure.png already exists — skipping image generation (use --force to override)")
        _json_out(
            {
                "status": "skipped",
                "generated_image_path": str(existing),
            }
        )
        return

    method_desc = state.get("method_description", "")
    if not method_desc:
        # Try reading from method_description.md
        md_path = run_path / "method_description.md"
        if md_path.exists():
            method_desc = md_path.read_text(encoding="utf-8").strip()
        if not method_desc:
            _error("No method_description in state.json or method_description.md")

    from graphs.svg_method_pipeline import image_generation_node

    # Build minimal state for the node
    pipe_state = {
        "run_dir": run_dir,
        "method_description": method_desc,
        "verbose": args.verbose,
        "architecture_few_shots": state.get("architecture_few_shots", []),
    }

    # Pass refined prompt if provided
    if args.refined_prompt:
        pipe_state["refined_prompt"] = args.refined_prompt

    _progress("Generating raster image from method description...")
    result = image_generation_node(pipe_state)

    if result.get("error"):
        _error(result["error"])

    # Persist to state.json
    for key in ("generated_image_path", "drawing_prompt"):
        if key in result:
            state[key] = result[key]
    state["completed_steps"] = state.get("completed_steps", []) + ["image-generate"]
    _save_state(run_dir, state)

    _progress(f"Image saved: {result.get('generated_image_path', '')}", "ok")
    _json_out(
        {
            "status": "ok",
            "generated_image_path": result.get("generated_image_path", ""),
            "drawing_prompt": result.get("drawing_prompt", "")[:300],
        }
    )


# ── icon-replace ─────────────────────────────────────────────────────


def cmd_icon_replace(args: argparse.Namespace) -> None:
    """Replace icon placeholders in an SVG with base64-encoded PNGs.

    Wraps icon_replacement_node() for standalone use by the @svg-builder agent.
    Reads template.svg (or --svg-path), icon_infos.json, and boxlib.json from run_dir.
    Produces final.svg with embedded base64 icons.
    """
    run_dir = args.run_dir
    state = _load_state(run_dir)
    if not state:
        _error("No state.json found. Run 'init' first.")

    run_path = Path(run_dir)

    # Determine SVG source
    svg_path = args.svg_path
    if not svg_path:
        svg_path = state.get("template_svg_path", str(run_path / "template.svg"))
    if not Path(svg_path).exists():
        _error(f"SVG file not found: {svg_path}")
    svg_code = Path(svg_path).read_text(encoding="utf-8")

    # Load icon_infos
    icon_infos_path = args.icon_infos or state.get("icon_infos_path", str(run_path / "icon_infos.json"))
    if not Path(icon_infos_path).exists():
        _progress("No icon_infos.json found — writing SVG as final.svg without icon replacement")
        final_path = str(run_path / "final.svg")
        Path(final_path).write_text(svg_code, encoding="utf-8")
        state["final_svg_path"] = final_path
        state["completed_steps"] = state.get("completed_steps", []) + ["icon-replace"]
        _save_state(run_dir, state)
        _json_out({"status": "ok", "final_svg_path": final_path, "icons_replaced": 0})
        return

    icon_infos = json.loads(Path(icon_infos_path).read_text(encoding="utf-8"))

    from graphs.svg_method_pipeline import icon_replacement_node

    # Build minimal state for the node
    pipe_state = {
        "run_dir": run_dir,
        "svg_code": svg_code,
        "icon_infos": icon_infos,
        "generated_image_path": state.get("generated_image_path", str(run_path / "figure.png")),
        "verbose": args.verbose,
    }

    _progress(f"Replacing {len(icon_infos)} icon placeholders in SVG...")
    result = icon_replacement_node(pipe_state)

    if result.get("error"):
        _error(result["error"])

    # Persist to state.json
    final_svg_path = result.get("final_svg_path", "")
    state["final_svg_path"] = final_svg_path
    if result.get("scale_factors"):
        state["scale_factors"] = result["scale_factors"]
    state["completed_steps"] = state.get("completed_steps", []) + ["icon-replace"]
    _save_state(run_dir, state)

    _progress(f"Final SVG saved: {final_svg_path}", "ok")
    _json_out(
        {
            "status": "ok",
            "final_svg_path": final_svg_path,
            "icons_replaced": len(icon_infos),
        }
    )


# ── services ─────────────────────────────────────────────────────────


_SERVICE_NAMES = ("sam3", "ocr", "ben2")
_SERVICE_HEALTH_IMPORTS = {
    "sam3": "services.sam3.client",
    "ocr": "services.ocr.client",
    "ben2": "services.ben2.client",
}


def _service_log_dir(*, create: bool) -> Path:
    log_dir = REPO_ROOT / "notes" / "service_logs"
    if create:
        log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _detect_service_device() -> str:
    device = "cpu"
    try:
        import torch

        if torch.cuda.is_available():
            device = "cuda"
    except ImportError:
        pass
    return device


def _service_definitions(device: str | None = None) -> dict[str, dict[str, object]]:
    resolved_device = device or "cpu"
    return {
        "sam3": {
            "module": "services.sam3.server",
            "port": 8001,
            "extra_args": ["--config", str(REPO_ROOT / "configs" / "services.yaml"), "--device", resolved_device],
        },
        "ocr": {
            "module": "services.ocr.server",
            "port": 8002,
            "extra_args": [],
        },
        "ben2": {
            "module": "services.ben2.server",
            "port": 8003,
            "extra_args": ["--device", resolved_device],
        },
    }


def _service_pid_file(log_dir: Path, name: str) -> Path:
    return log_dir / f"{name}.pid"


def _http_service_healthy(port: int) -> bool:
    try:
        import requests as _req

        resp = _req.get(f"http://127.0.0.1:{port}/health", timeout=3)
        return resp.status_code == 200
    except (ImportError, OSError, ValueError):
        return False


def _collect_service_health() -> dict:
    results = {}
    for name, module_name in _SERVICE_HEALTH_IMPORTS.items():
        try:
            module = importlib.import_module(module_name)
            results[name] = bool(module.check_health())
        except Exception as exc:
            results[name] = False
            results[f"{name}_error"] = str(exc)
    results["all_healthy"] = all(results.get(name, False) for name in _SERVICE_NAMES)
    return results


def _start_services() -> dict:
    # Start services as detached processes that survive after this CLI exits.
    # The launchers (ensure_*_service) use atexit + PIPE which kills services
    # on process exit — so we spawn directly here instead.
    results = {}
    errors = []
    log_dir = _service_log_dir(create=True)
    services = _service_definitions(_detect_service_device())

    for name, service in services.items():
        port = int(service["port"])
        if _http_service_healthy(port):
            results[name] = True
            continue

        cmd = [
            sys.executable,
            "-m",
            str(service["module"]),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            *[str(arg) for arg in service["extra_args"]],
        ]

        log_file = log_dir / f"{name}.log"
        with open(log_file, "w") as lf:
            proc = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                cwd=str(REPO_ROOT),
                start_new_session=True,
            )
        _service_pid_file(log_dir, name).write_text(str(proc.pid))
        results[name] = "starting"

    timeout = 300  # SAM3 vit_h model can take 2-5 min to load
    deadline = time.time() + timeout
    pending = [name for name in _SERVICE_NAMES if results.get(name) == "starting"]
    while pending and time.time() < deadline:
        still_pending = []
        for name in pending:
            port = int(services[name]["port"])
            if _http_service_healthy(port):
                results[name] = True
                continue
            still_pending.append(name)
        pending = still_pending
        if pending:
            time.sleep(5)

    for name in pending:
        results[name] = False
        log_file = log_dir / f"{name}.log"
        err_msg = f"{name} not healthy after {timeout}s"
        if log_file.exists():
            tail = log_file.read_text().strip().split("\n")[-10:]
            err_msg += f" (last log: {''.join(tail)[:200]})"
        errors.append(err_msg)

    results["all_healthy"] = not errors
    if errors:
        results["errors"] = errors
    return results


def _is_expected_service_pid(pid: int, service_name: str) -> bool | None:
    """Check if PID belongs to the expected service.

    Returns True if confirmed ours, False if confirmed not ours,
    None if validation is unavailable (non-Linux, permission denied).
    """
    module_marker = str(_service_definitions()[service_name]["module"])
    try:
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if not cmdline_path.exists():
            return None
        cmdline = cmdline_path.read_bytes().decode("utf-8", errors="replace")
        return module_marker in cmdline
    except (OSError, PermissionError):
        return None


def _stop_services() -> dict:
    results = {}
    log_dir = _service_log_dir(create=False)

    for name in _SERVICE_NAMES:
        pid_file = _service_pid_file(log_dir, name)
        if not pid_file.exists():
            results[name] = "no pid file (not started by pipeline_cli?)"
            continue
        try:
            pid = int(pid_file.read_text().strip())
            ownership = _is_expected_service_pid(pid, name)
            if ownership is False:
                pid_file.unlink(missing_ok=True)
                results[name] = "stale pid file (process is not this service); cleaned up"
                continue
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.5)
                except OSError:
                    break
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            pid_file.unlink(missing_ok=True)
            results[name] = "stopped"
        except (ValueError, OSError) as exc:
            results[name] = f"error: {exc}"
            pid_file.unlink(missing_ok=True)

    return results


def cmd_services(args: argparse.Namespace) -> None:
    """Manage microservices (SAM3, OCR, BEN2)."""
    action = args.service_action

    if action == "health":
        _json_out(_collect_service_health())
    elif action == "start":
        _json_out(_start_services())
    elif action == "stop":
        _json_out(_stop_services())
    else:
        _error(f"Unknown service action: {action}. Use start|stop|health.")


# ── main ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="HappyFigure pipeline CLI — opencode tool backend")
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Initialize run directory and load proposal")
    p_init.add_argument("--proposal", required=True, help="Path to proposal file (.md, .pdf, .tex) or directory")
    p_init.add_argument("--results-dir", default=None, help="Path to experiments/results directory")
    p_init.add_argument("--llm-preset", default=None, choices=["azure", "gemini", "mixed"])
    p_init.add_argument("--run-dir", default=None, help="Custom run directory path")
    p_init.add_argument(
        "--mode", default="happyfigure", choices=["happyfigure", "exp_plot", "composite", "paper_composite"]
    )
    p_init.add_argument("--style-examples", default=None, help="Style examples directory")

    # data-scan
    p_scan = sub.add_parser("data-scan", help="Scan data directory and discover experiments")
    p_scan.add_argument("--run-dir", required=True)
    p_scan.add_argument("--verbose", action="store_true")

    # data-process
    p_proc = sub.add_parser("data-process", help="Process raw data for figure generation")
    p_proc.add_argument("--run-dir", required=True)
    p_proc.add_argument("--verbose", action="store_true")
    p_proc.add_argument("--data-processing-mode", default="regen", choices=["regen", "reuse"])

    # figure-plan
    p_plan = sub.add_parser("figure-plan", help="Plan figures for all experiments")
    p_plan.add_argument("--run-dir", required=True)
    p_plan.add_argument("--verbose", action="store_true")
    p_plan.add_argument(
        "--exploration-report", default=None, help="Path to pre-computed data exploration report (skip LLM explorer)"
    )
    p_plan.add_argument(
        "--proposal", default=None, help="Path to proposal .md file (for auto-bootstrap when state.json missing)"
    )
    p_plan.add_argument("--results-dir", default=None, help="Path to results directory (for auto-bootstrap)")
    p_plan.add_argument(
        "--llm-preset", default=None, choices=["azure", "gemini", "mixed"], help="LLM preset (for auto-bootstrap)"
    )
    p_plan.add_argument(
        "--mode",
        default="exp_plot",
        choices=["happyfigure", "exp_plot", "composite"],
        help="Pipeline mode (for auto-bootstrap)",
    )

    # figure-execute
    p_exec = sub.add_parser("figure-execute", help="Execute figure generation for one experiment")
    p_exec.add_argument("--run-dir", required=True)
    p_exec.add_argument("--experiment", default=None, help="Experiment name (default: current)")
    p_exec.add_argument("--verbose", action="store_true")

    # figure-execute-parallel
    p_par = sub.add_parser("figure-execute-parallel", help="Execute all experiments in parallel")
    p_par.add_argument("--run-dir", required=True)
    p_par.add_argument("--verbose", action="store_true")

    # figure-execute-beam
    p_beam = sub.add_parser("figure-execute-beam", help="Beam search figure generation")
    p_beam.add_argument("--run-dir", required=True)
    p_beam.add_argument("--verbose", action="store_true")
    p_beam.add_argument("--beam-width", type=int, default=2)
    p_beam.add_argument("--style-variants", type=int, default=2)
    p_beam.add_argument("--code-variants", type=int, default=2)
    p_beam.add_argument("--beam-iterations", type=int, default=2)

    # method-propose
    p_mp = sub.add_parser("method-propose", help="Extract method description and drawing prompt")
    p_mp.add_argument("--run-dir", required=True)
    p_mp.add_argument("--verbose", action="store_true")
    p_mp.add_argument("--doc-type", default="journal")
    p_mp.add_argument("--architecture-examples", default=None)

    # svg-pipeline
    p_svg = sub.add_parser("svg-pipeline", help="Run full SVG method drawing pipeline")
    p_svg.add_argument("--run-dir", required=True)
    p_svg.add_argument("--verbose", action="store_true")
    p_svg.add_argument("--doc-type", default="journal")
    p_svg.add_argument("--architecture-examples", default=None)
    p_svg.add_argument("--max-team-iterations", type=int, default=3)
    p_svg.add_argument(
        "--sam-prompts",
        default="rectangle,box,arrow,icon,graph,subfigure,robot,logo,dashed_line,dotted_line,dashed_box,dotted_box,dashed_rounded_box,rounded_box",
    )
    p_svg.add_argument("--sam-min-score", type=float, default=0.0)
    p_svg.add_argument("--sam-merge-threshold", type=float, default=0)
    p_svg.add_argument("--optimize-iterations", type=int, default=2)

    # image-generate
    p_img = sub.add_parser("image-generate", help="Generate raster image from method description")
    p_img.add_argument("--run-dir", required=True)
    p_img.add_argument("--verbose", action="store_true")
    p_img.add_argument("--force", action="store_true", help="Regenerate even if figure.png exists")
    p_img.add_argument("--refined-prompt", default=None, help="Override drawing prompt for regeneration")

    # icon-replace
    p_icon = sub.add_parser("icon-replace", help="Replace icon placeholders in SVG with base64 PNGs")
    p_icon.add_argument("--run-dir", required=True)
    p_icon.add_argument("--verbose", action="store_true")
    p_icon.add_argument("--svg-path", default=None, help="Path to SVG file (default: template.svg from state)")
    p_icon.add_argument("--icon-infos", default=None, help="Path to icon_infos.json (default: from state)")

    # services
    p_svc = sub.add_parser("services", help="Manage SAM3/OCR/BEN2 microservices")
    p_svc.add_argument("service_action", choices=["start", "stop", "health"])

    args = parser.parse_args()

    dispatch = {
        "init": cmd_init,
        "data-scan": cmd_data_scan,
        "data-process": cmd_data_process,
        "figure-plan": cmd_figure_plan,
        "figure-execute": cmd_figure_execute,
        "figure-execute-parallel": cmd_figure_execute_parallel,
        "figure-execute-beam": cmd_figure_execute_beam,
        "method-propose": cmd_method_propose,
        "svg-pipeline": cmd_svg_pipeline,
        "image-generate": cmd_image_generate,
        "icon-replace": cmd_icon_replace,
        "services": cmd_services,
    }

    handler = dispatch.get(args.command)
    if handler:
        try:
            handler(args)
        except SystemExit:
            raise
        except Exception as exc:
            _error(f"{args.command} failed: {exc}")
    else:
        _error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
