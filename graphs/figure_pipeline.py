"""
Figure generation pipeline: load input -> figure planner -> stylist -> code agent -> execute -> critic -> (loop 2-3x).
Reads prompts from prompts/; uses llm.gpt_example.run_prompt.
"""

from __future__ import annotations
import concurrent.futures
import datetime
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TypedDict, Literal, Any, List

from langgraph.graph import StateGraph, END

from graphs.svg_utils import load_pipeline_config
from pipeline.orchestrator import artifacts as _orch_art

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
REPO_ROOT = PROMPT_DIR.parent
RESULTS_DEFAULT = Path.cwd() / "results"

_cfg = load_pipeline_config()
MAX_ITERATIONS = _cfg.get("scoring", {}).get("max_iterations", 3)

# Beam search defaults
_beam_cfg = _cfg.get("beam", {})
BEAM_WIDTH = _beam_cfg.get("width", 2)
BEAM_STYLE_VARIANTS = _beam_cfg.get("style_variants", 2)
BEAM_CODE_VARIANTS = _beam_cfg.get("code_variants", 2)
BEAM_ITERATIONS = _beam_cfg.get("iterations", 2)

# Prompt payload caps to avoid context-window failures on large results directories.
_TREE_CHARS_MAX = 2_000
_SCHEMAS_CHARS_MAX = 6_000
_STATISTICS_CHARS_MAX = 3_000
_SEMANTICS_CHARS_MAX = 2_000
_FULL_TREE_CHARS_MAX = 3_000
_FULL_SCHEMAS_CHARS_MAX = 12_000
_FULL_STATISTICS_CHARS_MAX = 4_000
_FULL_SEMANTICS_CHARS_MAX = 3_000

_VISION_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

# ── Thread-safe progress callback for parallel execution ─────────────
# Set by cli.py (or any caller) before invoking the parallel pipeline.
# Signature: callback(experiment_name: str, node_name: str, iteration: int, **kwargs)
_parallel_progress_lock = threading.Lock()
_parallel_progress_callback = None


def _status_print(message: str) -> None:
    """Emit human-readable progress without polluting JSON stdout callers."""
    print(message, file=sys.stderr, flush=True)


def _report_progress(experiment_name: str, node_name: str, iteration: int, **kwargs):
    """Thread-safe progress reporting for parallel execution."""
    cb = _parallel_progress_callback
    if cb:
        with _parallel_progress_lock:
            cb(experiment_name, node_name, iteration, **kwargs)


class FigurePipelineState(TypedDict, total=False):
    proposal: str
    results_summary: str
    pre_data_overview: str  # Lightweight pre-scan report from data tools
    # Hierarchical data catalog — current experiment
    data_tree: str  # L1: compact directory tree
    data_schemas: str  # L2: column definitions + sample rows
    data_statistics: str  # L3: per-file numeric ranges
    data_semantics: str  # L4: structural metadata
    # Hierarchical data catalog — ALL experiments concatenated
    full_data_tree: str
    full_data_schemas: str
    full_data_semantics: str
    data_exploration_report: str  # Tool-grounded data summary for planner/stylist
    results_dir: str
    full_results_dir: str  # Original root results dir (before per-experiment splitting)
    figure_plan: str  # Per-experiment figure spec (extracted from multi_figure_plan)
    multi_figure_plan: str  # Full planner output covering ALL experiments
    per_experiment_specs: dict  # {experiment_name: figure_spec_text}
    style_spec: str  # Kept for backward compat (aliased to styled_figure_spec)
    styled_figure_spec: str  # Merged figure spec + style (stylist output)
    code: str
    run_stdout: str
    run_stderr: str
    figure_path: str
    run_success: bool
    critic_feedback: str
    critic_verdict: str
    critic_score: float
    issue_code: str
    iteration: int
    error: str
    # Artifact tracking: one run_dir per pipeline run, subdir per experiment
    run_dir: str
    experiment_groups: List[Any]
    current_experiment_index: int
    current_experiment_name: str
    figure_paths: List[str]
    # For parallel execution
    all_plans_created: bool  # Track if all experiments have been planned
    experiment_states: List[dict]  # Store state for each experiment during parallel execution
    # For verbose logging
    verbose: bool  # Enable detailed JSON logging of node inputs/outputs
    verbose_log_dir: str  # Directory for verbose logs
    # For style few-shot examples
    style_examples_dir: str  # Directory containing style example images + descriptions
    style_few_shots: List[dict]  # List of {"description": str, "image_path": str}
    run_mode: str  # "happyfigure" | "exp_plot" (legacy: "multipanel" | "single_panel_statistical")
    planner_mode_hint: str  # planner instruction suffix controlled by cli mode
    force_route: str  # optional forced route category for all experiments
    prefer_multi_panel: bool  # planner preference toggle (soft hint only)
    route_type: str  # "statistical" | "visualization" | "multi_panel"
    route_subcategory: str  # e.g. "bar_group_plots"
    per_experiment_routes: dict  # {experiment_name: routing_dict}
    panel_outputs: dict  # {panel_id: panel_result}
    routing_retry_done: bool  # whether planner retry was already attempted for routing failures
    # Beam search
    beam_width: int
    beam_style_variants: int
    beam_code_variants: int
    beam_iterations: int
    beam_candidate_tag: str  # Subdirectory tag for beam candidate artifacts
    best_figure_path: str  # Path to final best figure from beam search
    # Data processing stage
    data_processing_code: str  # Code generated by data_processor
    processed_results_dir: str  # Path to processed data directory
    data_processing_stdout: str  # stdout from processing execution
    data_processing_stderr: str  # stderr from processing execution
    data_processing_success: bool  # Whether processing succeeded
    data_processing_reused: bool  # Whether processed_data was reused instead of regenerated
    data_processing_mode: str  # "regen" | "reuse"
    data_tool_cache: dict  # Optional per-run cache for repeated data-tool calls


def _retry_llm_call(fn, max_attempts: int = 3):
    """Retry an LLM call with exponential backoff. Returns result or raises last error."""
    last_error = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_error = e
            if attempt < max_attempts - 1:
                time.sleep(2 * (attempt + 1))
    raise last_error


from graphs.svg_utils import load_prompt as _load_prompt  # noqa: E402 — unified prompt loader


def _truncate_for_prompt(text: str, max_chars: int, label: str) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    suffix = f"\n\n... (truncated {omitted} chars from {label})"
    keep = max_chars - len(suffix)
    if keep <= 0:
        return text[:max_chars]
    return text[:keep].rstrip() + suffix


def _compact_experiment_group(group: dict) -> dict:
    name = group.get("name") or "experiment"
    compact = dict(group)
    compact["tree"] = _truncate_for_prompt(group.get("tree") or "", _TREE_CHARS_MAX, f"{name}.tree")
    compact["schemas"] = _truncate_for_prompt(group.get("schemas") or "", _SCHEMAS_CHARS_MAX, f"{name}.schemas")
    compact["statistics"] = _truncate_for_prompt(
        group.get("statistics") or "", _STATISTICS_CHARS_MAX, f"{name}.statistics"
    )
    compact["semantics"] = _truncate_for_prompt(group.get("semantics") or "", _SEMANTICS_CHARS_MAX, f"{name}.semantics")
    return compact


def _safe_image_data_url(path: Path, encoder) -> str | None:
    """Encode image path to data URL and keep only image/* MIME payloads."""
    try:
        data_url = encoder(path)
    except Exception:
        return None
    if isinstance(data_url, str) and data_url.startswith("data:image/"):
        return data_url
    return None


def _find_renderable_image(path: Path) -> Path | None:
    """Resolve a renderable raster image path for vision models."""
    if path.exists() and path.suffix.lower() in _VISION_IMAGE_EXTS:
        return path
    stem = path.with_suffix("")
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
        alt = stem.with_suffix(ext)
        if alt.exists():
            return alt
    return None


def _write_preview_png(src: Path, dest_png: Path) -> bool:
    """Write a PNG preview from a source raster image."""
    try:
        if src.suffix.lower() == ".png":
            shutil.copy2(src, dest_png)
            return True
        from PIL import Image

        with Image.open(src) as img:
            img.convert("RGB").save(dest_png, format="PNG")
        return True
    except Exception:
        return False


def _ensure_gpt() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


CONFIGS_DIR = REPO_ROOT / "configs"
STATISTICAL_EXAMPLES_DIR = CONFIGS_DIR / "statistical_examples"
METHOD_EXAMPLES_DIR = CONFIGS_DIR / "method_examples"
VISUALIZATION_EXAMPLES_DIR = CONFIGS_DIR / "visualization"
MULTI_PANEL_EXAMPLES_DIR = CONFIGS_DIR / "multi_panel_examples"

_VALID_FIGURE_CATEGORIES = {"statistical", "visualization", "multi_panel"}
_VALID_STATISTICAL_SUBCATEGORIES = {
    "bar_group_plots",
    "bar_ablation",
    "heatmap",
    "line_chart",
    "scatter_plots",
    "trend_plots",
    "composite_graphs_plots",
    "violin_box_plots",
    "dot_lollipop_plots",
    "area_plots",
    "radar_plots",
    "others",
}


def _load_style_few_shots(style_dir: str | None, *, recursive: bool = False) -> List[dict]:
    """Load style few-shot examples from a directory.

    Expected directory structure:
        style_examples/
        ├── example1.png       # Example figure image
        ├── example1.txt       # Description of the style (plain text)
        ├── example2.png
        ├── example2.txt
        └── ...

    Each pair (image + .txt description) becomes one few-shot example.
    Also supports .jpg, .jpeg, .pdf, .svg image formats.
    Returns list of {"description": str, "image_path": str}.
    """
    if not style_dir:
        if STATISTICAL_EXAMPLES_DIR.exists():
            style_dir = str(STATISTICAL_EXAMPLES_DIR)
        else:
            return []

    examples_path = Path(style_dir)
    if not examples_path.is_absolute():
        examples_path = REPO_ROOT / style_dir
    if not examples_path.exists():
        return []

    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".pdf", ".svg"}
    few_shots = []

    # Find all image files and match with their .txt descriptions
    finder = examples_path.rglob("*") if recursive else examples_path.iterdir()
    image_files = sorted(p for p in finder if p.is_file() and p.suffix.lower() in IMAGE_EXTS)

    for img_path in image_files:
        # Look for matching description file (same stem, .txt extension)
        desc_path = img_path.with_suffix(".txt")
        description = ""
        if desc_path.exists():
            description = desc_path.read_text(encoding="utf-8").strip()
        else:
            # Also try .md
            desc_md = img_path.with_suffix(".md")
            if desc_md.exists():
                description = desc_md.read_text(encoding="utf-8").strip()

        # Check for matching source code (.py file)
        code_path = img_path.with_suffix(".py")
        code = code_path.read_text(encoding="utf-8").strip() if code_path.exists() else None

        few_shots.append(
            {
                "description": description or f"Example figure: {img_path.name}",
                "image_path": str(img_path),
                "code": code,
            }
        )

    return few_shots


def _default_routing(error: str | None = None) -> dict:
    return {
        "figure_category": "statistical",
        "statistical_subcategory": None,
        "panels": [],
        "_routing_valid": error is None,
        "_routing_error": error or "",
    }


def _clean_routing_value(v: str) -> str:
    return v.strip().strip('"').strip("'")


def _parse_panel_block(block: str) -> list[dict]:
    """Parse `panels:` YAML-like list from routing block without external deps."""
    lines = block.splitlines()
    in_panels = False
    current: dict | None = None
    panels: list[dict] = []

    for raw in lines:
        if re.match(r"^\s*panels\s*:\s*$", raw):
            in_panels = True
            continue
        if not in_panels:
            continue
        if raw and not raw.startswith((" ", "\t")):
            break
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("-"):
            if current:
                panels.append(current)
            current = {}
            payload = stripped[1:].strip()
            if payload and ":" in payload:
                k, v = payload.split(":", 1)
                current[k.strip()] = _clean_routing_value(v)
            continue
        if ":" in stripped and current is not None:
            k, v = stripped.split(":", 1)
            current[k.strip()] = _clean_routing_value(v)

    if current:
        panels.append(current)
    return panels


