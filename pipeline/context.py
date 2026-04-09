"""RunnerContext — typed holder replacing the global ``_orchestrator``."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents import OrchestratorBase

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RunnerContext:
    """Immutable-ish bag of state shared across all pipeline steps."""

    orchestrator: OrchestratorBase
    config: dict = field(default_factory=dict)
    verbose: bool = False
    llm_preset: str | None = None
    critic_available: bool = True

    # Convenience proxies
    @property
    def platform_name(self) -> str:
        return self.orchestrator.platform_name

    @property
    def model_display(self) -> str:
        return self.orchestrator.model_display


# Module-level singleton — set once in main(), read everywhere else.
_ctx: RunnerContext | None = None


def get_ctx() -> RunnerContext:
    """Return the current RunnerContext (raises if not initialized)."""
    if _ctx is None:
        raise RuntimeError("RunnerContext not initialized. Call set_ctx() first.")
    return _ctx


def set_ctx(ctx: RunnerContext) -> None:
    global _ctx
    _ctx = ctx
