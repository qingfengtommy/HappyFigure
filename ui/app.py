"""Unified Rich-based terminal UI for HappyFigure orchestrator.

All styled output goes to stderr. stdout is reserved for machine-readable
output (agent subprocess streams, JSON from pipeline_cli.py).

Respects NO_COLOR, CI, TERM=dumb, and HAPPYFIGURE_OUTPUT env vars.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# ---------------------------------------------------------------------------
# Output mode detection
# ---------------------------------------------------------------------------


def _detect_mode() -> str:
    """Returns 'rich' or 'plain'."""
    explicit = os.environ.get("HAPPYFIGURE_OUTPUT", "").lower()
    if explicit in ("plain", "rich"):
        return explicit
    if os.environ.get("NO_COLOR") or os.environ.get("CI") or os.environ.get("TERM") == "dumb":
        return "plain"
    if not sys.stderr.isatty():
        return "plain"
    return "rich"


_MODE = _detect_mode()
_USE_COLOR = _MODE == "rich"


# ---------------------------------------------------------------------------
# Dynamic terminal size (re-read on every call so resizes take effect)
# ---------------------------------------------------------------------------


def term_width() -> int:
    """Current terminal width, re-queried each call to handle live resizing."""
    return shutil.get_terminal_size((80, 24)).columns


def _panel_width(tw: int | None = None) -> int:
    """Readable content width with a small outer margin and sane max width."""
    tw = tw or term_width()
    usable = max(20, tw - 2)
    return min(tw, max(44, min(132, usable)))


def _compact_layout(tw: int | None = None) -> bool:
    """Whether metadata panels should collapse into a single column."""
    return _panel_width(tw) < 96


def _grid_padding(tw: int | None = None) -> tuple[int, int]:
    """Adaptive grid padding for metadata-heavy panels."""
    return (0, 2) if _compact_layout(tw) else (0, 3)


def _text_budget(prefix_len: int = 0, *, tw: int | None = None, min_width: int = 20, margin: int = 2) -> int:
    """Available text width after subtracting known prefix and margin."""
    width = (tw or term_width()) - prefix_len - margin
    return max(min_width, width)


def _prompt_limit(tw: int | None = None) -> int:
    """Prompt line budget inside agent/session panels."""
    return max(40, _panel_width(tw) - 20)


def _dashboard_name_budget(tw: int | None = None) -> int:
    """Abbreviation budget for progress dashboard experiment names.

    Avoid coupling the budget to experiment count. The dashboard already wraps
    across lines, so shrinking names based on the number of experiments only
    makes labels unreadable without improving layout.
    """
    width = tw or term_width()
    return max(14, min(28, width // 3))


def _truncate_limit() -> int:
    """Adaptive truncation limit for tool-call summaries (leaves room for prefix)."""
    return max(40, _panel_width() - 20)


# ---------------------------------------------------------------------------
# Path shortening — strip project root to show relative paths
# ---------------------------------------------------------------------------

_project_root: str = ""


def set_project_root(root: str) -> None:
    """Set the project root for path shortening in UI output."""
    global _project_root
    _project_root = root.rstrip("/") + "/"


def short_path(path: str) -> str:
    """Shorten an absolute path by stripping the project root prefix."""
    if not _project_root:
        return path
    # Exact match with project root (with or without trailing /)
    root_bare = _project_root.rstrip("/")
    if path == root_bare or path == _project_root:
        return "."
    if path.startswith(_project_root):
        rel = path[len(_project_root) :]
        return rel or "."
    return path


def _shorten_paths_in_text(text: str) -> str:
    """Replace absolute project paths in text with relative ones."""
    if not _project_root:
        return text
    root_bare = _project_root.rstrip("/")
    # Replace root path references (longer first to avoid partial matches)
    text = text.replace(_project_root, "")
    text = text.replace(root_bare, ".")
    return text


def _smart_path_truncate(path: str, limit: int) -> str:
    """Truncate a path keeping the last components (filename matters most).

    Instead of ``runs/figure_runs/run_20260406/experiments/cross_...``
    produces ``…/experiments/cross_cell_line/figure_code.py``.
    """
    if len(path) <= limit:
        return path
    parts = path.split("/")
    # Always keep the last 2 components (dir + filename)
    if len(parts) <= 2:
        return path[: limit - 1] + "…"
    # Try keeping more from the right until it fits
    for keep in range(2, len(parts)):
        tail = "/".join(parts[-keep:])
        if len(tail) + 2 <= limit:  # "…/" prefix
            continue
        # Previous keep count was the max that fits
        tail = "/".join(parts[-(keep - 1) :])
        return f"…/{tail}"
    return path[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Pipeline cost tracking
# ---------------------------------------------------------------------------


class CostTracker:
    """Accumulates cost, tokens, and duration across agent sessions."""

    def __init__(self):
        self._lock = threading.Lock()
        self.sessions: list[dict] = []
        self.total_cost: float | None = None
        self.total_duration_ms: float = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0

    def record(
        self,
        *,
        cost: float | None = None,
        duration_ms: float | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        role: str = "subagent",
    ) -> None:
        with self._lock:
            entry = {
                "role": role,
                "cost": cost,
                "duration_ms": duration_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
            self.sessions.append(entry)
            if cost is not None:
                if self.total_cost is None:
                    self.total_cost = 0.0
                self.total_cost += cost
            if duration_ms is not None:
                self.total_duration_ms += duration_ms
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens

    @property
    def has_cost_data(self) -> bool:
        return self.total_cost is not None or self.total_input_tokens > 0

    @property
    def session_count(self) -> int:
        return len(self.sessions)


_cost_tracker = CostTracker()


def get_cost_tracker() -> CostTracker:
    """Access the global cost tracker."""
    return _cost_tracker


_THEME = Theme(
    {
        "orch.cyan": "bold #7dd3fc",
        "orch.green": "bold #86efac",
        "orch.yellow": "bold #facc15",
        "orch.red": "bold #fda4af",
        "orch.dim": "#94a3b8",
        "orch.bold": "bold #f8fafc",
        "orch.magenta": "bold #c4b5fd",
        "orch.border": "#475569",
        "orch.border.active": "#38bdf8",
        "orch.border.success": "#22c55e",
        "orch.border.warn": "#f59e0b",
        "orch.border.error": "#ef4444",
        "orch.title": "bold #f8fafc",
        "orch.label": "bold #cbd5e1",
    }
)

console = Console(
    stderr=True,
    theme=_THEME,
    highlight=False,
    no_color=(not _USE_COLOR),
)

# Optional log console (plain text, writes to file)
_log_console: Console | None = None
_lock = threading.Lock()

# Thread-local agent label for parallel output prefixing.
# Set via set_agent_label() in run_agent(); read by raw output functions.
_thread_local = threading.local()

# Rotating ANSI colors for agent labels (bright, distinguishable)
_LABEL_COLORS = ["33", "35", "32", "36", "34"]  # yellow, magenta, green, cyan, blue
_label_color_idx = 0
_label_color_map: dict[str, str] = {}


def set_agent_label(label: str | None) -> None:
    """Set the output label for the current thread (e.g., experiment name)."""
    global _label_color_idx
    _thread_local.label = label
    if label and label not in _label_color_map:
        with _lock:
            # Double-check after acquiring lock to avoid duplicate assignment.
            if label not in _label_color_map:
                _label_color_map[label] = _LABEL_COLORS[_label_color_idx % len(_LABEL_COLORS)]
                _label_color_idx += 1


def set_log_console(c: Console | None) -> None:
    global _log_console
    with _lock:
        _log_console = c


def _print(msg, **kw):
    """Print to both stderr console and optional log console.

    Thread-safe: acquires _lock to prevent races between parallel agents
    and orchestrator_log teardown.
    """
    with _lock:
        console.print(msg, **kw)
        if _log_console is not None:
            _log_console.print(msg, **kw)


def truncate_text(text: str, limit: int | None = None) -> str:
    """Truncate *text* to *limit* characters (default: adaptive to terminal width)."""
    if limit is None:
        limit = _truncate_limit()
    text = " ".join(str(text).split())
    if limit <= 1:
        return "…" if text else ""
    return text[: limit - 1] + "…" if len(text) > limit else text


def _kv(label: str, value: str, style: str = "orch.bold") -> Text:
    return Text.assemble((f"{label} ", "orch.label"), (value, style))


def _panel(title: str, body, *, border_style: str = "orch.border") -> Panel:
    tw = term_width()
    return Panel(
        body,
        title=f"[orch.title]{title}[/]",
        title_align="left",
        border_style=border_style,
        padding=(0, 1 if _compact_layout(tw) else 2),
        width=_panel_width(tw),
        expand=False,
    )


# ---------------------------------------------------------------------------
# ANSI helpers for stdout inline output (agent stream decorations)
# ---------------------------------------------------------------------------


def _ansi(code: str, text: str) -> str:
    """Wrap text in ANSI escape if color is enabled."""
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def _badge(label: str, fg: str = "97", bg: str = "44") -> str:
    """Render a colored badge like  Read  or  Bash  using ANSI bg+fg codes.

    Default: white text on blue background. Common combos:
      Read/Glob/Grep: 97;44  (white on blue)
      Bash:           97;42  (white on green)
      Agent:          97;45  (white on magenta)
      Patch:          97;43  (white on yellow)
      Error:          97;41  (white on red)
      Todo:           97;44  (white on blue)
    """
    if not _USE_COLOR:
        return f"[{label}]"
    return f"\033[{fg};{bg}m {label} \033[0m"


# Badge presets for tool types
_TOOL_BADGES = {
    "read": ("Read", "97", "44"),
    "glob": ("Glob", "97", "44"),
    "grep": ("Grep", "97", "44"),
    "bash": ("Bash", "97", "42"),
    "write": ("Write", "97", "43"),
    "edit": ("Edit", "97", "43"),
    "todowrite": ("Todo", "97", "44"),
    "skill": ("Skill", "97", "45"),
}


def _get_badge(name: str) -> str:
    """Get a badge for a tool name, with fallback to generic style."""
    lowered = name.lower()
    if lowered in _TOOL_BADGES:
        label, fg, bg = _TOOL_BADGES[lowered]
        return _badge(label, fg, bg)
    return _badge(name[:8].capitalize(), "97", "46")  # white on cyan for unknown


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences for plain-text length measurement."""
    return _ANSI_RE.sub("", text)


