"""Google Gemini provider — delegates to existing gemini_example.py."""

from __future__ import annotations

import os

from llm.providers import LLMProvider, ToolCallResult, register_provider


@register_provider("google")
class GoogleProvider(LLMProvider):
    """Provider for Google Gemini API.

    Delegates to the existing ``llm.gemini_example`` module which handles
    Gemini-specific message format conversion and content building.
    """

    capabilities = {
        "text": True,
        "tools": True,
        "vision": True,
        "image_gen": True,
        "embeddings": True,
    }

    def __init__(self, config: dict | None = None):
        config = config or {}
        self._api_key_env = config.get("api_key_env", "GEMINI_API_KEY")
        if not os.environ.get(self._api_key_env, "") and not os.environ.get("GOOGLE_CLOUD_PROJECT", ""):
            raise RuntimeError(
                f"Gemini API key not found. Set {self._api_key_env} environment variable, "
                f"or set GOOGLE_CLOUD_PROJECT + GOOGLE_APPLICATION_CREDENTIALS "
                f"for Vertex AI auth. Install with: pip install 'happyfigure[google]'"
            )
        # Import check — fail fast if google-genai not installed
        try:
            from google import genai  # noqa: F401
        except ImportError:
            raise ImportError("google-genai package not installed. Install with: pip install 'happyfigure[google]'")

    def check_auth(self) -> dict:
        """Verify Gemini credentials by listing models."""
        name = self.__class__.__name__
        try:
            from llm.gemini_example import _get_client

            client = _get_client(api_key_env=self._api_key_env)
            # Lightweight call: list models (paginated, gets first page only)
            next(iter(client.models.list(config={"page_size": 1})))
            return {"ok": True, "provider": name, "message": "Gemini auth OK", "error": None}
        except Exception as e:
            return {"ok": False, "provider": name, "message": "Gemini auth failed", "error": str(e)}

    def run_prompt(
        self,
        model: str,
        prompt: str,
        *,
        system_prompt: str | None = None,
        image_base64: str | None = None,
        few_shot_messages: list[dict] | None = None,
    ) -> str:
        from llm.gemini_example import run_prompt

        return run_prompt(
            model,
            prompt,
            system_prompt=system_prompt,
            image_base64=image_base64,
            few_shot_messages=few_shot_messages,
            api_key_env=self._api_key_env,
        )

    def run_image_prompt(self, model: str, prompt: str, *, reference_images: list[str] | None = None) -> bytes | None:
        from llm.gemini_example import run_image_prompt

        return run_image_prompt(
            prompt, reference_images=reference_images, model_name=model, api_key_env=self._api_key_env
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
        tool_executor=None,
        max_tool_rounds: int = 5,
    ) -> ToolCallResult:
        from llm.gemini_example import run_prompt_with_tools as _run

        result = _run(
            model,
            prompt,
            system_prompt=system_prompt,
            image_base64=image_base64,
            few_shot_messages=few_shot_messages,
            tools=tools,
            tool_choice=tool_choice,
            tool_executor=tool_executor,
            max_tool_rounds=max_tool_rounds,
            api_key_env=self._api_key_env,
        )
        return ToolCallResult(
            text=result.text,
            tool_calls=result.tool_calls,
            tool_results=result.tool_results,
            raw_response=result.raw_response,
        )
