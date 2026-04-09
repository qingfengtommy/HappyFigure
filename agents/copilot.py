"""GitHub Copilot CLI platform adapter (EXPERIMENTAL).

Generates ``.github/agents/<name>.md`` agent profiles and launches agents via
``gh copilot``.  This adapter is experimental — the Copilot CLI does not yet
expose a stable non-interactive agent execution interface.  The generated
agent files follow the ``.github/agents/`` convention used by GitHub Copilot
Extensions but may not be consumed by the current CLI version.
"""

from __future__ import annotations

import logging
import os
from shutil import which

from agents import AgentCommand, OrchestratorBase, PROJECT_ROOT, register_platform

logger = logging.getLogger(__name__)


@register_platform("copilot")
class CopilotOrchestrator(OrchestratorBase):
    """GitHub Copilot CLI platform adapter (EXPERIMENTAL).

    The Copilot CLI does not yet expose a stable non-interactive agent
    execution interface.  This adapter generates agent profiles and
    constructs a best-effort CLI command, but it may not work with the
    current version of the Copilot CLI.
    """

    @property
    def model_display(self) -> str:
        copilot_cfg = self.config.get("agent", {}).get("copilot", {})
        return copilot_cfg.get("model", "gpt-5.4")

    def check_auth(self) -> dict:
        name = "copilot"
        if not which("copilot"):
            return {
                "ok": False,
                "platform": name,
                "message": "Copilot CLI not found",
                "error": "Install from https://github.com/github/copilot-cli",
            }
        # Copilot CLI authenticates via GitHub token or gh CLI session
        if os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
            return {
                "ok": True,
                "platform": name,
                "message": "Copilot CLI ready (GitHub token detected)",
                "error": None,
            }
        return {
            "ok": True,
            "platform": name,
            "message": "Copilot CLI ready (auth managed by Copilot CLI or gh session)",
            "error": None,
        }

    def setup(self, run_dir: str, *, mode: str = "", execution: str = "") -> None:
        self._run_dir = run_dir
        self._store_mode(mode, execution)

        if not which("copilot"):
            raise RuntimeError("Copilot CLI not found. Install from: https://github.com/github/copilot-cli")

        copilot_cfg = self.config.get("agent", {}).get("copilot", {})
        self._model = copilot_cfg.get("model", "gpt-5.4")

        # Generate .github/agents/<name>.md per agent.
        # Copilot CLI reads these as project-scoped custom agent profiles.
        agents_dir = PROJECT_ROOT / ".github" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)

        for agent_name in self.list_agents():
            prompt_body = self.get_agent_prompt(agent_name, mode=mode, execution=execution)
            agent_file = agents_dir / f"{agent_name}.md"
            self._write_if_changed(
                agent_file,
                f"---\n"
                f"name: {agent_name}\n"
                f"description: {agent_name} agent for HappyFigure pipeline\n"
                f"---\n\n"
                f"{prompt_body}",
            )

        logger.info(
            "Generated %d Copilot agent files in %s",
            len(self.list_agents()),
            agents_dir,
        )

    def build_agent_command(self, agent_name: str, prompt: str) -> AgentCommand:
        model = getattr(self, "_model", self.model_display)
        cmd = [
            "copilot",
            f"--agent={agent_name}",
            "-p",
            prompt,
            "--model",
            model,
            "--allow-all-tools",
            "--output-format",
            "json",
        ]
        return AgentCommand(cmd=cmd, stream_format="copilot-json")

    def cleanup(self) -> None:
        """Remove generated Copilot agent files."""
        self._cleanup_generated_files()
        super().cleanup()
