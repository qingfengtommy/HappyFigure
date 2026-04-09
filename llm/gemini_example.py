# Gemini backend — drop-in replacement for gpt_example.py
#
# Install:
#   pip install google-genai python-dotenv
#
# .env file (in repo root):
#   GEMINI_API_KEY=your-api-key-here

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path

try:
    from google import genai
    from google.genai import types
except ImportError:
    raise ImportError(
        "google-genai is required for the Gemini backend. "
        "Install it with: pip install happyfigure[google]"
    )
from dotenv import load_dotenv

# Load API key from .env
load_dotenv()
_CLIENTS: dict[tuple[str, ...], genai.Client] = {}


def _get_client(api_key_env: str = "GEMINI_API_KEY") -> genai.Client:
    """Lazy-init the Gemini client. Supports API key or Vertex AI auth."""
    api_key = os.environ.get(api_key_env, "")
    if api_key:
        cache_key = ("api", api_key_env, api_key)
        if cache_key not in _CLIENTS:
            _CLIENTS[cache_key] = genai.Client(api_key=api_key)
        return _CLIENTS[cache_key]

    # Vertex AI fallback — uses GOOGLE_APPLICATION_CREDENTIALS / ADC
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    if not project:
        raise RuntimeError(
            f"Neither {api_key_env} nor GOOGLE_CLOUD_PROJECT is set. "
            f"Set {api_key_env} for API key auth, or set "
            "GOOGLE_CLOUD_PROJECT + GOOGLE_APPLICATION_CREDENTIALS "
            "for Vertex AI."
        )
    cache_key = ("vertex", project, location)
    if cache_key not in _CLIENTS:
        _CLIENTS[cache_key] = genai.Client(
            vertexai=True,
            project=project,
            location=location,
        )
    return _CLIENTS[cache_key]


def get_model_name(model_mode: str) -> str:
    """Return Gemini model name by mode."""
    model_map = {
        "chat": os.environ.get("GEMINI_MODEL_CHAT", "gemini-3.1-pro-preview"),
        "code": os.environ.get("GEMINI_MODEL_CODE", "gemini-3.1-pro-preview"),
        "drawing": os.environ.get("GEMINI_MODEL_DRAWING", "gemini-3.1-flash-image-preview"),
    }
    return model_map.get(model_mode, model_mode)


def encode_image_to_data_url(file_path: str | Path) -> str:
    """Read an image file and return a ``data:<mime>;base64,...`` URL.

    Same interface as gpt_example — the pipeline stores these URLs and passes
    them to run_prompt via image_base64.
    """
    p = Path(file_path)
    mime, _ = mimetypes.guess_type(str(p))
    if mime is None:
        mime = "image/png"
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _parse_data_url(data_url: str) -> tuple[str, bytes]:
    """Parse ``data:<mime>;base64,<b64>`` → (mime_type, raw_bytes)."""
    header, b64_data = data_url.split(",", 1)
    mime = header.split(":")[1].split(";")[0]
    raw = base64.b64decode(b64_data)
    return mime, raw


def _build_contents(
    few_shot_messages: list[dict] | None,
    prompt: str,
    image_base64: str | None,
) -> list[types.Content]:
    """Convert OpenAI-style messages to google.genai Content objects."""
    contents: list[types.Content] = []

    # Few-shot examples
    if few_shot_messages:
        for msg in few_shot_messages:
            role = msg.get("role", "user")
            gemini_role = "model" if role == "assistant" else "user"
            parts: list[types.Part] = []

            content_items = msg.get("content", [])
            if isinstance(content_items, str):
                parts.append(types.Part.from_text(text=content_items))
            else:
                for item in content_items:
                    item_type = item.get("type", "")
                    if item_type in ("input_text", "output_text", "text"):
                        parts.append(types.Part.from_text(text=item.get("text", "")))
                    elif item_type in ("input_image", "image"):
                        img_url = item.get("image_url", "")
                        if img_url and "," in img_url:
                            mime, raw = _parse_data_url(img_url)
                            parts.append(types.Part.from_bytes(
                                data=raw, mime_type=mime,
                            ))

            if parts:
                contents.append(types.Content(role=gemini_role, parts=parts))

    # Main user message
    user_parts: list[types.Part] = [types.Part.from_text(text=prompt)]
    if image_base64 and "," in image_base64:
        mime, raw = _parse_data_url(image_base64)
        user_parts.append(types.Part.from_bytes(data=raw, mime_type=mime))
    contents.append(types.Content(role="user", parts=user_parts))

    return contents