def _parse_figure_routing(spec_text: str) -> dict:
    """Extract and parse <!-- FIGURE_ROUTING --> block from a FIGURE_SPEC."""
    match = re.search(
        r"<!--\s*FIGURE_ROUTING\s*-->(.*?)<!--\s*END_FIGURE_ROUTING\s*-->",
        spec_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return _default_routing("missing routing block")

    block = match.group(1)
    category_match = re.search(r"^\s*figure_category\s*:\s*([A-Za-z_]+)\s*$", block, flags=re.MULTILINE)
    subcat_match = re.search(r"^\s*statistical_subcategory\s*:\s*([A-Za-z_]+)\s*$", block, flags=re.MULTILINE)

    category = _clean_routing_value(category_match.group(1)).lower() if category_match else "statistical"
    subcategory = _clean_routing_value(subcat_match.group(1)).lower() if subcat_match else None
    panels = _parse_panel_block(block)

    errors: list[str] = []
    if category not in _VALID_FIGURE_CATEGORIES:
        errors.append(f"invalid figure_category='{category}'")
        category = "statistical"
        subcategory = None
        panels = []

    if category == "statistical" and subcategory and subcategory not in _VALID_STATISTICAL_SUBCATEGORIES:
        errors.append(f"invalid statistical_subcategory='{subcategory}'")
        subcategory = None

    if category != "multi_panel":
        panels = []
    else:
        if not panels:
            errors.append("multi_panel missing panels")
        normalized_panels = []
        for p in panels:
            panel_category = (p.get("panel_category") or "").strip().lower()
            if panel_category not in {"statistical", "visualization"}:
                panel_category = "statistical"
                errors.append(f"panel '{p.get('panel_id', '?')}' missing/invalid panel_category")
            panel_sub = (p.get("statistical_subcategory") or "").strip().lower() or None
            if panel_category == "statistical" and panel_sub and panel_sub not in _VALID_STATISTICAL_SUBCATEGORIES:
                panel_sub = None
                errors.append(f"panel '{p.get('panel_id', '?')}' invalid statistical_subcategory")
            normalized_panels.append(
                {
                    "panel_id": p.get("panel_id", ""),
                    "panel_category": panel_category,
                    "statistical_subcategory": panel_sub,
                    "panel_description": p.get("panel_description", ""),
                }
            )
        panels = normalized_panels

    routing = {
        "figure_category": category,
        "statistical_subcategory": subcategory,
        "panels": panels,
        "_routing_valid": len(errors) == 0,
        "_routing_error": "; ".join(errors),
    }
    return routing


def load_routed_few_shots(routing: dict, override_dir: str | None = None) -> List[dict]:
    """Load few-shots from category/subcategory-matched directory."""
    if override_dir:
        return _load_style_few_shots(override_dir, recursive=True)

    category = (routing.get("figure_category") or "statistical").lower()
    subcategory = routing.get("statistical_subcategory")

    if category == "visualization":
        return _load_style_few_shots(str(VISUALIZATION_EXAMPLES_DIR))
    if category == "multi_panel":
        return _load_style_few_shots(str(MULTI_PANEL_EXAMPLES_DIR))
    if category == "statistical" and subcategory:
        subdir = STATISTICAL_EXAMPLES_DIR / str(subcategory)
        if subdir.exists():
            return _load_style_few_shots(str(subdir))
    return _load_style_few_shots(str(STATISTICAL_EXAMPLES_DIR), recursive=True)


def _apply_route_overrides(per_experiment_routes: dict[str, dict], state: FigurePipelineState) -> dict[str, dict]:
    """Apply mode-driven route overrides from CLI orchestration."""
    force_route = (state.get("force_route") or "").strip().lower()
    if force_route not in _VALID_FIGURE_CATEGORIES:
        return per_experiment_routes

    updated: dict[str, dict] = {}
    for name, routing in (per_experiment_routes or {}).items():
        merged = dict(routing or {})
        if force_route == "statistical":
            merged["figure_category"] = "statistical"
            if merged.get("statistical_subcategory") not in _VALID_STATISTICAL_SUBCATEGORIES:
                merged["statistical_subcategory"] = None
            merged["panels"] = []
            merged["_routing_valid"] = True
            merged["_routing_error"] = ""
        elif force_route == "visualization":
            merged["figure_category"] = force_route
            merged["statistical_subcategory"] = None
            merged["panels"] = []
            merged["_routing_valid"] = True
            merged["_routing_error"] = ""
        elif force_route == "multi_panel":
            merged["figure_category"] = "multi_panel"
            merged["statistical_subcategory"] = None
            merged["_routing_valid"] = True
            # Keep planner panels if present; otherwise downstream will use fallback handling.
        updated[name] = merged
    return updated


def _serialize_for_json(obj: Any) -> Any:
    """Recursively serialize objects for JSON, handling non-serializable types."""
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    elif isinstance(obj, dict):
        return {k: _serialize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_serialize_for_json(item) for item in obj]
    elif isinstance(obj, Path):
        return str(obj)
    else:
        return str(obj)


def _log_node_io(
    state: FigurePipelineState,
    node_name: str,
    io_type: str,
    data: dict,
) -> None:
    """Log node input/output to JSON file if verbose mode is enabled.

    Args:
        state: Current pipeline state
        node_name: Name of the node being logged
        io_type: Either "input" or "output"
        data: Data to log (will be serialized to JSON)
    """
    if not state.get("verbose"):
        return

    verbose_dir = state.get("verbose_log_dir")
    if not verbose_dir:
        return

    log_dir = Path(verbose_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Create experiment-specific subdirectory if applicable
    exp_name = state.get("current_experiment_name")
    if exp_name:
        log_dir = log_dir / exp_name
        log_dir.mkdir(parents=True, exist_ok=True)

    # Create iteration-specific filename
    iteration = state.get("iteration", 1)
    timestamp = datetime.datetime.now().strftime("%H%M%S_%f")[:12]  # HHMMSSμs (truncated)
    filename = f"iter{iteration:02d}_{node_name}_{io_type}_{timestamp}.json"

    log_path = log_dir / filename

    # Serialize and write
    serialized = _serialize_for_json(data)
    log_entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "node_name": node_name,
        "io_type": io_type,
        "iteration": iteration,
        "experiment_name": exp_name,
        "data": serialized,
    }

    try:
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(log_entry, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to write verbose log %s: %s", log_path, e)


def _log_prompt_input(
    state: FigurePipelineState,
    node_name: str,
    system_prompt: str,
    user_prompt: str,
    **metadata: Any,
) -> None:
    """Persist prompt input payload for a node when verbose logging is enabled."""
    payload: dict[str, Any] = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
    }
    if metadata:
        payload["metadata"] = metadata
    _log_node_io(state, node_name, "prompt_input", payload)


def _with_logging(node_func):
    """Decorator to add verbose logging to a node function."""

    def wrapper(state: FigurePipelineState) -> FigurePipelineState:
        node_name = node_func.__name__.replace("_node", "")

        # Log input
        _log_node_io(state, node_name, "input", dict(state))

        # Execute node
        result = node_func(state)

        # Log output
        _log_node_io(state, node_name, "output", result if isinstance(result, dict) else {})

        return result

    wrapper.__name__ = node_func.__name__
    wrapper.__doc__ = node_func.__doc__
    return wrapper


def _artifact_dir(state: FigurePipelineState) -> Path:
    """Directory for current experiment's artifacts: run_dir/current_experiment_name[/beam_candidate_tag]."""
    run_dir = state.get("run_dir") or ""
    name = state.get("current_experiment_name") or "default"
    beam_tag = state.get("beam_candidate_tag") or ""
    if beam_tag:
        d = Path(run_dir) / name / beam_tag
    else:
        d = Path(run_dir) / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _md_to_dataframe(fp: Path):
    """Convert a Markdown file containing tables to section-aware DataFrames.

    Extracts all markdown tables (pipe-delimited) along with their preceding
    section headers.  Returns a list of ``(section_name, DataFrame)`` pairs.
    Tables under the same section with identical columns are merged; tables
    with different columns are kept separate so downstream consumers see
    each schema clearly (instead of a garbled pd.concat with NaN columns).

    Returns ``[]`` when no tables are found.
    """
    import pandas as pd
    import re

    text = fp.read_text(encoding="utf-8")
    # (section_name, header_cells, data_rows)
    raw_tables: list[tuple[str, list[str], list[list[str]]]] = []
    current_section = ""
    current_rows: list[list[str]] = []
    header: list[str] | None = None

    for line in text.splitlines():
        stripped = line.strip()
        # Detect section headers: markdown headings or bold-only lines
        if stripped.startswith("#") or (stripped.startswith("**") and stripped.endswith("**") and "|" not in stripped):
            # Flush any in-progress table
            if header and current_rows:
                raw_tables.append((current_section, header, current_rows))
                header = None
                current_rows = []
            current_section = re.sub(r"^[#*\s]+|[*\s]+$", "", stripped).strip()
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip().replace("**", "") for c in stripped.split("|")[1:-1]]
            # Skip separator rows like |---|---|
            if all(re.fullmatch(r":?-+:?", c) for c in cells):
                continue
            if header is None:
                header = cells
            else:
                current_rows.append(cells)
        else:
            # End of a table block
            if header and current_rows:
                raw_tables.append((current_section, header, current_rows))
            header = None
            current_rows = []

    # Catch trailing table
    if header and current_rows:
        raw_tables.append((current_section, header, current_rows))

    if not raw_tables:
        return []

    # Group tables by (section_name, column_tuple) so that tables with
    # identical schemas under the same section are merged, while tables with
    # different schemas stay separate.
    from collections import OrderedDict

    grouped: OrderedDict[tuple, tuple] = OrderedDict()  # key -> (section, header, merged_rows)
    for section, hdr, rows in raw_tables:
        key = (section, tuple(hdr))
        if key in grouped:
            grouped[key] = (section, hdr, grouped[key][2] + rows)
        else:
            grouped[key] = (section, hdr, rows)

    results: list[tuple[str, pd.DataFrame]] = []
    for (section, _hdr_tuple), (sec_name, hdr, rows) in grouped.items():
        padded = [r + [""] * (len(hdr) - len(r)) for r in rows]
        df = pd.DataFrame(padded, columns=hdr)
        for col in df.columns:
            try:
                df[col] = pd.to_numeric(df[col])
            except (ValueError, TypeError):
                pass
        label = sec_name if sec_name else fp.stem
        results.append((label, df))

    return results


def _json_to_dataframe(fp: Path):
    """Convert a JSON file to a pandas DataFrame.

    Handles common structures:
    - List of dicts  → rows directly
    - Dict of lists  → columns directly
    - Flat/nested dict → single-row normalised table
    """
    import json
    import pandas as pd

    with open(fp, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, list):
        if raw and isinstance(raw[0], dict):
            return pd.json_normalize(raw)
        return pd.DataFrame(raw, columns=["value"])
    if isinstance(raw, dict):
        # Dict of lists (columnar) – all values are equal-length lists
        if raw and all(isinstance(v, list) for v in raw.values()):
            lens = {len(v) for v in raw.values()}
            if len(lens) == 1:
                return pd.DataFrame(raw)
        # Flat or nested dict → single-row normalised table
        return pd.json_normalize(raw)
    # Scalar / other
    return pd.DataFrame([{"value": raw}])


def _scan_data_files(path: Path, rel_base: Path, max_files: int = 40) -> tuple:
    """Read all TSV/CSV/JSON files under *path*, group by schema key.

    Returns (schema_groups: OrderedDict, error_sections: list[str]).
    schema_groups[key] = {"col_dtypes": str, "ncols": int,
                          "files": [{"rel", "nrows", "sample_lines", "range_lines"}]}
    """
    import pandas as pd
    from collections import OrderedDict

    TABULAR_EXTS = {".tsv", ".csv", ".json", ".md"}
    BINARY_EXTS = {".npy", ".npz", ".pkl", ".pt", ".ckpt"}
    ALL_DATA_EXTS = TABULAR_EXTS | BINARY_EXTS

    data_files = sorted(
        [p for p in path.rglob("*") if p.suffix.lower() in ALL_DATA_EXTS],
        key=lambda p: str(p),
    )

    schema_groups: OrderedDict[tuple, dict] = OrderedDict()
    error_sections: list[str] = []

    for fp in data_files[:max_files]:
        rel = fp.relative_to(rel_base)
        try:
            # Binary files: list as opaque entries (the data processor can handle them)
            if fp.suffix.lower() in BINARY_EXTS:
                size_mb = fp.stat().st_size / (1024 * 1024)
                ext_label = {
                    ".npy": "NumPy array",
                    ".npz": "NumPy archive",
                    ".pkl": "Pickle",
                    ".pt": "PyTorch checkpoint",
                    ".ckpt": "PyTorch checkpoint",
                }.get(fp.suffix.lower(), "Binary")
                schema_key = (("__binary__", fp.suffix.lower()),)
                file_info = {
                    "rel": str(rel),
                    "nrows": 0,
                    "ncols": 0,
                    "sample_lines": f"  ({ext_label}, {size_mb:.1f} MB)",
                    "range_lines": [],
                }
                if schema_key not in schema_groups:
                    schema_groups[schema_key] = {
                        "col_dtypes": f"{ext_label} file",
                        "ncols": 0,
                        "files": [],
                    }
                schema_groups[schema_key]["files"].append(file_info)
                continue

            # For JSON files, capture raw structure alongside DataFrame view
            raw_json_snippet = ""
            if fp.suffix.lower() == ".md":
                section_dfs = _md_to_dataframe(fp)
                if not section_dfs:
                    # No tables found in markdown — include as text content
                    md_text = fp.read_text(encoding="utf-8")
                    if len(md_text) > 2000:
                        md_text = md_text[:2000] + "\n... (truncated)"
                    schema_key = (("__markdown__", ".md"),)
                    file_info = {
                        "rel": str(rel),
                        "nrows": 0,
                        "ncols": 0,
                        "sample_lines": "\n".join("  " + line for line in md_text.splitlines()[:30]),
                        "range_lines": [],
                    }
                    if schema_key not in schema_groups:
                        schema_groups[schema_key] = {
                            "col_dtypes": "Markdown text file",
                            "ncols": 0,
                            "files": [],
                        }
                    schema_groups[schema_key]["files"].append(file_info)
                    continue
                # Process each section as a separate schema entry so that
                # tables with different columns are not garbled into one.
                for section_name, section_df in section_dfs:
                    sec_nrows, sec_ncols = section_df.shape
                    sec_schema_key = tuple((c, str(section_df[c].dtype)) for c in section_df.columns)
                    sec_col_dtypes = ", ".join(f"{c} ({section_df[c].dtype})" for c in section_df.columns)

                    virtual_rel = f"{rel} [Section: {section_name}]" if len(section_dfs) > 1 else str(rel)

                    sec_sample_buf = io.StringIO()
                    section_df.head(3).to_csv(sec_sample_buf, sep="\t", index=False)
                    sec_sample_text = sec_sample_buf.getvalue().rstrip()
                    sec_sample_lines = "\n".join("  " + line for line in sec_sample_text.splitlines())
                    # Unique values for categorical columns (markdown sections)
                    sec_cat_cols = [c for c in section_df.columns if section_df[c].dtype == "object"]
                    if sec_cat_cols:
                        sec_uv_parts: list[str] = []
                        for c in sec_cat_cols:
                            uniques = section_df[c].dropna().unique().tolist()
                            if len(uniques) <= 30:
                                sec_uv_parts.append(f"    {c}: {uniques}")
                            else:
                                sec_uv_parts.append(f"    {c}: {uniques[:30]} ... ({len(uniques)} unique)")
                        sec_sample_lines += "\n  --- Unique values per categorical column ---\n" + "\n".join(
                            sec_uv_parts
                        )

                    sec_num_cols = section_df.select_dtypes("number").columns.tolist()
                    sec_range_lines: list[str] = []
                    for c in sec_num_cols:
                        col_min = section_df[c].min()
                        col_mean = section_df[c].mean()
                        col_max = section_df[c].max()
                        sec_range_lines.append(f"    {c}:  {col_min:.6g} / {col_mean:.6g} / {col_max:.6g}")

                    sec_file_info = {
                        "rel": virtual_rel,
                        "nrows": sec_nrows,
                        "ncols": sec_ncols,
                        "sample_lines": sec_sample_lines,
                        "range_lines": sec_range_lines,
                    }

                    if sec_schema_key not in schema_groups:
                        schema_groups[sec_schema_key] = {
                            "col_dtypes": sec_col_dtypes,
                            "ncols": sec_ncols,
                            "files": [],
                        }
                    schema_groups[sec_schema_key]["files"].append(sec_file_info)
                continue
            elif fp.suffix.lower() == ".json":
                try:
                    with open(fp, "r", encoding="utf-8") as jf:
                        raw = json.load(jf)
                    # Truncated pretty-print of raw JSON (first item if list, whole if dict)
                    sample_obj = raw[0] if isinstance(raw, list) and raw else raw
                    raw_json_snippet = json.dumps(sample_obj, indent=2, default=str)
                    if len(raw_json_snippet) > 1500:
                        raw_json_snippet = raw_json_snippet[:1500] + "\n  ... (truncated)"
                except (TypeError, ValueError, KeyError):
                    pass  # JSON structure not suitable for preview snippet
                df = _json_to_dataframe(fp)
            else:
                sep = "\t" if fp.suffix.lower() == ".tsv" else ","
                df = pd.read_csv(fp, sep=sep, engine="python", on_bad_lines="skip")

            nrows, ncols = df.shape
            schema_key = tuple((c, str(df[c].dtype)) for c in df.columns)
            col_dtypes = ", ".join(f"{c} ({df[c].dtype})" for c in df.columns)

            # Sample rows (first 3)
            sample_buf = io.StringIO()
            df.head(3).to_csv(sample_buf, sep="\t", index=False)
            sample_text = sample_buf.getvalue().rstrip()
            sample_lines = "\n".join("  " + line for line in sample_text.splitlines())
            # Unique values for categorical (non-numeric) columns
            cat_cols = [c for c in df.columns if df[c].dtype == "object"]
            if cat_cols:
                uv_parts: list[str] = []
                for c in cat_cols:
                    uniques = df[c].dropna().unique().tolist()
                    if len(uniques) <= 30:
                        uv_parts.append(f"    {c}: {uniques}")
                    else:
                        uv_parts.append(f"    {c}: {uniques[:30]} ... ({len(uniques)} unique)")
                sample_lines += "\n  --- Unique values per categorical column ---\n" + "\n".join(uv_parts)
            # Append raw JSON structure for JSON files
            if raw_json_snippet:
                sample_lines += "\n  --- Raw JSON structure ---\n" + "\n".join(
                    "  " + line for line in raw_json_snippet.splitlines()
                )

            # Numeric summary per file
            num_cols = df.select_dtypes("number").columns.tolist()
            range_lines: list[str] = []
            for c in num_cols:
                col_min = df[c].min()
                col_mean = df[c].mean()
                col_max = df[c].max()
                range_lines.append(f"    {c}:  {col_min:.6g} / {col_mean:.6g} / {col_max:.6g}")

            file_info = {
                "rel": str(rel),
                "nrows": nrows,
                "ncols": ncols,
                "sample_lines": sample_lines,
                "range_lines": range_lines,
            }

            if schema_key not in schema_groups:
                schema_groups[schema_key] = {
                    "col_dtypes": col_dtypes,
                    "ncols": ncols,
                    "files": [],
                }
            schema_groups[schema_key]["files"].append(file_info)
        except Exception as exc:
            error_sections.append(f"### File: {rel}\n(could not profile: {exc})")

    if len(data_files) > max_files:
        error_sections.append(f"\n... and {len(data_files) - max_files} more data files (truncated)")

    return schema_groups, error_sections


def _build_tree(path: Path, rel_base: Path) -> str:
    """L1 — compact directory tree with file counts at leaf directories.

    Renders with ├──/└── characters, caps depth at 4.
    Leaf directories (containing data files) annotated with [N files: a.tsv, b.tsv].
    """
    DATA_EXTS = {".tsv", ".csv", ".json", ".md", ".npy", ".npz", ".pkl", ".pt", ".ckpt"}
    lines: list[str] = []

    def _has_data(d: Path) -> bool:
        return any(d.rglob("*") if False else (f for f in d.rglob("*") if f.suffix.lower() in DATA_EXTS))

    def _walk(d: Path, prefix: str, depth: int):
        if depth > 4:
            return
        children = sorted([c for c in d.iterdir() if c.is_dir() and _has_data(c)])
        data_here = sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() in DATA_EXTS)

        # If this is a leaf (no child dirs with data), show file list
        if not children and data_here:
            names = ", ".join(f.name for f in data_here[:6])
            suffix = ", ..." if len(data_here) > 6 else ""
            lines.append(f"{prefix}[{len(data_here)} file{'s' if len(data_here) != 1 else ''}: {names}{suffix}]")
            return

        # Show data files at this level if any (mixed dir)
        if data_here:
            names = ", ".join(f.name for f in data_here[:4])
            suffix = ", ..." if len(data_here) > 4 else ""
            lines.append(f"{prefix}({len(data_here)} file{'s' if len(data_here) != 1 else ''} here: {names}{suffix})")

        for i, child in enumerate(children):
            is_last = i == len(children) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{child.name}/")
            child_prefix = prefix + ("    " if is_last else "│   ")
            _walk(child, child_prefix, depth + 1)

    rel_root = path.relative_to(rel_base) if path != rel_base else Path(path.name)
    lines.append(f"{rel_root}/")
    _walk(path, "  ", 1)
    return "\n".join(lines) if lines else "(empty directory)"


