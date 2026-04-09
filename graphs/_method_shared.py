"""Shared definitions for method/architecture diagram pipelines.

Contains the base state type, node functions (load_markdown, data_explorer,
method_proposer), constants, and helpers used by the SVG method pipeline.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path
from typing import TypedDict, List

from graphs.svg_utils import load_pipeline_config
from graphs.figure_pipeline import (
    _load_prompt,
    _load_style_few_shots,
    _build_style_few_shot_messages,
    _ensure_gpt,
    _truncate_for_prompt,
)

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
REPO_ROOT = PROMPT_DIR.parent
ARCHITECTURE_EXAMPLES_DEFAULT = REPO_ROOT / "configs" / "method_examples"


class MethodDrawingPipelineState(TypedDict, total=False):
    input_dir: str  # directory containing input markdown files
    results_dir: str  # optional experiments/results directory for tool-based exploration
    proposal: str  # concatenated markdown content
    data_exploration_report: str  # tool-grounded data summary
    method_description: str  # method proposer output
    drawing_prompt: str  # final prompt for image generation
    figure_path: str  # output image path
    figure_paths: List[str]  # all generated figures
    run_dir: str  # artifact directory
    review_score: float  # quality score from review
    review_feedback: str  # quality review text
    success: bool
    error: str
    verbose: bool
    doc_type: str  # quality threshold type (default: "journal")
    max_drawing_iterations: int  # max image generation iterations (default: 2)
    architecture_examples_dir: str  # path to few-shot architecture examples
    architecture_few_shots: List[dict]  # loaded examples [{"description": str, "image_path": str}]


# Prompt size caps to keep method-mode tool-calling responsive on large proposals/results.
_METHOD_EXPLORER_PROPOSAL_CHARS_MAX = 8_000
_METHOD_EXPLORER_TREE_CHARS_MAX = 2_500
_METHOD_EXPLORER_SCHEMAS_CHARS_MAX = 4_000
_METHOD_EXPLORER_REPORT_CHARS_MAX = 4_500
_METHOD_PROPOSER_PROPOSAL_CHARS_MAX = 14_000
_METHOD_PROPOSER_REPORT_CHARS_MAX = 3_500
_METHOD_EXPLORER_FILE_SAMPLE_LIMIT = 50
_METHOD_EXPLORER_SCHEMA_FILE_LIMIT = 6


def _save_prompt_input(
    run_dir: str,
    node_name: str,
    system_prompt: str,
    user_prompt: str,
    *,
    suffix: str = "",
    metadata: dict | None = None,
) -> None:
    """Persist prompt input payloads for verbose debugging."""
    if not run_dir:
        return
    log_dir = Path(run_dir) / "verbose_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix_part = f"_{suffix}" if suffix else ""
    payload = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "node": node_name,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "metadata": metadata or {},
    }
    (log_dir / f"{node_name}_prompt_input{suffix_part}_{ts}.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )


def _build_method_explorer_seed(results_dir_path: Path) -> tuple[str, str]:
    """Build a lightweight file+schema seed for method data explorer prompts."""
    try:
        from tools import execute_data_tool
    except Exception:
        return "", ""

    file_map: dict[str, dict] = {}
    for pattern in ("**/*.csv", "**/*.tsv", "**/*.json", "**/*.md"):
        out = execute_data_tool("list_data_files", {"pattern": pattern}, results_dir_path)
        for item in out.get("files") or []:
            rel = str(item.get("path") or "")
            if rel:
                file_map[rel] = item

    if not file_map:
        return "No tabular/json/markdown files found under results directory.", ""

    files = [file_map[k] for k in sorted(file_map)]
    by_type: dict[str, int] = {}
    by_group: dict[str, int] = {}
    for item in files:
        ftype = str(item.get("type") or "unknown")
        by_type[ftype] = by_type.get(ftype, 0) + 1
        rel = str(item.get("path") or "")
        parts = Path(rel).parts
        group = parts[0] if parts else "root"
        by_group[group] = by_group.get(group, 0) + 1

    tree_lines = [
        "## Lightweight file inventory",
        f"Root: {results_dir_path}",
        f"Total candidate files: {len(files)}",
        "",
        "### File types",
    ]
    for k, v in sorted(by_type.items(), key=lambda kv: kv[0]):
        tree_lines.append(f"- {k}: {v}")
    tree_lines.extend(["", "### Top groups"])
    for k, v in sorted(by_group.items(), key=lambda kv: kv[1], reverse=True)[:12]:
        tree_lines.append(f"- {k}: {v} files")
    tree_lines.extend(["", "### Sample files"])
    for item in files[:_METHOD_EXPLORER_FILE_SAMPLE_LIMIT]:
        tree_lines.append(f"- {item.get('path')}")
    if len(files) > _METHOD_EXPLORER_FILE_SAMPLE_LIMIT:
        tree_lines.append(
            f"- ... ({len(files) - _METHOD_EXPLORER_FILE_SAMPLE_LIMIT} more)"
        )
    tree_text = _truncate_for_prompt(
        "\n".join(tree_lines),
        _METHOD_EXPLORER_TREE_CHARS_MAX,
        "method_explorer.seed_tree",
    )

    schema_lines = ["## Seed schema previews"]
    previews = 0
    prioritized = sorted(
        files,
        key=lambda item: (
            0
            if Path(str(item.get("path") or "")).suffix.lower()
            in {".csv", ".tsv", ".json"}
            else 1,
            str(item.get("path") or ""),
        ),
    )
    for item in prioritized:
        if previews >= _METHOD_EXPLORER_SCHEMA_FILE_LIMIT:
            break
        rel = str(item.get("path") or "")
        if not rel:
            continue
        meta = execute_data_tool(
            "read_data_file",
            {"file_path": rel, "head": 3},
            results_dir_path,
        )
        if meta.get("error"):
            continue
        ftype = str(meta.get("type") or "")
        if ftype in {"tabular", "json"}:
            cols = [
                str(c.get("name"))
                for c in (meta.get("columns") or [])
                if isinstance(c, dict) and c.get("name")
            ]
            shown = ", ".join(cols[:12]) if cols else "(no columns detected)"
            if len(cols) > 12:
                shown += f", ... (+{len(cols) - 12} more)"
            row_count = meta.get("row_count")
            row_text = f"{row_count} rows" if isinstance(row_count, int) else "rows unknown"
            schema_lines.append(f"- `{rel}` [{row_text}] columns: {shown}")
            previews += 1
        elif ftype == "markdown":
            snippet = str(meta.get("content") or "")
            first_line = next((ln.strip() for ln in snippet.splitlines() if ln.strip()), "")
            if first_line:
                schema_lines.append(f"- `{rel}` markdown snippet: {first_line[:120]}")
                previews += 1
    if previews == 0:
        schema_lines.append("- No readable schema previews extracted.")

    schema_text = _truncate_for_prompt(
        "\n".join(schema_lines),
        _METHOD_EXPLORER_SCHEMAS_CHARS_MAX,
        "method_explorer.seed_schemas",
    )
    return tree_text, schema_text


# ── Nodes ─────────────────────────────────────────────────────────────


def load_markdown_node(state: MethodDrawingPipelineState) -> MethodDrawingPipelineState:
    """Load proposal files from input_dir, concatenate text, create run_dir, load architecture few-shots.

    Supports Markdown, PDF, LaTeX, and other formats via :mod:`pipeline.proposal_loader`.
    """
    from pipeline.proposal_loader import extract_text, gather_proposal_files

    # Skip if already bootstrapped from a reuse_run_dir (run_dir + method_description pre-set)
    if state.get("run_dir") and state.get("method_description"):
        if state.get("verbose"):
            logger.info("load_markdown SKIPPED — reusing run_dir=%s", state['run_dir'])
        return {}

    input_dir = state.get("input_dir", "")
    if not input_dir:
        return {"error": "No input_dir specified", "success": False}

    input_path = Path(input_dir)
    if not input_path.is_absolute():
        input_path = REPO_ROOT / input_dir
    if not input_path.exists():
        return {"error": f"Input directory does not exist: {input_path}", "success": False}

    # Gather all proposal-relevant files (not just *.md)
    proposal_files = gather_proposal_files(input_path)
    if not proposal_files:
        return {"error": f"No proposal files found in {input_path}", "success": False}

    # Concatenate text content with filename headers
    parts = []
    for pf in proposal_files:
        content = extract_text(pf).strip()
        if content:
            parts.append(f"# File: {pf.name}\n\n{content}")
    if not parts:
        return {"error": f"No extractable text in proposal files at {input_path}", "success": False}
    proposal = "\n\n---\n\n".join(parts)

    # Reuse the caller-provided run_dir when available; otherwise create one.
    run_dir_value = state.get("run_dir", "")
    if run_dir_value:
        run_dir = Path(run_dir_value)
        if not run_dir.is_absolute():
            run_dir = REPO_ROOT / run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        notes_dir = REPO_ROOT / "notes" / "diagram_runs"
        notes_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = notes_dir / f"run_{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)

    # Load architecture few-shot examples (same pattern as _load_style_few_shots)
    arch_examples_dir = state.get("architecture_examples_dir")
    if not arch_examples_dir and ARCHITECTURE_EXAMPLES_DEFAULT.exists():
        arch_examples_dir = str(ARCHITECTURE_EXAMPLES_DEFAULT)
    few_shots = _load_style_few_shots(arch_examples_dir) if arch_examples_dir else []

    if state.get("verbose"):
        logger.info("Loaded %d proposal file(s) from %s", len(proposal_files), input_path)
        logger.info("Loaded %d architecture example(s)", len(few_shots))
        logger.info("Run directory: %s", run_dir)

    return {
        "proposal": proposal,
        "run_dir": str(run_dir),
        "architecture_few_shots": few_shots,
        "figure_paths": [],
    }


def method_data_explorer_node(state: MethodDrawingPipelineState) -> MethodDrawingPipelineState:
    """Explore results_dir with tools and summarize plot-/diagram-relevant signals."""
    results_dir = (state.get("results_dir") or "").strip()
    if not results_dir:
        return {"data_exploration_report": ""}

    _ensure_gpt()
    from llm import run_prompt

    system_text = _load_prompt("data_explorer_system.md")
    if not system_text.strip():
        return {"data_exploration_report": ""}

    results_dir_path = Path(results_dir)
    if not results_dir_path.is_absolute():
        results_dir_path = REPO_ROOT / results_dir
    if not results_dir_path.exists():
        return {
            "data_exploration_report": f"Results path does not exist: {results_dir_path}",
        }

    seed_tree, seed_schemas = _build_method_explorer_seed(results_dir_path)
    user_text = _load_prompt(
        "data_explorer_user_template.md",
        proposal=_truncate_for_prompt(
            state.get("proposal") or "",
            _METHOD_EXPLORER_PROPOSAL_CHARS_MAX,
            "method_explorer.proposal",
        ),
        data_tree=seed_tree,
        data_schemas=seed_schemas,
        results_dir=results_dir,
    )
    user_text = (
        user_text.strip()
        + "\n\nFocus on architecture/method-diagram-relevant findings: module names, pipeline stages, "
          "experiment groups, and key metric tables that explain method flow."
    )
    if state.get("verbose"):
        _save_prompt_input(
            state.get("run_dir", ""),
            "method_data_explorer",
            system_text.rstrip(),
            user_text,
            metadata={"model_mode": "chat", "tool_calling": True, "max_tool_rounds": 4},
        )

    report = ""
    last_error = ""
    tool_call_log: list[dict] = []

    try:
        from llm import run_prompt_with_tools
        from tools import DATA_TOOL_SCHEMAS, execute_data_tool

        def _data_tool_executor(name: str, args: dict) -> dict:
            return execute_data_tool(name, args, results_dir_path)

        for attempt in range(3):
            try:
                result = run_prompt_with_tools(
                    "chat",
                    user_text,
                    system_prompt=system_text.rstrip(),
                    tools=DATA_TOOL_SCHEMAS,
                    tool_choice="auto",
                    tool_executor=_data_tool_executor,
                    max_tool_rounds=4,
                )
                report = result.text.strip()
                tool_call_log = result.tool_calls
                last_error = ""
                break
            except Exception as e:
                last_error = str(e)
                if attempt < 2:
                    import time as _time
                    _time.sleep(2 * (attempt + 1))
    except ImportError:
        pass

    if not report:
        for attempt in range(3):
            try:
                report = run_prompt("chat", user_text, system_prompt=system_text.rstrip()).strip()
                last_error = ""
                break
            except Exception as e:
                last_error = str(e)
                if attempt < 2:
                    import time as _time
                    _time.sleep(2 * (attempt + 1))

    if not report:
        report = f"Method data exploration failed after 3 attempts: {last_error}"
    report = _truncate_for_prompt(
        report,
        _METHOD_EXPLORER_REPORT_CHARS_MAX,
        "method_data_exploration_report",
    )

    run_dir = state.get("run_dir", "")
    if run_dir:
        rd = Path(run_dir)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "method_data_exploration_report.md").write_text(report, encoding="utf-8")
        if tool_call_log:
            (rd / "method_data_explorer_tool_calls.json").write_text(
                json.dumps(tool_call_log, indent=2),
                encoding="utf-8",
            )

    return {"data_exploration_report": report}


def method_proposer_node(state: MethodDrawingPipelineState) -> MethodDrawingPipelineState:
    """LLM: analyze markdown proposal and produce a structured architecture description."""
    # Skip if method_description already injected (e.g. from --reuse-run-dir)
    if state.get("method_description"):
        if state.get("verbose"):
            logger.info("method_proposer SKIPPED — method_description already set")
        return {}

    _ensure_gpt()
    from llm import run_prompt

    proposal = state.get("proposal", "")
    if not proposal:
        return {"error": "No proposal text available", "success": False}

    system_text = _load_prompt("method_proposer_system.md")
    user_text = _load_prompt(
        "method_proposer_user_template.md",
        proposal=_truncate_for_prompt(
            proposal,
            _METHOD_PROPOSER_PROPOSAL_CHARS_MAX,
            "method_proposer.proposal",
        ),
        data_exploration_report=_truncate_for_prompt(
            state.get("data_exploration_report") or "",
            _METHOD_PROPOSER_REPORT_CHARS_MAX,
            "method_proposer.data_exploration_report",
        ),
    )

    # Build few-shot messages from architecture examples (same pattern as stylist)
    few_shots = state.get("architecture_few_shots") or []
    few_shot_messages = _build_style_few_shot_messages(few_shots) if few_shots else None

    # Add note about architecture references if examples exist
    if few_shots:
        n = len(few_shots)
        refs_note = (
            f"\n\n---\n\n**Architecture references:** {n} example architecture diagram(s) were provided above. "
            "Analyze their visual style (layout direction, color scheme, block shapes, arrow styles, level of detail) "
            "and incorporate similar patterns in your Drawing Instruction."
        )
        user_text = user_text.strip() + refs_note

    if state.get("verbose"):
        _save_prompt_input(
            state.get("run_dir", ""),
            "method_proposer",
            system_text.rstrip(),
            user_text.strip(),
            metadata={"model_mode": "chat", "few_shot_count": len(few_shots)},
        )

    response = run_prompt(
        "chat",
        user_text.strip(),
        system_prompt=system_text.rstrip(),
        few_shot_messages=few_shot_messages,
    ).strip()

    # Save to run_dir
    run_dir = state.get("run_dir", "")
    if run_dir:
        (Path(run_dir) / "method_description.md").write_text(response, encoding="utf-8")

    if state.get("verbose"):
        from llm import get_backend, get_model_display
        logger.info("Method description generated (%d chars, backend=%s, model=%s)", len(response), get_backend(), get_model_display('chat'))

    return {"method_description": response}


def _extract_drawing_instruction(method_description: str) -> str:
    """Extract the Drawing Instruction section from method_description.

    Falls back to the full description if the section is not found.
    """
    pattern = r'#{2,3}\s*Drawing\s+Instruction\s*\n(.*?)(?=\n#{2,3}\s|\Z)'
    match = re.search(pattern, method_description, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return method_description


SCIENTIFIC_DIAGRAM_GUIDELINES = """
Create a high-quality scientific diagram with these requirements:

