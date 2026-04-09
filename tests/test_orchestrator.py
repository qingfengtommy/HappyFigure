"""Tests for the orchestrator registry in orchestrator/__init__.py."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# AgentCommand dataclass
# ---------------------------------------------------------------------------


class TestAgentCommand:
    def test_defaults(self):
        from agents import AgentCommand

        ac = AgentCommand(cmd=["echo", "hi"])
        assert ac.cmd == ["echo", "hi"]
        assert ac.env is None
        assert ac.input_text is None
        assert ac.cwd is None
        assert ac.stream_format is None
        assert ac.use_pty is False
        assert ac.silent_stdout is False

    def test_with_all_fields(self):
        from agents import AgentCommand

        ac = AgentCommand(
            cmd=["claude", "--model", "opus"],
            env={"FOO": "bar"},
            input_text="prompt text",
            cwd="/tmp",
            stream_format="claude-stream-json",
            use_pty=True,
            silent_stdout=True,
            metadata={"step": "1"},
        )
        assert ac.env == {"FOO": "bar"}
        assert ac.use_pty is True


# ---------------------------------------------------------------------------
# OrchestratorBase
# ---------------------------------------------------------------------------


class TestOrchestratorBase:
    def test_cannot_instantiate_abstract(self):
        from agents import OrchestratorBase

        with pytest.raises(TypeError):
            OrchestratorBase({})

    def test_concrete_subclass(self):
        from agents import OrchestratorBase, AgentCommand

        class Dummy(OrchestratorBase):
            def setup(self, run_dir):
                self._run_dir = run_dir

            def build_agent_command(self, agent_name, prompt):
                return AgentCommand(cmd=["echo", agent_name])

        d = Dummy({"key": "val"})
        assert d.config == {"key": "val"}
        assert d.platform_name == "unknown"
        assert d.model_display == "unknown"

    def test_list_agents_returns_prompt_files(self):
        from agents import OrchestratorBase, AgentCommand, PROMPTS_DIR

        class Dummy(OrchestratorBase):
            def setup(self, run_dir):
                pass
            def build_agent_command(self, agent_name, prompt):
                return AgentCommand(cmd=[])

        d = Dummy({})
        agents = d.list_agents()
        # Should find the shared agent prompts
        assert isinstance(agents, list)
        if PROMPTS_DIR.exists():
            assert "exp-explore" in agents
            assert "code-agent" in agents

    def test_get_agent_prompt_reads_file(self):
        from agents import OrchestratorBase, AgentCommand, PROMPTS_DIR

        class Dummy(OrchestratorBase):
            def setup(self, run_dir):
                pass
            def build_agent_command(self, agent_name, prompt):
                return AgentCommand(cmd=[])

        d = Dummy({})
        if (PROMPTS_DIR / "exp-explore.md").exists():
            text = d.get_agent_prompt("exp-explore")
            assert len(text) > 0

    def test_get_agent_prompt_missing_raises(self):
        from agents import OrchestratorBase, AgentCommand

        class Dummy(OrchestratorBase):
            def setup(self, run_dir):
                pass
            def build_agent_command(self, agent_name, prompt):
                return AgentCommand(cmd=[])

        d = Dummy({})
        with pytest.raises(FileNotFoundError, match="Agent prompt not found"):
            d.get_agent_prompt("nonexistent_agent_xyz")

    def test_cleanup_removes_platform_dir(self, tmp_path):
        from agents import OrchestratorBase, AgentCommand

        class Dummy(OrchestratorBase):
            def setup(self, run_dir):
                self._run_dir = run_dir
            def build_agent_command(self, agent_name, prompt):
                return AgentCommand(cmd=[])

        d = Dummy({})
        d.setup(str(tmp_path))
        platform_dir = tmp_path / ".platform"
        platform_dir.mkdir()
        (platform_dir / "config.json").write_text("{}")

        d.cleanup()
        assert not platform_dir.exists()


# ---------------------------------------------------------------------------
# Platform registry
# ---------------------------------------------------------------------------


class TestPlatformRegistry:
    def test_register_and_create(self):
        from agents import register_platform, create_orchestrator
        from agents import OrchestratorBase, AgentCommand

        @register_platform("test_plat")
        class TestPlat(OrchestratorBase):
            def setup(self, run_dir):
                pass
            def build_agent_command(self, agent_name, prompt):
                return AgentCommand(cmd=["test"])

        # Mark as loaded to prevent re-import
        import agents
        agents._loaded = True

        orch = create_orchestrator("test_plat", {"x": 1})
        assert orch.platform_name == "test_plat"
        assert orch.config == {"x": 1}

    def test_create_unknown_platform_raises(self):
        import agents

        agents._loaded = True  # prevent lazy load

        with pytest.raises(ValueError, match="Unknown platform"):
            agents.create_orchestrator("nonexistent_platform")

    def test_list_platforms_triggers_load(self):
        import agents

        with patch("importlib.import_module", side_effect=ImportError("nope")):
            result = agents.list_platforms()

        assert result == []
        assert agents._loaded is True

    def test_lazy_loading_only_runs_once(self):
        import agents

        with patch("importlib.import_module", side_effect=ImportError("nope")):
            agents._ensure_loaded()

        agents._PLATFORMS["sentinel"] = "fake"
        agents._ensure_loaded()  # should not re-import
        assert "sentinel" in agents._PLATFORMS

    def test_register_platform_sets_registered_name(self):
        from agents import register_platform, OrchestratorBase, AgentCommand

        @register_platform("my_plat")
        class MyPlat(OrchestratorBase):
            def setup(self, run_dir):
                pass
            def build_agent_command(self, agent_name, prompt):
                return AgentCommand(cmd=[])

        assert hasattr(MyPlat, "_registered_name")
        assert MyPlat._registered_name == "my_plat"


# ---------------------------------------------------------------------------
# Pipeline steps: _ensure_session_proposal
# ---------------------------------------------------------------------------


class TestEnsureSessionProposal:
    """_ensure_session_proposal copies files as-is for agent-first mode."""

    def test_single_md_copied_to_run_dir(self, tmp_path):
        import argparse
        from pipeline.orchestrator.steps import _ensure_session_proposal

        proposal = tmp_path / "paper.md"
        proposal.write_text("# My Paper\n")
        run_dir = str(tmp_path / "run")
        (tmp_path / "run").mkdir()

        args = argparse.Namespace(proposal=str(proposal), _proposal_dir=None)
        result = _ensure_session_proposal(run_dir, args)
        # Should be a copy in run_dir, not the original
        assert result == str(tmp_path / "run" / "paper.md")
        assert open(result).read() == "# My Paper\n"

    def test_single_pdf_copied_to_run_dir(self, tmp_path):
        import argparse
        from pipeline.orchestrator.steps import _ensure_session_proposal

        proposal = tmp_path / "paper.pdf"
        proposal.write_bytes(b"%PDF-1.4 fake content")
        run_dir = str(tmp_path / "run")
        (tmp_path / "run").mkdir()

        args = argparse.Namespace(proposal=str(proposal), _proposal_dir=None)
        result = _ensure_session_proposal(run_dir, args)
        assert result == str(tmp_path / "run" / "paper.pdf")
        assert open(result, "rb").read() == b"%PDF-1.4 fake content"

    def test_single_tex_copied_to_run_dir(self, tmp_path):
        import argparse
        from pipeline.orchestrator.steps import _ensure_session_proposal

        proposal = tmp_path / "main.tex"
        proposal.write_text(r"\documentclass{article}")
        run_dir = str(tmp_path / "run")
        (tmp_path / "run").mkdir()

        args = argparse.Namespace(proposal=str(proposal), _proposal_dir=None)
        result = _ensure_session_proposal(run_dir, args)
        assert result == str(tmp_path / "run" / "main.tex")

    def test_dir_proposal_copies_all_files(self, tmp_path):
        import argparse
        from pipeline.orchestrator.steps import _ensure_session_proposal

        proposal_dir = tmp_path / "docs"
        proposal_dir.mkdir()
        (proposal_dir / "intro.md").write_text("# Intro\nHello\n")
        (proposal_dir / "methods.tex").write_text(r"\section{Methods}")
        run_dir = str(tmp_path / "run")
        (tmp_path / "run").mkdir()

        args = argparse.Namespace(proposal=str(proposal_dir), _proposal_dir=str(proposal_dir))
        result = _ensure_session_proposal(run_dir, args)

        # Returns the proposal/ subdirectory in run_dir
        assert result.endswith("proposal")
        assert os.path.isdir(result)
        copied = {f.name for f in Path(result).iterdir()}
        assert copied == {"intro.md", "methods.tex"}

    def test_dir_proposal_no_files_generates_placeholder(self, tmp_path):
        import argparse
        from pipeline.orchestrator.steps import _ensure_session_proposal

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        run_dir = str(tmp_path / "run")
        (tmp_path / "run").mkdir()

        args = argparse.Namespace(proposal=str(empty_dir), _proposal_dir=str(empty_dir))
        result = _ensure_session_proposal(run_dir, args)

        content = open(result).read()
        assert "No proposal files found" in content

    def test_dir_proposal_finds_one_level_deep(self, tmp_path):
        import argparse
        from pipeline.orchestrator.steps import _ensure_session_proposal

        proposal_dir = tmp_path / "project"
        proposal_dir.mkdir()
        subdir = proposal_dir / "sub"
        subdir.mkdir()
        (subdir / "deep.md").write_text("# Deep file\n")
        run_dir = str(tmp_path / "run")
        (tmp_path / "run").mkdir()

        args = argparse.Namespace(proposal=str(proposal_dir), _proposal_dir=str(proposal_dir))
        result = _ensure_session_proposal(run_dir, args)

        assert os.path.isdir(result)
        assert (Path(result) / "deep.md").exists()

    def test_no_proposal_generates_placeholder(self, tmp_path):
        import argparse
        from pipeline.orchestrator.steps import _ensure_session_proposal

        run_dir = str(tmp_path / "run")
        (tmp_path / "run").mkdir()

        args = argparse.Namespace(proposal=None, _proposal_dir=None)
        result = _ensure_session_proposal(run_dir, args)

        content = open(result).read()
        assert "Autogenerated placeholder" in content


# ---------------------------------------------------------------------------
# Pipeline steps: _scan_results_dir
# ---------------------------------------------------------------------------


class TestScanResultsDir:
    def test_single_dir(self, tmp_path):
        from pipeline.orchestrator.steps import _scan_results_dir

        (tmp_path / "exp1").mkdir()
        (tmp_path / "exp2").mkdir()
        (tmp_path / "file.csv").write_text("data")

        count, subdirs = _scan_results_dir(str(tmp_path))
        assert count >= 1
        assert len(subdirs) == 2
        # Subdirs should be full absolute paths
        assert all(str(tmp_path) in s for s in subdirs)

    def test_comma_separated_dirs(self, tmp_path):
        from pipeline.orchestrator.steps import _scan_results_dir

        d1 = tmp_path / "results1"
        d2 = tmp_path / "results2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "exp_a").mkdir()
        (d1 / "data.csv").write_text("1")
        (d2 / "exp_b").mkdir()
        (d2 / "data.csv").write_text("2")

        count, subdirs = _scan_results_dir(f"{d1},{d2}")
        assert count == 2  # two data.csv files
        assert len(subdirs) == 2
        subdir_names = [s.split("/")[-1] for s in subdirs]
        assert "exp_a" in subdir_names
        assert "exp_b" in subdir_names

    def test_empty_string(self):
        from pipeline.orchestrator.steps import _scan_results_dir

        count, subdirs = _scan_results_dir("")
        assert count == 0
        assert subdirs == []

    def test_nonexistent_dir_skipped(self, tmp_path):
        from pipeline.orchestrator.steps import _scan_results_dir

        real = tmp_path / "real"
        real.mkdir()
        (real / "sub").mkdir()
        (real / "f.txt").write_text("x")

        count, subdirs = _scan_results_dir(f"{real},/nonexistent/path")
        assert count == 1
        assert len(subdirs) == 1

    def test_hidden_dirs_excluded(self, tmp_path):
        from pipeline.orchestrator.steps import _scan_results_dir

        (tmp_path / "visible").mkdir()
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "f.txt").write_text("x")

        count, subdirs = _scan_results_dir(str(tmp_path))
        assert len(subdirs) == 1
        assert subdirs[0].endswith("visible")
