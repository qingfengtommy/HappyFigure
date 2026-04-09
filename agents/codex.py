"""Codex (OpenAI) platform adapter.

Generates ``.codex/agents/<name>.toml`` files from shared prompts and launches
agents via ``codex exec``.  Codex has built-in shell, file-patching tools,
and native subagent support (agents can spawn other project-scoped agents).
"""

from __future__ import annotations

import logging
import os
from shutil import which

from agents import AgentCommand, OrchestratorBase, PROJECT_ROOT, register_platform

logger = logging.getLogger(__name__)

# Linux bubblewrap errors that indicate the sandbox cannot start
# (e.g. inside containers or restrictive AppArmor profiles).
_BWRAP_STARTUP_ERRORS = (
    "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted",
    "bwrap: setting up uid map: Permission denied",
    "bwrap: Creating new namespace failed: Operation not permitted",
)

# Per-agent metadata for TOML generation.
_AGENT_META = {
    "happyfigure-orchestrator": {
        "description": "Main HappyFigure orchestrator session.",
        "sandbox_mode": "workspace-write",
    },
    "data-explore": {"description": "Data and experiment exploration agent.", "sandbox_mode": "workspace-write"},
    "code-explore": {"description": "Code and config exploration agent.", "sandbox_mode": "workspace-write"},
    "exp-explore": {"description": "Experiment exploration agent.", "sandbox_mode": "workspace-write"},
    "planner-stylist": {"description": "Plans and styles figures.", "sandbox_mode": "workspace-write"},
    "style-variant": {"description": "Produces beam-search style variants.", "sandbox_mode": "workspace-write"},
    "code-agent": {"description": "Generates and iterates figure code.", "sandbox_mode": "workspace-write"},
    "figure-critic": {"description": "Evaluates figures on quality rubric.", "sandbox_mode": "read-only"},
    "method-explore": {"description": "Method exploration for SVG diagrams.", "sandbox_mode": "workspace-write"},
    "svg-author": {"description": "Agent-driven SVG author.", "sandbox_mode": "workspace-write"},
    "svg-builder": {"description": "SVG builder with segmentation.", "sandbox_mode": "workspace-write"},
    "svg-refiner": {"description": "SVG refinement agent.", "sandbox_mode": "workspace-write"},
    "build": {"description": "Full-access dev agent.", "sandbox_mode": "workspace-write"},
}


def _to_toml_string(value: str) -> str:
    """Escape a string for TOML (use triple-quoted for multi-line)."""
    if "\n" in value:
        # Use TOML multi-line basic string (triple quotes)
        escaped = value.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        return f'"""\n{escaped}"""'
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@register_platform("codex")
class CodexOrchestrator(OrchestratorBase):
    """Codex CLI platform adapter."""

    @property
    def model_display(self) -> str:
        codex_cfg = self.config.get("agent", {}).get("codex", {})
        return codex_cfg.get("model", "gpt-5.4")

    def check_auth(self) -> dict:
        name = "codex"
        if not which("codex"):
            return {
                "ok": False,
                "platform": name,
                "message": "Codex CLI not found",
                "error": "Install from https://github.com/openai/codex",
            }
        if os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"):
            return {"ok": True, "platform": name, "message": "Codex CLI ready (API key detected)", "error": None}
        return {
            "ok": True,
            "platform": name,
            "message": "Codex CLI ready (auth managed by Codex login or environment)",
            "error": None,
        }

    def setup(self, run_dir: str, *, mode: str = "", execution: str = "") -> None:
        self._run_dir = run_dir
        self._store_mode(mode, execution)

        if not which("codex"):
            raise RuntimeError("Codex CLI not found. Install from: https://github.com/openai/codex")

        codex_cfg = self.config.get("agent", {}).get("codex", {})
        self._model = codex_cfg.get("model", "gpt-5.4")
        self._sandbox_mode = codex_cfg.get("sandbox_mode", "workspace-write")
        self._retry_dangerous_on_sandbox_failure = codex_cfg.get("retry_dangerous_on_sandbox_failure", True)
        self._reasoning_effort = codex_cfg.get("reasoning_effort", None)
        self._reasoning_summary = codex_cfg.get("reasoning_summary", None)

        # Generate .codex/agents/<name>.toml per agent.
        agents_dir = PROJECT_ROOT / ".codex" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)

        for agent_name in self.list_agents():
            prompt_body = self.get_agent_prompt(agent_name, mode=mode, execution=execution)
            meta = _AGENT_META.get(
                agent_name,
                {
                    "description": f"{agent_name} agent.",
                    "sandbox_mode": "workspace-write",
                },
            )

            lines = [
                f'name = "{agent_name}"',
                f"description = {_to_toml_string(meta['description'])}",
                f'model = "{self._model}"',
                f'sandbox_mode = "{meta["sandbox_mode"]}"',
                f"developer_instructions = {_to_toml_string(prompt_body)}",
            ]

            agent_file = agents_dir / f"{agent_name}.toml"
            self._write_if_changed(agent_file, "\n".join(lines) + "\n")

        logger.info(
            "Generated %d Codex agent files in %s",
            len(self.list_agents()),
            agents_dir,
        )

    def _build_exec_command(self, prompt: str, sandbox_mode: str) -> AgentCommand:
        model = self._model
        cmd = [
            "codex",
            "-a",
            "never",  # truly non-interactive (top-level flag)
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-m",
            model,
            "-s",
            sandbox_mode,
        ]
        if self._reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{self._reasoning_effort}"'])
        if self._reasoning_summary:
            cmd.extend(["-c", f'model_reasoning_summary="{self._reasoning_summary}"'])
        cmd.append(prompt)
        return AgentCommand(cmd=cmd, stream_format="codex-json")

    def build_agent_command(self, agent_name: str, prompt: str) -> AgentCommand:
        composed_prompt = self.compose_agent_prompt(agent_name, prompt)
        return self._build_exec_command(composed_prompt, self._sandbox_mode)

    def cleanup(self) -> None:
        """Remove generated Codex agent files."""
        self._cleanup_generated_files()
        super().cleanup()

    def build_fallback_agent_command(self, prompt: str) -> AgentCommand | None:
        if not self._retry_dangerous_on_sandbox_failure:
            return None
        if self._sandbox_mode == "danger-full-access":
            return None
        return self._build_exec_command(prompt, "danger-full-access")

    def should_retry_with_dangerous_sandbox(self, output_text: str) -> bool:
        if not self._retry_dangerous_on_sandbox_failure:
            return False
        return any(sig in output_text for sig in _BWRAP_STARTUP_ERRORS)
