"""Tests for scripts/evaluate_pipeline.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from scripts import evaluate_pipeline as ep


class TestCheckAuth:
    def test_uses_orchestrator_check_auth_result(self, sample_pipeline_config):
        """Evaluator auth checks should defer to the orchestrator implementation."""
        fake_orch = MagicMock()
        fake_orch.check_auth.return_value = {
            "ok": True,
            "message": "Codex CLI ready (auth managed by Codex login or environment)",
            "error": None,
        }

        with (
            patch.object(ep, "_check_agent_available", return_value=True),
            patch("agents.create_orchestrator", return_value=fake_orch) as mock_create,
            patch("scripts.evaluate_pipeline._load_env"),
        ):
            results = ep._check_auth(["codex"], sample_pipeline_config)

        assert results["codex"]["ok"] is True
        assert "Codex CLI ready" in results["codex"]["detail"]
        mock_create.assert_called_once_with("codex", sample_pipeline_config)

    def test_includes_error_text_when_auth_fails(self, sample_pipeline_config):
        fake_orch = MagicMock()
        fake_orch.check_auth.return_value = {
            "ok": False,
            "message": "OpenCode: no Azure credentials",
            "error": "Set AZURE_OPENAI_API_KEY, or install azure-identity",
        }

        with (
            patch.object(ep, "_check_agent_available", return_value=True),
            patch("agents.create_orchestrator", return_value=fake_orch),
            patch("scripts.evaluate_pipeline._load_env"),
        ):
            results = ep._check_auth(["opencode"], sample_pipeline_config)

        assert results["opencode"]["ok"] is False
        assert "OpenCode: no Azure credentials" in results["opencode"]["detail"]
        assert "AZURE_OPENAI_API_KEY" in results["opencode"]["detail"]

    def test_marks_missing_cli_unavailable(self, sample_pipeline_config):
        with (
            patch.object(ep, "_check_agent_available", return_value=False),
            patch("scripts.evaluate_pipeline._load_env"),
        ):
            results = ep._check_auth(["gemini"], sample_pipeline_config)

        assert results["gemini"] == {"ok": False, "detail": "CLI not found on PATH"}


class TestRunMatrix:
    def test_runs_pairs_sequentially_in_order(self):
        seen: list[tuple[str, str]] = []

        def fake_run_single(command, agent, proposal, results_dir, llm_preset, execution_mode="sequential"):
            seen.append((command, agent))
            return ep.RunResult(command=command, agent=agent, status="pass")

        run_pairs = [("plot", "opencode"), ("sketch", "codex"), ("diagram", "gemini")]
        with patch("scripts.evaluate_pipeline._run_single", side_effect=fake_run_single):
            results = ep._run_matrix(run_pairs, "/tmp/proposal.md", "/tmp/results", "gemini")

        assert seen == run_pairs
        assert [(r.command, r.agent) for r in results] == run_pairs
