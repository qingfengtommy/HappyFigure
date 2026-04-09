"""Tests for LLM provider and agent platform auth checks."""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_gpt_router_state():
    """Keep llm router globals from leaking across test cases."""
    import llm

    llm._config_mode = False
    llm._role_providers = {}
    llm._provider_instances = {}
    yield
    llm._config_mode = False
    llm._role_providers = {}
    llm._provider_instances = {}


# ---------------------------------------------------------------------------
# LLMProvider base check_auth (default implementation)
# ---------------------------------------------------------------------------


class TestProviderBaseCheckAuth:
    def test_default_check_auth_returns_ok(self):
        """Base LLMProvider.check_auth() should return ok=True (assumed OK)."""
        from llm.providers import LLMProvider, ToolCallResult

        class MinimalProvider(LLMProvider):
            def run_prompt(self, *a, **kw):
                return ""
            def run_image_prompt(self, *a, **kw):
                return None
            def run_prompt_with_tools(self, *a, **kw):
                return ToolCallResult(text="")

        p = MinimalProvider()
        result = p.check_auth()
        assert result["ok"] is True
        assert "not implemented" in result["message"]


# ---------------------------------------------------------------------------
# OpenAI provider check_auth
# ---------------------------------------------------------------------------


class TestOpenAIProviderCheckAuth:
    def _make_provider(self):
        """Create an OpenAI provider with mocked SDK."""
        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_openai.OpenAI.return_value = mock_client
        sys.modules["openai"] = mock_openai

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            from llm.providers.openai_provider import OpenAIProvider
            provider = OpenAIProvider({"api_key_env": "OPENAI_API_KEY"})
        return provider, mock_client

    def test_check_auth_success(self):
        provider, mock_client = self._make_provider()
        mock_client.models.list.return_value = []
        result = provider.check_auth()
        assert result["ok"] is True
        assert "OpenAI" in result["provider"]

    def test_check_auth_failure(self):
        provider, mock_client = self._make_provider()
        mock_client.models.list.side_effect = Exception("401 Unauthorized")
        result = provider.check_auth()
        assert result["ok"] is False
        assert "401" in result["error"]


# ---------------------------------------------------------------------------
# Anthropic provider check_auth
# ---------------------------------------------------------------------------


class TestAnthropicProviderCheckAuth:
    def _make_provider(self):
        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        sys.modules["anthropic"] = mock_anthropic

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            from llm.providers.anthropic_provider import AnthropicProvider
            provider = AnthropicProvider({"api_key_env": "ANTHROPIC_API_KEY"})
        return provider, mock_client

    def test_check_auth_success(self):
        provider, mock_client = self._make_provider()
        result = provider.check_auth()
        assert result["ok"] is True

    def test_check_auth_invalid_key(self):
        provider, mock_client = self._make_provider()
        mock_client.messages.create.side_effect = Exception("authentication_error: invalid API key")
        result = provider.check_auth()
        assert result["ok"] is False
        assert "authentication" in result["error"].lower()


# ---------------------------------------------------------------------------
# Bedrock provider check_auth
# ---------------------------------------------------------------------------


class TestBedrockProviderCheckAuth:
    def _make_provider(self):
        mock_boto = MagicMock()
        mock_runtime = MagicMock()
        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456"}

        clients = {"bedrock-runtime": mock_runtime, "sts": mock_sts}
        mock_boto.client.side_effect = lambda svc, **kw: clients.get(svc, MagicMock())
        sys.modules["boto3"] = mock_boto

        from llm.providers.bedrock_provider import BedrockProvider
        provider = BedrockProvider()
        return provider, mock_sts

    def test_check_auth_success(self):
        provider, mock_sts = self._make_provider()
        result = provider.check_auth()
        assert result["ok"] is True
        assert "123456" in result["message"]

    def test_check_auth_failure(self):
        provider, mock_sts = self._make_provider()
        mock_sts.get_caller_identity.side_effect = Exception("ExpiredTokenException")
        result = provider.check_auth()
        assert result["ok"] is False
        assert "ExpiredToken" in result["error"]


# ---------------------------------------------------------------------------
# Azure provider check_auth
# ---------------------------------------------------------------------------


