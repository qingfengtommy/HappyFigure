"""OpenAI provider — standard OpenAI API (api.openai.com)."""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from llm.providers import LLMProvider, ToolCallResult, register_provider


@register_provider("openai")
class OpenAIProvider(LLMProvider):
    """Provider for OpenAI's API (GPT-4o, o3, gpt-image-1, etc.)."""

    capabilities = {
        "text": True,
        "tools": True,
        "vision": True,
        "image_gen": True,
        "embeddings": True,
    }

    def __init__(self, config: dict | None = None):
        config = config or {}
        api_key_env = config.get("api_key_env", "OPENAI_API_KEY")
        base_url = config.get("base_url")

        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise RuntimeError(
                f"OpenAI API key not found. Set {api_key_env} environment variable."
            )

        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "openai package not installed. Install with: pip install openai"
            )

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def check_auth(self) -> dict:
        """Verify OpenAI credentials by listing models."""
        name = self.__class__.__name__
        try:
            self._client.models.list(limit=1)
            return {"ok": True, "provider": name, "message": "OpenAI auth OK", "error": None}
        except Exception as e:
            return {"ok": False, "provider": name, "message": "OpenAI auth failed", "error": str(e)}

    def run_prompt(
        self,
        model: str,
        prompt: str,
        *,
        system_prompt: str | None = None,
        image_base64: str | None = None,
        few_shot_messages: list[dict] | None = None,
    ) -> str:
        messages = _build_messages(system_prompt, few_shot_messages, prompt, image_base64)
        response = self._client.responses.create(model=model, input=messages)
        return _get_response_text(response)

    def run_image_prompt(
        self,
        model: str,
        prompt: str,
        *,
        reference_images: list[str] | None = None,
    ) -> bytes | None:
        import base64

        full_prompt = prompt
        if reference_images:
            full_prompt = (
                "Generate a scientific architecture diagram. "
                "Use a clean, professional style similar to reference images provided.\n\n"
                f"{prompt}"
            )

        try:
            from graphs.svg_utils import load_pipeline_config
            img_size = load_pipeline_config().get("image_generation", {}).get("size", "1024x1024")
        except ImportError:
            img_size = "1024x1024"

        response = self._client.images.generate(
            model=model, prompt=full_prompt, n=1, size=img_size, quality="high",
        )
        if not response.data:
            return None
        item = response.data[0]
        if getattr(item, "b64_json", None):
            return base64.b64decode(item.b64_json)
        if getattr(item, "url", None):
            import urllib.request
            with urllib.request.urlopen(item.url) as resp:
                return resp.read()
        return None

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
        openai_tools = _convert_tools(tools)
        messages = _build_messages(system_prompt, few_shot_messages, prompt, image_base64)

        all_tool_calls: list[dict] = []
        all_tool_results: list = []
        response = None

        for _ in range(max_tool_rounds):
            kwargs: dict = {"model": model, "input": messages}
            if openai_tools:
                kwargs["tools"] = openai_tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice

            response = self._client.responses.create(**kwargs)
            output = response.output or []
            function_calls = [item for item in output if item.type == "function_call"]

            if not function_calls:
                return ToolCallResult(
                    text=_get_response_text(response),
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

            for fc in function_calls:
                try:
                    args = json.loads(fc.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                result = tool_executor(fc.name, args)
                all_tool_calls.append({"name": fc.name, "arguments": args})
                all_tool_results.append(result)
                messages.append(fc)
                messages.append({
                    "type": "function_call_output",
                    "call_id": fc.call_id,
                    "output": json.dumps(result, default=str),
                })

            tool_choice = None

        text = ""
        if response is not None:
            try:
                text = _get_response_text(response)
            except ValueError:
                pass
        return ToolCallResult(
            text=text, tool_calls=all_tool_calls,
            tool_results=all_tool_results, raw_response=response,
        )


# ── Helpers ──────────────────────────────────────────────────────────

def _build_messages(
    system_prompt: str | None,
    few_shot_messages: list[dict] | None,
    prompt: str,
    image_base64: str | None,
) -> list[dict]:
    messages: list[dict] = []
    if system_prompt:
        messages.append({
            "role": "developer",
            "content": [{"type": "input_text", "text": system_prompt}],
        })
    if few_shot_messages:
        messages.extend(few_shot_messages)
    user_content: list[dict] = [{"type": "input_text", "text": prompt}]
    if image_base64:
        user_content.append({"type": "input_image", "image_url": image_base64})
    messages.append({"role": "user", "content": user_content})
    return messages


def _get_response_text(response) -> str:
    text = getattr(response, "output_text", None)
    if text is None or (isinstance(text, str) and not text.strip()):
        raise ValueError("response has no output_text")
    return text


def _convert_tools(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    try:
        from tools.tool_schemas import to_openai_tools
        return to_openai_tools(tools)
    except ImportError:
        return tools