def _build_schemas_from_scan(schema_groups, error_sections: list[str], path: Path) -> str:
    """L2 — column definitions grouped by schema, one sample per group, file list with row counts only."""
    sections: list[str] = []

    for group_idx, (schema_key, group) in enumerate(schema_groups.items(), 1):
        files = group["files"]
        representative = files[0]

        header = f"### Schema group {group_idx} ({len(files)} file{'s' if len(files) != 1 else ''})"
        header += f"\nColumns ({group['ncols']}): {group['col_dtypes']}"
        header += f"\nSample (first 3 rows, from {representative['rel']}):"
        header += f"\n{representative['sample_lines']}"

        file_lines = [f"- {fi['rel']} ({fi['nrows']} rows)" for fi in files]

        block = header + "\n\nFiles:\n" + "\n".join(file_lines)
        sections.append(block)

    sections.extend(error_sections)

    # Discover existing .py scripts
    py_files = sorted(path.rglob("*.py"))
    if py_files:
        try:
            script_lines = [f"- {p.relative_to(path.parent)}" for p in py_files]
        except ValueError:
            script_lines = [f"- {p.name}" for p in py_files]
        sections.append("### Existing scripts\n" + "\n".join(script_lines))

    return "\n\n".join(sections) if sections else "(no data files found)"


def _build_statistics_from_scan(schema_groups, error_sections: list[str]) -> str:
    """L3 — per-file numeric ranges (min/mean/max) grouped by schema."""
    sections: list[str] = []

    for group_idx, (schema_key, group) in enumerate(schema_groups.items(), 1):
        files = group["files"]
        header = f"### Schema group {group_idx} — numeric ranges"

        file_lines: list[str] = []
        for fi in files:
            file_lines.append(f"- {fi['rel']} ({fi['nrows']} rows)")
            if fi["range_lines"]:
                file_lines.extend(fi["range_lines"])

        if file_lines:
            sections.append(header + "\n" + "\n".join(file_lines))

    sections.extend(error_sections)
    return "\n\n".join(sections) if sections else ""


def _build_semantics(path: Path, rel_base: Path) -> str:
    """L4 — auto-extracted structural metadata.

    For each depth level reports: unique directory names, cardinality,
    branch vs leaf role, children-per-entry consistency.
    Detects factorial structure by comparing actual leaf count to product of
    cardinalities.  No hardcoded rules or domain-specific heuristics.
    """
    DATA_EXTS = {".tsv", ".csv", ".json", ".md", ".npy", ".npz", ".pkl", ".pt", ".ckpt"}

    # Collect all leaf directories (dirs that directly contain data files)
    leaf_dirs: list[Path] = []
    for d in sorted(path.rglob("*")):
        if d.is_dir():
            data_here = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in DATA_EXTS]
            if data_here:
                leaf_dirs.append(d)

    if not leaf_dirs:
        return "(no data directories found)"

    # Compute relative paths from experiment root
    rel_paths = []
    for ld in leaf_dirs:
        try:
            rel_paths.append(ld.relative_to(path))
        except ValueError:
            continue

    if not rel_paths:
        return "(no data directories found)"

    # Determine max depth
    max_depth = max(len(rp.parts) for rp in rel_paths)

    # Analyse each depth level
    level_info: list[dict] = []
    for depth in range(max_depth):
        names_at_depth: list[str] = []
        for rp in rel_paths:
            if len(rp.parts) > depth:
                names_at_depth.append(rp.parts[depth])
        unique_names = sorted(set(names_at_depth))
        cardinality = len(unique_names)

        # Determine role: branch if there are deeper levels, leaf if this is the last
        is_leaf = depth == max_depth - 1
        role = "leaf" if is_leaf else "branch"

        # Children-per-entry consistency: for each unique name at this depth,
        # how many unique children does it have at the next depth?
        children_counts: list[int] = []
        if not is_leaf:
            for name in unique_names:
                child_names = set()
                for rp in rel_paths:
                    if len(rp.parts) > depth + 1 and rp.parts[depth] == name:
                        child_names.add(rp.parts[depth + 1])
                children_counts.append(len(child_names))

        level_info.append(
            {
                "depth": depth,
                "unique_names": unique_names,
                "cardinality": cardinality,
                "role": role,
                "children_counts": children_counts,
            }
        )

    # Detect factorial structure
    cardinality_product = 1
    for li in level_info:
        cardinality_product *= li["cardinality"]
    actual_leaves = len(set(str(rp) for rp in rel_paths))
    is_factorial = (actual_leaves == cardinality_product) and max_depth > 1

    # Render
    lines: list[str] = ["## Directory structure semantics", ""]
    for li in level_info:
        depth_label = f"Level {li['depth']}"
        names_str = ", ".join(li["unique_names"][:10])
        if len(li["unique_names"]) > 10:
            names_str += f", ... ({li['cardinality']} total)"
        lines.append(f"**{depth_label}** ({li['role']}, cardinality {li['cardinality']}): {names_str}")
        if li["children_counts"]:
            counts_set = set(li["children_counts"])
            if len(counts_set) == 1:
                lines.append(f"  Children per entry: {counts_set.pop()} (uniform)")
            else:
                lines.append(
                    f"  Children per entry: {min(li['children_counts'])}–{max(li['children_counts'])} (variable)"
                )
        lines.append("")

    lines.append(f"Leaf directories: {actual_leaves}")
    lines.append(f"Cardinality product: {cardinality_product}")
    if is_factorial:
        dims = " × ".join(str(li["cardinality"]) for li in level_info)
        lines.append(f"Structure: full factorial ({dims})")
    else:
        lines.append("Structure: non-factorial (some combinations missing)")

    return "\n".join(lines)


def _split_multi_figure_plan(plan_text: str, experiment_names: list[str]) -> dict[str, str]:
    """Split a multi-figure plan into per-experiment specs.

    Parses ``<!-- FIGURE_SPEC: name -->`` delimiters emitted by the planner.
    Also handles alternative bold-markdown delimiters that the LLM sometimes
    produces instead of HTML comments:
      - ``**FIGURE_SPEC: name**``
      - ``** FIGURE_SPEC: name **``
      - ``**Figure Spec: name**``

    Any text *before* the first delimiter is treated as a shared preamble and
    prepended to every experiment's spec.  Falls back to assigning the entire
    plan to each experiment when delimiters are missing.
    """
    # Try HTML comment format first: <!-- FIGURE_SPEC: name -->
    pattern = r"<!--\s*FIGURE_SPEC:\s*(.+?)\s*-->"
    parts = re.split(pattern, plan_text)

    # If HTML comments didn't work, try bold markdown: **FIGURE_SPEC: name**
    if len(parts) < 3:
        pattern = r"\*\*\s*FIGURE[_ ]SPEC:\s*(.+?)\s*\*\*"
        parts = re.split(pattern, plan_text, flags=re.IGNORECASE)

    result: dict[str, str] = {}
    if len(parts) >= 3:
        preamble = parts[0].strip()
        for i in range(1, len(parts), 2):
            name = parts[i].strip()
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            result[name] = (preamble + "\n\n" + content).strip() if preamble else content

    # Fallback: if parsing failed, assign the whole plan to each experiment
    if not result:
        for name in experiment_names:
            result[name] = plan_text
        return result

    # Fuzzy match experiment names that didn't get an exact hit
    for exp_name in experiment_names:
        if exp_name not in result:
            for key in result:
                if key.lower() == exp_name.lower() or key in exp_name or exp_name in key:
                    result[exp_name] = result[key]
                    break
            else:
                result[exp_name] = plan_text  # last-resort fallback

    return result


def _expand_groups_from_specs(
    per_experiment_specs: dict[str, str],
    experiment_groups: List[dict],
) -> List[dict]:
    """Expand experiment_groups when the planner emitted more FIGURE_SPECs than experiments.

    This handles the common case where a single experiment directory contains
    multiple data sections (e.g., section_1/, section_2/, section_3/) and the
    planner correctly created a separate FIGURE_SPEC for each section.

    If there are N specs but only 1 experiment group, we clone that group N times
    (one per spec), keeping the same path/schemas/tree but with distinct names.
    If the counts already match, returns groups unchanged.
    """
    spec_names = list(per_experiment_specs.keys())
    exp_names = [g["name"] for g in experiment_groups]

    # Only expand when we have more specs than groups
    if len(spec_names) <= len(experiment_groups):
        return experiment_groups

    # Check which spec names are NOT already covered by existing groups
    unmatched_specs = [s for s in spec_names if s not in exp_names]
    if not unmatched_specs:
        return experiment_groups

    # Clone the best-matching group for each unmatched spec
    expanded = list(experiment_groups)
    for spec_name in unmatched_specs:
        # Find the best base group to clone (fuzzy match or fall back to first)
        base = experiment_groups[0]
        for g in experiment_groups:
            if g["name"].lower() in spec_name.lower() or spec_name.lower() in g["name"].lower():
                base = g
                break
        expanded.append(
            {
                **base,
                "name": spec_name,
            }
        )

    return expanded


# -------------------------
# Nodes
# -------------------------


def pre_data_explorer_node(state: FigurePipelineState) -> FigurePipelineState:
    """Lightweight pre-scan before heavy catalog/data processing nodes."""
    results_dir = state.get("results_dir")
    if not results_dir:
        results_dir = str(RESULTS_DEFAULT)
    results_path = Path(results_dir)
    if not results_path.is_absolute():
        results_path = Path.cwd() / results_dir

    if not results_path.exists():
        return {
            "pre_data_overview": f"Results path does not exist: {results_path}",
        }

    file_map: dict[str, dict] = {}
    try:
        from tools import execute_data_tool

        for pattern in (
            "**/*.csv",
            "**/*.tsv",
            "**/*.json",
            "**/*.md",
            "**/*.npy",
            "**/*.npz",
            "**/*.pkl",
            "**/*.pt",
            "**/*.ckpt",
        ):
            out = execute_data_tool("list_data_files", {"pattern": pattern}, results_path)
            for item in out.get("files") or []:
                path = str(item.get("path") or "")
                if path:
                    file_map[path] = item
    except Exception:
        # Fallback to direct scan if tool package is unavailable.
        exts = {".csv", ".tsv", ".json", ".md", ".npy", ".npz", ".pkl", ".pt", ".ckpt"}
        for p in sorted(results_path.rglob("*"), key=lambda x: str(x)):
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            rel = str(p.relative_to(results_path))
            file_map[rel] = {
                "path": rel,
                "size_bytes": p.stat().st_size,
                "type": "binary" if p.suffix.lower() in {".npy", ".npz", ".pkl", ".pt", ".ckpt"} else "tabular",
            }

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

    lines = [
        "## Lightweight pre-scan",
        f"Root: {results_path}",
        f"Total data files: {len(files)}",
        "",
        "### File types",
    ]
    for k, v in sorted(by_type.items(), key=lambda kv: kv[0]):
        lines.append(f"- {k}: {v}")
    lines.extend(["", "### Top groups (by first path segment)"])
    for k, v in sorted(by_group.items(), key=lambda kv: kv[1], reverse=True)[:12]:
        lines.append(f"- {k}: {v} files")
    lines.extend(["", "### Sample files"])
    for item in files[:40]:
        lines.append(f"- {item.get('path')}")
    if len(files) > 40:
        lines.append(f"- ... ({len(files) - 40} more)")

    return {
        "pre_data_overview": _truncate_for_prompt(
            "\n".join(lines),
            3_000,
            "pre_data_overview",
        ),
    }


def load_input_node(state: FigurePipelineState) -> FigurePipelineState:
    """Load proposal; discover experiment groups (top-level subdirs with TSV/CSV/JSON files); set run_dir and first experiment."""
    out: FigurePipelineState = {}
    proposal = (state.get("proposal") or "").strip()
    if not proposal:
        logger.warning("No proposal provided in state.")
    out["proposal"] = proposal

    results_dir = state.get("results_dir")
    if not results_dir:
        logger.warning("No results_dir provided. Falling back to default: %s", RESULTS_DEFAULT)
        results_dir = str(RESULTS_DEFAULT)
    results_path = Path(results_dir)
    if not results_path.is_absolute():
        results_path = Path.cwd() / results_dir
    try:
        results_dir_rel = (
            str(results_path.relative_to(REPO_ROOT)) if results_path.is_relative_to(REPO_ROOT) else str(results_path)
        )
    except ValueError:
        results_dir_rel = str(results_path)

    # Create run dir for this pipeline run (all experiments)
    notes_dir = Path.cwd() / "notes" / "figure_runs"
    notes_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = notes_dir / f"run_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    out["run_dir"] = str(run_dir)

    # Setup verbose logging directory if enabled
    if state.get("verbose"):
        verbose_log_dir = run_dir / "verbose_logs"
        verbose_log_dir.mkdir(parents=True, exist_ok=True)
        out["verbose_log_dir"] = str(verbose_log_dir)

    # Discover experiment groups: top-level subdirs of results_path that contain data files
    experiment_groups: List[dict] = []
    if results_path.exists():
        for child in sorted(results_path.iterdir()):
            if child.is_dir():
                _DATA_GLOBS = ("*.tsv", "*.csv", "*.json", "*.md", "*.npy", "*.npz", "*.pkl", "*.pt", "*.ckpt")
                if any(f for g in _DATA_GLOBS for f in child.rglob(g)):
                    scan, errors = _scan_data_files(child, results_path)
                    experiment_groups.append(
                        {
                            "name": child.name,
                            "path": f"{results_dir_rel}/{child.name}",
                            "tree": _build_tree(child, results_path),
                            "schemas": _build_schemas_from_scan(scan, errors, child),
                            "statistics": _build_statistics_from_scan(scan, errors),
                            "semantics": _build_semantics(child, results_path),
                        }
                    )
    if not experiment_groups:
        # Single "experiment": whole results dir
        scan, errors = _scan_data_files(results_path, results_path)
        experiment_groups = [
            {
                "name": "all",
                "path": results_dir_rel,
                "tree": _build_tree(results_path, results_path),
                "schemas": _build_schemas_from_scan(scan, errors, results_path),
                "statistics": _build_statistics_from_scan(scan, errors),
                "semantics": _build_semantics(results_path, results_path),
            }
        ]

    experiment_groups = [_compact_experiment_group(g) for g in experiment_groups]

    # Build full (ALL experiments) concatenated strings per level
    full_tree_parts = []
    full_schemas_parts = []
    full_semantics_parts = []
    full_summary_parts = []
    for g in experiment_groups:
        full_tree_parts.append(f"## Experiment: {g['name']}\n\n{g.get('tree', '')}")
        full_schemas_parts.append(f"## Experiment: {g['name']}\n\n{g.get('schemas', '')}")
        full_semantics_parts.append(f"## Experiment: {g['name']}\n\n{g.get('semantics', '')}")
        full_summary_parts.append(f"## Experiment: {g['name']}\n\n{g.get('tree', '')}")
    out["full_data_tree"] = _truncate_for_prompt(
        "\n\n---\n\n".join(full_tree_parts),
        _FULL_TREE_CHARS_MAX,
        "full_data_tree",
    )
    out["full_data_schemas"] = _truncate_for_prompt(
        "\n\n---\n\n".join(full_schemas_parts),
        _FULL_SCHEMAS_CHARS_MAX,
        "full_data_schemas",
    )
    out["full_data_semantics"] = _truncate_for_prompt(
        "\n\n---\n\n".join(full_semantics_parts),
        _FULL_SEMANTICS_CHARS_MAX,
        "full_data_semantics",
    )
    out["results_summary"] = _truncate_for_prompt(
        "\n\n---\n\n".join(full_summary_parts),
        _FULL_TREE_CHARS_MAX,
        "results_summary",
    )

    out["experiment_groups"] = experiment_groups
    out["full_results_dir"] = results_dir_rel  # Preserve the root results dir for data processor
    out["current_experiment_index"] = 0
    out["current_experiment_name"] = experiment_groups[0]["name"]
    out["results_dir"] = experiment_groups[0]["path"]
    # Set current experiment's per-level fields
    out["data_tree"] = experiment_groups[0]["tree"]
    out["data_schemas"] = experiment_groups[0]["schemas"]
    out["data_statistics"] = experiment_groups[0]["statistics"]
    out["data_semantics"] = experiment_groups[0]["semantics"]
    out["iteration"] = 1
    out["figure_paths"] = []
    out["route_type"] = "statistical"
    out["route_subcategory"] = ""
    out["panel_outputs"] = {}

    # Eager few-shot loading only for explicit CLI override; routed loading happens later
    if state.get("style_examples_dir"):
        style_few_shots = _load_style_few_shots(state["style_examples_dir"], recursive=True)
        if style_few_shots:
            out["style_few_shots"] = style_few_shots

    return out


def _strip_code_fences(code: str) -> str:
    """Strip markdown code fences if present."""
    if code.startswith("```"):
        lines = code.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        code = "\n".join(lines)
    return code


