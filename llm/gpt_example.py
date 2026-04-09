# Azure OpenAI-compatible backend.  Configure via environment variables:
#   AZURE_OPENAI_ENDPOINT  — base URL
#   AZURE_OPENAI_API_KEY   — auth (or use Azure CLI / Managed Identity)

from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path

from openai import OpenAI

# Default endpoint (read from env at import time — lightweight)
_DEFAULT_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()

# Authentication — deferred to first use so importing this module in non-Azure
# environments (e.g. Gemini backend, tests) doesn't trigger credential errors.
_credential = None


def _get_credential():
    """Lazy-init the Azure credential provider."""
    global _credential
    if _credential is None:
        try:
            from azure.identity import (
                ChainedTokenCredential,
                AzureCliCredential,
                ManagedIdentityCredential,
                get_bearer_token_provider,
            )
        except ImportError as exc:
            raise ImportError(
                "azure-identity is required for Azure CLI / Managed Identity auth. "
                "Install with: pip install 'happyfigure[azure]', or set "
                "AZURE_OPENAI_API_KEY."
            ) from exc
        scope = os.environ.get("AZURE_OPENAI_SCOPE", "https://cognitiveservices.azure.com/.default")
        _credential = get_bearer_token_provider(
            ChainedTokenCredential(AzureCliCredential(), ManagedIdentityCredential()),
            scope,
        )
    return _credential


def _normalize_openai_base_url(url: str) -> str:
    """Normalize Azure/OpenAI-compatible base URLs for the OpenAI client."""
    base = url.rstrip("/")
    if base.endswith("/openai/v1"):
        return base
    if base.endswith("/openai"):
        return f"{base}/v1"
    return f"{base}/openai/v1"


def _resolve_base_url() -> str:
    """Resolve the API base URL for Azure OpenAI."""
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip() or _DEFAULT_ENDPOINT
    if not endpoint:
        raise ValueError("No Azure OpenAI endpoint configured. Set AZURE_OPENAI_ENDPOINT environment variable.")
    return _normalize_openai_base_url(endpoint)


def get_azure_client(
    *,
    api_key_env: str = "AZURE_OPENAI_API_KEY",
) -> OpenAI:
    """Build and return an Azure OpenAI-compatible client.

    Auth priority:
    1. API key from ``api_key_env``
    2. Azure CLI / Managed Identity via ``azure-identity``
    """
    base_url = _resolve_base_url()
    api_key = os.environ.get(api_key_env)
    if api_key:
        return OpenAI(base_url=base_url, api_key=api_key)
    return OpenAI(base_url=base_url, api_key=_get_credential()())


def get_model_name(model_mode: str) -> str:
    """Return model name by mode: chat, code, drawing."""
    model_map = {
        "chat": "gpt-5.4_2026-03-05",
        "code": "gpt-5.4_2026-03-05",
        "drawing": "gpt-image-1",
    }
    return model_map.get(model_mode, model_mode)


def get_response_text(response) -> str:
    """Extract output text from response; raise if missing so callers can handle."""
    text = getattr(response, "output_text", None)
    if text is None or (isinstance(text, str) and not text.strip()):
        raise ValueError("response has no output_text")
    return text


def encode_image_to_data_url(file_path: str | Path) -> str:
    """Read an image file and return a ``data:<mime>;base64,...`` URL."""
    p = Path(file_path)
    mime, _ = mimetypes.guess_type(str(p))
    if mime is None:
        mime = "image/png"
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def run_image_prompt(
    prompt: str,
    *,
    reference_images: list[str] | None = None,
    model_name: str | None = None,
    api_key_env: str = "AZURE_OPENAI_API_KEY",
) -> bytes | None:
    """Generate an image using the Azure OpenAI images API.

    Args:
        prompt: Text description of the image to generate.
        reference_images: Optional list of data-URL strings to include as
                          reference context (appended to prompt text since
                          the images API only accepts text prompts).
        model_name: Model to use. If None, falls back to get_model_name("drawing").

    Returns:
        PNG image bytes, or None if generation failed.
    """
    client = get_azure_client(api_key_env=api_key_env)
    if not model_name:
        model_name = get_model_name("drawing")

    # Build the prompt — include reference context if provided
    full_prompt = prompt
    if reference_images:
        full_prompt = (
            "Generate a scientific architecture diagram. "
            f"Use a clean, professional style similar to reference images provided.\n\n"
            f"{prompt}"
        )

    try:
        from graphs.svg_utils import load_pipeline_config

        _img_size = load_pipeline_config().get("image_generation", {}).get("size", "1024x1024")
        response = client.images.generate(
            model=model_name,
            prompt=full_prompt,
            n=1,
            size=_img_size,
            quality="high",
        )
    except Exception as e:
        raise RuntimeError(f"Azure image generation failed: {e}") from e

    if not response.data:
        return None

    item = response.data[0]
    # Try b64_json first (returned by some models directly), then download from URL
    if getattr(item, "b64_json", None):
        return base64.b64decode(item.b64_json)
    if getattr(item, "url", None):
        import urllib.request

        with urllib.request.urlopen(item.url) as resp:
            return resp.read()

    return None


