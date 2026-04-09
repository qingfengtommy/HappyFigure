"""State I/O and directory layout helpers for plot runs."""

from __future__ import annotations

import glob
import json
import os

from pipeline.contracts import StageRecord


# ---------------------------------------------------------------------------
# state.json helpers
# ---------------------------------------------------------------------------


def read_state(run_dir: str) -> dict:
    state_path = os.path.join(run_dir, "state.json")
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def write_state(run_dir: str, state: dict) -> None:
    state_path = os.path.join(run_dir, "state.json")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def append_completed_step(state: dict, step: str) -> None:
    steps = list(state.get("completed_steps", []))
    if step not in steps:
        steps.append(step)
    state["completed_steps"] = steps


# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------


def plot_outputs_root(run_dir: str) -> str:
    return os.path.join(run_dir, "outputs")


def plot_debug_root(run_dir: str) -> str:
    return os.path.join(run_dir, "debug")


def plot_output_dir(run_dir: str, experiment: str) -> str:
    return os.path.join(plot_outputs_root(run_dir), experiment)


def plot_debug_dir(run_dir: str, experiment: str) -> str:
    return os.path.join(plot_debug_root(run_dir), experiment)


def plot_experiment_workspace(run_dir: str, experiment: str) -> str:
    """Per-experiment workspace under ``experiments/<name>/`` (code, critic, iter archives)."""
    return os.path.join(run_dir, "experiments", experiment)


def ensure_plot_run_layout(run_dir: str) -> None:
    os.makedirs(plot_outputs_root(run_dir), exist_ok=True)
    os.makedirs(plot_debug_root(run_dir), exist_ok=True)
    os.makedirs(os.path.join(run_dir, "experiments"), exist_ok=True)
    os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)


# ---------------------------------------------------------------------------
# Experiment discovery
# ---------------------------------------------------------------------------


def get_experiments(run_dir: str) -> list[str]:
    """Discover experiment names, preferring the manifest/index over legacy state."""
    manifest = read_manifest(run_dir)
    for stage_name in ("generate", "design", "explore"):
        stage = manifest.get("stages", {}).get(stage_name) or {}
        metadata = stage.get("metadata") or {}
        experiment_artifacts = metadata.get("experiment_artifacts") or {}
        if isinstance(experiment_artifacts, dict) and experiment_artifacts:
            return sorted(experiment_artifacts.keys())
        experiments = stage.get("experiments") or []
        if experiments:
            return sorted(experiments)

    state = read_state(run_dir)
    if state:
        if "per_experiment_specs" in state:
            return list(state["per_experiment_specs"].keys())
        if "per_experiment_routes" in state:
            return list(state["per_experiment_routes"].keys())
        if "experiments" in state:
            return list(state["experiments"])

    experiments: list[str] = []
    exp_root = os.path.join(run_dir, "experiments")
    if os.path.isdir(exp_root):
        for entry in os.scandir(exp_root):
            if not entry.is_dir():
                continue
            for name in ("styled_spec.md", "styled_figure_spec.md"):
                if os.path.exists(os.path.join(entry.path, name)):
                    experiments.append(entry.name)
                    break

    outputs_dir = plot_outputs_root(run_dir)
    if os.path.isdir(outputs_dir):
        for entry in os.scandir(outputs_dir):
            if not entry.is_dir():
                continue
            for name in ("styled_spec.md", "styled_figure_spec.md"):
                if os.path.exists(os.path.join(entry.path, name)):
                    if entry.name not in experiments:
                        experiments.append(entry.name)
                    break
    return sorted(experiments)


def read_critic_result(run_dir: str, experiment: str) -> dict:
    for path in (
        # Primary: workspace (experiments/<exp>/)
        os.path.join(plot_experiment_workspace(run_dir, experiment), "critic_result.json"),
        # Backward compat: old runs wrote to outputs/<exp>/
        os.path.join(plot_output_dir(run_dir, experiment), "critic_result.json"),
    ):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    return {}


# ---------------------------------------------------------------------------
# Execution-result persistence for plot pipeline
# ---------------------------------------------------------------------------


def persist_plot_plan_state(run_dir: str, experiments: list[str], *, execution: str) -> None:
    state = read_state(run_dir)
    state["experiments"] = experiments
    state["execution"] = execution
    state["multi_figure_plan_path"] = os.path.join(run_dir, "multi_figure_plan.md")
    state["outputs_dir"] = plot_outputs_root(run_dir)
    state["debug_dir"] = plot_debug_root(run_dir)
    state["experiments_root"] = os.path.join(run_dir, "experiments")
    state["per_experiment_specs"] = {
        exp: os.path.join(plot_experiment_workspace(run_dir, exp), "styled_spec.md") for exp in experiments
    }
    state["experiment_artifacts"] = {
        exp: {
            "styled_spec_path": os.path.join(plot_experiment_workspace(run_dir, exp), "styled_spec.md"),
            "workspace_dir": plot_experiment_workspace(run_dir, exp),
            "output_dir": plot_output_dir(run_dir, exp),
            "debug_dir": plot_debug_dir(run_dir, exp),
        }
        for exp in experiments
    }
    append_completed_step(state, "figure_plan")
    write_state(run_dir, state)


def select_plot_figure(work_dir: str, result: dict) -> str:
    figure_path = str(result.get("figure_path") or "").strip()
    candidates: list[str] = []
    if figure_path:
        candidates.append(figure_path)
    candidates.extend(
        [
            os.path.join(work_dir, "figure.png"),
            *sorted(glob.glob(os.path.join(work_dir, "figure_iter*.png"))),
        ]
    )
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