def _get_label_prefix() -> tuple[str, str]:
    """Return (plain_prefix, styled_prefix) for the current thread's agent label."""
    label = getattr(_thread_local, "label", None)
    if not label:
        return "", ""
    color = _label_color_map.get(label, "37")
    return f"[{label}] ", f"{_ansi(color, f'[{label}]')} "


# ---------------------------------------------------------------------------
# Logging context manager (replaces _TeeWriter + _orchestrator_log)
# ---------------------------------------------------------------------------


@contextmanager
def orchestrator_log(run_dir: str):
    """Context manager: tees orchestrator output to run_dir/logs/orchestrator.log.

    Styled output goes to stderr (terminal). Plain text goes to the log file.
    Agent subprocess output must be tee'd separately via raw().
    """
    log_path = os.path.join(run_dir, "logs", "orchestrator.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8")
    log_con = Console(file=log_file, theme=_THEME, no_color=True, highlight=False, width=200)
    set_log_console(log_con)
    try:
        yield log_file
    finally:
        set_log_console(None)
        log_file.close()


# ---------------------------------------------------------------------------
# Core output functions (stderr — orchestrator chrome)
# ---------------------------------------------------------------------------


def info(msg: str) -> None:
    _print(f"[orch.cyan]>[/] {msg}")


