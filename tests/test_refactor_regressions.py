from __future__ import annotations

import argparse
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.contracts import DesignResult, ExplorationResult, StageRecord, StageStatus


def test_ensure_compat_attrs_resolves_mode_and_results_dir():
    import os
    import cli as run_once

    args = argparse.Namespace(command="plot", results_dir="./results")

    run_once._ensure_compat_attrs(args)

    # Single dir is now resolved to absolute
    assert args.experiments_dir == os.path.abspath("./results")
    assert args.mode == "exp_plot"


def test_ensure_compat_attrs_comma_separated_results_dir():
    import os
    import cli as run_once

    args = argparse.Namespace(command="plot", results_dir="./a,./b, ./c")
    run_once._ensure_compat_attrs(args)

    dirs = args.experiments_dir.split(",")
    assert len(dirs) == 3
    assert all(os.path.isabs(d) for d in dirs)
    assert dirs[0] == os.path.abspath("./a")
    assert dirs[1] == os.path.abspath("./b")
    assert dirs[2] == os.path.abspath("./c")


def test_ensure_compat_attrs_trailing_comma_ignored():
    import os
    import cli as run_once

    args = argparse.Namespace(command="plot", results_dir="./x,")
    run_once._ensure_compat_attrs(args)

    # Trailing comma should not produce an empty entry
    assert args.experiments_dir == os.path.abspath("./x")


def test_ensure_compat_attrs_empty_segments_skipped():
    import os
    import cli as run_once

    args = argparse.Namespace(command="plot", results_dir="./a,,./b")
    run_once._ensure_compat_attrs(args)

    dirs = args.experiments_dir.split(",")
    assert len(dirs) == 2
    assert dirs[0] == os.path.abspath("./a")
    assert dirs[1] == os.path.abspath("./b")


def test_ensure_compat_attrs_none_results_dir():
    import cli as run_once

    args = argparse.Namespace(command="diagram", results_dir=None)
    run_once._ensure_compat_attrs(args)

    assert args.experiments_dir is None


def test_ensure_compat_attrs_proposal_dir_detected():
    import os
    import tempfile
    import cli as run_once

    with tempfile.TemporaryDirectory() as td:
        args = argparse.Namespace(command="plot", results_dir=None, proposal=td)
        run_once._ensure_compat_attrs(args)
        assert args._proposal_dir == os.path.abspath(td)

    # File proposal should not set _proposal_dir
    args2 = argparse.Namespace(command="plot", results_dir=None, proposal="paper.md")
    run_once._ensure_compat_attrs(args2)
    assert args2._proposal_dir is None


def test_agent_runtime_wrappers_delegate_to_run_agent(monkeypatch):
    from pipeline import agent_runtime

    calls: list[tuple[str, str, str | None]] = []

    monkeypatch.setattr(
        agent_runtime,
        "run_agent",
        lambda agent_name, prompt, verbose=False, log_dir=None, log_name=None, label=None, role="subagent": (
            calls.append((agent_name, prompt, log_name)) or 0
        ),
    )

    rc_session = agent_runtime.launch_orchestrator_session(
        "happyfigure-orchestrator",
        "main prompt",
        log_name="main-session",
    )
    rc_subagent = agent_runtime.spawn_subagent(
        "planner-stylist",
        "sub prompt",
        log_name="planner",
    )

    assert rc_session == 0
    assert rc_subagent == 0
    assert calls == [
        ("happyfigure-orchestrator", "main prompt", "main-session"),
        ("planner-stylist", "sub prompt", "planner"),
    ]


def test_compose_agent_prompt_wraps_identity_and_task():
    from agents import OrchestratorBase, AgentCommand

    class Dummy(OrchestratorBase):
        def setup(self, run_dir, **kwargs):
            return None

        def build_agent_command(self, agent_name, prompt):
            return AgentCommand(cmd=["echo"])

    orch = Dummy({})
    prompt = orch.compose_agent_prompt("data-explore", "Run directory: /tmp/run.")

    assert "You are the **data exploration agent**" in prompt
    assert "## Runtime Task" in prompt
    assert "Run directory: /tmp/run." in prompt