def data_processor_node(state: FigurePipelineState) -> FigurePipelineState:
    """LLM: generate Python script to transform raw data into figure-ready CSVs."""
    mode = (state.get("data_processing_mode") or "regen").strip().lower()
    if mode == "reuse":
        return {
            "data_processing_code": "# skipped: reuse existing processed_data",
            "data_processing_reused": True,
        }

    _ensure_gpt()
    from llm import run_prompt

    system_text = _load_prompt("data_process_system.md")
    # Use the FULL results dir (root), not the per-experiment subdir
    results_dir = state.get("full_results_dir") or state.get("results_dir") or str(RESULTS_DEFAULT)

    user_text = _load_prompt(
        "data_process_user_template.md",
        proposal=state.get("proposal") or "",
        pre_data_overview=state.get("pre_data_overview") or "",
        data_tree=state.get("full_data_tree") or state.get("data_tree") or "",
        data_schemas=state.get("full_data_schemas") or state.get("data_schemas") or "",
        data_semantics=state.get("full_data_semantics") or state.get("data_semantics") or "",
        results_dir=results_dir,
    )
    _log_prompt_input(
        state,
        "data_processor",
        system_text.rstrip(),
        user_text.strip(),
        model_mode_tool_path="code",
        model_mode_fallback="chat",
        tool_calling=True,
        max_tool_rounds=5,
        results_dir=results_dir,
    )

    code = ""
    last_error = ""
    tool_call_log: list[dict] = []

    # Try tool-calling path first (CC code-explorer style).
    try:
        from llm import run_prompt_with_tools
        from tools import DATA_TOOL_SCHEMAS, execute_data_tool

        results_dir_path = Path(results_dir)
        if not results_dir_path.is_absolute():
            results_dir_path = Path.cwd() / results_dir

        def _data_tool_executor(name: str, args: dict) -> dict:
            return execute_data_tool(name, args, results_dir_path)

        result = _retry_llm_call(
            lambda: run_prompt_with_tools(
                "code",
                user_text.strip(),
                system_prompt=system_text.rstrip(),
                tools=DATA_TOOL_SCHEMAS,
                tool_choice="auto",
                tool_executor=_data_tool_executor,
                max_tool_rounds=5,
            )
        )
        code = result.text.strip()
        tool_call_log = result.tool_calls
    except (ImportError, Exception) as e:
        last_error = str(e)

    # Fallback: plain run_prompt
    if not code:
        try:
            code = _retry_llm_call(
                lambda: run_prompt("chat", user_text.strip(), system_prompt=system_text.rstrip())
            ).strip()
        except Exception as e:
            last_error = str(e)

    if not code:
        logger.error("Data processor failed after 3 attempts: %s", last_error)
        return {"data_processing_code": "", "data_processing_success": False}

    code = _strip_code_fences(code)

    # Save to run_dir
    run_dir = state.get("run_dir") or ""
    if run_dir:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        (Path(run_dir) / "data_processing_code.py").write_text(code, encoding="utf-8")
        if tool_call_log:
            (Path(run_dir) / "data_processor_tool_calls.json").write_text(
                json.dumps(tool_call_log, indent=2),
                encoding="utf-8",
            )

    return {"data_processing_code": code}


MAX_DATA_PROCESSING_RETRIES = 3  # Total attempts: 1 initial + 2 retries with error feedback


def _run_data_processing_script(code: str, run_dir: str, attempt: int) -> tuple:
    """Execute data processing script. Returns (returncode, stdout, stderr)."""
    script_name = f"data_processing_code_v{attempt}.py" if attempt > 1 else "data_processing_code.py"
    script_path = Path(run_dir) / script_name if run_dir else Path(script_name)
    try:
        script_path.write_text(code, encoding="utf-8")
    except Exception as e:
        return (-1, "", str(e))

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(Path(run_dir)) if run_dir else str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return (result.returncode, result.stdout or "", result.stderr or "")
    except subprocess.TimeoutExpired:
        return (-1, "", "Execution timed out (120s).")
    except Exception as e:
        return (-1, "", str(e))


def _find_processed_data_dir(state: FigurePipelineState) -> Path | None:
    """Locate the processed_data/ directory after script execution."""
    # Check under full_results_dir first (where data processor writes)
    for dir_key in ("full_results_dir", "results_dir"):
        results_dir = state.get(dir_key) or ""
        if not results_dir:
            continue
        results_path = Path(results_dir)
        if not results_path.is_absolute():
            results_path = Path.cwd() / results_dir
        candidate = results_path / "processed_data"
        if candidate.exists():
            return candidate
    # Fallback: check current working directory
    candidate = Path.cwd() / "processed_data"
    if candidate.exists():
        return candidate
    return None


def _scan_processed_data(processed_path: Path) -> tuple:
    """Scan processed_data dir and return (experiment_groups, full catalogs dict)."""
    _DATA_GLOBS = ("*.tsv", "*.csv", "*.json", "*.md", "*.npy", "*.npz", "*.pkl", "*.pt", "*.ckpt")

    try:
        processed_dir_rel = (
            str(processed_path.relative_to(REPO_ROOT))
            if processed_path.is_relative_to(REPO_ROOT)
            else str(processed_path)
        )
    except ValueError:
        processed_dir_rel = str(processed_path)

    experiment_groups: List[dict] = []
    for child in sorted(processed_path.iterdir()):
        if child.is_dir():
            if any(f for g in _DATA_GLOBS for f in child.rglob(g)):
                scan, errors = _scan_data_files(child, processed_path)
                experiment_groups.append(
                    {
                        "name": child.name,
                        "path": f"{processed_dir_rel}/{child.name}",
                        "tree": _build_tree(child, processed_path),
                        "schemas": _build_schemas_from_scan(scan, errors, child),
                        "statistics": _build_statistics_from_scan(scan, errors),
                        "semantics": _build_semantics(child, processed_path),
                    }
                )

    if not experiment_groups:
        scan, errors = _scan_data_files(processed_path, processed_path)
        experiment_groups = [
            {
                "name": "all",
                "path": processed_dir_rel,
                "tree": _build_tree(processed_path, processed_path),
                "schemas": _build_schemas_from_scan(scan, errors, processed_path),
                "statistics": _build_statistics_from_scan(scan, errors),
                "semantics": _build_semantics(processed_path, processed_path),
            }
        ]

    experiment_groups = [_compact_experiment_group(g) for g in experiment_groups]

    full_tree_parts = []
    full_schemas_parts = []
    full_statistics_parts = []
    full_semantics_parts = []
    for g in experiment_groups:
        full_tree_parts.append(f"## Experiment: {g['name']}\n\n{g.get('tree', '')}")
        full_schemas_parts.append(f"## Experiment: {g['name']}\n\n{g.get('schemas', '')}")
        full_statistics_parts.append(f"## Experiment: {g['name']}\n\n{g.get('statistics', '')}")
        full_semantics_parts.append(f"## Experiment: {g['name']}\n\n{g.get('semantics', '')}")

    catalogs = {
        "full_data_tree": _truncate_for_prompt(
            "\n\n---\n\n".join(full_tree_parts),
            _FULL_TREE_CHARS_MAX,
            "full_data_tree",
        ),
        "full_data_schemas": _truncate_for_prompt(
            "\n\n---\n\n".join(full_schemas_parts),
            _FULL_SCHEMAS_CHARS_MAX,
            "full_data_schemas",
        ),
        "full_data_statistics": _truncate_for_prompt(
            "\n\n---\n\n".join(full_statistics_parts),
            _FULL_STATISTICS_CHARS_MAX,
            "full_data_statistics",
        ),
        "full_data_semantics": _truncate_for_prompt(
            "\n\n---\n\n".join(full_semantics_parts),
            _FULL_SEMANTICS_CHARS_MAX,
            "full_data_semantics",
        ),
    }
    return experiment_groups, catalogs


def execute_data_processing_node(state: FigurePipelineState) -> FigurePipelineState:
    """Run data processing code with iterative retry.

    If execution fails, feeds the error back to the LLM to generate a fixed
    version.  Up to MAX_DATA_PROCESSING_RETRIES total attempts.
    """
    mode = (state.get("data_processing_mode") or "regen").strip().lower()
    if mode not in {"regen", "reuse"}:
        mode = "regen"

    code = state.get("data_processing_code") or ""

    run_dir = state.get("run_dir") or ""
    system_text = _load_prompt("data_process_system.md")
    results_dir = state.get("full_results_dir") or state.get("results_dir") or str(RESULTS_DEFAULT)

    def _scan_and_build_output(
        processed_path: Path,
        *,
        stdout: str,
        stderr: str,
        current_code: str,
        reused: bool,
    ) -> tuple[FigurePipelineState | None, str]:
        experiment_groups, catalogs = _scan_processed_data(processed_path)
        _DATA_GLOBS = ("*.tsv", "*.csv", "*.json")
        has_real_data = any(f for g in _DATA_GLOBS for f in processed_path.rglob(g))
        if not has_real_data:
            err = (
                "processed_data/ exists but contains NO output CSV/TSV/JSON files. "
                "The script likely failed to parse the input data."
            )
            return None, err

        out: FigurePipelineState = {
            "data_processing_success": True,
            "data_processing_reused": reused,
            "data_processing_mode": mode,
            "data_processing_code": current_code,
            "data_processing_stdout": stdout,
            "data_processing_stderr": stderr,
            "processed_results_dir": str(processed_path),
            "experiment_groups": experiment_groups,
            "current_experiment_index": 0,
            "current_experiment_name": experiment_groups[0]["name"],
            "results_dir": experiment_groups[0]["path"],
            "data_tree": experiment_groups[0]["tree"],
            "data_schemas": experiment_groups[0]["schemas"],
            "data_statistics": experiment_groups[0]["statistics"],
            "data_semantics": experiment_groups[0]["semantics"],
        }
        out.update(catalogs)
        return out, ""

    if mode == "reuse":
        processed_path = _find_processed_data_dir(state)
        if not processed_path:
            msg = "data-processing-mode=reuse but processed_data/ directory was not found."
            logger.info("Data processing skipped: %s", msg)
            return {
                "data_processing_success": False,
                "data_processing_reused": False,
                "data_processing_mode": mode,
                "data_processing_code": code,
                "data_processing_stdout": "",
                "data_processing_stderr": msg,
            }

        logger.info("Reusing processed data at: %s", processed_path)
        out, err = _scan_and_build_output(
            processed_path,
            stdout="",
            stderr="",
            current_code=code,
            reused=True,
        )
        if out is not None:
            logger.info(
                "Data processing skipped (reuse): %d experiment group(s).", len(out.get("experiment_groups") or [])
            )
            return out

        logger.info("Data processing skipped: %s", err)
        return {
            "data_processing_success": False,
            "data_processing_reused": False,
            "data_processing_mode": mode,
            "data_processing_code": code,
            "data_processing_stdout": "",
            "data_processing_stderr": err,
        }

    if not code.strip():
        return {
            "data_processing_success": False,
            "data_processing_reused": False,
            "data_processing_mode": mode,
        }

    # Clean up stale processed_data/ from a prior run so the scanner starts fresh
    stale_path = _find_processed_data_dir(state)
    if stale_path and stale_path.exists():
        shutil.rmtree(stale_path, ignore_errors=True)

    last_stdout = ""
    last_stderr = ""

    _ensure_gpt()
    from llm import run_prompt

    for attempt in range(1, MAX_DATA_PROCESSING_RETRIES + 1):
        logger.info("Data processing attempt %d/%d...", attempt, MAX_DATA_PROCESSING_RETRIES)
        returncode, stdout, stderr = _run_data_processing_script(code, run_dir, attempt)
        last_stdout = stdout
        last_stderr = stderr

        if returncode == 0:
            # Success — check for processed_data dir
            processed_path = _find_processed_data_dir(state)
            if processed_path:
                logger.info("Re-scanning processed data at: %s", processed_path)
                out, err = _scan_and_build_output(
                    processed_path,
                    stdout=stdout,
                    stderr=stderr,
                    current_code=code,
                    reused=False,
                )
                if out is not None:
                    logger.info(
                        "Data processing complete: %d experiment group(s).", len(out.get("experiment_groups") or [])
                    )
                    return out
                stderr = err
                logger.warning("  %s", stderr)
            else:
                stderr = "Script ran successfully but processed_data/ directory was not created."
                logger.warning("  %s", stderr)
                # Fall through to retry

        # ── Execution failed — ask LLM to fix the code ──────────────
        error_msg = stderr[:2000] if stderr else f"Exit code {returncode}"
        logger.warning("  Attempt %d failed: %s", attempt, error_msg[:200])

        if attempt >= MAX_DATA_PROCESSING_RETRIES:
            break

        # Build retry prompt with error feedback
        retry_prompt = (
            f"The previous data processing script failed with the following error:\n\n"
            f"```\n{error_msg}\n```\n\n"
            f"Previous code:\n```python\n{code}\n```\n\n"
            f"Results root directory: `{results_dir}`\n\n"
            f"Fix the script. Common issues:\n"
            f"- Missing imports (import ALL modules you use: glob, csv, re, etc.)\n"
            f"- Wrong file paths (check the results_dir and use the correct root)\n"
            f"- Incorrect data parsing assumptions\n\n"
            f"Output ONLY the corrected Python code."
        )
        _log_node_io(
            state,
            "execute_data_processing",
            f"prompt_retry_attempt{attempt + 1:02d}",
            {
                "system_prompt": system_text.rstrip(),
                "user_prompt": retry_prompt.strip(),
                "attempt": attempt + 1,
                "max_attempts": MAX_DATA_PROCESSING_RETRIES,
            },
        )
        try:
            fixed_code = run_prompt("chat", retry_prompt.strip(), system_prompt=system_text.rstrip()).strip()
            fixed_code = _strip_code_fences(fixed_code)
            if fixed_code.strip():
                code = fixed_code
                # Save the fixed version
                if run_dir:
                    (Path(run_dir) / f"data_processing_code_v{attempt + 1}.py").write_text(code, encoding="utf-8")
        except Exception as e:
            logger.warning("  LLM retry failed: %s", e)

    # All attempts exhausted — graceful fallback to raw data
    logger.warning("Data processing failed after %d attempts. Using raw data.", MAX_DATA_PROCESSING_RETRIES)
    return {
        "data_processing_success": False,
        "data_processing_reused": False,
        "data_processing_mode": mode,
        "data_processing_code": code,
        "data_processing_stdout": last_stdout,
        "data_processing_stderr": last_stderr,
    }


def data_explorer_node(state: FigurePipelineState) -> FigurePipelineState:
    """LLM + tools: explore datasets and produce a structured report for planning/styling."""
    _ensure_gpt()
    from llm import run_prompt

    system_text = _load_prompt("data_explorer_system.md")
    if not system_text.strip():
        return {"data_exploration_report": ""}

    results_dir = (
        state.get("processed_results_dir")
        or state.get("full_results_dir")
        or state.get("results_dir")
        or str(RESULTS_DEFAULT)
    )
    user_text = _load_prompt(
        "data_explorer_user_template.md",
        proposal=state.get("proposal") or "",
        data_tree=state.get("full_data_tree") or state.get("data_tree") or "",
        data_schemas=state.get("full_data_schemas") or state.get("data_schemas") or "",
        results_dir=results_dir,
    )
    results_dir_path = Path(results_dir)
    if not results_dir_path.is_absolute():
        results_dir_path = Path.cwd() / results_dir
    _log_prompt_input(
        state,
        "data_explorer",
        system_text.rstrip(),
        user_text.strip(),
        model_mode="chat",
        tool_calling=results_dir_path.exists(),
        max_tool_rounds=5,
        results_dir=str(results_dir_path),
    )

    report = ""
    last_error = ""
    tool_call_log: list[dict] = []

    # Prefer tool-calling when results path exists.
    if results_dir_path.exists():
        try:
            from llm import run_prompt_with_tools
            from tools import DATA_TOOL_SCHEMAS, execute_data_tool

            def _data_tool_executor(name: str, args: dict) -> dict:
                return execute_data_tool(name, args, results_dir_path)

            result = _retry_llm_call(
                lambda: run_prompt_with_tools(
                    "chat",
                    user_text.strip(),
                    system_prompt=system_text.rstrip(),
                    tools=DATA_TOOL_SCHEMAS,
                    tool_choice="required",
                    tool_executor=_data_tool_executor,
                    max_tool_rounds=5,
                )
            )
            report = result.text.strip()
            tool_call_log = result.tool_calls
        except (ImportError, Exception) as e:
            last_error = str(e)

    if not report:
        try:
            report = _retry_llm_call(
                lambda: run_prompt("chat", user_text.strip(), system_prompt=system_text.rstrip())
            ).strip()
        except Exception as e:
            last_error = str(e)

    if not report:
        report = f"Data exploration failed after 3 attempts: {last_error}"

    run_dir = state.get("run_dir") or ""
    if run_dir:
        rd = Path(run_dir)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / _orch_art.PLOT_EXPLORATION_REPORT).write_text(report, encoding="utf-8")
        if tool_call_log:
            (rd / "data_explorer_tool_calls.json").write_text(
                json.dumps(tool_call_log, indent=2),
                encoding="utf-8",
            )

    return {"data_exploration_report": report}