def finalize_plot_experiment(run_dir: str, experiment: str, result: dict, *, work_dir: str) -> dict:
    import shutil

    output_dir = plot_output_dir(run_dir, experiment)
    debug_dir = plot_debug_dir(run_dir, experiment)
    workspace_dir = plot_experiment_workspace(run_dir, experiment)
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(debug_dir, exist_ok=True)
    os.makedirs(workspace_dir, exist_ok=True)

    # Promote final figure to outputs/<exp>/figure.png (the only file in output_dir)
    figure_src = select_plot_figure(work_dir, result)
    figure_dst = os.path.join(output_dir, "figure.png")
    if figure_src:
        shutil.copy2(figure_src, figure_dst)

    # Ensure figure_code.py is in workspace (experiments/<exp>/) — NOT in output_dir
    code_src = os.path.join(work_dir, "figure_code.py")
    ws_code = os.path.join(workspace_dir, "figure_code.py")
    if os.path.exists(code_src) and os.path.abspath(work_dir) != os.path.abspath(workspace_dir):
        shutil.copy2(code_src, ws_code)

    # Write critic result to workspace only — NOT to output_dir
    critic_workspace = os.path.join(workspace_dir, "critic_result.json")
    critic_payload = dict(result)
    if figure_src:
        critic_payload["figure_path"] = figure_dst
    critic_payload["debug_dir"] = debug_dir
    critic_payload["output_dir"] = output_dir
    critic_payload["workspace_dir"] = workspace_dir
    with open(critic_workspace, "w", encoding="utf-8") as f:
        json.dump(critic_payload, f, indent=2)

    score = (
        critic_payload.get("score") or critic_payload.get("overall_score") or critic_payload.get("total_score") or 0.0
    )
    raw_verdict = critic_payload.get("verdict")
    if raw_verdict is None:
        accept_flag = critic_payload.get("accept")
        if accept_flag is True:
            raw_verdict = "ACCEPT"
        elif accept_flag is False:
            raw_verdict = "REJECT"
        else:
            raw_verdict = "UNKNOWN"

    spec_path = os.path.join(workspace_dir, "styled_spec.md")
    legacy_spec = os.path.join(output_dir, "styled_figure_spec.md")
    if not os.path.exists(spec_path) and os.path.exists(legacy_spec):
        spec_path = legacy_spec

    code_path_final = ws_code if os.path.exists(ws_code) else ""

    summary = {
        "experiment": experiment,
        "score": float(score),
        "verdict": raw_verdict,
        "iterations": critic_payload.get("iteration", 0),
        "figure_path": figure_dst if figure_src else "",
        "figure_code_path": code_path_final,
        "critic_result_path": critic_workspace,
        "styled_spec_path": spec_path if os.path.exists(spec_path) else "",
        "workspace_dir": workspace_dir,
        "output_dir": output_dir,
        "debug_dir": debug_dir,
    }
    return summary


# ---------------------------------------------------------------------------
# Run manifest (run_manifest.json) — stage-oriented index
# ---------------------------------------------------------------------------

_MANIFEST_FILE = "run_manifest.json"
_MANIFEST_SCHEMA_VERSION = 1


def read_manifest(run_dir: str) -> dict:
    """Read the full run manifest.  Returns empty structure if absent."""
    path = os.path.join(run_dir, _MANIFEST_FILE)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"schema_version": _MANIFEST_SCHEMA_VERSION, "mode": "", "stages": {}}


def read_manifest_stage(run_dir: str, stage: str) -> StageRecord | None:
    """Read a single stage entry.  Returns ``None`` if not present."""
    data = read_manifest(run_dir).get("stages", {}).get(stage)
    if data is None:
        return None
    return StageRecord.from_dict(data)


def write_manifest_stage(
    run_dir: str,
    stage: str,
    record: StageRecord,
    *,
    mode: str | None = None,
) -> None:
    """Atomically write/replace a stage entry in ``run_manifest.json``.

    Uses write-to-temp + ``os.replace`` so concurrent writers never
    produce a truncated file.
    """
    from pipeline.orchestrator import artifacts as orch_art

    os.makedirs(run_dir, exist_ok=True)
    manifest = read_manifest(run_dir)
    if mode:
        manifest["mode"] = mode
    manifest.setdefault("schema_version", _MANIFEST_SCHEMA_VERSION)
    manifest["artifact_layout_version"] = getattr(orch_art, "ARTIFACT_LAYOUT_VERSION", 1)
    manifest.setdefault("stages", {})[stage] = record.to_dict()
    tmp = os.path.join(run_dir, f".{_MANIFEST_FILE}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, os.path.join(run_dir, _MANIFEST_FILE))


# ---------------------------------------------------------------------------
# State persistence for plot pipeline
# ---------------------------------------------------------------------------


def persist_plot_execution_state(run_dir: str, execution: str, results: list[dict]) -> None:
    state = read_state(run_dir)
    per_experiment_results = {item["experiment"]: item for item in results}
    figure_paths = [item["figure_path"] for item in results if item.get("figure_path")]
    all_accept = bool(results) and all("ACCEPT" in str(item.get("verdict", "")).upper() for item in results)
    state["execution"] = execution
    state["outputs_dir"] = plot_outputs_root(run_dir)
    state["debug_dir"] = plot_debug_root(run_dir)
    state["per_experiment_results"] = per_experiment_results
    state["figure_paths"] = figure_paths
    state["final_status"] = "completed" if all_accept else "completed_with_issues"
    append_completed_step(state, "figure_execute")
    append_completed_step(state, f"figure_execute_{execution}")
    write_state(run_dir, state)
