"""Unified agent orchestrator for HappyFigure.

Supports multiple agent platforms (OpenCode, Claude Code, Codex, Gemini)
through a common interface. Shared agent prompts live in ``prompts/agents/``
and platform adapters generate platform-specific configs at runtime.

Usage::

    from agents import create_orchestrator

    orch = create_orchestrator("claude", config)
    orch.setup(run_dir)
    cmd = orch.build_agent_command("data-explore", "Explore data...")
    # run cmd.cmd with subprocess
"""

from __future__ import annotations

__all__ = [
    "AgentCommand",
    "OrchestratorBase",
    "create_orchestrator",
    "list_platforms",
    "register_platform",
]

import dataclasses
import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "agents"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclasses.dataclass
class AgentCommand:
    """Platform-specific command to launch an agent."""

    cmd: list[str]
    env: dict[str, str] | None = None  # Extra env vars (merged with os.environ)
    input_text: str | None = None  # Stdin text (e.g., Claude piped prompt)
    cwd: str | None = None  # Working directory override
    stream_format: str | None = None  # "claude-stream-json" to parse Claude streaming events
    use_pty: bool = False  # Use pseudo-TTY (for CLIs that suppress output without TTY)
    silent_stdout: bool = False  # Discard raw stdout/stderr (used when another monitor supplies progress)
    metadata: dict[str, str] | None = None


class OrchestratorBase(ABC):
    """Base class for agent platform adapters.

    Each adapter knows how to:
    - Generate platform-specific config from shared agent prompts
    - Build the CLI command to launch an agent
    - Clean up generated configs
    """

    def __init__(self, config: dict):
        """Initialize with pipeline config (from pipeline.yaml)."""
        self.config = config
        self._run_dir: str | None = None
        self._platform_name: str = "unknown"
        self._mode: str = ""
        self._execution: str = ""

    @property
    def platform_name(self) -> str:
        """Registered platform name (e.g., 'opencode', 'claude')."""
        return self._platform_name

    @property
    def model_display(self) -> str:
        """Human-readable model string for logging."""
        return "unknown"

    def check_auth(self) -> dict:
        """Verify that the agent CLI is installed and credentials are available.

        Returns a dict with:
          - ``ok`` (bool): True if the CLI is ready
          - ``platform`` (str): platform name
          - ``message`` (str): human-readable status
          - ``error`` (str | None): error detail on failure
        """
        return {
            "ok": True,
            "platform": self._platform_name,
            "message": "auth check not implemented (assumed OK)",
            "error": None,
        }

    @abstractmethod
    def setup(self, run_dir: str, *, mode: str = "", execution: str = "") -> None:
        """Generate platform-specific config files."""
        ...

    def _store_mode(self, mode: str, execution: str) -> None:
        """Store mode/execution so compose_agent_prompt can inject fragments."""
        self._mode = mode
        self._execution = execution

    @abstractmethod
    def build_agent_command(self, agent_name: str, prompt: str) -> AgentCommand:
        """Build the CLI command to launch an agent.

        Called by ``cli._run_agent()`` which handles subprocess streaming,
        logging, and exit code reporting uniformly across all platforms.
        """
        ...

    def run_agent(
        self,
        agent_name: str,
        prompt: str,
        *,
        log_dir: str | None = None,
        verbose: bool = False,
    ) -> int:
        """Launch an agent and wait for completion. Returns exit code.

        Default implementation uses ``build_agent_command`` + subprocess.
        Prefer using ``build_agent_command`` from ``cli._run_agent()``
        for streaming output support.
        """
        import os
        import subprocess

        ac = self.build_agent_command(agent_name, prompt)
        env = {**os.environ, **(ac.env or {})}
        proc = subprocess.run(
            ac.cmd,
            input=ac.input_text,
            text=True if ac.input_text else None,
            cwd=ac.cwd or str(PROJECT_ROOT),
            env=env,
            stdout=None if verbose else subprocess.PIPE,
            stderr=None if verbose else subprocess.PIPE,
        )
        return proc.returncode

    def cleanup(self) -> None:
        """Remove generated platform config files."""
        if self._run_dir:
            platform_dir = Path(self._run_dir) / ".platform"
            if platform_dir.exists():
                shutil.rmtree(platform_dir, ignore_errors=True)

    def _write_if_changed(self, path: Path, content: str) -> bool:
        """Write *content* to *path* only if the file is missing or differs.

        Tracks written paths in ``_generated_files`` so ``cleanup()`` only
        removes files that were actually generated (not user-created ones).
        Returns True if the file was written, False if it was already up-to-date.
        """
        if not hasattr(self, "_generated_files"):
            self._generated_files: list[Path] = []
        self._generated_files.append(path)
        if path.exists() and path.read_text() == content:
            return False
        path.write_text(content)
        return True

    def _cleanup_generated_files(self) -> None:
        """Remove only the files written by ``_write_if_changed``."""
        for path in getattr(self, "_generated_files", []):
            if path.exists():
                path.unlink()
        self._generated_files = []

    def get_agent_prompt(self, agent_name: str, *, mode: str = "", execution: str = "") -> str:
        """Read a shared agent prompt from prompts/agents/.

        For ``happyfigure-orchestrator``, replaces ``<!-- WORKFLOW_FRAGMENT -->``
        and ``<!-- CODE_FRAGMENT -->`` markers with mode-specific content from
        ``prompts/agents/orchestrator_fragments/``.  Other agents are returned
        as-is.
        """
        prompt_file = PROMPTS_DIR / f"{agent_name}.md"
        if not prompt_file.exists():
            raise FileNotFoundError(
                f"Agent prompt not found: {prompt_file}. Available: {[p.stem for p in PROMPTS_DIR.glob('*.md')]}"
            )
        text = prompt_file.read_text()

        if agent_name == "happyfigure-orchestrator" and mode:
            fragments_dir = PROMPTS_DIR / "orchestrator_fragments"
            # Select workflow fragment
            if mode == "exp_plot":
                workflow = (fragments_dir / "workflow_plot.md").read_text()
            elif mode == "paper_composite":
                workflow = (fragments_dir / "workflow_paper_composite.md").read_text()
            else:
                workflow = (fragments_dir / "workflow_diagram.md").read_text()

            # Select CODE fragment
            code_fragment_map = {
                ("exp_plot", "sequential"): "code_sequential.md",
                ("exp_plot", "parallel"): "code_parallel.md",
                ("exp_plot", "beam"): "code_beam.md",
                ("composite", ""): "diagram_composite.md",
                ("agent_svg", ""): "diagram_sketch.md",
                ("paper_composite", ""): "code_paper_composite.md",
            }
            code_key = (mode, execution if mode == "exp_plot" else "")
            code_file = code_fragment_map.get(code_key, "code_sequential.md")
            code_fragment = (fragments_dir / code_file).read_text()

            text = text.replace("<!-- WORKFLOW_FRAGMENT -->", workflow)
            text = text.replace("<!-- CODE_FRAGMENT -->", code_fragment)

        return text

    def compose_agent_prompt(self, agent_name: str, prompt: str) -> str:
        """Combine agent instructions with a runtime task prompt.

        Adapters that cannot explicitly select a project-scoped agent should
        use this helper so the requested agent identity is still applied.
        Uses stored mode/execution from setup() for fragment injection.
        """
        prompt_body = self.get_agent_prompt(
            agent_name,
            mode=self._mode,
            execution=self._execution,
        ).strip()
        task = prompt.strip()
        return f"{prompt_body}\n\n---\n\n## Runtime Task\n\n{task}\n"

    def list_agents(self) -> list[str]:
        """Return names of all available agent prompts."""
        return sorted(p.stem for p in PROMPTS_DIR.glob("*.md"))