class TestAzureProviderCheckAuth:
    def _mock_gpt_example(self, base_url="https://my.azure.com/openai/v1"):
        """Return a mock gpt.gpt_example module with configurable base URL."""
        m = MagicMock()
        m._resolve_base_url.return_value = base_url
        return m

    def test_check_auth_success_with_api_key(self):
        """Should succeed with API key + valid endpoint."""
        with patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "test-key"}):
            from llm.providers.azure_provider import AzureProvider
            provider = AzureProvider()

            mock_mod = self._mock_gpt_example()
            with patch.dict(sys.modules, {"llm.gpt_example": mock_mod}):
                result = provider.check_auth()

        assert result["ok"] is True
        assert "API key" in result["message"]

    def test_check_auth_no_endpoint_still_ok(self):
        """Should pass with credentials but no endpoint (agents configure their own)."""
        with patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "test-key"}):
            from llm.providers.azure_provider import AzureProvider
            provider = AzureProvider()

            mock_mod = self._mock_gpt_example("/openai/v1")
            with patch.dict(sys.modules, {"llm.gpt_example": mock_mod}):
                result = provider.check_auth()

        assert result["ok"] is True
        assert "no endpoint" in result["message"].lower()

    def test_check_auth_client_init_failure(self):
        """Should fail when client construction raises."""
        with patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "test-key"}):
            from llm.providers.azure_provider import AzureProvider
            provider = AzureProvider()

            mock_mod = self._mock_gpt_example()
            mock_mod.get_azure_client.side_effect = Exception("bad URL format")
            with patch.dict(sys.modules, {"llm.gpt_example": mock_mod}):
                result = provider.check_auth()

        assert result["ok"] is False
        assert "bad URL" in result["error"]

    def test_check_auth_no_credentials_no_identity(self):
        """Should fail when no credentials and no azure-identity."""
        with patch.dict(os.environ, {}, clear=True), \
             patch.dict(sys.modules, {"azure": None, "azure.identity": None}):
            # Force re-import so the import check fails
            try:
                from llm.providers.azure_provider import AzureProvider
                provider = AzureProvider()
            except (RuntimeError, ImportError):
                # Constructor itself may fail — that's the expected path
                return
            result = provider.check_auth()

        assert result["ok"] is False
        assert "No credentials" in result["error"]


# ---------------------------------------------------------------------------
# Google provider check_auth
# ---------------------------------------------------------------------------


class TestGoogleProviderCheckAuth:
    def _make_provider(self):
        # Mock google.genai so the import check passes
        mock_google = MagicMock()
        sys.modules["google"] = mock_google
        sys.modules["google.genai"] = mock_google.genai

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            from llm.providers.google_provider import GoogleProvider
            provider = GoogleProvider()
        return provider

    def test_check_auth_success(self):
        provider = self._make_provider()
        mock_client = MagicMock()
        mock_model = MagicMock()
        mock_client.models.list.return_value = iter([mock_model])

        mock_gemini = MagicMock()
        mock_gemini._get_client.return_value = mock_client
        with patch.dict(sys.modules, {"llm.gemini_example": mock_gemini}):
            result = provider.check_auth()

        assert result["ok"] is True

    def test_check_auth_failure(self):
        provider = self._make_provider()
        mock_client = MagicMock()
        mock_client.models.list.return_value = iter([])  # empty → StopIteration

        mock_gemini = MagicMock()
        mock_gemini._get_client.return_value = mock_client
        with patch.dict(sys.modules, {"llm.gemini_example": mock_gemini}):
            result = provider.check_auth()

        assert result["ok"] is False


# ---------------------------------------------------------------------------
# llm.check_connections()
# ---------------------------------------------------------------------------


