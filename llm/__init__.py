"""LLM backend router.

**Config-driven mode** (recommended): Reads ``configs/pipeline.yaml`` ``llm``
section to route each role (chat, code, drawing) to a specific provider and
model.  Call ``init_from_config()`` at startup.

If ``pipeline.yaml`` has no ``llm`` section, falls back to direct backend
routing via ``set_backend()``.

Usage::

    import llm
    llm.init_from_config()                    # reads configs/pipeline.yaml
    result = llm.run_prompt("chat", "Hello")  # routes to configured provider
"""
from __future__ import annotations

__all__ = [
    "init_from_config",
    "apply_preset",
    "run_prompt",
    "run_image_prompt",
    "run_prompt_with_tools",
    "check_connections",
    "encode_image_to_data_url",
    "get_model_display",
    "set_backend",
    "get_backend",
    "ToolCallResult",
]

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── Shared result type ────────────────────────────────────────────────

from llm.providers import ToolCallResult  # noqa: E402  — single definition


# ── Config-driven routing state ──────────────────────────────────────

_config_mode: bool = False
_role_providers: dict[str, tuple[Any, str]] = {}  # role -> (provider_instance, model_name)
_provider_instances: dict[str, Any] = {}           # provider_name -> instance


def init_from_config(config_path: str | None = None) -> None:
    """Initialize providers from pipeline.yaml config.

    Reads the ``llm`` section which maps roles to providers and models.
    Falls back gracefully if config is missing (uses legacy mode).
    """
    global _config_mode, _role_providers, _provider_instances

    # Reset config-driven state so repeated initialization reflects the
    # current config instead of leaking providers from an earlier call.
    _config_mode = False
    _role_providers = {}
    _provider_instances = {}

    from graphs.svg_utils import load_pipeline_config
    cfg = load_pipeline_config()
    llm_cfg = cfg.get("llm")
    if not llm_cfg:
        logger.info("No 'llm' section in pipeline.yaml; using legacy backend mode")
        return

    providers_cfg = llm_cfg.get("providers", {})
    roles_cfg = llm_cfg.get("roles", {})

    if not roles_cfg:
        logger.info("No 'llm.roles' in pipeline.yaml; using legacy backend mode")
        return

    from llm.providers import create_provider

    for role_name, role_def in roles_cfg.items():
        provider_name = role_def.get("provider")
        model_name = role_def.get("model")
        if not provider_name or not model_name:
            logger.warning("Role %r missing provider or model, skipping", role_name)
            continue

        # Reuse existing provider instance if same name
        if provider_name not in _provider_instances:
            provider_cfg = providers_cfg.get(provider_name, {})
            try:
                _provider_instances[provider_name] = create_provider(
                    provider_name, provider_cfg
                )
            except Exception as e:
                logger.warning(
                    "Failed to create provider %r for role %r: %s",
                    provider_name, role_name, e,
                )
                continue

        _role_providers[role_name] = (_provider_instances[provider_name], model_name)

    if _role_providers:
        _config_mode = True
        logger.info(
            "Config-driven LLM routing enabled for roles: %s",
            ", ".join(sorted(_role_providers.keys())),
        )


def apply_preset(preset_name: str) -> None:
    """Apply a named LLM preset, overriding specific roles.

    Reads ``llm.presets.<name>`` from pipeline.yaml and overlays those
    role definitions onto the already-initialized roles. Call after
    ``init_from_config()``.
    """
    global _config_mode, _role_providers, _provider_instances

    from graphs.svg_utils import load_pipeline_config
    from llm.providers import create_provider

    cfg = load_pipeline_config()
    llm_cfg = cfg.get("llm", {})
    presets = llm_cfg.get("presets", {})
    preset = presets.get(preset_name)
    if not preset:
        logger.warning("Unknown LLM preset: %r (available: %s)",
                        preset_name, ", ".join(presets.keys()))
        return

    providers_cfg = llm_cfg.get("providers", {})

    for role_name, role_def in preset.items():
        provider_name = role_def.get("provider")
        model_name = role_def.get("model")
        if not provider_name or not model_name:
            continue

        if provider_name not in _provider_instances:
            provider_cfg = providers_cfg.get(provider_name, {})
            try:
                _provider_instances[provider_name] = create_provider(
                    provider_name, provider_cfg
                )
            except Exception as e:
                logger.warning("Failed to create provider %r: %s", provider_name, e)
                continue

        _role_providers[role_name] = (_provider_instances[provider_name], model_name)

    if not _config_mode and _role_providers:
        _config_mode = True

    logger.info("Applied LLM preset %r", preset_name)


# ── Legacy backend routing ───────────────────────────────────────────

_backend: str = "azure"  # "azure" | "svg" | "gemini"
_VALID_BACKENDS = ("azure", "svg", "gemini")


def set_backend(name: str) -> None:
    """Set the active LLM backend (legacy mode).

    Modes:
      "azure"  — Azure OpenAI for everything (default)
      "svg"    — Azure for LLM, Gemini for image generation
      "gemini" — Gemini for LLM + image gen
    """
    global _backend
    if name not in _VALID_BACKENDS:
        raise ValueError(
            f"Unknown backend: {name!r}. Choose from {_VALID_BACKENDS}."
        )
    _backend = name


