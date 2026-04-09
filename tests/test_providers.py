"""Tests for the provider registry in llm/providers/__init__.py."""
from __future__ import annotations

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# ToolCallResult dataclass
# ---------------------------------------------------------------------------


class TestToolCallResult:
    def test_defaults(self):
        from llm.providers import ToolCallResult

        r = ToolCallResult(text="hello")
        assert r.text == "hello"
        assert r.tool_calls == []
        assert r.tool_results == []
        assert r.raw_response is None

    def test_with_values(self):
        from llm.providers import ToolCallResult

        r = ToolCallResult(
            text="ok",
            tool_calls=[{"name": "fn"}],
            tool_results=["result1"],
            raw_response={"raw": True},
        )
        assert len(r.tool_calls) == 1
        assert r.tool_results == ["result1"]
        assert r.raw_response == {"raw": True}


# ---------------------------------------------------------------------------
# LLMProvider base class
# ---------------------------------------------------------------------------


class TestLLMProvider:
    def test_cannot_instantiate_abstract(self):
        from llm.providers import LLMProvider

        with pytest.raises(TypeError):
            LLMProvider()

    def test_default_capabilities(self):
        from llm.providers import LLMProvider

        # Verify the class-level defaults
        assert LLMProvider.capabilities["text"] is True
        assert LLMProvider.capabilities["tools"] is False
        assert LLMProvider.capabilities["image_gen"] is False


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


class TestRegistry:
    @staticmethod
    def _missing_sdk_error(name="openai"):
        err = ImportError(f"No module named {name}")
        err.name = name
        return err

    def test_register_and_retrieve(self):
        """register_provider decorator should add class to registry."""
        from llm.providers import register_provider, _REGISTRY, LLMProvider

        @register_provider("test_dummy")
        class DummyProvider(LLMProvider):
            def run_prompt(self, *a, **kw):
                return ""
            def run_image_prompt(self, *a, **kw):
                return None
            def run_prompt_with_tools(self, *a, **kw):
                from llm.providers import ToolCallResult
                return ToolCallResult(text="")

        assert "test_dummy" in _REGISTRY
        assert _REGISTRY["test_dummy"] is DummyProvider

    def test_create_provider_with_registered(self):
        """create_provider should instantiate a registered provider."""
        from llm.providers import register_provider, create_provider, LLMProvider

        @register_provider("test_create")
        class CreateProvider(LLMProvider):
            def __init__(self, config):
                self.config = config
            def run_prompt(self, *a, **kw):
                return ""
            def run_image_prompt(self, *a, **kw):
                return None
            def run_prompt_with_tools(self, *a, **kw):
                from llm.providers import ToolCallResult
                return ToolCallResult(text="")

        # Mark as loaded so _ensure_providers_loaded doesn't clear our test registration
        import llm.providers as prov
        prov._providers_loaded = True

        instance = create_provider("test_create", {"key": "val"})
        assert instance.config == {"key": "val"}

    def test_create_provider_unknown_raises(self):
        """create_provider with unknown name should raise ValueError."""
        import llm.providers as prov

        # Mark as loaded to avoid import side effects
        prov._providers_loaded = True

        with pytest.raises(ValueError, match="Unknown provider"):
            prov.create_provider("nonexistent_provider")

    def test_list_providers_triggers_load(self):
        """list_providers should trigger lazy loading of provider modules."""
        import llm.providers as prov

        # Patch importlib to avoid real SDK imports
        with patch("importlib.import_module") as mock_import:
            mock_import.side_effect = self._missing_sdk_error()
            result = prov.list_providers()

        # All imports failed, so registry is empty
        assert result == []
        # But import was attempted for all provider modules
        assert mock_import.call_count == 5

    def test_lazy_loading_skips_missing_sdks(self):
        """Provider modules that fail to import should be silently skipped."""
        import llm.providers as prov

        with patch("importlib.import_module", side_effect=self._missing_sdk_error()):
            prov._ensure_providers_loaded()

        assert prov._providers_loaded is True
        # No providers registered since all imports failed
        assert len(prov._REGISTRY) == 0

    def test_lazy_loading_reraises_broken_provider_imports(self):
        """Non-SDK ImportErrors inside a provider module should fail fast."""
        import llm.providers as prov

        broken = ImportError("broken provider import")
        broken.name = "llm.providers.shared_helpers"

        with patch("importlib.import_module", side_effect=broken):
            with pytest.raises(ImportError, match="broken provider import"):
                prov._ensure_providers_loaded()

    def test_lazy_loading_only_runs_once(self):
        """_ensure_providers_loaded should be idempotent after first call."""
        import llm.providers as prov

        with patch("importlib.import_module", side_effect=self._missing_sdk_error()):
            prov._ensure_providers_loaded()

        # Set flag manually and add fake entry
        prov._REGISTRY["sentinel"] = "fake"

        # Second call should not re-import
        prov._ensure_providers_loaded()
        assert "sentinel" in prov._REGISTRY  # still there, wasn't cleared