class TestCheckConnections:
    def test_returns_results_for_configured_providers(self, sample_pipeline_config, fake_provider):
        """check_connections should return one result per unique provider."""
        import llm

        fake_provider.check_auth = lambda: {
            "ok": True, "provider": "FakeProvider", "message": "OK", "error": None,
        }

        with patch("graphs.svg_utils.load_pipeline_config", return_value=sample_pipeline_config), \
             patch("llm.providers.create_provider", return_value=fake_provider):
            llm.init_from_config()

        results = llm.check_connections()
        assert len(results) >= 1
        assert all(r["ok"] for r in results)

    def test_returns_empty_when_no_providers(self):
        """check_connections with no init should return empty list."""
        import llm

        results = llm.check_connections()
        assert results == []

    def test_catches_check_auth_exception(self, sample_pipeline_config, fake_provider):
        """check_connections should catch and report exceptions from check_auth."""
        import llm

        def boom():
            raise RuntimeError("unexpected error")
        fake_provider.check_auth = boom

        with patch("graphs.svg_utils.load_pipeline_config", return_value=sample_pipeline_config), \
             patch("llm.providers.create_provider", return_value=fake_provider):
            llm.init_from_config()

        results = llm.check_connections()
        assert len(results) >= 1
        assert not results[0]["ok"]
        assert "unexpected error" in results[0]["error"]


# ---------------------------------------------------------------------------
# Orchestrator platform check_auth
# ---------------------------------------------------------------------------


class TestOrchestratorCheckAuth:
    def test_base_returns_ok(self):
        """OrchestratorBase.check_auth() default should return ok=True."""
        from agents import OrchestratorBase

        class Stub(OrchestratorBase):
            def setup(self, run_dir):
                pass
            def build_agent_command(self, agent_name, prompt):
                pass

        s = Stub({})
        result = s.check_auth()
        assert result["ok"] is True

    def test_claude_check_auth_cli_present(self):
        """Claude check_auth should succeed when CLI is installed."""
        from agents import _ensure_loaded
        _ensure_loaded()
        from agents.claude_code import ClaudeCodeOrchestrator

        orch = ClaudeCodeOrchestrator({})
        with patch("agents.claude_code.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = orch.check_auth()

        assert result["ok"] is True
        assert result["platform"] == "claude"

    def test_claude_check_auth_cli_missing(self):
        """Claude check_auth should fail when CLI is missing."""
        from agents.claude_code import ClaudeCodeOrchestrator

        orch = ClaudeCodeOrchestrator({})
        with patch("agents.claude_code.which", return_value=None):
            result = orch.check_auth()

        assert result["ok"] is False

    def test_codex_check_auth_without_api_key_still_ready(self):
        """Codex check_auth should not reject CLI-managed auth."""
        from agents.codex import CodexOrchestrator

        orch = CodexOrchestrator({})
        with patch("agents.codex.which", return_value="/usr/bin/codex"), \
             patch.dict(os.environ, {}, clear=True):
            result = orch.check_auth()

        assert result["ok"] is True
        assert "Codex CLI ready" in result["message"]

    def test_codex_check_auth_success(self):
        """Codex check_auth should mention API key when present."""
        from agents.codex import CodexOrchestrator

        orch = CodexOrchestrator({})
        with patch("agents.codex.which", return_value="/usr/bin/codex"), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            result = orch.check_auth()

        assert result["ok"] is True
        assert "API key" in result["message"]

    def test_gemini_check_auth_cli_missing(self):
        """Gemini check_auth should fail when CLI is missing."""
        from agents.gemini import GeminiOrchestrator

        orch = GeminiOrchestrator({})
        with patch("agents.gemini.which", return_value=None):
            result = orch.check_auth()

        assert result["ok"] is False

    def test_gemini_check_auth_without_env_still_ready(self):
        """Gemini check_auth should not reject CLI-managed auth."""
        from agents.gemini import GeminiOrchestrator

        orch = GeminiOrchestrator({})
        with patch("agents.gemini.which", return_value="/usr/bin/gemini"), \
             patch.dict(os.environ, {}, clear=True):
            result = orch.check_auth()

        assert result["ok"] is True
        assert "Gemini CLI ready" in result["message"]

    def test_gemini_check_auth_success(self):
        """Gemini check_auth should mention env-based auth when detected."""
        from agents.gemini import GeminiOrchestrator

        orch = GeminiOrchestrator({})
        with patch("agents.gemini.which", return_value="/usr/bin/gemini"), \
             patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            result = orch.check_auth()

        assert result["ok"] is True
        assert "API key" in result["message"]

    def test_opencode_check_auth_cli_missing(self):
        """OpenCode check_auth should fail when CLI is missing."""
        from agents.opencode import OpenCodeOrchestrator

        orch = OpenCodeOrchestrator({})
        with patch("agents.opencode.which", return_value=None):
            result = orch.check_auth()

        assert result["ok"] is False

    def test_opencode_check_auth_with_api_key(self):
        """OpenCode check_auth should succeed with AZURE_OPENAI_API_KEY + endpoint."""
        from agents.opencode import OpenCodeOrchestrator

        orch = OpenCodeOrchestrator({"agent": {"opencode": {"provider": "azure"}}})
        env = {"AZURE_OPENAI_API_KEY": "test-key", "AZURE_OPENAI_ENDPOINT": "https://azure.example.com"}
        with patch("agents.opencode.which", return_value="/usr/bin/opencode"), \
             patch.dict(os.environ, env):
            result = orch.check_auth()

        assert result["ok"] is True

    def test_opencode_check_auth_no_endpoint(self):
        """OpenCode check_auth should fail without Azure endpoint."""
        from agents.opencode import OpenCodeOrchestrator

        orch = OpenCodeOrchestrator({"agent": {"opencode": {"provider": "azure"}}})
        with patch("agents.opencode.which", return_value="/usr/bin/opencode"), \
             patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "test-key"}, clear=True), \
             patch.object(orch, "_resolve_endpoint", return_value=""):
            result = orch.check_auth()

        assert result["ok"] is False
        assert "endpoint" in result["error"].lower()


