"""Anthropic provider — Claude models via the Anthropic API."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from llm.providers import LLMProvider, ToolCallResult, register_provider


@register_provider("anthropic")
class AnthropicProvider(LLMProvider):
    """Provider for Anthropic's Claude API."""

    capabilities = {
        "text": True,
        "tools": True,
        "vision": True,
        "image_gen": False,
        "embeddings": False,
    }

    def __init__(self, config: dict | None = None):
        config = config or {}
        api_key_env = config.get("api_key_env", "ANTHROPIC_API_KEY")
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise RuntimeError(
                f"Anthropic API key not found. Set {api_key_env} environment variable. "
                f"Install with: pip install 'happyfigure[anthropic]'"
            )
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package not installed. Install with: pip install 'happyfigure[anthropic]'")
        self._client = anthropic.Anthropic(api_key=api_key)

    def check_auth(self) -> dict:
        """Verify Anthropic credentials by sending a minimal prompt."""
        name = self.__class__.__name__
        try:
            self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return {"ok": True, "provider": name, "message": "Anthropic auth OK", "error": None}
        except Exception as e:
            err = str(e)
            # Authentication errors are definitive; allow known model lookup
            # failures because they still imply the credentials were accepted.
            if "authentication" in err.lower() or "invalid" in err.lower():
                return {"ok": False, "provider": name, "message": "Anthropic auth failed", "error": err}
            if "model" in err.lower() and (
                "not found" in err.lower() or "unknown" in err.lower() or "does not exist" in err.lower()
            ):
                return {"ok": True, "provider": name, "message": "Anthropic auth OK", "error": None}
            return {"ok": False, "provider": name, "message": "Anthropic auth failed", "error": err}

    def run_prompt(
        self,
        model: str,
        prompt: str,
        *,
        system_prompt: str | None = None,
        image_base64: str | None = None,
        few_shot_messages: list[dict] | None = None,
    ) -> str:
        messages = _build_messages(few_shot_messages, prompt, image_base64)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": 16384,
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        response = self._client.messages.create(**kwargs)
        return _extract_text(response)

    def run_image_prompt(
        self,
        model: str,
        prompt: str,
        *,
        reference_images: list[str] | None = None,
    ) -> bytes | None:
        raise NotImplementedError(
            "Anthropic does not support image generation. Use a different provider for the 'drawing' role."
        )

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
        messages = _build_messages(few_shot_messages, prompt, image_base64)
        anthropic_tools = _convert_tools(tools)

        all_tool_calls: list[dict] = []
        all_tool_results: list = []
        response = None

        for _ in range(max_tool_rounds):
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": 16384,
            }
            if system_prompt:
                kwargs["system"] = system_prompt
            if anthropic_tools:
                kwargs["tools"] = anthropic_tools

            response = self._client.messages.create(**kwargs)

            # Check for tool_use blocks
            tool_use_blocks = [block for block in response.content if block.type == "tool_use"]

            if not tool_use_blocks:
                return ToolCallResult(
                    text=_extract_text(response),
                    tool_calls=all_tool_calls,
                    tool_results=all_tool_results,
                    raw_response=response,
                )

            if tool_executor is None:
                return ToolCallResult(
                    text="",
                    tool_calls=[{"name": b.name, "arguments": b.input} for b in tool_use_blocks],
                    tool_results=[{"error": "no tool_executor provided"}] * len(tool_use_blocks),
                    raw_response=response,
                )

            # Append assistant message with tool use
            messages.append({"role": "assistant", "content": response.content})

            # Execute tools and build tool_result message
            tool_results_content = []
            for block in tool_use_blocks:
                result = tool_executor(block.name, block.input)
                all_tool_calls.append({"name": block.name, "arguments": block.input})
                all_tool_results.append(result)
                tool_results_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    }
                )
            messages.append({"role": "user", "content": tool_results_content})

        text = _extract_text(response) if response else ""
        return ToolCallResult(
            text=text,
            tool_calls=all_tool_calls,
            tool_results=all_tool_results,
            raw_response=response,
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _build_messages(
    few_shot_messages: list[dict] | None,
    prompt: str,
    image_base64: str | None,
) -> list[dict]:
    """Convert to Anthropic message format."""
    messages: list[dict] = []

    if few_shot_messages:
        for msg in few_shot_messages:
            role = msg.get("role", "user")
            content_items = msg.get("content", [])
            if isinstance(content_items, str):
                messages.append({"role": role, "content": content_items})
            else:
                parts = []
                for item in content_items:
                    item_type = item.get("type", "")
                    if item_type in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "text": item.get("text", "")})
                    elif item_type in ("input_image", "image"):
                        img_url = item.get("image_url", "")
                        if img_url and "," in img_url:
                            header, b64_data = img_url.split(",", 1)
                            media_type = header.split(":")[1].split(";")[0]
                            parts.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": b64_data,
                                    },
                                }
                            )
                if parts:
                    messages.append({"role": role, "content": parts})

    # Main user message
    user_content: list[dict] = [{"type": "text", "text": prompt}]
    if image_base64 and "," in image_base64:
        header, b64_data = image_base64.split(",", 1)
        media_type = header.split(":")[1].split(";")[0]
        user_content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64_data,
                },
            }
        )
    messages.append({"role": "user", "content": user_content})
    return messages


def _extract_text(response) -> str:
    """Extract text from Anthropic response."""
    for block in response.content:
        if block.type == "text":
            return block.text
    raise ValueError("Anthropic response has no text content")


def _convert_tools(tools: list[dict] | None) -> list[dict] | None:
    """Convert internal tool schema to Anthropic format."""
    if not tools:
        return None
    anthropic_tools = []
    for tool in tools:
        anthropic_tools.append(
            {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "input_schema": tool.get("parameters", tool.get("input_schema", {})),
            }
        )
    return anthropic_tools
