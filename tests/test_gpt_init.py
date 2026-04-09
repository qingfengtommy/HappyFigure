"""Tests for the LLM router in llm/__init__.py."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# init_from_config
# ---------------------------------------------------------------------------


class TestInitFromConfig:
    """Test llm.init_from_config() config-driven routing setup."""

    def test_enables_config_mode_with_valid_config(self, sample_pipeline_config, fake_provider):
        """init_from_config should set _config_mode=True when roles are configured."""
        import llm

        with patch("graphs.svg_utils.load_pipeline_config", return_value=sample_pipeline_config), \
             patch("llm.providers.create_provider", return_value=fake_provider):
            llm.init_from_config()

        assert llm._config_mode is True
        assert "chat" in llm._role_providers
        assert "code" in llm._role_providers
        assert "drawing" in llm._role_providers

    def test_falls_back_when_no_llm_section(self):
        """Without an llm section, should stay in legacy mode."""
        import llm

        with patch("graphs.svg_utils.load_pipeline_config", return_value={"scoring": {}}):
            llm.init_from_config()

        assert llm._config_mode is False
        assert llm._role_providers == {}

    def test_falls_back_when_no_roles(self):
        """llm section without roles should stay in legacy mode."""
        import llm

        cfg = {"llm": {"providers": {"azure": {}}}}
        with patch("graphs.svg_utils.load_pipeline_config", return_value=cfg):
            llm.init_from_config()

        assert llm._config_mode is False

    def test_skips_role_missing_provider_or_model(self, fake_provider):
        """Roles without provider or model should be skipped."""
        import llm

        cfg = {
            "llm": {
                "providers": {"azure": {}},
                "roles": {
                    "chat": {"provider": "azure", "model": "gpt-5.4"},
                    "bad_role": {"provider": "azure"},  # no model
                },
            }
        }
        with patch("graphs.svg_utils.load_pipeline_config", return_value=cfg), \
             patch("llm.providers.create_provider", return_value=fake_provider):
            llm.init_from_config()

        assert "chat" in llm._role_providers
        assert "bad_role" not in llm._role_providers

    def test_reuses_provider_instances(self, sample_pipeline_config, fake_provider):
        """Same provider name should produce only one instance."""
        import llm

        create_mock = MagicMock(return_value=fake_provider)
        with patch("graphs.svg_utils.load_pipeline_config", return_value=sample_pipeline_config), \
             patch("llm.providers.create_provider", create_mock):
            llm.init_from_config()

        # azure is used by chat+code, google by drawing -> 2 create calls
        provider_names = [c.args[0] for c in create_mock.call_args_list]
        assert provider_names.count("azure") == 1
        assert provider_names.count("google") == 1


# ---------------------------------------------------------------------------
# apply_preset
# ---------------------------------------------------------------------------


class TestApplyPreset:
    """Test llm.apply_preset() overlaying roles."""

    def test_preset_overrides_roles(self, sample_pipeline_config, fake_provider):
        """Applying the 'gemini' preset should override all three roles."""
        import llm

        with patch("graphs.svg_utils.load_pipeline_config", return_value=sample_pipeline_config), \
             patch("llm.providers.create_provider", return_value=fake_provider):
            llm.init_from_config()
            llm.apply_preset("gemini")

        # After gemini preset, chat model should be gemini-2.5-flash
        _, model = llm._role_providers["chat"]
        assert model == "gemini-2.5-flash"

    def test_unknown_preset_warns(self, sample_pipeline_config, fake_provider):
        """Unknown preset name should log a warning and not crash."""
        import llm

        with patch("graphs.svg_utils.load_pipeline_config", return_value=sample_pipeline_config), \
             patch("llm.providers.create_provider", return_value=fake_provider):
            llm.init_from_config()
            # Should not raise
            llm.apply_preset("nonexistent_preset")

        # Original roles should remain
        assert "chat" in llm._role_providers

    def test_mixed_preset_only_overrides_drawing(self, sample_pipeline_config, fake_provider):
        """The 'mixed' preset should only change the drawing role."""
        import llm

        with patch("graphs.svg_utils.load_pipeline_config", return_value=sample_pipeline_config), \
             patch("llm.providers.create_provider", return_value=fake_provider):
            llm.init_from_config()
            # Record chat model before preset
            _, chat_model_before = llm._role_providers["chat"]
            llm.apply_preset("mixed")

        _, chat_model_after = llm._role_providers["chat"]
        assert chat_model_before == chat_model_after  # chat unchanged


# ---------------------------------------------------------------------------
# run_prompt routing
# ---------------------------------------------------------------------------


class TestRunPrompt:
    """Test run_prompt routes correctly in config vs legacy mode."""

    def test_config_mode_routes_to_provider(self, sample_pipeline_config, fake_provider):
        """In config mode, run_prompt should call the provider's run_prompt."""
        import llm

        with patch("graphs.svg_utils.load_pipeline_config", return_value=sample_pipeline_config), \
             patch("llm.providers.create_provider", return_value=fake_provider):
            llm.init_from_config()

        result = llm.run_prompt("chat", "Hello world")
        assert result.startswith("fake:")
        assert "gpt-5.4" in result

    def test_legacy_mode_azure_fallback(self):
        """In legacy mode with azure backend, should call gpt_example.run_prompt."""
        import llm
        import sys

        # Create a mock module so patch can resolve the dotted path
        mock_mod = MagicMock()
        mock_mod.run_prompt = MagicMock(return_value="azure_response")
        sys.modules["llm.gpt_example"] = mock_mod

        try:
            result = llm.run_prompt("chat", "Hello")
            assert result == "azure_response"
            mock_mod.run_prompt.assert_called_once()
        finally:
            sys.modules.pop("llm.gpt_example", None)

    def test_legacy_mode_gemini_fallback(self):
        """In legacy mode with gemini backend, should call gemini_example.run_prompt."""
        import llm
        import sys

        llm.set_backend("gemini")
        mock_mod = MagicMock()
        mock_mod.run_prompt = MagicMock(return_value="gemini_response")
        sys.modules["llm.gemini_example"] = mock_mod

        try:
            result = llm.run_prompt("chat", "Hello")
            assert result == "gemini_response"
        finally:
            sys.modules.pop("llm.gemini_example", None)