# ---------------------------------------------------------------------------
# OpenCode endpoint patching
# ---------------------------------------------------------------------------


class TestOpenCodeEndpointPatching:
    def test_patch_and_restore(self, tmp_path):
        """setup() should patch baseURL, cleanup() should restore it."""
        from agents.opencode import OpenCodeOrchestrator

        # Create a minimal opencode.jsonc
        opencode_dir = tmp_path / ".opencode"
        opencode_dir.mkdir()
        jsonc = opencode_dir / "opencode.jsonc"
        original = '{\n  "provider": {\n    "azure": {\n      "options": {\n        "baseURL": "https://placeholder.example.com/openai/v1"\n      }\n    }\n  }\n}'
        jsonc.write_text(original)

        orch = OpenCodeOrchestrator({"agent": {"opencode": {"provider": "azure"}}})
        with patch.object(orch, "_resolve_endpoint", return_value="https://real-azure.example.com"):
            orch._patch_opencode_config(tmp_path)

        patched = jsonc.read_text()
        assert "real-azure.example.com" in patched
        assert "placeholder.example.com" not in patched

        # Cleanup restores original
        with patch("agents.opencode.Path") as mock_path_cls:
            mock_path_cls.return_value.resolve.return_value.parent.parent = tmp_path
            orch.cleanup()

        assert jsonc.read_text() == original

    def test_no_patch_without_endpoint(self, tmp_path):
        """setup() should not patch if no endpoint is resolved."""
        from agents.opencode import OpenCodeOrchestrator

        opencode_dir = tmp_path / ".opencode"
        opencode_dir.mkdir()
        jsonc = opencode_dir / "opencode.jsonc"
        original = '{"provider": {"azure": {"options": {"baseURL": "https://placeholder/openai/v1"}}}}'
        jsonc.write_text(original)

        orch = OpenCodeOrchestrator({})
        with patch.object(orch, "_resolve_endpoint", return_value=""):
            orch._patch_opencode_config(tmp_path)

        assert jsonc.read_text() == original
        assert orch._opencode_jsonc_original is None

    def test_resolve_endpoint_from_config(self):
        """_resolve_endpoint should read endpoint from pipeline.yaml config."""
        from agents.opencode import OpenCodeOrchestrator

        config = {"llm": {"providers": {"azure": {"endpoint": "https://from-config.example.com"}}}}
        orch = OpenCodeOrchestrator(config)
        with patch.dict(os.environ, {}, clear=True):
            result = orch._resolve_endpoint()

        assert result == "https://from-config.example.com"

    def test_resolve_endpoint_env_overrides_config(self):
        """Env var should take priority over pipeline.yaml endpoint."""
        from agents.opencode import OpenCodeOrchestrator

        config = {"llm": {"providers": {"azure": {"endpoint": "https://from-config.example.com"}}}}
        orch = OpenCodeOrchestrator(config)
        with patch.dict(os.environ, {"AZURE_OPENAI_ENDPOINT": "https://from-env.example.com"}):
            result = orch._resolve_endpoint()

        assert result == "https://from-env.example.com"