def success(msg: str) -> None:
    _print(f"[orch.green]✓[/] {msg}")


def warn(msg: str) -> None:
    _print(f"[orch.yellow]![/] {msg}")


def error(msg: str) -> None:
    _print(f"[orch.red]✗[/] {msg}")


def dim(msg: str) -> None:
    _print(f"[orch.dim]{msg}[/]")


def section(title: str) -> None:
    """Major phase divider."""
    rule = Rule(title=f"[orch.title]{title}[/]", style="orch.border.active")
    pw = _panel_width()
    with _lock:
        saved = console.width
        console.width = pw
        console.print(rule)
        if _log_console is not None:
            _log_console.print(rule)
        console.width = saved


# ---------------------------------------------------------------------------
# Structured output (stderr — orchestrator chrome)
# ---------------------------------------------------------------------------


def banner(
    command: str,
    platform: str,
    model: str,
    preset: str | None = None,
    execution: str | None = None,
    llm_roles: dict[str, str] | None = None,
    orchestrator_mode: str | None = None,
) -> None:
    """Boxless startup header — styled text lines."""
    # Title line
    _print(
        f"[orch.title]HappyFigure[/]  [orch.cyan]{command}[/] [orch.dim]·[/] "
        f"[orch.magenta]{platform}[/] [orch.dim]·[/] [orch.bold]{model}[/]"
    )
    # Tags line: orchestrator mode + execution
    tags: list[str] = []
    if orchestrator_mode:
        style = "orch.cyan" if orchestrator_mode == "agent-first" else "orch.dim"
        tags.append(f"[{style}]{orchestrator_mode}[/]")
    if execution and execution != "sequential":
        tags.append(f"[orch.cyan]{execution}[/]")
    if tags:
        _print(f"  {' [orch.dim]·[/] '.join(tags)}")
    # LLM roles: compact inline
    if llm_roles:
        role_parts = [f"[orch.dim]{r}[/] → {m}" for r, m in llm_roles.items()]
        _print(f"  [orch.dim]LLM[/]  {'  '.join(role_parts)}")