def figure_planner_node(state: FigurePipelineState) -> FigurePipelineState:
    """LLM: plan ALL figures at once from proposal + L1+L2+L4 (all experiments). Runs once per pipeline invocation."""
    _ensure_gpt()
    from llm import run_prompt

    system_text = _load_prompt("figure_plan_system.md")

    # Build experiment names list for the delimiter instruction
    groups = state.get("experiment_groups") or []
    experiment_names = ", ".join(g["name"] for g in groups)

    user_text = _load_prompt(
        "figure_plan_user_template.md",
        proposal=state.get("proposal") or "",
        data_tree=state.get("full_data_tree") or state.get("data_tree") or "",
        data_schemas=state.get("full_data_schemas") or state.get("data_schemas") or "",
        data_exploration_report=state.get("data_exploration_report") or "",
        data_semantics=state.get("full_data_semantics") or state.get("data_semantics") or "",
        experiment_names=experiment_names,
    )
    mode_hint = (state.get("planner_mode_hint") or "").strip()
    if mode_hint:
        user_text = user_text.strip() + "\n\n# Mode-specific planning directive\n\n" + mode_hint
    _log_prompt_input(
        state,
        "figure_planner",
        system_text.rstrip(),
        user_text.strip(),
        model_mode="chat",
        experiment_count=len(groups),
    )
    figure_plan = run_prompt("chat", user_text.strip(), system_prompt=system_text.rstrip()).strip()
    # Persist to run_dir root (planner runs once for all experiments)
    run_dir = state.get("run_dir") or ""
    if run_dir:
        p = Path(run_dir)
        p.mkdir(parents=True, exist_ok=True)
        (p / "multi_figure_plan.md").write_text(figure_plan, encoding="utf-8")
    return {"figure_plan": figure_plan, "multi_figure_plan": figure_plan}


def split_plan_node(state: FigurePipelineState) -> FigurePipelineState:
    """Parse multi_figure_plan into per-experiment specs; set figure_plan for the first experiment.

    If the planner emitted more FIGURE_SPECs than experiment groups (e.g., one
    experiment with 3 data sections → 3 specs), expand experiment_groups so
    each spec gets its own figure.
    """
    multi_plan = state.get("multi_figure_plan") or state.get("figure_plan") or ""
    groups = state.get("experiment_groups") or []
    experiment_names = [g["name"] for g in groups]

    per_experiment_specs = _split_multi_figure_plan(multi_plan, experiment_names)

    # Expand groups if planner created more specs than experiments
    groups = _expand_groups_from_specs(per_experiment_specs, groups)
    experiment_names = [g["name"] for g in groups]

    first_name = state.get("current_experiment_name") or (experiment_names[0] if experiment_names else "")
    first_spec = per_experiment_specs.get(first_name, multi_plan)

    out: FigurePipelineState = {
        "multi_figure_plan": multi_plan,
        "per_experiment_specs": per_experiment_specs,
        "figure_plan": first_spec,
    }
    # Update groups and current experiment if expansion happened
    if len(groups) != len(state.get("experiment_groups") or []):
        out["experiment_groups"] = groups
        out["current_experiment_name"] = groups[0]["name"]
        out["current_experiment_index"] = 0
        out["results_dir"] = groups[0]["path"]
        out["data_tree"] = groups[0].get("tree", "")
        out["data_schemas"] = groups[0].get("schemas", "")
        out["data_statistics"] = groups[0].get("statistics", "")
        out["data_semantics"] = groups[0].get("semantics", "")
    return out


def _retry_planner_for_routing(state: FigurePipelineState) -> tuple[str, dict[str, str], List[dict]]:
    """Retry planner once to recover malformed/missing routing metadata."""
    groups = state.get("experiment_groups") or []
    planner_state: FigurePipelineState = {
        "proposal": state.get("proposal", ""),
        "full_data_tree": state.get("full_data_tree", ""),
        "full_data_schemas": state.get("full_data_schemas", ""),
        "full_data_semantics": state.get("full_data_semantics", ""),
        "data_tree": state.get("full_data_tree", ""),
        "data_schemas": state.get("full_data_schemas", ""),
        "data_semantics": state.get("full_data_semantics", ""),
        "data_exploration_report": state.get("data_exploration_report", ""),
        "results_summary": state.get("results_summary", ""),
        "experiment_groups": groups,
        "run_dir": state.get("run_dir", ""),
        "current_experiment_name": "all",
        "iteration": 1,
        "verbose": state.get("verbose", False),
        "verbose_log_dir": state.get("verbose_log_dir", ""),
    }
    plan_result = figure_planner_node(planner_state)
    multi_plan = plan_result.get("multi_figure_plan") or plan_result.get("figure_plan", "")
    experiment_names = [g["name"] for g in groups]
    per_specs = _split_multi_figure_plan(multi_plan, experiment_names)
    expanded_groups = _expand_groups_from_specs(per_specs, groups)
    return multi_plan, per_specs, expanded_groups


def route_figures_node(state: FigurePipelineState) -> FigurePipelineState:
    """Parse per-figure routing metadata and load category-matched few-shots."""
    per_experiment_specs = dict(state.get("per_experiment_specs") or {})
    groups = list(state.get("experiment_groups") or [])
    override_dir = state.get("style_examples_dir")

    def _parse_all(specs: dict[str, str]) -> tuple[dict[str, dict], list[str]]:
        routes: dict[str, dict] = {}
        invalid: list[str] = []
        for name, spec in specs.items():
            routing = _parse_figure_routing(spec)
            routes[name] = routing
            if not routing.get("_routing_valid", True):
                invalid.append(name)
        return routes, invalid

    per_experiment_routes, invalid_routes = _parse_all(per_experiment_specs)
    multi_plan = state.get("multi_figure_plan") or state.get("figure_plan") or ""
    retry_used = False
    if invalid_routes and not state.get("routing_retry_done"):
        retry_used = True
        logger.warning("Routing metadata missing/invalid for %d spec(s). Retrying planner once.", len(invalid_routes))
        multi_plan, per_experiment_specs, groups = _retry_planner_for_routing(state)
        per_experiment_routes, invalid_routes = _parse_all(per_experiment_specs)

    for name in invalid_routes:
        per_experiment_routes[name] = _default_routing(
            f"fallback after {'retry' if retry_used else 'no-retry'}: {per_experiment_routes[name].get('_routing_error', '')}"
        )

    per_experiment_routes = _apply_route_overrides(per_experiment_routes, state)

    if not groups:
        groups = state.get("experiment_groups") or []
    first_name = state.get("current_experiment_name") or (groups[0]["name"] if groups else "")
    first_routing = per_experiment_routes.get(first_name, _default_routing())
    first_spec = per_experiment_specs.get(first_name, multi_plan)
    few_shots = load_routed_few_shots(first_routing, override_dir)

    run_dir = state.get("run_dir", "")
    if run_dir:
        safe_routes = {
            k: {ik: iv for ik, iv in v.items() if not str(ik).startswith("_")} for k, v in per_experiment_routes.items()
        }
        (Path(run_dir) / "routing_decisions.json").write_text(
            json.dumps(safe_routes, indent=2),
            encoding="utf-8",
        )

    out: FigurePipelineState = {
        "multi_figure_plan": multi_plan,
        "per_experiment_specs": per_experiment_specs,
        "figure_plan": first_spec,
        "per_experiment_routes": per_experiment_routes,
        "route_type": first_routing.get("figure_category", "statistical"),
        "route_subcategory": first_routing.get("statistical_subcategory") or "",
        "style_few_shots": few_shots,
        "routing_retry_done": True if retry_used else bool(state.get("routing_retry_done", False)),
    }

    if groups and (
        len(groups) != len(state.get("experiment_groups") or []) or first_name != state.get("current_experiment_name")
    ):
        out["experiment_groups"] = groups
        out["current_experiment_name"] = first_name
        out["current_experiment_index"] = 0
        out["results_dir"] = groups[0]["path"]
        out["data_tree"] = groups[0].get("tree", "")
        out["data_schemas"] = groups[0].get("schemas", "")
        out["data_statistics"] = groups[0].get("statistics", "")
        out["data_semantics"] = groups[0].get("semantics", "")

    return out


def _compose_multi_panel(panel_records: list[dict], script_path: Path, output_path: Path) -> tuple[bool, str, str]:
    """Compose panel PNGs into one figure using a generated matplotlib script."""
    script = (
        "import math\n"
        "import matplotlib.pyplot as plt\n"
        "from matplotlib.gridspec import GridSpec\n\n"
        f"panel_records = {json.dumps(panel_records)}\n"
        f"output_path = {json.dumps(str(output_path))}\n\n"
        "n = max(1, len(panel_records))\n"
        "ncols = 1 if n == 1 else 2\n"
        "nrows = math.ceil(n / ncols)\n"
        "fig = plt.figure(figsize=(3.8 * ncols, 3.0 * nrows), dpi=300)\n"
        "grid = GridSpec(nrows, ncols, figure=fig)\n"
        "for i, rec in enumerate(panel_records):\n"
        "    ax = fig.add_subplot(grid[i // ncols, i % ncols])\n"
        "    img = plt.imread(rec['panel_path'])\n"
        "    ax.imshow(img)\n"
        "    ax.axis('off')\n"
        "    panel_id = rec.get('panel_id', chr(ord('a') + i))\n"
        "    ax.set_title(f'({panel_id})', loc='left', fontsize=10, fontweight='bold')\n"
        "for j in range(len(panel_records), nrows * ncols):\n"
        "    ax = fig.add_subplot(grid[j // ncols, j % ncols])\n"
        "    ax.axis('off')\n"
        "plt.tight_layout(pad=0.2)\n"
        "fig.savefig(output_path, bbox_inches='tight', pad_inches=0.02)\n"
        "print(f'Saved: {output_path}')\n"
    )
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    ok = result.returncode == 0 and output_path.exists()
    return ok, (result.stdout or ""), (result.stderr or "")


def multi_panel_node(state: FigurePipelineState) -> FigurePipelineState:
    """Generate each panel by category, then compose into one multi-panel figure."""
    per_routes = state.get("per_experiment_routes") or {}
    exp_name = state.get("current_experiment_name") or ""
    routing = per_routes.get(exp_name, {})
    panels = routing.get("panels") or []
    if not panels:
        return {"run_success": False, "run_stderr": "No panels in multi_panel routing", "panel_outputs": {}}

    iteration = state.get("iteration") or 1
    panel_records: list[dict] = []
    panel_outputs: dict = {}
    shared_tool_cache: dict[str, dict] = {}
    for idx, panel in enumerate(panels):
        panel_id = panel.get("panel_id") or chr(ord("a") + idx)
        panel_category = panel.get("panel_category", "statistical")
        panel_subcategory = panel.get("statistical_subcategory")
        panel_desc = panel.get("panel_description", "")
        panel_state: FigurePipelineState = dict(state)
        panel_state["current_experiment_name"] = f"{exp_name}_panel_{panel_id}"
        panel_state["figure_paths"] = []
        panel_state["iteration"] = 1
        panel_state["route_type"] = panel_category
        panel_state["data_tool_cache"] = shared_tool_cache
        panel_route = {
            "figure_category": panel_category,
            "statistical_subcategory": panel_subcategory,
        }
        panel_state["style_few_shots"] = load_routed_few_shots(panel_route, state.get("style_examples_dir"))
        panel_plan = (
            state.get("figure_plan") or ""
        ) + f"\n\n## Panel {panel_id}\nCategory: {panel_category}\nDescription: {panel_desc}\n"
        if iteration > 1 and state.get("critic_feedback"):
            panel_plan += "\n\n# Revision guidance\n\n" + (state.get("critic_feedback") or "")
        panel_state["figure_plan"] = panel_plan

        style_result = stylist_node(panel_state)
        panel_state.update(style_result)
        code_result = code_agent_node(panel_state)
        panel_state.update(code_result)
        panel_result = execute_code_node(panel_state)

        panel_path = panel_result.get("figure_path", "")
        panel_outputs[panel_id] = {
            "panel_id": panel_id,
            "panel_category": panel_category,
            "statistical_subcategory": panel_subcategory,
            "panel_description": panel_desc,
            "figure_path": panel_path,
            "success": bool(panel_result.get("run_success", panel_result.get("run_success", True))),
        }
        if panel_path:
            panel_records.append({"panel_id": panel_id, "panel_path": panel_path})

    if not panel_records:
        return {
            "run_success": False,
            "run_stderr": "All panel generations failed for multi_panel route",
            "panel_outputs": panel_outputs,
        }

    adir = _artifact_dir(state)
    compose_script = adir / f"multi_panel_compose_iter{iteration}.py"
    composed_path = adir / f"figure_iter{iteration}.png"
    ok, stdout, stderr = _compose_multi_panel(panel_records, compose_script, composed_path)

    figure_path = ""
    if ok:
        exp_name_safe = state.get("current_experiment_name") or "default"
        mp_run_dir = Path(state.get("run_dir") or str(Path.cwd()))
        final_fig = mp_run_dir / "figures" / f"generated_figure_{exp_name_safe}_multi_panel.png"
        final_fig.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(composed_path, final_fig)
        figure_path = str(final_fig)

    figure_paths = list(state.get("figure_paths") or [])
    if composed_path.exists():
        figure_paths.append(str(composed_path))

    return {
        "styled_figure_spec": state.get("figure_plan", ""),
        "style_spec": state.get("figure_plan", ""),
        "code": compose_script.read_text(encoding="utf-8") if compose_script.exists() else "",
        "figure_path": figure_path or str(composed_path),
        "figure_paths": figure_paths,
        "run_success": ok,
        "run_stdout": stdout,
        "run_stderr": stderr,
        "panel_outputs": panel_outputs,
    }


def _route_after_routing(state: FigurePipelineState) -> Literal["stylist", "multi_panel"]:
    route_type = (state.get("route_type") or "statistical").lower()
    if route_type == "multi_panel":
        return "multi_panel"
    return "stylist"


def _route_after_next_experiment(state: FigurePipelineState) -> Literal["stylist", "multi_panel", "stop"]:
    idx = state.get("current_experiment_index") or 0
    groups = state.get("experiment_groups") or []
    if idx >= len(groups):
        return "stop"
    route_type = (state.get("route_type") or "statistical").lower()
    if route_type == "multi_panel":
        return "multi_panel"
    return "stylist"


def _build_style_few_shot_messages(few_shots: List[dict]) -> list[dict]:
    """Build few-shot message list from style examples for the LLM.

    Each example becomes a user message (description + image) followed by
    an assistant message acknowledging the style reference.
    """
    from llm import encode_image_to_data_url

    messages = []
    for i, shot in enumerate(few_shots, 1):
        desc = shot.get("description", "")
        img_path = shot.get("image_path", "")

        # Build text parts — include full code with framing guidance
        text_parts = [f"Style reference example {i}:\n{desc}"]
        code = shot.get("code")
        if code:
            if len(code) > 3000:
                code = code[:3000] + "\n# ... (truncated)"
            text_parts.append(
                "\nReference code (study structural patterns like subplot creation, "
                "spine removal, legend placement — do NOT copy style values like "
                "font sizes, line widths, figsize, or colors):"
                f"\n```python\n{code}\n```"
            )

        # Build user message with image + description
        user_content: list[dict] = [
            {"type": "input_text", "text": "\n".join(text_parts)},
        ]
        if img_path and Path(img_path).exists():
            try:
                data_url = _safe_image_data_url(Path(img_path), encode_image_to_data_url)
                if data_url:
                    user_content.append({"type": "input_image", "image_url": data_url})
            except Exception:
                pass  # Skip image if encoding fails

        messages.append({"role": "user", "content": user_content})

        # Assistant acknowledgement
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": f"Noted. I will use this as a style reference (example {i})."}
                ],
            }
        )

    return messages