def run_prompt(
    model_mode: str,
    prompt: str,
    *,
    system_prompt: str | None = None,
    image_base64: str | None = None,
    few_shot_messages: list[dict] | None = None,
    api_key_env: str = "AZURE_OPENAI_API_KEY",
) -> str:
    """Send prompt with selected mode; return output text or embeddings.

    Parameters
    ----------
    system_prompt : str | None
        If provided, sent as a ``developer`` role message (Responses API
        equivalent of the ``system`` role).
    image_base64 : str | None
        A ``data:<mime>;base64,...`` URL of an image to include in the user
        message (vision input).
    few_shot_messages : list[dict] | None
        List of few-shot example messages to prepend before the main prompt.
        Each dict should have ``role`` (``"user"`` or ``"assistant"``) and
        ``content`` (list of content items like ``{"type": "input_text", ...}``
        or ``{"type": "input_image", ...}``).
    """
    client = get_azure_client(api_key_env=api_key_env)
    model = model_mode or "chat"
    deployment_name = get_model_name(model)
    # Build message list ------------------------------------------------
    messages: list[dict] = []
    if system_prompt:
        messages.append(
            {
                "role": "developer",
                "content": [{"type": "input_text", "text": system_prompt}],
            }
        )

    # Insert few-shot examples before the main user prompt
    if few_shot_messages:
        messages.extend(few_shot_messages)

    user_content: list[dict] = [{"type": "input_text", "text": prompt}]
    if image_base64:
        user_content.append({"type": "input_image", "image_url": image_base64})
    messages.append({"role": "user", "content": user_content})

    response = client.responses.create(
        model=deployment_name,
        input=messages,
    )
    return get_response_text(response)


def run_prompt_with_tools(
    model_mode: str,
    prompt: str,
    *,
    system_prompt: str | None = None,
    image_base64: str | None = None,
    few_shot_messages: list[dict] | None = None,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    tool_executor=None,
    max_tool_rounds: int = 5,
    api_key_env: str = "AZURE_OPENAI_API_KEY",
):
    """Send prompt with tool-calling support (agentic loop).

    Uses the OpenAI Responses API with function calling.
    """
    from llm import ToolCallResult

    client = get_azure_client(api_key_env=api_key_env)
    model = model_mode or "chat"
    deployment_name = get_model_name(model)

    # Convert tools to OpenAI format
    openai_tools = None
    if tools:
        try:
            from tools.tool_schemas import to_openai_tools

            openai_tools = to_openai_tools(tools)
        except ImportError:
            import logging

            logging.getLogger(__name__).warning("tools.tool_schemas not available; passing tools as-is to OpenAI API")
            openai_tools = tools  # assume already in OpenAI format

    # Build initial message list (same pattern as run_prompt)
    messages: list = []
    if system_prompt:
        messages.append(
            {
                "role": "developer",
                "content": [{"type": "input_text", "text": system_prompt}],
            }
        )
    if few_shot_messages:
        messages.extend(few_shot_messages)

    user_content: list[dict] = [{"type": "input_text", "text": prompt}]
    if image_base64:
        user_content.append({"type": "input_image", "image_url": image_base64})
    messages.append({"role": "user", "content": user_content})

    # Agentic loop
    all_tool_calls: list[dict] = []
    all_tool_results: list = []
    response = None

    for _round in range(max_tool_rounds):
        kwargs: dict = {"model": deployment_name, "input": messages}
        if openai_tools:
            kwargs["tools"] = openai_tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        response = client.responses.create(**kwargs)

        # Check for function calls in output
        output = response.output or []
        function_calls = [item for item in output if item.type == "function_call"]

        if not function_calls:
            # No more tool calls — return final result
            return ToolCallResult(
                text=get_response_text(response),
                tool_calls=all_tool_calls,
                tool_results=all_tool_results,
                raw_response=response,
            )

        if tool_executor is None:
            return ToolCallResult(
                text="",
                tool_calls=[{"name": fc.name, "arguments": json.loads(fc.arguments)} for fc in function_calls],
                tool_results=[{"error": "no tool_executor provided"}] * len(function_calls),
                raw_response=response,
            )

        # Execute each function call
        for fc in function_calls:
            try:
                args = json.loads(fc.arguments)
            except (json.JSONDecodeError, TypeError) as e:
                args = {}
                result = {"error": f"Malformed tool arguments: {e}"}
                all_tool_calls.append({"name": fc.name, "arguments": args})
                all_tool_results.append(result)
                messages.append(fc)
                messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": fc.call_id,
                        "output": json.dumps(result),
                    }
                )
                continue
            result = tool_executor(fc.name, args)
            all_tool_calls.append({"name": fc.name, "arguments": args})
            all_tool_results.append(result)
            # Append the function_call output item and its result
            messages.append(fc)
            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": fc.call_id,
                    "output": json.dumps(result, default=str),
                }
            )

        # Clear tool_choice after first round
        tool_choice = None

    # Max rounds reached — return what we have
    text = ""
    if response is not None:
        try:
            text = get_response_text(response)
        except ValueError:
            text = ""
    return ToolCallResult(
        text=text,
        tool_calls=all_tool_calls,
        tool_results=all_tool_results,
        raw_response=response,
    )
