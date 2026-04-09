"""Priority-ordered prompt composition for Python-built task prompts.

This module handles only the dynamic task message that Python passes to agent
CLIs.  Static agent identity (``prompts/agents/*.md``) stays with the platform
adapters and is NOT managed here.

Sections are ordered by priority (lower = earlier in the composed output).
Stable-across-experiments content comes first so that LLM prefix caching
is maximised.

A section can be ``"inline"`` (text embedded in the prompt) or
``"path_ref"`` (a hint telling the agent where to read the full file).
``add_bundled`` auto-selects: inline when content is short, path_ref when it
exceeds ``max_lines``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace


# ---------------------------------------------------------------------------
# Section dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptSection:
    """One named section of a composed task prompt."""

    name: str
    content: str
    priority: int
    mode: str = "inline"  # "inline" | "path_ref"
    path: str | None = None  # file path (only meaningful for path_ref)
    max_chars: int | None = None


# ---------------------------------------------------------------------------
# Budget reduction policy
# ---------------------------------------------------------------------------

# (section_name, strategy)
# Tried in order; first match that is still inline gets reduced.
BUDGET_REDUCTION_ORDER: list[tuple[str, str]] = [
    ("prior_feedback", "truncate"),
    ("human_feedback_refs", "path_ref"),
    ("spec_content", "path_ref"),
    ("global_style", "path_ref"),
    # "task" and "context" are never reduced.
]


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


class PromptComposer:
    """Build a task prompt from priority-ordered sections."""

    def __init__(self) -> None:
        self._sections: dict[str, PromptSection] = {}
        # Preserve insertion order as a tie-breaker within the same priority.
        self._insert_order: dict[str, int] = {}
        self._counter = 0

    # -- mutators (return self for chaining) --------------------------------

    def add(self, section: PromptSection) -> "PromptComposer":
        self._sections[section.name] = section
        if section.name not in self._insert_order:
            self._insert_order[section.name] = self._counter
            self._counter += 1
        return self

    def add_bundled(
        self,
        name: str,
        path: str,
        content: str,
        priority: int,
        *,
        max_lines: int | None = None,
    ) -> "PromptComposer":
        """Inline *content* if it fits within *max_lines*, else emit a path ref."""
        if max_lines is not None and len(content.splitlines()) > max_lines:
            line_count = len(content.splitlines())
            hint = (
                f"The full content is at {path} ({line_count} lines). "
                f"Read the file if you need it."
            )
            return self.add(PromptSection(
                name=name, content=hint, priority=priority,
                mode="path_ref", path=path,
            ))
        # Inline — include the raw content with clear delimiters.
        wrapped = (
            f"The content of {os.path.basename(path)} is included below — "
            f"do NOT re-read {path} from disk.\n\n"
            f"--- BEGIN {os.path.basename(path)} ---\n"
            f"{content}\n"
            f"--- END {os.path.basename(path)} ---"
        )
        return self.add(PromptSection(
            name=name, content=wrapped, priority=priority,
            mode="inline", path=path,
        ))

    def remove(self, name: str) -> "PromptComposer":
        self._sections.pop(name, None)
        return self

    def replace(self, name: str, content: str) -> "PromptComposer":
        old = self._sections.get(name)
        if old is not None:
            self._sections[name] = replace(old, content=content)
        return self

    def get(self, name: str) -> PromptSection | None:
        return self._sections.get(name)

    # -- path-ref / truncation helpers --------------------------------------

    def replace_with_path_ref(self, name: str) -> "PromptComposer":
        """Convert an inline section to a path reference (lossless)."""
        section = self._sections.get(name)
        if section is None or section.mode == "path_ref" or not section.path:
            return self
        line_count = len(section.content.splitlines())
        hint = (
            f"The full content is at {section.path} ({line_count} lines). "
            f"Read the file if you need it."
        )
        self._sections[name] = replace(section, content=hint, mode="path_ref")
        return self

    def truncate_section(self, name: str, target_chars: int) -> "PromptComposer":
        """Destructively truncate a section to *target_chars*."""
        section = self._sections.get(name)
        if section is None or len(section.content) <= target_chars:
            return self
        truncated = section.content[:target_chars].rsplit("\n", 1)[0] + "\n[...truncated]"
        self._sections[name] = replace(section, content=truncated)
        return self

    # -- output -------------------------------------------------------------

    def compose(self) -> str:
        """Return the final prompt with sections sorted by priority."""
        ordered = sorted(
            self._sections.values(),
            key=lambda s: (s.priority, self._insert_order.get(s.name, 0)),
        )
        parts = [s.content for s in ordered if s.content]
        return "\n\n".join(parts)

    def estimate_tokens(self) -> int:
        """Rough token estimate (1 token ≈ 3.5 chars)."""
        total_chars = sum(len(s.content) for s in self._sections.values())
        return int(total_chars / 3.5)

    def section_names(self) -> list[str]:
        """Return section names in compose order (for test assertions)."""
        ordered = sorted(
            self._sections.values(),
            key=lambda s: (s.priority, self._insert_order.get(s.name, 0)),
        )
        return [s.name for s in ordered]


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def apply_budget(
    composer: PromptComposer,
    budget_tokens: int = 30_000,
) -> PromptComposer:
    """Reduce sections in :data:`BUDGET_REDUCTION_ORDER` until within budget.

    Prefers lossless path-ref conversion over destructive truncation.
    """
    for name, strategy in BUDGET_REDUCTION_ORDER:
        if composer.estimate_tokens() <= budget_tokens:
            break
        section = composer.get(name)
        if section is None or section.mode == "path_ref":
            continue
        if strategy == "path_ref" and section.path:
            composer.replace_with_path_ref(name)
        elif strategy == "truncate":
            target = section.max_chars or 3000
            composer.truncate_section(name, target_chars=target)
    return composer