def stylist_node(state: FigurePipelineState) -> FigurePipelineState:
    """LLM: produce a styled figure spec (merged figure plan + style) for ONE experiment.

    Receives one experiment's figure spec (L1+L2+L4 current, L1 all experiments
    for cross-figure consistency), and the research proposal.  Outputs a single
    self-contained ``styled_figure_spec`` that the code agent can implement.

    If few-shot style examples are provided (images + descriptions), they are
    included as reference examples so the LLM can match the target style.
    """
    _ensure_gpt()
    from llm import run_prompt

    system_text = _load_prompt("figure_style_system.md")

    # Build few-shot messages from style examples
    few_shots = state.get("style_few_shots") or []
    few_shot_messages = _build_style_few_shot_messages(few_shots) if few_shots else None

    # Add note about style references to user text if examples exist
    if few_shots:
        n = len(few_shots)
        refs_note = (
            f"\n\n---\n\n**Style references:** {n} example figure(s) were provided above. "
            "Match their visual style (colors, fonts, layout, line weights, etc.) as closely as possible "
            "while following the plan below."
        )
    else:
        refs_note = ""

    user_text = _load_prompt(
        "figure_style_user_template.md",
        figure_plan=state.get("figure_plan") or "",
        data_tree=state.get("data_tree") or "",
        data_schemas=state.get("data_schemas") or "",
        data_exploration_report=state.get("data_exploration_report") or "",
        data_semantics=state.get("data_semantics") or "",
        full_data_tree=state.get("full_data_tree") or "",
        proposal=state.get("proposal") or "",
    )
    user_text = user_text.strip() + refs_note
    _log_prompt_input(
        state,
        "stylist",
        system_text.rstrip(),
        user_text.strip(),
        model_mode="chat",
        few_shot_count=len(few_shots),
    )

    styled_figure_spec = run_prompt(
        "chat",
        user_text,
        system_prompt=system_text.rstrip(),
        few_shot_messages=few_shot_messages,
    ).strip()

    iteration = state.get("iteration") or 1
    _artifact_dir(state).joinpath(f"styled_figure_spec_iter{iteration}.md").write_text(
        styled_figure_spec, encoding="utf-8"
    )
    return {"styled_figure_spec": styled_figure_spec, "style_spec": styled_figure_spec}


def code_agent_node(state: FigurePipelineState) -> FigurePipelineState:
    """LLM: generate Python script from styled_figure_spec.

    Iteration 1: Generate from styled_figure_spec
    Iterations 2-3: Refine based on previous code + critic feedback + previous figure image
    """
    _ensure_gpt()
    from llm import run_prompt, encode_image_to_data_url

    iteration = state.get("iteration") or 1
    system_text = _load_prompt("figure_code_system.md")

    # For iterations 2+, try to load the previous iteration's figure
    previous_image_url: str | None = None
    if iteration > 1:
        prev_iter = iteration - 1
        adir = _artifact_dir(state)
        prev_figure_path = adir / f"figure_iter{prev_iter}.png"
        if prev_figure_path.exists():
            previous_image_url = _safe_image_data_url(prev_figure_path, encode_image_to_data_url)
        else:
            # Fallback: use figure_path from state (beam mode — artifact dirs differ per candidate)
            fig_path = state.get("figure_path", "")
            if fig_path:
                fig_p = Path(fig_path)
                if not fig_p.is_absolute():
                    fig_p = REPO_ROOT / fig_path
                if fig_p.exists():
                    previous_image_url = _safe_image_data_url(fig_p, encode_image_to_data_url)

    # Build style few-shot messages for code agent (iteration 1 only)
    style_few_shot_for_code = None
    if iteration == 1:
        few_shots = state.get("style_few_shots") or []
        if few_shots:
            style_few_shot_for_code = _build_style_few_shot_messages(few_shots[:2])

    # Code agent gets L1+L2+L3 (current experiment only)
    data_catalog_for_code = "\n\n".join(
        filter(
            None,
            [
                "## Directory tree\n" + (state.get("data_tree") or ""),
                "## Schemas\n" + (state.get("data_schemas") or ""),
                "## Numeric statistics\n" + (state.get("data_statistics") or ""),
            ],
        )
    )

    if iteration == 1:
        # First iteration: generate from styled_figure_spec (no previous image)
        user_text = _load_prompt(
            "figure_code_user_template.md",
            styled_figure_spec=state.get("styled_figure_spec") or "",
            results_dir=state.get("results_dir") or str(RESULTS_DEFAULT),
            data_catalog=data_catalog_for_code,
        )
        image_to_send = None
    else:
        # Subsequent iterations: refine based on issue code from critic + critic feedback + previous image
        user_text = _load_prompt(
            "figure_code_refine_template.md",
            styled_figure_spec=state.get("styled_figure_spec") or "",
            issue_code=state.get("issue_code") or "",
            critic_feedback=state.get("critic_feedback") or "",
            results_dir=state.get("results_dir") or str(RESULTS_DEFAULT),
            data_catalog=data_catalog_for_code,
            previous_code=state.get("code") or "",
            execution_error=state.get("run_stderr") or "",
        )
        image_to_send = previous_image_url

    intended_max_tool_rounds = 3 if (state.get("route_type") or "").lower() == "multi_panel" else 5
    _log_prompt_input(
        state,
        "code_agent",
        system_text.rstrip(),
        user_text.strip(),
        model_mode="code",
        iteration=iteration,
        has_previous_image=bool(image_to_send),
        few_shot_count=(len(few_shots[:2]) if (iteration == 1 and few_shots) else 0),
        tool_calling=True,
        max_tool_rounds=intended_max_tool_rounds,
    )

    # ── Try tool-calling path first (LLM can explore data before coding) ──
    code = ""
    last_error = ""
    tool_call_log: list[dict] = []
    try:
        from llm import run_prompt_with_tools
        from tools import DATA_TOOL_SCHEMAS, execute_data_tool

        results_dir_path = Path(state.get("results_dir") or str(RESULTS_DEFAULT))
        if not results_dir_path.is_absolute():
            results_dir_path = Path.cwd() / results_dir_path
        tool_cache = state.get("data_tool_cache")

        def _data_tool_executor(name: str, args: dict) -> dict:
            if isinstance(tool_cache, dict):
                cache_key = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
                cached = tool_cache.get(cache_key)
                if cached is not None:
                    return cached
                result = execute_data_tool(name, args, results_dir_path)
                tool_cache[cache_key] = result
                return result
            return execute_data_tool(name, args, results_dir_path)

        result = _retry_llm_call(
            lambda: run_prompt_with_tools(
                "code",
                user_text.strip(),
                system_prompt=system_text.rstrip(),
                image_base64=image_to_send,
                few_shot_messages=style_few_shot_for_code,
                tools=DATA_TOOL_SCHEMAS,
                tool_choice="auto",
                tool_executor=_data_tool_executor,
                max_tool_rounds=intended_max_tool_rounds,
            )
        )
        code = result.text.strip()
        tool_call_log = result.tool_calls
    except (ImportError, Exception) as e:
        last_error = str(e)

    # Fallback: plain run_prompt with retries
    if not code:
        try:
            code = _retry_llm_call(
                lambda: run_prompt(
                    "code",
                    user_text.strip(),
                    system_prompt=system_text.rstrip(),
                    image_base64=image_to_send,
                    few_shot_messages=style_few_shot_for_code,
                )
            ).strip()
        except Exception as e:
            last_error = str(e)
    if not code:
        return {"code": "", "error": f"Code agent failed after 3 attempts: {last_error}"}

    # Save tool call log to artifacts if verbose
    if tool_call_log and state.get("verbose"):
        _log_node_io(state, "code_agent", "tool_calls", {"tool_calls": tool_call_log})
    # Strip markdown code fence if present
    if code.startswith("```"):
        lines = code.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        code = "\n".join(lines)
    _artifact_dir(state).joinpath(f"code_iter{iteration}.py").write_text(code, encoding="utf-8")
    return {"code": code, "error": ""}