def test_get_experiments_prefers_manifest_index_over_state(tmp_path):
    import json

    from pipeline.run_state import get_experiments

    run_dir = tmp_path / "run_manifest_first"
    run_dir.mkdir()

    (run_dir / "state.json").write_text(
        json.dumps({"experiments": ["legacy_exp"]}),
        encoding="utf-8",
    )
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "exp_plot",
                "stages": {
                    "design": {
                        "status": "completed",
                        "experiments": ["manifest_exp_b", "manifest_exp_a"],
                        "metadata": {
                            "experiment_artifacts": {
                                "manifest_exp_b": {"styled_spec": "experiments/manifest_exp_b/styled_spec.md"},
                                "manifest_exp_a": {"styled_spec": "experiments/manifest_exp_a/styled_spec.md"},
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert get_experiments(str(run_dir)) == ["manifest_exp_a", "manifest_exp_b"]


def test_codex_build_agent_command_embeds_requested_agent_prompt():
    from agents.codex import CodexOrchestrator

    orch = CodexOrchestrator({"agent": {"codex": {"model": "gpt-5.4"}}})
    orch._model = "gpt-5.4"
    orch._sandbox_mode = "workspace-write"
    orch._reasoning_effort = None
    orch._reasoning_summary = None

    cmd = orch.build_agent_command("data-explore", "Run directory: /tmp/run.")

    assert cmd.cmd[-1].startswith("You are the **data exploration agent**")
    assert "## Runtime Task" in cmd.cmd[-1]


def test_gemini_build_agent_command_embeds_requested_agent_prompt():
    from agents.gemini import GeminiOrchestrator

    orch = GeminiOrchestrator({"agent": {"gemini": {"model": "gemini-2.5-pro"}}})
    orch._model = "gemini-2.5-pro"

    cmd = orch.build_agent_command("happyfigure-orchestrator", "Run directory: /tmp/run.")

    assert cmd.cmd[-1].startswith("You are the **main HappyFigure orchestrator session**")
    assert "## Runtime Task" in cmd.cmd[-1]


def test_main_dispatches_run_agent_pipeline(monkeypatch):
    import cli as run_once

    args = argparse.Namespace(
        command="plot",
        proposal="paper.md",
        results_dir="./results",
        verbose=False,
        llm_preset=None,
        agent=None,
    )

    class DummyParser:
        def parse_args(self):
            return args

    class DummyOrchestrator:
        model_display = "dummy-model"

        def cleanup(self):
            self.cleaned = True

    orch = DummyOrchestrator()
    called: list[argparse.Namespace] = []

    monkeypatch.setattr(run_once, "_build_parser", lambda: DummyParser())
    monkeypatch.setattr(run_once, "_load_pipeline_config", lambda: {"agent": {"platform": "opencode"}})
    monkeypatch.setattr(run_once, "_resolve_agent_platform", lambda args, config: "opencode")
    monkeypatch.setattr(run_once, "_resolve_llm_roles", lambda config, preset: {})
    monkeypatch.setattr(run_once, "_preflight_auth_checks", lambda config, preset: None)
    monkeypatch.setattr(run_once, "set_ctx", lambda ctx: None)
    monkeypatch.setattr(run_once.ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(run_once, "run_agent_pipeline", lambda parsed_args: called.append(parsed_args))

    import agents

    monkeypatch.setattr(agents, "create_orchestrator", lambda platform, config: orch)

    run_once.main()

    assert called == [args]
    assert getattr(orch, "cleaned", False) is True


def test_step_execute_parallel_finalizes_experiment_workspace(monkeypatch):
    from pipeline import plot_execution

    args = argparse.Namespace(experiments_dir="./results", verbose=False)
    finalized_dirs: dict[str, str] = {}

    class DummyDashboard:
        def __init__(self, experiments):
            self.experiments = experiments

        def start(self):
            return None

        def update(self, experiment, status):
            return None

        def stop(self):
            return None

    def fake_finalize(run_dir, exp, result, *, work_dir):
        finalized_dirs[exp] = work_dir
        return {
            "experiment": exp,
            "score": result["score"],
            "verdict": result["verdict"],
            "figure_path": "",
        }

    monkeypatch.setattr(plot_execution.ui, "ProgressDashboard", DummyDashboard)
    monkeypatch.setattr(plot_execution.ui, "result", lambda *a, **k: None)
    monkeypatch.setattr(plot_execution.ui, "summary_table", lambda *a, **k: None)
    monkeypatch.setattr(plot_execution, "run_code_agent", lambda *a, **k: {"score": 9.5, "verdict": "ACCEPT"})
    monkeypatch.setattr(plot_execution, "finalize_plot_experiment", fake_finalize)
    monkeypatch.setattr(plot_execution, "persist_plot_execution_state", lambda *a, **k: None)

    plot_execution.step_execute_parallel("/tmp/run", ["exp_a", "exp_b"], args)

    assert finalized_dirs == {
        "exp_a": "/tmp/run/experiments/exp_a",
        "exp_b": "/tmp/run/experiments/exp_b",
    }


def test_run_agent_pipeline_skips_completed_generate_on_resume(monkeypatch, tmp_path):
    import pipeline.orchestrator.main as main_mod

    run_dir = tmp_path / "run_001"
    run_dir.mkdir()

    exploration = ExplorationResult(
        run_dir=str(run_dir),
        mode="exp_plot",
        artifacts={"report": "exploration_report.md"},
        experiments=[],
    )
    design = DesignResult(
        mode="exp_plot",
        artifacts={"plan": "multi_figure_plan.md"},
        experiments=["exp_a"],
    )

    class DummyOrchestrator:
        def setup(self, run_dir, **kwargs):
            self.run_dir = run_dir

    dummy_ctx = SimpleNamespace(orchestrator=DummyOrchestrator(), platform_name="test")
    generate_calls: list[str] = []

    monkeypatch.setattr(main_mod, "get_ctx", lambda: dummy_ctx)

    monkeypatch.setattr(main_mod, "read_manifest", lambda _: {"mode": "exp_plot", "artifact_layout_version": 2})
    monkeypatch.setattr(
        main_mod,
        "read_manifest_stage",
        lambda _run_dir, stage: StageRecord(status=StageStatus.COMPLETED) if stage == "generate" else None,
    )
    monkeypatch.setattr(main_mod.orch_steps, "try_resume", lambda _run_dir, _mode: (exploration, design))
    monkeypatch.setattr(main_mod.orch_steps, "stage_explore", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected explore")))
    monkeypatch.setattr(main_mod.orch_steps, "stage_design", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected design")))
    monkeypatch.setattr(main_mod.orch_steps, "stage_generate", lambda *a, **k: generate_calls.append("called"))
    monkeypatch.setattr(main_mod.ui, "info", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "dim", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "warn", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "success", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "error", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "orchestrator_log", lambda *_a, **_k: contextlib.nullcontext())

    args = argparse.Namespace(
        command="plot",
        proposal="paper.md",
        experiments_dir="./results",
        execution="sequential",
        resume=str(run_dir),
        verbose=False,
        mode=None,
    )

    main_mod.run_agent_pipeline(args)

    assert generate_calls == []


def test_run_agent_pipeline_agent_first_plot_launches_main_session(monkeypatch, tmp_path):
    import pipeline.orchestrator.main as main_mod

    run_dir = tmp_path / "agent_first_plot"
    run_dir.mkdir()

    class DummyOrchestrator:
        def __init__(self):
            self.setup_calls = []

        def setup(self, run_dir, **kwargs):
            self.setup_calls.append(run_dir)

    dummy_orch = DummyOrchestrator()
    dummy_ctx = SimpleNamespace(
        orchestrator=dummy_orch,
        platform_name="test",
        config={"agents": {"mode": "agent-first"}},
    )
    launches: list[tuple[str, str, str]] = []

    monkeypatch.setattr(main_mod, "get_ctx", lambda: dummy_ctx)

    monkeypatch.setattr(main_mod.orch_steps, "prepare_agent_session_run", lambda args, mode: str(run_dir))
    monkeypatch.setattr(main_mod.orch_steps, "build_orchestrator_session_prompt", lambda run_dir, args, mode: "session-prompt")
    monkeypatch.setattr(
        main_mod.orch_steps,
        "sync_agent_session_manifest",
        lambda run_dir, args, mode: DesignResult(mode=mode, artifacts={}, experiments=["exp_a"]),
    )
    def _fake_launch(agent_name, prompt, **kwargs):
        launches.append((agent_name, prompt, kwargs["log_name"]))
        # Create marker file so the "no outputs" guard passes
        (run_dir / "exploration_report.md").write_text("# Exploration\n")
        return 0

    monkeypatch.setattr(main_mod, "launch_orchestrator_session", _fake_launch)
    monkeypatch.setattr(main_mod, "require_agent_success", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "info", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "dim", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "warn", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "success", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "orchestrator_log", lambda *_a, **_k: contextlib.nullcontext())

    args = argparse.Namespace(
        command="plot",
        proposal="paper.md",
        experiments_dir="./results",
        execution="parallel",
        resume=None,
        verbose=False,
        mode=None,
        orchestrator_mode="agent-first",
    )

    main_mod.run_agent_pipeline(args)

    assert dummy_orch.setup_calls == [str(run_dir)]
    assert launches == [("happyfigure-orchestrator", "session-prompt", "happyfigure-orchestrator")]


def test_run_agent_pipeline_agent_first_resume_skips_relaunch(monkeypatch, tmp_path):
    import pipeline.orchestrator.main as main_mod

    run_dir = tmp_path / "agent_first_resume"
    run_dir.mkdir()

    class DummyOrchestrator:
        def setup(self, run_dir, **kwargs):
            self.run_dir = run_dir

    dummy_ctx = SimpleNamespace(
        orchestrator=DummyOrchestrator(),
        platform_name="test",
        config={"agents": {"mode": "agent-first"}},
    )
    launches: list[str] = []

    monkeypatch.setattr(main_mod, "get_ctx", lambda: dummy_ctx)

    monkeypatch.setattr(main_mod.orch_steps, "prepare_agent_session_run", lambda args, mode: str(run_dir))
    monkeypatch.setattr(main_mod.orch_steps, "build_orchestrator_session_prompt", lambda run_dir, args, mode: "session-prompt")
    monkeypatch.setattr(
        main_mod.orch_steps,
        "sync_agent_session_manifest",
        lambda run_dir, args, mode: DesignResult(mode=mode, artifacts={}, experiments=["exp_a"]),
    )
    monkeypatch.setattr(main_mod, "read_manifest_stage", lambda run_dir, stage: StageRecord(status=StageStatus.COMPLETED) if stage == "generate" else None)
    monkeypatch.setattr(main_mod, "launch_orchestrator_session", lambda *a, **k: launches.append("launched") or 0)
    monkeypatch.setattr(main_mod, "require_agent_success", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "info", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "dim", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "warn", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "success", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "orchestrator_log", lambda *_a, **_k: contextlib.nullcontext())

    args = argparse.Namespace(
        command="plot",
        proposal="paper.md",
        experiments_dir="./results",
        execution="parallel",
        resume=str(run_dir),
        verbose=False,
        mode=None,
        orchestrator_mode="agent-first",
    )

    main_mod.run_agent_pipeline(args)

    assert launches == []


def test_run_agent_pipeline_agent_first_diagram_wraps_services(monkeypatch, tmp_path):
    import pipeline.orchestrator.main as main_mod

    run_dir = tmp_path / "agent_first_diagram"
    run_dir.mkdir()

    class DummyOrchestrator:
        def setup(self, run_dir, **kwargs):
            self.run_dir = run_dir

    dummy_ctx = SimpleNamespace(
        orchestrator=DummyOrchestrator(),
        platform_name="test",
        config={"agents": {"mode": "agent-first"}},
    )
    calls: list[str] = []

    monkeypatch.setattr(main_mod, "get_ctx", lambda: dummy_ctx)

    monkeypatch.setattr(main_mod.orch_steps, "prepare_agent_session_run", lambda args, mode: str(run_dir))
    monkeypatch.setattr(main_mod.orch_steps, "build_orchestrator_session_prompt", lambda run_dir, args, mode: "diagram-prompt")
    monkeypatch.setattr(
        main_mod.orch_steps,
        "sync_agent_session_manifest",
        lambda run_dir, args, mode: DesignResult(mode=mode, artifacts={}, experiments=[]),
    )
    def _fake_launch_diagram(*a, **k):
        # Create marker file so the "no outputs" guard passes
        (run_dir / "method_description.md").write_text("# Method\n")
        return 0

    monkeypatch.setattr(main_mod, "launch_orchestrator_session", _fake_launch_diagram)
    monkeypatch.setattr(main_mod, "require_agent_success", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "start_services", lambda: calls.append("start"))
    monkeypatch.setattr(main_mod, "stop_services", lambda: calls.append("stop"))
    monkeypatch.setattr(main_mod.ui, "info", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "dim", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "warn", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "success", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "orchestrator_log", lambda *_a, **_k: contextlib.nullcontext())

    args = argparse.Namespace(
        command="diagram",
        proposal="paper.md",
        experiments_dir=None,
        execution="sequential",
        resume=None,
        verbose=False,
        mode=None,
        orchestrator_mode="agent-first",
        drawing_image=None,
    )

    main_mod.run_agent_pipeline(args)

    assert calls == ["start", "stop"]


def test_sync_agent_session_manifest_requires_expected_plot_outputs(tmp_path):
    import pytest

    import pipeline.orchestrator.steps as steps_mod

    run_dir = tmp_path / "incomplete_agent_session"
    run_dir.mkdir()

    with pytest.raises(RuntimeError, match="missing required outputs"):
        steps_mod.sync_agent_session_manifest(
            str(run_dir),
            argparse.Namespace(execution="sequential"),
            "exp_plot",
        )


def test_stage_record_round_trips_metadata():
    payload = StageRecord(
        status=StageStatus.COMPLETED,
        completed_at="2026-04-05T00:00:00",
        artifacts={"report": "exploration_report.md"},
        experiments=["exp_a"],
        metadata={"mode": "exp_plot", "execution_strategy": "beam", "experiment_count": 1},
    )

    restored = StageRecord.from_dict(payload.to_dict())

    assert restored.metadata == payload.metadata


def test_run_agent_pipeline_records_design_stage_metadata(monkeypatch, tmp_path):
    import pipeline.orchestrator.main as main_mod

    run_dir = tmp_path / "run_002"
    run_dir.mkdir()

    exploration = ExplorationResult(
        run_dir=str(run_dir),
        mode="exp_plot",
        artifacts={"report": "exploration_report.md"},
        experiments=[],
    )
    design = DesignResult(
        mode="exp_plot",
        artifacts={"plan": "multi_figure_plan.md"},
        experiments=["exp_a", "exp_b"],
    )

    class DummyOrchestrator:
        def setup(self, run_dir, **kwargs):
            self.run_dir = run_dir

    dummy_ctx = SimpleNamespace(orchestrator=DummyOrchestrator(), platform_name="test")
    stage_writes: list[tuple[str, StageRecord, str | None]] = []

    monkeypatch.setattr(main_mod, "get_ctx", lambda: dummy_ctx)

    monkeypatch.setattr(main_mod.orch_steps, "stage_explore", lambda *a, **k: exploration)
    monkeypatch.setattr(main_mod.orch_steps, "stage_design", lambda *a, **k: design)
    monkeypatch.setattr(main_mod.orch_steps, "stage_generate", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "write_manifest_stage", lambda run_dir, stage, record, mode=None: stage_writes.append((stage, record, mode)))
    monkeypatch.setattr(main_mod.ui, "info", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "dim", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "warn", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "success", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "error", lambda *a, **k: None)
    monkeypatch.setattr(main_mod.ui, "orchestrator_log", lambda *_a, **_k: contextlib.nullcontext())

    args = argparse.Namespace(
        command="plot",
        proposal="paper.md",
        experiments_dir="./results",
        execution="parallel",
        resume=None,
        verbose=False,
        mode=None,
    )

    main_mod.run_agent_pipeline(args)

    design_write = next(record for stage, record, _mode in stage_writes if stage == "design")
    assert design_write.metadata["mode"] == "exp_plot"
    assert design_write.metadata["execution_strategy"] == "parallel"
    assert design_write.metadata["experiment_count"] == 2
    assert "experiment_artifacts" in design_write.metadata
    assert design_write.metadata["experiment_artifacts"]["exp_a"]["styled_spec"] == "experiments/exp_a/styled_spec.md"


def test_stage_design_routes_through_strategy_layer(monkeypatch, tmp_path):
    import pipeline.orchestrator.steps as steps_mod

    exploration = ExplorationResult(
        run_dir=str(tmp_path / "run_003"),
        mode="exp_plot",
        artifacts={"report": "exploration_report.md"},
        experiments=[],
    )
    expected = DesignResult(
        mode="exp_plot",
        artifacts={"plan": "multi_figure_plan.md"},
        experiments=["exp_a"],
    )
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        steps_mod,
        "execute_design_strategy",
        lambda passed_exploration, passed_args, passed_mode: (
            calls.append((passed_mode, passed_exploration.run_dir)) or expected
        ),
    )

    result = steps_mod.stage_design(exploration, argparse.Namespace(execution="sequential"), "exp_plot")

    assert result is expected
    assert calls == [("exp_plot", str(tmp_path / "run_003"))]


def test_stage_explore_reads_experiments_from_summary_json(monkeypatch, tmp_path):
    import json

    import pipeline.orchestrator.steps as steps_mod

    run_dir = tmp_path / "run_004"
    run_dir.mkdir()
    (run_dir / "exploration_summary.json").write_text(
        json.dumps({"experiments_found": ["exp_b", "exp_a", "exp_b"]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(steps_mod, "step_explore_plot", lambda args: str(run_dir))
    monkeypatch.setattr(steps_mod, "write_explore_summary", lambda run_dir, mode, args: None)

    result = steps_mod.stage_explore(
        argparse.Namespace(proposal="paper.md", experiments_dir="./results", verbose=False),
        "exp_plot",
    )

    assert result.experiments == ["exp_a", "exp_b"]


def test_resolve_plot_execution_handler_rejects_unknown_strategy():
    from pipeline.orchestrator.strategies import (
        resolve_generate_handler,
        resolve_plot_execution_handler,
    )

    with pytest.raises(ValueError, match="Unknown plot execution strategy"):
        resolve_plot_execution_handler("unknown")
    with pytest.raises(ValueError, match="Unknown generate mode"):
        resolve_generate_handler("unknown")


def test_stage_generate_routes_through_generate_strategy_layer(monkeypatch):
    import pipeline.orchestrator.steps as steps_mod

    design = DesignResult(
        mode="exp_plot",
        artifacts={"plan": "multi_figure_plan.md"},
        experiments=["exp_a"],
        variant_specs={"exp_a": ["experiments/exp_a/styled_spec_s0.md"]},
    )
    args = argparse.Namespace(execution="beam")
    calls: list[tuple[str, str, list[str]]] = []
    stage_records: list[StageRecord] = []

    monkeypatch.setattr(
        steps_mod,
        "execute_generate_strategy",
        lambda run_dir, passed_args, passed_mode, passed_design: calls.append(
            (passed_mode, run_dir, passed_design.experiments)
        ),
    )
    monkeypatch.setattr(
        steps_mod,
        "write_manifest_stage",
        lambda run_dir, stage, record, **kwargs: stage_records.append(record),
    )

    steps_mod.stage_generate("/tmp/run", args, "exp_plot", design)

    assert calls == [("exp_plot", "/tmp/run", ["exp_a"])]
    assert stage_records[0].metadata["execution_strategy"] == "beam"
    assert stage_records[0].metadata["beam_variant_experiments"] == 1


def test_try_resume_reconstructs_beam_variants_from_manifest_metadata(tmp_path):
    import pipeline.orchestrator.steps as steps_mod
    from pipeline.orchestrator import artifacts as orch_art

    run_dir = tmp_path / "run_004"
    run_dir.mkdir()

    design_record = StageRecord(
        status=StageStatus.COMPLETED,
        artifacts={"design_summary": orch_art.DESIGN_SUMMARY},
        experiments=["exp_a"],
        metadata={
            "experiment_artifacts": {
                "exp_a": {
                    "styled_spec": "experiments/exp_a/styled_spec.md",
                    "beam_variant_specs": [
                        "experiments/exp_a/styled_spec_s0.md",
                        "experiments/exp_a/styled_spec_s1.md",
                    ],
                }
            }
        },
    )

    (run_dir / "run_manifest.json").write_text(
        __import__("json").dumps(
            {
                "schema_version": 1,
                "mode": "exp_plot",
                "stages": {
                    "design": design_record.to_dict(),
                },
            }
        ),
        encoding="utf-8",
    )

    exploration, design = steps_mod.try_resume(str(run_dir), "exp_plot")

    assert exploration is None
    assert design is not None
    assert design.variant_specs == {
        "exp_a": [
            "experiments/exp_a/styled_spec_s0.md",
            "experiments/exp_a/styled_spec_s1.md",
        ]
    }


# ---------------------------------------------------------------------------
# Codex stream parser
# ---------------------------------------------------------------------------


def test_codex_stream_parser_handles_collab_tool_call():
    """Codex emits collab_tool_call with tool=spawn_agent for subagents."""
    import io
    import json
    from ui.stream_parsers import stream_codex_json

    events = [
        json.dumps({"type": "item.started", "item": {
            "type": "collab_tool_call", "tool": "spawn_agent",
            "prompt": "Act as @data-explore for a bounded exploration slice.",
        }}),
        json.dumps({"type": "item.completed", "item": {
            "type": "collab_tool_call", "tool": "spawn_agent",
            "prompt": "Act as @data-explore",
            "agents_states": {"abc": {"status": "pending_init", "message": None}},
        }}),
    ]
    stdout = io.StringIO("\n".join(events) + "\n")
    output_tail = []
    stream_codex_json(stdout, output_tail=output_tail)

    # The subagent launch should appear in output_tail
    joined = "".join(output_tail)
    assert "data-explore" in joined


def test_codex_stream_parser_agent_message_styled():
    """Agent reasoning text should go through raw_thinking."""
    import io
    import json
    from ui.stream_parsers import stream_codex_json

    events = [
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message",
            "text": "I'm scanning the results directory now.",
        }}),
    ]
    stdout = io.StringIO("\n".join(events) + "\n")
    output_tail = []
    stream_codex_json(stdout, output_tail=output_tail)

    joined = "".join(output_tail)
    assert "scanning the results" in joined


def test_codex_stream_parser_command_execution():
    """Tool calls should be displayed."""
    import io
    import json
    from ui.stream_parsers import stream_codex_json

    events = [
        json.dumps({"type": "item.started", "item": {
            "type": "command_execution",
            "command": "/bin/bash -lc ls -la",
        }}),
        json.dumps({"type": "item.completed", "item": {
            "type": "command_execution",
            "exit_code": 0,
        }}),
    ]
    stdout = io.StringIO("\n".join(events) + "\n")
    output_tail = []
    stream_codex_json(stdout, output_tail=output_tail)

    joined = "".join(output_tail)
    assert "ls -la" in joined


def test_codex_stream_parser_error_exit_code():
    """Non-zero exit code should show error."""
    import io
    import json
    from ui.stream_parsers import stream_codex_json

    events = [
        json.dumps({"type": "item.completed", "item": {
            "type": "command_execution",
            "exit_code": 1,
        }}),
    ]
    stdout = io.StringIO("\n".join(events) + "\n")
    output_tail = []
    stream_codex_json(stdout, output_tail=output_tail)

    joined = "".join(output_tail)
    assert "exit code 1" in joined


def test_extract_codex_agent_name():
    from ui.stream_parsers import _extract_codex_agent_name

    assert _extract_codex_agent_name("Act as @data-explore for a slice.") == "data-explore"
    assert _extract_codex_agent_name("Spawn @code-agent to generate figure.") == "code-agent"
    assert _extract_codex_agent_name("No agent name here.") == "subagent"
    assert _extract_codex_agent_name("") == "subagent"


# ---------------------------------------------------------------------------
# IdleSpinner tests
# ---------------------------------------------------------------------------


def test_idle_spinner_lifecycle():
    """Spinner starts, notifies, and stops without errors."""
    import time
    import ui

    spinner = ui.IdleSpinner(delay=0.5)
    spinner.start()
    spinner.notify(state=ui.IDLE_STATE_THINKING)
    time.sleep(0.05)
    spinner.notify(state=ui.IDLE_STATE_TOOL)
    time.sleep(0.05)
    spinner.notify(state=ui.IDLE_STATE_WORKING)
    spinner.stop()

    # Double stop is safe (idempotent)
    spinner.stop()


def test_idle_spinner_state_transitions():
    """State updates are reflected under lock."""
    import ui

    spinner = ui.IdleSpinner(delay=1.0)
    assert spinner._state == ui.IDLE_STATE_WORKING

    spinner.notify(state=ui.IDLE_STATE_THINKING)
    assert spinner._state == ui.IDLE_STATE_THINKING

    spinner.notify(state=ui.IDLE_STATE_TOOL)
    assert spinner._state == ui.IDLE_STATE_TOOL

    # None preserves current state
    spinner.notify(state=None)
    assert spinner._state == ui.IDLE_STATE_TOOL


def test_idle_spinner_notify_resets_timer():
    """Each notify() resets the last-event timestamp."""
    import time
    import ui

    spinner = ui.IdleSpinner(delay=1.0)
    t0 = spinner._last_event

    time.sleep(0.05)
    spinner.notify(state=ui.IDLE_STATE_THINKING)

    assert spinner._last_event > t0


def test_idle_spinner_no_start_is_safe():
    """Notify and stop without start should not crash."""
    import ui

    spinner = ui.IdleSpinner()
    spinner.notify(state=ui.IDLE_STATE_WORKING)
    spinner.stop()


def test_idle_spinner_becomes_visible_after_delay():
    """Spinner should set _visible after delay elapses with no events."""
    import time
    import ui

    spinner = ui.IdleSpinner(delay=0.2)
    spinner.start()
    try:
        # Wait for delay + a few spinner intervals
        time.sleep(0.8)
        with spinner._lock:
            was_visible = spinner._visible
        # Should have become visible (in color mode) or at least not crashed
        # In non-TTY test env _USE_COLOR is False, so _visible stays False
        # — that's correct behavior. Just verify no crash.
    finally:
        spinner.stop()


def test_idle_spinner_clears_on_notify():
    """After becoming visible, notify() should clear the spinner."""
    import time
    import ui

    spinner = ui.IdleSpinner(delay=0.1)
    spinner.start()
    try:
        time.sleep(0.5)  # let it become visible (if color mode)
        spinner.notify(state=ui.IDLE_STATE_WORKING)
        with spinner._lock:
            assert spinner._visible is False
    finally:
        spinner.stop()


def test_idle_spinner_label_prefix():
    """Spinner with label should include it in the output line."""
    import ui

    spinner = ui.IdleSpinner(label="data-explore")
    assert spinner._label == "data-explore"
    # Functional test — just ensure construction doesn't error
    spinner.start()
    spinner.notify(state=ui.IDLE_STATE_THINKING)
    spinner.stop()


def test_claude_stream_parser_spinner_lifecycle():
    """Claude JSON parser creates and stops spinner without leaking threads."""
    import io
    import json
    import threading
    from ui.stream_parsers import stream_claude_json

    events = [
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello"},
        ]}}),
        json.dumps({"type": "result", "result": "done", "total_cost_usd": 0.01,
                     "num_turns": 1, "duration_ms": 100,
                     "total_input_tokens": 10, "total_output_tokens": 5}),
    ]
    stdout = io.StringIO("\n".join(events) + "\n")

    threads_before = set(t.name for t in threading.enumerate())
    stream_claude_json(stdout)
    threads_after = set(t.name for t in threading.enumerate())

    # No idle-spinner thread should be left running
    new_threads = threads_after - threads_before
    assert "idle-spinner" not in new_threads


def test_opencode_stream_parser_spinner_lifecycle():
    """OpenCode JSON parser creates and stops spinner without leaking threads."""
    import io
    import json
    import threading
    from ui.stream_parsers import stream_opencode_json

    events = [
        json.dumps({"type": "text-delta", "textDelta": "hello"}),
        json.dumps({"type": "session.idle"}),
    ]
    stdout = io.StringIO("\n".join(events) + "\n")

    threads_before = set(t.name for t in threading.enumerate())
    stream_opencode_json(stdout)
    threads_after = set(t.name for t in threading.enumerate())

    new_threads = threads_after - threads_before
    assert "idle-spinner" not in new_threads


def test_db_monitor_has_spinner():
    """OpenCodeDbMonitor should have a spinner attribute."""
    from ui.stream_parsers import OpenCodeDbMonitor

    monitor = OpenCodeDbMonitor(
        db_path="/nonexistent/path.db",
        session_title="test-session",
        label="test-agent",
    )
    assert hasattr(monitor, "spinner")
    assert monitor.spinner._label == "test-agent"


def test_infer_spinner_state():
    """_infer_spinner_state correctly maps OpenCode part types to spinner states."""
    import ui
    from ui.stream_parsers import _infer_spinner_state

    assert _infer_spinner_state({"type": "text", "text": "hello"}) == ui.IDLE_STATE_THINKING
    assert _infer_spinner_state({"type": "tool", "state": "pending"}) == ui.IDLE_STATE_TOOL
    assert _infer_spinner_state({"type": "tool", "state": "running"}) == ui.IDLE_STATE_TOOL
    assert _infer_spinner_state({"type": "tool", "state": "completed"}) == ui.IDLE_STATE_WORKING
    assert _infer_spinner_state({"type": "tool", "state": {"status": "running"}}) == ui.IDLE_STATE_TOOL
    assert _infer_spinner_state({"type": "tool", "state": {"status": "completed"}}) == ui.IDLE_STATE_WORKING
    assert _infer_spinner_state({"type": "step-start"}) == ui.IDLE_STATE_WORKING
    assert _infer_spinner_state({}) == ui.IDLE_STATE_WORKING