# ── Platform Registry ────────────────────────────────────────────────

_PLATFORMS: dict[str, type[OrchestratorBase]] = {}


def register_platform(name: str):
    """Decorator to register a platform adapter."""

    def decorator(cls: type[OrchestratorBase]):
        _PLATFORMS[name] = cls
        cls._registered_name = name
        return cls

    return decorator


def create_orchestrator(platform: str, config: dict | None = None) -> OrchestratorBase:
    """Create an orchestrator for the given platform."""
    _ensure_loaded()
    if platform not in _PLATFORMS:
        available = ", ".join(sorted(_PLATFORMS.keys()))
        raise ValueError(f"Unknown platform: {platform!r}. Available: {available}")
    orch = _PLATFORMS[platform](config or {})
    orch._platform_name = platform
    return orch


def list_platforms() -> list[str]:
    """Return names of all registered platforms."""
    _ensure_loaded()
    return sorted(_PLATFORMS.keys())


_loaded = False


def _ensure_loaded():
    global _loaded
    if _loaded:
        return
    _loaded = True
    import importlib

    for mod in [
        "agents.opencode",
        "agents.claude_code",
        "agents.codex",
        "agents.gemini",
        "agents.copilot",
    ]:
        try:
            importlib.import_module(mod)
        except ImportError as e:
            logger.debug("Platform %s not available: %s", mod, e)
