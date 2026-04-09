"""Subprocess calls to pipeline_cli.py and microservice management."""

from __future__ import annotations

import json
import subprocess
import sys

import ui
from pipeline.context import PROJECT_ROOT


def run_pipeline_init(
    run_dir: str,
    proposal: str,
    experiments_dir: str,
    mode: str = "exp_plot",
    llm_preset: str | None = None,
) -> None:
    """Run pipeline init to create state.json."""
    init_cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "pipeline_cli.py"),
        "init",
        "--proposal",
        proposal,
        "--run-dir",
        run_dir,
        "--mode",
        mode,
    ]
    if llm_preset:
        init_cmd.extend(["--llm-preset", llm_preset])
    if experiments_dir:
        init_cmd.extend(["--results-dir", experiments_dir])
    # Show a compact summary instead of the full command with absolute paths
    short_proposal = ui.short_path(proposal)
    short_run = ui.short_path(run_dir)
    short_results = ui.short_path(experiments_dir) if experiments_dir else ""
    init_summary = f"mode={mode} proposal={short_proposal} run_dir={short_run}"
    if short_results:
        init_summary += f" results={short_results}"
    ui.dim(f"  Init: {init_summary}")
    result = subprocess.run(init_cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        ui.warn(f"Pipeline init failed: {result.stderr}")
    else:
        ui.success("Pipeline init complete")


def start_services() -> None:
    """Start SAM3/OCR/BEN2 microservices and wait for health checks."""
    ui.info("Starting microservices (SAM3, OCR, BEN2)...")
    ui.dim("SAM3 model loading can take 2-5 min on first run.")
    result = subprocess.run(
        [sys.executable, "scripts/pipeline_cli.py", "services", "start"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        ui.warn("Services may not have started cleanly.")
        if result.stderr:
            ui.dim(result.stderr[:500])
    else:
        try:
            status = json.loads(result.stdout)
            healthy = [s for s in ("sam3", "ocr", "ben2") if status.get(s) is True]
            failed = [s for s in ("sam3", "ocr", "ben2") if status.get(s) is not True]
            ui.service_status(healthy, failed)
            for err in status.get("errors", []):
                ui.dim(f"  {err}")
        except (json.JSONDecodeError, KeyError):
            ui.info("Services started (could not parse status).")


def stop_services() -> None:
    """Stop SAM3/OCR/BEN2 microservices."""
    ui.info("Stopping microservices...")
    subprocess.run(
        [sys.executable, "scripts/pipeline_cli.py", "services", "stop"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    ui.success("Services stopped.")
