"""Claude Code platform adapter.

Generates ``.claude/agents/<name>.md`` files from shared prompts and launches
agents via ``claude -p --agent <name>``. Claude Code has built-in
Bash/Read/Glob/Grep tools matching what the agent prompts expect.
"""
from __future__ import annotations

import json
import logging
from shutil import which

from agents import AgentCommand, OrchestratorBase, PROJECT_ROOT, register_platform

logger = logging.getLogger(__name__)

# Claude-specific behavioral constraints appended to agent prompts.
# Claude explores the codebase and shortcuts to automated pipelines rather than
# following its own step-by-step instructions.  These suffixes override that.
_CLAUDE_AGENT_CONSTRAINTS: dict[str, str] = {
    "svg-builder": """

---

## IMPORTANT — Execution Constraints (Claude-specific)

You MUST follow Steps 1–14 in this prompt sequentially. Do NOT explore the
codebase looking for shortcuts or automated pipelines.

Specifically:
- **Do NOT run `pipeline_cli.py svg-pipeline`** — that is the automated
  LangGraph pipeline. You ARE the pipeline; execute each step yourself.
- **Do NOT spawn Agent subagents** to explore how the pipeline works.
- The only `pipeline_cli.py` subcommands you may call are:
  `services`, `image-generate`, and `icon-replace`.
- All output files (figure.png, template.svg, final.svg, etc.) go directly
  in `<run_dir>/`, NOT in a subdirectory.
""",
}


@register_platform("claude")
class ClaudeCodeOrchestrator(OrchestratorBase):
    """Claude Code platform adapter."""

    @property
    def model_display(self) -> str:
        claude_cfg = self.config.get("agent", {}).get("claude", {})
        return claude_cfg.get("model", "claude-sonnet-4-6")

    def check_auth(self) -> dict:
        name = "claude"
        if not which("claude"):
            return {"ok": False, "platform": name, "message": "Claude Code CLI not found", "error": "Install from https://docs.anthropic.com/en/docs/claude-code"}
        # Claude Code manages its own auth (OAuth / API key) — check via `claude --version`
        import subprocess
        try:
            proc = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                return {"ok": False, "platform": name, "message": "Claude CLI error", "error": proc.stderr.strip()}
        except Exception as e:
            return {"ok": False, "platform": name, "message": "Claude CLI check failed", "error": str(e)}
        return {"ok": True, "platform": name, "message": "Claude Code CLI ready", "error": None}

    def setup(self, run_dir: str, *, mode: str = "", execution: str = "") -> None:
        self._run_dir = run_dir
        self._store_mode(mode, execution)

        if not which("claude"):
            raise RuntimeError(
                "Claude Code CLI not found. Install from: "
                "https://docs.anthropic.com/en/docs/claude-code"
            )

        claude_cfg = self.config.get("agent", {}).get("claude", {})
        # Claude CLI uses hyphens in model IDs (claude-opus-4-6), not dots
        raw_model = claude_cfg.get("model", "claude-sonnet-4-6")
        self._model = raw_model.replace(".", "-")

        # Generate .claude/agents/<name>.md for each agent.
        # Claude Code agents use YAML frontmatter for metadata.
        agents_dir = PROJECT_ROOT / ".claude" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        for agent_name in self.list_agents():
            prompt_body = self.get_agent_prompt(agent_name, mode=mode, execution=execution)
            # Build frontmatter with tool restrictions
            tools = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
            if agent_name == "code-agent":
                # code-agent needs Agent scoped to figure-critic only
                tools.append("Agent(figure-critic)")
            elif agent_name == "happyfigure-orchestrator":
                # orchestrator can spawn any subagent
                tools.append("Agent")
            frontmatter = (
                "---\n"
                f"name: {agent_name}\n"
                f"description: {agent_name} agent for HappyFigure pipeline\n"
                f"tools: {', '.join(tools)}\n"
                "---\n\n"
            )

            # Claude-specific behavioral constraints.
            # Claude tends to explore the codebase and call automated pipelines
            # instead of following its step-by-step instructions.
            suffix = _CLAUDE_AGENT_CONSTRAINTS.get(agent_name, "")

            agent_file = agents_dir / f"{agent_name}.md"
            self._write_if_changed(agent_file, frontmatter + prompt_body + suffix)

        logger.info(
            "Generated %d Claude agent files in %s",
            len(self.list_agents()), agents_dir,
        )

    def build_agent_command(self, agent_name: str, prompt: str) -> AgentCommand:
        model = getattr(self, "_model", self.model_display)

        # Use acceptEdits permission mode: auto-allows file reads/writes,
        # prompts for bash.  Layer --settings on top to also allow Bash(*)
        # and Agent (for code-agent subagent delegation).
        allow = ["Bash(*)", "Read", "Write", "Edit", "Glob", "Grep"]
        if agent_name in {"code-agent", "happyfigure-orchestrator"}:
            allow.append("Agent")
        settings = json.dumps({"permissions": {"allow": allow}})

        cmd = [
            "claude",
            "-p",                          # non-interactive (print mode)
            "--agent", agent_name,         # use .claude/agents/<name>.md
            "--model", model,
            "--verbose",                   # required for stream-json
            "--output-format", "stream-json",
            "--permission-mode", "acceptEdits",
            "--settings", settings,
            "--",                           # terminate flags; prompt follows
            prompt,
        ]
        return AgentCommand(cmd=cmd, stream_format="claude-stream-json")

    def cleanup(self) -> None:
        """Remove generated Claude agent files."""
        self._cleanup_generated_files()
        super().cleanup()
