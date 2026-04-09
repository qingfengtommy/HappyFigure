"""OpenCode platform adapter.

Generates .opencode/agent/*.md files with YAML frontmatter and launches
agents via ``opencode run --agent <name>``.

On ``setup()`` the adapter also patches ``.opencode/opencode.jsonc`` to
inject the real Azure OpenAI endpoint resolved from environment variables
(``AZURE_OPENAI_ENDPOINT``).  The placeholder URL checked into the repo
is restored on ``cleanup()``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import uuid
from pathlib import Path

from shutil import which

from agents import AgentCommand, OrchestratorBase, register_platform

logger = logging.getLogger(__name__)

# Default YAML frontmatter for each agent
_AGENT_META = {
    "happyfigure-orchestrator": {
        "description": "Main HappyFigure orchestrator session.",
        "mode": "primary",
        "color": "#9B59B6",
    },
    "data-explore": {"description": "Data and experiment exploration agent.", "mode": "primary", "color": "#2ECC71"},
    "code-explore": {"description": "Code and config exploration agent.", "mode": "primary", "color": "#1ABC9C"},
    "exp-explore": {"description": "Experiment exploration agent.", "mode": "primary", "color": "#2ECC71"},
    "planner-stylist": {"description": "Plans and styles figures.", "mode": "primary", "color": "#F39C12"},
    "style-variant": {"description": "Produces beam-search style variants.", "mode": "primary", "color": "#F1C40F"},
    "code-agent": {"description": "Generates and iterates figure code.", "mode": "primary", "color": "#2980B9"},
    "figure-critic": {"description": "Evaluates figures on quality rubric.", "mode": "subagent", "color": "#E74C3C"},
    "method-explore": {"description": "Method exploration for SVG diagrams.", "mode": "primary", "color": "#27AE60"},
    "svg-author": {"description": "Agent-driven SVG author.", "mode": "primary", "color": "#16A085"},
    "svg-builder": {"description": "SVG builder with segmentation.", "mode": "primary", "color": "#8E44AD"},
    "svg-refiner": {"description": "SVG refinement agent.", "mode": "primary", "color": "#2980B9"},
    "build": {"description": "Full-access dev agent.", "mode": "primary", "color": "#E74C3C"},
}

# Default permissions for agents
_DEFAULT_PERMISSIONS = {
    "bash": {"*": "allow", "rm *": "ask", "kill *": "ask", "mv *": "ask"},
    "read": {"*": "allow"},
    "glob": {"*": "allow"},
    "grep": {"*": "allow"},
}


@register_platform("opencode")
class OpenCodeOrchestrator(OrchestratorBase):
    """OpenCode platform adapter."""

    @property
    def model_display(self) -> str:
        oc_cfg = self.config.get("agent", {}).get("opencode", {})
        model = oc_cfg.get("model", "gpt-5.4")
        provider = oc_cfg.get("provider", "azure")
        return f"{provider}/{model}"

    def check_auth(self) -> dict:
        name = "opencode"
        if not which("opencode"):
            return {"ok": False, "platform": name, "message": "opencode CLI not found", "error": "Install opencode CLI"}
        # Verify LLM backend credentials (OpenCode typically uses Azure OpenAI)
        oc_cfg = self.config.get("agent", {}).get("opencode", {})
        provider = oc_cfg.get("provider", "azure")
        if "azure" in provider:
            if not os.environ.get("AZURE_OPENAI_API_KEY"):
                try:
                    import azure.identity  # noqa: F401
                except ImportError:
                    return {
                        "ok": False,
                        "platform": name,
                        "message": "OpenCode: no Azure credentials",
                        "error": "Set AZURE_OPENAI_API_KEY, or install azure-identity",
                    }
            endpoint = self._resolve_endpoint()
            if not endpoint:
                return {
                    "ok": False,
                    "platform": name,
                    "message": "OpenCode: no Azure endpoint",
                    "error": "Set AZURE_OPENAI_ENDPOINT",
                }
        return {"ok": True, "platform": name, "message": "OpenCode CLI ready", "error": None}

    def setup(self, run_dir: str, *, mode: str = "", execution: str = "") -> None:
        self._run_dir = run_dir
        self._store_mode(mode, execution)

        project_root = Path(__file__).resolve().parent.parent

        # ── Patch opencode.jsonc with real endpoint from env ────────
        self._patch_opencode_config(project_root)

        # ── Generate agent files into .opencode/agent/ ──────────────
        agent_dir = project_root / ".opencode" / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)

        # Determine model from config: agent.opencode section, fallback to llm.agent_model
        oc_cfg = self.config.get("agent", {}).get("opencode", {})
        provider = oc_cfg.get("provider", "azure")
        model = oc_cfg.get("model", "gpt-5.4")
        model_str = f"{provider}/{model}"

        for agent_name in self.list_agents():
            prompt_body = self.get_agent_prompt(agent_name, mode=mode, execution=execution)
            meta = _AGENT_META.get(
                agent_name,
                {
                    "description": f"{agent_name} agent.",
                    "mode": "primary",
                    "color": "#95A5A6",
                },
            )

            # Build YAML frontmatter
            frontmatter_lines = [
                "---",
                f"description: {meta['description']}",
                f"mode: {meta['mode']}",
                f"model: {model_str}",
                f'color: "{meta["color"]}"',
                "permission:",
            ]
            perms = _DEFAULT_PERMISSIONS
            if meta["mode"] == "subagent":
                perms = {"read": {"*": "allow"}, "bash": {"*": "allow"}}
            for tool, rules in perms.items():
                if len(rules) == 1 and "*" in rules:
                    frontmatter_lines.append(f"  {tool}: {rules['*']}")
                else:
                    frontmatter_lines.append(f"  {tool}:")
                    for pattern, action in rules.items():
                        frontmatter_lines.append(f'    "{pattern}": {action}')
            frontmatter_lines.append("---")

            agent_file = agent_dir / f"{agent_name}.md"
            self._write_if_changed(agent_file, "\n".join(frontmatter_lines) + "\n\n" + prompt_body)

        logger.info("Generated %d OpenCode agent files in %s", len(self.list_agents()), agent_dir)

    def build_agent_command(self, agent_name: str, prompt: str) -> AgentCommand:
        session_title = f"happyfigure::{agent_name}::{uuid.uuid4().hex}"
        return AgentCommand(
            cmd=["opencode", "run", "--title", session_title, "--agent", agent_name, prompt],
            stream_format=None,
            use_pty=False,
            silent_stdout=True,
            metadata={
                "opencode_session_title": session_title,
                "opencode_db_path": os.path.expanduser("~/.local/share/opencode/opencode.db"),
            },
        )

    def run_agent(
        self,
        agent_name: str,
        prompt: str,
        *,
        log_dir: str | None = None,
        verbose: bool = False,
    ) -> int:
        cmd = ["opencode", "run", "--agent", agent_name, prompt]

        stdout_dest = subprocess.PIPE if not verbose else None
        stderr_dest = subprocess.PIPE if not verbose else None

        if log_dir:
            log_path = Path(log_dir) / f"{agent_name}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "w") as log_file:
                proc = subprocess.run(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
        else:
            proc = subprocess.run(
                cmd,
                stdout=stdout_dest,
                stderr=stderr_dest,
            )

        if proc.returncode != 0:
            logger.warning("Agent %s exited with code %d", agent_name, proc.returncode)

        return proc.returncode

    # ── opencode.jsonc endpoint patching ──────────────────────────────

    _opencode_jsonc_original: str | None = None

    def _resolve_endpoint(self) -> str:
        """Resolve the Azure OpenAI base URL.

        Resolution order:
        1. ``AZURE_OPENAI_ENDPOINT`` env var (or the env var named in
           ``llm.providers.azure.endpoint_env``)
        2. ``llm.providers.azure.endpoint`` in ``pipeline.yaml``
        3. Fall back to ``gpt_example._resolve_base_url()``

        Returns an empty string if no endpoint is configured.
        """
        azure_cfg = self.config.get("llm", {}).get("providers", {}).get("azure", {})
        endpoint_env = azure_cfg.get("endpoint_env", "AZURE_OPENAI_ENDPOINT")
        endpoint = os.environ.get(endpoint_env, "").strip()
        if endpoint:
            return endpoint.rstrip("/")

        # Explicit endpoint in pipeline.yaml
        endpoint = azure_cfg.get("endpoint", "").strip()
        if endpoint:
            return endpoint.rstrip("/")

        # Fall back to gpt_example's resolved default
        try:
            from llm.gpt_example import _resolve_base_url

            base_url = _resolve_base_url()
            cleaned = re.sub(r"/openai(/v\d+)?$", "", base_url.rstrip("/"))
            if cleaned and "your-endpoint" not in cleaned and "example.com" not in cleaned:
                return cleaned
        except (ImportError, ValueError):
            pass

        return ""

    def _patch_opencode_config(self, project_root: Path) -> None:
        """Replace the placeholder baseURL in opencode.jsonc with the real endpoint."""
        jsonc_path = project_root / ".opencode" / "opencode.jsonc"
        if not jsonc_path.exists():
            return

        endpoint = self._resolve_endpoint()
        if not endpoint:
            logger.debug("No Azure endpoint in env — skipping opencode.jsonc patch")
            return

        # Normalize to the /openai/v1 suffix OpenCode expects
        base_url = endpoint.rstrip("/")
        if not base_url.endswith("/openai/v1"):
            if base_url.endswith("/openai"):
                base_url += "/v1"
            else:
                base_url += "/openai/v1"

        original = jsonc_path.read_text(encoding="utf-8")
        self._opencode_jsonc_original = original

        # Replace the baseURL value (works for both placeholder and any previous value)
        patched = re.sub(
            r'("baseURL"\s*:\s*)"[^"]*"',
            rf'\1"{base_url}"',
            original,
        )

        if patched != original:
            jsonc_path.write_text(patched, encoding="utf-8")
            logger.info("Patched opencode.jsonc baseURL → %s", base_url)

    def cleanup(self) -> None:
        # Restore original opencode.jsonc if we patched it
        if self._opencode_jsonc_original is not None:
            project_root = Path(__file__).resolve().parent.parent
            jsonc_path = project_root / ".opencode" / "opencode.jsonc"
            jsonc_path.write_text(self._opencode_jsonc_original, encoding="utf-8")
            self._opencode_jsonc_original = None
            logger.debug("Restored original opencode.jsonc")
