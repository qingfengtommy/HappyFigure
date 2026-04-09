"""LLM Provider abstraction layer.

Each provider implements the LLMProvider interface and is registered
by name. The router in ``llm/__init__.py`` looks up providers from
``configs/pipeline.yaml`` and dispatches calls accordingly.

Providers lazily import their SDKs — only the SDK for the configured
provider needs to be installed.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class ToolCallResult:
    """Result of a tool-calling prompt session."""
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    raw_response: Any = None


class LLMProvider(ABC):
    """Base class for LLM providers.

    Subclasses must declare their capabilities and implement the core methods.
    SDK imports should happen inside ``__init__`` or method bodies — never at
    module level — so users only need to install the SDK for providers they use.
    """

    # Override in subclasses: which features this provider supports
    capabilities: dict = {
        "text": True,
        "tools": False,
        "vision": False,
        "image_gen": False,
        "embeddings": False,
    }

    def check_auth(self) -> dict:
        """Verify credentials are valid by making a lightweight API call.

        Returns a dict with:
          - ``ok`` (bool): True if auth succeeded
          - ``provider`` (str): provider class name
          - ``message`` (str): human-readable status
          - ``error`` (str | None): error detail on failure
        """
        return {
            "ok": True,
            "provider": self.__class__.__name__,
            "message": "auth check not implemented (assumed OK)",
            "error": None,
        }

    @abstractmethod
    def run_prompt(
        self,
        model: str,
        prompt: str,
        *,
        system_prompt: str | None = None,
        image_base64: str | None = None,
        few_shot_messages: list[dict] | None = None,
    ) -> str:
        """Send a text prompt and return the response text."""
        ...

    @abstractmethod
    def run_image_prompt(
        self,
        model: str,
        prompt: str,
        *,
        reference_images: list[str] | None = None,
    ) -> bytes | None:
        """Generate an image. Returns PNG bytes or None."""
        ...

    @abstractmethod
    def run_prompt_with_tools(
        self,
        model: str,
        prompt: str,
        *,
        system_prompt: str | None = None,
        image_base64: str | None = None,
        few_shot_messages: list[dict] | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        tool_executor: Callable | None = None,
        max_tool_rounds: int = 5,
    ) -> ToolCallResult:
        """Send a prompt with tool-calling support (agentic loop)."""
        ...



# ── Provider Registry ────────────────────────────────────────────────

_REGISTRY: dict[str, type[LLMProvider]] = {}


def register_provider(name: str):
    """Decorator to register a provider class by name."""
    def decorator(cls: type[LLMProvider]):
        _REGISTRY[name] = cls
        return cls
    return decorator


def get_provider_class(name: str) -> type[LLMProvider]:
    """Look up a registered provider class by name."""
    # Trigger registration by importing the provider modules
    _ensure_providers_loaded()
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys()))
        raise ValueError(
            f"Unknown provider: {name!r}. Available: {available}"
        )
    return _REGISTRY[name]


def create_provider(name: str, config: dict | None = None) -> LLMProvider:
    """Instantiate a provider by name with optional config."""
    cls = get_provider_class(name)
    return cls(config or {})


def list_providers() -> list[str]:
    """Return names of all registered providers."""
    _ensure_providers_loaded()
    return sorted(_REGISTRY.keys())


# ── Lazy loading ─────────────────────────────────────────────────────

_providers_loaded = False


def _ensure_providers_loaded():
    """Import all provider modules to trigger registration."""
    global _providers_loaded
    if _providers_loaded:
        return
    _providers_loaded = True
    # Import each provider module — they self-register via @register_provider
    import importlib
    for mod_name in [
        "llm.providers.openai_provider",
        "llm.providers.azure_provider",
        "llm.providers.anthropic_provider",
        "llm.providers.google_provider",
        "llm.providers.bedrock_provider",
    ]:
        try:
            importlib.import_module(mod_name)
        except ImportError as e:
            # Only swallow errors from missing optional SDK packages.
            # If the provider module itself is broken, re-raise.
            missing_pkg = getattr(e, "name", "") or ""
            optional_sdks = {"openai", "anthropic", "google", "google.genai", "boto3", "azure", "azure.identity"}
            if any(missing_pkg.startswith(sdk) for sdk in optional_sdks):
                logger.debug("Provider %s not available (missing %s): %s", mod_name, missing_pkg, e)
            else:
                logger.warning("Provider %s failed to load: %s", mod_name, e)
                raise
