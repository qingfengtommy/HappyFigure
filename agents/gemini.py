"""Gemini CLI platform adapter.

Generates ``.gemini/agents/<name>.md`` files with YAML frontmatter and launches
agents via ``gemini -p``.  Gemini CLI has built-in shell, file, and web tools,
plus native subagent support (agents in ``.gemini/agents/`` are exposed as
tools to the main agent and can be invoked via ``@agent-name``).
"""

from __future__ import annotations

import logging
import os
from shutil import which

from agents import AgentCommand, OrchestratorBase, PROJECT_ROOT, register_platform

logger = logging.getLogger(__name__)

# Per-agent metadata for Gemini YAML frontmatter.
_AGENT_META = {
    "happyfigure-orchestrator": {"description": "Main HappyFigure orchestrator session."},
    "data-explore": {"description": "Data and experiment exploration agent."},
    "code-explore": {"description": "Code and config exploration agent."},
    "exp-explore": {"description": "Experiment exploration agent."},
    "planner-stylist": {"description": "Plans and styles figures."},
    "style-variant": {"description": "Produces beam-search style variants."},
    "code-agent": {"description": "Generates and iterates figure code."},
    "figure-critic": {"description": "Evaluates figures on quality rubric."},
    "method-explore": {"description": "Method exploration for SVG diagrams."},
    "svg-author": {"description": "Agent-driven SVG author."},
    "svg-builder": {"description": "SVG builder with segmentation."},
    "svg-refiner": {"description": "SVG refinement agent."},
    "build": {"description": "Full-access dev agent."},
}

# Tool lists per agent role.
_AGENT_TOOLS = {
    "figure-critic": ["ReadFile", "GrepSearch"],
    # All other agents get full tool access (wildcard).
}


@register_platform("gemini")
class GeminiOrchestrator(OrchestratorBase):
    """Gemini CLI platform adapter."""

    @property
    def model_display(self) -> str:
        gemini_cfg = self.config.get("agent", {}).get("gemini", {})
        return gemini_cfg.get("model", "gemini-3.1-pro-preview")

    def check_auth(self) -> dict:
        name = "gemini"
        if not which("gemini"):
            return {
                "ok": False,
                "platform": name,
                "message": "Gemini CLI not found",
                "error": "Install from https://github.com/google-gemini/gemini-cli",
            }
        if os.environ.get("GEMINI_API_KEY"):
            return {"ok": True, "platform": name, "message": "Gemini CLI ready (API key detected)", "error": None}
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_CLOUD_PROJECT"):
            return {"ok": True, "platform": name, "message": "Gemini CLI ready (Vertex AI env detected)", "error": None}
        return {
            "ok": True,
            "platform": name,
            "message": "Gemini CLI ready (auth managed by Gemini CLI or environment)",
            "error": None,
        }

    def setup(self, run_dir: str, *, mode: str = "", execution: str = "") -> None:
        self._run_dir = run_dir
        self._store_mode(mode, execution)

        if not which("gemini"):
            raise RuntimeError("Gemini CLI not found. Install from: https://github.com/google-gemini/gemini-cli")

        gemini_cfg = self.config.get("agent", {}).get("gemini", {})
        self._model = gemini_cfg.get("model", "gemini-3.1-pro-preview")

        # Generate .gemini/agents/<name>.md per agent.
        # Gemini CLI reads these as project-scoped subagents, exposed as
        # tools to the main agent.
        agents_dir = PROJECT_ROOT / ".gemini" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)

        for agent_name in self.list_agents():
            prompt_body = self.get_agent_prompt(agent_name, mode=mode, execution=execution)
            meta = _AGENT_META.get(
                agent_name,
                {
                    "description": f"{agent_name} agent.",
                },
            )
            tools = _AGENT_TOOLS.get(agent_name, ["*"])
            tools_yaml = "\n".join(f"  - {t}" for t in tools)

            frontmatter = (
                f"---\n"
                f"name: {agent_name}\n"
                f"description: {meta['description']}\n"
                f"kind: local\n"
                f"model: {self._model}\n"
                f"tools:\n{tools_yaml}\n"
                f"max_turns: 30\n"
                f"---\n\n"
            )

            agent_file = agents_dir / f"{agent_name}.md"
            self._write_if_changed(agent_file, frontmatter + prompt_body)

        logger.info(
            "Generated %d Gemini agent files in %s",
            len(self.list_agents()),
            agents_dir,
        )

    def build_agent_command(self, agent_name: str, prompt: str) -> AgentCommand:
        model = getattr(self, "_model", self.model_display)
        composed_prompt = self.compose_agent_prompt(agent_name, prompt)
        cmd = [
            "gemini",
            "-m",
            model,
            "-o",
            "stream-json",
            "-p",
            composed_prompt,
        ]
        return AgentCommand(cmd=cmd, stream_format="gemini-stream-json")

    def cleanup(self) -> None:
        """Remove generated Gemini agent files."""
        self._cleanup_generated_files()
        super().cleanup()