def _print_prompt_context(prompt: str) -> None:
    """Extract and display key context fields from an agent task prompt."""
    # Try to parse structured fields like "Run directory: X. Proposal path: Y."
    fields: list[tuple[str, str]] = []
    for pattern, label in [
        (r"Run directory:\s*(\S+)", "Run"),
        (r"Proposal(?:\s+path)?:\s*(\S+)", "Proposal"),
        (r"Results(?:\s+directory)?:\s*(\S+)", "Results"),
        (r"Plot execution strategy:\s*(\S+)", "Execution"),
        (r"Diagram mode:\s*(\S+)", "Mode"),
    ]:
        m = re.search(pattern, prompt)
        if m:
            val = m.group(1).rstrip(".")
            fields.append((label, short_path(val)))

    if fields:
        budget = _text_budget(4)
        for label, val in fields:
            line = f"{label}: {val}"
            _print(f"  [orch.dim]{truncate_text(line, budget)}[/]")
    else:
        # Fallback: show truncated prompt as-is
        budget = _text_budget(4)
        _print(f"  [orch.dim]{truncate_text(prompt, budget)}[/]")


def agent_start(name: str, platform: str, model: str, prompt: str, *, role: str = "subagent") -> None:
    """Agent launch header.

    *role* is ``"session"`` for the main orchestrator agent or
    ``"subagent"`` for agents spawned by the orchestrator.

    Session agents get a styled Rule line with key context below.
    Subagents get a slim indented marker (handled by raw_subagent).
    """
    is_session = role == "session"
    prompt = _shorten_paths_in_text(prompt)

    if is_session:
        # Rule-style header: ──── ▶ name · model ────
        # Constrain Rule width to match panel width
        title = f"[orch.cyan]▶ {name}[/] [orch.dim]·[/] [orch.bold]{model}[/]"
        rule = Rule(title=title, style="orch.border.active")
        pw = _panel_width()
        with _lock:
            saved = console.width
            console.width = pw
            console.print(rule)
            if _log_console is not None:
                _log_console.print(rule)
            console.width = saved
        # Parse key fields from prompt and show as clean indented lines
        _print_prompt_context(prompt)
    else:
        # Subagents: compact panel
        tw = term_width()
        meta = Table.grid(padding=(0, 2))
        meta.add_column()
        meta.add_row(
            Text.assemble(
                (f"{name} ", "orch.cyan"),
                ("· ", "orch.dim"),
                (model, "orch.bold"),
            )
        )
        prompt_limit = _prompt_limit(tw)
        meta.add_row(Text(truncate_text(prompt, prompt_limit), style="orch.dim"))
        _print(_panel(f"▹ {name}", Group(meta), border_style="orch.border"))


