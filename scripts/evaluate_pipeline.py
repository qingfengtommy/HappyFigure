#!/usr/bin/env python3 -u
"""
HappyFigure pipeline evaluation script.

Runs cli.py across a matrix of commands x agent platforms, collects
results (exit codes, durations, figures), and produces both a terminal
summary and a self-contained HTML report.

Runs are executed sequentially on purpose. The current orchestrators write
shared config files in the repository root (for example AGENTS.md,
GEMINI.md, and .opencode/agent/*.md), so parallel launches would race and
invalidate the evaluation.

Usage:
    python scripts/evaluate_pipeline.py \
        --proposal path/to/proposal.txt \
        --results-dir path/to/results

    python scripts/evaluate_pipeline.py \
        --proposal path/to/proposal.txt \
        --results-dir path/to/results \
        --commands plot sketch --agents claude --llm-preset gemini
"""
from __future__ import annotations

import argparse
import base64
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ALL_COMMANDS = ["plot", "sketch", "diagram", "composite"]
ALL_AGENTS = ["opencode", "claude", "codex", "gemini", "copilot"]
NEEDS_SERVICES = {"diagram", "composite"}

TIMEOUTS = {
    "plot": 2400,
    "sketch": 2400,
    "diagram": 2400,
    "composite": 2400,
}

