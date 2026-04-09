"""AWS Bedrock provider — supports Anthropic Claude and Amazon models via Bedrock."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from llm.providers import LLMProvider, ToolCallResult, register_provider


@register_provider("bedrock")
class BedrockProvider(LLMProvider):
    """AWS Bedrock provider using boto3.

    Uses standard AWS credential chain (env vars, ~/.aws/credentials, IAM role).
    Supports Anthropic Claude models on Bedrock with the Messages API.
    """

    capabilities = {
        "text": True,
        "tools": True,
        "vision": True,
        "image_gen": False,
        "embeddings": True,
    }

    def __init__(self, config: dict | None = None):
        config = config or {}
        region_env = config.get("region_env", "AWS_DEFAULT_REGION")
        region = os.environ.get(region_env, "us-east-1")

        try:
            import boto3
        except ImportError:
            raise ImportError("boto3 package not installed. Install with: pip install 'happyfigure[bedrock]'")

        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region,
        )
        self._region = region

    def check_auth(self) -> dict:
        """Verify AWS Bedrock credentials using STS GetCallerIdentity."""
        name = self.__class__.__name__
        try:
            import boto3

            sts = boto3.client("sts", region_name=self._region)
            identity = sts.get_caller_identity()
            acct = identity.get("Account", "unknown")
            return {
                "ok": True,
                "provider": name,
                "message": f"Bedrock auth OK (account {acct})",
                "error": None,
            }
        except Exception as e:
            return {"ok": False, "provider": name, "message": "Bedrock auth failed", "error": str(e)}

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
        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "messages": messages,
            "max_tokens": 16384,
        }
        if system_prompt:
            body["system"] = system_prompt

        response = self._client.invoke_model(
            modelId=model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
        result = json.loads(response["body"].read())
        return _extract_text(result)

    def run_image_prompt(
        self,
        model: str,
        prompt: str,
        *,
        reference_images: list[str] | None = None,
    ) -> bytes | None:
        raise NotImplementedError(
            "Bedrock image generation not yet implemented. Use OpenAI or Google provider for the 'drawing' role."
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
        result = None

        for _ in range(max_tool_rounds):
            body: dict[str, Any] = {
                "anthropic_version": "bedrock-2023-05-31",
                "messages": messages,
                "max_tokens": 16384,
            }
            if system_prompt:
                body["system"] = system_prompt
            if anthropic_tools:
                body["tools"] = anthropic_tools

            response = self._client.invoke_model(
                modelId=model,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )
            result = json.loads(response["body"].read())
            content = result.get("content", [])

            tool_use_blocks = [b for b in content if b.get("type") == "tool_use"]
            if not tool_use_blocks:
                return ToolCallResult(
                    text=_extract_text(result),
                    tool_calls=all_tool_calls,
                    tool_results=all_tool_results,
                    raw_response=result,
                )

            if tool_executor is None:
                return ToolCallResult(
                    text="",
                    tool_calls=[{"name": b["name"], "arguments": b["input"]} for b in tool_use_blocks],
                    tool_results=[{"error": "no tool_executor provided"}] * len(tool_use_blocks),
                    raw_response=result,
                )

            messages.append({"role": "assistant", "content": content})
            tool_results_content = []
            for block in tool_use_blocks:
                res = tool_executor(block["name"], block["input"])
                all_tool_calls.append({"name": block["name"], "arguments": block["input"]})
                all_tool_results.append(res)
                tool_results_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": json.dumps(res, default=str),
                    }
                )
            messages.append({"role": "user", "content": tool_results_content})

        text = _extract_text(result) if result else ""
        return ToolCallResult(
            text=text,
            tool_calls=all_tool_calls,
            tool_results=all_tool_results,
            raw_response=result,
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _build_messages(
    few_shot_messages: list[dict] | None,
    prompt: str,
    image_base64: str | None,
) -> list[dict]:
    """Build Anthropic-format messages for Bedrock."""
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
                                    "source": {"type": "base64", "media_type": media_type, "data": b64_data},
                                }
                            )
                if parts:
                    messages.append({"role": role, "content": parts})

    user_content: list[dict] = [{"type": "text", "text": prompt}]
    if image_base64 and "," in image_base64:
        header, b64_data = image_base64.split(",", 1)
        media_type = header.split(":")[1].split(";")[0]
        user_content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64_data},
            }
        )
    messages.append({"role": "user", "content": user_content})
    return messages


def _extract_text(result: dict) -> str:
    for block in result.get("content", []):
        if block.get("type") == "text":
            return block["text"]
    raise ValueError("Bedrock response has no text content")


def _convert_tools(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    return [
        {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "input_schema": t.get("parameters", t.get("input_schema", {})),
        }
        for t in tools
    ]