def execute_code_node(state: FigurePipelineState) -> FigurePipelineState:
    """Run generated code in subprocess; capture stdout/stderr and figure path; save figure copy to run_dir."""
    code = state.get("code") or ""
    if not code.strip():
        return {
            "run_success": False,
            "run_stdout": "",
            "run_stderr": "No code to run.",
            "figure_path": "",
            "figure_paths": list(state.get("figure_paths") or []),
        }
    run_dir = Path(state.get("run_dir") or str(Path.cwd()))
    figures_dir = run_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    iteration = state.get("iteration") or 1
    adir = _artifact_dir(state)

    # ── Per-experiment unique figure filename to avoid race conditions ──
    # When experiments run in parallel, each must write to a unique file
    # instead of all sharing "generated_figure.png".
    exp_name = state.get("current_experiment_name") or "default"
    beam_tag = state.get("beam_candidate_tag") or ""
    if beam_tag:
        safe_tag = beam_tag.replace("/", "_")
        unique_fig_name = f"generated_figure_{exp_name}_{safe_tag}.png"
    else:
        unique_fig_name = f"generated_figure_{exp_name}.png"
    code = code.replace("generated_figure.png", unique_fig_name)

    script_path = adir / f"code_iter{iteration}.py"
    # Always overwrite: code may have been patched with unique figure name
    try:
        script_path.write_text(code, encoding="utf-8")
    except Exception as e:
        return {
            "run_success": False,
            "run_stdout": "",
            "run_stderr": str(e),
            "figure_path": "",
            "figure_paths": list(state.get("figure_paths") or []),
        }
    # Record time before execution so glob fallback only finds new files
    _exec_start_time = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(run_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        figure_path = ""
        # Try parsing "Saved: <path>" from output
        for line in stdout.splitlines() + stderr.splitlines():
            m = re.search(r"(?:saved|Saved|figure saved to|output)[:.]?\s*(\S+\.(?:png|pdf|svg))", line, re.IGNORECASE)
            if m:
                figure_path = m.group(1).strip()
                break
        # Fallback: check expected filename
        if not figure_path and (figures_dir / unique_fig_name).exists():
            figure_path = str(figures_dir / unique_fig_name)
        # Fallback: glob for matching figures created after execution started
        if not figure_path:
            import glob as _glob

            safe_exp = exp_name.replace("[", "[[]")
            candidates = [
                p
                for p in _glob.glob(str(figures_dir / f"*{safe_exp}*.png"))
                if Path(p).stat().st_mtime >= _exec_start_time
            ]
            if candidates:
                figure_path = max(candidates, key=lambda p: Path(p).stat().st_mtime)
        # Copy figure to artifact dir for this experiment/iteration
        versioned_path = ""
        preview_src: Path | None = None
        if figure_path:
            src = Path(figure_path)
            if not src.is_absolute():
                src = run_dir / figure_path
            if src.exists():
                preview_src = _find_renderable_image(src)
        if preview_src is None:
            fallback_png = figures_dir / unique_fig_name
            if fallback_png.exists():
                preview_src = fallback_png
        if preview_src is not None:
            dest = adir / f"figure_iter{iteration}.png"
            if _write_preview_png(preview_src, dest):
                versioned_path = str(dest)
        figure_paths = list(state.get("figure_paths") or [])
        if versioned_path:
            figure_paths.append(versioned_path)

        # Determine success: either clean execution OR figure was generated
        # This handles cases where code has warnings but produces valid output
        figure_generated = bool(figure_path or versioned_path)
        execution_clean = result.returncode == 0
        run_success = execution_clean or figure_generated

        return {
            "run_success": run_success,
            "run_stdout": stdout,
            "run_stderr": stderr,
            "figure_path": versioned_path or figure_path,
            "figure_paths": figure_paths,
        }
    except subprocess.TimeoutExpired:
        return {
            "run_success": False,
            "run_stdout": "",
            "run_stderr": "Execution timed out (60s).",
            "figure_path": "",
            "figure_paths": list(state.get("figure_paths") or []),
        }
    except Exception as e:
        return {
            "run_success": False,
            "run_stdout": "",
            "run_stderr": str(e),
            "figure_path": "",
            "figure_paths": list(state.get("figure_paths") or []),
        }


def critic_node(state: FigurePipelineState) -> FigurePipelineState:
    """LLM (vision): evaluate run outcome by looking at the actual figure image."""
    _ensure_gpt()
    from llm import run_prompt, encode_image_to_data_url

    critique_doc = _load_prompt("figure_critique.md")
    agent_instructions = _load_prompt("figure_critique_agent.md")
    system_text = critique_doc.rstrip() + "\n\n" + agent_instructions

    # Critic gets L1+L2 (current experiment only)
    critic_data = "\n\n".join(
        filter(
            None,
            [
                "## Directory tree\n" + (state.get("data_tree") or ""),
                "## Schemas\n" + (state.get("data_schemas") or ""),
            ],
        )
    )
    user_parts = [
        "## Data catalog\n" + critic_data,
        "## Styled figure specification\n" + (state.get("styled_figure_spec") or ""),
        "## Generated code\n```python\n" + (state.get("code") or "") + "\n```",
        "## Run outcome\n"
        + f"Success: {state.get('run_success')}\nStdout: {state.get('run_stdout', '')[:500]}\nStderr: {state.get('run_stderr', '')[:500]}",
    ]
    user_text = "\n\n".join(user_parts)

    # Encode the generated figure for vision input
    # IMPORTANT: Check if figure exists regardless of run_success status
    # Sometimes figures are generated despite warnings/stderr
    image_data_url: str | None = None
    figure_path = state.get("figure_path") or ""
    if figure_path:  # Removed run_success check - let critic decide based on actual image
        fig_p = Path(figure_path)
        if not fig_p.is_absolute():
            fig_p = REPO_ROOT / figure_path
        if fig_p.exists():
            image_data_url = _safe_image_data_url(fig_p, encode_image_to_data_url)

    _log_prompt_input(
        state,
        "critic",
        system_text.rstrip(),
        user_text,
        model_mode="chat",
        has_image_input=bool(image_data_url),
        tool_calling=True,
        tool_choice="required",
        max_tool_rounds=1,
    )

    # ── Try structured tool-calling path first (with retries) ──
    critic_score = 5.0
    verdict = "NEEDS_IMPROVEMENT"
    issue_code = ""
    response = ""
    try:
        from llm import run_prompt_with_tools
        from tools import CRITIC_TOOL_SCHEMAS, execute_critic_tool

        last_tool_error = ""
        for _critic_attempt in range(3):
            try:
                result = run_prompt_with_tools(
                    "chat",
                    user_text,
                    system_prompt=system_text,
                    image_base64=image_data_url,
                    tools=CRITIC_TOOL_SCHEMAS,
                    tool_choice="required",
                    tool_executor=lambda name, args: execute_critic_tool(name, args),
                    max_tool_rounds=1,
                )

                if result.tool_calls:
                    review = result.tool_calls[0]["arguments"]
                    critic_score = review["score"]
                    verdict = review["verdict"]

                    # Build issue_code from structured issues
                    issue_parts = []
                    for issue in review.get("issues", []):
                        part = f"Issue: {issue['description']}"
                        if issue.get("code_snippet"):
                            part += f"\n```\n{issue['code_snippet']}\n```"
                        if issue.get("fix_suggestion"):
                            part += f"\nFix: {issue['fix_suggestion']}"
                        issue_parts.append(part)
                    issue_code = "\n\n".join(issue_parts)

                    # Build human-readable feedback for code agent
                    lines = [f"SCORE: {critic_score}", f"VERDICT: {verdict}", "", "STRENGTHS:"]
                    for s in review.get("strengths", []):
                        lines.append(f"- {s}")
                    lines.append("\nISSUES:")
                    for issue in review.get("issues", []):
                        lines.append(f"- {issue['description']}")
                    if issue_code:
                        lines.append(f"\nISSUE_CODE:\n{issue_code}")
                    response = "\n".join(lines)
                    last_tool_error = ""
                    break  # Success — exit retry loop
                else:
                    # Tool call not made — use text and fall through to regex
                    response = result.text.strip()
                    raise ValueError("No tool call in critic response")
            except Exception as e:
                last_tool_error = str(e)
                if _critic_attempt < 2:
                    time.sleep(2 * (_critic_attempt + 1))

        if last_tool_error:
            raise ValueError(last_tool_error)
    except Exception:
        # Fallback: plain run_prompt + regex parsing
        if not response:
            response = run_prompt("chat", user_text, system_prompt=system_text, image_base64=image_data_url).strip()

        # Parse SCORE: X.X
        score_match = re.search(r"SCORE:\s*([\d]+(?:\.[\d]+)?)", response)
        critic_score = float(score_match.group(1)) if score_match else 5.0

        # Parse VERDICT
        if "VERDICT: NEEDS_IMPROVEMENT" in response.upper():
            verdict = "NEEDS_IMPROVEMENT"
        elif "VERDICT: ACCEPT" in response.upper():
            verdict = "ACCEPT"
        else:
            _fig_threshold = load_pipeline_config().get("scoring", {}).get("figure_score_threshold", 9.0)
            verdict = "ACCEPT" if critic_score >= _fig_threshold else "NEEDS_IMPROVEMENT"

        # Parse ISSUE_CODE section
        issue_code = ""
        issue_code_match = re.search(r"ISSUE_CODE:\s*\n(.*?)(?=\nVERDICT:|\Z)", response, re.DOTALL)
        if issue_code_match:
            issue_code = issue_code_match.group(1).strip()

    iteration = state.get("iteration") or 1
    _artifact_dir(state).joinpath(f"critic_feedback_iter{iteration}.md").write_text(response, encoding="utf-8")
    out: FigurePipelineState = {
        "critic_feedback": response,
        "critic_verdict": verdict,
        "critic_score": critic_score,
        "issue_code": issue_code,
    }
    if verdict == "NEEDS_IMPROVEMENT":
        out["iteration"] = iteration + 1
    return out


_STYLE_KEYWORDS = re.compile(
    r"\b(palette|color[_ ]map|color[_ ]scheme|layout|sizing|font[_ ]size|figsize|figure[_ ]size)\b",
    re.IGNORECASE,
)


def should_continue(
    state: FigurePipelineState,
) -> Literal["stylist", "code_agent", "multi_panel", "next_experiment"]:
    """Loop by route-specific generator when critic requests improvement, else advance."""
    iteration = state.get("iteration") or 1
    verdict = (state.get("critic_verdict") or "").upper()
    if iteration < MAX_ITERATIONS and "NEEDS_IMPROVEMENT" in verdict:
        route_type = (state.get("route_type") or "statistical").lower()
        if route_type == "multi_panel":
            return "multi_panel"
        # For statistical/visualization: route through stylist if critic flagged style issues
        feedback = state.get("critic_feedback") or ""
        if route_type in ("statistical", "visualization") and _STYLE_KEYWORDS.search(feedback):
            return "stylist"
        return "code_agent"
    return "next_experiment"


def next_experiment_node(state: FigurePipelineState) -> FigurePipelineState:
    """Advance to next experiment; load its figure spec from per_experiment_specs and reset iteration."""
    groups = state.get("experiment_groups") or []
    idx = (state.get("current_experiment_index") or 0) + 1
    out: FigurePipelineState = {"current_experiment_index": idx}
    if idx < len(groups):
        ex = groups[idx]
        per_experiment_specs = state.get("per_experiment_specs") or {}
        per_experiment_routes = state.get("per_experiment_routes") or {}
        routing = per_experiment_routes.get(ex["name"], _default_routing())
        out["current_experiment_name"] = ex["name"]
        out["results_dir"] = ex["path"]
        out["data_tree"] = ex.get("tree", "")
        out["data_schemas"] = ex.get("schemas", "")
        out["data_statistics"] = ex.get("statistics", "")
        out["data_semantics"] = ex.get("semantics", "")
        out["figure_plan"] = per_experiment_specs.get(ex["name"], "")
        out["route_type"] = routing.get("figure_category", "statistical")
        out["route_subcategory"] = routing.get("statistical_subcategory") or ""
        out["style_few_shots"] = load_routed_few_shots(routing, state.get("style_examples_dir"))
        out["iteration"] = 1
        # Clear per-iteration outputs so next experiment starts fresh
        out["styled_figure_spec"] = ""
        out["style_spec"] = ""
        out["code"] = ""
        out["critic_feedback"] = ""
        out["issue_code"] = ""
        out["figure_path"] = ""
        out["run_success"] = False
        out["run_stdout"] = ""
        out["run_stderr"] = ""
        out["critic_verdict"] = ""
        out["panel_outputs"] = {}
    return out


def should_continue_experiments(
    state: FigurePipelineState,
) -> Literal["stylist", "multi_panel", "stop"]:
    """Route next experiment to the correct branch by route_type."""
    return _route_after_next_experiment(state)


def plan_all_experiments_node(state: FigurePipelineState) -> FigurePipelineState:
    """Plan all experiments: run planner ONCE with all data, split, then style each.

    1. Call figure_planner_node once with all experiments' data (L1+L2+L4).
    2. Split the multi-figure plan into per-experiment specs.
    3. Run stylist_node per experiment (each gets one spec + full data context).
    """
    groups = state.get("experiment_groups") or []
    experiment_names = [g["name"] for g in groups]
    verbose = state.get("verbose", False)
    verbose_log_dir = state.get("verbose_log_dir", "")

    # Step 1: Run planner ONCE with full data (L1+L2+L4 all experiments)
    _status_print(f"\n=== Planning all {len(groups)} experiments at once ===")
    planner_state: FigurePipelineState = {
        "proposal": state.get("proposal", ""),
        "full_data_tree": state.get("full_data_tree", ""),
        "full_data_schemas": state.get("full_data_schemas", ""),
        "full_data_semantics": state.get("full_data_semantics", ""),
        "data_tree": state.get("full_data_tree", ""),
        "data_schemas": state.get("full_data_schemas", ""),
        "data_semantics": state.get("full_data_semantics", ""),
        "data_exploration_report": state.get("data_exploration_report", ""),
        "results_summary": state.get("results_summary", ""),
        "experiment_groups": groups,
        "run_dir": state.get("run_dir", ""),
        "current_experiment_name": "all",
        "iteration": 1,
        "verbose": verbose,
        "verbose_log_dir": verbose_log_dir,
        "run_mode": state.get("run_mode", ""),
        "planner_mode_hint": state.get("planner_mode_hint", ""),
        "force_route": state.get("force_route", ""),
        "prefer_multi_panel": bool(state.get("prefer_multi_panel", False)),
    }
    _log_node_io(planner_state, "figure_planner", "input", dict(planner_state))
    plan_result = figure_planner_node(planner_state)
    _log_node_io(planner_state, "figure_planner", "output", plan_result)
    multi_figure_plan = plan_result.get("multi_figure_plan") or plan_result.get("figure_plan", "")

    # Step 2: Split plan
    per_experiment_specs = _split_multi_figure_plan(multi_figure_plan, experiment_names)
    groups = _expand_groups_from_specs(per_experiment_specs, groups)

    # Step 3: Parse routing for each experiment; retry planner once if missing/malformed
    per_experiment_routes = {name: _parse_figure_routing(spec) for name, spec in per_experiment_specs.items()}
    invalid_routes = [n for n, r in per_experiment_routes.items() if not r.get("_routing_valid", True)]
    if invalid_routes:
        _status_print(
            f"WARNING: Routing metadata missing/invalid for {len(invalid_routes)} spec(s). Retrying planner once."
        )
        multi_figure_plan, per_experiment_specs, groups = _retry_planner_for_routing(state)
        per_experiment_routes = {name: _parse_figure_routing(spec) for name, spec in per_experiment_specs.items()}
        for n, r in list(per_experiment_routes.items()):
            if not r.get("_routing_valid", True):
                per_experiment_routes[n] = _default_routing(f"fallback after retry: {r.get('_routing_error', '')}")
    per_experiment_routes = _apply_route_overrides(per_experiment_routes, state)

    # Step 4: Prepare each experiment state. Style only statistical/visualization routes.
    experiment_states = []
    for idx, group in enumerate(groups):
        route = per_experiment_routes.get(group["name"], _default_routing())
        route_type = route.get("figure_category", "statistical")
        route_subcategory = route.get("statistical_subcategory") or ""
        _status_print(f"\n--- Preparing experiment {idx + 1}/{len(groups)}: {group['name']} (route={route_type}) ---")
        _report_progress(group["name"], "stylist", 1)
        exp_spec = per_experiment_specs.get(group["name"], multi_figure_plan)

        exp_state: FigurePipelineState = {
            "proposal": state.get("proposal", ""),
            "results_dir": group["path"],
            "data_tree": group.get("tree", ""),
            "data_schemas": group.get("schemas", ""),
            "data_statistics": group.get("statistics", ""),
            "data_semantics": group.get("semantics", ""),
            "full_data_tree": state.get("full_data_tree", ""),
            "full_data_schemas": state.get("full_data_schemas", ""),
            "full_data_semantics": state.get("full_data_semantics", ""),
            "data_exploration_report": state.get("data_exploration_report", ""),
            "figure_plan": exp_spec,
            "multi_figure_plan": multi_figure_plan,
            "per_experiment_specs": per_experiment_specs,
            "current_experiment_name": group["name"],
            "current_experiment_index": idx,
            "run_dir": state.get("run_dir", ""),
            "iteration": 1,
            "verbose": verbose,
            "verbose_log_dir": verbose_log_dir,
            "route_type": route_type,
            "route_subcategory": route_subcategory,
            "per_experiment_routes": per_experiment_routes,
            "style_few_shots": load_routed_few_shots(route, state.get("style_examples_dir")),
            "panel_outputs": {},
        }

        if route_type in {"statistical", "visualization"}:
            _log_node_io(exp_state, "stylist", "input", dict(exp_state))
            style_result = stylist_node(exp_state)
            _log_node_io(exp_state, "stylist", "output", style_result)
            exp_state.update(style_result)
        else:
            exp_state["styled_figure_spec"] = exp_spec
            exp_state["style_spec"] = exp_spec

        experiment_states.append(exp_state)
        _status_print(f"✓ Prepared {group['name']}")

    return {
        "experiment_states": experiment_states,
        "all_plans_created": True,
        "multi_figure_plan": multi_figure_plan,
        "per_experiment_specs": per_experiment_specs,
        "per_experiment_routes": per_experiment_routes,
    }


def execute_experiment_iteration(exp_state: dict, iteration: int) -> dict:
    """Execute one iteration of code generation, execution, and critique for a single experiment.

    Args:
        exp_state: Experiment state dict
        iteration: Current iteration number (1-based)

    Returns:
        Updated experiment state dict
    """
    exp_state["iteration"] = iteration
    name = exp_state.get("current_experiment_name", "unknown")
    route_type = (exp_state.get("route_type") or "statistical").lower()

    if route_type in {"statistical", "visualization"}:
        _report_progress(name, "code_agent", iteration)
        _log_node_io(exp_state, "code_agent", "input", dict(exp_state))
        code_result = code_agent_node(exp_state)
        _log_node_io(exp_state, "code_agent", "output", code_result)
        exp_state.update(code_result)

        if not exp_state.get("code", "").strip():
            error_msg = exp_state.get("error", "Unknown error")
            _status_print(f"  ✗ Code generation failed: {error_msg}")
            _report_progress(name, "code_agent_failed", iteration, error=error_msg)
            exp_state["critic_feedback"] = f"CODE GENERATION FAILED: {error_msg}. Regenerate the complete script."
            exp_state["critic_verdict"] = "NEEDS_IMPROVEMENT"
            exp_state["critic_score"] = 0.0
            return exp_state

        _report_progress(name, "execute_code", iteration)
        _log_node_io(exp_state, "execute_code", "input", dict(exp_state))
        exec_result = execute_code_node(exp_state)
        _log_node_io(exp_state, "execute_code", "output", exec_result)
        exp_state.update(exec_result)
        if not exp_state.get("run_success"):
            _status_print(f"  ✗ Execution failed: {exp_state.get('run_stderr', '')[:100]}")
    elif route_type == "multi_panel":
        _report_progress(name, "multi_panel", iteration)
        _log_node_io(exp_state, "multi_panel", "input", dict(exp_state))
        panel_result = multi_panel_node(exp_state)
        _log_node_io(exp_state, "multi_panel", "output", panel_result)
        exp_state.update(panel_result)
    else:
        exp_state["run_success"] = False
        exp_state["run_stderr"] = f"Unsupported route_type: {route_type}"
        exp_state["styled_figure_spec"] = exp_state.get("figure_plan", "")

    # Critique
    _report_progress(name, "critic", iteration)
    _log_node_io(exp_state, "critic", "input", dict(exp_state))
    critic_result = critic_node(exp_state)
    _log_node_io(exp_state, "critic", "output", critic_result)
    exp_state.update(critic_result)

    verdict = exp_state.get("critic_verdict", "")
    score = exp_state.get("critic_score", 0)
    _status_print(f"  Iteration {iteration}: {verdict} (score: {score})")
    _report_progress(name, "critic_done", iteration, verdict=verdict, score=score)

    return exp_state


def execute_all_experiments_parallel_node(state: FigurePipelineState) -> FigurePipelineState:
    """Execute all planned experiments in parallel, each with up to MAX_ITERATIONS iterations.

    Each experiment runs: code -> execute -> critic (loop up to MAX_ITERATIONS times)
    """
    experiment_states = state.get("experiment_states") or []
    if not experiment_states:
        return {"error": "No experiment states found for parallel execution"}

    _status_print(f"\n=== Executing {len(experiment_states)} experiments in parallel ===")

    def process_experiment(exp_state: dict) -> dict:
        """Process a single experiment through all iterations."""
        name = exp_state.get("current_experiment_name", "unknown")
        _status_print(f"\n[{name}] Starting execution...")

        try:
            for iteration in range(1, MAX_ITERATIONS + 1):
                exp_state = execute_experiment_iteration(exp_state, iteration)

                # Check if we should continue
                verdict = exp_state.get("critic_verdict", "")
                if iteration >= MAX_ITERATIONS or "ACCEPT" in verdict.upper():
                    _status_print(f"[{name}] Completed after {iteration} iteration(s)")
                    break
        except Exception as e:
            _status_print(f"[{name}] FATAL ERROR: {e}")
            exp_state["error"] = f"Experiment failed: {e}"

        return exp_state

    # Execute all experiments in parallel (preserve submission order)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(experiment_states)) as executor:
        futures = [executor.submit(process_experiment, exp_state) for exp_state in experiment_states]
        updated_states = [f.result() for f in futures]

    # Collect all figure paths
    all_figure_paths = []
    for exp_state in updated_states:
        all_figure_paths.extend(exp_state.get("figure_paths", []))

    _status_print("\n=== All experiments completed ===")
    _status_print(f"Generated {len(all_figure_paths)} figures total")

    return {
        "experiment_states": updated_states,
        "figure_paths": all_figure_paths,
    }


# ── Beam search support ───────────────────────────────────────────────

_STYLE_VARIATION_HINTS = [
    # Variant 0: original styled approach (uses few-shot style examples if available)
    {"hint": "", "generic": False},
    # Variant 1: generic scientific style (no few-shot references, standard palette)
    {
        "hint": (
            "\n\n<!-- STYLE_VARIATION: Use a GENERIC, clean scientific figure style. "
            "Ignore any style reference images. Instead, use a standard, widely-accepted "
            "scientific figure style: Palette B (Okabe-Ito colorblind-safe), clean white "
            "background, minimal decoration, standard Nature/Science journal conventions. "
            "Prioritize maximum clarity and data-ink ratio over visual flair. -->"
        ),
        "generic": True,
    },
    # Variant 2: alternative palette with compact layout
    {
        "hint": (
            "\n\n<!-- STYLE_VARIATION: Style variant 3. "
            "Use Palette C (NPG at 70% alpha) with a compact layout. "
            "Minimize whitespace; prefer smaller figure size tiers. -->"
        ),
        "generic": False,
    },
    # Variant 3: Tableau10-safe with spacious layout (replaces Earth Tone which
    # had invisible #FEFAE0 on white backgrounds)
    {
        "hint": (
            "\n\n<!-- STYLE_VARIATION: Style variant 4. "
            "Use Palette D (Tableau10-safe: #4E79A7, #F28E2B, #E15759, #76B7B2, "
            "#59A14F, #EDC948, #B07AA1, #FF9DA7) with a spacious, minimalist layout. "
            "Maximize whitespace and prefer the 'medium' figure size tier. "
            "Ensure COLOR_MAP and COLOR_MAP_PYTHON use these exact colors. -->"
        ),
        "generic": False,
    },
]


def _beam_search_experiment(
    exp_state: dict,
    beam_width: int = BEAM_WIDTH,
    style_variants: int = BEAM_STYLE_VARIANTS,
    code_variants: int = BEAM_CODE_VARIANTS,
    beam_iterations: int = BEAM_ITERATIONS,
) -> dict:
    """Run beam search for a single experiment.

    Iteration 1: Generate S style variants x C code variants = S*C candidates.
      - Variant 0: full styled approach (with few-shot style references)
      - Variant 1: generic scientific style (no few-shots, standard palette)
    Iterations 2+: For each of B survivors, generate C refinements = B*C candidates.
    After each iteration, keep top B by critic score.
    Returns the single best candidate's state.
    """
    name = exp_state.get("current_experiment_name", "unknown")
    run_dir = exp_state.get("run_dir", "")
    exp_dir = Path(run_dir) / name
    exp_dir.mkdir(parents=True, exist_ok=True)

    beam_log = [
        f"# Beam Search: {name}",
        "",
        f"Parameters: B={beam_width}, S={style_variants}, C={code_variants}, iters={beam_iterations}",
        "",
    ]

    # ── Iteration 1: Style variants x Code variants ──────────────────

    # Generate S style variants
    # Variant 0 = original styled (with few-shot refs), Variant 1 = generic scientific style
    _report_progress(name, "beam_stylist", 1, total_variants=style_variants)
    style_specs = []
    style_is_generic = []  # Track which variants are generic (no few-shots for code agent)
    for s in range(style_variants):
        variation = _STYLE_VARIATION_HINTS[s] if s < len(_STYLE_VARIATION_HINTS) else _STYLE_VARIATION_HINTS[-1]
        is_generic = variation.get("generic", False)
        hint_text = variation.get("hint", "")
        label = "generic" if is_generic else "styled"
        _status_print(f"  [{name}] Generating style variant {s + 1}/{style_variants} ({label})...")

        variant_state = dict(exp_state)
        if hint_text:
            variant_state["figure_plan"] = (variant_state.get("figure_plan") or "") + hint_text
        # Generic variant: strip few-shot style examples so stylist uses standard conventions
        if is_generic:
            variant_state["style_few_shots"] = []

        style_result = stylist_node(variant_state)
        spec = style_result["styled_figure_spec"]
        style_specs.append(spec)
        style_is_generic.append(is_generic)

        # Save styled spec artifact
        tag = "generic" if is_generic else "styled"
        (exp_dir / f"styled_figure_spec_variant{s + 1}_{tag}.md").write_text(spec, encoding="utf-8")

    # Build S x C candidates
    candidate_states = []
    for s_idx, spec in enumerate(style_specs):
        for c_idx in range(code_variants):
            cand_id = s_idx * code_variants + c_idx
            cand_tag = f"iter1/candidate_{cand_id}"

            cand_state = dict(exp_state)
            cand_state["styled_figure_spec"] = spec
            cand_state["style_spec"] = spec
            cand_state["iteration"] = 1
            cand_state["beam_candidate_tag"] = cand_tag
            cand_state["figure_paths"] = list(exp_state.get("figure_paths") or [])
            # Generic variants: strip few-shot refs so code agent doesn't reference them
            if style_is_generic[s_idx]:
                cand_state["style_few_shots"] = []

            candidate_states.append(cand_state)

    total_cands = len(candidate_states)
    _report_progress(name, "beam_iter_start", 1, total_candidates=total_cands)
    _status_print(f"  [{name}] Iteration 1: running {total_cands} candidates...")

    # Execute all candidates in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(total_cands, 4)) as executor:
        futures = [executor.submit(execute_experiment_iteration, cs, 1) for cs in candidate_states]
        candidates = [f.result() for f in futures]

    # Rank by critic score (tie-break: ACCEPT > NEEDS_IMPROVEMENT)
    candidates.sort(
        key=lambda c: (
            c.get("critic_score", 0),
            1 if "ACCEPT" in (c.get("critic_verdict") or "").upper() else 0,
        ),
        reverse=True,
    )

    beam_log.append(f"## Iteration 1 — {len(candidates)} candidates")
    for i, c in enumerate(candidates):
        beam_log.append(
            f"  {i + 1}. score={c.get('critic_score', 0):.1f} "
            f"verdict={c.get('critic_verdict', '?')} "
            f"tag={c.get('beam_candidate_tag', '?')}"
        )

    survivors = candidates[:beam_width]
    scores_str = ", ".join(f"{s.get('critic_score', 0):.1f}" for s in survivors)
    beam_log.append(f"  -> Kept top {beam_width}: scores=[{scores_str}]")
    beam_log.append("")

    _report_progress(
        name,
        "beam_rank",
        1,
        survivors=beam_width,
        best_score=survivors[0].get("critic_score", 0) if survivors else 0,
    )
    _status_print(f"  [{name}] Iteration 1 ranked. Top scores: [{scores_str}]")

    # Check for early stop
    if "ACCEPT" in (survivors[0].get("critic_verdict") or "").upper():
        beam_log.append(f"## Early stop — best candidate ACCEPTED with score {survivors[0].get('critic_score', 0):.1f}")
    else:
        # ── Iterations 2+: Refine survivors ──────────────────────────
        for iteration in range(2, beam_iterations + 1):
            candidate_states = []
            for b_idx, survivor in enumerate(survivors):
                for c_idx in range(code_variants):
                    cand_id = b_idx * code_variants + c_idx
                    cand_tag = f"iter{iteration}/candidate_{cand_id}"

                    cand_state = dict(survivor)
                    cand_state["beam_candidate_tag"] = cand_tag
                    cand_state["figure_paths"] = list(survivor.get("figure_paths") or [])

                    candidate_states.append(cand_state)

            total_cands = len(candidate_states)
            _report_progress(name, "beam_iter_start", iteration, total_candidates=total_cands)
            _status_print(f"  [{name}] Iteration {iteration}: refining {total_cands} candidates...")

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(total_cands, 4)) as executor:
                futures = [executor.submit(execute_experiment_iteration, cs, iteration) for cs in candidate_states]
                candidates = [f.result() for f in futures]

            candidates.sort(
                key=lambda c: (
                    c.get("critic_score", 0),
                    1 if "ACCEPT" in (c.get("critic_verdict") or "").upper() else 0,
                ),
                reverse=True,
            )

            beam_log.append(f"## Iteration {iteration} — {len(candidates)} candidates")
            for i, c in enumerate(candidates):
                beam_log.append(
                    f"  {i + 1}. score={c.get('critic_score', 0):.1f} "
                    f"verdict={c.get('critic_verdict', '?')} "
                    f"tag={c.get('beam_candidate_tag', '?')}"
                )

            survivors = candidates[:beam_width]
            scores_str = ", ".join(f"{s.get('critic_score', 0):.1f}" for s in survivors)
            beam_log.append(f"  -> Kept top {beam_width}: scores=[{scores_str}]")
            beam_log.append("")

            _report_progress(
                name,
                "beam_rank",
                iteration,
                survivors=beam_width,
                best_score=survivors[0].get("critic_score", 0) if survivors else 0,
            )
            _status_print(f"  [{name}] Iteration {iteration} ranked. Top scores: [{scores_str}]")

            # Early stop if best is ACCEPTED
            if "ACCEPT" in (survivors[0].get("critic_verdict") or "").upper():
                beam_log.append(
                    f"## Early stop at iteration {iteration} — "
                    f"best candidate ACCEPTED with score {survivors[0].get('critic_score', 0):.1f}"
                )
                break

    # ── Pick the best ────────────────────────────────────────────────
    best = survivors[0] if survivors else exp_state
    best_score = best.get("critic_score", 0)
    best_tag = best.get("beam_candidate_tag", "?")

    beam_log.append("## Final Selection")
    beam_log.append(f"  Winner: {best_tag} (score={best_score:.1f}, verdict={best.get('critic_verdict', '?')})")

    # Copy best figure to experiment root as best_figure.png
    best_figure = best.get("figure_path", "")
    if best_figure:
        src = Path(best_figure)
        if not src.is_absolute():
            src = REPO_ROOT / best_figure
        if src.exists():
            dest = exp_dir / "best_figure.png"
            shutil.copy2(src, dest)
            best["best_figure_path"] = str(dest)
            figure_paths = list(best.get("figure_paths") or [])
            figure_paths.append(str(dest))
            best["figure_paths"] = figure_paths

    # Save beam summary
    (exp_dir / "beam_summary.md").write_text("\n".join(beam_log), encoding="utf-8")

    _report_progress(name, "beam_done", 0, best_score=best_score)
    _status_print(f"  [{name}] Beam search complete. Best score: {best_score:.1f}")

    # Clean up beam-specific state fields
    best.pop("beam_candidate_tag", None)

    return best