RUN_DIR_PATTERNS = [
    r"(notes/figure_runs/run_\S+)",
    r"(notes/diagram_runs/run_\S+)",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    command: str
    agent: str
    status: str = "skip"  # pass, fail, skip, timeout
    exit_code: int = -1
    duration: float = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""
    run_dir: Optional[str] = None
    figures: list[str] = field(default_factory=list)  # absolute paths
    error_msg: str = ""
    execution_mode: str = "sequential"
    figure_scores: dict = field(default_factory=dict)  # exp_name -> score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


def _tail(text: str, n: int = 50) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if len(lines) > n else text


def _check_agent_available(agent: str) -> bool:
    """Return True if the agent CLI binary is on PATH."""
    return shutil.which(agent) is not None


def _load_env() -> None:
    """Load repo-local .env when python-dotenv is available."""
    try:
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass


def _check_auth(agents: list[str], config: dict | None = None) -> dict[str, dict]:
    """Pre-check each agent platform using the same orchestrator logic as cli.py."""
    _load_env()

    if config is None:
        from graphs.svg_utils import load_pipeline_config
        config = load_pipeline_config()

    from agents import create_orchestrator

    results: dict[str, dict] = {}
    for agent in agents:
        if not _check_agent_available(agent):
            results[agent] = {"ok": False, "detail": "CLI not found on PATH"}
            continue

        try:
            orch = create_orchestrator(agent, config)
            result = orch.check_auth()
            detail = result.get("message") or "auth check completed"
            if result.get("error"):
                detail = f"{detail} — {result['error']}"
            results[agent] = {"ok": bool(result.get("ok")), "detail": detail}
        except Exception as exc:
            results[agent] = {"ok": False, "detail": f"auth check failed: {exc}"}

    return results


def _find_run_dir(stdout: str) -> Optional[str]:
    """Extract the run_dir path from stdout."""
    for pattern in RUN_DIR_PATTERNS:
        matches = re.findall(pattern, stdout)
        if matches:
            # Return the last match (most likely the final run_dir)
            candidate = matches[-1].rstrip("/")
            full = PROJECT_ROOT / candidate
            if full.is_dir():
                return str(full)
    return None


def _collect_figures(run_dir: str) -> list[str]:
    """Glob for PNG and SVG files in run_dir tree."""
    figures: list[str] = []
    for ext in ("*.png", "*.svg"):
        figures.extend(glob.glob(os.path.join(run_dir, "**", ext), recursive=True))
    # Sort by name for deterministic ordering; limit to 20 to keep report sane
    figures.sort()
    return figures[:20]


def _image_to_base64(path: str) -> Optional[str]:
    """Read an image file and return a base64-encoded data URI."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    ext = Path(path).suffix.lower()
    mime = "image/png" if ext == ".png" else "image/svg+xml" if ext == ".svg" else "image/png"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _resolve_llm_config(config: dict, llm_preset: Optional[str]) -> dict[str, str]:
    """Return a summary of active LLM roles: {role: "provider/model"}."""
    llm_cfg = config.get("llm", {})
    roles = llm_cfg.get("roles", {})

    # Start with default roles
    summary: dict[str, str] = {}
    for role, role_def in roles.items():
        if isinstance(role_def, dict):
            provider = role_def.get("provider", "?")
            model = role_def.get("model", "?")
            summary[role] = f"{provider}/{model}"

    # Overlay preset if specified
    if llm_preset:
        presets = llm_cfg.get("presets", {})
        preset = presets.get(llm_preset, {})
        for role, role_def in preset.items():
            if isinstance(role_def, dict):
                provider = role_def.get("provider", "?")
                model = role_def.get("model", "?")
                summary[role] = f"{provider}/{model}"

    return summary


def _resolve_agent_model(config: dict, agent: str) -> str:
    """Return the model name configured for an agent platform."""
    agent_cfg = config.get("agent", {}).get(agent, {})
    model = agent_cfg.get("model", "?")
    provider = agent_cfg.get("provider", "")
    return f"{provider}/{model}" if provider else model


def _extract_figure_scores(run_dir: str) -> dict[str, float]:
    """Parse critic scores from run_dir.

    Looks for critic_result*.json files and state.json for score data.
    Returns {experiment_name: best_score}.
    """
    scores: dict[str, float] = {}
    run_path = Path(run_dir)

    # Try critic_result files in experiment subdirectories
    for critic_file in sorted(run_path.glob("*/critic_result*.json")):
        exp_name = critic_file.parent.name
        try:
            data = json.loads(critic_file.read_text())
            score = data.get("total_score") or data.get("score")
            if score is not None:
                # Keep the best score per experiment
                score = float(score)
                if exp_name not in scores or score > scores[exp_name]:
                    scores[exp_name] = score
        except (json.JSONDecodeError, OSError, ValueError):
            continue

    # Fallback: check state.json per_experiment_results for scores
    state_file = run_path / "state.json"
    if state_file.exists() and not scores:
        try:
            state = json.loads(state_file.read_text())
            per_exp = state.get("per_experiment_results", {})
            for name, exp_data in per_exp.items():
                if isinstance(exp_data, dict):
                    score = exp_data.get("score")
                    if score is not None:
                        scores[str(name)] = float(score)
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    # Also check outputs directory for critic results
    for critic_file in sorted(run_path.glob("outputs/*/critic_result*.json")):
        exp_name = critic_file.parent.name
        try:
            data = json.loads(critic_file.read_text())
            score = data.get("total_score") or data.get("score")
            if score is not None:
                score = float(score)
                if exp_name not in scores or score > scores[exp_name]:
                    scores[exp_name] = score
        except (json.JSONDecodeError, OSError, ValueError):
            continue

    return scores


# ---------------------------------------------------------------------------
# Service management
# ---------------------------------------------------------------------------


def _services_cmd(action: str, timeout: int = 360) -> dict:
    """Run pipeline_cli.py services <action> and return parsed JSON."""
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "pipeline_cli.py"),
        "services",
        action,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(PROJECT_ROOT)
        )
        return json.loads(proc.stdout) if proc.stdout.strip() else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as exc:
        return {"all_healthy": False, "error": str(exc)}


def _start_services() -> bool:
    """Start microservices and wait for health. Returns True if all healthy."""
    # First check if services are already running
    health = _services_cmd("health", timeout=15)
    svc_keys = [k for k in health if k not in ("all_healthy", "error") and not k.endswith("_error")]
    already_up = sum(1 for k in svc_keys if health[k] is True)
    if already_up >= 2:
        print(f"  Services already running ({already_up}/3 healthy). Skipping start.")
        return health.get("all_healthy", False)

    print("  Starting services (SAM3, OCR, BEN2)...")
    # Start in background — don't block if one service is slow
    try:
        subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "pipeline_cli.py"), "services", "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(PROJECT_ROOT),
        )
    except Exception as exc:
        print(f"  WARNING: Failed to launch services: {exc}")
        return False

    # Wait up to 120s for services to become healthy
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(10)
        health = _services_cmd("health", timeout=15)
        svc_keys2 = [k for k in health if k not in ("all_healthy", "error") and not k.endswith("_error")]
        healthy_count = sum(1 for k in svc_keys2 if health[k] is True)
        if health.get("all_healthy"):
            print("  All services healthy.")
            return True
        print(f"  Waiting for services... ({healthy_count}/3 healthy)")

    print("  WARNING: Not all services healthy after 120s. Proceeding anyway.")
    return False


def _stop_services() -> None:
    print("  Stopping services...")
    _services_cmd("stop", timeout=60)
    print("  Services stopped.")


# ---------------------------------------------------------------------------
# Single run execution
# ---------------------------------------------------------------------------


def _run_single(
    command: str,
    agent: str,
    proposal: str,
    results_dir: str,
    llm_preset: Optional[str],
    execution_mode: str = "sequential",
) -> RunResult:
    """Execute one cli.py invocation and return a RunResult."""
    result = RunResult(command=command, agent=agent, execution_mode=execution_mode)

    # Global flags (--agent, --llm-preset) go BEFORE the subcommand
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "cli.py"),
        "--agent", agent,
    ]
    if llm_preset:
        cmd.extend(["--llm-preset", llm_preset])
    cmd.extend([
        command,
        "--proposal", proposal,
        "--results-dir", results_dir,
    ])
    if execution_mode != "sequential" and command == "plot":
        cmd.extend(["--execution", execution_mode])

    timeout = TIMEOUTS.get(command, 600)
    label = f"[{command}/{agent}]"
    print(f"  {label} Running (timeout {_fmt_duration(timeout)})...")
    print(f"  {label} cmd: {' '.join(cmd)}")

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
        )
        result.duration = time.monotonic() - t0
        result.exit_code = proc.returncode
        result.stdout_tail = _tail(proc.stdout)
        result.stderr_tail = _tail(proc.stderr)
        result.status = "pass" if proc.returncode == 0 else "fail"
        if proc.returncode != 0:
            result.error_msg = f"Exit code {proc.returncode}"

        # Find run_dir from stdout
        result.run_dir = _find_run_dir(proc.stdout)
        if result.run_dir:
            result.figures = _collect_figures(result.run_dir)
            result.figure_scores = _extract_figure_scores(result.run_dir)

    except subprocess.TimeoutExpired as exc:
        result.duration = time.monotonic() - t0
        result.status = "timeout"
        result.error_msg = f"Timed out after {timeout}s"
        _out = exc.stdout or b""
        _err = exc.stderr or b""
        result.stdout_tail = _tail(_out.decode("utf-8", errors="replace") if isinstance(_out, bytes) else _out)
        result.stderr_tail = _tail(_err.decode("utf-8", errors="replace") if isinstance(_err, bytes) else _err)

    except Exception as exc:
        result.duration = time.monotonic() - t0
        result.status = "fail"
        result.error_msg = str(exc)

    symbol = {"pass": "OK", "fail": "FAIL", "timeout": "TIMEOUT", "skip": "SKIP"}[result.status]
    print(f"  {label} {symbol} ({_fmt_duration(result.duration)})")
    if result.run_dir:
        print(f"  {label}   run_dir: {result.run_dir}")
    if result.figure_scores:
        scores_str = ", ".join(f"{k}: {v:.1f}" for k, v in result.figure_scores.items())
        print(f"  {label}   scores: {scores_str}")
    if result.status != "pass" and result.stderr_tail:
        # Print last 5 lines of stderr for quick diagnosis
        for line in result.stderr_tail.strip().splitlines()[-5:]:
            print(f"  {label}   stderr: {line}")
    return result


def _run_matrix(
    run_pairs: list[tuple[str, str]],
    proposal: str,
    results_dir: str,
    llm_preset: Optional[str],
    execution_mode: str = "sequential",
) -> list[RunResult]:
    """Execute the evaluation matrix sequentially.

    The current orchestrators mutate shared repo-root files during setup, so
    running multiple HappyFigure processes concurrently is unsafe.
    """
    results: list[RunResult] = []
    for command, agent in run_pairs:
        results.append(
            _run_single(command, agent, proposal, results_dir, llm_preset, execution_mode)
        )
    return results


# ---------------------------------------------------------------------------
# HTML report generation
# ---------------------------------------------------------------------------

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       margin: 0; padding: 24px; background: #f8f9fa; color: #212529; }
h1 { margin-top: 0; }
.meta { color: #6c757d; margin-bottom: 24px; }
table { border-collapse: collapse; margin-bottom: 32px; }
th, td { border: 1px solid #dee2e6; padding: 10px 16px; text-align: center; }
th { background: #343a40; color: #fff; }
.pass { background: #d4edda; color: #155724; }
.fail { background: #f8d7da; color: #721c24; }
.timeout { background: #f8d7da; color: #721c24; }
.skip { background: #e2e3e5; color: #383d41; }
details { background: #fff; border: 1px solid #dee2e6; border-radius: 6px;
          margin-bottom: 16px; padding: 16px; }
summary { cursor: pointer; font-weight: 600; font-size: 1.05em; }
pre { background: #f1f3f5; padding: 12px; border-radius: 4px;
      overflow-x: auto; font-size: 0.85em; max-height: 400px; overflow-y: auto; }
.figures { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; }
.figures img { max-width: 360px; max-height: 300px; border: 1px solid #dee2e6;
               border-radius: 4px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: 600;
         font-size: 0.85em; }
"""


def _generate_html_report(
    results: list[RunResult],
    proposal: str,
    total_duration: float,
    output_path: Path,
    commands: list[str],
    agents: list[str],
    llm_config: Optional[dict[str, str]] = None,
    execution_mode: str = "sequential",
) -> None:
    """Write a self-contained HTML evaluation report."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    proposal_name = Path(proposal).name

    # Build lookup
    lookup: dict[tuple[str, str], RunResult] = {}
    for r in results:
        lookup[(r.command, r.agent)] = r

    html_parts: list[str] = []
    html_parts.append(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>HappyFigure Evaluation Report</title>
<style>{_CSS}</style></head><body>
<h1>HappyFigure Evaluation Report</h1>
<div class="meta">
  <strong>Date:</strong> {now}<br>
  <strong>Proposal:</strong> {proposal_name}<br>
  <strong>Total duration:</strong> {_fmt_duration(total_duration)}<br>
  <strong>Execution mode:</strong> {execution_mode}<br>
  <strong>Matrix:</strong> {len(commands)} commands &times; {len(agents)} agents
</div>
""")

    # LLM config table
    if llm_config:
        html_parts.append("<h2>LLM Configuration</h2>\n<table><tr><th>Role</th><th>Provider / Model</th></tr>")
        for role, model_str in llm_config.items():
            html_parts.append(f"<tr><td>{role}</td><td>{model_str}</td></tr>")
        html_parts.append("</table>")

    # Summary matrix
    html_parts.append("<h2>Summary Matrix</h2>\n<table><tr><th>Command</th>")
    for agent in agents:
        html_parts.append(f"<th>{agent}</th>")
    html_parts.append("</tr>")

    for cmd in commands:
        html_parts.append(f"<tr><td><strong>{cmd}</strong></td>")
        for agent in agents:
            r = lookup.get((cmd, agent))
            if r is None or r.status == "skip":
                html_parts.append('<td class="skip">SKIP</td>')
            else:
                cls = r.status
                label = r.status.upper()
                dur = _fmt_duration(r.duration)
                html_parts.append(f'<td class="{cls}">{label}<br><small>{dur}</small></td>')
        html_parts.append("</tr>")
    html_parts.append("</table>")

    # Per-run details
    html_parts.append("<h2>Run Details</h2>")
    for r in results:
        status_cls = r.status
        html_parts.append(f"""<details>
<summary>
  <span class="badge {status_cls}">{r.status.upper()}</span>
  {r.command} / {r.agent} &mdash; {_fmt_duration(r.duration)}
</summary>
<table>
  <tr><td><strong>Command</strong></td><td>{r.command}</td></tr>
  <tr><td><strong>Agent</strong></td><td>{r.agent}</td></tr>
  <tr><td><strong>Status</strong></td><td>{r.status.upper()}</td></tr>
  <tr><td><strong>Exit code</strong></td><td>{r.exit_code}</td></tr>
  <tr><td><strong>Duration</strong></td><td>{_fmt_duration(r.duration)}</td></tr>
  <tr><td><strong>Execution</strong></td><td>{r.execution_mode}</td></tr>
  <tr><td><strong>Run dir</strong></td><td>{r.run_dir or 'N/A'}</td></tr>
</table>
""")
        if r.figure_scores:
            html_parts.append("<p><strong>Figure Scores:</strong></p><table><tr><th>Experiment</th><th>Score</th></tr>")
            for exp_name, score in r.figure_scores.items():
                html_parts.append(f"<tr><td>{exp_name}</td><td>{score:.1f}</td></tr>")
            html_parts.append("</table>")
        if r.error_msg:
            html_parts.append(f"<p><strong>Error:</strong> {r.error_msg}</p>")

        # Embedded figures (PNG only for display; SVGs listed as filenames)
        if r.figures:
            html_parts.append('<div class="figures">')
            for fig_path in r.figures:
                if fig_path.lower().endswith(".png"):
                    data_uri = _image_to_base64(fig_path)
                    if data_uri:
                        fname = Path(fig_path).name
                        html_parts.append(
                            f'<div><img src="{data_uri}" alt="{fname}"><br>'
                            f"<small>{fname}</small></div>"
                        )
                else:
                    html_parts.append(f"<div><small>SVG: {Path(fig_path).name}</small></div>")
            html_parts.append("</div>")

        # stdout / stderr
        if r.stdout_tail.strip():
            html_parts.append(
                "<details><summary>stdout (last 50 lines)</summary>"
                f"<pre>{_escape_html(r.stdout_tail)}</pre></details>"
            )
        if r.stderr_tail.strip():
            html_parts.append(
                "<details><summary>stderr (last 50 lines)</summary>"
                f"<pre>{_escape_html(r.stderr_tail)}</pre></details>"
            )
        html_parts.append("</details>")

    html_parts.append("</body></html>")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(html_parts), encoding="utf-8")


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Terminal summary
# ---------------------------------------------------------------------------