# ---------------------------------------------------------------------------
# Legacy backend
# ---------------------------------------------------------------------------


class TestLegacyBackend:
    """Test set_backend / get_backend."""

    def test_default_backend_is_azure(self):
        import llm

        assert llm.get_backend() == "azure"

    def test_set_valid_backends(self):
        import llm

        for name in ("azure", "svg", "gemini"):
            llm.set_backend(name)
            assert llm.get_backend() == name

    def test_set_invalid_backend_raises(self):
        import llm

        with pytest.raises(ValueError, match="Unknown backend"):
            llm.set_backend("openai")


# ---------------------------------------------------------------------------
# get_model_display
# ---------------------------------------------------------------------------


class TestGetModelDisplay:
    def test_config_mode_display(self, sample_pipeline_config, fake_provider):
        import llm

        with patch("graphs.svg_utils.load_pipeline_config", return_value=sample_pipeline_config), \
             patch("llm.providers.create_provider", return_value=fake_provider):
            llm.init_from_config()

        display = llm.get_model_display("chat")
        assert display == "chat:gpt-5.4"


# ---------------------------------------------------------------------------
# run_prompt_with_tools
# ---------------------------------------------------------------------------


class TestRunPromptWithTools:
    def test_config_mode_routes_tools(self, sample_pipeline_config, fake_provider):
        import llm
        from llm.providers import ToolCallResult

        with patch("graphs.svg_utils.load_pipeline_config", return_value=sample_pipeline_config), \
             patch("llm.providers.create_provider", return_value=fake_provider):
            llm.init_from_config()

        result = llm.run_prompt_with_tools("chat", "use tool")
        assert isinstance(result, ToolCallResult)
        assert "gpt-5.4" in result.text

    def test_raises_when_provider_lacks_tools(self, sample_pipeline_config):
        """Provider without tools capability should raise RuntimeError."""
        import llm
        from tests.conftest import FakeProvider

        no_tools = FakeProvider()
        no_tools.capabilities = {"text": True, "tools": False}

        with patch("graphs.svg_utils.load_pipeline_config", return_value=sample_pipeline_config), \
             patch("llm.providers.create_provider", return_value=no_tools):
            llm.init_from_config()

        with pytest.raises(RuntimeError, match="does not support tool calling"):
            llm.run_prompt_with_tools("chat", "use tool")