def plan_beam_experiments_node(state: FigurePipelineState) -> FigurePipelineState:
    """Plan all experiments for beam mode: planner ONCE -> split (no stylist — that's in beam search)."""
    groups = state.get("experiment_groups") or []
    experiment_names = [g["name"] for g in groups]
    verbose = state.get("verbose", False)
    verbose_log_dir = state.get("verbose_log_dir", "")

    # Step 1: Run planner ONCE
    _status_print(f"\n=== Planning all {len(groups)} experiments (beam mode) ===")
    planner_state: FigurePipelineState = {
        "proposal": state.get("proposal", ""),
        "full_data_tree": state.get("full_data_tree", ""),
        "full_data_schemas": state.get("full_data_schemas", ""),
        "full_data_semantics": state.get("full_data_semantics", ""),
        "data_tree": state.get("full_data_tree", ""),
        "data_schemas": state.get("full_data_schemas", ""),
        "data_semantics": state.get("full_data_semantics", ""),
        "data_exploration_report": state.get("data_exploration_report", ""),
        "results_summary": state.get("results_summary", ""),
        "experiment_groups": groups,
        "run_dir": state.get("run_dir", ""),
        "current_experiment_name": "all",
        "iteration": 1,
        "verbose": verbose,
        "verbose_log_dir": verbose_log_dir,
        "run_mode": state.get("run_mode", ""),
        "planner_mode_hint": state.get("planner_mode_hint", ""),
        "force_route": state.get("force_route", ""),
        "prefer_multi_panel": bool(state.get("prefer_multi_panel", False)),
    }
    plan_result = figure_planner_node(planner_state)
    multi_figure_plan = plan_result.get("multi_figure_plan") or plan_result.get("figure_plan", "")

    # Step 2: Split plan
    per_experiment_specs = _split_multi_figure_plan(multi_figure_plan, experiment_names)

    # Expand groups if planner created more specs than experiments
    groups = _expand_groups_from_specs(per_experiment_specs, groups)

    # Step 3: Parse routing and retry planner once if needed
    per_experiment_routes = {name: _parse_figure_routing(spec) for name, spec in per_experiment_specs.items()}
    invalid_routes = [n for n, r in per_experiment_routes.items() if not r.get("_routing_valid", True)]
    if invalid_routes:
        _status_print(
            f"WARNING: Routing metadata missing/invalid for {len(invalid_routes)} spec(s). Retrying planner once."
        )
        multi_figure_plan, per_experiment_specs, groups = _retry_planner_for_routing(state)
        per_experiment_routes = {name: _parse_figure_routing(spec) for name, spec in per_experiment_specs.items()}
        for n, r in list(per_experiment_routes.items()):
            if not r.get("_routing_valid", True):
                per_experiment_routes[n] = _default_routing(f"fallback after retry: {r.get('_routing_error', '')}")
    per_experiment_routes = _apply_route_overrides(per_experiment_routes, state)

    # Step 4: Build experiment states (beam only for statistical/visualization routes)
    experiment_states = []
    for idx, group in enumerate(groups):
        route = per_experiment_routes.get(group["name"], _default_routing())
        exp_state: FigurePipelineState = {
            "proposal": state.get("proposal", ""),
            "results_dir": group["path"],
            "data_tree": group.get("tree", ""),
            "data_schemas": group.get("schemas", ""),
            "data_statistics": group.get("statistics", ""),
            "data_semantics": group.get("semantics", ""),
            "full_data_tree": state.get("full_data_tree", ""),
            "full_data_schemas": state.get("full_data_schemas", ""),
            "full_data_semantics": state.get("full_data_semantics", ""),
            "data_exploration_report": state.get("data_exploration_report", ""),
            "figure_plan": per_experiment_specs.get(group["name"], multi_figure_plan),
            "multi_figure_plan": multi_figure_plan,
            "per_experiment_specs": per_experiment_specs,
            "current_experiment_name": group["name"],
            "current_experiment_index": idx,
            "run_dir": state.get("run_dir", ""),
            "iteration": 1,
            "verbose": verbose,
            "verbose_log_dir": verbose_log_dir,
            "route_type": route.get("figure_category", "statistical"),
            "route_subcategory": route.get("statistical_subcategory") or "",
            "per_experiment_routes": per_experiment_routes,
            "style_few_shots": load_routed_few_shots(route, state.get("style_examples_dir")),
            "figure_paths": [],
            "panel_outputs": {},
            # Beam parameters
            "beam_width": state.get("beam_width", BEAM_WIDTH),
            "beam_style_variants": state.get("beam_style_variants", BEAM_STYLE_VARIANTS),
            "beam_code_variants": state.get("beam_code_variants", BEAM_CODE_VARIANTS),
            "beam_iterations": state.get("beam_iterations", BEAM_ITERATIONS),
        }
        experiment_states.append(exp_state)

    return {
        "experiment_states": experiment_states,
        "all_plans_created": True,
        "multi_figure_plan": multi_figure_plan,
        "per_experiment_specs": per_experiment_specs,
        "per_experiment_routes": per_experiment_routes,
    }


def execute_beam_all_parallel_node(state: FigurePipelineState) -> FigurePipelineState:
    """Execute beam search for all experiments in parallel."""
    experiment_states = state.get("experiment_states") or []
    if not experiment_states:
        return {"error": "No experiment states found for beam execution"}

    _status_print(f"\n=== Beam search: {len(experiment_states)} experiments in parallel ===")

    def process_experiment_beam(exp_state: dict) -> dict:
        name = exp_state.get("current_experiment_name", "unknown")
        route_type = (exp_state.get("route_type") or "statistical").lower()
        try:
            if route_type not in {"statistical", "visualization"}:
                _status_print(f"  [{name}] Non-beam route ({route_type}); running iterative execution.")
                for iteration in range(1, MAX_ITERATIONS + 1):
                    exp_state = execute_experiment_iteration(exp_state, iteration)
                    verdict = exp_state.get("critic_verdict", "")
                    if iteration >= MAX_ITERATIONS or "ACCEPT" in verdict.upper():
                        break
                if exp_state.get("figure_path"):
                    exp_state["best_figure_path"] = exp_state["figure_path"]
                return exp_state
            return _beam_search_experiment(
                exp_state,
                beam_width=exp_state.get("beam_width", BEAM_WIDTH),
                style_variants=exp_state.get("beam_style_variants", BEAM_STYLE_VARIANTS),
                code_variants=exp_state.get("beam_code_variants", BEAM_CODE_VARIANTS),
                beam_iterations=exp_state.get("beam_iterations", BEAM_ITERATIONS),
            )
        except Exception as e:
            _status_print(f"  [{name}] FATAL ERROR: {e}")
            import traceback

            traceback.print_exc()
            exp_state["error"] = f"Beam search failed: {e}"
            return exp_state

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(experiment_states)) as executor:
        futures = [executor.submit(process_experiment_beam, es) for es in experiment_states]
        updated_states = [f.result() for f in futures]

    # Collect best figure paths
    all_figure_paths = []
    for exp_state in updated_states:
        best_path = exp_state.get("best_figure_path")
        if best_path:
            all_figure_paths.append(best_path)
        else:
            all_figure_paths.extend(exp_state.get("figure_paths", []))

    _status_print("\n=== Beam search complete ===")
    _status_print(f"Best figures: {len(all_figure_paths)}")

    return {
        "experiment_states": updated_states,
        "figure_paths": all_figure_paths,
    }


def build_figure_pipeline_graph():
    """Build the sequential pipeline.

    Flow:
      pre_data_explorer → load_input → data_processor → execute_data_processing → data_explorer → figure_planner
      → split_plan → route_figures
      → [statistical/visualization: stylist → code_agent → execute_code]
      → [multi_panel: multi_panel]
      → critic ↔ route-specific generation loop
      → next_experiment → route next branch or END
    """
    g = StateGraph(FigurePipelineState)

    # Wrap nodes with logging
    g.add_node("pre_data_explorer", _with_logging(pre_data_explorer_node))
    g.add_node("load_input", _with_logging(load_input_node))
    g.add_node("data_processor", _with_logging(data_processor_node))
    g.add_node("execute_data_processing", _with_logging(execute_data_processing_node))
    g.add_node("data_explorer", _with_logging(data_explorer_node))
    g.add_node("figure_planner", _with_logging(figure_planner_node))
    g.add_node("split_plan", _with_logging(split_plan_node))
    g.add_node("route_figures", _with_logging(route_figures_node))
    g.add_node("stylist", _with_logging(stylist_node))
    g.add_node("code_agent", _with_logging(code_agent_node))
    g.add_node("execute_code", _with_logging(execute_code_node))
    g.add_node("multi_panel", _with_logging(multi_panel_node))
    g.add_node("critic", _with_logging(critic_node))
    g.add_node("next_experiment", _with_logging(next_experiment_node))

    g.set_entry_point("pre_data_explorer")
    g.add_edge("pre_data_explorer", "load_input")
    g.add_edge("load_input", "data_processor")
    g.add_edge("data_processor", "execute_data_processing")
    g.add_edge("execute_data_processing", "data_explorer")
    g.add_edge("data_explorer", "figure_planner")
    g.add_edge("figure_planner", "split_plan")
    g.add_edge("split_plan", "route_figures")
    g.add_conditional_edges(
        "route_figures",
        _route_after_routing,
        {
            "stylist": "stylist",
            "multi_panel": "multi_panel",
        },
    )
    g.add_edge("stylist", "code_agent")
    g.add_edge("code_agent", "execute_code")
    g.add_edge("execute_code", "critic")
    g.add_edge("multi_panel", "critic")
    g.add_conditional_edges(
        "critic",
        should_continue,
        {
            "stylist": "stylist",
            "code_agent": "code_agent",
            "multi_panel": "multi_panel",
            "next_experiment": "next_experiment",
        },
    )
    g.add_conditional_edges(
        "next_experiment",
        should_continue_experiments,
        {
            "stylist": "stylist",
            "multi_panel": "multi_panel",
            "stop": END,
        },
    )

    return g.compile()


# Export sequential pipeline (parallel/beam execution handled by pipeline_cli.py subcommands)
app = build_figure_pipeline_graph()