def _print_terminal_summary(
    results: list[RunResult],
    commands: list[str],
    agents: list[str],
    total_duration: float,
    report_path: Path,
) -> None:
    lookup: dict[tuple[str, str], RunResult] = {}
    for r in results:
        lookup[(r.command, r.agent)] = r

    col_w = 14
    symbols = {"pass": "+", "fail": "X", "timeout": "T", "skip": "-"}

    print()
    print("HappyFigure Evaluation Report")
    print("=" * (16 + col_w * len(agents)))
    header = f"{'':>14s}"
    for agent in agents:
        header += f"{agent:>{col_w}s}"
    print(header)
    print("-" * (16 + col_w * len(agents)))

    passed = failed = skipped = 0
    for cmd in commands:
        row = f"{cmd:>14s}"
        for agent in agents:
            r = lookup.get((cmd, agent))
            if r is None or r.status == "skip":
                row += f"{'- SKIP':>{col_w}s}"
                skipped += 1
            elif r.status == "pass":
                dur_str = _fmt_duration(r.duration)
                if r.figure_scores:
                    avg = sum(r.figure_scores.values()) / len(r.figure_scores)
                    dur_str = f"{dur_str} [{avg:.1f}]"
                row += f"{symbols['pass']} {dur_str:>{col_w - 2}s}"
                passed += 1
            elif r.status == "timeout":
                row += f"{'T TIMEOUT':>{col_w}s}"
                failed += 1
            else:
                row += f"{'X FAIL':>{col_w}s}"
                failed += 1
        print(row)

    total = passed + failed + skipped
    print("=" * (16 + col_w * len(agents)))
    print(
        f"Total: {passed}/{total} passed, {failed} failed, {skipped} skipped "
        f"({_fmt_duration(total_duration)})"
    )
    print(f"Report: {report_path}")
    print()


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate HappyFigure pipeline across commands and agent platforms."
    )
    parser.add_argument("--proposal", required=True, help="Path to the proposal file")
    parser.add_argument("--results-dir", required=True, help="Path to the results directory")
    parser.add_argument(
        "--commands",
        nargs="+",
        default=ALL_COMMANDS,
        choices=ALL_COMMANDS,
        help=f"Commands to test (default: {' '.join(ALL_COMMANDS)})",
    )
    parser.add_argument(
        "--agents",
        nargs="+",
        default=ALL_AGENTS,
        choices=ALL_AGENTS,
        help=f"Agent platforms to test (default: {' '.join(ALL_AGENTS)})",
    )
    parser.add_argument(
        "--output-dir",
        default="notes/eval_report",
        help="Directory for the evaluation report (default: notes/eval_report)",
    )
    parser.add_argument(
        "--llm-preset",
        default=None,
        help="LLM preset to pass to cli.py (e.g. azure, gemini, mixed)",
    )
    parser.add_argument(
        "--execution",
        default="sequential",
        choices=["sequential", "parallel", "beam"],
        help="Execution mode for plot runs (default: sequential)",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    proposal = str((PROJECT_ROOT / args.proposal).resolve())
    results_dir = str((PROJECT_ROOT / args.results_dir).resolve())

    # Validate paths
    if not Path(proposal).is_file():
        print(f"ERROR: Proposal file not found: {proposal}")
        sys.exit(1)
    if not Path(results_dir).is_dir():
        print(f"ERROR: Results directory not found: {results_dir}")
        sys.exit(1)

    # Check agent availability and authentication
    print("--- Pre-flight Auth Check ---")
    from graphs.svg_utils import load_pipeline_config

    config = load_pipeline_config()
    auth_results = _check_auth(args.agents, config)
    available_agents: list[str] = []
    for agent in args.agents:
        info = auth_results.get(agent, {"ok": False, "detail": "unknown"})
        status = "OK" if info["ok"] else "FAIL"
        print(f"  {agent:12s} [{status}] {info['detail']}")
        if info["ok"]:
            available_agents.append(agent)
    print()

    if not available_agents:
        print("ERROR: No agent platforms authenticated. Nothing to evaluate.")
        print("  Fix auth issues above, then re-run.")
        sys.exit(1)

    commands: list[str] = args.commands
    output_dir = PROJECT_ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "eval_report.html"

    # Resolve LLM config for display
    llm_config = _resolve_llm_config(config, args.llm_preset)
    execution_mode = args.execution

    print("HappyFigure Evaluation")
    print(f"  Proposal:    {proposal}")
    print(f"  Results dir: {results_dir}")
    print(f"  Commands:    {', '.join(commands)}")
    print(f"  Agents:      {', '.join(available_agents)}")
    print(f"  Execution:   {execution_mode}")
    print(f"  LLM preset:  {args.llm_preset or '(default)'}")
    for role, model_str in llm_config.items():
        print(f"    {role:12s} {model_str}")
    for agent in available_agents:
        agent_model = _resolve_agent_model(config, agent)
        print(f"  Agent model: {agent} -> {agent_model}")
    print(f"  Output:      {report_path}")
    print()

    all_results: list[RunResult] = []
    t_total_start = time.monotonic()

    # Start services upfront if any command needs them
    needs_any_svc = any(c in NEEDS_SERVICES for c in commands)
    if needs_any_svc:
        print("--- Starting services (SAM3, OCR, BEN2) for diagram/composite ---")
        services_ok = _start_services()
        if not services_ok:
            print("  WARNING: Services may not be fully healthy.")
        print()

    # Build all (command, agent) pairs to run
    skip_results = []
    run_pairs: list[tuple[str, str]] = []
    for command in commands:
        for agent in args.agents:
            if agent not in available_agents:
                detail = auth_results.get(agent, {}).get("detail", "Agent unavailable")
                r = RunResult(command=command, agent=agent, status="skip",
                              error_msg=detail)
                skip_results.append(r)
                print(f"  [{command}/{agent}] SKIP ({detail})")
            else:
                run_pairs.append((command, agent))

    all_results.extend(skip_results)

    print(f"--- Running {len(run_pairs)} evaluations sequentially ---")
    all_results.extend(
        _run_matrix(run_pairs, proposal, results_dir, args.llm_preset, execution_mode)
    )

    # Stop services if we started them
    if needs_any_svc:
        print()
        print("--- Stopping services ---")
        _stop_services()

    total_duration = time.monotonic() - t_total_start

    # Generate report
    _generate_html_report(
        results=all_results,
        proposal=proposal,
        total_duration=total_duration,
        output_path=report_path,
        commands=commands,
        agents=args.agents,  # use full list so skipped agents show in matrix
        llm_config=llm_config,
        execution_mode=execution_mode,
    )

    # Terminal summary
    _print_terminal_summary(all_results, commands, args.agents, total_duration, report_path)


if __name__ == "__main__":
    main()