def agent_done(
    turns=None,
    duration_ms=None,
    cost=None,
    *,
    role: str = "subagent",
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Agent completion footer. Also records cost/token data in the tracker."""
    _cost_tracker.record(
        cost=cost,
        duration_ms=duration_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        role=role,
    )
    parts = ["Done"]
    if turns is not None:
        parts.append(f"{turns} turns")
    if duration_ms is not None:
        parts.append(f"{duration_ms / 1000:.1f}s")
    total_tok = input_tokens + output_tokens
    if total_tok > 0:
        parts.append(f"{total_tok:,} tokens")
    if cost is not None:
        parts.append(f"${cost:.2f}")
    style = "orch.border.active" if role == "session" else "orch.border"
    rule = Rule(title=f"[orch.dim]{' · '.join(parts)}[/]", style=style)
    pw = _panel_width()
    with _lock:
        saved = console.width
        console.width = pw
        console.print(rule)
        if _log_console is not None:
            _log_console.print(rule)
        console.width = saved


def pipeline_cost_summary() -> None:
    """Print a one-line cost/usage summary for the entire pipeline run."""
    t = _cost_tracker
    if not t.has_cost_data and t.session_count == 0:
        return
    parts: list[str] = []
    if t.total_cost is not None:
        parts.append(f"[orch.bold]${t.total_cost:.2f}[/]")
    elif t.total_input_tokens > 0:
        # Token-only (Codex) — show tokens instead of cost
        total_tok = t.total_input_tokens + t.total_output_tokens
        parts.append(f"[orch.bold]{total_tok:,} tokens[/]")
    else:
        parts.append("[orch.dim]cost unavailable[/]")
    if t.total_duration_ms > 0:
        parts.append(f"{t.total_duration_ms / 1000:.0f}s")
    parts.append(f"{t.session_count} agent session{'s' if t.session_count != 1 else ''}")
    _print(f"  [orch.dim]Cost:[/] {' · '.join(parts)}")


def tool_call(name: str, summary: str, *, indent: int = 0) -> None:
    """Display a tool call (stderr — orchestrator chrome)."""
    pad = " " * indent
    prefix_len = len(name) + 7 + indent
    max_summary = _text_budget(prefix_len)
    lines = summary.split("\n")
    first = truncate_text(lines[0], max_summary)
    _print(f"{pad}  [orch.cyan]⟡[/] [bold]{name}[/]: [orch.dim]{first}[/]")
    if len(lines) > 1:
        cont_pad = " " * prefix_len
        cont_max = _text_budget(len(cont_pad))
        for line in lines[1:]:
            _print(f"{cont_pad}[orch.dim]{truncate_text(line, cont_max)}[/]")


def tool_error(msg: str) -> None:
    """Display a tool error (stderr — orchestrator chrome)."""
    _print(f"  [orch.red]✗ tool error:[/] {msg}")


def result(exp: str, score, verdict: str) -> None:
    """Inline experiment result."""
    style = "orch.green" if verdict == "ACCEPT" else "orch.yellow"
    _print(f"  [orch.cyan]{exp}[/]: score={score}, verdict=[{style}]{verdict}[/]")


def summary_table(results: list[tuple[str, dict]]) -> None:
    """Final results table."""
    tw = term_width()
    table_width = _panel_width(tw)
    # On narrow terminals (<90), hide the Figure path column to avoid wrapping
    show_figure = table_width >= 90
    show_iterations = table_width >= 72
    table = Table(
        title="Final Results",
        show_header=True,
        header_style="orch.bold",
        border_style="orch.border.active",
        box=box.SIMPLE_HEAVY,
        expand=False,
        width=table_width,
        padding=(0, 1 if _compact_layout(tw) else 2),
    )
    table.add_column("Experiment", style="cyan", overflow="fold", max_width=max(18, table_width // 3))
    table.add_column("Score", justify="right")
    table.add_column("Verdict", justify="center")
    if show_iterations:
        table.add_column("Iterations", justify="right")
    if show_figure:
        fig_max = max(20, table_width // 3)
        table.add_column("Figure", style="dim", overflow="fold", max_width=fig_max)

    all_pass = True
    for exp, res in results:
        score = res.get("score", "N/A")
        verdict = res.get("verdict", "N/A")
        fig_path = res.get("figure_path", "N/A")
        iterations = res.get("iterations", res.get("iteration", "N/A"))
        if verdict != "ACCEPT":
            all_pass = False
        v_style = "green" if verdict == "ACCEPT" else "red"
        row = [exp, str(score), Text(str(verdict), style=v_style)]
        if show_iterations:
            row.append(str(iterations))
        if show_figure:
            row.append(str(fig_path))
        table.add_row(*row)

    _print(table)
    if all_pass:
        success("All figures ACCEPTED.")
    else:
        warn("Some figures did not reach ACCEPT threshold.")


def service_status(healthy: list[str], failed: list[str]) -> None:
    """Display service health status."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column()
    grid.add_row(_kv("Healthy", ", ".join(healthy) if healthy else "none", "orch.green"))
    if failed:
        grid.add_row(_kv("Unavailable", ", ".join(failed), "orch.red"))
    else:
        grid.add_row(_kv("Status", "All services ready", "orch.green"))
    _print(_panel("Services", grid, border_style="orch.border.success" if not failed else "orch.border.warn"))


# ---------------------------------------------------------------------------
# Raw pass-through (for agent subprocess output — stdout)
# ---------------------------------------------------------------------------


def raw(text: str, log_file=None) -> None:
    """Write raw text to stdout (agent subprocess pass-through).

    Does NOT go through Rich — preserves agent CLI formatting.
    Optionally tees to a log file.  Prepends thread-local agent label
    when running parallel agents.
    """
    plain_pfx, styled_pfx = _get_label_prefix()
    sys.stdout.write(styled_pfx + text)
    sys.stdout.flush()
    if log_file is not None:
        log_file.write(plain_pfx + text)


def _truncate_summary(summary: str, limit: int) -> str:
    """Smart-truncate a tool call summary.

    Uses path-aware truncation for path-like strings (keeps filename),
    falls back to regular truncation otherwise.
    """
    summary = summary.strip()
    # Looks like a file path — use smart truncation to keep the filename
    if "/" in summary and not summary.startswith("{") and "\n" not in summary:
        return _smart_path_truncate(summary, limit)
    return truncate_text(summary, limit)


# Duplicate tool call collapsing: track last emitted call per thread
_last_tool: dict[int, tuple[str, str, int]] = {}  # thread_id → (name, summary, count)


def _flush_tool_duplicates() -> str:
    """Flush any pending duplicate count for the current thread."""
    tid = threading.get_ident()
    entry = _last_tool.pop(tid, None)
    if entry and entry[2] > 1:
        count_msg = f"  {_ansi('2', f'  … ×{entry[2]} total')}\n"
        return _raw_emit(f"    … ×{entry[2]} total\n", count_msg)
    return ""


def _raw_emit(plain: str, styled: str) -> str:
    """Write styled text to stdout, return plain text for logging.

    All raw_* helpers delegate here so stdout write/flush is centralized.
    Uses ANSI (not Rich) to stay on stdout alongside agent text.
    Prepends thread-local agent label when running parallel agents.
    """
    plain_pfx, styled_pfx = _get_label_prefix()
    sys.stdout.write(styled_pfx + styled)
    sys.stdout.flush()
    return plain_pfx + plain


def raw_tool_call(name: str, summary: str, *, indent: int = 0) -> str:
    """Tool call inline in agent stdout stream.

    Supports multiline summaries (e.g. todo lists): the first line gets the
    ``⟡ name:`` prefix, continuation lines are indented to align.
    *indent* adds extra leading spaces (used for subagent nesting).
    Collapses consecutive duplicate tool calls (same name + similar summary).
    """
    summary = _shorten_paths_in_text(summary)
    # Tree connector for nested subagent calls
    if indent > 0:
        tree = _ansi("2", "│") if _USE_COLOR else "│"
        pad = f"  {tree} "
        plain_pad = "  │ "
    else:
        pad = ""
        plain_pad = ""

    # Duplicate collapsing: suppress consecutive calls with same name AND same summary
    tid = threading.get_ident()
    prev = _last_tool.get(tid)
    if prev and prev[0] == name and prev[1] == summary and "\n" not in summary:
        _last_tool[tid] = (name, summary, prev[2] + 1)
        return ""  # suppress exact duplicate

    # Flush previous duplicate count before rendering new/different tool
    result = _flush_tool_duplicates()
    _last_tool[tid] = (name, summary, 1)

    badge = _get_badge(name)
    # Badge plain text length for alignment
    badge_plain_len = min(len(name), 8) + 3  # " Name "
    prefix_len = badge_plain_len + 1 + len(plain_pad)

    lines = summary.split("\n")
    if len(lines) <= 1:
        max_summary = _text_budget(prefix_len)
        summary = _truncate_summary(summary, max_summary)
        result += _raw_emit(
            f"{plain_pad}[{name}] {summary}\n",
            f"{pad}{badge} {_ansi('2', summary)}\n",
        )
        return result
    # Multiline: header + indented continuation lines
    max_summary = _text_budget(prefix_len)
    first = truncate_text(lines[0], max_summary)
    cont_pad_plain = " " * prefix_len
    cont_pad_styled = " " * prefix_len
    result += _raw_emit(
        f"{plain_pad}[{name}] {first}\n",
        f"{pad}{badge} {_ansi('2', first)}\n",
    )
    cont_max = _text_budget(len(cont_pad_plain))
    for line in lines[1:]:
        line = truncate_text(line, cont_max)
        result += _raw_emit(
            f"{cont_pad_plain}{line}\n",
            f"{cont_pad_styled}{_ansi('2', line)}\n",
        )
    return result


def raw_subagent(name: str, summary: str, *, indent: int = 0) -> str:
    """Subagent launch inline in agent stdout stream."""
    result = _flush_tool_duplicates()  # flush pending tool dups before subagent
    summary = _shorten_paths_in_text(summary)
    pad = " " * indent
    badge = _badge("Agent", "97", "45")  # white on magenta
    max_summary = _text_budget(len(name) + 10 + indent)
    summary = truncate_text(summary, max_summary)
    result += _raw_emit(
        f"{pad}  [Agent] {name}: {summary}\n",
        f"{pad}{badge} {_ansi('1', name)} {_ansi('2', summary)}\n",
    )
    return result


def raw_step(title: str) -> str:
    """Step marker inline in agent stdout stream."""
    return _raw_emit(
        f"\n> {title}\n\n",
        f"\n{_ansi('36', '>')} {_ansi('1', title)}\n\n",
    )


def raw_patch(files: list[str]) -> str:
    """Edited files inline in agent stdout stream."""
    result = _flush_tool_duplicates()
    label = ", ".join(short_path(f) for f in files[:4])
    if len(files) > 4:
        label += f", +{len(files) - 4} more"
    badge = _badge("Patch", "97", "43")  # white on yellow
    result += _raw_emit(
        f"  [Patch] {label}\n",
        f"{badge} {_ansi('2', label)}\n",
    )
    return result


def raw_tool_error(msg: str) -> str:
    """Tool error inline in agent stdout stream."""
    result = _flush_tool_duplicates()
    msg = _shorten_paths_in_text(msg)
    badge = _badge("Error", "97", "41")  # white on red
    max_msg = _text_budget(10)
    msg = truncate_text(msg, max_msg)
    result += _raw_emit(
        f"  [Error] {msg}\n",
        f"{badge} {msg}\n",
    )
    return result


def raw_error(msg: str) -> str:
    """Error inline in agent stdout stream."""
    badge = _badge("Error", "97", "41")
    return _raw_emit(
        f"[Error] {msg}\n",
        f"{badge} {msg}\n",
    )


def raw_thinking(text: str) -> str:
    """Agent reasoning/thinking text inline in stdout stream (complete messages).

    Styled as dim italic to visually distinguish from tool output.
    """
    text = _shorten_paths_in_text(text)
    return _raw_emit(
        text + "\n",
        _ansi("2;3", text) + "\n",  # dim + italic
    )


# Streaming thinking: for platforms that send text as deltas (partial chunks),
# we wrap the stream in ANSI dim+italic codes. Call thinking_start() before the
# first delta and thinking_end() when the next non-text event arrives.

_thinking_active = threading.local()


def thinking_start() -> None:
    """Begin dim+italic styling for streamed thinking deltas."""
    if not getattr(_thinking_active, "on", False):
        _thinking_active.on = True
        if _USE_COLOR:
            sys.stdout.write("\033[2;3m")  # dim + italic
            sys.stdout.flush()


def thinking_end() -> None:
    """End dim+italic styling and ensure next output starts on a new line."""
    if getattr(_thinking_active, "on", False):
        _thinking_active.on = False
        if _USE_COLOR:
            sys.stdout.write("\033[0m\n")  # reset + newline
        else:
            sys.stdout.write("\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Idle spinner — animated status when no events arrive for a while
# ---------------------------------------------------------------------------

# Agent activity states (inferred from last event type)
IDLE_STATE_THINKING = "thinking"
IDLE_STATE_TOOL = "executing tool"
IDLE_STATE_WORKING = "working"

_SPINNER_FRAMES = [".", "..", "..."]
_SPINNER_DELAY = 2.0  # seconds before spinner appears
_SPINNER_INTERVAL = 0.4  # seconds between frame changes


class IdleSpinner:
    """Animated spinner that shows when no agent events arrive for a while.

    Usage::

        spinner = IdleSpinner()
        spinner.start()
        # ... on each event:
        spinner.notify(state="thinking")  # resets timer, updates state
        # ... when done:
        spinner.stop()

    The spinner writes directly to stderr so it doesn't pollute stdout.
    It uses ``\\r`` to overwrite itself in place and clears on the next event.

    Thread safety: all reads/writes of ``_visible``, ``_state``, and
    ``_last_event`` happen under ``_lock``.  Stderr writes are also
    serialized through the same lock to prevent interleaving.
    """

    def __init__(self, delay: float = _SPINNER_DELAY, label: str | None = None):
        self._delay = delay
        self._label = label  # optional agent label prefix
        self._state: str = IDLE_STATE_WORKING
        self._last_event: float = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._visible = False  # whether spinner text is currently on screen
        self._lock = threading.Lock()  # guards _visible, _state, _last_event, stderr writes

    def start(self) -> None:
        """Start the spinner background thread."""
        if self._thread is not None:
            return
        with self._lock:
            self._last_event = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="idle-spinner",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the spinner and clear any visible output."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._clear()

    def notify(self, state: str | None = None) -> None:
        """Signal that an event arrived. Resets the idle timer and clears spinner."""
        with self._lock:
            self._last_event = time.monotonic()
            if state is not None:
                self._state = state
            self._clear_locked()

    def _clear(self) -> None:
        """Erase the spinner line if visible (acquires lock)."""
        with self._lock:
            self._clear_locked()

    def _clear_locked(self) -> None:
        """Erase the spinner line if visible (caller must hold _lock)."""
        if self._visible:
            self._visible = False
            if _USE_COLOR:
                sys.stderr.write("\r\033[K")
                sys.stderr.flush()

    def _run(self) -> None:
        if not _USE_COLOR:
            return  # no spinner in plain mode — exit thread immediately
        # Pre-compute label prefix (immutable after __init__)
        prefix = f"  {self._label} " if self._label else "  "
        frame_idx = 0
        while not self._stop.is_set():
            self._stop.wait(timeout=_SPINNER_INTERVAL)
            if self._stop.is_set():
                break
            with self._lock:
                elapsed = time.monotonic() - self._last_event
                if elapsed < self._delay:
                    self._clear_locked()
                    continue
                state = self._state
                dots = _SPINNER_FRAMES[frame_idx % len(_SPINNER_FRAMES)]
                elapsed_s = int(elapsed)
                line = f"{prefix}\033[2;3m{state}{dots} ({elapsed_s}s)\033[0m"
                sys.stderr.write(f"\r\033[K{line}")
                sys.stderr.flush()
                self._visible = True
            frame_idx += 1


# ---------------------------------------------------------------------------
# Parallel execution progress dashboard
# ---------------------------------------------------------------------------


def _abbreviate(name: str, max_len: int = 24) -> str:
    """Shorten a name intelligently for dashboard display.

    Strategy: keep first and last segment of underscore-separated names,
    abbreviate middle segments to first 3 chars.  Falls back to truncation.
    """
    if len(name) <= max_len:
        return name
    parts = name.split("_")
    if len(parts) <= 2:
        return name[:max_len]
    # Keep first and last, abbreviate middle
    middle = "_".join(p[:3] for p in parts[1:-1])
    short = f"{parts[0]}_{middle}_{parts[-1]}"
    if len(short) <= max_len:
        return short
    return name[:max_len]


class ProgressDashboard:
    """Event-driven progress display for parallel agent execution.

    Prints a status line to stdout (same stream as agent output) only when
    a state change occurs, avoiding interleave issues with stderr.
    """

    def __init__(self, experiments: list[str]):
        self._experiments = experiments
        self._status: dict[str, str] = {e: "queued" for e in experiments}
        self._lock = threading.Lock()

    def start(self) -> None:
        """Print initial status line."""
        self._render()

    def stop(self) -> None:
        """Print final status line."""
        self._render()

    def update(self, experiment: str, status: str) -> None:
        """Update status and print a new status line."""
        with self._lock:
            old = self._status.get(experiment)
            self._status[experiment] = status
            if old != status:
                self._render_locked()

    def _render(self) -> None:
        with self._lock:
            self._render_locked()

    def _render_locked(self) -> None:
        """Print status line(s) to stdout. Must be called with _lock held.

        Adapts to terminal width: wraps experiment entries across multiple
        lines if they would exceed the current terminal width.
        """
        tw = term_width()
        name_budget = _dashboard_name_budget(tw)
        parts = []
        for exp in self._experiments:
            s = self._status[exp]
            short = _abbreviate(exp, max_len=name_budget)
            if s == "queued":
                parts.append(f"{_ansi('2', short)}: {_ansi('2', 'queued')}")
            elif s.startswith("done"):
                parts.append(f"{short}: {_ansi('32', s)}")
            elif s == "failed":
                parts.append(f"{short}: {_ansi('31', 'failed')}")
            else:
                parts.append(f"{short}: {_ansi('33', s)}")

        # Join with separator, wrapping to new lines if needed
        sep = "  |  "
        lines: list[str] = []
        current = "  "
        for i, part in enumerate(parts):
            # Plain-text length (strip ANSI for measurement)
            plain_part = _strip_ansi(part)
            addition = plain_part if not lines and current == "  " else sep + plain_part
            if len(_strip_ansi(current)) + len(addition) > tw and current != "  ":
                lines.append(current)
                current = "  " + part
            else:
                current += (sep + part) if current != "  " else part
        if current.strip():
            lines.append(current)

        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
