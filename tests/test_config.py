"""Tests for config loading — verifies pipeline.yaml structure and required keys."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PIPELINE_YAML = Path(__file__).resolve().parent.parent / "configs" / "pipeline.yaml"


@pytest.fixture()
def config() -> dict:
    assert PIPELINE_YAML.exists(), f"pipeline.yaml not found at {PIPELINE_YAML}"
    return yaml.safe_load(PIPELINE_YAML.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Top-level structure
# ---------------------------------------------------------------------------


class TestTopLevelKeys:
    def test_has_required_sections(self, config):
        for key in ("llm", "scoring", "svg", "agent", "beam", "sam"):
            assert key in config, f"Missing top-level key: {key}"

    def test_config_is_dict(self, config):
        assert isinstance(config, dict)


# ---------------------------------------------------------------------------
# LLM section
# ---------------------------------------------------------------------------


class TestLLMConfig:
    def test_has_providers(self, config):
        llm = config["llm"]
        assert "providers" in llm
        assert isinstance(llm["providers"], dict)
        assert len(llm["providers"]) > 0

    def test_has_roles(self, config):
        llm = config["llm"]
        assert "roles" in llm
        roles = llm["roles"]
        # Minimum required roles
        for role in ("chat", "code", "drawing"):
            assert role in roles, f"Missing role: {role}"

    def test_roles_have_provider_and_model(self, config):
        for name, role_def in config["llm"]["roles"].items():
            assert "provider" in role_def, f"Role {name} missing 'provider'"
            assert "model" in role_def, f"Role {name} missing 'model'"

    def test_has_presets(self, config):
        llm = config["llm"]
        assert "presets" in llm
        presets = llm["presets"]
        # At least azure, gemini, mixed
        for name in ("azure", "gemini", "mixed"):
            assert name in presets, f"Missing preset: {name}"

    def test_preset_roles_have_provider_and_model(self, config):
        for preset_name, preset in config["llm"]["presets"].items():
            for role_name, role_def in preset.items():
                assert "provider" in role_def, f"Preset {preset_name}.{role_name} missing 'provider'"
                assert "model" in role_def, f"Preset {preset_name}.{role_name} missing 'model'"

    def test_role_providers_exist_in_providers_section(self, config):
        """Every provider referenced by a role should be defined in providers."""
        defined_providers = set(config["llm"]["providers"].keys())
        for name, role_def in config["llm"]["roles"].items():
            assert role_def["provider"] in defined_providers, \
                f"Role {name} references undefined provider: {role_def['provider']}"


# ---------------------------------------------------------------------------
# Scoring section
# ---------------------------------------------------------------------------


class TestScoringConfig:
    def test_has_quality_thresholds(self, config):
        scoring = config["scoring"]
        assert "quality_thresholds" in scoring
        qt = scoring["quality_thresholds"]
        assert "journal" in qt
        assert "conference" in qt
        assert "default" in qt

    def test_thresholds_are_numeric(self, config):
        for name, val in config["scoring"]["quality_thresholds"].items():
            assert isinstance(val, (int, float)), f"Threshold {name} is not numeric: {val}"

    def test_has_figure_score_threshold(self, config):
        assert "figure_score_threshold" in config["scoring"]
        assert isinstance(config["scoring"]["figure_score_threshold"], (int, float))

    def test_has_max_iterations(self, config):
        assert "max_iterations" in config["scoring"]
        assert config["scoring"]["max_iterations"] >= 1


# ---------------------------------------------------------------------------
# SVG section
# ---------------------------------------------------------------------------


class TestSVGConfig:
    def test_has_render_scale(self, config):
        assert "render_scale" in config["svg"]
        assert config["svg"]["render_scale"] > 0

    def test_has_font_limits(self, config):
        svg = config["svg"]
        assert "label_font_min" in svg
        assert "label_font_max" in svg
        assert svg["label_font_min"] < svg["label_font_max"]


# ---------------------------------------------------------------------------
# Agent section
# ---------------------------------------------------------------------------


class TestAgentConfig:
    def test_has_platform(self, config):
        assert "platform" in config["agent"]
        assert config["agent"]["platform"] in ("opencode", "claude", "codex", "gemini")

    def test_has_platform_configs(self, config):
        agent = config["agent"]
        # At least the default platform should have a config block
        platform = agent["platform"]
        assert platform in agent, f"No config block for default platform: {platform}"


# ---------------------------------------------------------------------------
# Beam section
# ---------------------------------------------------------------------------


class TestBeamConfig:
    def test_has_required_keys(self, config):
        beam = config["beam"]
        for key in ("width", "style_variants", "code_variants", "iterations"):
            assert key in beam, f"Missing beam key: {key}"
            assert isinstance(beam[key], int)
            assert beam[key] >= 1


# ---------------------------------------------------------------------------
# load_pipeline_config integration
# ---------------------------------------------------------------------------


class TestLoadPipelineConfigIntegration:
    def test_returns_same_as_direct_yaml_load(self, config):
        """load_pipeline_config() should return equivalent data to direct yaml.safe_load."""
        from graphs.svg_utils import load_pipeline_config

        loaded = load_pipeline_config()
        # Check key sections match
        assert loaded["llm"]["roles"] == config["llm"]["roles"]
        assert loaded["scoring"] == config["scoring"]