def get_backend() -> str:
    return _backend


def _text_backend() -> str:
    if _backend == "gemini":
        return "gemini"
    return "azure"


def _image_gen_backend() -> str:
    if _backend == "azure":
        return "azure"
    return "gemini"


# ── Public API ────────────────────────────────────────────────────────

def run_prompt(
    model_mode: str,
    prompt: str,
    *,
    system_prompt: str | None = None,
    image_base64: str | None = None,
    few_shot_messages: list[dict] | None = None,
) -> str:
    """Route to the configured provider for this role."""
    # Config-driven mode
    if _config_mode and model_mode in _role_providers:
        provider, model = _role_providers[model_mode]
        return provider.run_prompt(
            model, prompt,
            system_prompt=system_prompt,
            image_base64=image_base64,
            few_shot_messages=few_shot_messages,
        )

    # Legacy mode
    if _text_backend() == "gemini":
        from llm.gemini_example import run_prompt as _run
    else:
        from llm.gpt_example import run_prompt as _run
    return _run(
        model_mode, prompt,
        system_prompt=system_prompt,
        image_base64=image_base64,
        few_shot_messages=few_shot_messages,
    )


def get_model_display(model_mode: str) -> str:
    """Return a short display string like 'chat:gpt-5.4' for progress UI."""
    # Config-driven mode
    if _config_mode and model_mode in _role_providers:
        _, model = _role_providers[model_mode]
        return f"{model_mode}:{model}"

    # Legacy mode
    if model_mode == "drawing":
        use_gemini = _image_gen_backend() == "gemini"
    else:
        use_gemini = _text_backend() == "gemini"
    if use_gemini:
        from llm.gemini_example import get_model_name
    else:
        from llm.gpt_example import get_model_name
    try:
        name = get_model_name(model_mode)
    except ValueError:
        name = "?"
    return f"{model_mode}:{name}"


def run_image_prompt(
    prompt: str,
    *,
    reference_images: list[str] | None = None,
) -> bytes | None:
    """Route to the image-generation provider."""
    # Config-driven mode
    if _config_mode and "drawing" in _role_providers:
        provider, model = _role_providers["drawing"]
        if not provider.capabilities.get("image_gen"):
            raise RuntimeError(
                f"Provider {provider.__class__.__name__} does not support image generation. "
                f"Configure a different provider for the 'drawing' role."
            )
        return provider.run_image_prompt(
            model, prompt, reference_images=reference_images,
        )

    # Legacy mode
    if _image_gen_backend() == "gemini":
        from llm.gemini_example import run_image_prompt as _run
    else:
        from llm.gpt_example import run_image_prompt as _run
    return _run(prompt, reference_images=reference_images)


def run_prompt_with_tools(
    model_mode: str,
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
    """Route to the configured provider for tool-calling."""
    # Config-driven mode
    if _config_mode and model_mode in _role_providers:
        provider, model = _role_providers[model_mode]
        if not provider.capabilities.get("tools"):
            raise RuntimeError(
                f"Provider {provider.__class__.__name__} does not support tool calling."
            )
        result = provider.run_prompt_with_tools(
            model, prompt,
            system_prompt=system_prompt,
            image_base64=image_base64,
            few_shot_messages=few_shot_messages,
            tools=tools,
            tool_choice=tool_choice,
            tool_executor=tool_executor,
            max_tool_rounds=max_tool_rounds,
        )
        # Normalize to our ToolCallResult type
        return ToolCallResult(
            text=result.text,
            tool_calls=result.tool_calls,
            tool_results=result.tool_results,
            raw_response=result.raw_response,
        )

    # Legacy mode
    if _text_backend() == "gemini":
        from llm.gemini_example import run_prompt_with_tools as _run
    else:
        from llm.gpt_example import run_prompt_with_tools as _run
    return _run(
        model_mode, prompt,
        system_prompt=system_prompt,
        image_base64=image_base64,
        few_shot_messages=few_shot_messages,
        tools=tools,
        tool_choice=tool_choice,
        tool_executor=tool_executor,
        max_tool_rounds=max_tool_rounds,
    )


def check_connections() -> list[dict]:
    """Check auth for all configured LLM providers.

    Returns a list of dicts, one per unique provider instance, each with
    keys: ``ok``, ``provider``, ``message``, ``error``.

    Only checks providers that were initialized via ``init_from_config()``.
    """
    results: list[dict] = []
    checked: set[str] = set()

    for provider_name, instance in _provider_instances.items():
        if provider_name in checked:
            continue
        checked.add(provider_name)
        try:
            result = instance.check_auth()
        except Exception as e:
            result = {
                "ok": False,
                "provider": instance.__class__.__name__,
                "message": f"Auth check error for {provider_name}",
                "error": str(e),
            }
        results.append(result)

    return results


def encode_image_to_data_url(file_path) -> str:
    """Encode an image file to a data URL. Backend-independent (same logic)."""
    from llm.gpt_example import encode_image_to_data_url as _enc
    return _enc(file_path)
