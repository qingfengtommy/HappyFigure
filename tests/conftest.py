"""Shared fixtures for HappyFigure test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path so bare `import llm` etc. resolve.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

CONFIGS_DIR = PROJECT_ROOT / "configs"
PIPELINE_YAML = CONFIGS_DIR / "pipeline.yaml"


@pytest.fixture()
def pipeline_config() -> dict:
    """Load and return the real pipeline.yaml as a dict."""
    import yaml

    return yaml.safe_load(PIPELINE_YAML.read_text(encoding="utf-8"))


@pytest.fixture()
def sample_pipeline_config() -> dict:
    """A minimal in-memory config dict for tests that don't need the real file."""
    return {
        "llm": {
            "providers": {
                "azure": {"api_key_env": "AZURE_OPENAI_API_KEY"},
                "google": {"api_key_env": "GEMINI_API_KEY"},
            },
            "roles": {
                "chat": {"provider": "azure", "model": "gpt-5.4"},
                "code": {"provider": "azure", "model": "gpt-5.4"},
                "drawing": {"provider": "google", "model": "gemini-img"},
            },
            "presets": {
                "gemini": {
                    "chat": {"provider": "google", "model": "gemini-3.1-pro-preview"},
                    "code": {"provider": "google", "model": "gemini-3.1-pro-preview"},
                    "drawing": {"provider": "google", "model": "gemini-img"},
                },
                "mixed": {
                    "drawing": {"provider": "google", "model": "gemini-img"},
                },
            },
        },
        "scoring": {
            "quality_thresholds": {"journal": 10.2, "conference": 9.6, "default": 9.0},
            "figure_score_threshold": 9.0,
            "max_iterations": 3,
        },
        "svg": {
            "render_scale": 2.0,
            "label_font_min": 12,
            "label_font_max": 48,
        },
        "agent": {
            "platform": "opencode",
        },
    }


# ---------------------------------------------------------------------------
# Reset module-level caches between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_gpt_state():
    """Reset llm/__init__.py global state before each test."""
    import llm

    llm._config_mode = False
    llm._role_providers = {}
    llm._provider_instances = {}
    llm._backend = "azure"
    yield


@pytest.fixture(autouse=True)
def _reset_svg_utils_cache():
    """Clear the cached pipeline config so each test starts fresh."""
    import graphs.svg_utils as su

    su._PIPELINE_CONFIG = None
    yield
    su._PIPELINE_CONFIG = None


@pytest.fixture(autouse=True)
def _reset_provider_registry():
    """Reset the provider registry loaded flag so lazy loading can be re-tested."""
    import llm.providers as prov

    prov._providers_loaded = False
    prov._REGISTRY.clear()
    yield
    prov._providers_loaded = False
    prov._REGISTRY.clear()


@pytest.fixture(autouse=True)
def _reset_orchestrator_registry():
    """Reset the orchestrator registry loaded flag."""
    import agents

    agents._loaded = False
    agents._PLATFORMS.clear()
    yield
    agents._loaded = False
    agents._PLATFORMS.clear()


# ---------------------------------------------------------------------------
# Mock provider / orchestrator helpers
# ---------------------------------------------------------------------------


class FakeProvider:
    """Minimal LLMProvider stand-in for routing tests."""

    capabilities = {"text": True, "tools": True, "vision": False, "image_gen": False}

    def __init__(self, config=None):
        self.config = config or {}

    def run_prompt(self, model, prompt, **kw):
        return f"fake:{model}:{prompt[:20]}"

    def run_image_prompt(self, model, prompt, **kw):
        return b"\x89PNG"

    def run_prompt_with_tools(self, model, prompt, **kw):
        from llm.providers import ToolCallResult

        return ToolCallResult(text=f"tool:{model}", tool_calls=[], tool_results=[])


@pytest.fixture()
def fake_provider():
    return FakeProvider()