def run_image_prompt(
    prompt: str,
    *,
    reference_images: list[str] | None = None,
    model_name: str | None = None,
    api_key_env: str = "GEMINI_API_KEY",
) -> bytes | None:
    """Generate an image using the drawing model.

    Args:
        prompt: Text description of the image to generate.
        reference_images: Optional list of data-URL strings (from encode_image_to_data_url)
                          to include as style references.
        model_name: Model to use. If None, falls back to get_model_name("drawing").

    Returns:
        PNG image bytes, or None if generation failed.
    """
    client = _get_client(api_key_env=api_key_env)
    if not model_name:
        model_name = get_model_name("drawing")

    # Build content parts
    parts: list[types.Part] = []

    # Include reference images if provided
    if reference_images:
        parts.append(types.Part.from_text(
            text="Use these images as style references for the diagram you will generate:",
        ))
        for ref_url in reference_images:
            if ref_url and "," in ref_url:
                mime, raw = _parse_data_url(ref_url)
                parts.append(types.Part.from_bytes(data=raw, mime_type=mime))
        parts.append(types.Part.from_text(
            text=f"\nNow generate the following diagram:\n\n{prompt}",
        ))
    else:
        parts.append(types.Part.from_text(text=prompt))

    response = client.models.generate_content(
        model=model_name,
        contents=types.Content(role="user", parts=parts),
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            httpOptions=types.HttpOptions(timeout=180_000),  # 3 min timeout
        ),
    )

    # Extract image bytes from response parts
    if response.candidates:
        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                return part.inline_data.data

    return None


def run_prompt(
    model_mode: str,
    prompt: str,
    *,
    system_prompt: str | None = None,
    image_base64: str | None = None,
    few_shot_messages: list[dict] | None = None,
    api_key_env: str = "GEMINI_API_KEY",
) -> str:
    """Send prompt to Gemini; return output text.

    Same interface as gpt_example.run_prompt so the pipeline can use either
    backend without code changes.
    """
    client = _get_client(api_key_env=api_key_env)
    model_name = get_model_name(model_mode or "chat")

    # Build contents
    contents = _build_contents(few_shot_messages, prompt, image_base64)

    # Build config
    config = types.GenerateContentConfig(
        system_instruction=system_prompt if system_prompt else None,
    )

    response = client.models.generate_content(
        model=model_name,
        contents=contents,
        config=config,
    )

    # Extract text
    if not response.text:
        raise ValueError("Gemini response has no text content")
    return response.text


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
    api_key_env: str = "GEMINI_API_KEY",
):
    """Send prompt with tool-calling support (agentic loop).

    Uses Google Gemini's generate_content with function calling.
    """
    from llm import ToolCallResult

    client = _get_client(api_key_env=api_key_env)
    model_name = get_model_name(model_mode or "chat")

    # Convert tools to Gemini format
    gemini_tools = None
    if tools:
        try:
            from tools.tool_schemas import to_gemini_tools
            gemini_tools = to_gemini_tools(tools)
        except ImportError:
            import logging
            logging.getLogger(__name__).warning(
                "tools.tool_schemas not available; passing tools as-is to Gemini API"
            )
            gemini_tools = tools  # assume already in Gemini format

    # Build initial contents
    contents = _build_contents(few_shot_messages, prompt, image_base64)

    # Build config
    config_kwargs: dict = {}
    if system_prompt:
        config_kwargs["system_instruction"] = system_prompt
    if gemini_tools is not None:
        config_kwargs["tools"] = gemini_tools
    if tool_choice is not None:
        config_kwargs["tool_config"] = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=tool_choice)
        )
    config = types.GenerateContentConfig(**config_kwargs)

    # Agentic loop
    all_tool_calls: list[dict] = []
    all_tool_results: list = []
    response = None

    for _round in range(max_tool_rounds):
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config,
        )

        # Check for function calls in response parts
        candidates = response.candidates or []
        if not candidates or not candidates[0].content or not candidates[0].content.parts:
            return ToolCallResult(
                text=response.text or "",
                tool_calls=all_tool_calls,
                tool_results=all_tool_results,
                raw_response=response,
            )
        function_calls = [
            p for p in candidates[0].content.parts
            if p.function_call is not None
        ]

        if not function_calls:
            return ToolCallResult(
                text=response.text or "",
                tool_calls=all_tool_calls,
                tool_results=all_tool_results,
                raw_response=response,
            )

        if tool_executor is None:
            return ToolCallResult(
                text="",
                tool_calls=[{"name": p.function_call.name, "arguments": dict(p.function_call.args)} for p in function_calls],
                tool_results=[{"error": "no tool_executor provided"}] * len(function_calls),
                raw_response=response,
            )

        # Execute each function call and build responses
        function_responses = []
        for part in function_calls:
            fc = part.function_call
            args = dict(fc.args)
            result = tool_executor(fc.name, args)
            all_tool_calls.append({"name": fc.name, "arguments": args})
            all_tool_results.append(result)
            function_responses.append(
                types.Part.from_function_response(name=fc.name, response=result)
            )

        # Append model response and function results to conversation
        contents.append(response.candidates[0].content)
        contents.append(types.Content(role="user", parts=function_responses))

        # Clear tool_choice after first round
        if "tool_config" in config_kwargs:
            del config_kwargs["tool_config"]
            config = types.GenerateContentConfig(**config_kwargs)

    # Max rounds reached — return what we have
    text = ""
    if response is not None:
        text = response.text or ""
    return ToolCallResult(
        text=text,
        tool_calls=all_tool_calls,
        tool_results=all_tool_results,
        raw_response=response,
    )


if __name__ == "__main__":
    print(run_prompt("chat", "What is the capital of France?"))