VISUAL QUALITY:
- Clean white or light background (no textures or gradients)
- High contrast for readability and printing
- Professional, publication-ready appearance
- Sharp, clear lines and text
- Adequate spacing between elements to prevent crowding

TYPOGRAPHY:
- Clear, readable sans-serif fonts (Arial, Helvetica style)
- Minimum 10pt font size for all labels
- Consistent font sizes throughout
- All text horizontal or clearly readable
- No overlapping text

SCIENTIFIC STANDARDS:
- Accurate representation of concepts
- Clear labels for all components
- Use standard scientific notation and symbols

ACCESSIBILITY:
- Colorblind-friendly color palette (use Okabe-Ito colors if using color)
- High contrast between elements
- Redundant encoding (shapes + colors, not just colors)
- Works well in grayscale

LAYOUT:
- Logical flow (left-to-right or top-to-bottom)
- Clear visual hierarchy
- Balanced composition
- Appropriate use of whitespace
- No clutter or unnecessary decorative elements

IMPORTANT - NO FIGURE NUMBERS:
- Do NOT include "Figure 1:", "Fig. 1", or any figure numbering in the image
- The diagram should contain only the visual content itself
"""

# Quality thresholds by document type (score out of 12, 6 dimensions x 2.0)
_DEFAULT_THRESHOLDS = {
    "journal": 10.2,
    "conference": 9.6,
    "poster": 8.4,
    "presentation": 7.8,
    "report": 9.0,
    "grant": 9.6,
    "thesis": 9.6,
    "preprint": 9.0,
    "default": 9.0,
}
QUALITY_THRESHOLDS = load_pipeline_config().get("scoring", {}).get("quality_thresholds", _DEFAULT_THRESHOLDS)
