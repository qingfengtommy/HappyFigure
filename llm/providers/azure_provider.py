"""Azure OpenAI provider."""

from __future__ import annotations

import os

from llm.providers import LLMProvider, ToolCallResult, register_provider


@register_provider("azure")
class AzureProvider(LLMProvider):
    """Azure OpenAI provider with API key or credential-chain auth.

    Auth priority:
    1. AZURE_OPENAI_API_KEY env var
    2. Azure CLI / Managed Identity credential chain (requires azure-identity)
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
        self._api_key_env = config.get("api_key_env", "AZURE_OPENAI_API_KEY")

        if os.environ.get(self._api_key_env):
            return
        try:
            import azure.identity  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                f"Azure auth not configured. Set {self._api_key_env}, "
                "or install 'happyfigure[azure]' for Azure CLI / Managed Identity auth."
            ) from exc

    def check_auth(self) -> dict:
        """Verify Azure OpenAI credentials and endpoint configuration."""
        name = self.__class__.__name__

        # 1. Credential presence
        api_key = os.environ.get(self._api_key_env, "")
        if not api_key:
            try:
                import azure.identity  # noqa: F401
            except ImportError:
                return {
                    "ok": False,
                    "provider": name,
                    "message": "Azure auth failed",
                    "error": f"No credentials: set {self._api_key_env}, or install azure-identity",
                }

        # 2. Check endpoint URL.
        try:
            from llm.gpt_example import _resolve_base_url

            base_url = _resolve_base_url()
        except Exception:
            base_url = ""

        cred_type = "API key" if api_key else "Azure identity"

        if not base_url or base_url.rstrip("/") in ("", "/openai/v1"):
            return {
                "ok": True,
                "provider": name,
                "message": f"Azure credentials present ({cred_type}), no endpoint in env (agents may configure their own)",
                "error": None,
            }

        # 3. Verify client construction
        try:
            from llm.gpt_example import get_azure_client

            get_azure_client(api_key_env=self._api_key_env)
        except Exception as e:
            return {"ok": False, "provider": name, "message": "Azure client init failed", "error": str(e)}

        return {"ok": True, "provider": name, "message": f"Azure auth OK ({cred_type})", "error": None}

    def run_prompt(self, model, prompt, *, system_prompt=None, image_base64=None, few_shot_messages=None) -> str:
        from llm.gpt_example import run_prompt

        return run_prompt(
            model,
            prompt,
            system_prompt=system_prompt,
            image_base64=image_base64,
            few_shot_messages=few_shot_messages,
            api_key_env=self._api_key_env,
        )

    def run_image_prompt(self, model, prompt, *, reference_images=None) -> bytes | None:
        from llm.gpt_example import run_image_prompt

        return run_image_prompt(
            prompt, reference_images=reference_images, model_name=model, api_key_env=self._api_key_env
        )

    def run_prompt_with_tools(
        self,
        model,
        prompt,
        *,
        system_prompt=None,
        image_base64=None,
        few_shot_messages=None,
        tools=None,
        tool_choice=None,
        tool_executor=None,
        max_tool_rounds=5,
    ) -> ToolCallResult:
        from llm.gpt_example import run_prompt_with_tools as _run

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
        # Convert from llm.ToolCallResult to providers.ToolCallResult
        return ToolCallResult(
            text=result.text,
            tool_calls=result.tool_calls,
            tool_results=result.tool_results,
            raw_response=result.raw_response,
        )
